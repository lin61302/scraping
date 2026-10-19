#!/usr/bin/env python3
"""Run one immutable ten-country pure-observation campaign on this host.

This controller is deliberately smaller than the production orchestrator.  It
executes only ``scripts/parallel_observe_pure.py`` with one worker per source.
It never imports the promotion pipeline, opens MongoDB, or offers a production
write option.  Four country sessions may advance concurrently and sources in a
country session always run in plan order.

The plan and every executable Python dependency are hash pinned.  Observations
are evidence for later review; a terminal receipt does not advance a tracker
boundary or authorize production promotion.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
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
from typing import Callable, Iterator, Mapping, Optional, Sequence

try:  # support both ``python ops/file.py`` and namespace-package test imports
    from .host_capacity_guard import collect_metrics, evaluate_capacity
except ImportError:  # pragma: no cover - exercised by direct script execution
    from host_capacity_guard import collect_metrics, evaluate_capacity


AUTOMATION_ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORBIDDEN_IMPORTS = frozenset({"pymongo", "mongoengine", "motor"})
FORBIDDEN_CALLS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many",
    "delete_one", "delete_many", "bulk_write",
})
BROWSER_IMPORTS = frozenset({"selenium", "playwright"})
SOURCE_TERMINAL = "terminal_receipt.json"
OBSERVER_LOG_MAX_BYTES = 50 * 1024 * 1024
CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
})


class PlanError(ValueError):
    """The immutable observation plan is unsafe or inconsistent."""


class ControllerAlreadyRunning(RuntimeError):
    """Another local country observation controller owns the host lock."""


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
        raise PlanError(f"invalid JSON: {exc}") from exc


def _create_json_exclusive(path: Path, value: object) -> None:
    """Create one immutable receipt without replacing an existing receipt."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()
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


