from __future__ import annotations

"""Fail-closed retention for immutable observation directories.

Compression and deletion are intentionally separate operations.  A source
directory remains authoritative after compression.  Deletion is permitted
only after both the source tree and its deterministic archive are verified
against an integrity receipt.
"""

import hashlib
import json
import lzma
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Sequence


UTC = timezone.utc
FINAL_OBSERVATION_STATUSES = frozenset({"observed", "completed"})
REFERENCE_SUFFIXES = frozenset({".json", ".jsonl", ".md", ".csv", ".txt", ".toml", ".yaml", ".yml"})


class RetentionSafetyError(RuntimeError):
    """The requested operation cannot prove that it is safe."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Candidate:
    root: str
    path: str
    run_id: str
    status: str
    finished_at: str
    size_bytes: int
    tree_sha256: str
    eligible: bool
    reasons: tuple[str, ...]
    references: tuple[str, ...]
    archive_path: str
    receipt_path: str


@dataclass(frozen=True)
class RetentionPlan:
    mode: str
    dry_run: bool
    candidates: tuple[Candidate, ...]
    selected_count: int
    selected_bytes: int


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _safe_relative(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RetentionSafetyError(f"candidate escapes observation root: {path}") from exc


def _walk_regular_files(run_dir: Path) -> tuple[list[FileRecord], list[str]]:
    files: list[FileRecord] = []
    unsafe: list[str] = []
    for entry in sorted(run_dir.rglob("*"), key=lambda item: item.relative_to(run_dir).as_posix()):
        relative = entry.relative_to(run_dir).as_posix()
        if entry.is_symlink():
            unsafe.append(f"symlink:{relative}")
        elif entry.is_file():
            stat = entry.stat()
            files.append(FileRecord(relative, stat.st_size, _sha256_path(entry)))
        elif entry.is_dir() and not any(entry.iterdir()):
            # The integrity inventory is file based. Refuse trees with empty
            # directories instead of silently dropping that structure.
            unsafe.append(f"empty_directory:{relative}")
        elif not entry.is_dir():
            unsafe.append(f"special_file:{relative}")
    return files, unsafe


def _tree_sha256(files: Sequence[FileRecord]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _archive_paths(archive_root: Path, root: Path, run_dir: Path) -> tuple[Path, Path]:
    root_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    relative = _safe_relative(run_dir, root)
    archive = archive_root.resolve() / root_key / relative.parent / f"{relative.name}.tar.xz"
    return archive, archive.with_suffix(archive.suffix + ".integrity.json")


def _candidate_manifests(observation_roots: Sequence[Path]) -> Iterator[tuple[Path, Path]]:
    seen: set[Path] = set()
    for root in observation_roots:
        resolved_root = root.resolve()
        if not resolved_root.exists():
            continue
        for manifest in sorted(resolved_root.rglob("manifest.json")):
            run_dir = manifest.parent.resolve()
            if run_dir in seen or any(part.startswith(".retention-delete-pending-") for part in run_dir.parts):
                continue
            seen.add(run_dir)
            yield resolved_root, manifest.resolve()


def _reference_files(reference_roots: Sequence[Path], observation_roots: Sequence[Path], archive_root: Path) -> list[Path]:
    found: set[Path] = set()
    archive = archive_root.resolve()
    for root in reference_roots:
        root = root.resolve()
        if not root.exists():
            continue
        iterator = [root] if root.is_file() else root.rglob("*")
        for path in iterator:
            resolved = path.resolve()
            if not path.is_file() or archive == resolved or archive in resolved.parents:
                continue
            if path.suffix.lower() in REFERENCE_SUFFIXES:
                found.add(resolved)
    # Observation manifests can bind parent and shard runs.  A run's own
    # manifest is ignored later so that self-identification is not a reference.
    for root in observation_roots:
        if root.exists():
            found.update(path.resolve() for path in root.rglob("manifest.json") if path.is_file())
    return sorted(found)


def _read_reference_texts(paths: Sequence[Path], max_reference_bytes: int) -> list[tuple[Path, str]]:
    used = 0
    result: list[tuple[Path, str]] = []
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RetentionSafetyError(f"cannot stat reference file {path}: {exc}") from exc
        used += size
        if used > max_reference_bytes:
            raise RetentionSafetyError(
                f"reference scan exceeds limit ({used} > {max_reference_bytes} bytes); raise the bound explicitly"
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RetentionSafetyError(f"cannot read reference file {path}: {exc}") from exc
        result.append((path, text))
    return result


def _sqlite_reference_text(path: Path, max_reference_bytes: int) -> str:
    if not path.exists():
        return ""
    if path.stat().st_size > max_reference_bytes:
        raise RetentionSafetyError(
            f"control database exceeds reference scan limit: {path.stat().st_size} > {max_reference_bytes}"
        )
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        chunks: list[str] = []
        used = 0
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (table,) in tables:
            quoted = '"' + str(table).replace('"', '""') + '"'
            for row in connection.execute(f"SELECT * FROM {quoted}"):
                for value in row:
                    if value is None:
                        continue
                    text = str(value)
                    used += len(text.encode("utf-8", errors="replace"))
                    if used > max_reference_bytes:
                        raise RetentionSafetyError("control database values exceed reference scan limit")
                    chunks.append(text)
        return "\n".join(chunks)
    except (sqlite3.Error, OSError) as exc:
        raise RetentionSafetyError(f"cannot inspect control database {path}: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _references_for(
    run_dir: Path,
    observation_root: Path,
    run_id: str,
    reference_texts: Sequence[tuple[Path, str]],
    state_text: str,
    project_root: Path,
) -> tuple[str, ...]:
    tokens = {run_id, run_dir.name, str(run_dir.resolve())}
    root = observation_root.resolve()
    paths = [run_dir.resolve()]
    ancestor = run_dir.resolve().parent
    # Reports commonly name a country/campaign output root rather than its
    # immutable run leaf. Protect every candidate below that cited root. Stop
    # before the shared observation root so a generic documentation mention
    # does not retain every historical run forever.
    while ancestor != root and root in ancestor.parents:
        paths.append(ancestor)
        ancestor = ancestor.parent
    for path in paths:
        tokens.add(str(path))
        tokens.add(path.name)
        try:
            tokens.add(path.relative_to(project_root.resolve()).as_posix())
        except ValueError:
            pass
        try:
            tokens.add(path.relative_to(root).as_posix())
        except ValueError:
            pass
    tokens = {token for token in tokens if len(token) >= 12}
    references: list[str] = []
    for path, text in reference_texts:
        if path.parent.resolve() == run_dir.resolve() and path.name == "manifest.json":
            continue
        if any(token in text for token in tokens):
            references.append(str(path))
    if any(token in state_text for token in tokens):
        references.append("control_db")
    return tuple(sorted(set(references)))


def build_plan(
    *,
    project_root: Path,
    observation_roots: Sequence[Path],
    archive_root: Path,
    reference_roots: Sequence[Path],
    state_path: Path,
    mode: str = "plan",
    min_age_days: float = 7.0,
    max_runs: int = 10,
    max_input_bytes: int = 5 * 1024**3,
    max_reference_bytes: int = 512 * 1024**2,
    now: datetime | None = None,
) -> RetentionPlan:
    if mode not in {"plan", "compress", "delete_verified"}:
        raise ValueError(f"unsupported retention mode: {mode}")
    if min_age_days < 0 or max_runs < 1 or max_input_bytes < 1 or max_reference_bytes < 1:
        raise ValueError("retention bounds must be positive (min_age_days may be zero)")
    project_root = project_root.resolve()
    roots = tuple(path.resolve() for path in observation_roots)
    archive_root = archive_root.resolve()
    for root in roots:
        if archive_root == root or root in archive_root.parents or archive_root in root.parents:
            raise RetentionSafetyError("archive root must be outside every observation root")
    reference_texts = _read_reference_texts(
        _reference_files(reference_roots, roots, archive_root), max_reference_bytes
    )
    state_text = _sqlite_reference_text(state_path.resolve(), max_reference_bytes)
    cutoff = (now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=min_age_days)
    candidates: list[Candidate] = []

    for root, manifest_path in _candidate_manifests(roots):
        run_dir = manifest_path.parent
        reasons: list[str] = []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            manifest = {}
            reasons.append(f"invalid_manifest:{type(exc).__name__}")
        run_id = str(manifest.get("run_id") or run_dir.name)
        status = str(manifest.get("status") or "")
        finished_raw = str(manifest.get("finished_at") or "")
        finished = _parse_utc(finished_raw)
        if status not in FINAL_OBSERVATION_STATUSES:
            reasons.append(f"nonfinal_status:{status or 'missing'}")
        if finished is None:
            reasons.append("missing_or_invalid_finished_at")
        elif finished > cutoff:
            reasons.append("younger_than_min_age")
        files, unsafe = _walk_regular_files(run_dir)
        reasons.extend(unsafe)
        if not files:
            reasons.append("empty_run")
        if files:
            newest = max(datetime.fromtimestamp((run_dir / item.path).stat().st_mtime, UTC) for item in files)
            if newest > cutoff:
                reasons.append("files_younger_than_min_age")
        references = _references_for(
            run_dir, root, run_id, reference_texts, state_text, project_root,
        )
        if references:
            reasons.append("referenced")
        archive, receipt = _archive_paths(archive_root, root, run_dir)
        candidates.append(Candidate(
            root=str(root), path=str(run_dir), run_id=run_id, status=status,
            finished_at=finished_raw, size_bytes=sum(item.size for item in files),
            tree_sha256=_tree_sha256(files), eligible=not reasons,
            reasons=tuple(sorted(set(reasons))), references=references,
            archive_path=str(archive), receipt_path=str(receipt),
        ))

    # The orchestrator keeps several observation runs under a stable
    # per-source directory.  Keep the newest one in every such directory even
    # when status files are missing or stale, so its latest artifacts can
    # never become retention candidates.
    orchestrator_roots = {
        root for root in roots
        if root.name == "orchestrator" and root.parent.name == "runs"
    }
    latest_by_parent: dict[str, Candidate] = {}
    for item in candidates:
        path = Path(item.path)
        if not any(root == path or root in path.parents for root in orchestrator_roots):
            continue
        key = str(path.parent)
        previous = latest_by_parent.get(key)
        if previous is None or (item.finished_at, item.path) > (previous.finished_at, previous.path):
            latest_by_parent[key] = item
    latest_paths = {item.path for item in latest_by_parent.values()}
    protected: list[Candidate] = []
    for item in candidates:
        reasons = item.reasons
        if item.path in latest_paths:
            reasons = tuple(sorted(set(reasons + ("latest_orchestrator_run",))))
            item = Candidate(**{
                **asdict(item), "eligible": False, "reasons": reasons,
            })
        if mode == "delete_verified" and item.eligible:
            if not Path(item.archive_path).is_file() or not Path(item.receipt_path).is_file():
                item = Candidate(**{
                    **asdict(item), "eligible": False,
                    "reasons": tuple(sorted(set(item.reasons + ("missing_archive_or_receipt",)))),
                })
        protected.append(item)
    candidates = protected

    # Oldest completed runs go first.  Both count and input bytes bound work.
    candidates.sort(key=lambda item: (item.finished_at or "9999", item.path))
    selected_count = 0
    selected_bytes = 0
    selected_paths: set[str] = set()
    for item in candidates:
        if not item.eligible or selected_count >= max_runs:
            continue
        if selected_bytes + item.size_bytes > max_input_bytes:
            continue
        selected_paths.add(item.path)
        selected_count += 1
        selected_bytes += item.size_bytes
    bounded: list[Candidate] = []
    for item in candidates:
        if item.eligible and item.path not in selected_paths:
            item = Candidate(**{**asdict(item), "eligible": False, "reasons": tuple(sorted(set(item.reasons + ("outside_operation_bounds",))))})
        bounded.append(item)
    return RetentionPlan(mode, mode == "plan", tuple(bounded), selected_count, selected_bytes)


def _normalized_tarinfo(name: str, *, directory: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory and not name.endswith("/") else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if directory else 0o644
    return info


def _write_deterministic_archive(source: Path, destination: Path, files: Sequence[FileRecord]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with lzma.LZMAFile(raw, "wb", format=lzma.FORMAT_XZ, preset=6) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                top = source.name
                archive.addfile(_normalized_tarinfo(top, directory=True))
                directories = sorted({PurePosixPath(item.path).parent.as_posix() for item in files} - {"."})
                for relative in directories:
                    archive.addfile(_normalized_tarinfo(f"{top}/{relative}", directory=True))
                for item in files:
                    path = source / item.path
                    info = _normalized_tarinfo(f"{top}/{item.path}", directory=False, size=item.size)
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)


def _verify_archive(archive_path: Path, source_name: str, files: Sequence[FileRecord]) -> None:
    expected = {f"{source_name}/{item.path}": item for item in files}
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:xz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile() or member.name not in expected or member.name in seen:
                    raise RetentionSafetyError(f"unexpected archive member: {member.name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RetentionSafetyError(f"cannot read archive member: {member.name}")
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                wanted = expected[member.name]
                if size != wanted.size or digest.hexdigest() != wanted.sha256:
                    raise RetentionSafetyError(f"archive member integrity mismatch: {member.name}")
                seen.add(member.name)
    except (tarfile.TarError, lzma.LZMAError, OSError) as exc:
        raise RetentionSafetyError(f"cannot verify archive {archive_path}: {exc}") from exc
    missing = sorted(set(expected) - seen)
    if missing:
        raise RetentionSafetyError(f"archive is missing {len(missing)} files")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def compress_candidate(candidate: Candidate) -> dict[str, object]:
    if not candidate.eligible:
        raise RetentionSafetyError(f"candidate is not eligible: {candidate.path}")
    source = Path(candidate.path)
    archive = Path(candidate.archive_path)
    receipt = Path(candidate.receipt_path)
    files, unsafe = _walk_regular_files(source)
    if unsafe or _tree_sha256(files) != candidate.tree_sha256:
        raise RetentionSafetyError("source changed after retention planning")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(f".{archive.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        _write_deterministic_archive(source, temporary, files)
        _verify_archive(temporary, source.name, files)
        after, unsafe_after = _walk_regular_files(source)
        if unsafe_after or _tree_sha256(after) != candidate.tree_sha256:
            raise RetentionSafetyError("source changed while it was being archived")
        archive_sha256 = _sha256_path(temporary)
        os.replace(temporary, archive)
        payload = {
            "schema_version": 1,
            "source_path": str(source.resolve()),
            "source_root": candidate.root,
            "run_id": candidate.run_id,
            "status": candidate.status,
            "finished_at": candidate.finished_at,
            "tree_sha256": candidate.tree_sha256,
            "source_bytes": candidate.size_bytes,
            "archive_path": str(archive.resolve()),
            "archive_sha256": archive_sha256,
            "archive_bytes": archive.stat().st_size,
            "files": [asdict(item) for item in files],
            "created_at": datetime.now(UTC).isoformat(),
        }
        _atomic_json(receipt, payload)
        return payload
    finally:
        temporary.unlink(missing_ok=True)


def _load_verified_receipt(candidate: Candidate) -> tuple[dict[str, object], list[FileRecord]]:
    source = Path(candidate.path).resolve()
    archive = Path(candidate.archive_path).resolve()
    receipt_path = Path(candidate.receipt_path)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        files = [FileRecord(str(item["path"]), int(item["size"]), str(item["sha256"])) for item in receipt["files"]]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RetentionSafetyError(f"invalid or missing integrity receipt: {receipt_path}") from exc
    if (
        receipt.get("schema_version") != 1
        or receipt.get("source_path") != str(source)
        or receipt.get("source_root") != candidate.root
        or receipt.get("run_id") != candidate.run_id
        or receipt.get("tree_sha256") != candidate.tree_sha256
        or receipt.get("archive_path") != str(archive)
    ):
        raise RetentionSafetyError("integrity receipt does not identify the selected source and archive")
    current_files, unsafe = _walk_regular_files(source)
    if unsafe or current_files != files or _tree_sha256(current_files) != receipt["tree_sha256"]:
        raise RetentionSafetyError("source tree no longer matches the integrity receipt")
    if not archive.is_file() or _sha256_path(archive) != receipt.get("archive_sha256"):
        raise RetentionSafetyError("archive digest does not match the integrity receipt")
    _verify_archive(archive, source.name, files)
    return receipt, files


def delete_verified_candidate(candidate: Candidate) -> dict[str, object]:
    if not candidate.eligible:
        raise RetentionSafetyError(f"candidate is not eligible: {candidate.path}")
    receipt, files = _load_verified_receipt(candidate)
    source = Path(candidate.path)
    pending = source.with_name(f".retention-delete-pending-{source.name}-{uuid.uuid4().hex}")
    os.replace(source, pending)
    try:
        moved_files, unsafe = _walk_regular_files(pending)
        if unsafe or moved_files != files or _tree_sha256(moved_files) != receipt["tree_sha256"]:
            raise RetentionSafetyError("source changed at the deletion boundary")
        shutil.rmtree(pending)
    except Exception:
        if pending.exists() and not source.exists():
            os.replace(pending, source)
        raise
    deletion = {
        "schema_version": 1,
        "run_id": candidate.run_id,
        "source_path": str(source.resolve()),
        "tree_sha256": receipt["tree_sha256"],
        "archive_path": receipt["archive_path"],
        "archive_sha256": receipt["archive_sha256"],
        "deleted_at": datetime.now(UTC).isoformat(),
    }
    deletion_path = Path(candidate.receipt_path).with_suffix(".deletion.json")
    _atomic_json(deletion_path, deletion)
    return deletion


def execute_plan(plan: RetentionPlan) -> list[dict[str, object]]:
    if plan.mode == "plan":
        return []
    results: list[dict[str, object]] = []
    for candidate in plan.candidates:
        if not candidate.eligible:
            continue
        if plan.mode == "compress":
            results.append(compress_candidate(candidate))
        elif plan.mode == "delete_verified":
            results.append(delete_verified_candidate(candidate))
    return results


def plan_as_dict(plan: RetentionPlan) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": plan.mode,
        "dry_run": plan.dry_run,
        "selected_count": plan.selected_count,
        "selected_bytes": plan.selected_bytes,
        "candidates": [asdict(item) for item in plan.candidates],
    }
