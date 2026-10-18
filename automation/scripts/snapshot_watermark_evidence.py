"""Bind watermark evidence to immutable report snapshots.

The watermark manifest must never point at a mutable report with a stale hash.
This command copies each selected report to a content-addressed path under the
automation root and rewrites matching manifest evidence to that stable path.
The default is a dry run; ``--commit`` performs atomic writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _matches_report_reference(current: str, report: Path, destination: Path) -> bool:
    """Match the mutable report or any older content-addressed snapshot of it."""

    name = Path(current).name
    if name in {report.name, destination.name}:
        return True
    snapshot_name = re.compile(
        rf"^{re.escape(report.stem)}\.[0-9a-f]{{64}}{re.escape(report.suffix)}$"
    )
    return bool(snapshot_name.fullmatch(name))


def snapshot_evidence(
    manifest_path: Path,
    report_paths: Iterable[Path],
    evidence_dir: Path,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    automation_root = manifest_path.parent.parent
    evidence_dir = evidence_dir.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = payload.get("watermarks")
    if not isinstance(entries, list):
        raise ValueError("manifest watermarks must be a list")

    reports = [path.resolve() for path in report_paths]
    names = [path.name for path in reports]
    if len(names) != len(set(names)):
        raise ValueError("report basenames must be unique")

    operations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for report in reports:
        try:
            content = report.read_bytes()
        except OSError as exc:
            errors.append({"report": str(report), "error": f"unreadable_report: {exc}"})
            continue
        digest = _sha256(content)
        destination = evidence_dir / f"{report.stem}.{digest}{report.suffix}"
        try:
            relative_destination = destination.relative_to(automation_root).as_posix()
        except ValueError:
            errors.append({"report": str(report), "error": "evidence_dir_outside_automation_root"})
            continue
        if destination.exists() and destination.read_bytes() != content:
            errors.append({"report": str(report), "error": "content_address_collision"})
            continue

        matched: list[str] = []
        for entry in entries:
            evidence = entry.get("evidence") if isinstance(entry, dict) else None
            if not isinstance(evidence, dict):
                continue
            current = str(evidence.get("status_report") or "")
            if not _matches_report_reference(current, report, destination):
                continue
            evidence["status_report"] = relative_destination
            evidence["status_report_sha256"] = digest
            matched.append(str(entry.get("source_id") or ""))
        if not matched:
            errors.append({"report": str(report), "error": "no_manifest_entries_matched"})
            continue
        operations.append(
            {
                "report": str(report),
                "sha256": digest,
                "snapshot": relative_destination,
                "matched_entries": len(matched),
                "source_ids": matched,
                "content": content,
                "destination": destination,
            }
        )

    result: dict[str, Any] = {
        "mode": "commit" if commit else "dry_run",
        "manifest": str(manifest_path),
        "requested_reports": len(reports),
        "matched_entries": sum(item["matched_entries"] for item in operations),
        "reports": [
            {key: value for key, value in item.items() if key not in {"content", "destination"}}
            for item in operations
        ],
        "errors": errors,
        "committed": False,
    }
    if errors or not commit:
        return result

    evidence_dir.mkdir(parents=True, exist_ok=True)
    for item in operations:
        destination = item["destination"]
        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            temporary.write_bytes(item["content"])
            os.replace(temporary, destination)
    _atomic_json(manifest_path, payload)
    result["committed"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path, action="append")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = snapshot_evidence(
        args.manifest, args.report, args.evidence_dir, commit=args.commit
    )
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
