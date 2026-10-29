#!/usr/bin/env python3
"""Run a hash-pinned unattended sequence of exact and discovery-only lanes.

The controller accepts only immutable observation plans with
``production_writes=false``.  A phase can contain bounded discovery-only lanes
or exact URL lanes.  Phases are barriers; failures are isolated and later
lanes continue.  At most two network workers and zero browser workers are
admitted across the sequence.

Exact lanes reuse ``scripts/parallel_observe_pure.py`` with a pinned private
URL file and pinned parent artifacts.  They never rediscover a source.
Discovery lanes reuse ``ops/run_pure_discovery_diagnostic.py`` and call only
``discover_urls(day, day)``.  Every child process has a continuously drained
50 MiB log cap, a bounded timeout, pre/post pin verification, process-group
quiescence proof, and exclusive start/terminal receipts.

Terminal observations are review evidence only.  This controller cannot
promote data, advance tracker boundaries, or invoke a production pipeline.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

try:
    from .host_capacity_guard import collect_metrics, evaluate_capacity
except ImportError:  # direct script execution
    from host_capacity_guard import collect_metrics, evaluate_capacity


UTC = timezone.utc
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORBIDDEN_IMPORTS = frozenset({"pymongo", "mongoengine", "motor"})
FORBIDDEN_CALLS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many",
    "delete_one", "delete_many", "bulk_write",
})
OBSERVER_LOG_MAX_BYTES = 50 * 1024 * 1024
NETWORK_CEILING = 2
BROWSER_CEILING = 0
MAX_WALL_SECONDS = 43_200
PYTHON_NO_BYTECODE_FLAG = "-B"
PYTHON_DONT_WRITE_BYTECODE = "PYTHONDONTWRITEBYTECODE"
CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
})
PARENT_ARTIFACTS = (
    "manifest.json", "urls.jsonl", "field_checks.jsonl", "issues.jsonl",
)
REVIEWED_REDIRECT_EXACT_SETS = {
    "mlp:LBR:liberianinvestigator.com": (
        853, "4fa1f52f7a97307618b8f58c271005e6013f0bee2f6a5d322646a33ff752118f",
    ),
    "mlp:MWI:247malawi.com": (
        780, "c5e788933d43fdd8baba920310acbdee0dba823f83beb21259e73aa552053563",
    ),
    "mlp:PAN:newsroompanama.com": (
        1048, "d8f100249b71bcadc9f00eed26496e0b9a4bd0d5d5f3519b86f992051fa80997",
    ),
    "mlp:SLB:sibconline.com.sb": (
        203, "107bd7da22cb4712142a586944c738294304ae367ed293e06b5f4e6ce105efce",
    ),
    "mlp:SSD:eyeradio.org": (
        802, "9d1560a75c2aad6cf10ceec2f4b5eec0df5b5691a9040cbe709bf8d599b03b9a",
    ),
    "mlp:UGA:eagle.co.ug": (
        332, "a2a413417e04f1842505ddcc7f58dd3ce8be806b45ef0d7fdb56801ff2d84ffd",
    ),
}


class PlanError(ValueError):
    """An immutable sequence plan is unsafe or inconsistent."""


class ControllerAlreadyRunning(RuntimeError):
    """Another exact-observation sequence owns the host lock."""


@dataclass(frozen=True)
class FilePin:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ParentPin:
    run_dir: Path
    artifacts: Mapping[str, str]


@dataclass(frozen=True)
class SealedClassificationParentPin:
    handoff_manifest: FilePin
    worklist: FilePin
    classification_seal: FilePin
    independent_audit: FilePin
    source_summary: FilePin
    classified_source_ledger: FilePin
    preserved_passes: FilePin
    verified_exclusions: FilePin


@dataclass(frozen=True)
class LaneSpec:
    lane_id: str
    phase_id: str
    kind: str
    source_id: str
    adapter: FilePin
    start: date
    end: date
    workers: int
    timeout_seconds: int
    expected_requests: Optional[Mapping[str, int]]
    exact_input: Optional[FilePin]
    exact_input_count: Optional[int]
    parent: Optional[ParentPin]
    sealed_classification_parent: Optional[SealedClassificationParentPin]
    derivation_reason: Optional[str]
    acceptance: Mapping[str, object]
    repair_class: Optional[str]


@dataclass(frozen=True)
class PhaseSpec:
    phase_id: str
    max_active_lanes: int
    lanes: Tuple[LaneSpec, ...]


@dataclass(frozen=True)
class SequencePlan:
    sequence_id: str
    plan_sha256: str
    code_git_commit: str
    execution_files: Tuple[FilePin, ...]
    phases: Tuple[PhaseSpec, ...]
    max_wall_seconds: int
    log_max_bytes: int


@dataclass(frozen=True)
class RuntimePaths:
    python: Path
    deployment_root: Path
    automation_root: Path
    output_root: Path
    receipt_root: Path
    host_lock_path: Path
    allowed_parent_roots: Tuple[Path, ...]


@dataclass(frozen=True)
class LaneResult:
    state: str
    receipt: Mapping[str, object]


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_sha256(command: Sequence[str]) -> str:
    return sha256_bytes(b"\0".join(value.encode("utf-8") for value in command))


def pin_bundle_sha256(pins: Iterable[FilePin]) -> str:
    return sha256_bytes(
        "".join("%s\n" % pin.sha256 for pin in sorted(pins, key=lambda item: item.sha256)).encode(
            "utf-8"
        )
    )


def target_url_sha256(lines: Iterable[str]) -> str:
    values = dict.fromkeys(line.strip() for line in lines if line.strip())
    return sha256_bytes("".join("%s\n" % value for value in values).encode("utf-8"))


def json_no_duplicates(text: str) -> object:
    def pairs(values: List[Tuple[str, object]]) -> Dict[str, object]:
        result = {}  # type: Dict[str, object]
        for key, value in values:
            if key in result:
                raise PlanError("duplicate JSON key: %s" % key)
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise PlanError("invalid JSON") from exc


def load_object(path: Path) -> Dict[str, object]:
    value = json_no_duplicates(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PlanError("JSON object required")
    return value


def write_json_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(".%s.%s.%s.tmp" % (
        path.name, os.getpid(), threading.get_ident(),
    ))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def append_event(path: Path, kind: str, **values: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at_utc": iso_now(), "kind": kind, **values}, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")[:80]
    return "%s-%s" % (clean, sha256_bytes(value.encode("utf-8"))[:10])


def parse_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanError("%s must be an ISO date" % field) from exc


def positive_int(value: object, field: str, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise PlanError("%s must be an integer" % field)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanError("%s must be an integer" % field) from exc
    if result < 1 or (maximum is not None and result > maximum):
        raise PlanError("%s is outside its reviewed bound" % field)
    return result


def resolve_deployment_file(raw: object, root: Path, field: str) -> Path:
    value = str(raw or "")
    candidate = Path(value)
    if not value or candidate.is_absolute():
        raise PlanError("%s must be deployment-root-relative" % field)
    resolved = (root / candidate).resolve()
    base = root.resolve()
    if resolved != base and base not in resolved.parents:
        raise PlanError("%s escapes the deployment root" % field)
    return resolved


def file_pin(value: object, root: Path, field: str) -> FilePin:
    if not isinstance(value, Mapping):
        raise PlanError("%s must be a file pin" % field)
    digest = str(value.get("sha256", ""))
    if not HEX64.fullmatch(digest):
        raise PlanError("%s has an invalid SHA-256" % field)
    return FilePin(resolve_deployment_file(value.get("path"), root, field), digest)


def parent_pin(value: object, allowed_roots: Sequence[Path], field: str) -> ParentPin:
    if not isinstance(value, Mapping):
        raise PlanError("%s must be a parent pin" % field)
    raw = Path(str(value.get("run_dir", "")))
    if not raw.is_absolute():
        raise PlanError("%s.run_dir must be absolute" % field)
    resolved = raw.resolve()
    roots = tuple(item.resolve() for item in allowed_roots)
    if not roots or not any(resolved == root or root in resolved.parents for root in roots):
        raise PlanError("%s.run_dir is outside allowed parent roots" % field)
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(PARENT_ARTIFACTS):
        raise PlanError("%s must pin all required parent artifacts" % field)
    normalized = {}  # type: Dict[str, str]
    for name in PARENT_ARTIFACTS:
        digest = str(artifacts.get(name, ""))
        if not HEX64.fullmatch(digest):
            raise PlanError("%s has an invalid parent artifact hash" % field)
        normalized[name] = digest
    return ParentPin(resolved, normalized)


def sealed_classification_parent_pin(
    value: object, root: Path, field: str,
) -> SealedClassificationParentPin:
    if not isinstance(value, Mapping):
        raise PlanError("%s must be a sealed classification parent pin" % field)
    required = {
        "handoff_manifest", "worklist", "classification_seal",
        "independent_audit", "source_summary", "preserved_passes",
        "verified_exclusions", "classified_source_ledger",
    }
    if set(value) != required:
        raise PlanError("%s must pin every sealed classification artifact" % field)
    return SealedClassificationParentPin(
        handoff_manifest=file_pin(value["handoff_manifest"], root, "%s.handoff_manifest" % field),
        worklist=file_pin(value["worklist"], root, "%s.worklist" % field),
        classification_seal=file_pin(
            value["classification_seal"], root, "%s.classification_seal" % field,
        ),
        independent_audit=file_pin(
            value["independent_audit"], root, "%s.independent_audit" % field,
        ),
        source_summary=file_pin(value["source_summary"], root, "%s.source_summary" % field),
        classified_source_ledger=file_pin(
            value["classified_source_ledger"], root, "%s.classified_source_ledger" % field,
        ),
        preserved_passes=file_pin(
            value["preserved_passes"], root, "%s.preserved_passes" % field,
        ),
        verified_exclusions=file_pin(
            value["verified_exclusions"], root, "%s.verified_exclusions" % field,
        ),
    )


def _exact_url_file_facts(pin: FilePin, expected_count: int) -> Dict[str, object]:
    verify_file(pin)
    lines = pin.path.read_text(encoding="utf-8").splitlines()
    nonempty = [line.strip() for line in lines if line.strip()]
    unique = list(dict.fromkeys(nonempty))
    if len(nonempty) != expected_count or len(unique) != expected_count:
        raise PlanError("exact URL input count or uniqueness mismatch")
    return {
        "count": len(unique),
        "file_sha256": pin.sha256,
        "url_set_sha256": target_url_sha256(unique),
    }


def _count_hash(value: object, field: str) -> Tuple[int, str]:
    if not isinstance(value, Mapping):
        raise PlanError("%s must contain count and SHA-256" % field)
    count = value.get("count")
    digest = str(value.get("sha256", ""))
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise PlanError("%s count is invalid" % field)
    if not HEX64.fullmatch(digest):
        raise PlanError("%s SHA-256 is invalid" % field)
    return count, digest


def _identity_jsonl_facts(pin: FilePin, source_id: str, field: str) -> Dict[str, object]:
    verify_file(pin)
    urls = []  # type: List[str]
    for line in pin.path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlanError("%s contains invalid JSONL" % field) from exc
        if not isinstance(row, Mapping) or row.get("source_id") != source_id:
            raise PlanError("%s contains an invalid source identity" % field)
        url = str(row.get("url", "")).strip()
        if not url:
            raise PlanError("%s contains a missing URL" % field)
        urls.append(url)
    if len(urls) != len(set(urls)):
        raise PlanError("%s contains duplicate identities" % field)
    return {
        "count": len(urls), "sha256": pin.sha256,
        "url_set_sha256": target_url_sha256(urls),
        "_urls": frozenset(urls),
    }


def _assert_path_free_handoff(value: object, field: str = "handoff_manifest") -> None:
    """Reject local/runtime paths from the portable sealed handoff manifest."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in {
                "path", "run_dir", "runtime_root", "output_root", "receipt_root",
                "closed_runtime", "parent_run_dir",
            } or normalized.endswith("_path"):
                raise PlanError("%s contains a forbidden local/runtime path field" % field)
            _assert_path_free_handoff(child, field)
    elif isinstance(value, list):
        for child in value:
            _assert_path_free_handoff(child, field)
    elif isinstance(value, str) and Path(value).is_absolute():
        raise PlanError("%s contains a forbidden absolute path" % field)


