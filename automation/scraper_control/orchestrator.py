from __future__ import annotations

"""Resumable, bounded orchestration for pure scraping adapters.

The default execution path stops after a clean promotion dry run. Production
writes require the explicit ``commit=True`` option (``--commit`` in the CLI).
"""

import argparse
import ast
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional, Sequence

from .observe import adapter_bundle_sha256, observe_source
from .postcheck import postcheck_passed, verify_exact_observation
from .promote import promote
from .review import analyze as analyze_observation
from .state import initialize as initialize_control_state


UTC = timezone.utc
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
ORCHESTRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS orchestration_source_state (
    source_id TEXT PRIMARY KEY REFERENCES sources(source_id),
    phase TEXT NOT NULL DEFAULT 'pending',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_attempt_at TEXT,
    last_cycle_at TEXT,
    last_commit_at TEXT,
    observation_run_dir TEXT,
    dry_run_result_path TEXT,
    commit_result_path TEXT,
    postcheck_result_path TEXT,
    last_error_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orchestration_retry
    ON orchestration_source_state(phase, next_attempt_at);

CREATE TABLE IF NOT EXISTS orchestration_runs (
    orchestration_run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    commit_enabled INTEGER NOT NULL,
    workers INTEGER NOT NULL,
    selected_count INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS orchestration_tasks (
    orchestration_run_id TEXT NOT NULL REFERENCES orchestration_runs(orchestration_run_id),
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    adapter_path TEXT NOT NULL,
    observation_run_dir TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY(orchestration_run_id, source_id)
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def _parse_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _redact(value: object) -> str:
    """Keep status artifacts useful without persisting credentials."""

    text = str(value)
    text = re.sub(r"([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^@/\s]+)@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[:=]\s*([^\s,;}]+)",
        r"\1=[REDACTED]",
        text,
    )
    return text[:4000]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def initialize_orchestration(path: Path) -> None:
    initialize_control_state(path)
    with _connect(path) as connection:
        connection.executescript(ORCHESTRATION_SCHEMA)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(orchestration_source_state)")
        }
        if "postcheck_result_path" not in columns:
            connection.execute(
                "ALTER TABLE orchestration_source_state ADD COLUMN postcheck_result_path TEXT"
            )


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        yield connection
        connection.commit()
    finally:
        connection.close()


@dataclass(frozen=True)
class AdapterSpec:
    source_id: str
    project: str
    country_code: str
    domain: str
    path: Path


def _adapter_literals(path: Path) -> Optional[dict[str, object]]:
    """Read the pure adapter identity without importing network dependencies."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not {"discover_urls", "scrape_url"}.issubset(functions):
        return None
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                    if statement.targets[0].id in {"source_name", "country_code", "is_env"}:
                        try:
                            values[statement.targets[0].id] = ast.literal_eval(statement.value)
                        except (ValueError, TypeError):
                            pass
    if not isinstance(values.get("source_name"), str) or not isinstance(values.get("country_code"), str):
        return None
    return values


def discover_adapters(adapter_root: Path, explicit_map: Optional[Mapping[str, str]] = None) -> dict[str, AdapterSpec]:
    adapter_root = adapter_root.resolve()
    discovered: dict[str, AdapterSpec] = {}
    for path in sorted(adapter_root.rglob("*.py")):
        if path.name in {"__init__.py", "common.py"} or "__pycache__" in path.parts:
            continue
        values = _adapter_literals(path)
        if not values:
            continue
        project = "mleed" if bool(values.get("is_env")) else "mlp"
        country = str(values["country_code"]).upper()
        domain = str(values["source_name"]).lower()
        if domain.startswith("www."):
            domain = domain[4:]
        source_id = f"{project}:{country}:{domain}"
        if source_id in discovered:
            raise ValueError(f"Multiple pure adapters map to {source_id}")
        discovered[source_id] = AdapterSpec(source_id, project, country, domain, path.resolve())
    for source_id, configured_path in (explicit_map or {}).items():
        path = Path(configured_path)
        if not path.is_absolute():
            path = adapter_root / path
        values = _adapter_literals(path.resolve())
        if not values:
            raise ValueError(f"Explicit adapter does not satisfy the pure contract: {path}")
        project, country, domain = source_id.split(":", 2)
        discovered[source_id] = AdapterSpec(source_id, project, country, domain, path.resolve())
    return discovered


def scheduled_boundary(schedule: Optional[str], now: datetime) -> Optional[datetime]:
    """Return the latest due boundary represented by a registry schedule."""

    if not schedule:
        return None
    text = schedule.strip()
    interval = re.fullmatch(r"(?i)every\s*:?\s*(\d+)\s*(h|hr|hrs|hour|hours|d|day|days)", text)
    if interval:
        return None
    months = {MONTHS[token.lower()[:3]] for token in re.findall(r"[A-Za-z]+", text) if token.lower()[:3] in MONTHS}
    if not months:
        return None
    candidates = []
    for year in (now.year - 1, now.year):
        for month in months:
            candidate = datetime(year, month, 1, tzinfo=UTC)
            if candidate <= now:
                candidates.append(candidate)
    return max(candidates) if candidates else None


def schedule_due(
    schedule: Optional[str], last_cycle: Optional[datetime], now: datetime, default_interval_hours: float
) -> tuple[bool, datetime]:
    """Evaluate daily/interval and quarterly month-list registry schedules."""

    boundary = scheduled_boundary(schedule, now)
    if boundary is not None:
        return last_cycle is None or last_cycle < boundary, boundary
    interval_hours = default_interval_hours
    if schedule:
        match = re.fullmatch(
            r"(?i)every\s*:?\s*(\d+)\s*(h|hr|hrs|hour|hours|d|day|days)", schedule.strip()
        )
        if match:
            amount = int(match.group(1))
            interval_hours = amount * (24 if match.group(2).lower().startswith("d") else 1)
        elif schedule.strip().lower() in {"daily", "@daily"}:
            interval_hours = 24
        elif schedule.strip().lower() in {"weekly", "@weekly"}:
            interval_hours = 24 * 7
    boundary = now - timedelta(hours=interval_hours)
    return last_cycle is None or last_cycle <= boundary, boundary


@dataclass(frozen=True)
class RunOptions:
    state_path: Path
    adapter_root: Path
    framework_root: Path
    output_root: Path
    status_root: Path
    lock_root: Path
    baseline_path: Path
    start: Optional[date] = None
    end: date = field(default_factory=date.today)
    projects: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    limit: int = 25
    workers: int = 4
    overlap_days: int = 2
    initial_lookback_days: int = 35
    default_interval_hours: float = 24
    base_backoff_seconds: int = 300
    max_backoff_seconds: int = 86400
    max_failures: int = 5
    heartbeat_seconds: float = 15
    min_free_gb: float = 0
    commit: bool = False
    force: bool = False
    ignore_backoff: bool = False
    retry_blocked: bool = False
    explicit_adapter_map: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 32:
            raise ValueError("workers must be between 1 and 32")
        if self.limit < 1:
            raise ValueError("limit must be positive")
        if self.start and self.start > self.end:
            raise ValueError("start cannot be after end")
        if self.overlap_days < 0 or self.initial_lookback_days < 1:
            raise ValueError("date window values are invalid")
        if self.default_interval_hours <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("intervals must be positive")
        if self.base_backoff_seconds < 0 or self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("backoff values are invalid")
        if self.max_failures < 1:
            raise ValueError("max_failures must be positive")
        if not math.isfinite(self.min_free_gb) or self.min_free_gb < 0:
            raise ValueError("min_free_gb must be a finite nonnegative number")
        if not self.baseline_path.exists():
            raise ValueError(f"baseline file does not exist: {self.baseline_path}")


@dataclass(frozen=True)
class PipelineHooks:
    observe: Callable[..., Path] = observe_source
    promote: Callable[..., dict[str, object]] = promote
    review: Callable[..., dict[str, object]] = analyze_observation
    verify: Callable[..., dict[str, object]] = verify_exact_observation


@dataclass(frozen=True)
class SourcePlan:
    source_id: str
    project: str
    country_code: str
    domain: str
    priority: int
    adapter: AdapterSpec
    start: date
    end: date


class SourceNeedsReview(RuntimeError):
    pass


@contextmanager
def source_lock(lock_root: Path, source_id: str) -> Iterator[bool]:
    """Acquire a nonblocking advisory lock shared by scheduler invocations."""

    lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    path = lock_root / f"{digest}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            metadata = json.dumps({"source_id": source_id, "pid": os.getpid(), "acquired_at": iso_now()}) + "\n"
            os.ftruncate(descriptor, 0)
            os.write(descriptor, metadata.encode("utf-8"))
            os.fsync(descriptor)
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class OrchestrationStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        initialize_orchestration(self.path)

    def create_run(self, run_id: str, options: RunOptions) -> None:
        config = {
            "start": options.start.isoformat() if options.start else None,
            "end": options.end.isoformat(),
            "projects": list(options.projects),
            "countries": list(options.countries),
            "source_ids": list(options.source_ids),
            "limit": options.limit,
            "workers": options.workers,
            "min_free_gb": options.min_free_gb,
            "commit": options.commit,
            "force": options.force,
        }
        now = iso_now()
        with _connect(self.path) as connection:
            connection.execute(
                "INSERT INTO orchestration_runs VALUES (?, 'planning', ?, ?, 0, ?, ?, ?, NULL)",
                (run_id, int(options.commit), options.workers, json.dumps(config), now, now),
            )

    def finish_run(self, run_id: str, status: str) -> None:
        now = iso_now()
        with _connect(self.path) as connection:
            connection.execute(
                "UPDATE orchestration_runs SET status=?, heartbeat_at=?, finished_at=? WHERE orchestration_run_id=?",
                (status, now, now, run_id),
            )

    def heartbeat_run(self, run_id: str, status: str, selected_count: int) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                "UPDATE orchestration_runs SET status=?, selected_count=?, heartbeat_at=? WHERE orchestration_run_id=?",
                (status, selected_count, iso_now(), run_id),
            )

    def source_rows(self, source_ids: Sequence[str]) -> list[dict[str, object]]:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        query = f"""
            SELECT s.*, w.discovery_through, w.parsed_through,
                   w.validated_through, w.safe_through,
                   w.assessed_at AS watermark_assessed_at,
                   o.phase, o.consecutive_failures, o.next_attempt_at,
                   o.last_cycle_at, o.last_commit_at, o.observation_run_dir,
                   o.dry_run_result_path, o.commit_result_path, o.postcheck_result_path,
                   o.last_error_json
            FROM sources s
            LEFT JOIN watermarks w ON w.source_id=s.source_id
            LEFT JOIN orchestration_source_state o ON o.source_id=s.source_id
            WHERE s.enabled=1 AND s.source_id IN ({placeholders})
        """
        with _connect(self.path) as connection:
            return [dict(row) for row in connection.execute(query, list(source_ids))]

    def registry_rows(
        self, projects: Sequence[str] = (), countries: Sequence[str] = (), source_ids: Sequence[str] = ()
    ) -> list[dict[str, object]]:
        clauses = ["enabled=1"]
        parameters: list[object] = []
        for column, values in (("project", projects), ("country_code", countries), ("source_id", source_ids)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                parameters.extend(values)
        with _connect(self.path) as connection:
            return [
                dict(row) for row in connection.execute(
                    f"SELECT source_id, project, country_code, domain FROM sources WHERE {' AND '.join(clauses)}",
                    parameters,
                )
            ]

    def add_task(self, run_id: str, plan: SourcePlan) -> None:
        now = iso_now()
        with _connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO orchestration_tasks (
                    orchestration_run_id, source_id, status, stage, requested_start,
                    requested_end, adapter_path, heartbeat_at
                ) VALUES (?, ?, 'queued', 'queued', ?, ?, ?, ?)
                """,
                (run_id, plan.source_id, plan.start.isoformat(), plan.end.isoformat(), str(plan.adapter.path), now),
            )

    def update_task(self, run_id: str, source_id: str, **changes: object) -> None:
        allowed = {
            "status", "stage", "observation_run_dir", "attempts", "error_json",
            "started_at", "heartbeat_at", "finished_at",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Invalid task fields: {sorted(invalid)}")
        changes.setdefault("heartbeat_at", iso_now())
        assignments = ", ".join(f"{key}=?" for key in changes)
        values = [json.dumps(value) if key == "error_json" and not isinstance(value, str) else value for key, value in changes.items()]
        with _connect(self.path) as connection:
            connection.execute(
                f"UPDATE orchestration_tasks SET {assignments} WHERE orchestration_run_id=? AND source_id=?",
                values + [run_id, source_id],
            )

    def source_state(self, source_id: str) -> dict[str, object]:
        with _connect(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM orchestration_source_state WHERE source_id=?", (source_id,)
            ).fetchone()
        return dict(row) if row else {}

    def update_source(self, source_id: str, **changes: object) -> None:
        allowed = {
            "phase", "consecutive_failures", "next_attempt_at", "last_attempt_at", "last_cycle_at",
            "last_commit_at", "observation_run_dir", "dry_run_result_path", "commit_result_path",
            "postcheck_result_path", "last_error_json",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Invalid source state fields: {sorted(invalid)}")
        current = self.source_state(source_id)
        defaults = {
            "phase": "pending", "consecutive_failures": 0, "next_attempt_at": None,
            "last_attempt_at": None, "last_cycle_at": None, "last_commit_at": None,
            "observation_run_dir": None, "dry_run_result_path": None, "commit_result_path": None,
            "postcheck_result_path": None, "last_error_json": "{}",
        }
        values = {**defaults, **current, **changes, "updated_at": iso_now(), "source_id": source_id}
        if not isinstance(values["last_error_json"], str):
            values["last_error_json"] = json.dumps(values["last_error_json"])
        columns = list(defaults) + ["updated_at"]
        placeholders = ",".join("?" for _ in ["source_id", *columns])
        updates = ",".join(f"{column}=excluded.{column}" for column in columns)
        with _connect(self.path) as connection:
            connection.execute(
                f"INSERT INTO orchestration_source_state (source_id,{','.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(source_id) DO UPDATE SET {updates}",
                [values["source_id"], *[values[column] for column in columns]],
            )

    def task_rows(self, run_id: str) -> list[dict[str, object]]:
        with _connect(self.path) as connection:
            return [
                dict(row) for row in connection.execute(
                    "SELECT * FROM orchestration_tasks WHERE orchestration_run_id=? ORDER BY source_id", (run_id,)
                )
            ]

    def record_watermark(
        self, source_id: str, safe_through: date, run_id: str, evidence: dict[str, object]
    ) -> None:
        assessed_at = iso_now()
        with _connect(self.path) as connection:
            existing = connection.execute(
                "SELECT safe_through, validated_through, confidence, evidence_json "
                "FROM watermarks WHERE source_id=?", (source_id,)
            ).fetchone()
            strict_safe = safe_through
            if existing and existing["safe_through"] and existing["validated_through"]:
                old_strict = date.fromisoformat(existing["safe_through"])
                old_recent = date.fromisoformat(existing["validated_through"])
                if old_strict < old_recent:
                    # A verified recent interval has not filled the recorded
                    # historical gap. Keep the strict watermark until that
                    # gap is explicitly repaired and independently verified.
                    strict_safe = old_strict
                    evidence = {
                        **evidence,
                        "strict_historical_gap_preserved": True,
                        "previous_evidence": json.loads(existing["evidence_json"] or "{}"),
                    }
            connection.execute(
                """
                INSERT INTO watermarks (
                    source_id, discovery_through, parsed_through, validated_through,
                    safe_through, confidence, evidence_json, run_id, assessed_at
                ) VALUES (?, ?, ?, ?, ?, 'high', ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    discovery_through=excluded.discovery_through,
                    parsed_through=excluded.parsed_through,
                    validated_through=excluded.validated_through,
                    safe_through=excluded.safe_through,
                    confidence=excluded.confidence,
                    evidence_json=excluded.evidence_json,
                    run_id=excluded.run_id,
                    assessed_at=excluded.assessed_at
                """,
                (
                    source_id, safe_through.isoformat(), safe_through.isoformat(),
                    safe_through.isoformat(), strict_safe.isoformat(),
                    json.dumps(evidence, ensure_ascii=False), run_id, assessed_at,
                ),
            )


class Orchestrator:
    def __init__(self, options: RunOptions, hooks: Optional[PipelineHooks] = None):
        self.options = options
        self.hooks = hooks or PipelineHooks()
        self.store = OrchestrationStore(options.state_path)
        self.adapters = discover_adapters(options.adapter_root, options.explicit_adapter_map)
        self.run_id = f"orchestrate-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.store.create_run(self.run_id, options)
        self._status_lock = threading.Lock()
        self._runner_status = "planning"
        self._plans: list[SourcePlan] = []
        self._planning_issues: dict[str, object] = {"missing_adapter_count": 0, "missing_adapters": []}
        self._storage = {
            "path": str(self.options.output_root.resolve()),
            "free_bytes": None,
            "required_free_bytes": int(self.options.min_free_gb * 1024**3),
            "sufficient": None,
        }

    def _check_storage(self) -> bool:
        """Fail closed when the observation filesystem cannot satisfy the free-space gate."""

        path = self.options.output_root.resolve()
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        required = int(self.options.min_free_gb * 1024**3)
        try:
            free = int(shutil.disk_usage(probe).free)
            self._storage = {
                "path": str(path),
                "probe_path": str(probe),
                "free_bytes": free,
                "required_free_bytes": required,
                "sufficient": free >= required,
            }
        except OSError as exc:
            self._storage = {
                "path": str(path),
                "probe_path": str(probe),
                "free_bytes": None,
                "required_free_bytes": required,
                "sufficient": False,
                "error": _redact(exc),
            }
        return bool(self._storage["sufficient"])

    def _last_cycle(self, row: Mapping[str, object]) -> Optional[datetime]:
        # Notion's Last update property is editorial metadata.  It can change
        # without an observation and must never suppress a due source.  A
        # verified imported watermark, unlike that property, is a cycle anchor.
        return _parse_datetime(row.get("last_cycle_at")) or _parse_datetime(
            row.get("watermark_assessed_at")
        )

    def _window(self, row: Mapping[str, object]) -> tuple[date, date]:
        if self.options.start:
            return self.options.start, self.options.end
        # `safe_through` is the strict continuous completeness watermark.
        # `validated_through` is also the rolling cursor for a clean recent
        # interval after a separately recorded historical gap.  Using the
        # latter prevents every recurring cycle from replaying that backlog;
        # it does not change the strict source or country watermark.
        # Only verified coverage may narrow the next window.  A recent Notion
        # edit is not evidence that an unwatermarked source was scraped.
        anchor_text = row.get("validated_through") or row.get("safe_through")
        if anchor_text:
            try:
                anchor = date.fromisoformat(str(anchor_text)[:10])
            except ValueError:
                anchor = self.options.end - timedelta(days=self.options.initial_lookback_days)
        else:
            anchor = self.options.end - timedelta(days=self.options.initial_lookback_days)
        anchor = min(anchor, self.options.end)
        return anchor - timedelta(days=self.options.overlap_days), self.options.end

    @staticmethod
    def _ready_window(row: Mapping[str, object], adapter: AdapterSpec) -> Optional[tuple[date, date]]:
        """Keep an approved dry run resumable when the calendar day advances."""

        raw_path = row.get("observation_run_dir")
        if not raw_path:
            return None
        manifest_path = Path(str(raw_path)) / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_hash = hashlib.sha256(adapter.path.read_bytes()).hexdigest()
            if (
                manifest.get("status") != "observed"
                or manifest.get("source") != adapter.domain
                or manifest.get("source_sha256") != source_hash
                or (
                    manifest.get("source_bundle_sha256")
                    and manifest.get("source_bundle_sha256") != adapter_bundle_sha256(adapter.path)
                )
            ):
                return None
            return date.fromisoformat(manifest["requested_start"]), date.fromisoformat(manifest["requested_end"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def plan(self) -> list[SourcePlan]:
        registry_scope = self.store.registry_rows(
            self.options.projects, self.options.countries, self.options.source_ids
        )
        missing_adapters = sorted(
            str(row["source_id"]) for row in registry_scope if str(row["source_id"]) not in self.adapters
        )
        self._planning_issues = {
            "missing_adapter_count": len(missing_adapters),
            "missing_adapters": missing_adapters[:100],
            "missing_adapters_truncated": len(missing_adapters) > 100,
        }
        adapter_ids = set(self.adapters)
        if self.options.source_ids:
            adapter_ids &= set(self.options.source_ids)
        rows = self.store.source_rows(sorted(adapter_ids))
        now = utc_now()
        country_order = {country: index for index, country in enumerate(self.options.countries)}
        projects = set(self.options.projects)
        plans: list[tuple[tuple[object, ...], SourcePlan]] = []
        for row in rows:
            country = str(row["country_code"])
            if self.options.countries and country not in country_order:
                continue
            if projects and row["project"] not in projects:
                continue
            phase = str(row.get("phase") or "pending")
            if phase in {"needs_review", "blocked"} and not self.options.retry_blocked:
                continue
            next_attempt = _parse_datetime(row.get("next_attempt_at"))
            if next_attempt and next_attempt > now and not self.options.ignore_backoff:
                continue
            last_cycle = self._last_cycle(row)
            due, boundary = schedule_due(
                row.get("schedule"), last_cycle, now, self.options.default_interval_hours
            )
            waiting_commit = self.options.commit and phase in {"dry_run_complete", "ready_for_commit"}
            if not (self.options.force or due or waiting_commit):
                continue
            source_id = str(row["source_id"])
            adapter = self.adapters[source_id]
            ready_window = self._ready_window(row, adapter) if waiting_commit else None
            start, end = ready_window or self._window(row)
            plan = SourcePlan(
                source_id=source_id, project=str(row["project"]), country_code=country,
                domain=str(row["domain"]), priority=int(row["priority"]),
                adapter=adapter, start=start, end=end,
            )
            lateness = (now - boundary).total_seconds()
            sort_key = (
                country_order.get(country, len(country_order)),
                -plan.priority,
                -lateness,
                source_id,
            )
            plans.append((sort_key, plan))
        self._plans = [plan for _, plan in sorted(plans, key=lambda item: item[0])[: self.options.limit]]
        for plan in self._plans:
            self.store.add_task(self.run_id, plan)
        self.store.heartbeat_run(self.run_id, "queued", len(self._plans))
        self._write_status("queued")
        return self._plans

    def _source_output(self, plan: SourcePlan) -> Path:
        digest = hashlib.sha256(plan.source_id.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", plan.source_id)
        return self.options.output_root.resolve() / f"{slug}-{digest}"

    def _matching_observation(self, plan: SourcePlan) -> Optional[Path]:
        output = self._source_output(plan)
        if not output.exists():
            return None
        source_hash = hashlib.sha256(plan.adapter.path.read_bytes()).hexdigest()
        candidates = sorted(output.glob("observe-*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for manifest_path in candidates:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                manifest.get("status") == "observed"
                and manifest.get("requested_start") == plan.start.isoformat()
                and manifest.get("requested_end") == plan.end.isoformat()
                and manifest.get("source_sha256") == source_hash
                and (
                    not manifest.get("source_bundle_sha256")
                    or manifest.get("source_bundle_sha256") == adapter_bundle_sha256(plan.adapter.path)
                )
                and manifest.get("source") == plan.domain
            ):
                return manifest_path.parent
        return None

    @staticmethod
    def _promotion_result(path: Path, observation_run_id: str, mode: str) -> Optional[dict[str, object]]:
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if result.get("observation_run_id") != observation_run_id or result.get("mode") != mode:
            return None
        if result.get("errors"):
            return None
        if mode == "commit" and result.get("inserted") != result.get("new"):
            return None
        return result

    @staticmethod
    def _postcheck_result(path: Path, observation_run_id: str) -> Optional[dict[str, object]]:
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if result.get("observation_run_id") != observation_run_id or not postcheck_passed(result):
            return None
        return result

    def _transition(self, plan: SourcePlan, stage: str, status: str, observation: Optional[Path] = None) -> None:
        changes: dict[str, object] = {"stage": stage, "status": status}
        if observation:
            changes["observation_run_dir"] = str(observation)
        if status == "running":
            changes.setdefault("started_at", iso_now())
        if status in {"ready_for_commit", "completed", "needs_review", "failed", "blocked", "locked"}:
            changes["finished_at"] = iso_now()
        self.store.update_task(self.run_id, plan.source_id, **changes)
        self._write_status("running")

    def _verify_and_complete(
        self,
        plan: SourcePlan,
        observation: Path,
        manifest: dict[str, object],
        commit_result: dict[str, object],
        review_report: dict[str, object],
    ) -> str:
        observation_run_id = str(manifest["run_id"])
        postcheck_path = observation / "postwrite_exact_verification.json"
        result = self._postcheck_result(postcheck_path, observation_run_id)
        if result is None:
            self._transition(plan, "postwrite_exact_verification", "running", observation)
            result = self.hooks.verify(observation, self.options.framework_root)
            _atomic_write(postcheck_path, json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        if result.get("observation_run_id") != observation_run_id or not postcheck_passed(result):
            raise SourceNeedsReview(
                "Exact post-write verification failed: "
                f"expected={result.get('expected_eligible')} exact={result.get('exact_match')} "
                f"missing={len(result.get('missing_urls') or [])} "
                f"duplicates={len(result.get('duplicate_urls') or [])} "
                f"field_mismatches={len(result.get('field_mismatches') or [])}"
            )
        candidate = review_report.get("safe_through_candidate")
        if not candidate:
            raise SourceNeedsReview("Review did not provide a safe-through candidate")
        safe_through = date.fromisoformat(str(candidate))
        evidence = {
            "observation_run_id": observation_run_id,
            "observation_run_dir": str(observation),
            "promotion_id": commit_result.get("promotion_id"),
            "postcheck_result_path": str(postcheck_path),
            "expected_eligible": result.get("expected_eligible"),
            "exact_match": result.get("exact_match"),
            "latest": result.get("latest"),
        }
        self.store.record_watermark(plan.source_id, safe_through, observation_run_id, evidence)
        now = iso_now()
        self.store.update_source(
            plan.source_id, phase="complete", consecutive_failures=0, next_attempt_at=None,
            last_cycle_at=now, last_commit_at=now,
            commit_result_path=str(observation / "promotion.json"),
            postcheck_result_path=str(postcheck_path), last_error_json={},
        )
        self._transition(plan, "postwrite_exact_verification", "completed", observation)
        return "completed"

    def _run_source(self, plan: SourcePlan) -> str:
        with source_lock(self.options.lock_root, plan.source_id) as acquired:
            if not acquired:
                self._transition(plan, "lock", "locked")
                return "locked"
            current = self.store.source_state(plan.source_id)
            attempts = int(current.get("consecutive_failures") or 0) + 1
            self.store.update_task(self.run_id, plan.source_id, status="running", stage="resume", attempts=attempts, started_at=iso_now())
            self.store.update_source(plan.source_id, phase="running", last_attempt_at=iso_now())
            try:
                # Make sibling ``adapters`` imports available while the observer
                # separately adds the legacy framework root for promotion.
                adapter_python_root = str(self.options.adapter_root.resolve().parent)
                if adapter_python_root not in sys.path:
                    sys.path.insert(0, adapter_python_root)

                observation = self._matching_observation(plan)
                if observation is None:
                    self._transition(plan, "observation", "running")
                    observation = self.hooks.observe(
                        self.options.framework_root,
                        plan.adapter.path,
                        plan.start,
                        plan.end,
                        self._source_output(plan),
                        None,
                        None,
                        False,
                    )
                self.store.update_source(
                    plan.source_id, phase="observed", observation_run_dir=str(observation),
                    dry_run_result_path=None, commit_result_path=None, postcheck_result_path=None,
                )
                self._transition(plan, "observed", "running", observation)
                manifest = json.loads((observation / "manifest.json").read_text(encoding="utf-8"))
                observation_run_id = str(manifest["run_id"])

                self._transition(plan, "coverage_field_volume_review", "running", observation)
                review_result = self.hooks.review(
                    observation,
                    self.options.baseline_path,
                    observation / "review",
                )
                review_report = review_result.get("report", {})
                if review_report.get("status") != "ready_for_dry_run":
                    raise SourceNeedsReview(
                        "Observation review blocked promotion: "
                        f"safe_through={review_report.get('safe_through_candidate')} "
                        f"field_issue_dates={len(review_report.get('unresolved_by_date') or {})} "
                        f"unknown_window_issues={len(review_report.get('undated_or_unknown_window_issues') or [])} "
                        f"volume_anomalies={len(review_report.get('volume_anomalies') or [])} "
                        f"discovery_failures={len(review_report.get('discovery_or_runtime_failures') or [])}"
                    )

                committed = self._promotion_result(observation / "promotion.json", observation_run_id, "commit")
                if committed is not None:
                    return self._verify_and_complete(
                        plan, observation, manifest, committed, review_report
                    )

                dry_path = observation / "promotion_dry_run.json"
                dry_result = self._promotion_result(dry_path, observation_run_id, "dry_run")
                if dry_result is None:
                    self._transition(plan, "promotion_dry_run", "running", observation)
                    dry_result = self.hooks.promote(observation, self.options.framework_root, False)
                if dry_result.get("errors") or int(dry_result.get("rejected", 0)):
                    raise SourceNeedsReview(
                        f"Promotion gate rejected {dry_result.get('rejected', 0)} candidate(s) "
                        f"and reported {len(dry_result.get('errors') or [])} error(s)"
                    )
                now = iso_now()
                self.store.update_source(
                    plan.source_id, phase="ready_for_commit", consecutive_failures=0, next_attempt_at=None,
                    last_cycle_at=now, observation_run_dir=str(observation),
                    dry_run_result_path=str(dry_path), last_error_json={},
                )
                if not self.options.commit:
                    self._transition(plan, "promotion_dry_run", "ready_for_commit", observation)
                    return "ready_for_commit"

                self._transition(plan, "promotion_commit", "running", observation)
                commit_result = self.hooks.promote(observation, self.options.framework_root, True)
                if commit_result.get("errors") or commit_result.get("inserted") != commit_result.get("new"):
                    raise SourceNeedsReview(
                        f"Commit incomplete: inserted={commit_result.get('inserted')} new={commit_result.get('new')} "
                        f"errors={len(commit_result.get('errors') or [])}"
                    )
                return self._verify_and_complete(
                    plan, observation, manifest, commit_result, review_report
                )
            except (SourceNeedsReview, ValueError) as exc:
                error = {"type": type(exc).__name__, "message": _redact(exc), "retryable": False}
                self.store.update_source(plan.source_id, phase="needs_review", next_attempt_at=None, last_error_json=error)
                self.store.update_task(
                    self.run_id, plan.source_id, status="needs_review", stage="gate", error_json=error,
                    finished_at=iso_now(),
                )
                self._write_status("running")
                return "needs_review"
            except Exception as exc:
                failures = int(current.get("consecutive_failures") or 0) + 1
                blocked = failures >= self.options.max_failures
                delay = min(self.options.max_backoff_seconds, self.options.base_backoff_seconds * (2 ** (failures - 1)))
                next_attempt = None if blocked else (utc_now() + timedelta(seconds=delay)).isoformat()
                error = {
                    "type": type(exc).__name__, "message": _redact(exc), "retryable": not blocked,
                    "traceback_tail": _redact(
                        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    )[-2000:],
                }
                self.store.update_source(
                    plan.source_id, phase="blocked" if blocked else "backoff",
                    consecutive_failures=failures, next_attempt_at=next_attempt, last_error_json=error,
                )
                self.store.update_task(
                    self.run_id, plan.source_id, status="blocked" if blocked else "failed",
                    stage="error", error_json=error, finished_at=iso_now(),
                )
                self._write_status("running")
                return "blocked" if blocked else "failed"

    def _status_payload(self) -> dict[str, object]:
        tasks = self.store.task_rows(self.run_id)
        counts: dict[str, int] = {}
        for task in tasks:
            status = str(task["status"])
            counts[status] = counts.get(status, 0) + 1
            if task.get("error_json"):
                try:
                    task["error"] = json.loads(str(task.pop("error_json")))
                except json.JSONDecodeError:
                    task["error"] = {"message": "Unreadable error record"}
        return {
            "schema_version": 1,
            "orchestration_run_id": self.run_id,
            "runner_status": self._runner_status,
            "heartbeat_at": iso_now(),
            "commit_enabled": self.options.commit,
            "workers": self.options.workers,
            "selected_count": len(self._plans),
            "counts": counts,
            "planning_issues": self._planning_issues,
            "storage": self._storage,
            "tasks": tasks,
        }

    def _write_status(self, status: Optional[str] = None) -> None:
        with self._status_lock:
            if status:
                self._runner_status = status
            payload = self._status_payload()
            rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            run_json = self.options.status_root / "runs" / f"{self.run_id}.json"
            latest_json = self.options.status_root / "latest.json"
            _atomic_write(run_json, rendered)
            _atomic_write(latest_json, rendered)
            lines = [
                "# Scraping orchestration heartbeat", "",
                f"- Run: `{self.run_id}`",
                f"- Status: **{payload['runner_status']}**",
                f"- Heartbeat (UTC): {payload['heartbeat_at']}",
                f"- Production commit enabled: **{'yes' if self.options.commit else 'no'}**",
                f"- Worker limit: {self.options.workers}",
                f"- Selected sources: {len(self._plans)}", "",
                "## Storage gate", "",
                f"- Observation output: `{self._storage['path']}`",
                f"- Free bytes: {self._storage['free_bytes']}",
                f"- Required free bytes: {self._storage['required_free_bytes']}",
                f"- Sufficient: **{'yes' if self._storage['sufficient'] else 'no'}**", "",
                "## Task counts", "",
            ]
            if payload["counts"]:
                lines.extend(f"- {key}: {value}" for key, value in sorted(payload["counts"].items()))
            else:
                lines.append("- No sources selected")
            if payload["planning_issues"]["missing_adapter_count"]:
                lines.extend([
                    "", "## Planning issues", "",
                    f"- Registry sources without a pure adapter: {payload['planning_issues']['missing_adapter_count']}",
                ])
                lines.extend(
                    f"  - `{source_id}`" for source_id in payload["planning_issues"]["missing_adapters"]
                )
                if payload["planning_issues"]["missing_adapters_truncated"]:
                    lines.append("  - Additional missing adapters omitted from this compact status")
            lines.extend(["", "## Sources", ""])
            for task in payload["tasks"]:
                lines.append(
                    f"- `{task['source_id']}` — {task['status']} / {task['stage']} "
                    f"({task['requested_start']} through {task['requested_end']})"
                )
                if task.get("error"):
                    lines.append(f"  - {task['error'].get('type', 'Error')}: {task['error'].get('message', '')}")
            markdown = "\n".join(lines) + "\n"
            _atomic_write(self.options.status_root / "runs" / f"{self.run_id}.md", markdown)
            _atomic_write(self.options.status_root / "latest.md", markdown)

    def _write_audit_index(self) -> None:
        """Persist one compact, final evidence index for every selected task.

        The status heartbeat is rewritten while a run progresses. This index is
        written only after the run finishes, at a unique run-ID path, so a later
        reviewer can find a failed discovery as readily as a clean observation.
        """

        def artifact(path: Path) -> dict[str, object]:
            resolved = path.resolve()
            try:
                portable = resolved.relative_to(self.options.output_root.resolve()).as_posix()
                location: dict[str, object] = {"root": "output_root", "path": portable}
            except ValueError:
                location = {"root": "absolute", "path": str(resolved)}
            try:
                digest = hashlib.sha256()
                size = 0
                with resolved.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
            except OSError:
                return {**location, "exists": False, "sha256": None}
            return {**location, "exists": True, "sha256": digest.hexdigest(), "bytes": size}

        def load(path: Path) -> dict[str, object]:
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return result if isinstance(result, dict) else {}

        follow_up = {
            "failed": "Investigate the recorded stage and error; retry after access or adapter repair.",
            "blocked": "Investigate repeated failures and explicitly retry after remediation.",
            "needs_review": "Resolve discovery, field, or volume findings in the review before promotion.",
            "locked": "Let the active source run finish, then inspect its final audit index.",
            "ready_for_commit": "Inspect the review and candidate plot, then run an explicit commit cycle.",
            "completed": "Refresh source/country plots, issue reports, and Notion from the verified postcheck.",
        }
        indexed = []
        for task in self.store.task_rows(self.run_id):
            source_id = str(task["source_id"])
            project, country, domain = source_id.split(":", 2)
            status = str(task["status"])
            observation = Path(str(task["observation_run_dir"])) if task.get("observation_run_dir") else None
            files: dict[str, dict[str, object]] = {}
            manifest: dict[str, object] = {}
            review: dict[str, object] = {}
            dry_promotion: dict[str, object] = {}
            promotion: dict[str, object] = {}
            postcheck: dict[str, object] = {}
            if observation:
                manifest_path = observation / "manifest.json"
                files["observation_manifest"] = artifact(manifest_path)
                manifest = load(manifest_path)
                for name, filename in (
                    ("discovered_urls", "urls.jsonl"),
                    ("parsed_articles", "articles.jsonl"),
                    ("field_checks", "field_checks.jsonl"),
                    ("observation_issues", "issues.jsonl"),
                ):
                    files[name] = artifact(observation / filename)
                review_path = observation / "review" / f"{domain.replace('.', '_')}_observation_review.json"
                if not review_path.is_file():
                    candidates = sorted((observation / "review").glob("*_review.json"))
                    if len(candidates) == 1:
                        review_path = candidates[0]
                files["observation_review"] = artifact(review_path)
                review = load(review_path)
                if isinstance(review.get("report"), dict):
                    review = review["report"]
                plot_path = observation / "review" / f"{domain.replace('.', '_')}_candidate_daily.png"
                files["candidate_daily_plot"] = artifact(plot_path)
                for name, filename in (
                    ("promotion_dry_run", "promotion_dry_run.json"),
                    ("promotion_commit", "promotion.json"),
                    ("exact_postcheck", "postwrite_exact_verification.json"),
                ):
                    path = observation / filename
                    files[name] = artifact(path)
                    if name == "promotion_dry_run":
                        dry_promotion = load(path)
                    elif name == "promotion_commit":
                        promotion = load(path)
                    elif name == "exact_postcheck":
                        postcheck = load(path)
            counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
            issue_codes = review.get("issue_code_counts") if isinstance(review.get("issue_code_counts"), dict) else {}
            anomalies = review.get("volume_anomalies") if isinstance(review.get("volume_anomalies"), list) else []
            issues = set(str(code) for code in issue_codes)
            issues.update(str(item.get("flag")) for item in anomalies if isinstance(item, dict) and item.get("flag"))
            discovery_failures = review.get("discovery_or_runtime_failures")
            if isinstance(discovery_failures, list) and discovery_failures:
                issues.add("discovery_or_runtime_failure")
            error = {}
            try:
                error = json.loads(str(task.get("error_json") or "{}"))
            except json.JSONDecodeError:
                error = {"type": "UnreadableErrorRecord"}
            if error.get("type"):
                issues.add(str(error["type"]))
            review_status = review.get("status")
            audit_ready = bool(manifest.get("status") == "observed" and review_status)
            indexed.append({
                "source_id": source_id, "project": project, "country": country, "domain": domain,
                "window": {"start": task["requested_start"], "end": task["requested_end"]},
                "status": status, "stage": task["stage"], "error": error,
                "observation_run_id": manifest.get("run_id"),
                "audit_ready": audit_ready,
                "review_ready_for_dry_run": bool(review_status == "ready_for_dry_run"),
                "manifest_counts": counts,
                "review": {
                    "status": review_status,
                    "safe_through_candidate": review.get("safe_through_candidate"),
                    "eligible_in_window": review.get("eligible_in_window"),
                    "verified_inactive_window": review.get("verified_inactive_window") is True,
                    "issue_code_counts": issue_codes,
                    "unresolved_date_count": len(review.get("unresolved_by_date") or {}),
                    "unknown_window_issue_count": len(review.get("undated_or_unknown_window_issues") or []),
                    "discovery_failure_count": len(discovery_failures) if isinstance(discovery_failures, list) else None,
                    "volume_anomaly_count": len(anomalies),
                    "policy_exclusion_count": len(review.get("policy_exclusions") or []),
                },
                "issue_categories": sorted(issues),
                "promotion_dry_run": {
                    **{key: dry_promotion.get(key) for key in
                       ("mode", "eligible", "new", "already_present", "rejected")},
                    "error_count": len(dry_promotion.get("errors") or []),
                } if dry_promotion else None,
                "promotion_commit": {
                    **{key: promotion.get(key) for key in
                       ("mode", "eligible", "new", "inserted", "rejected")},
                    "error_count": len(promotion.get("errors") or []),
                } if promotion else None,
                "postcheck": {
                    "expected_eligible": postcheck.get("expected_eligible"),
                    "exact_match": postcheck.get("exact_match"),
                    "missing_url_count": len(postcheck.get("missing_urls") or []),
                    "duplicate_url_count": len(postcheck.get("duplicate_urls") or []),
                    "field_mismatch_count": len(postcheck.get("field_mismatches") or []),
                    "passed": postcheck_passed(postcheck),
                } if postcheck else None,
                "artifacts": files,
                "plot_report_follow_up": follow_up.get(status, "Inspect task status and artifacts."),
            })
        index = {
            "schema_version": 1,
            "kind": "scraping_cycle_audit_index",
            "orchestration_run_id": self.run_id,
            "runner_status": self._runner_status,
            "finished_at": iso_now(),
            "commit_enabled": self.options.commit,
            "roots": {"output_root": str(self.options.output_root.resolve())},
            "source_count": len(indexed),
            "sources": indexed,
        }
        _atomic_write(self.options.status_root / "runs" / f"{self.run_id}.audit.json",
                      json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    def run_once(self) -> dict[str, object]:
        if not self._check_storage():
            self.store.finish_run(self.run_id, "blocked_storage")
            self._write_status("blocked_storage")
            self._write_audit_index()
            return self._status_payload()
        plans = self.plan()
        if not plans:
            final = "completed_with_issues" if self._planning_issues["missing_adapter_count"] else "idle"
            self.store.finish_run(self.run_id, final)
            self._write_status(final)
            self._write_audit_index()
            return self._status_payload()
        self.store.heartbeat_run(self.run_id, "running", len(plans))
        self._write_status("running")
        with ThreadPoolExecutor(max_workers=self.options.workers, thread_name_prefix="scrape-source") as executor:
            future_to_plan = {executor.submit(self._run_source, plan): plan for plan in plans}
            pending = set(future_to_plan)
            while pending:
                done, pending = wait(pending, timeout=self.options.heartbeat_seconds, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        future.result()
                    except Exception as exc:
                        # _run_source handles source failures. This final guard
                        # accounts for defects before its normal exception gate.
                        plan = future_to_plan[future]
                        error = {
                            "type": type(exc).__name__, "message": _redact(exc),
                            "retryable": True, "scope": "worker_wrapper",
                        }
                        current = self.store.source_state(plan.source_id)
                        failures = int(current.get("consecutive_failures") or 0) + 1
                        delay = min(
                            self.options.max_backoff_seconds,
                            self.options.base_backoff_seconds * (2 ** (failures - 1)),
                        )
                        self.store.update_source(
                            plan.source_id, phase="backoff", consecutive_failures=failures,
                            next_attempt_at=(utc_now() + timedelta(seconds=delay)).isoformat(),
                            last_error_json=error,
                        )
                        self.store.update_task(
                            self.run_id, plan.source_id, status="failed", stage="worker_wrapper",
                            error_json=error, finished_at=iso_now(),
                        )
                self.store.heartbeat_run(self.run_id, "running", len(plans))
                self._write_status("running")
        tasks = self.store.task_rows(self.run_id)
        statuses = {str(task["status"]) for task in tasks}
        final = "completed"
        if statuses & {"failed", "blocked", "needs_review", "queued", "running"} or self._planning_issues["missing_adapter_count"]:
            final = "completed_with_issues"
        elif statuses == {"locked"}:
            final = "deferred"
        self.store.finish_run(self.run_id, final)
        self._write_status(final)
        self._write_audit_index()
        return self._status_payload()


def _comma_values(values: Optional[Iterable[str]]) -> tuple[str, ...]:
    return tuple(
        part.strip().upper()
        for value in (values or ())
        for part in value.split(",")
        if part.strip()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run resumable pure-adapter scraping cycles")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--adapter-root", required=True, type=Path)
    parser.add_argument("--framework-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--status-root", required=True, type=Path)
    parser.add_argument("--lock-root", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path, help="Historical daily metrics JSON used by the anomaly gate")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--end-lag-days", type=int, default=0,
        help="When --end is omitted, stop this many complete calendar days before today; resolved on every loop cycle",
    )
    parser.add_argument("--project", action="append", choices=("mlp", "mleed"))
    parser.add_argument("--country", action="append", help="Country code; repeat or pass a comma-separated priority order")
    parser.add_argument("--source-id", action="append")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--overlap-days", type=int, default=2)
    parser.add_argument("--initial-lookback-days", type=int, default=35)
    parser.add_argument("--default-interval-hours", type=float, default=24)
    parser.add_argument("--base-backoff-seconds", type=int, default=300)
    parser.add_argument("--max-backoff-seconds", type=int, default=86400)
    parser.add_argument("--max-failures", type=int, default=5)
    parser.add_argument("--heartbeat-seconds", type=float, default=15)
    parser.add_argument(
        "--min-free-gb", type=float, default=0,
        help="Block a cycle before planning when the observation filesystem has less free space",
    )
    parser.add_argument("--adapter-map", type=Path, help="Optional JSON object mapping source_id to adapter path")
    parser.add_argument("--commit", action="store_true", help="Explicitly enable production promotion writes")
    parser.add_argument("--force", action="store_true", help="Ignore schedule due dates; retry backoff still applies")
    parser.add_argument("--ignore-backoff", action="store_true")
    parser.add_argument("--retry-blocked", action="store_true")
    parser.add_argument("--loop", action="store_true", help="Run cycles continuously")
    parser.add_argument("--poll-seconds", type=float, default=900)
    return parser


def options_from_args(args: argparse.Namespace, *, today: Optional[date] = None) -> RunOptions:
    if args.end_lag_days < 0:
        raise ValueError("end-lag-days must be nonnegative")
    explicit_map = {}
    if args.adapter_map:
        explicit_map = json.loads(args.adapter_map.read_text(encoding="utf-8"))
        if not isinstance(explicit_map, dict):
            raise ValueError("adapter-map must contain a JSON object")
    return RunOptions(
        state_path=args.state, adapter_root=args.adapter_root, framework_root=args.framework_root,
        output_root=args.output_root, status_root=args.status_root, lock_root=args.lock_root,
        baseline_path=args.baseline,
        start=args.start, end=args.end or ((today or date.today()) - timedelta(days=args.end_lag_days)),
        projects=tuple(args.project or ()),
        countries=_comma_values(args.country), source_ids=tuple(args.source_id or ()),
        limit=args.limit, workers=args.workers, overlap_days=args.overlap_days,
        initial_lookback_days=args.initial_lookback_days,
        default_interval_hours=args.default_interval_hours,
        base_backoff_seconds=args.base_backoff_seconds, max_backoff_seconds=args.max_backoff_seconds,
        max_failures=args.max_failures, heartbeat_seconds=args.heartbeat_seconds,
        min_free_gb=args.min_free_gb,
        commit=args.commit, force=args.force, ignore_backoff=args.ignore_backoff,
        retry_blocked=args.retry_blocked, explicit_adapter_map=explicit_map,
    )


def main() -> None:
    args = build_parser().parse_args()
    while True:
        result = Orchestrator(options_from_args(args)).run_once()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not args.loop:
            break
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    main()
