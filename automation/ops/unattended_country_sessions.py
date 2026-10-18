#!/usr/bin/env python3
"""Run a hash-pinned, observe-only multi-country campaign unattended.

The input plan is prepared and reviewed separately.  This program never builds
or changes a plan and has no production-write option.  Five to ten country
sessions may run concurrently, while the sources inside each session always
run in their listed order.  Host-wide capacity is bounded to at most four
network workers and one browser worker, including live external
``parallel_observe_pure.py`` and browser-driver processes.

Each source is executed through :mod:`scraper_control.orchestrator` against a
dedicated campaign registry database.  The orchestrator stops at its promotion
dry run.  A separate acceptance audit classifies article dates against
``accept_start`` and ``end`` without changing the observation.  A terminal
receipt means that an attempt is finished and reviewable; it never means that
coverage or first-pass completion has been verified.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence


AUTOMATION_ROOT = Path(__file__).resolve().parents[1]
if str(AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_ROOT))

from scraper_control.observe import adapter_bundle_sha256  # noqa: E402
from scraper_control.orchestrator import discover_adapters  # noqa: E402
from scraper_control.registry import read_registry  # noqa: E402
from scraper_control.state import initialize, upsert_sources  # noqa: E402


UTC = timezone.utc
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_FILENAME = "terminal_receipt.json"
START_FILENAME = "start_receipt.json"
SAFE_TERMINAL_SOURCE_STATES = {
    "observed_dry_run_ready",
    "observed_needs_review",
    "isolated_source_failure",
    "deferred_source_locked",
    "skipped_tracker_terminal",
    "interrupted_requires_review",
    "systemic_failure",
}
TRACKER_SKIP_STATES = {
    "first-pass complete", "complete", "complete through target",
    "blocked", "running", "in progress",
}
SOURCE_LOCAL_INCOMPLETE_AUDIT_FINDINGS = frozenset({
    "artifacts_not_object",
    "artifact_record_missing",
    "observed_task_without_ready_audit",
    "required_artifact_absent",
})


class PlanError(ValueError):
    """The immutable campaign plan is unsafe or inconsistent."""


class CampaignAlreadyRunning(RuntimeError):
    """Another unattended campaign controller holds the host-wide lock."""


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_no_duplicates(text: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise PlanError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid JSON plan: {exc}") from exc


def _create_json(path: Path, value: object) -> None:
    """Create an immutable JSON receipt atomically and refuse replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, object]:
    value = _json_no_duplicates(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return f"{clean[:100]}-{hashlib.sha256(value.encode()).hexdigest()[:10]}"


def _date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{field} must be an ISO date") from exc


def _positive_int(value: object, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise PlanError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{field} must be an integer") from exc
    if number < minimum:
        raise PlanError(f"{field} must be at least {minimum}")
    return number


def _tracker_state(raw: object) -> Optional[str]:
    if not isinstance(raw, Mapping):
        return None
    normalized = {re.sub(r"[^a-z]", "", str(key).lower()): value for key, value in raw.items()}
    for key in ("firstpassstatus", "firstpassstate"):
        value = normalized.get(key)
        if value:
            return str(value).strip().lower()
    return None


@dataclass(frozen=True)
class ExecutionFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    adapter_path: Path
    adapter_sha256: str
    source_bundle_sha256: str
    execution_bundle_sha256: Optional[str]
    execution_bundle_files: tuple[ExecutionFile, ...]
    fetch_start: date
    accept_start: date
    end: date
    resource_class: str
    timeout_seconds: int
    tracker_state: Optional[str]

    @property
    def skip_from_tracker(self) -> bool:
        return self.tracker_state in TRACKER_SKIP_STATES


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    country: str
    sources: tuple[SourceSpec, ...]


@dataclass(frozen=True)
class CampaignPlan:
    campaign_id: str
    plan_sha256: str
    raw_bytes: bytes
    sessions: tuple[SessionSpec, ...]
    prelude_sources: tuple[SourceSpec, ...]
    country_session_limit: int
    network_limit: int
    browser_limit: int
    systemic_failure_threshold: int
    max_wall_seconds: int
    capacity_wait_seconds: int


@dataclass(frozen=True)
class RuntimePaths:
    python: Path
    automation_root: Path
    framework_root: Path
    registry_path: Path
    baseline_path: Path
    output_root: Path
    status_root: Path
    lock_root: Path
    receipt_root: Path
    min_free_gb: float = 0.0


@dataclass(frozen=True)
class ExecutionResult:
    state: str
    systemic_signature: Optional[str]
    details: Mapping[str, object]


def _resolve_adapter(raw: object, automation_root: Path, repository_path: object = None) -> Path:
    text = str(raw or "")
    if not text:
        raise PlanError("adapter_path is required")
    path = Path(text)
    if path.is_absolute():
        if path.is_file() or not repository_path:
            return path.resolve()
        path = Path(str(repository_path))
    candidates = [automation_root / path]
    if path.parts and path.parts[0] == "automation":
        candidates.append(automation_root.joinpath(*path.parts[1:]))
    candidates.append(automation_root.parent / path)
    existing = [item.resolve() for item in candidates if item.is_file()]
    if len(set(existing)) == 1:
        return existing[0]
    return candidates[0].resolve()


def _parse_source(raw: object, runtime: RuntimePaths, label: str) -> SourceSpec:
    if not isinstance(raw, Mapping):
        raise PlanError(f"{label} must be an object")
    source_id = str(raw.get("source_id") or "")
    if source_id.count(":") != 2:
        raise PlanError(f"{label}.source_id must be project:country:domain")
    adapter_hash = str(raw.get("adapter_sha256") or "").lower()
    bundle_hash = str(raw.get("source_bundle_sha256") or raw.get("adapter_bundle_sha256") or "").lower()
    if not HEX64.fullmatch(adapter_hash) or not HEX64.fullmatch(bundle_hash):
        raise PlanError(f"{label} requires exact adapter and source-bundle SHA-256 values")
    fetch_start = _date(raw.get("fetch_start"), f"{label}.fetch_start")
    accept_start = _date(raw.get("accept_start"), f"{label}.accept_start")
    end = _date(raw.get("end"), f"{label}.end")
    if not fetch_start <= accept_start <= end:
        raise PlanError(f"{label} requires fetch_start <= accept_start <= end")
    resource = str(raw.get("resource_class") or raw.get("lane") or "")
    if resource not in {"network", "browser"}:
        raise PlanError(f"{label}.resource_class must be network or browser")
    execution_hash_raw = raw.get("execution_bundle_sha256")
    execution_files_raw = raw.get("execution_bundle_files")
    execution_hash: Optional[str] = None
    execution_files: list[ExecutionFile] = []
    if execution_hash_raw is not None or execution_files_raw is not None:
        execution_hash = str(execution_hash_raw or "").lower()
        if not HEX64.fullmatch(execution_hash) or not isinstance(execution_files_raw, list) or not execution_files_raw:
            raise PlanError(f"{label} execution bundle requires a hash and nonempty file list")
        for index, descriptor in enumerate(execution_files_raw):
            if not isinstance(descriptor, Mapping):
                raise PlanError(f"{label}.execution_bundle_files[{index}] must be an object")
            digest = str(descriptor.get("sha256") or "").lower()
            if not HEX64.fullmatch(digest):
                raise PlanError(f"{label}.execution_bundle_files[{index}] has no SHA-256")
            execution_files.append(ExecutionFile(
                _resolve_adapter(
                    descriptor.get("path"), runtime.automation_root,
                    descriptor.get("repository_path"),
                ),
                digest,
            ))
    return SourceSpec(
        source_id=source_id,
        adapter_path=_resolve_adapter(
            raw.get("adapter_path"), runtime.automation_root,
            raw.get("adapter_repository_path"),
        ),
        adapter_sha256=adapter_hash,
        source_bundle_sha256=bundle_hash,
        execution_bundle_sha256=execution_hash,
        execution_bundle_files=tuple(execution_files),
        fetch_start=fetch_start,
        accept_start=accept_start,
        end=end,
        resource_class=resource,
        timeout_seconds=_positive_int(raw.get("timeout_seconds", 14400), f"{label}.timeout_seconds"),
        tracker_state=_tracker_state(
            raw.get("tracker_provenance", raw.get("tracker_snapshot"))
        ),
    )


def load_plan(path: Path, expected_sha256: str, runtime: RuntimePaths) -> CampaignPlan:
    expected_sha256 = expected_sha256.lower()
    if not HEX64.fullmatch(expected_sha256):
        raise PlanError("--plan-sha256 must be 64 lowercase hexadecimal characters")
    raw_bytes = path.read_bytes()
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != expected_sha256:
        raise PlanError(f"plan hash mismatch: expected {expected_sha256}, got {actual}")
    payload = _json_no_duplicates(raw_bytes.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise PlanError("plan must be a JSON object")
    if payload.get("schema_version") != 1:
        raise PlanError("plan schema_version must be 1")
    if payload.get("immutable") is not True:
        raise PlanError("plan must declare immutable=true")
    if payload.get("production_writes") is not False:
        raise PlanError("plan must declare production_writes=false")
    if payload.get("mode") not in (
        None, "observation_dry_run", "observe_only",
        "observation_and_promotion_dry_run_only",
    ):
        raise PlanError("plan mode must be observation/dry-run only")
    campaign_id = str(payload.get("campaign_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", campaign_id):
        raise PlanError("campaign_id is empty or unsafe")
    concurrency = payload.get("concurrency")
    if not isinstance(concurrency, Mapping):
        raise PlanError("concurrency object is required")
    network_limit = _positive_int(concurrency.get("network"), "concurrency.network")
    browser_raw = concurrency.get("browser", 0)
    if isinstance(browser_raw, bool):
        raise PlanError("concurrency.browser must be an integer")
    browser_limit = int(browser_raw)
    if network_limit > 4 or not 0 <= browser_limit <= 1:
        raise PlanError("capacity may not exceed four network and one browser workers")
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list) or not 5 <= len(raw_sessions) <= 10:
        raise PlanError("plan must contain five to ten country sessions")
    if payload.get("session_count") is not None and int(payload["session_count"]) != len(raw_sessions):
        raise PlanError("session_count does not match sessions")
    country_session_limit = _positive_int(
        concurrency.get("max_active_country_sessions", concurrency.get("country_sessions", len(raw_sessions))),
        "concurrency.country_sessions",
    )
    if country_session_limit > len(raw_sessions):
        raise PlanError("active country-session capacity exceeds logical session count")
    sessions: list[SessionSpec] = []
    all_ids: list[str] = []
    session_ids: set[str] = set()
    for session_index, raw_session in enumerate(raw_sessions):
        label = f"sessions[{session_index}]"
        if not isinstance(raw_session, Mapping):
            raise PlanError(f"{label} must be an object")
        session_id = str(raw_session.get("session_id") or "")
        country = str(raw_session.get("country") or raw_session.get("country_code") or "").upper()
        if not session_id or session_id in session_ids or not country:
            raise PlanError(f"{label} has an empty/duplicate session_id or country")
        raw_sources = raw_session.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise PlanError(f"{label}.sources must be a nonempty list")
        sources = tuple(_parse_source(item, runtime, f"{label}.sources[{index}]") for index, item in enumerate(raw_sources))
        for source in sources:
            if source.source_id.split(":", 2)[1].upper() != country:
                raise PlanError(f"{source.source_id} does not belong to session country {country}")
            all_ids.append(source.source_id)
        session_ids.add(session_id)
        sessions.append(SessionSpec(session_id, country, sources))
    raw_prelude = payload.get("prelude_sources", [])
    if not isinstance(raw_prelude, list):
        raise PlanError("prelude_sources must be a list")
    prelude = tuple(_parse_source(item, runtime, f"prelude_sources[{index}]") for index, item in enumerate(raw_prelude))
    for source in prelude:
        if source.source_id.split(":", 2)[1].upper() not in {"INT", "REG"}:
            raise PlanError("prelude_sources may contain only INT or REG sources")
        all_ids.append(source.source_id)
    if len(all_ids) != len(set(all_ids)):
        raise PlanError("source_id values must be unique across the campaign")
    country_source_count = sum(len(session.sources) for session in sessions)
    declared_sources = payload.get("source_count")
    if declared_sources is not None and int(declared_sources) != len(all_ids):
        raise PlanError("source_count does not match all campaign sources")
    declared_country = payload.get("country_source_count")
    if declared_country is not None and int(declared_country) != country_source_count:
        raise PlanError("country_source_count does not match country-session sources")
    declared_prelude = payload.get("prelude_source_count")
    if declared_prelude is not None and int(declared_prelude) != len(prelude):
        raise PlanError("prelude_source_count does not match prelude_sources")
    declared_total = payload.get("total_source_count")
    if declared_total is not None and int(declared_total) != len(all_ids):
        raise PlanError("total_source_count does not match all campaign sources")
    if browser_limit == 0 and any(source.resource_class == "browser" for source in (*prelude, *(s for session in sessions for s in session.sources))):
        raise PlanError("browser source present while concurrency.browser is zero")
    return CampaignPlan(
        campaign_id=campaign_id,
        plan_sha256=actual,
        raw_bytes=raw_bytes,
        sessions=tuple(sessions),
        prelude_sources=prelude,
        country_session_limit=country_session_limit,
        network_limit=network_limit,
        browser_limit=browser_limit,
        systemic_failure_threshold=_positive_int(payload.get("systemic_failure_threshold", 3), "systemic_failure_threshold"),
        max_wall_seconds=_positive_int(payload.get("max_wall_seconds", 86400), "max_wall_seconds"),
        capacity_wait_seconds=_positive_int(payload.get("capacity_wait_seconds", 43200), "capacity_wait_seconds"),
    )


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def append(self, kind: str, **values: object) -> None:
        payload = json.dumps({"at_utc": iso_now(), "kind": kind, **values}, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())


def live_external_capacity(proc_root: Path = Path("/proc")) -> tuple[int, int]:
    """Count external bounded-network workers and browser driver processes."""

    network = 0
    browser = 0
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return 0, 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            args = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue
        joined = " ".join(args)
        if any(Path(arg).name == "parallel_observe_pure.py" for arg in args):
            workers = 1
            if "--workers" in args:
                try:
                    workers = max(1, int(args[args.index("--workers") + 1]))
                except (IndexError, ValueError):
                    workers = 1
            network += workers
        executable_names = {Path(arg).name.lower() for arg in args[:2]}
        if executable_names & {"chromedriver", "geckodriver"} or ("playwright" in joined.lower() and "run-driver" in joined.lower()):
            browser += 1
    return network, browser


def execution_bundle_sha256(paths: Sequence[Path], automation_root: Path) -> str:
    root = automation_root.resolve()
    records = sorted(
        ((path.resolve().relative_to(root).as_posix(), path.resolve()) for path in paths),
        key=lambda item: item[0],
    )
    digest = hashlib.sha256()
    for relative, path in records:
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def matching_live_process(source_id: str, controller_pid: object, proc_root: Path = Path("/proc")) -> bool:
    """Return true only for a matching live controller or source subprocess."""

    try:
        pid = int(controller_pid)
    except (TypeError, ValueError):
        pid = -1
    candidates = {pid}
    if proc_root.exists():
        try:
            candidates.update(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit())
        except OSError:
            pass
    for candidate in candidates:
        if candidate <= 0:
            continue
        try:
            args = [part.decode("utf-8", "replace") for part in
                    (proc_root / str(candidate) / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue
        joined = " ".join(args)
        if candidate == pid and any(Path(arg).name == "unattended_country_sessions.py" for arg in args):
            return True
        if source_id in joined and "scraper_control.orchestrator" in joined:
            return True
    return False


def planned_source_process_active(source: SourceSpec, proc_root: Path = Path("/proc")) -> bool:
    """Detect an identical source already owned by another observation runner."""

    if not proc_root.exists():
        return False
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return False
    adapter = str(source.adapter_path)
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = [part.decode("utf-8", "replace") for part in
                    (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue
        joined = " ".join(args)
        is_observer = "parallel_observe_pure.py" in joined or "scraper_control.orchestrator" in joined
        if is_observer and (adapter in args or source.source_id in joined):
            return True
    return False


class CapacityGate:
    def __init__(
        self,
        network_limit: int,
        browser_limit: int,
        events: EventLog,
        probe: Callable[[], tuple[int, int]] = live_external_capacity,
    ):
        self.limits = {"network": network_limit, "browser": browser_limit}
        self.held = {"network": 0, "browser": 0}
        self.events = events
        self.probe = probe
        self.condition = threading.Condition()

    @contextmanager
    def acquire(self, resource: str, source_id: str, deadline: float) -> Iterator[bool]:
        waited = False
        with self.condition:
            while True:
                external_network, external_browser = self.probe()
                external = {"network": external_network, "browser": external_browser}
                if external[resource] + self.held[resource] < self.limits[resource]:
                    self.held[resource] += 1
                    self.events.append(
                        "capacity_acquired", source_id=source_id, resource_class=resource,
                        external=external, campaign_held=dict(self.held), limits=dict(self.limits),
                    )
                    acquired = True
                    break
                if not waited:
                    self.events.append(
                        "capacity_wait", source_id=source_id, resource_class=resource,
                        external=external, campaign_held=dict(self.held), limits=dict(self.limits),
                    )
                    waited = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    acquired = False
                    break
                self.condition.wait(timeout=min(2.0, remaining))
        try:
            yield acquired
        finally:
            if acquired:
                with self.condition:
                    self.held[resource] -= 1
                    self.condition.notify_all()
                self.events.append("capacity_released", source_id=source_id, resource_class=resource)


class SystemicCircuit:
    def __init__(self, threshold: int, events: EventLog):
        self.threshold = threshold
        self.counts: Counter[str] = Counter()
        self.open_signature: Optional[str] = None
        self.lock = threading.Lock()
        self.events = events

    def record(self, signature: Optional[str], source_id: str) -> None:
        if not signature:
            return
        with self.lock:
            self.counts[signature] += 1
            if self.counts[signature] >= self.threshold and self.open_signature is None:
                self.open_signature = signature
                self.events.append(
                    "systemic_circuit_open", signature=signature,
                    failure_count=self.counts[signature], source_id=source_id,
                )

    def is_open(self) -> bool:
        with self.lock:
            return self.open_signature is not None


def _artifact_path(record: object, output_root: Path) -> Optional[Path]:
    if not isinstance(record, Mapping) or not record.get("exists") or not record.get("path"):
        return None
    root = record.get("root")
    path = Path(str(record["path"]))
    if root == "output_root":
        return (output_root / path).resolve()
    if root == "absolute":
        return path.resolve()
    return None


def _source_local_cycle_audit_failure(
    verification: Mapping[str, object], expected_source_id: str,
) -> bool:
    """Return whether verification found only incomplete evidence for one source.

    A source that did not emit its ready audit or one of its required artifact
    records needs an adapter/source repair. It does not demonstrate that the
    controller, host, or shared runtime is broken. Require every finding to be
    one of those narrow incomplete-evidence findings and to name the expected
    source so structural, integrity, and cross-source failures still reach the
    global circuit.
    """

    findings = verification.get("findings")
    if not isinstance(findings, list) or not findings:
        return False
    for finding in findings:
        if not isinstance(finding, Mapping):
            return False
        if finding.get("issue") not in SOURCE_LOCAL_INCOMPLETE_AUDIT_FINDINGS:
            return False
        if finding.get("source_id") != expected_source_id:
            return False
    return True


def _published_day(value: object) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def acceptance_audit(
    articles_path: Optional[Path],
    field_checks_path: Optional[Path],
    source: SourceSpec,
    output_path: Path,
) -> dict[str, object]:
    """Classify saved rows against the acceptance window without mutation."""

    eligible_by_url: dict[str, bool] = {}
    if field_checks_path and field_checks_path.is_file():
        with field_checks_path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    eligible_by_url[str(row.get("url") or "")] = row.get("eligible") is True
                except (json.JSONDecodeError, AttributeError) as exc:
                    raise ValueError(f"invalid field-check JSONL line {line_number}") from exc
    classifications: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    if articles_path and articles_path.is_file():
        with articles_path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid article JSONL line {line_number}") from exc
                url = str(row.get("url") or "")
                published = _published_day(row.get("date_publish"))
                if published is None:
                    classification = "undated_or_invalid"
                elif published < source.fetch_start:
                    classification = "before_fetch_window"
                elif published < source.accept_start:
                    classification = "prefetch_only_before_accept"
                elif published > source.end:
                    classification = "after_campaign_end"
                else:
                    classification = "inside_accept_window"
                counts[classification] += 1
                classifications.append({
                    "url": url,
                    "date_publish": published.isoformat() if published else row.get("date_publish"),
                    "classification": classification,
                    "eligible_in_observation": eligible_by_url.get(url),
                })
    accepted_eligible = sum(
        1 for row in classifications
        if row["classification"] == "inside_accept_window" and row["eligible_in_observation"] is True
    )
    outside_accept_eligible = sum(
        1 for row in classifications
        if row["classification"] != "inside_accept_window" and row["eligible_in_observation"] is True
    )
    accepted_urls = sorted({
        str(row["url"]) for row in classifications
        if row["classification"] == "inside_accept_window"
        and row["eligible_in_observation"] is True and row["url"]
    })
    accepted_url_payload = "".join(f"{url}\n" for url in accepted_urls).encode("utf-8")
    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "terminal_acceptance_window_audit",
        "source_id": source.source_id,
        "generated_at_utc": iso_now(),
        "window": {
            "fetch_start": source.fetch_start.isoformat(),
            "accept_start": source.accept_start.isoformat(),
            "end": source.end.isoformat(),
        },
        "mode": "read_only_classification",
        "production_writes": False,
        "verification_state": "classified_for_review_not_verified_completion",
        "input_artifacts": {
            "articles": {
                "path": str(articles_path) if articles_path else None,
                "sha256": sha256_path(articles_path) if articles_path and articles_path.is_file() else None,
            },
            "field_checks": {
                "path": str(field_checks_path) if field_checks_path else None,
                "sha256": sha256_path(field_checks_path) if field_checks_path and field_checks_path.is_file() else None,
            },
        },
        "counts": {
            **dict(sorted(counts.items())),
            "total_articles": len(classifications),
            "accepted_eligible": accepted_eligible,
            "outside_accept_eligible": outside_accept_eligible,
        },
        "accepted_eligible_url_count": len(accepted_urls),
        "accepted_eligible_url_set_sha256": hashlib.sha256(accepted_url_payload).hexdigest(),
        "accepted_eligible_urls": accepted_urls,
        "rows": classifications,
    }
    _create_json(output_path, result)
    return result


def build_orchestrator_command(
    runtime: RuntimePaths,
    state_path: Path,
    source: SourceSpec,
    output_root: Path,
    status_root: Path,
) -> list[str]:
    """Build the only child command.  Deliberately has no commit argument."""

    return [
        str(runtime.python), "-m", "scraper_control.orchestrator",
        "--state", str(state_path),
        "--adapter-root", str(runtime.automation_root / "adapters"),
        "--framework-root", str(runtime.framework_root),
        "--baseline", str(runtime.baseline_path),
        "--output-root", str(output_root),
        "--status-root", str(status_root),
        "--lock-root", str(runtime.lock_root),
        "--source-id", source.source_id,
        "--start", source.fetch_start.isoformat(),
        "--end", source.end.isoformat(),
        "--workers", "1", "--limit", "1", "--force",
        "--heartbeat-seconds", "30", "--min-free-gb", str(runtime.min_free_gb),
    ]


def _terminate_process_group(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


class OrchestratorSourceExecutor:
    def __init__(self, runtime: RuntimePaths, state_path: Path, campaign_id: str):
        self.runtime = runtime
        self.state_path = state_path
        self.campaign_id = campaign_id

    def __call__(self, source: SourceSpec, source_dir: Path, deadline: float) -> ExecutionResult:
        key = _safe_name(source.source_id)
        output_root = self.runtime.output_root / self.campaign_id / key
        status_root = self.runtime.status_root / self.campaign_id / key
        output_root.mkdir(parents=True, exist_ok=True)
        status_root.mkdir(parents=True, exist_ok=True)
        log_path = source_dir / "operator.log"
        command = build_orchestrator_command(self.runtime, self.state_path, source, output_root, status_root)
        env = os.environ.copy()
        pythonpath = [str(self.runtime.automation_root), str(self.runtime.framework_root)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        timed_out = False
        with log_path.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(
                command, cwd=str(self.runtime.automation_root.parent), env=env,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            )
            timeout = max(0.1, min(source.timeout_seconds, deadline - time.monotonic()))
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
                returncode = process.returncode
            stream.flush()
            os.fsync(stream.fileno())
        if timed_out:
            return ExecutionResult("systemic_failure", "source_timeout", {
                "runner_exit_code": returncode, "timed_out": True,
                "operator_log": {"path": str(log_path), "sha256": sha256_path(log_path)},
            })
        audits = sorted((status_root / "runs").glob("*.audit.json"))
        if len(audits) != 1:
            return ExecutionResult("systemic_failure", "orchestrator_audit_count", {
                "runner_exit_code": returncode, "audit_count": len(audits),
                "operator_log": {"path": str(log_path), "sha256": sha256_path(log_path)},
            })
        audit_path = audits[0]
        audit = _load_json(audit_path)
        verification_path = source_dir / "cycle_audit_verification.json"
        verifier = self.runtime.automation_root / "scripts/verify_cycle_audit.py"
        checked = subprocess.run(
            [str(self.runtime.python), str(verifier), str(audit_path), "--output", str(verification_path)],
            cwd=str(self.runtime.automation_root.parent), env=env,
            capture_output=True, text=True, timeout=180, check=False,
        )
        try:
            verification = _load_json(verification_path)
        except (OSError, ValueError):
            verification = {}
        if checked.returncode != 0 or verification.get("passed") is not True:
            source_local = (
                checked.returncode == 1
                and _source_local_cycle_audit_failure(verification, source.source_id)
            )
            return ExecutionResult(
                "isolated_source_failure" if source_local else "systemic_failure",
                None if source_local else "cycle_audit_verification_failed",
                {
                    "runner_exit_code": returncode,
                    "verification_exit_code": checked.returncode,
                    "failure_reason": "cycle_audit_verification_failed",
                    "verification_findings": verification.get("findings", []),
                    "audit": {"path": str(audit_path), "sha256": sha256_path(audit_path)},
                    "verification": {
                        "path": str(verification_path) if verification_path.exists() else None,
                        "sha256": sha256_path(verification_path) if verification_path.exists() else None,
                    },
                },
            )
        sources = audit.get("sources")
        if audit.get("commit_enabled") is not False or not isinstance(sources, list) or len(sources) != 1:
            return ExecutionResult("systemic_failure", "unsafe_or_invalid_cycle_audit", {
                "commit_enabled": audit.get("commit_enabled"), "source_count": len(sources or []),
            })
        item = sources[0]
        if item.get("source_id") != source.source_id:
            return ExecutionResult("systemic_failure", "cycle_source_mismatch", {"observed_source_id": item.get("source_id")})
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), Mapping) else {}
        manifest_path = _artifact_path(artifacts.get("observation_manifest"), output_root)
        if manifest_path is None:
            return ExecutionResult("isolated_source_failure", None, {
                "failure_reason": "observation_manifest_missing",
                "orchestrator_task_status": str(item.get("status") or ""),
            })
        manifest = _load_json(manifest_path)
        unsafe = (
            manifest.get("mode") != "observation_no_production_writes"
            or manifest.get("requested_start") != source.fetch_start.isoformat()
            or manifest.get("requested_end") != source.end.isoformat()
            or manifest.get("source_sha256") != source.adapter_sha256
            or manifest.get("source_bundle_sha256") != source.source_bundle_sha256
            or bool(_artifact_path(artifacts.get("promotion_commit"), output_root))
            or (manifest_path.parent / "promotion.json").exists()
        )
        if unsafe:
            return ExecutionResult("systemic_failure", "observation_safety_contract_failed", {
                "manifest_path": str(manifest_path), "manifest_mode": manifest.get("mode"),
                "manifest_source_sha256": manifest.get("source_sha256"),
                "manifest_bundle_sha256": manifest.get("source_bundle_sha256"),
            })
        articles = _artifact_path(artifacts.get("parsed_articles"), output_root)
        checks = _artifact_path(artifacts.get("field_checks"), output_root)
        acceptance_path = source_dir / "acceptance_audit.json"
        acceptance = acceptance_audit(articles, checks, source, acceptance_path)
        task_state = str(item.get("status") or "")
        acceptance_scope_blocked = int(acceptance["counts"].get("outside_accept_eligible", 0)) > 0
        if task_state == "ready_for_commit" and not acceptance_scope_blocked:
            state = "observed_dry_run_ready"
            signature = None
        elif task_state in {"ready_for_commit", "needs_review"}:
            state = "observed_needs_review"
            signature = None
        elif task_state == "locked":
            state = "deferred_source_locked"
            signature = None
        else:
            state = "isolated_source_failure"
            signature = None
        return ExecutionResult(state, signature, {
            "runner_exit_code": returncode,
            "orchestrator_task_status": task_state,
            "dry_run_ready": task_state == "ready_for_commit" and not acceptance_scope_blocked,
            "acceptance_scope_blocked": acceptance_scope_blocked,
            "verified_completion": False,
            "audit": {"path": str(audit_path), "sha256": sha256_path(audit_path)},
            "verification": {"path": str(verification_path), "sha256": sha256_path(verification_path)},
            "acceptance_audit": {"path": str(acceptance_path), "sha256": sha256_path(acceptance_path),
                                 "counts": acceptance["counts"]},
            "operator_log": {"path": str(log_path), "sha256": sha256_path(log_path)},
        })


class CampaignController:
    def __init__(
        self,
        plan: CampaignPlan,
        runtime: RuntimePaths,
        *,
        executor: Optional[Callable[[SourceSpec, Path, float], ExecutionResult]] = None,
        capacity_probe: Callable[[], tuple[int, int]] = live_external_capacity,
    ):
        self.plan = plan
        self.runtime = runtime
        self.campaign_dir = runtime.receipt_root / plan.campaign_id
        self.events = EventLog(self.campaign_dir / "events.jsonl")
        self.capacity = CapacityGate(plan.network_limit, plan.browser_limit, self.events, capacity_probe)
        self.circuit = SystemicCircuit(plan.systemic_failure_threshold, self.events)
        self.state_path = self.campaign_dir / "campaign_control.db"
        self.executor = executor
        self.deadline = 0.0

    @contextmanager
    def outer_lock(self) -> Iterator[None]:
        self.runtime.receipt_root.mkdir(parents=True, exist_ok=True)
        path = self.runtime.receipt_root / ".unattended_country_sessions.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CampaignAlreadyRunning(f"unattended controller lock is held: {path}") from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (json.dumps({"pid": os.getpid(), "campaign_id": self.plan.campaign_id,
                                             "acquired_at_utc": iso_now()}) + "\n").encode())
            os.fsync(descriptor)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _pin_plan(self) -> None:
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        pinned = self.campaign_dir / "plan.json"
        if pinned.exists():
            if pinned.read_bytes() != self.plan.raw_bytes or sha256_path(pinned) != self.plan.plan_sha256:
                raise PlanError("campaign plan changed after its first launch")
        else:
            descriptor = os.open(pinned, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            try:
                os.write(descriptor, self.plan.raw_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        receipt = self.campaign_dir / "plan_receipt.json"
        if not receipt.exists():
            _create_json(receipt, {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "plan_path": str(pinned), "plan_sha256": self.plan.plan_sha256,
                "pinned_at_utc": iso_now(), "production_writes": False,
            })

    def _prepare_state(self) -> None:
        if self.state_path.exists():
            return
        records = read_registry(self.runtime.registry_path)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        initialize(temporary)
        upsert_sources(temporary, records)
        os.replace(temporary, self.state_path)

    def preflight(self) -> dict[str, object]:
        errors: list[str] = []
        for path, label in (
            (self.runtime.python, "python"),
            (self.runtime.registry_path, "source_registry"),
            (self.runtime.baseline_path, "baseline"),
            (self.runtime.framework_root, "framework_root"),
        ):
            if not path.exists():
                errors.append(f"missing_{label}:{path}")
        catalog = {}
        try:
            catalog = discover_adapters(self.runtime.automation_root / "adapters")
        except Exception as exc:
            errors.append(f"adapter_catalog:{type(exc).__name__}:{exc}")
        registry_ids: set[str] = set()
        try:
            registry_ids = {record.source_id for record in read_registry(self.runtime.registry_path)}
        except Exception as exc:
            errors.append(f"source_registry:{type(exc).__name__}:{exc}")
        all_sources = [*self.plan.prelude_sources, *(source for session in self.plan.sessions for source in session.sources)]
        for source in all_sources:
            if source.source_id not in registry_ids:
                errors.append(f"registry_missing:{source.source_id}")
            if not source.adapter_path.is_file():
                errors.append(f"adapter_missing:{source.source_id}:{source.adapter_path}")
                continue
            if sha256_path(source.adapter_path) != source.adapter_sha256:
                errors.append(f"adapter_hash_drift:{source.source_id}")
            if adapter_bundle_sha256(source.adapter_path) != source.source_bundle_sha256:
                errors.append(f"adapter_bundle_hash_drift:{source.source_id}")
            for descriptor in source.execution_bundle_files:
                if not descriptor.path.is_file():
                    errors.append(f"execution_bundle_file_missing:{source.source_id}:{descriptor.path}")
                elif sha256_path(descriptor.path) != descriptor.sha256:
                    errors.append(f"execution_bundle_file_drift:{source.source_id}:{descriptor.path}")
            if source.execution_bundle_sha256 and all(item.path.is_file() for item in source.execution_bundle_files):
                if execution_bundle_sha256(
                    [item.path for item in source.execution_bundle_files], self.runtime.automation_root
                ) != source.execution_bundle_sha256:
                    errors.append(f"execution_bundle_hash_drift:{source.source_id}")
            catalog_item = catalog.get(source.source_id)
            if catalog_item is None or catalog_item.path.resolve() != source.adapter_path.resolve():
                errors.append(f"adapter_catalog_mismatch:{source.source_id}")
        return {
            "schema_version": 1,
            "campaign_id": self.plan.campaign_id,
            "plan_sha256": self.plan.plan_sha256,
            "checked_at_utc": iso_now(),
            "passed": not errors,
            "errors": errors,
            "production_writes": False,
            "session_count": len(self.plan.sessions),
            "prelude_source_count": len(self.plan.prelude_sources),
            "source_count": len(all_sources),
            "capacity": {"country_sessions": self.plan.country_session_limit,
                         "network": self.plan.network_limit, "browser": self.plan.browser_limit},
        }

    def _source_dir(self, group: str, source: SourceSpec) -> Path:
        return self.campaign_dir / group / "sources" / _safe_name(source.source_id)

    def _run_source(self, group: str, source: SourceSpec) -> dict[str, object]:
        source_dir = self._source_dir(group, source)
        terminal_path = source_dir / TERMINAL_FILENAME
        start_path = source_dir / START_FILENAME
        if terminal_path.exists():
            receipt = _load_json(terminal_path)
            self.events.append("source_resume_terminal", source_id=source.source_id, group=group,
                               terminal_state=receipt.get("state"))
            return receipt
        if start_path.exists():
            start = _load_json(start_path)
            live = (
                start.get("hostname") == socket.gethostname()
                and matching_live_process(source.source_id, start.get("pid"))
            )
            if live:
                self.events.append("source_active_no_duplicate", source_id=source.source_id, group=group)
                return {
                    "schema_version": 1, "source_id": source.source_id,
                    "state": "active_or_incomplete_no_relaunch", "terminal": False,
                    "systemic_signature": None,
                }
            receipt = {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "source_id": source.source_id, "state": "interrupted_requires_review",
                "terminal": True, "finished_at_utc": iso_now(),
                "production_writes": False, "verified_completion": False,
                "completion_semantics": "stale start was closed without relaunch; inspect preserved artifacts",
                "start_receipt": {"path": str(start_path), "sha256": sha256_path(start_path)},
            }
            _create_json(terminal_path, receipt)
            self.events.append("source_stale_start_closed", source_id=source.source_id, group=group)
            return receipt
        if planned_source_process_active(source):
            self.events.append("source_active_external_no_duplicate", source_id=source.source_id, group=group)
            return {
                "schema_version": 1, "source_id": source.source_id,
                "state": "active_or_incomplete_no_relaunch", "terminal": False,
                "systemic_signature": None, "external_active": True,
            }
        if self.circuit.is_open():
            return {"source_id": source.source_id, "state": "not_started_circuit_open", "terminal": False}
        if source.skip_from_tracker:
            start = {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "source_id": source.source_id, "started_at_utc": iso_now(),
                "pid": os.getpid(), "production_writes": False,
                "plan_sha256": self.plan.plan_sha256,
            }
            _create_json(start_path, start)
            receipt = {
                **start, "finished_at_utc": iso_now(), "terminal": True,
                "state": "skipped_tracker_terminal", "tracker_state": source.tracker_state,
                "verified_completion": False,
            }
            _create_json(terminal_path, receipt)
            return receipt
        capacity_deadline = min(self.deadline, time.monotonic() + self.plan.capacity_wait_seconds)
        with self.capacity.acquire(source.resource_class, source.source_id, capacity_deadline) as acquired:
            if not acquired:
                result = ExecutionResult("systemic_failure", "capacity_wait_timeout", {"capacity_wait_timed_out": True})
            else:
                start = {
                    "schema_version": 1, "campaign_id": self.plan.campaign_id,
                    "source_id": source.source_id, "group": group,
                    "started_at_utc": iso_now(), "pid": os.getpid(),
                    "hostname": socket.gethostname(), "plan_sha256": self.plan.plan_sha256,
                    "adapter_path": str(source.adapter_path),
                    "adapter_sha256": source.adapter_sha256,
                    "source_bundle_sha256": source.source_bundle_sha256,
                    "execution_bundle_sha256": source.execution_bundle_sha256,
                    "resource_class": source.resource_class,
                    "window": {"fetch_start": source.fetch_start.isoformat(),
                               "accept_start": source.accept_start.isoformat(), "end": source.end.isoformat()},
                    "mode": "observation_and_promotion_dry_run_only",
                    "production_writes": False,
                }
                _create_json(start_path, start)
                self.events.append("source_start", source_id=source.source_id, group=group,
                                   resource_class=source.resource_class)
                try:
                    assert self.executor is not None
                    result = self.executor(source, source_dir, min(self.deadline, time.monotonic() + source.timeout_seconds))
                except Exception as exc:
                    result = ExecutionResult("systemic_failure", "source_executor_exception", {
                        "error_type": type(exc).__name__, "error": str(exc)[:2000],
                    })
        if not start_path.exists():
            _create_json(start_path, {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "source_id": source.source_id, "started_at_utc": iso_now(),
                "pid": os.getpid(), "plan_sha256": self.plan.plan_sha256,
                "production_writes": False, "launch_state": "capacity_not_acquired",
            })
        receipt = {
            "schema_version": 1,
            "campaign_id": self.plan.campaign_id,
            "source_id": source.source_id,
            "state": result.state,
            "systemic_signature": result.systemic_signature,
            "terminal": True,
            "finished_at_utc": iso_now(),
            "mode": "observation_and_promotion_dry_run_only",
            "production_writes": False,
            "verified_completion": False,
            "completion_semantics": "terminal observation evidence only; tracker advancement requires later review",
            "plan_sha256": self.plan.plan_sha256,
            "adapter_sha256": source.adapter_sha256,
            "source_bundle_sha256": source.source_bundle_sha256,
            "execution_bundle_sha256": source.execution_bundle_sha256,
            "window": {"fetch_start": source.fetch_start.isoformat(),
                       "accept_start": source.accept_start.isoformat(), "end": source.end.isoformat()},
            "details": dict(result.details),
        }
        _create_json(terminal_path, receipt)
        self.circuit.record(result.systemic_signature, source.source_id)
        self.events.append("source_terminal", source_id=source.source_id, group=group,
                           state=result.state, systemic_signature=result.systemic_signature)
        return receipt

    def _run_session(self, session: SessionSpec) -> dict[str, object]:
        session_dir = self.campaign_dir / "sessions" / _safe_name(session.session_id)
        terminal_path = session_dir / TERMINAL_FILENAME
        if terminal_path.exists():
            return _load_json(terminal_path)
        source_receipts: list[dict[str, object]] = []
        status = "terminal_observation_sequence"
        for source in session.sources:
            if self.circuit.is_open() or time.monotonic() >= self.deadline:
                status = "stopped_before_remaining_sources"
                break
            receipt = self._run_source(f"sessions/{_safe_name(session.session_id)}", source)
            source_receipts.append(receipt)
            if receipt.get("state") == "active_or_incomplete_no_relaunch":
                status = "blocked_by_active_or_incomplete_source"
                break
        result = {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "session_id": session.session_id, "country": session.country,
            "finished_at_utc": iso_now(), "status": status,
            "production_writes": False, "verified_completion": False,
            "planned_sources": len(session.sources), "terminal_or_seen_sources": len(source_receipts),
            "sources": [
                {"source_id": row.get("source_id"), "state": row.get("state"),
                 "terminal": row.get("terminal"),
                 "receipt_path": str(self._source_dir(f"sessions/{_safe_name(session.session_id)}", source) / TERMINAL_FILENAME)
                 if (source := next((item for item in session.sources if item.source_id == row.get("source_id")), None)) else None}
                for row in source_receipts
            ],
        }
        if status != "blocked_by_active_or_incomplete_source":
            _create_json(terminal_path, result)
        return result

    def _run_prelude(self) -> dict[str, object]:
        terminal_path = self.campaign_dir / "prelude_terminal_receipt.json"
        if terminal_path.exists():
            return _load_json(terminal_path)
        receipts: list[dict[str, object]] = []
        if self.plan.prelude_sources:
            with ThreadPoolExecutor(max_workers=len(self.plan.prelude_sources), thread_name_prefix="prelude-source") as pool:
                futures = {
                    pool.submit(self._run_source, "prelude", source): source
                    for source in self.plan.prelude_sources
                }
                for future in as_completed(futures):
                    receipts.append(future.result())
        active = any(row.get("state") == "active_or_incomplete_no_relaunch" for row in receipts)
        result = {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "finished_at_utc": iso_now(),
            "status": "blocked_active_or_incomplete" if active else "terminal",
            "production_writes": False, "verified_completion": False,
            "planned_sources": len(self.plan.prelude_sources),
            "sources": sorted(
                ({"source_id": row.get("source_id"), "state": row.get("state"), "terminal": row.get("terminal")}
                 for row in receipts), key=lambda row: str(row["source_id"]),
            ),
        }
        if not active:
            _create_json(terminal_path, result)
        return result

    def run(self) -> dict[str, object]:
        with self.outer_lock():
            self._pin_plan()
            terminal_path = self.campaign_dir / TERMINAL_FILENAME
            if terminal_path.exists():
                return _load_json(terminal_path)
            startup_path = self.campaign_dir / "startup_receipt.json"
            if not startup_path.exists():
                _create_json(startup_path, {
                    "schema_version": 1, "campaign_id": self.plan.campaign_id,
                    "state": "running_or_waiting_capacity", "started_at_utc": iso_now(),
                    "pid": os.getpid(), "hostname": socket.gethostname(),
                    "plan_sha256": self.plan.plan_sha256,
                    "production_writes": False,
                    "capacity": {"country_sessions": self.plan.country_session_limit,
                                 "network": self.plan.network_limit, "browser": self.plan.browser_limit},
                })
            check = self.preflight()
            preflight_path = self.campaign_dir / "preflight.json"
            if not preflight_path.exists():
                _create_json(preflight_path, check)
            if not check["passed"]:
                result = {
                    "schema_version": 1, "campaign_id": self.plan.campaign_id,
                    "status": "preflight_failed", "finished_at_utc": iso_now(),
                    "production_writes": False, "verified_completion": False,
                    "preflight": {"path": str(preflight_path), "sha256": sha256_path(preflight_path)},
                }
                _create_json(terminal_path, result)
                return result
            self._prepare_state()
            if self.executor is None:
                self.executor = OrchestratorSourceExecutor(
                    self.runtime, self.state_path, self.plan.campaign_id
                )
            self.deadline = time.monotonic() + self.plan.max_wall_seconds
            self.events.append("campaign_start", campaign_id=self.plan.campaign_id, pid=os.getpid())
            prelude = self._run_prelude()
            sessions: list[dict[str, object]] = []
            if prelude.get("status") == "terminal" and not self.circuit.is_open():
                with ThreadPoolExecutor(
                    max_workers=self.plan.country_session_limit,
                    thread_name_prefix="country-session",
                ) as pool:
                    futures = {pool.submit(self._run_session, session): session for session in self.plan.sessions}
                    for future in as_completed(futures):
                        sessions.append(future.result())
            status = "terminal_observation_campaign"
            if self.circuit.is_open():
                status = "systemic_circuit_open"
            elif prelude.get("status") != "terminal":
                status = "blocked_by_prelude_active_or_incomplete"
            elif any(row.get("status") != "terminal_observation_sequence" for row in sessions):
                status = "terminal_with_incomplete_sessions"
            result = {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "status": status, "finished_at_utc": iso_now(),
                "plan_sha256": self.plan.plan_sha256,
                "production_writes": False, "verified_completion": False,
                "completion_semantics": "observations are terminal and await evidence review; no tracker watermark advanced",
                "systemic_circuit": {"open": self.circuit.is_open(),
                                     "signature": self.circuit.open_signature,
                                     "counts": dict(self.circuit.counts)},
                "prelude": {"path": str(self.campaign_dir / "prelude_terminal_receipt.json"),
                            "sha256": sha256_path(self.campaign_dir / "prelude_terminal_receipt.json")},
                "sessions": sorted(
                    ({"session_id": row.get("session_id"), "country": row.get("country"),
                     "status": row.get("status"),
                     "receipt_path": str(self.campaign_dir / "sessions" / _safe_name(str(row.get("session_id"))) / TERMINAL_FILENAME)}
                    for row in sessions), key=lambda row: str(row["session_id"]),
                ),
            }
            if status not in {
                "blocked_by_prelude_active_or_incomplete",
                "waiting_for_active_or_incomplete_source",
            } and not any(
                row.get("status") == "blocked_by_active_or_incomplete_source" for row in sessions
            ):
                _create_json(terminal_path, result)
                self.events.append("campaign_terminal", status=status)
            else:
                result["status"] = "waiting_for_active_or_incomplete_source"
                self.events.append("campaign_waiting_active_source")
            return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--automation-root", type=Path, default=AUTOMATION_ROOT)
    parser.add_argument("--framework-root", required=True, type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--status-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--lock-root", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--min-free-gb", type=float, default=0.0)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def runtime_from_args(args: argparse.Namespace) -> RuntimePaths:
    automation = args.automation_root.resolve()
    return RuntimePaths(
        python=args.python.resolve(), automation_root=automation,
        framework_root=args.framework_root.resolve(),
        registry_path=(args.registry or automation / "var/source_registry.json").resolve(),
        baseline_path=(args.baseline or automation / "metrics/baseline-2026.json").resolve(),
        output_root=args.output_root.resolve(), status_root=args.status_root.resolve(),
        lock_root=(args.lock_root or automation / "var/locks").resolve(),
        receipt_root=args.receipt_root.resolve(), min_free_gb=args.min_free_gb,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.min_free_gb < 0:
            raise PlanError("--min-free-gb must be nonnegative")
        runtime = runtime_from_args(args)
        plan = load_plan(args.plan.resolve(), args.plan_sha256, runtime)
        controller = CampaignController(plan, runtime)
        if args.preflight_only:
            result = controller.preflight()
        else:
            result = controller.run()
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("passed", result.get("status") == "terminal_observation_campaign") else 2
    except CampaignAlreadyRunning as exc:
        print(json.dumps({"status": "already_running", "error": str(exc)}, indent=2), file=sys.stderr)
        return 75
    except (OSError, PlanError, ValueError) as exc:
        print(json.dumps({"status": "invalid_or_unsafe", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