def _load_object(path: Path) -> dict[str, object]:
    value = _json_no_duplicates(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PlanError(f"expected JSON object: {path}")
    return value


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")[:90]
    return f"{clean}-{hashlib.sha256(value.encode()).hexdigest()[:10]}"


def _parse_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{field} must be an ISO date") from exc


def _positive_int(value: object, field: str, *, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise PlanError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{field} must be an integer") from exc
    if number < 1 or (maximum is not None and number > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise PlanError(f"{field} must be at least 1{suffix}")
    return number


def _resolve_repository_file(raw: object, automation_root: Path, field: str) -> Path:
    text = str(raw or "")
    if not text or Path(text).is_absolute():
        raise PlanError(f"{field} must be a repository-relative path")
    path = Path(text)
    if path.parts and path.parts[0] == "automation":
        path = Path(*path.parts[1:])
    resolved = (automation_root / path).resolve()
    root = automation_root.resolve()
    if root != resolved and root not in resolved.parents:
        raise PlanError(f"{field} escapes automation root")
    return resolved


def execution_bundle_sha256(paths: Sequence[Path], automation_root: Path) -> str:
    root = automation_root.resolve()
    records: list[tuple[str, Path]] = []
    for item in paths:
        resolved = item.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise PlanError(f"execution file outside automation root: {resolved}") from exc
        records.append((relative, resolved))
    digest = hashlib.sha256()
    for relative, path in sorted(records):
        digest.update(relative.encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def adapter_bundle_sha256(path: Path) -> str:
    files = [path.resolve()]
    common = path.resolve().parent / "common.py"
    if common.is_file() and common not in files:
        files.append(common)
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda candidate: candidate.name):
        digest.update(item.name.encode() + b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _syntax_tree(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise PlanError(f"cannot parse Python file {path}: {exc}") from exc


def _static_date_call(value: ast.expr, path: Path, name: str) -> date:
    """Parse a literal ``date(year, month, day)`` call without importing code."""

    if (
        not isinstance(value, ast.Call)
        or not isinstance(value.func, ast.Name)
        or value.func.id != "date"
        or value.keywords
        or len(value.args) != 3
    ):
        raise PlanError(f"{path} {name} must be a literal date(year, month, day) call")
    parts: list[int] = []
    for argument in value.args:
        if (
            not isinstance(argument, ast.Constant)
            or isinstance(argument.value, bool)
            or not isinstance(argument.value, int)
        ):
            raise PlanError(f"{path} {name} must use literal integer date parts")
        parts.append(argument.value)
    try:
        return date(*parts)
    except ValueError as exc:
        raise PlanError(f"{path} {name} is not a valid date") from exc


def _adapter_observation_window(path: Path) -> tuple[date, date]:
    """Read the adapter's top-level immutable observation window statically."""

    required = {"ACCEPT_START", "FIRST_PASS_TARGET"}
    values: dict[str, date] = {}
    for statement in _syntax_tree(path).body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        names = {
            target.id
            for target in targets
            if isinstance(target, ast.Name) and target.id in required
        }
        for name in names:
            if name in values:
                raise PlanError(f"{path} has multiple top-level assignments for {name}")
            if value is None:
                raise PlanError(f"{path} {name} must have a value")
            values[name] = _static_date_call(value, path, name)
    missing = sorted(required - values.keys())
    if missing:
        raise PlanError(f"{path} lacks top-level date assignment(s): {', '.join(missing)}")
    return values["ACCEPT_START"], values["FIRST_PASS_TARGET"]


def _module_path(module: str, automation_root: Path) -> Optional[Path]:
    if not module:
        return None
    candidate = automation_root.joinpath(*module.split("."))
    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return module_file.resolve()
    package = candidate / "__init__.py"
    return package.resolve() if package.is_file() else None


def _local_imports(path: Path, automation_root: Path) -> set[Path]:
    """Resolve direct imports that point to Python files under automation."""

    tree = _syntax_tree(path)
    root = automation_root.resolve()
    relative = path.resolve().relative_to(root)
    package_parts = list(relative.parts[:-1])
    imports: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _module_path(alias.name, root)
                if resolved:
                    imports.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package_parts) - node.level + 1)
                base_parts = package_parts[:keep]
                if node.module:
                    base_parts.extend(node.module.split("."))
                module = ".".join(base_parts)
            else:
                module = node.module or ""
            resolved = _module_path(module, root)
            if resolved:
                imports.add(resolved)
            for alias in node.names:
                if alias.name == "*":
                    continue
                child = _module_path(".".join(part for part in (module, alias.name) if part), root)
                if child:
                    imports.add(child)
    return imports


def local_dependency_closure(path: Path, automation_root: Path) -> set[Path]:
    pending = [path.resolve()]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(sorted(_local_imports(current, automation_root) - seen))
    return seen


def _python_contract(path: Path, *, require_adapter: bool = False) -> dict[str, object]:
    tree = _syntax_tree(path)
    imports: set[str] = set()
    calls: set[str] = set()
    functions: set[str] = set()
    config_values: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        # Relative imports name files inside the already pinned local dependency
        # closure (for example ``from .validation import ...``).  Treating the
        # relative module name as a third-party package makes a valid immutable
        # bundle fail preflight with a misleading missing-dependency error.
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        elif isinstance(node, ast.ClassDef) and node.name == "Config":
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id in {
                        "source_name", "country_code", "is_env", "production_writes"
                    }
                ):
                    try:
                        config_values[statement.targets[0].id] = ast.literal_eval(statement.value)
                    except (TypeError, ValueError):
                        pass
    bad_imports = sorted(imports & FORBIDDEN_IMPORTS)
    bad_calls = sorted(calls & FORBIDDEN_CALLS)
    browser_imports = sorted(imports & BROWSER_IMPORTS)
    if bad_imports or bad_calls or browser_imports:
        raise PlanError(
            f"unsafe observation file {path}: database_imports={bad_imports} "
            f"database_calls={bad_calls} browser_imports={browser_imports}"
        )
    if require_adapter and not {"discover_urls", "scrape_url"} <= functions:
        raise PlanError(f"adapter lacks pure discover_urls/scrape_url contract: {path}")
    if require_adapter and config_values.get("production_writes") not in {None, False}:
        raise PlanError(f"adapter enables production writes: {path}")
    return {"imports": imports, "functions": functions, "config": config_values}


@dataclass(frozen=True)
class PinnedFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    adapter: Path
    adapter_sha256: str
    source_bundle_sha256: str
    execution_files: tuple[PinnedFile, ...]
    execution_bundle_sha256: str
    start: date
    end: date
    timeout_seconds: int


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
    target_end: date
    observer: Path
    observer_sha256: str
    observer_execution_files: tuple[PinnedFile, ...]
    observer_execution_bundle_sha256: str
    sessions: tuple[SessionSpec, ...]
    max_wall_seconds: int


@dataclass(frozen=True)
class RuntimePaths:
    python: Path
    automation_root: Path
    output_root: Path
    receipt_root: Path
    host_lock_path: Path
    min_free_gb: float = 20.0
    network_sessions: int = 4


@dataclass(frozen=True)
class ExecutionResult:
    state: str
    details: Mapping[str, object]


def host_capacity_report(runtime: RuntimePaths) -> dict[str, object]:
    """Run the shared read-only capacity guard with campaign-only patterns."""

    runtime.output_root.mkdir(parents=True, exist_ok=True)
    runtime.receipt_root.mkdir(parents=True, exist_ok=True)
    metrics = collect_metrics(
        runtime.automation_root.parent,
        [runtime.receipt_root],
        [runtime.output_root],
        scrape_patterns=(
            "parallel_observe_pure.py",
            "local_unattended_country_observation.py",
        ),
        browser_patterns=(
            "chromedriver", "geckodriver", "playwright", "--headless",
            "--remote-debugging-port", "selenium",
        ),
    )
    return evaluate_capacity(metrics)


def _parse_pinned_files(
    raw: object, automation_root: Path, field: str,
) -> tuple[PinnedFile, ...]:
    if not isinstance(raw, list) or not raw:
        raise PlanError(f"{field} must be a nonempty list")
    result: list[PinnedFile] = []
    seen: set[Path] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PlanError(f"{field}[{index}] must be an object")
        path = _resolve_repository_file(
            item.get("repository_path", item.get("path")), automation_root,
            f"{field}[{index}].repository_path",
        )
        digest = str(item.get("sha256") or "").lower()
        if not HEX64.fullmatch(digest) or path in seen:
            raise PlanError(f"{field}[{index}] has invalid hash or duplicate path")
        seen.add(path)
        result.append(PinnedFile(path, digest))
    return tuple(result)


def _verify_bundle(
    files: tuple[PinnedFile, ...], expected_hash: object, automation_root: Path, field: str,
) -> str:
    digest = str(expected_hash or "").lower()
    if not HEX64.fullmatch(digest):
        raise PlanError(f"{field} requires a SHA-256")
    for item in files:
        if not item.path.is_file() or sha256_path(item.path) != item.sha256:
            raise PlanError(f"{field} file missing or hash drifted: {item.path}")
    actual = execution_bundle_sha256([item.path for item in files], automation_root)
    if actual != digest:
        raise PlanError(f"{field} aggregate hash mismatch")
    return digest


def _verify_execution_pins(
    plan: CampaignPlan, source: SourceSpec, automation_root: Path,
) -> None:
    """Recheck every executable pin at the child process boundary."""

    if not plan.observer.is_file() or sha256_path(plan.observer) != plan.observer_sha256:
        raise PlanError(f"observer hash drifted: {plan.observer}")
    _verify_bundle(
        plan.observer_execution_files,
        plan.observer_execution_bundle_sha256,
        automation_root,
        "observer.execution_bundle",
    )
    if not source.adapter.is_file() or sha256_path(source.adapter) != source.adapter_sha256:
        raise PlanError(f"source adapter hash drifted: {source.adapter}")
    if adapter_bundle_sha256(source.adapter) != source.source_bundle_sha256:
        raise PlanError(f"source adapter bundle hash drifted: {source.adapter}")
    _verify_bundle(
        source.execution_files,
        source.execution_bundle_sha256,
        automation_root,
        f"source {source.source_id} execution_bundle",
    )


def _require_closure_in_bundle(
    entry: Path, files: tuple[PinnedFile, ...], automation_root: Path, field: str,
) -> None:
    declared = {item.path.resolve() for item in files}
    missing = sorted(local_dependency_closure(entry, automation_root) - declared)
    if missing:
        names = ", ".join(str(path) for path in missing[:8])
        raise PlanError(f"{field} omits local Python dependencies: {names}")


def _parse_source(
    raw: object, *, label: str, country: str, target_end: date, automation_root: Path,
) -> SourceSpec:
    if not isinstance(raw, Mapping):
        raise PlanError(f"{label} must be an object")
    source_id = str(raw.get("source_id") or "")
    parts = source_id.split(":", 2)
    if len(parts) != 3 or parts[1].upper() != country:
        raise PlanError(f"{label}.source_id does not match session country")
    if raw.get("production_writes") is not False or raw.get("resource_class") != "network":
        raise PlanError(f"{label} must declare network observation with production_writes=false")
    adapter = _resolve_repository_file(
        raw.get("adapter_repository_path", raw.get("adapter_path")), automation_root,
        f"{label}.adapter_repository_path",
    )
    adapter_hash = str(raw.get("adapter_sha256") or "").lower()
    source_bundle_hash = str(raw.get("source_bundle_sha256") or "").lower()
    if not HEX64.fullmatch(adapter_hash) or not HEX64.fullmatch(source_bundle_hash):
        raise PlanError(f"{label} requires adapter and source-bundle SHA-256 values")
    if not adapter.is_file() or sha256_path(adapter) != adapter_hash:
        raise PlanError(f"{label} adapter missing or hash drifted")
    if adapter_bundle_sha256(adapter) != source_bundle_hash:
        raise PlanError(f"{label} source bundle hash mismatch")
    contract = _python_contract(adapter, require_adapter=True)
    config = contract["config"]
    expected_project = "mleed" if config.get("is_env") is True else "mlp"
    configured_domain = str(config.get("source_name") or "").lower()
    if configured_domain.startswith("www."):
        configured_domain = configured_domain[4:]
    configured_country = str(config.get("country_code") or "").upper()
    source_domain = parts[2].lower()
    if source_domain.startswith("www."):
        source_domain = source_domain[4:]
    if configured_domain and configured_domain != source_domain:
        raise PlanError(f"{label} source identity differs from Config.source_name")
    if configured_country and configured_country != country:
        raise PlanError(f"{label} country differs from Config.country_code")
    if "is_env" in config and parts[0].lower() != expected_project:
        raise PlanError(f"{label} project differs from Config.is_env")
    start = _parse_date(raw.get("start", raw.get("fetch_start")), f"{label}.start")
    end = _parse_date(raw.get("end"), f"{label}.end")
    if not start <= end or end != target_end:
        raise PlanError(f"{label} requires start <= end and end equal to target")
    adapter_start, adapter_end = _adapter_observation_window(adapter)
    if start != adapter_start or end != adapter_end:
        raise PlanError(
            f"{label} plan window {start.isoformat()}..{end.isoformat()} does not match "
            f"adapter ACCEPT_START/FIRST_PASS_TARGET "
            f"{adapter_start.isoformat()}..{adapter_end.isoformat()}"
        )
    files = _parse_pinned_files(raw.get("execution_bundle_files"), automation_root, f"{label}.execution_bundle_files")
    bundle_hash = _verify_bundle(
        files, raw.get("execution_bundle_sha256"), automation_root,
        f"{label}.execution_bundle",
    )
    if adapter.resolve() not in {item.path.resolve() for item in files}:
        raise PlanError(f"{label} execution bundle omits adapter")
    _require_closure_in_bundle(adapter, files, automation_root, f"{label}.execution_bundle")
    for item in files:
        if item.path.suffix == ".py":
            _python_contract(item.path)
    return SourceSpec(
        source_id=source_id, adapter=adapter, adapter_sha256=adapter_hash,
        source_bundle_sha256=source_bundle_hash, execution_files=files,
        execution_bundle_sha256=bundle_hash, start=start, end=end,
        timeout_seconds=_positive_int(raw.get("timeout_seconds"), f"{label}.timeout_seconds", maximum=86_400),
    )


def load_plan(path: Path, expected_sha256: str, automation_root: Path) -> CampaignPlan:
    expected_sha256 = expected_sha256.lower()
    if not HEX64.fullmatch(expected_sha256):
        raise PlanError("--plan-sha256 must be lowercase hexadecimal")
    raw_bytes = path.read_bytes()
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != expected_sha256:
        raise PlanError(f"plan hash mismatch: expected {expected_sha256}, got {actual}")
    payload = _json_no_duplicates(raw_bytes.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise PlanError("plan must be an object")
    if payload.get("schema_version") != 1 or payload.get("immutable") is not True:
        raise PlanError("plan must declare schema_version=1 and immutable=true")
    if payload.get("mode") != "pure_observation_only" or payload.get("production_writes") is not False:
        raise PlanError("plan must be pure_observation_only with production_writes=false")
    campaign_id = str(payload.get("campaign_id") or "")
    if not SAFE_ID.fullmatch(campaign_id):
        raise PlanError("campaign_id is empty or unsafe")
    target_end = _parse_date(payload.get("first_pass_target_end"), "first_pass_target_end")
    concurrency = payload.get("concurrency")
    if not isinstance(concurrency, Mapping) or concurrency.get("network") != 4:
        raise PlanError("plan must declare exactly four network workers")
    if concurrency.get("browser") != 0 or concurrency.get("max_active_country_sessions") != 4:
        raise PlanError("local queue requires browser=0 and four active country sessions")
    observer_raw = payload.get("observer")
    if not isinstance(observer_raw, Mapping):
        raise PlanError("observer object is required")
    observer = _resolve_repository_file(
        observer_raw.get("repository_path"), automation_root, "observer.repository_path"
    )
    if observer.name != "parallel_observe_pure.py":
        raise PlanError("observer must be parallel_observe_pure.py")
    observer_hash = str(observer_raw.get("sha256") or "").lower()
    if not observer.is_file() or not HEX64.fullmatch(observer_hash) or sha256_path(observer) != observer_hash:
        raise PlanError("observer is missing or hash drifted")
    observer_files = _parse_pinned_files(
        observer_raw.get("execution_bundle_files"), automation_root,
        "observer.execution_bundle_files",
    )
    observer_bundle = _verify_bundle(
        observer_files, observer_raw.get("execution_bundle_sha256"), automation_root,
        "observer.execution_bundle",
    )
    if observer.resolve() not in {item.path.resolve() for item in observer_files}:
        raise PlanError("observer execution bundle omits observer")
    _require_closure_in_bundle(observer, observer_files, automation_root, "observer.execution_bundle")
    for item in observer_files:
        if item.path.suffix == ".py":
            _python_contract(item.path)
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list) or len(raw_sessions) != 10:
        raise PlanError("local campaign must contain exactly ten country sessions")
    sessions: list[SessionSpec] = []
    session_ids: set[str] = set()
    source_ids: set[str] = set()
    countries: set[str] = set()
    for session_index, raw_session in enumerate(raw_sessions):
        label = f"sessions[{session_index}]"
        if not isinstance(raw_session, Mapping):
            raise PlanError(f"{label} must be an object")
        session_id = str(raw_session.get("session_id") or "")
        country = str(raw_session.get("country") or "").upper()
        if (
            not SAFE_ID.fullmatch(session_id) or session_id in session_ids
            or not re.fullmatch(r"[A-Z0-9]{2,4}", country) or country in countries
            or raw_session.get("execution") != "sequential_sources"
        ):
            raise PlanError(f"{label} has invalid identity, country, or execution mode")
        raw_sources = raw_session.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise PlanError(f"{label}.sources must be nonempty")
        sources = tuple(
            _parse_source(
                row, label=f"{label}.sources[{index}]", country=country,
                target_end=target_end, automation_root=automation_root,
            )
            for index, row in enumerate(raw_sources)
        )
        for source in sources:
            if source.source_id in source_ids:
                raise PlanError(f"duplicate source_id: {source.source_id}")
            source_ids.add(source.source_id)
        session_ids.add(session_id)
        countries.add(country)
        sessions.append(SessionSpec(session_id, country, sources))
    declared = payload.get("source_count")
    if declared is not None and int(declared) != len(source_ids):
        raise PlanError("source_count does not match plan")
    return CampaignPlan(
        campaign_id=campaign_id, plan_sha256=actual, raw_bytes=raw_bytes,
        target_end=target_end, observer=observer, observer_sha256=observer_hash,
        observer_execution_files=observer_files,
        observer_execution_bundle_sha256=observer_bundle,
        sessions=tuple(sessions),
        max_wall_seconds=_positive_int(
            payload.get("max_wall_seconds", 604_800), "max_wall_seconds", maximum=604_800
        ),
    )


def external_dependency_errors(files: Sequence[Path], automation_root: Path) -> list[str]:
    """Report missing imported packages without importing selected adapters."""

    local_roots = {"adapters", "scraper_control", "ops", "scripts"}
    modules: set[str] = set()
    for path in files:
        contract = _python_contract(path)
        relative = path.resolve().relative_to(automation_root.resolve()).as_posix()
        for item in contract["imports"]:
            module = str(item)
            # ``scraper_control.observe`` retains a legacy SourcePipeline
            # fallback behind ``if not pure_adapter``.  This controller admits
            # only adapters that statically provide the pure contract, so that
            # branch is unreachable and its separately deployed ``core``
            # package is neither required nor allowed into this run bundle.
            if module == "core" and relative == "scraper_control/observe.py":
                continue
            modules.add(module)
    errors: list[str] = []
    stdlib_module_names = frozenset(
        getattr(sys, "stdlib_module_names", sys.builtin_module_names)
    )
    for module in sorted(modules - local_roots):
        if module in stdlib_module_names:
            continue
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, AttributeError, ValueError):
            available = False
        if not available:
            errors.append(f"missing_python_dependency:{module}")
    return errors


def build_observer_command(
    runtime: RuntimePaths, plan: CampaignPlan, source: SourceSpec, output_root: Path,
) -> list[str]:
    return [
        str(runtime.python), str(plan.observer),
        "--automation-root", str(runtime.automation_root),
        "--source-file", str(source.adapter),
        "--start", source.start.isoformat(),
        "--end", source.end.isoformat(),
        "--output-root", str(output_root),
        "--workers", "1",
    ]


def terminate_process_group(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


class PureObserverExecutor:
    def __init__(self, runtime: RuntimePaths, plan: CampaignPlan):
        self.runtime = runtime
        self.plan = plan

    def __call__(self, source: SourceSpec, source_dir: Path, deadline: float) -> ExecutionResult:
        output_root = self.runtime.output_root / self.plan.campaign_id / _safe_name(source.source_id)
        output_root.mkdir(parents=True, exist_ok=True)
        before = {path.resolve() for path in output_root.glob("observe-parallel-*") if path.is_dir()}
        log_path = source_dir / "observer.log"
        command = build_observer_command(self.runtime, self.plan, source, output_root)
        env = {
            key: value for key, value in os.environ.items()
            if key in CHILD_ENV_ALLOWLIST
        }
        env["PYTHONPATH"] = str(self.runtime.automation_root)
        # Candidate adapters fail closed unless the reviewed bounded controller
        # explicitly opts them into network observation.  Never inherit a
        # caller supplied value here: this is the one authorized launch path.
        env["SCRAPER_BOUNDED_OBSERVATION"] = "1"
        pin_verification: dict[str, object] = {}
        details: dict[str, object] = {
            "runner_exit_code": None,
            "timed_out": False,
            "command_contract": {
                "observer_sha256": self.plan.observer_sha256,
                "workers": 1,
                "bounded_observation_environment": {"SCRAPER_BOUNDED_OBSERVATION": "1"},
                "production_writes": False,
            },
            "execution_pin_verification": pin_verification,
        }
        try:
            _verify_execution_pins(self.plan, source, self.runtime.automation_root)
        except (OSError, PlanError) as exc:
            pin_verification["before_run"] = {
                "passed": False, "checked_at_utc": iso_now(),
                "error": str(exc)[:500],
            }
            details["contract_error"] = "execution_pin_verification_failed_before_run"
            return ExecutionResult("isolated_source_failure", details)
        pin_verification["before_run"] = {"passed": True, "checked_at_utc": iso_now()}

        timed_out = False
        log_counts = {"bytes_seen": 0, "bytes_kept": 0}
        drain_errors: list[str] = []
        with log_path.open("xb") as stream:
            process = subprocess.Popen(
                command, cwd=str(self.runtime.automation_root.parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
            )
            if process.stdout is None:  # pragma: no cover - PIPE guarantees this
                raise RuntimeError("observer stdout pipe was not created")

            def drain_stdout() -> None:
                try:
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk:
                            break
                        log_counts["bytes_seen"] += len(chunk)
                        remaining = OBSERVER_LOG_MAX_BYTES - log_counts["bytes_kept"]
                        if remaining > 0:
                            kept = chunk[:remaining]
                            stream.write(kept)
                            log_counts["bytes_kept"] += len(kept)
                except Exception as exc:  # preserve a terminal receipt for I/O failures
                    drain_errors.append(f"{type(exc).__name__}:{str(exc)[:300]}")
                finally:
                    process.stdout.close()

            drain_thread = threading.Thread(
                target=drain_stdout,
                name=f"observer-log-{_safe_name(source.source_id)[:32]}",
                daemon=True,
            )
            drain_thread.start()
            timeout = max(0.1, min(source.timeout_seconds, deadline - time.monotonic()))
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process_group(process)
                returncode = process.returncode
            drain_thread.join()
            stream.flush()
            os.fsync(stream.fileno())

        details["runner_exit_code"] = returncode
        details["timed_out"] = timed_out
        details["observer_log"] = {
            "path": str(log_path), "sha256": sha256_path(log_path),
            "max_bytes": OBSERVER_LOG_MAX_BYTES,
            "bytes_seen": log_counts["bytes_seen"],
            "bytes_kept": log_counts["bytes_kept"],
            "bytes_truncated": log_counts["bytes_seen"] - log_counts["bytes_kept"],
        }
        if drain_errors:
            details["observer_log"]["drain_errors"] = drain_errors
        try:
            _verify_execution_pins(self.plan, source, self.runtime.automation_root)
        except (OSError, PlanError) as exc:
            pin_verification["after_run"] = {
                "passed": False, "checked_at_utc": iso_now(),
                "error": str(exc)[:500],
            }
            details["contract_error"] = "execution_pin_verification_failed_after_run"
            return ExecutionResult("isolated_source_failure", details)
        pin_verification["after_run"] = {"passed": True, "checked_at_utc": iso_now()}
        if drain_errors:
            details["contract_error"] = "observer_log_drain_failed"
            return ExecutionResult("isolated_source_failure", details)
        if timed_out:
            return ExecutionResult("isolated_source_timeout", details)
        after = {path.resolve() for path in output_root.glob("observe-parallel-*") if path.is_dir()}
        created = sorted(after - before)
        if returncode != 0 or len(created) != 1:
            details["created_run_directory_count"] = len(created)
            return ExecutionResult("isolated_source_failure", details)
        run_dir = created[0]
        manifest_path = run_dir / "manifest.json"
        try:
            manifest = _load_object(manifest_path)
        except (OSError, PlanError) as exc:
            details["contract_error"] = f"manifest_unreadable:{type(exc).__name__}"
            return ExecutionResult("isolated_source_failure", details)
        unsafe = (
            manifest.get("status") != "observed"
            or manifest.get("mode") != "observation_no_production_writes"
            or manifest.get("adapter_contract") != "pure_network_only"
            or manifest.get("source_sha256") != source.adapter_sha256
            or manifest.get("source_bundle_sha256") != source.source_bundle_sha256
            or manifest.get("requested_start") != source.start.isoformat()
            or manifest.get("requested_end") != source.end.isoformat()
            or (manifest.get("observer_execution") or {}).get("workers") != 1
            or (run_dir / "promotion.json").exists()
            or (run_dir / "promotion_dry_run.json").exists()
        )
        details.update({
            "run_dir": str(run_dir),
            "manifest": {"path": str(manifest_path), "sha256": sha256_path(manifest_path)},
            "counts": manifest.get("counts") if isinstance(manifest.get("counts"), Mapping) else {},
        })
        if unsafe:
            details["contract_error"] = "observation_safety_contract_failed"
            return ExecutionResult("isolated_source_failure", details)
        counts = details["counts"]
        shortfalls = int(counts.get("exceptions", 0)) + int(counts.get("quarantined", 0))
        return ExecutionResult(
            "observed_with_shortfalls" if shortfalls else "observed_terminal", details
        )


class CampaignController:
    def __init__(
        self,
        plan: CampaignPlan,
        runtime: RuntimePaths,
        *,
        executor: Optional[Callable[[SourceSpec, Path, float], ExecutionResult]] = None,
        capacity_reporter: Optional[Callable[[], Mapping[str, object]]] = None,
    ):
        self.plan = plan
        self.runtime = runtime
        self.campaign_dir = runtime.receipt_root / plan.campaign_id
        self.executor = executor or PureObserverExecutor(runtime, plan)
        self.capacity_reporter = capacity_reporter or (lambda: host_capacity_report(runtime))
        self.deadline = 0.0
        self._event_lock = threading.Lock()
        self._capacity_condition = threading.Condition()
        self._active_sources = 0

    def _event(self, kind: str, **values: object) -> None:
        payload = json.dumps(
            {"at_utc": iso_now(), "kind": kind, **values}, ensure_ascii=False, sort_keys=True
        ) + "\n"
        with self._event_lock:
            path = self.campaign_dir / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    @contextmanager
    def host_lock(self) -> Iterator[None]:
        path = self.runtime.host_lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ControllerAlreadyRunning(f"controller lock is held: {path}") from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (json.dumps({
                "pid": os.getpid(), "campaign_id": self.plan.campaign_id,
                "acquired_at_utc": iso_now(),
            }) + "\n").encode())
            os.fsync(descriptor)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _pin_plan(self) -> None:
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        pinned = self.campaign_dir / "plan.json"
        if pinned.exists():
            if pinned.read_bytes() != self.plan.raw_bytes:
                raise PlanError("campaign plan changed after first launch")
        else:
            descriptor = os.open(pinned, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            try:
                os.write(descriptor, self.plan.raw_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        receipt = self.campaign_dir / "plan_receipt.json"
        if not receipt.exists():
            _create_json_exclusive(receipt, {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "plan_path": str(pinned), "plan_sha256": self.plan.plan_sha256,
                "pinned_at_utc": iso_now(), "production_writes": False,
            })

    def preflight(self) -> dict[str, object]:
        errors: list[str] = []
        if not self.runtime.python.is_file():
            errors.append(f"missing_python:{self.runtime.python}")
        if self.runtime.min_free_gb < 0:
            errors.append("min_free_gb_negative")
        if self.runtime.network_sessions not in {2, 4}:
            errors.append("network_sessions_must_be_2_or_4")
        probe = self.runtime.output_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_bytes: Optional[int]
        try:
            free_bytes = shutil.disk_usage(probe).free
            if free_bytes < int(self.runtime.min_free_gb * 1024**3):
                errors.append("insufficient_free_space")
        except OSError:
            free_bytes = None
            errors.append("free_space_unavailable")
        all_files = {
            item.path for item in self.plan.observer_execution_files
        } | {
            item.path
            for session in self.plan.sessions
            for source in session.sources
            for item in source.execution_files
        }
        errors.extend(external_dependency_errors(sorted(all_files), self.runtime.automation_root))
        return {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "plan_sha256": self.plan.plan_sha256, "checked_at_utc": iso_now(),
            "passed": not errors, "errors": errors, "production_writes": False,
            "session_count": 10,
            "source_count": sum(len(session.sources) for session in self.plan.sessions),
            "capacity": {
                "country_sessions": self.runtime.network_sessions,
                "network": self.runtime.network_sessions,
                "browser": 0,
                "plan_network_ceiling": 4,
            },
            "storage": {
                "probe_path": str(probe), "free_bytes": free_bytes,
                "required_free_bytes": int(self.runtime.min_free_gb * 1024**3),
            },
        }

    def _source_dir(self, session: SessionSpec, source: SourceSpec) -> Path:
        return (
            self.campaign_dir / "sessions" / _safe_name(session.session_id)
            / "sources" / _safe_name(source.source_id)
        )

    def _capacity_blocked_result(
        self, session: SessionSpec, source: SourceSpec, report: Mapping[str, object],
    ) -> dict[str, object]:
        source_dir = self._source_dir(session, source)
        terminal = source_dir / SOURCE_TERMINAL
        if terminal.exists():
            return _load_object(terminal)
        receipt = {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "source_id": source.source_id, "session_id": session.session_id,
            "state": "not_started_capacity_blocked", "terminal": True,
            "finished_at_utc": iso_now(), "production_writes": False,
            "verified_completion": False, "plan_sha256": self.plan.plan_sha256,
            "adapter_sha256": source.adapter_sha256,
            "window": {"start": source.start.isoformat(), "end": source.end.isoformat()},
            "capacity_guard": dict(report),
        }
        _create_json_exclusive(terminal, receipt)
        self._event("source_capacity_blocked", source_id=source.source_id)
        return receipt

    @contextmanager
    def _source_capacity_slot(self) -> Iterator[tuple[bool, Mapping[str, object]]]:
        """Recheck capacity and admit one source at the current network limit."""

        admitted = False
        report: Mapping[str, object] = {}
        with self._capacity_condition:
            while True:
                report = self.capacity_reporter()
                status = str(report.get("status") or "blocked")
                limits = report.get("recommended_limits")
                recommended = (
                    int(limits.get("network_workers") or 0)
                    if isinstance(limits, Mapping) else 0
                )
                if status == "blocked" or report.get("launch_allowed") is not True or recommended < 1:
                    break
                limit = min(self.runtime.network_sessions, recommended)
                if self._active_sources < limit:
                    self._active_sources += 1
                    admitted = True
                    break
                self._capacity_condition.wait(timeout=5)
        try:
            yield admitted, report
        finally:
            if admitted:
                with self._capacity_condition:
                    self._active_sources -= 1
                    self._capacity_condition.notify_all()

    def _deadline_result(self, source: SourceSpec, source_dir: Path) -> dict[str, object]:
        receipt = {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "source_id": source.source_id, "state": "not_started_campaign_deadline",
            "terminal": True, "finished_at_utc": iso_now(), "production_writes": False,
            "verified_completion": False, "plan_sha256": self.plan.plan_sha256,
            "adapter_sha256": source.adapter_sha256,
            "window": {"start": source.start.isoformat(), "end": source.end.isoformat()},
        }
        _create_json_exclusive(source_dir / SOURCE_TERMINAL, receipt)
        return receipt

    def _run_source(self, session: SessionSpec, source: SourceSpec) -> dict[str, object]:
        source_dir = self._source_dir(session, source)
        terminal = source_dir / SOURCE_TERMINAL
        start = source_dir / "start_receipt.json"
        if terminal.exists():
            return _load_object(terminal)
        if start.exists():
            receipt = {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "source_id": source.source_id, "state": "interrupted_requires_review",
                "terminal": True, "finished_at_utc": iso_now(), "production_writes": False,
                "verified_completion": False,
                "start_receipt": {"path": str(start), "sha256": sha256_path(start)},
            }
            _create_json_exclusive(terminal, receipt)
            return receipt
        if time.monotonic() >= self.deadline:
            return self._deadline_result(source, source_dir)
        _create_json_exclusive(start, {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "source_id": source.source_id, "session_id": session.session_id,
            "started_at_utc": iso_now(), "controller_pid": os.getpid(),
            "plan_sha256": self.plan.plan_sha256, "adapter_sha256": source.adapter_sha256,
            "observer_sha256": self.plan.observer_sha256, "workers": 1,
            "production_writes": False,
        })
        try:
            result = self.executor(source, source_dir, self.deadline)
        except Exception as exc:  # source isolation is intentional
            result = ExecutionResult("isolated_source_failure", {
                "exception_type": type(exc).__name__, "message": str(exc)[:500],
            })
        receipt = {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "source_id": source.source_id, "session_id": session.session_id,
            "state": result.state, "terminal": True, "finished_at_utc": iso_now(),
            "mode": "pure_observation_only", "production_writes": False,
            "verified_completion": False,
            "completion_semantics": "terminal observation evidence only; review is required",
            "plan_sha256": self.plan.plan_sha256,
            "adapter_sha256": source.adapter_sha256,
            "source_bundle_sha256": source.source_bundle_sha256,
            "execution_bundle_sha256": source.execution_bundle_sha256,
            "window": {"start": source.start.isoformat(), "end": source.end.isoformat()},
            "details": dict(result.details),
        }
        _create_json_exclusive(terminal, receipt)
        self._event("source_terminal", source_id=source.source_id, state=result.state)
        return receipt

    def _run_session(self, session: SessionSpec) -> dict[str, object]:
        directory = self.campaign_dir / "sessions" / _safe_name(session.session_id)
        terminal = directory / SOURCE_TERMINAL
        if terminal.exists():
            return _load_object(terminal)
        receipts: list[dict[str, object]] = []
        for source in session.sources:
            with self._source_capacity_slot() as (admitted, capacity):
                if admitted:
                    receipts.append(self._run_source(session, source))
                else:
                    receipts.append(self._capacity_blocked_result(session, source, capacity))
        states = [str(item.get("state")) for item in receipts]
        result = {
            "schema_version": 1, "campaign_id": self.plan.campaign_id,
            "session_id": session.session_id, "country": session.country,
            "status": "terminal_observation_sequence", "finished_at_utc": iso_now(),
            "production_writes": False, "planned_sources": len(session.sources),
            "terminal_sources": len(receipts), "states": states,
            "sources": [
                {"source_id": item.get("source_id"), "state": item.get("state")}
                for item in receipts
            ],
        }
        _create_json_exclusive(terminal, result)
        return result

    def run(self) -> dict[str, object]:
        with self.host_lock():
            self._pin_plan()
            campaign_terminal = self.campaign_dir / SOURCE_TERMINAL
            if campaign_terminal.exists():
                return _load_object(campaign_terminal)
            startup = self.campaign_dir / "startup_receipt.json"
            if not startup.exists():
                _create_json_exclusive(startup, {
                    "schema_version": 1, "campaign_id": self.plan.campaign_id,
                    "state": "running", "started_at_utc": iso_now(),
                    "pid": os.getpid(), "hostname": os.uname().nodename,
                    "plan_sha256": self.plan.plan_sha256,
                    "capacity": {
                        "country_sessions": self.runtime.network_sessions,
                        "network": self.runtime.network_sessions,
                        "browser": 0,
                        "plan_network_ceiling": 4,
                    },
                    "production_writes": False,
                })
            check = self.preflight()
            preflight_path = self.campaign_dir / "preflight.json"
            if not preflight_path.exists():
                _create_json_exclusive(preflight_path, check)
            if not check["passed"]:
                result = {
                    "schema_version": 1, "campaign_id": self.plan.campaign_id,
                    "status": "preflight_failed", "finished_at_utc": iso_now(),
                    "production_writes": False, "preflight": {
                        "path": str(preflight_path), "sha256": sha256_path(preflight_path),
                    },
                }
                _create_json_exclusive(campaign_terminal, result)
                return result
            self.deadline = time.monotonic() + self.plan.max_wall_seconds
            self._event("campaign_start", pid=os.getpid())
            sessions: list[dict[str, object]] = []
            with ThreadPoolExecutor(
                max_workers=self.runtime.network_sessions,
                thread_name_prefix="local-country",
            ) as pool:
                futures = {pool.submit(self._run_session, session): session for session in self.plan.sessions}
                for future in as_completed(futures):
                    try:
                        sessions.append(future.result())
                    except Exception as exc:
                        session = futures[future]
                        sessions.append({
                            "session_id": session.session_id, "country": session.country,
                            "status": "isolated_session_failure", "error": type(exc).__name__,
                        })
            try:
                terminal_capacity_report = dict(self.capacity_reporter())
            except Exception as exc:
                terminal_capacity_report = {
                    "status": "unavailable", "launch_allowed": False,
                    "error": f"{type(exc).__name__}:{str(exc)[:300]}",
                }
            states = [
                state
                for session in sessions
                for state in session.get("states", [])
            ]
            failed = sum(
                state in {
                    "isolated_source_failure", "isolated_source_timeout",
                    "interrupted_requires_review", "not_started_campaign_deadline",
                    "not_started_capacity_blocked",
                }
                for state in states
            ) + sum(session.get("status") == "isolated_session_failure" for session in sessions)
            shortfalls = sum(state == "observed_with_shortfalls" for state in states)
            status = "terminal_observation_campaign"
            if failed:
                status = "terminal_with_isolated_failures"
            elif shortfalls:
                status = "terminal_with_source_shortfalls"
            result = {
                "schema_version": 1, "campaign_id": self.plan.campaign_id,
                "status": status, "terminal": True, "finished_at_utc": iso_now(),
                "plan_sha256": self.plan.plan_sha256, "production_writes": False,
                "verified_completion": False,
                "completion_semantics": "observation evidence only; no tracker boundary advanced",
                "terminal_capacity_report": terminal_capacity_report,
                "counts": {
                    "sessions": len(sessions), "sources": len(states),
                    "isolated_failures": failed, "observed_with_shortfalls": shortfalls,
                },
                "sessions": sorted(
                    ({"session_id": item.get("session_id"), "country": item.get("country"),
                      "status": item.get("status")} for item in sessions),
                    key=lambda item: str(item["session_id"]),
                ),
            }
            _create_json_exclusive(campaign_terminal, result)
            self._event("campaign_terminal", status=status)
            return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--automation-root", type=Path, default=AUTOMATION_ROOT)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--host-lock-path", required=True, type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--network-sessions", type=int, choices=(2, 4), default=4)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def runtime_from_args(args: argparse.Namespace) -> RuntimePaths:
    return RuntimePaths(
        python=args.python.resolve(), automation_root=args.automation_root.resolve(),
        output_root=args.output_root.resolve(), receipt_root=args.receipt_root.resolve(),
        host_lock_path=args.host_lock_path.resolve(), min_free_gb=args.min_free_gb,
        network_sessions=args.network_sessions,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = runtime_from_args(args)
        plan = load_plan(args.plan.resolve(), args.plan_sha256, runtime.automation_root)
        controller = CampaignController(plan, runtime)
        result = controller.preflight() if args.preflight_only else controller.run()
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        good = result.get("passed") is True or str(result.get("status", "")).startswith("terminal_")
        return 0 if good else 2
    except ControllerAlreadyRunning as exc:
        print(json.dumps({"status": "already_running", "error": str(exc)}, indent=2), file=sys.stderr)
        return 75
    except (OSError, PlanError, ValueError) as exc:
        print(json.dumps({"status": "invalid_or_unsafe", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