def verify_sealed_classification_parent(
    parent: SealedClassificationParentPin,
    source_id: str,
    exact_input: FilePin,
    exact_count: int,
) -> Dict[str, object]:
    pins = {
        "handoff_manifest": parent.handoff_manifest,
        "worklist": parent.worklist,
        "classification_seal": parent.classification_seal,
        "independent_audit": parent.independent_audit,
        "source_summary": parent.source_summary,
        "classified_source_ledger": parent.classified_source_ledger,
        "preserved_passes": parent.preserved_passes,
        "verified_exclusions": parent.verified_exclusions,
    }
    artifacts = {name: verify_file(pin) for name, pin in pins.items()}
    if parent.worklist.path != exact_input.path or parent.worklist.sha256 != exact_input.sha256:
        raise PlanError("sealed classification worklist must be the exact URL input")
    facts = _exact_url_file_facts(parent.worklist, exact_count)
    worklist_urls = {
        line.strip() for line in parent.worklist.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    manifest = load_object(parent.handoff_manifest.path)
    _assert_path_free_handoff(manifest)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "sealed_classification_exact_handoff"
        or manifest.get("status") != "sealed"
        or manifest.get("source_id") != source_id
    ):
        raise PlanError("sealed classification handoff identity is invalid")
    source_name = source_id.split(":", 2)[-1]
    if manifest.get("source_name") != source_name:
        raise PlanError("sealed classification handoff source is invalid")
    worklist = manifest.get("worklist")
    if not isinstance(worklist, Mapping) or (
        worklist.get("count") != facts["count"]
        or worklist.get("sha256") != facts["file_sha256"]
        or worklist.get("url_set_sha256") != facts["url_set_sha256"]
    ):
        raise PlanError("sealed classification worklist count, hash, or URL set is invalid")
    if (
        manifest.get("classification_seal_sha256") != parent.classification_seal.sha256
        or manifest.get("independent_audit_sha256") != parent.independent_audit.sha256
        or manifest.get("source_summary_sha256") != parent.source_summary.sha256
    ):
        raise PlanError("sealed classification handoff artifact hashes are invalid")
    preserved_count, preserved_sha = _count_hash(
        manifest.get("preserved_passes"), "handoff_manifest.preserved_passes",
    )
    exclusion_count, exclusion_sha = _count_hash(
        manifest.get("verified_exclusions"), "handoff_manifest.verified_exclusions",
    )
    preserved_facts = _identity_jsonl_facts(
        parent.preserved_passes, source_id, "preserved_passes",
    )
    exclusion_facts = _identity_jsonl_facts(
        parent.verified_exclusions, source_id, "verified_exclusions",
    )
    classified_facts = _identity_jsonl_facts(
        parent.classified_source_ledger, source_id, "classified_source_ledger",
    )
    classified_urls = {
        str(json.loads(line).get("url", "")).strip()
        for line in parent.classified_source_ledger.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    preserved_urls = set(preserved_facts["_urls"])
    exclusion_urls = set(exclusion_facts["_urls"])
    if (
        worklist_urls & preserved_urls
        or worklist_urls & exclusion_urls
        or preserved_urls & exclusion_urls
    ):
        raise PlanError("sealed classification identity partitions overlap")
    if (
        (preserved_count, preserved_sha)
        != (preserved_facts["count"], preserved_facts["sha256"])
        or manifest.get("preserved_passes", {}).get("url_set_sha256")
        != preserved_facts["url_set_sha256"]
        or (exclusion_count, exclusion_sha)
        != (exclusion_facts["count"], exclusion_facts["sha256"])
        or manifest.get("verified_exclusions", {}).get("url_set_sha256")
        != exclusion_facts["url_set_sha256"]
        or manifest.get("classified_source_ledger") != {
            key: classified_facts[key] for key in ("count", "sha256", "url_set_sha256")
        }
        or not worklist_urls.issubset(classified_urls)
    ):
        raise PlanError("sealed preserved-pass or exclusion ledger facts are invalid")
    summary = load_object(parent.source_summary.path)
    _assert_path_free_handoff(summary, "source_summary")
    if (
        summary.get("source_id") != source_id
        or summary.get("classified_count") != classified_facts["count"]
        or _count_hash(summary.get("preserved_passes"), "source_summary.preserved_passes")
        != (preserved_count, preserved_sha)
        or _count_hash(summary.get("verified_exclusions"), "source_summary.verified_exclusions")
        != (exclusion_count, exclusion_sha)
    ):
        raise PlanError("sealed classification source summary does not match the handoff")
    audit = load_object(parent.independent_audit.path)
    _assert_path_free_handoff(audit, "independent_audit")
    seal = load_object(parent.classification_seal.path)
    _assert_path_free_handoff(seal, "classification_seal")
    source_code = source_id.split(":", 2)[1] if source_id.count(":") >= 2 else ""
    audit_summary = audit.get("check_summary")
    audit_sources = audit.get("sources")
    audit_source = audit_sources.get(source_code) if isinstance(audit_sources, Mapping) else None
    audited = audit.get("audited_artifacts")
    output_hashes = {
        item.get("sha256") for item in seal.get("output_artifacts", [])
        if isinstance(item, Mapping)
    }
    if (
        seal.get("kind") != "private_offline_classification_seal"
        or seal.get("status") != "sealed_pending_independent_audit"
        or parent.source_summary.sha256 not in output_hashes
        or parent.classified_source_ledger.sha256 not in output_hashes
        or audit.get("kind") != "private_independent_offline_source_local_classification_audit"
        or audit.get("result") != "approved"
        or not isinstance(audit_summary, Mapping)
        or audit_summary.get("failed") != 0
        or not isinstance(audit_summary.get("total"), int)
        or audit_summary.get("total", 0) <= 0
        or audit_summary.get("passed") != audit_summary.get("total")
        or not isinstance(audit.get("failed_checks"), list)
        or audit.get("failed_checks")
        or not isinstance(audited, Mapping)
        or audited.get("classification_seal_sha256") != parent.classification_seal.sha256
        or not isinstance(audit_source, Mapping)
        or audit_source.get("classified_count") != classified_facts["count"]
        or audit_source.get("preserved_pass_count") != preserved_count
        or audit_source.get("verified_exclusion_count") != exclusion_count
    ):
        raise PlanError("sealed classification independent audit is not approved for the source")
    return {
        "kind": "sealed_classification_parent",
        "source_id": source_id,
        "target_url_count": facts["count"],
        "target_url_set_sha256": facts["url_set_sha256"],
        "worklist_sha256": facts["file_sha256"],
        "classification_seal_sha256": parent.classification_seal.sha256,
        "independent_audit_sha256": parent.independent_audit.sha256,
        "source_summary_sha256": parent.source_summary.sha256,
        "handoff_manifest_sha256": parent.handoff_manifest.sha256,
        "preserved_pass_count": preserved_count,
        "preserved_pass_sha256": preserved_sha,
        "verified_exclusion_count": exclusion_count,
        "verified_exclusion_sha256": exclusion_sha,
        "preserved_pass_url_set_sha256": preserved_facts["url_set_sha256"],
        "verified_exclusion_url_set_sha256": exclusion_facts["url_set_sha256"],
        "classified_source_count": classified_facts["count"],
        "classified_source_ledger_sha256": classified_facts["sha256"],
        "classified_source_url_set_sha256": classified_facts["url_set_sha256"],
        "artifacts": artifacts,
    }


def assert_pure_python(path: Path) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise PlanError("cannot parse pinned Python file") from exc
    imports = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
    if imports & FORBIDDEN_IMPORTS or calls & FORBIDDEN_CALLS:
        raise PlanError("pinned Python file failed the pure-network static gate")


def verify_file(pin: FilePin) -> Dict[str, object]:
    actual = sha256_path(pin.path)
    if actual != pin.sha256:
        raise PlanError("pinned file SHA-256 mismatch")
    return {"sha256": actual, "bytes": pin.path.stat().st_size}


def verify_parent(parent: ParentPin) -> Dict[str, object]:
    result = {}  # type: Dict[str, object]
    for name in PARENT_ARTIFACTS:
        path = parent.run_dir / name
        actual = sha256_path(path)
        if actual != parent.artifacts[name]:
            raise PlanError("parent artifact SHA-256 mismatch: %s" % name)
        result[name] = {"sha256": actual, "bytes": path.stat().st_size}
    manifest = load_object(parent.run_dir / "manifest.json")
    if manifest.get("status") != "observed":
        raise PlanError("parent observation is not terminal observed evidence")
    result["parent_run_id"] = manifest.get("run_id")
    return result


def verify_publication_gate(value: object, root: Path, field: str) -> str:
    if not isinstance(value, Mapping):
        raise PlanError("%s publication gate is missing" % field)
    if (
        value.get("status") != "reviewed_published"
        or value.get("repository") != "lin61302/scraping"
        or value.get("branch") != "main"
        or not HEX40.fullmatch(str(value.get("commit", "")))
    ):
        raise PlanError("%s publication gate is not reviewed and pinned" % field)
    for name in ("review_receipt", "test_receipt"):
        verify_file(file_pin(value.get(name), root, "%s.%s" % (field, name)))
    return str(value["commit"])


def publication_redirect_exact_sets(value: object, field: str) -> frozenset[Tuple[str, int, str]]:
    if not isinstance(value, Mapping):
        return frozenset()
    raw_sets = value.get("redirect_exact_sets", [])
    if not isinstance(raw_sets, list):
        raise PlanError("%s redirect exact sets must be a list" % field)
    normalized = set()  # type: set[Tuple[str, int, str]]
    for index, item in enumerate(raw_sets):
        if not isinstance(item, Mapping) or set(item) != {"source_id", "count", "sha256"}:
            raise PlanError("%s redirect exact set is invalid" % field)
        source_id = str(item.get("source_id", ""))
        count = positive_int(item.get("count"), "%s.redirect_exact_sets[%s].count" % (field, index))
        digest = str(item.get("sha256", ""))
        if source_id not in REVIEWED_REDIRECT_EXACT_SETS or not HEX64.fullmatch(digest):
            raise PlanError("%s redirect exact set source or hash is invalid" % field)
        entry = (source_id, count, digest)
        if entry in normalized:
            raise PlanError("%s contains a duplicate redirect exact set" % field)
        normalized.add(entry)
    return frozenset(normalized)


def validate_acceptance_contract(value: Mapping[str, object]) -> None:
    targeted = value.get("targeted_issue_codes_zero")
    allowed = value.get("allowed_unrelated_issue_codes")
    if targeted is None and allowed is None:
        return
    if not isinstance(targeted, list) or not targeted:
        raise PlanError("targeted_issue_codes_zero must be a nonempty list")
    if not isinstance(allowed, list):
        raise PlanError("allowed_unrelated_issue_codes must be a list")
    for field, values in (
        ("targeted_issue_codes_zero", targeted),
        ("allowed_unrelated_issue_codes", allowed),
    ):
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise PlanError("%s contains an invalid issue code" % field)
        if len(set(values)) != len(values):
            raise PlanError("%s contains a duplicate issue code" % field)
    if "adapter_parser_error" not in targeted:
        raise PlanError("targeted_issue_codes_zero must include adapter_parser_error")
    if set(targeted) & set(allowed):
        raise PlanError("targeted and allowed unrelated issue codes overlap")


def load_plan(
    plan_path: Path,
    expected_sha256: str,
    deployment_root: Path,
    allowed_parent_roots: Sequence[Path],
) -> SequencePlan:
    if not HEX64.fullmatch(expected_sha256) or sha256_path(plan_path) != expected_sha256:
        raise PlanError("sequence plan SHA-256 mismatch")
    payload = load_object(plan_path)
    if (
        payload.get("schema_version") != 1
        or payload.get("mode") != "unattended_exact_observation_sequence"
        or payload.get("production_writes") is not False
        or payload.get("continue_after_isolated_lane_failure") is not True
    ):
        raise PlanError("unsafe exact-observation plan header")
    sequence_id = str(payload.get("sequence_id", ""))
    if not SAFE_ID.fullmatch(sequence_id):
        raise PlanError("invalid sequence_id")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise PlanError("missing publication provenance")
    code_git_commit = str(provenance.get("code_git_commit", ""))
    if (
        provenance.get("repository") != "lin61302/scraping"
        or provenance.get("branch") != "main"
        or not HEX40.fullmatch(code_git_commit)
    ):
        raise PlanError("invalid publication provenance")
    capacity = payload.get("capacity")
    if not isinstance(capacity, Mapping):
        raise PlanError("missing capacity contract")
    if (
        capacity.get("network_ceiling") != NETWORK_CEILING
        or capacity.get("browser_ceiling") != BROWSER_CEILING
        or capacity.get("log_max_bytes") != OBSERVER_LOG_MAX_BYTES
    ):
        raise PlanError("capacity contract exceeds the hard sequence ceiling")
    max_wall_seconds = positive_int(
        capacity.get("max_wall_seconds"), "capacity.max_wall_seconds", MAX_WALL_SECONDS,
    )
    execution_raw = payload.get("execution_files")
    if not isinstance(execution_raw, list) or not execution_raw:
        raise PlanError("execution_files must be a nonempty pin list")
    execution_files = tuple(
        file_pin(value, deployment_root, "execution_files[%s]" % index)
        for index, value in enumerate(execution_raw)
    )
    if len({item.path for item in execution_files}) != len(execution_files):
        raise PlanError("duplicate execution file pin")
    for pin in execution_files:
        verify_file(pin)
    required_execution_names = {
        "unattended_exact_observation_sequence.py",
        "run_pure_discovery_diagnostic.py",
        "host_capacity_guard.py",
        "parallel_observe_pure.py",
    }
    if not required_execution_names.issubset({pin.path.name for pin in execution_files}):
        raise PlanError("execution_files omit a required reviewed runner")

    phases_raw = payload.get("phases")
    if not isinstance(phases_raw, list) or not phases_raw:
        raise PlanError("phases must be a nonempty list")
    phases = []  # type: List[PhaseSpec]
    seen_phases = set()
    seen_lanes = set()
    for phase_index, raw_phase in enumerate(phases_raw):
        if not isinstance(raw_phase, Mapping):
            raise PlanError("phase must be an object")
        phase_id = str(raw_phase.get("phase_id", ""))
        if not SAFE_ID.fullmatch(phase_id) or phase_id in seen_phases:
            raise PlanError("invalid or duplicate phase_id")
        seen_phases.add(phase_id)
        if raw_phase.get("production_writes") is not False:
            raise PlanError("phase is not production-write-disabled")
        reviewed_publication = raw_phase.get("requires_reviewed_publication") is True
        publication_commit = None
        reviewed_redirect_sets = frozenset()  # type: frozenset[Tuple[str, int, str]]
        if reviewed_publication:
            publication_gate = raw_phase.get("publication_gate")
            publication_commit = verify_publication_gate(
                publication_gate, deployment_root,
                "phases[%s]" % phase_index,
            )
            reviewed_redirect_sets = publication_redirect_exact_sets(
                publication_gate, "phases[%s].publication_gate" % phase_index,
            )
        max_active = positive_int(
            raw_phase.get("max_active_lanes"),
            "phases[%s].max_active_lanes" % phase_index,
            NETWORK_CEILING,
        )
        raw_lanes = raw_phase.get("lanes")
        if not isinstance(raw_lanes, list) or not raw_lanes:
            raise PlanError("phase lanes must be nonempty")
        lanes = []  # type: List[LaneSpec]
        for lane_index, raw_lane in enumerate(raw_lanes):
            if not isinstance(raw_lane, Mapping):
                raise PlanError("lane must be an object")
            lane_id = str(raw_lane.get("lane_id", ""))
            if not SAFE_ID.fullmatch(lane_id) or lane_id in seen_lanes:
                raise PlanError("invalid or duplicate lane_id")
            seen_lanes.add(lane_id)
            if raw_lane.get("production_writes") is not False:
                raise PlanError("lane is not production-write-disabled")
            kind = str(raw_lane.get("kind", ""))
            if kind not in {"discovery_diagnostic", "exact_urls"}:
                raise PlanError("unsupported lane kind")
            source_id = str(raw_lane.get("source_id", ""))
            if not source_id.startswith("mlp:"):
                raise PlanError("invalid source_id")
            adapter = file_pin(
                raw_lane.get("adapter"), deployment_root,
                "phases[%s].lanes[%s].adapter" % (phase_index, lane_index),
            )
            verify_file(adapter)
            assert_pure_python(adapter.path)
            start = parse_date(raw_lane.get("start"), "lane.start")
            end = parse_date(raw_lane.get("end"), "lane.end")
            if start > end:
                raise PlanError("lane window is reversed")
            workers = positive_int(raw_lane.get("workers"), "lane.workers", NETWORK_CEILING)
            timeout_seconds = positive_int(
                raw_lane.get("timeout_seconds"), "lane.timeout_seconds", MAX_WALL_SECONDS,
            )
            acceptance = raw_lane.get("acceptance")
            if not isinstance(acceptance, Mapping):
                raise PlanError("lane acceptance contract is missing")
            validate_acceptance_contract(acceptance)
            expected_requests = None
            exact_input = None
            exact_count = None
            parent = None
            sealed_classification_parent = None
            derivation = None
            repair_class = None
            if kind == "discovery_diagnostic":
                if start != end or workers != 1:
                    raise PlanError("discovery diagnostics require one day and one worker")
                request_bound = raw_lane.get("expected_request_bound")
                if not isinstance(request_bound, Mapping):
                    raise PlanError("discovery request bound is missing")
                normalized = {
                    key: int(request_bound.get(key, -1))
                    for key in ("total", "robots", "inventory", "article")
                }
                if (
                    min(normalized.values()) < 0
                    or normalized["article"] != 0
                    or normalized["total"] != normalized["robots"] + normalized["inventory"]
                ):
                    raise PlanError("invalid discovery request bound")
                expected_requests = normalized
            else:
                if raw_lane.get("rediscovery") is not False:
                    raise PlanError("exact lane must explicitly disable rediscovery")
                exact_input = file_pin(raw_lane.get("exact_input"), deployment_root, "exact_input")
                exact_count = positive_int(raw_lane.get("exact_input_count"), "exact_input_count")
                verify_file(exact_input)
                has_legacy_parent = raw_lane.get("parent") is not None
                has_sealed_parent = raw_lane.get("sealed_classification_parent") is not None
                if has_legacy_parent == has_sealed_parent:
                    raise PlanError(
                        "exact lane requires exactly one legacy or sealed classification parent"
                    )
                if has_legacy_parent:
                    parent = parent_pin(raw_lane.get("parent"), allowed_parent_roots, "parent")
                    verify_parent(parent)
                else:
                    sealed_classification_parent = sealed_classification_parent_pin(
                        raw_lane.get("sealed_classification_parent"), deployment_root,
                        "sealed_classification_parent",
                    )
                    verify_sealed_classification_parent(
                        sealed_classification_parent, source_id, exact_input, exact_count,
                    )
                    if not {
                        "targeted_issue_codes_zero", "allowed_unrelated_issue_codes",
                    }.issubset(acceptance):
                        raise PlanError(
                            "sealed classification lane requires targeted issue acceptance"
                        )
                repair_class = str(raw_lane.get("repair_class", ""))
                if repair_class not in {"parser_exact", "redirect_exact", "other_exact"}:
                    raise PlanError("exact lane repair_class is missing or unsupported")
                if repair_class == "redirect_exact":
                    if not reviewed_publication:
                        raise PlanError("redirect exact lane requires a reviewed publication gate")
                    if publication_commit != code_git_commit:
                        raise PlanError("redirect exact lane publication commit differs from execution commit")
                    expected = REVIEWED_REDIRECT_EXACT_SETS.get(source_id)
                    exact_set = (source_id, exact_count, exact_input.sha256)
                    if expected is None or (
                        expected != (exact_count, exact_input.sha256)
                        and exact_set not in reviewed_redirect_sets
                    ):
                        raise PlanError("redirect exact lane does not match its reviewed exact set")
                derivation = str(raw_lane.get("derivation_reason", "")).strip()
                if not derivation:
                    raise PlanError("exact lane derivation_reason is required")
            lanes.append(LaneSpec(
                lane_id=lane_id, phase_id=phase_id, kind=kind, source_id=source_id,
                adapter=adapter, start=start, end=end, workers=workers,
                timeout_seconds=timeout_seconds, expected_requests=expected_requests,
                exact_input=exact_input, exact_input_count=exact_count, parent=parent,
                sealed_classification_parent=sealed_classification_parent,
                derivation_reason=derivation, acceptance=dict(acceptance),
                repair_class=repair_class,
            ))
        largest = sorted((lane.workers for lane in lanes), reverse=True)[:max_active]
        if sum(largest) > NETWORK_CEILING:
            raise PlanError("phase lane weights exceed the two-network-worker ceiling")
        phases.append(PhaseSpec(phase_id, max_active, tuple(lanes)))
    return SequencePlan(
        sequence_id=sequence_id,
        plan_sha256=expected_sha256,
        code_git_commit=code_git_commit,
        execution_files=execution_files,
        phases=tuple(phases),
        max_wall_seconds=max_wall_seconds,
        log_max_bytes=OBSERVER_LOG_MAX_BYTES,
    )


def capacity_report(runtime: RuntimePaths, sequence_id: str) -> Mapping[str, object]:
    receipt_dir = runtime.receipt_root / sequence_id
    output_dir = runtime.output_root / sequence_id
    metrics = collect_metrics(
        runtime.deployment_root,
        [receipt_dir],
        [output_dir],
        scrape_patterns=(
            "parallel_observe_pure.py", "run_pure_discovery_diagnostic.py",
            "unattended_exact_observation_sequence.py",
        ),
        browser_patterns=(
            "chromedriver", "geckodriver", "playwright", "--headless",
            "--remote-debugging-port", "selenium",
        ),
    )
    report = dict(evaluate_capacity(metrics, include_paths=False))
    recommended = report.get("recommended_limits")
    if isinstance(recommended, Mapping):
        report["sequence_limits"] = {
            "network_workers": min(NETWORK_CEILING, int(recommended.get("network_workers", 0))),
            "browser_workers": 0,
        }
    return report


def process_command(pid: object) -> Optional[str]:
    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        return Path("/proc/%s/cmdline" % numeric).read_bytes().replace(
            b"\0", b" ",
        ).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def process_group_present(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_group(pid: int) -> Dict[str, object]:
    present_before = process_group_present(pid)
    signals = []  # type: List[str]
    if not present_before:
        return {
            "process_group_present_before": False,
            "signals": signals,
            "process_group_present_after": False,
            "quiescent": True,
        }
    try:
        os.killpg(pid, signal.SIGTERM)
        signals.append("SIGTERM")
    except ProcessLookupError:
        return {
            "process_group_present_before": True,
            "signals": signals,
            "process_group_present_after": False,
            "quiescent": True,
        }
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not process_group_present(pid):
            return {
                "process_group_present_before": True,
                "signals": signals,
                "process_group_present_after": False,
                "quiescent": True,
            }
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
        signals.append("SIGKILL")
    except ProcessLookupError:
        pass
    kill_deadline = time.monotonic() + 5
    while time.monotonic() < kill_deadline and process_group_present(pid):
        time.sleep(0.1)
    present_after = process_group_present(pid)
    return {
        "process_group_present_before": True,
        "signals": signals,
        "process_group_present_after": present_after,
        "quiescent": not present_after,
    }


def quiescence_proof(pid: int, required_command_parts: Sequence[str]) -> Dict[str, object]:
    before = process_command(pid)
    group_present_before = process_group_present(pid)
    matched = bool(before and all(value in before for value in required_command_parts))
    cleanup = "none"
    group_cleanup = None
    if before and matched:
        group_cleanup = terminate_group(pid)
        cleanup = "matching_process_group_terminated"
    after = process_command(pid)
    group_present_after = process_group_present(pid)
    return {
        "checked_at_utc": iso_now(),
        "pid": pid,
        "process_present_before": before is not None,
        "process_group_present_before": group_present_before,
        "command_matched_before": matched,
        "cleanup": cleanup,
        "process_group_cleanup": group_cleanup,
        "process_present_after": after is not None,
        "process_group_present_after": group_present_after,
        # A reused live PID is not proof of quiescence.  Refuse to touch a
        # command that does not match the receipt, but keep the lane closed
        # until the recorded PID is absent and can no longer own descendants.
        "quiescent": after is None and not group_present_after,
    }


def terminate_child(process: subprocess.Popen) -> Dict[str, object]:
    before = process.poll()
    present_before = process_group_present(process.pid)
    signals = []  # type: List[str]
    if present_before:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            signals.append("SIGTERM")
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and process_group_present(process.pid):
        time.sleep(0.1)
    if process_group_present(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            signals.append("SIGKILL")
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    kill_deadline = time.monotonic() + 2
    while time.monotonic() < kill_deadline and process_group_present(process.pid):
        time.sleep(0.1)
    after = process.poll()
    present_after = process_group_present(process.pid)
    group_cleanup = {
        "process_group_present_before": present_before,
        "signals": signals,
        "process_group_present_after": present_after,
        "quiescent": not present_after,
    }
    return {
        "pid": process.pid,
        "exit_before_cleanup": before,
        "exit_after_cleanup": after,
        "process_group_cleanup": group_cleanup,
        "quiescent": after is not None and group_cleanup.get("quiescent") is True,
    }


def read_jsonl_code_counts(path: Path) -> Dict[str, int]:
    counts = {}  # type: Dict[str, int]
    if not path.is_file():
        return counts
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            code = str(value.get("code", "unknown")) if isinstance(value, Mapping) else "unknown"
            counts[code] = counts.get(code, 0) + 1
    return counts


def read_jsonl_failure_classes(path: Path) -> Dict[str, int]:
    counts = {}  # type: Dict[str, int]
    if not path.is_file():
        return counts
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            message = str(value.get("message", "")).casefold() if isinstance(value, Mapping) else ""
            if "unreviewed redirect" in message:
                key = "unreviewed_redirect"
            elif "403" in message or "access" in message or "challenge" in message:
                key = "access_block"
            elif "timeout" in message:
                key = "timeout"
            else:
                key = "other_exception"
            counts[key] = counts.get(key, 0) + 1
    return counts


def observed_url_set_sha256(path: Path) -> str:
    urls = []  # type: List[str]
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping) or not isinstance(value.get("url"), str):
                raise PlanError("observation urls artifact contains an invalid row")
            urls.append(str(value["url"]).strip())
    if len(urls) != len(set(urls)):
        raise PlanError("observation urls artifact contains duplicate identities")
    return target_url_sha256(urls)


def artifact_hashes(run_dir: Path) -> Dict[str, Mapping[str, object]]:
    result = {}  # type: Dict[str, Mapping[str, object]]
    for name in (
        "manifest.json", "urls.jsonl", "articles.jsonl", "field_checks.jsonl",
        "issues.jsonl", "errors.jsonl", "summary.md",
    ):
        path = run_dir / name
        if path.is_file():
            result[name] = {"sha256": sha256_path(path), "bytes": path.stat().st_size}
    return result


class CappedChild:
    def __init__(self, command: Sequence[str], env: Mapping[str, str], log_path: Path):
        self.command = list(command)
        self.env = dict(env)
        self.log_path = log_path
        self.process = None  # type: Optional[subprocess.Popen]
        self.bytes_seen = 0
        self.bytes_kept = 0
        self.drain_errors = []  # type: List[str]
        self.thread = None  # type: Optional[threading.Thread]
        self.stream = None
        self.stream_lock = threading.Lock()
        self.stop_writes = threading.Event()
        self.forced_cleanup = None  # type: Optional[Mapping[str, object]]

    def start(self) -> subprocess.Popen:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.log_path.open("xb")
        os.chmod(self.log_path, 0o600)
        self.process = subprocess.Popen(
            self.command,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if self.process.stdout is None:
            raise RuntimeError("child stdout pipe was not created")

        def drain() -> None:
            assert self.process is not None and self.process.stdout is not None
            assert self.stream is not None
            try:
                while True:
                    chunk = self.process.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    self.bytes_seen += len(chunk)
                    remaining = OBSERVER_LOG_MAX_BYTES - self.bytes_kept
                    if remaining > 0 and not self.stop_writes.is_set():
                        kept = chunk[:remaining]
                        with self.stream_lock:
                            if not self.stop_writes.is_set() and not self.stream.closed:
                                self.stream.write(kept)
                                self.bytes_kept += len(kept)
            except Exception as exc:
                self.drain_errors.append("%s:%s" % (type(exc).__name__, str(exc)[:200]))
            finally:
                try:
                    self.process.stdout.close()
                except (OSError, ValueError):
                    pass

        self.thread = threading.Thread(target=drain, name="exact-observer-log", daemon=True)
        self.thread.start()
        return self.process

    def finish(self, join_timeout_seconds: float = 3.0) -> Mapping[str, object]:
        if self.thread is not None:
            self.thread.join(timeout=join_timeout_seconds)
            if self.thread.is_alive():
                self.drain_errors.append("log_pipe_remained_open_after_child_exit")
                if self.process is not None:
                    self.forced_cleanup = terminate_child(self.process)
                    if self.process.stdout is not None:
                        try:
                            os.close(self.process.stdout.fileno())
                        except (OSError, ValueError):
                            pass
                self.thread.join(timeout=join_timeout_seconds)
            if self.thread.is_alive():
                self.drain_errors.append("log_drain_thread_did_not_quiesce")
        self.stop_writes.set()
        if self.stream is not None:
            with self.stream_lock:
                if not self.stream.closed:
                    self.stream.flush()
                    os.fsync(self.stream.fileno())
                    self.stream.close()
        return {
            "sha256": sha256_path(self.log_path),
            "max_bytes": OBSERVER_LOG_MAX_BYTES,
            "bytes_seen": self.bytes_seen,
            "bytes_kept": self.bytes_kept,
            "bytes_truncated": self.bytes_seen - self.bytes_kept,
            "drain_errors": list(self.drain_errors),
            "drain_thread_quiescent": self.thread is None or not self.thread.is_alive(),
            "forced_process_group_cleanup": self.forced_cleanup,
        }


class SequenceController:
    def __init__(
        self,
        plan: SequencePlan,
        runtime: RuntimePaths,
        *,
        capacity_reporter: Optional[Callable[[], Mapping[str, object]]] = None,
        resume: bool = False,
    ):
        self.plan = plan
        self.runtime = runtime
        self.resume = resume
        self.sequence_dir = runtime.receipt_root / plan.sequence_id
        self.sequence_output = runtime.output_root / plan.sequence_id
        self.events = self.sequence_dir / "events.jsonl"
        self.deadline = 0.0
        self.capacity_reporter = capacity_reporter or (
            lambda: capacity_report(runtime, plan.sequence_id)
        )
        self.event_lock = threading.Lock()
        self.guard_lock = threading.Lock()
        self.active_lock = threading.Lock()
        self.active_network = 0

    def event(self, kind: str, **values: object) -> None:
        with self.event_lock:
            append_event(self.events, kind, **values)

    def guard(self) -> Mapping[str, object]:
        with self.guard_lock:
            return self.capacity_reporter()

    @contextmanager
    def host_lock(self) -> Iterator[None]:
        self.runtime.host_lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.runtime.host_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ControllerAlreadyRunning("exact sequence lock is held") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def verify_global_pins(self) -> Mapping[str, object]:
        return {
            str(index): verify_file(pin)
            for index, pin in enumerate(self.plan.execution_files)
        }

    def lane_dir(self, lane: LaneSpec) -> Path:
        return self.sequence_dir / "lanes" / safe_name(lane.lane_id)

    def lane_output(self, lane: LaneSpec) -> Path:
        return self.sequence_output / safe_name(lane.lane_id)

    def exact_input_facts(self, lane: LaneSpec) -> Mapping[str, object]:
        assert lane.exact_input is not None and lane.exact_input_count is not None
        return _exact_url_file_facts(lane.exact_input, lane.exact_input_count)

    def verify_lane_pins(self, lane: LaneSpec) -> Mapping[str, object]:
        result = {
            "adapter": verify_file(lane.adapter),
            "execution_files": self.verify_global_pins(),
        }  # type: Dict[str, object]
        if lane.kind == "exact_urls":
            result["exact_input"] = self.exact_input_facts(lane)
            if lane.parent is not None:
                result["parent"] = verify_parent(lane.parent)
            else:
                assert lane.sealed_classification_parent is not None
                assert lane.exact_input is not None and lane.exact_input_count is not None
                result["sealed_classification_parent"] = verify_sealed_classification_parent(
                    lane.sealed_classification_parent, lane.source_id,
                    lane.exact_input, lane.exact_input_count,
                )
        return result

    def validate_lane_start(
        self, lane: LaneSpec, path: Path,
    ) -> Mapping[str, object]:
        receipt = load_object(path)
        expected = {
            "schema_version": 1,
            "kind": "exact_observation_lane_start",
            "sequence_id": self.plan.sequence_id,
            "phase_id": lane.phase_id,
            "lane_id": lane.lane_id,
            "source_id": lane.source_id,
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "adapter_sha256": lane.adapter.sha256,
            "production_writes": False,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise PlanError("lane start receipt identity or write contract is invalid")
        try:
            pid = int(receipt.get("pid"))
        except (TypeError, ValueError) as exc:
            raise PlanError("lane start receipt PID is invalid") from exc
        expected_command_sha = command_sha256(
            self.build_command(lane, self.lane_output(lane))
        )
        if pid <= 1 or receipt.get("command_sha256") != expected_command_sha:
            raise PlanError("lane start receipt process or command pin is invalid")
        current_pins = self.verify_lane_pins(lane)
        if receipt.get("execution_pin_verification") != current_pins:
            raise PlanError("lane start receipt pin accounting is invalid")
        if receipt.get("execution_bundle_sha256") != pin_bundle_sha256(
            tuple(self.plan.execution_files) + (lane.adapter,)
        ):
            raise PlanError("lane start receipt execution bundle pin is invalid")
        if not isinstance(receipt.get("capacity_snapshot"), Mapping):
            raise PlanError("lane start receipt lacks its capacity snapshot")
        if lane.kind == "exact_urls":
            facts = current_pins.get("exact_input")
            if not isinstance(facts, Mapping):
                raise PlanError("lane start receipt exact input facts are missing")
            expected_legacy_hashes = (
                dict(lane.parent.artifacts) if lane.parent is not None else None
            )
            expected_sealed = current_pins.get("sealed_classification_parent")
            if (
                receipt.get("private_input_count") != facts.get("count")
                or receipt.get("private_input_sha256") != facts.get("file_sha256")
                or receipt.get("parent_provenance_hashes") != expected_legacy_hashes
                or receipt.get("sealed_classification_provenance") != expected_sealed
            ):
                raise PlanError("lane start receipt exact input or parent pins are invalid")
        return receipt

    def validate_lane_terminal(
        self, lane: LaneSpec, path: Path,
    ) -> LaneResult:
        receipt = load_object(path)
        expected = {
            "schema_version": 1,
            "kind": "exact_observation_lane_terminal",
            "terminal": True,
            "sequence_id": self.plan.sequence_id,
            "phase_id": lane.phase_id,
            "lane_id": lane.lane_id,
            "source_id": lane.source_id,
            "plan_sha256": self.plan.plan_sha256,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise PlanError("lane terminal receipt identity or write contract is invalid")
        state = str(receipt.get("state", ""))
        allowed_states = {
            "observed_acceptance_passed", "isolated_lane_failure",
            "isolated_lane_timeout", "observed_contract_shortfall",
            "not_started_capacity_blocked", "stale_start_without_terminal_isolated",
        }
        if state not in allowed_states:
            raise PlanError("lane terminal receipt state is invalid")
        proof = receipt.get("process_quiescence_proof")
        if not isinstance(proof, Mapping) or proof.get("quiescent") is not True:
            raise PlanError("lane terminal receipt lacks process quiescence")
        if state in {
            "observed_acceptance_passed", "isolated_lane_failure",
            "isolated_lane_timeout", "observed_contract_shortfall",
        }:
            start = self.lane_dir(lane) / "start_receipt.json"
            start_hash = receipt.get("start_receipt_sha256")
            if start.is_file():
                self.validate_lane_start(lane, start)
                if start_hash != sha256_path(start):
                    raise PlanError("executed lane terminal start receipt hash is invalid")
            elif not (
                start_hash is None
                and receipt.get("exit_code") is None
                and isinstance(receipt.get("contract_error"), str)
                and receipt.get("contract_error")
            ):
                raise PlanError("executed lane terminal lacks exact start evidence")
            current_pins = self.verify_lane_pins(lane)
            if (
                receipt.get("code_git_commit") != self.plan.code_git_commit
                or receipt.get("pre_run_pin_verification") != current_pins
                or receipt.get("post_run_pin_verification") != current_pins
                or receipt.get("command_sha256") != command_sha256(
                    self.build_command(lane, self.lane_output(lane))
                )
            ):
                raise PlanError("executed lane terminal pin accounting is invalid")
            log = receipt.get("log_hash_and_byte_accounting")
            if not isinstance(log, Mapping):
                raise PlanError("executed lane terminal log accounting is missing")
            try:
                kept = int(log.get("bytes_kept", -1))
                seen = int(log.get("bytes_seen", -1))
                truncated = int(log.get("bytes_truncated", -1))
            except (TypeError, ValueError) as exc:
                raise PlanError("executed lane terminal log accounting is invalid") from exc
            if (
                not HEX64.fullmatch(str(log.get("sha256", "")))
                or log.get("max_bytes") != OBSERVER_LOG_MAX_BYTES
                or kept < 0 or kept > OBSERVER_LOG_MAX_BYTES
                or seen < kept or truncated != seen - kept
                or log.get("drain_thread_quiescent") is not True
            ):
                raise PlanError("executed lane terminal log accounting is invalid")
            log_path = self.lane_dir(lane) / "child.log"
            if (
                not log_path.is_file()
                or sha256_path(log_path) != log.get("sha256")
                or log_path.stat().st_size != kept
                or not isinstance(receipt.get("capacity_snapshot"), Mapping)
            ):
                raise PlanError("executed lane terminal log hash or capacity evidence drifted")
            contract_error = receipt.get("contract_error")
            if state == "observed_acceptance_passed" and (
                receipt.get("exit_code") != 0
                or receipt.get("timed_out") is not False
                or contract_error is not None
            ):
                raise PlanError("passing lane terminal has inconsistent exit state")
            if state == "isolated_lane_timeout" and receipt.get("timed_out") is not True:
                raise PlanError("timed-out lane terminal has inconsistent timeout state")
            if state == "isolated_lane_failure" and not (
                isinstance(contract_error, str) and contract_error
            ):
                raise PlanError("failed lane terminal lacks a failure class")
            if state == "observed_contract_shortfall" and (
                receipt.get("exit_code") != 0 or contract_error is not None
            ):
                raise PlanError("shortfall lane terminal has inconsistent exit state")
        if state == "observed_acceptance_passed":
            checks = receipt.get("acceptance_checks")
            if not isinstance(checks, Mapping) or not checks or not all(
                value is True for value in checks.values()
            ):
                raise PlanError("passing lane terminal has incomplete acceptance checks")
            if lane.kind == "exact_urls":
                runs = sorted(
                    path for path in (self.lane_output(lane) / "runs").glob(
                        "observe-parallel-*"
                    ) if path.is_dir()
                )
                if len(runs) != 1:
                    raise PlanError("passing exact lane lacks one immutable observation run")
                artifact_ok, details = self.acceptance_exact(
                    lane, runs[0], self.exact_input_facts(lane),
                )
                if (
                    artifact_ok is not True
                    or receipt.get("acceptance_checks") != details.get("checks")
                    or receipt.get("exact_identity_accounting") != details.get(
                        "exact_identity_accounting"
                    )
                    or receipt.get("remaining_failure_classes") != details.get(
                        "remaining_failure_classes"
                    )
                    or receipt.get("manifest_and_artifact_hashes") != details.get(
                        "manifest_and_artifact_hashes"
                    )
                ):
                    raise PlanError("passing exact lane artifact hashes or accounting drifted")
                accounting = receipt.get("exact_identity_accounting")
                if not isinstance(accounting, Mapping):
                    raise PlanError("passing exact lane lacks identity accounting")
                expected_count = int(lane.exact_input_count or 0)
                names = ("passed", "quarantined", "skipped", "exceptions")
                try:
                    total = sum(int(accounting.get(name, -1)) for name in names)
                    selected = int(accounting.get("selected", -1))
                    accounted = int(accounting.get("accounted", -1))
                    expected_accounting = int(accounting.get("expected", -1))
                except (TypeError, ValueError) as exc:
                    raise PlanError("passing exact lane identity accounting is invalid") from exc
                if not (
                    selected == accounted == expected_accounting == expected_count
                    and total == expected_count
                ):
                    raise PlanError("passing exact lane identity accounting is incomplete")
            else:
                diagnostic = self.lane_output(lane) / "diagnostic_manifest.private.json"
                if not diagnostic.is_file():
                    raise PlanError("passing diagnostic lane artifact is missing")
                artifact_ok, details = self.acceptance_diagnostic(lane, diagnostic)
                if (
                    artifact_ok is not True
                    or receipt.get("acceptance_checks") != details.get("checks")
                    or receipt.get("exact_identity_accounting") != details.get(
                        "exact_identity_accounting"
                    )
                    or receipt.get("manifest_and_artifact_hashes") != details.get(
                        "manifest_and_artifact_hashes"
                    )
                ):
                    raise PlanError("passing diagnostic lane artifact hashes or accounting drifted")
        elif state == "stale_start_without_terminal_isolated":
            start = self.lane_dir(lane) / "start_receipt.json"
            if (
                not start.is_file()
                or receipt.get("start_receipt_sha256") != sha256_path(start)
                or receipt.get("pre_run_pin_verification") != self.verify_lane_pins(lane)
                or receipt.get("post_run_pin_verification") != self.verify_lane_pins(lane)
            ):
                raise PlanError("stale lane terminal receipt lacks exact start and pin evidence")
            self.validate_lane_start(lane, start)
        elif state == "not_started_capacity_blocked":
            if (
                not isinstance(receipt.get("reason"), str)
                or not receipt.get("reason")
                or not isinstance(receipt.get("capacity_snapshot"), Mapping)
            ):
                raise PlanError("capacity-blocked lane receipt is incomplete")
        return LaneResult(state, receipt)

    def validate_phase_terminal(
        self, phase: PhaseSpec, path: Path,
    ) -> List[LaneResult]:
        receipt = load_object(path)
        expected = {
            "schema_version": 1,
            "kind": "exact_observation_phase_terminal",
            "terminal": True,
            "sequence_id": self.plan.sequence_id,
            "phase_id": phase.phase_id,
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "lane_count": len(phase.lanes),
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise PlanError("phase terminal receipt identity or write contract is invalid")
        results = []  # type: List[LaneResult]
        hashes = {}  # type: Dict[str, str]
        for lane in phase.lanes:
            terminal = self.lane_dir(lane) / "terminal_receipt.json"
            if not terminal.is_file():
                raise PlanError("phase terminal exists without every lane terminal")
            results.append(self.validate_lane_terminal(lane, terminal))
            hashes[lane.lane_id] = sha256_path(terminal)
        counts = {}  # type: Dict[str, int]
        for result in results:
            counts[result.state] = counts.get(result.state, 0) + 1
        if receipt.get("state_counts") != counts or receipt.get("lane_terminal_sha256") != hashes:
            raise PlanError("phase terminal receipt accounting or hashes are invalid")
        return results

    def validate_sequence_terminal(self, path: Path) -> Mapping[str, object]:
        receipt = load_object(path)
        startup_path = self.sequence_dir / "startup_receipt.json"
        attempt_path = self.sequence_dir / "attempt_receipt.json"
        if not startup_path.is_file() or not attempt_path.is_file():
            raise PlanError("sequence terminal lacks startup or attempt evidence")
        self.validate_sequence_startup(startup_path)
        attempt = load_object(attempt_path)
        expected_attempt = {
            "schema_version": 1,
            "kind": "exact_observation_sequence_attempt",
            "sequence_id": self.plan.sequence_id,
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "production_writes": False,
        }
        if any(attempt.get(key) != value for key, value in expected_attempt.items()):
            raise PlanError("sequence attempt receipt identity or pins are invalid")
        lane_results = []  # type: List[LaneResult]
        phase_hashes = {}  # type: Dict[str, str]
        lane_hashes = {}  # type: Dict[str, str]
        for phase in self.plan.phases:
            phase_path = (
                self.sequence_dir / "phases" / safe_name(phase.phase_id)
                / "terminal_receipt.json"
            )
            if not phase_path.is_file():
                raise PlanError("sequence terminal exists without every phase terminal")
            lane_results.extend(self.validate_phase_terminal(phase, phase_path))
            phase_hashes[phase.phase_id] = sha256_path(phase_path)
            for lane in phase.lanes:
                lane_hashes[lane.lane_id] = sha256_path(
                    self.lane_dir(lane) / "terminal_receipt.json"
                )
        counts = {}  # type: Dict[str, int]
        for result in lane_results:
            counts[result.state] = counts.get(result.state, 0) + 1
        expected_status = "terminal_observation_sequence"
        if counts.get("observed_acceptance_passed", 0) != len(lane_results):
            expected_status = "terminal_with_isolated_failures"
        expected = {
            "schema_version": 1,
            "kind": "exact_observation_sequence_terminal",
            "terminal": True,
            "status": expected_status,
            "sequence_id": self.plan.sequence_id,
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "lane_count": len(lane_results),
            "state_counts": counts,
            "lane_terminal_sha256": lane_hashes,
            "phase_terminal_sha256": phase_hashes,
            "startup_receipt_sha256": sha256_path(startup_path),
            "attempt_receipt_sha256": sha256_path(attempt_path),
            "post_run_pin_verification": self.verify_global_pins(),
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise PlanError("sequence terminal receipt content, hashes, or accounting are invalid")
        if not isinstance(receipt.get("terminal_capacity_snapshot"), Mapping):
            raise PlanError("sequence terminal lacks terminal capacity evidence")
        return receipt

    def validate_sequence_startup(self, path: Path) -> Mapping[str, object]:
        receipt = load_object(path)
        expected = {
            "schema_version": 1,
            "kind": "exact_observation_sequence_startup",
            "sequence_id": self.plan.sequence_id,
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "execution_pin_verification": self.verify_global_pins(),
            "capacity": {
                "network": NETWORK_CEILING,
                "browser": BROWSER_CEILING,
                "log_max_bytes": OBSERVER_LOG_MAX_BYTES,
            },
            "production_writes": False,
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise PlanError("sequence startup receipt content or pins are invalid")
        try:
            pid = int(receipt.get("pid"))
        except (TypeError, ValueError) as exc:
            raise PlanError("sequence startup receipt PID is invalid") from exc
        if pid <= 1 or not isinstance(receipt.get("host_capacity_guard"), Mapping):
            raise PlanError("sequence startup receipt lacks process or capacity evidence")
        return receipt

    def build_command(self, lane: LaneSpec, output_dir: Path) -> List[str]:
        if lane.kind == "discovery_diagnostic":
            assert lane.expected_requests is not None
            runner = self.runtime.automation_root / "ops/run_pure_discovery_diagnostic.py"
            return [
                str(self.runtime.python), PYTHON_NO_BYTECODE_FLAG, str(runner),
                "--automation-root", str(self.runtime.automation_root),
                "--source-file", str(lane.adapter.path),
                "--source-sha256", lane.adapter.sha256,
                "--day", lane.start.isoformat(),
                "--output", str(output_dir / "diagnostic_manifest.private.json"),
                "--expected-requests", str(lane.expected_requests["total"]),
                "--expected-robots", str(lane.expected_requests["robots"]),
                "--expected-inventory", str(lane.expected_requests["inventory"]),
            ]
        assert lane.exact_input is not None
        observer = self.runtime.automation_root / "scripts/parallel_observe_pure.py"
        command = [
            str(self.runtime.python), PYTHON_NO_BYTECODE_FLAG, str(observer),
            "--automation-root", str(self.runtime.automation_root),
            "--source-file", str(lane.adapter.path),
            "--start", lane.start.isoformat(),
            "--end", lane.end.isoformat(),
            "--output-root", str(output_dir / "runs"),
            "--workers", str(lane.workers),
            "--urls-file", str(lane.exact_input.path),
        ]
        if lane.parent is not None:
            command.extend([
                "--parent-run-dir", str(lane.parent.run_dir),
                "--derivation-reason", str(lane.derivation_reason),
            ])
        else:
            assert lane.sealed_classification_parent is not None
            sealed = lane.sealed_classification_parent
            for flag, pin in (
                ("handoff-manifest", sealed.handoff_manifest),
                ("worklist", sealed.worklist),
                ("classification-seal", sealed.classification_seal),
                ("independent-audit", sealed.independent_audit),
                ("source-summary", sealed.source_summary),
                ("classified-source-ledger", sealed.classified_source_ledger),
                ("preserved-passes", sealed.preserved_passes),
                ("verified-exclusions", sealed.verified_exclusions),
            ):
                command.extend([
                    "--sealed-classification-%s" % flag, str(pin.path),
                    "--sealed-classification-%s-sha256" % flag, pin.sha256,
                ])
            command.extend(["--derivation-reason", str(lane.derivation_reason)])
        return command

    def child_environment(self) -> Dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if key in CHILD_ENV_ALLOWLIST
        }
        env["PYTHONPATH"] = str(self.runtime.automation_root)
        env["SCRAPER_BOUNDED_OBSERVATION"] = "1"
        # Exact and diagnostic children import adapters from the immutable
        # deployment.  Force bytecode writes off even if an inherited host
        # environment would otherwise permit them.  ``build_command`` also
        # supplies ``-B`` so both the direct child and any Python descendants
        # remain isolated from the deployment tree.
        env[PYTHON_DONT_WRITE_BYTECODE] = "1"
        return env

    def stale_lane_outcome(self, lane: LaneSpec, start_path: Path, terminal_path: Path) -> LaneResult:
        start = self.validate_lane_start(lane, start_path)
        required = [
            "parallel_observe_pure.py" if lane.kind == "exact_urls"
            else "run_pure_discovery_diagnostic.py",
            str(self.lane_output(lane)),
        ]
        proof = quiescence_proof(int(start.get("pid", -1)), required)
        if proof.get("quiescent") is not True:
            raise PlanError("stale lane process could not be proven quiescent")
        current_pins = self.verify_lane_pins(lane)
        payload = {
            "schema_version": 1,
            "kind": "exact_observation_lane_terminal",
            "terminal": True,
            "state": "stale_start_without_terminal_isolated",
            "sequence_id": self.plan.sequence_id,
            "phase_id": lane.phase_id,
            "lane_id": lane.lane_id,
            "source_id": lane.source_id,
            "finished_at_utc": iso_now(),
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
            "start_receipt_sha256": sha256_path(start_path),
            "pre_run_pin_verification": current_pins,
            "post_run_pin_verification": current_pins,
            "process_quiescence_proof": proof,
        }
        write_json_exclusive(terminal_path, payload)
        return self.validate_lane_terminal(lane, terminal_path)

    def acceptance_exact(
        self, lane: LaneSpec, run_dir: Path, input_facts: Mapping[str, object],
    ) -> Tuple[bool, Mapping[str, object]]:
        manifest = load_object(run_dir / "manifest.json")
        counts = manifest.get("counts")
        if not isinstance(counts, Mapping):
            counts = {}
        accounted = sum(int(counts.get(name, 0)) for name in (
            "passed", "quarantined", "skipped", "exceptions",
        ))
        issue_counts = read_jsonl_code_counts(run_dir / "issues.jsonl")
        failure_classes = read_jsonl_failure_classes(run_dir / "errors.jsonl")
        derived = manifest.get("derived_from")
        derived = derived if isinstance(derived, Mapping) else {}
        checks = {
            "manifest_status": manifest.get("status") == lane.acceptance.get("manifest_status", "observed"),
            "mode": manifest.get("mode") == "observation_no_production_writes",
            "source_sha256": manifest.get("source_sha256") == lane.adapter.sha256,
            "selected": int(counts.get("selected", -1)) == int(input_facts["count"]),
            "accounted": accounted == int(input_facts["count"]),
            "derived_count": int(derived.get("target_url_count", -1)) == int(input_facts["count"]),
            "derived_url_set": derived.get("target_url_set_sha256") == input_facts["url_set_sha256"],
            "canonical_identity_set": observed_url_set_sha256(
                run_dir / "urls.jsonl"
            ) == input_facts["url_set_sha256"],
            "exceptions": int(counts.get("exceptions", 0)) <= int(
                lane.acceptance.get("exceptions_max", 0)
            ),
        }  # type: Dict[str, bool]
        if lane.sealed_classification_parent is not None:
            checks["issues_artifact_present"] = (run_dir / "issues.jsonl").is_file()
            assert lane.exact_input is not None and lane.exact_input_count is not None
            expected_parent = verify_sealed_classification_parent(
                lane.sealed_classification_parent, lane.source_id,
                lane.exact_input, lane.exact_input_count,
            )
            for key in (
                "kind", "source_id", "worklist_sha256",
                "classification_seal_sha256", "independent_audit_sha256",
                "source_summary_sha256", "handoff_manifest_sha256",
                "preserved_pass_count", "preserved_pass_sha256",
                "verified_exclusion_count", "verified_exclusion_sha256",
                "preserved_pass_url_set_sha256",
                "verified_exclusion_url_set_sha256",
                "classified_source_count", "classified_source_ledger_sha256",
                "classified_source_url_set_sha256",
            ):
                checks["sealed_classification_parent:%s" % key] = (
                    derived.get(key) == expected_parent.get(key)
                )
        issue_max = lane.acceptance.get("issue_code_max", {})
        if isinstance(issue_max, Mapping):
            for code, maximum in issue_max.items():
                checks["issue_code:%s" % code] = issue_counts.get(str(code), 0) <= int(maximum)
        failure_max = lane.acceptance.get("failure_class_max", {})
        if isinstance(failure_max, Mapping):
            for code, maximum in failure_max.items():
                checks["failure_class:%s" % code] = failure_classes.get(str(code), 0) <= int(maximum)
        targeted_issue_codes = lane.acceptance.get("targeted_issue_codes_zero")
        allowed_unrelated_issue_codes = lane.acceptance.get("allowed_unrelated_issue_codes")
        if isinstance(targeted_issue_codes, list) and isinstance(
            allowed_unrelated_issue_codes, list,
        ):
            for code in targeted_issue_codes:
                checks["targeted_issue_code_zero:%s" % code] = (
                    issue_counts.get(str(code), 0) == 0
                )
            checks["issue_code_allowlist"] = set(issue_counts).issubset(
                set(targeted_issue_codes) | set(allowed_unrelated_issue_codes)
            )
        if lane.repair_class == "redirect_exact":
            checks["failure_class:unreviewed_redirect"] = (
                failure_classes.get("unreviewed_redirect", 0) == 0
            )
        return all(checks.values()), {
            "checks": checks,
            "exact_identity_accounting": {
                "selected": int(counts.get("selected", 0)),
                "passed": int(counts.get("passed", 0)),
                "quarantined": int(counts.get("quarantined", 0)),
                "skipped": int(counts.get("skipped", 0)),
                "exceptions": int(counts.get("exceptions", 0)),
                "accounted": accounted,
                "expected": int(input_facts["count"]),
            },
            "remaining_failure_classes": {
                "issue_codes": issue_counts,
                "exception_classes": failure_classes,
            },
            "manifest_and_artifact_hashes": artifact_hashes(run_dir),
        }

    def acceptance_diagnostic(
        self, lane: LaneSpec, path: Path,
    ) -> Tuple[bool, Mapping[str, object]]:
        manifest = load_object(path)
        accounting = manifest.get("request_accounting")
        observed = accounting.get("observed") if isinstance(accounting, Mapping) else {}
        observed = observed if isinstance(observed, Mapping) else {}
        expected = lane.expected_requests or {}
        checks = {
            "status": manifest.get("status") == "observed",
            "mode": manifest.get("mode") == "observation_no_production_writes",
            "source_sha256": manifest.get("source_sha256") == lane.adapter.sha256,
            "requested_start": manifest.get("requested_start") == lane.start.isoformat(),
            "requested_end": manifest.get("requested_end") == lane.end.isoformat(),
            "scrape_url_calls": int(manifest.get("scrape_url_calls", -1)) == 0,
            "request_total": int(observed.get("total", -1)) == int(expected.get("total", -2)),
            "request_robots": int(observed.get("robots", -1)) == int(expected.get("robots", -2)),
            "request_inventory": int(observed.get("inventory", -1)) == int(expected.get("inventory", -2)),
            "selected": int(manifest.get("selected", 0)) >= int(
                lane.acceptance.get("selected_min", 1)
            ),
            "fatal_error_absent": manifest.get("fatal_error") is None,
        }
        return all(checks.values()), {
            "checks": checks,
            "exact_identity_accounting": {
                "selected": int(manifest.get("selected", 0)),
                "request_total": int(observed.get("total", 0)),
                "article_requests": int(observed.get("article", 0)),
            },
            "remaining_failure_classes": {},
            "manifest_and_artifact_hashes": {
                "diagnostic_manifest.private.json": {
                    "sha256": sha256_path(path), "bytes": path.stat().st_size,
                }
            },
        }

    def run_lane(self, lane: LaneSpec, capacity: Mapping[str, object]) -> LaneResult:
        lane_dir = self.lane_dir(lane)
        output_dir = self.lane_output(lane)
        start_path = lane_dir / "start_receipt.json"
        terminal_path = lane_dir / "terminal_receipt.json"
        if terminal_path.is_file():
            return self.validate_lane_terminal(lane, terminal_path)
        if start_path.is_file():
            return self.stale_lane_outcome(lane, start_path, terminal_path)
        lane_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        pre_pins = self.verify_lane_pins(lane)
        input_facts = pre_pins.get("exact_input", {})
        command = self.build_command(lane, output_dir)
        command_digest = command_sha256(command)
        log_path = lane_dir / "child.log"
        child = CappedChild(command, self.child_environment(), log_path)
        started = iso_now()
        timed_out = False
        exit_code = None
        process = None
        quiescence = {}  # type: Mapping[str, object]
        log_accounting = {}  # type: Mapping[str, object]
        post_pins = None
        acceptance_ok = False
        acceptance_details = {}  # type: Mapping[str, object]
        contract_error = None
        try:
            process = child.start()
            start_payload = {
                "schema_version": 1,
                "kind": "exact_observation_lane_start",
                "sequence_id": self.plan.sequence_id,
                "phase_id": lane.phase_id,
                "lane_id": lane.lane_id,
                "source_id": lane.source_id,
                "started_at_utc": started,
                "plan_sha256": self.plan.plan_sha256,
                "code_git_commit": self.plan.code_git_commit,
                "command_sha256": command_digest,
                "adapter_sha256": lane.adapter.sha256,
                "execution_bundle_sha256": pin_bundle_sha256(
                    tuple(self.plan.execution_files) + (lane.adapter,)
                ),
                "execution_pin_verification": pre_pins,
                "private_input_count": input_facts.get("count"),
                "private_input_sha256": input_facts.get("file_sha256"),
                "parent_provenance_hashes": (
                    dict(lane.parent.artifacts) if lane.parent is not None else None
                ),
                "sealed_classification_provenance": pre_pins.get(
                    "sealed_classification_parent"
                ),
                "capacity_snapshot": capacity,
                "production_writes": False,
                "pid": process.pid,
            }
            try:
                write_json_exclusive(start_path, start_payload)
            except Exception:
                terminate_child(process)
                raise
            remaining = min(
                lane.timeout_seconds,
                max(0.1, self.deadline - time.monotonic()),
            )
            try:
                exit_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                quiescence = terminate_child(process)
                exit_code = process.returncode
            if not quiescence:
                quiescence = terminate_child(process)
            log_accounting = child.finish()
            post_pins = self.verify_lane_pins(lane)
            if child.drain_errors:
                contract_error = "child_log_drain_failed"
            elif timed_out:
                contract_error = "lane_timeout"
            elif exit_code != 0:
                contract_error = "child_exit_nonzero"
            elif lane.kind == "exact_urls":
                runs_root = output_dir / "runs"
                runs = sorted(path for path in runs_root.glob("observe-parallel-*") if path.is_dir())
                if len(runs) != 1:
                    contract_error = "exact_observer_run_directory_count"
                else:
                    acceptance_ok, acceptance_details = self.acceptance_exact(
                        lane, runs[0], input_facts,
                    )
            else:
                diagnostic = output_dir / "diagnostic_manifest.private.json"
                if not diagnostic.is_file():
                    contract_error = "diagnostic_manifest_missing"
                else:
                    acceptance_ok, acceptance_details = self.acceptance_diagnostic(lane, diagnostic)
        except Exception as exc:
            contract_error = "%s:%s" % (type(exc).__name__, str(exc)[:300])
            if process is not None:
                quiescence = terminate_child(process)
            try:
                if child.stream is not None and not child.stream.closed:
                    log_accounting = child.finish()
            except Exception as log_exc:
                log_accounting = {"error": type(log_exc).__name__}
            if post_pins is None:
                try:
                    post_pins = self.verify_lane_pins(lane)
                except Exception:
                    post_pins = None
        if not quiescence:
            quiescence = (
                terminate_child(process) if process is not None else
                {
                    "pid": None,
                    "exit_before_cleanup": None,
                    "exit_after_cleanup": None,
                    "process_group_cleanup": None,
                    "quiescent": True,
                }
            )
        terminal_capacity = self.guard()
        state = "observed_acceptance_passed" if acceptance_ok else "isolated_lane_failure"
        if timed_out:
            state = "isolated_lane_timeout"
        elif exit_code == 0 and not acceptance_ok and not contract_error:
            state = "observed_contract_shortfall"
        payload = {
            "schema_version": 1,
            "kind": "exact_observation_lane_terminal",
            "terminal": True,
            "state": state,
            "sequence_id": self.plan.sequence_id,
            "phase_id": lane.phase_id,
            "lane_id": lane.lane_id,
            "source_id": lane.source_id,
            "started_at_utc": started,
            "finished_at_utc": iso_now(),
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "command_sha256": command_digest,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "pre_run_pin_verification": pre_pins,
            "post_run_pin_verification": post_pins,
            "manifest_and_artifact_hashes": acceptance_details.get(
                "manifest_and_artifact_hashes", {},
            ),
            "exact_identity_accounting": acceptance_details.get(
                "exact_identity_accounting", {},
            ),
            "remaining_failure_classes": acceptance_details.get(
                "remaining_failure_classes", {},
            ),
            "acceptance_checks": acceptance_details.get("checks", {}),
            "contract_error": contract_error,
            "start_receipt_sha256": (
                sha256_path(start_path) if start_path.is_file() else None
            ),
            "log_hash_and_byte_accounting": log_accounting,
            "process_quiescence_proof": quiescence,
            "capacity_snapshot": terminal_capacity,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
        }
        write_json_exclusive(terminal_path, payload)
        self.event("lane_terminal", phase_id=lane.phase_id, lane_id=lane.lane_id, state=state)
        return self.validate_lane_terminal(lane, terminal_path)

    def capacity_blocked_lane(
        self, lane: LaneSpec, report: Mapping[str, object], reason: str,
    ) -> LaneResult:
        lane_dir = self.lane_dir(lane)
        lane_dir.mkdir(parents=True, exist_ok=True)
        terminal = lane_dir / "terminal_receipt.json"
        payload = {
            "schema_version": 1,
            "kind": "exact_observation_lane_terminal",
            "terminal": True,
            "state": "not_started_capacity_blocked",
            "sequence_id": self.plan.sequence_id,
            "phase_id": lane.phase_id,
            "lane_id": lane.lane_id,
            "source_id": lane.source_id,
            "finished_at_utc": iso_now(),
            "plan_sha256": self.plan.plan_sha256,
            "reason": reason,
            "capacity_snapshot": report,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
            "process_quiescence_proof": {"quiescent": True, "pid": None},
        }
        write_json_exclusive(terminal, payload)
        self.event("lane_not_started", phase_id=lane.phase_id, lane_id=lane.lane_id, reason=reason)
        return self.validate_lane_terminal(lane, terminal)

    def finalize_phase(
        self, phase: PhaseSpec, results: Sequence[LaneResult], path: Path,
    ) -> List[LaneResult]:
        phase_states = {}  # type: Dict[str, int]
        for result in results:
            phase_states[result.state] = phase_states.get(result.state, 0) + 1
        lane_hashes = {
            lane.lane_id: sha256_path(self.lane_dir(lane) / "terminal_receipt.json")
            for lane in phase.lanes
        }
        write_json_exclusive(path, {
            "schema_version": 1,
            "kind": "exact_observation_phase_terminal",
            "terminal": True,
            "sequence_id": self.plan.sequence_id,
            "phase_id": phase.phase_id,
            "plan_sha256": self.plan.plan_sha256,
            "code_git_commit": self.plan.code_git_commit,
            "finished_at_utc": iso_now(),
            "lane_count": len(phase.lanes),
            "state_counts": phase_states,
            "lane_terminal_sha256": lane_hashes,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
        })
        self.event("phase_terminal", phase_id=phase.phase_id, state_counts=phase_states)
        return self.validate_phase_terminal(phase, path)

    def run_phase(self, phase: PhaseSpec) -> List[LaneResult]:
        self.event("phase_started", phase_id=phase.phase_id, lane_count=len(phase.lanes))
        phase_path = self.sequence_dir / "phases" / safe_name(phase.phase_id) / "terminal_receipt.json"
        if phase_path.is_file():
            return self.validate_phase_terminal(phase, phase_path)
        results = []  # type: List[LaneResult]
        # Process bounded batches so later lanes refill a freed slot instead of
        # being mistaken for capacity failures while an earlier batch is live.
        for offset in range(0, len(phase.lanes), phase.max_active_lanes):
            batch = phase.lanes[offset:offset + phase.max_active_lanes]
            futures = {}
            with ThreadPoolExecutor(max_workers=phase.max_active_lanes) as pool:
                for lane in batch:
                    terminal = self.lane_dir(lane) / "terminal_receipt.json"
                    start = self.lane_dir(lane) / "start_receipt.json"
                    if terminal.is_file() or start.is_file():
                        futures[pool.submit(self.run_lane, lane, {})] = lane
                        continue
                    if time.monotonic() >= self.deadline:
                        results.append(self.capacity_blocked_lane(
                            lane, {}, "campaign_wall_deadline",
                        ))
                        continue
                    report = self.guard()
                    limits = report.get("sequence_limits")
                    if not isinstance(limits, Mapping):
                        recommended = report.get("recommended_limits")
                        recommended = recommended if isinstance(recommended, Mapping) else {}
                        limits = {
                            "network_workers": min(
                                NETWORK_CEILING, int(recommended.get("network_workers", 0)),
                            ),
                            "browser_workers": 0,
                        }
                    with self.active_lock:
                        available = int(limits.get("network_workers", 0)) - self.active_network
                        allowed = (
                            report.get("launch_allowed") is True
                            and int(limits.get("browser_workers", 0)) == 0
                            and available >= lane.workers
                        )
                        if allowed:
                            self.active_network += lane.workers
                    if not allowed:
                        results.append(self.capacity_blocked_lane(
                            lane, report, "host_guard_or_sequence_network_capacity",
                        ))
                        continue

                    def invoke(item: LaneSpec, snapshot: Mapping[str, object]) -> LaneResult:
                        try:
                            return self.run_lane(item, snapshot)
                        finally:
                            with self.active_lock:
                                self.active_network -= item.workers

                    futures[pool.submit(invoke, lane, report)] = lane
                for future in as_completed(futures):
                    lane = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        terminal = self.lane_dir(lane) / "terminal_receipt.json"
                        if terminal.is_file():
                            results.append(self.validate_lane_terminal(lane, terminal))
                        else:
                            report = self.guard()
                            results.append(self.capacity_blocked_lane(
                                lane, report,
                                "controller_lane_exception:%s" % type(exc).__name__,
                            ))
        return self.finalize_phase(phase, results, phase_path)

    def reconcile_stale_starts(self) -> None:
        for phase in self.plan.phases:
            for lane in phase.lanes:
                lane_dir = self.lane_dir(lane)
                start = lane_dir / "start_receipt.json"
                terminal = lane_dir / "terminal_receipt.json"
                if start.is_file() and not terminal.is_file():
                    self.stale_lane_outcome(lane, start, terminal)

    def run(self) -> Mapping[str, object]:
        with self.host_lock():
            self.sequence_dir.mkdir(parents=True, exist_ok=True)
            self.sequence_output.mkdir(parents=True, exist_ok=True)
            terminal = self.sequence_dir / "terminal_receipt.json"
            if terminal.is_file():
                return self.validate_sequence_terminal(terminal)
            startup = self.sequence_dir / "startup_receipt.json"
            attempt = self.sequence_dir / "attempt_receipt.json"
            if startup.is_file() and not self.resume:
                raise PlanError("startup receipt exists; explicit --resume is required")
            self.deadline = time.monotonic() + self.plan.max_wall_seconds
            pins = self.verify_global_pins()
            startup_capacity = self.guard()
            if not attempt.is_file():
                write_json_exclusive(attempt, {
                    "schema_version": 1,
                    "kind": "exact_observation_sequence_attempt",
                    "sequence_id": self.plan.sequence_id,
                    "started_at_utc": iso_now(),
                    "plan_sha256": self.plan.plan_sha256,
                    "code_git_commit": self.plan.code_git_commit,
                    "production_writes": False,
                })
            if not startup.is_file():
                write_json_exclusive(startup, {
                    "schema_version": 1,
                    "kind": "exact_observation_sequence_startup",
                    "sequence_id": self.plan.sequence_id,
                    "started_at_utc": iso_now(),
                    "plan_sha256": self.plan.plan_sha256,
                    "code_git_commit": self.plan.code_git_commit,
                    "execution_pin_verification": pins,
                    "capacity": {
                        "network": NETWORK_CEILING,
                        "browser": BROWSER_CEILING,
                        "log_max_bytes": OBSERVER_LOG_MAX_BYTES,
                    },
                    "host_capacity_guard": startup_capacity,
                    "production_writes": False,
                    "pid": os.getpid(),
                })
            else:
                self.validate_sequence_startup(startup)
            self.event("sequence_started", resume=self.resume)
            self.reconcile_stale_starts()
            all_results = []  # type: List[LaneResult]
            if startup_capacity.get("launch_allowed") is True:
                for phase in self.plan.phases:
                    all_results.extend(self.run_phase(phase))
            else:
                for phase in self.plan.phases:
                    phase_path = (
                        self.sequence_dir / "phases" / safe_name(phase.phase_id)
                        / "terminal_receipt.json"
                    )
                    if phase_path.is_file():
                        all_results.extend(self.validate_phase_terminal(phase, phase_path))
                        continue
                    phase_results = []  # type: List[LaneResult]
                    for lane in phase.lanes:
                        lane_terminal = self.lane_dir(lane) / "terminal_receipt.json"
                        if lane_terminal.is_file():
                            phase_results.append(self.validate_lane_terminal(lane, lane_terminal))
                        else:
                            phase_results.append(self.capacity_blocked_lane(
                                lane, startup_capacity, "sequence_startup_capacity_blocked",
                            ))
                    all_results.extend(self.finalize_phase(phase, phase_results, phase_path))
            terminal_capacity = self.guard()
            counts = {}  # type: Dict[str, int]
            for result in all_results:
                counts[result.state] = counts.get(result.state, 0) + 1
            successful = counts.get("observed_acceptance_passed", 0)
            status = "terminal_observation_sequence"
            if successful != len(all_results):
                status = "terminal_with_isolated_failures"
            lane_hashes = {
                lane.lane_id: sha256_path(self.lane_dir(lane) / "terminal_receipt.json")
                for phase in self.plan.phases for lane in phase.lanes
            }
            phase_hashes = {
                phase.phase_id: sha256_path(
                    self.sequence_dir / "phases" / safe_name(phase.phase_id)
                    / "terminal_receipt.json"
                )
                for phase in self.plan.phases
            }
            payload = {
                "schema_version": 1,
                "kind": "exact_observation_sequence_terminal",
                "terminal": True,
                "status": status,
                "sequence_id": self.plan.sequence_id,
                "finished_at_utc": iso_now(),
                "plan_sha256": self.plan.plan_sha256,
                "code_git_commit": self.plan.code_git_commit,
                "lane_count": len(all_results),
                "state_counts": counts,
                "lane_terminal_sha256": lane_hashes,
                "phase_terminal_sha256": phase_hashes,
                "startup_receipt_sha256": sha256_path(startup),
                "attempt_receipt_sha256": sha256_path(attempt),
                "post_run_pin_verification": self.verify_global_pins(),
                "terminal_capacity_snapshot": terminal_capacity,
                "production_writes": False,
                "verified_completion": False,
                "tracker_boundary_advancement": False,
            }
            write_json_exclusive(terminal, payload)
            self.event("sequence_terminal", status=status, state_counts=counts)
            return self.validate_sequence_terminal(terminal)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", required=True, type=Path)
    result.add_argument("--plan-sha256", required=True)
    result.add_argument("--deployment-root", required=True, type=Path)
    result.add_argument("--automation-root", required=True, type=Path)
    result.add_argument("--output-root", required=True, type=Path)
    result.add_argument("--receipt-root", required=True, type=Path)
    result.add_argument("--host-lock-path", required=True, type=Path)
    result.add_argument("--allowed-parent-root", action="append", required=True, type=Path)
    result.add_argument("--python", type=Path, default=Path(sys.executable))
    result.add_argument("--resume", action="store_true")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    runtime = RuntimePaths(
        python=args.python.resolve(),
        deployment_root=args.deployment_root.resolve(),
        automation_root=args.automation_root.resolve(),
        output_root=args.output_root.resolve(),
        receipt_root=args.receipt_root.resolve(),
        host_lock_path=args.host_lock_path.resolve(),
        allowed_parent_roots=tuple(path.resolve() for path in args.allowed_parent_root),
    )
    try:
        plan = load_plan(
            args.plan.resolve(), args.plan_sha256.lower(), runtime.deployment_root,
            runtime.allowed_parent_roots,
        )
        result = SequenceController(plan, runtime, resume=args.resume).run()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": result.get("status"),
        "sequence_id": result.get("sequence_id"),
        "plan_sha256": result.get("plan_sha256"),
    }, sort_keys=True))
    return 0 if result.get("status") == "terminal_observation_sequence" else 1


if __name__ == "__main__":
    raise SystemExit(main())
