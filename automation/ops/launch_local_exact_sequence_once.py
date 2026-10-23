#!/usr/bin/env python3
"""Launch one immutable exact-observation sequence on the local host.

This launcher is deliberately initial-attempt-only.  It validates the exact
sequence plan and all pinned evidence, applies the shared host-capacity guard,
creates one exclusive attempt marker, starts the reviewed controller in a
detached process group, and inspects startup for exactly four seconds.  It has
no resume, database, promotion, or production-write mode.

An interrupted attempt remains closed.  A later recovery must be reviewed
separately; this launcher never removes or bypasses its immutable marker.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence


OPS_ROOT = Path(__file__).resolve().parent
if str(OPS_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_ROOT))

from host_capacity_guard import collect_metrics, evaluate_capacity  # noqa: E402
from unattended_exact_observation_sequence import (  # noqa: E402
    BROWSER_CEILING,
    CHILD_ENV_ALLOWLIST,
    NETWORK_CEILING,
    OBSERVER_LOG_MAX_BYTES,
    PARENT_ARTIFACTS,
    PlanError,
    RuntimePaths,
    SequenceController,
    load_plan,
    sha256_path,
)


CONTROLLER_PATH = OPS_ROOT / "unattended_exact_observation_sequence.py"
CAPACITY_GUARD_PATH = OPS_ROOT / "host_capacity_guard.py"
LAUNCHER_PATH = Path(__file__).resolve()
UTC = timezone.utc
HEX64 = re.compile(r"^[0-9a-f]{64}$")
STARTUP_INSPECTION_SECONDS = 4.0
SCRAPE_PATTERNS = (
    "parallel_observe_pure.py",
    "multiprocess_observe_pure.py",
    "run_pure_discovery_diagnostic.py",
    "unattended_exact_observation_sequence.py",
    "local_unattended_country_observation.py",
    "unattended_country_sessions.py",
)
BROWSER_PATTERNS = (
    "chromedriver", "geckodriver", "playwright", "--headless",
    "--remote-debugging-port", "selenium",
)
RUNTIME_PROCESS_MARKERS = (
    "unattended_exact_observation_sequence.py",
    "parallel_observe_pure.py",
    "run_pure_discovery_diagnostic.py",
)


class LaunchError(RuntimeError):
    """The local exact sequence is unsafe to launch."""


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _create_json_exclusive(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = _json_bytes(value)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(path, 0o600)


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchError("invalid local runtime JSON") from exc
    if not isinstance(value, dict):
        raise LaunchError("local runtime JSON must be an object")
    return value


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _local_path(
    raw: Path, workspace_root: Path, field: str, *, kind: str,
) -> Path:
    unresolved = raw.expanduser()
    if unresolved.is_symlink():
        raise LaunchError("%s must not be a symbolic link" % field)
    path = unresolved.resolve()
    if not _is_within(path, workspace_root):
        raise LaunchError("%s must remain inside the local workspace" % field)
    if kind == "file" and (not path.is_file() or path.is_symlink()):
        raise LaunchError("%s must be a regular local file" % field)
    if kind == "directory" and (not path.is_dir() or path.is_symlink()):
        raise LaunchError("%s must be a real local directory" % field)
    if kind == "future" and (not path.parent.is_dir() or path.parent.is_symlink()):
        raise LaunchError("%s parent must be a real local directory" % field)
    return path


def _private_directory(path: Path, field: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise LaunchError("%s must not grant group or world access" % field)


def _private_file(path: Path, field: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise LaunchError("%s must not grant group or world access" % field)


def _expected_hash(path: Path, value: object, field: str) -> str:
    expected = str(value).lower()
    if (
        not HEX64.fullmatch(expected)
        or not path.is_file()
        or path.is_symlink()
        or sha256_path(path) != expected
    ):
        raise LaunchError("%s SHA-256 mismatch" % field)
    return expected


def _deployment_tree_files(root: Path) -> dict[str, Path]:
    """Return the closed regular-file set, rejecting links and open directories."""

    _private_directory(root, "deployment_root")
    result: dict[str, Path] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        _private_directory(directory, "deployment_directory")
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                candidate = Path(entry.path)
                if stat.S_ISLNK(info.st_mode):
                    raise LaunchError("deployment tree contains a symbolic link")
                if stat.S_ISDIR(info.st_mode):
                    stack.append(candidate)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise LaunchError("deployment tree contains a non-regular entry")
                relative = candidate.relative_to(root).as_posix()
                if relative in result:
                    raise LaunchError("deployment tree contains a duplicate path")
                result[relative] = candidate
    return result


def deployment_digest(rows: Sequence[Mapping[str, object]]) -> str:
    """Hash the normalized closed deployment inventory."""

    normalized: list[tuple[str, str, int, str]] = []
    for raw in rows:
        try:
            normalized.append((
                str(raw["deployment_relative_path"]),
                str(raw["sha256"]),
                int(raw["bytes"]),
                str(raw["mode"]),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise LaunchError("deployment digest row is invalid") from exc
    digest = hashlib.sha256()
    for relative, file_hash, size, mode in sorted(normalized):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(file_hash.encode("ascii") + b"\0")
        digest.update(str(size).encode("ascii") + b"\0")
        digest.update(mode.encode("ascii") + b"\0")
    return digest.hexdigest()


def verify_deployment_manifest(
    *, manifest_path: Path, expected_manifest_sha256: object,
    expected_deployment_sha256: object, workspace: Path, runtime: RuntimePaths,
    paths: Mapping[str, Path], plan: object, plan_path: Path,
    controller_sha256: str,
) -> dict[str, object]:
    manifest_path = _local_path(
        manifest_path, workspace, "deployment_manifest", kind="file",
    )
    _private_file(manifest_path, "deployment_manifest")
    manifest_sha256 = _expected_hash(
        manifest_path, expected_manifest_sha256, "deployment manifest",
    )
    deployment_sha256 = str(expected_deployment_sha256).lower()
    if not HEX64.fullmatch(deployment_sha256):
        raise LaunchError("deployment SHA-256 is invalid")
    if (
        runtime.deployment_root.name != deployment_sha256
        or manifest_path == runtime.deployment_root
        or _is_within(manifest_path, runtime.deployment_root)
    ):
        raise LaunchError("deployment digest or manifest placement is invalid")
    manifest = _load_json(manifest_path)
    expected_header = {
        "schema_version": 1,
        "kind": "private_local_exact_sequence_fallback_deployment",
        "mode": "content_addressed_local_exact_sequence_deployment",
        "sequence_id": plan.sequence_id,
        "host_class": "local_darwin",
        "transport": "local",
        "production_writes": False,
        "repository": "lin61302/scraping",
        "branch": "main",
        "code_git_commit": plan.code_git_commit,
        "network_ceiling": NETWORK_CEILING,
        "browser_ceiling": BROWSER_CEILING,
        "deployment_sha256": deployment_sha256,
        "controller_sha256": controller_sha256,
        "plan_sha256": plan.plan_sha256,
    }
    if any(manifest.get(key) != value for key, value in expected_header.items()):
        raise LaunchError("deployment manifest header or pins are invalid")
    expected_paths = {
        "deployment_root_local": runtime.deployment_root,
        "automation_root_local": runtime.automation_root,
        "controller_local": runtime.automation_root
        / "ops/unattended_exact_observation_sequence.py",
        "plan_local": plan_path,
        "python_local": runtime.python,
        "output_root_local": runtime.output_root,
        "receipt_root_local": runtime.receipt_root,
        "log_root_local": paths["log_root"],
        "lock_root_local": paths["lock_root"],
        "attempt_root_local": paths["operation_root"],
        "host_lock_local": runtime.host_lock_path,
        "attempt_marker_local": paths["attempt_marker"],
        "launch_receipt_local": paths["launch_receipt"],
        "controller_log_local": paths["controller_log"],
    }
    if any(
        str(manifest.get(key, "")) != str(value)
        for key, value in expected_paths.items()
    ):
        raise LaunchError("deployment manifest local path contract is invalid")
    allowed = manifest.get("allowed_parent_roots_local")
    if not isinstance(allowed, list) or allowed != [
        str(path) for path in runtime.allowed_parent_roots
    ]:
        raise LaunchError("deployment manifest parent-root contract is invalid")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise LaunchError("deployment manifest file closure is missing")
    actual_files = _deployment_tree_files(runtime.deployment_root)
    listed: dict[str, Mapping[str, object]] = {}
    source_paths: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise LaunchError("deployment manifest contains an invalid file row")
        relative = str(raw.get("deployment_relative_path", ""))
        relative_path = Path(relative)
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in listed
        ):
            raise LaunchError("deployment manifest contains an unsafe file path")
        source = str(raw.get("source_path", ""))
        source_relative = Path(source)
        if (
            not source
            or source_relative.is_absolute()
            or ".." in source_relative.parts
            or source_relative.as_posix() != source
            or source in source_paths
        ):
            raise LaunchError("deployment manifest contains an unsafe source path")
        source_file = (workspace / source_relative).resolve()
        if (
            not _is_within(source_file, workspace)
            or not source_file.is_file()
            or source_file.is_symlink()
            or sha256_path(source_file) != str(raw.get("sha256", ""))
        ):
            raise LaunchError("deployment manifest source mapping is invalid")
        source_paths.add(source)
        listed[relative] = raw
    if set(listed) != set(actual_files):
        raise LaunchError("deployment manifest does not close the deployed file set")
    for relative, raw in listed.items():
        file_path = actual_files[relative]
        mode = stat.S_IMODE(file_path.stat().st_mode)
        expected_mode = str(raw.get("mode", ""))
        try:
            parsed_mode = int(expected_mode, 8)
        except ValueError as exc:
            raise LaunchError("deployment manifest file mode is invalid") from exc
        try:
            expected_bytes = int(raw.get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise LaunchError("deployment manifest file size is invalid") from exc
        expected_sha256 = str(raw.get("sha256", ""))
        if (
            str(raw.get("deployed_path", "")) != str(file_path)
            or not isinstance(raw.get("source_path"), str)
            or not str(raw.get("source_path"))
            or not HEX64.fullmatch(expected_sha256)
            or sha256_path(file_path) != expected_sha256
            or expected_bytes != file_path.stat().st_size
            or expected_mode != "%04o" % mode
            or parsed_mode != mode
            or bool(raw.get("private")) != (mode & 0o077 == 0)
            or mode & 0o022
        ):
            raise LaunchError("deployment manifest file hash, size, or mode drifted")
    if deployment_digest(raw_files) != deployment_sha256:
        raise LaunchError("content-addressed deployment digest mismatch")
    return {
        "manifest_sha256": manifest_sha256,
        "deployment_sha256": deployment_sha256,
        "file_count": len(actual_files),
    }


def _command_sha256(command: Sequence[str]) -> str:
    return hashlib.sha256(
        b"\0".join(value.encode("utf-8") for value in command)
    ).hexdigest()


def local_capacity_report(runtime: RuntimePaths) -> dict[str, object]:
    metrics = collect_metrics(
        runtime.deployment_root,
        [runtime.receipt_root],
        [runtime.output_root],
        scrape_patterns=SCRAPE_PATTERNS,
        browser_patterns=BROWSER_PATTERNS,
    )
    report = dict(evaluate_capacity(metrics, include_paths=False))
    recommended = report.get("recommended_limits")
    if isinstance(recommended, Mapping):
        report["sequence_limits"] = {
            "network_workers": min(
                NETWORK_CEILING, int(recommended.get("network_workers") or 0),
            ),
            "browser_workers": 0,
        }
    return report


def _capacity_errors(report: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    if report.get("launch_allowed") is not True or report.get("status") == "blocked":
        errors.append("shared_capacity_guard_blocked")
    limits = report.get("sequence_limits")
    if not isinstance(limits, Mapping):
        errors.append("sequence_limits_missing")
    else:
        if int(limits.get("network_workers") or 0) < NETWORK_CEILING:
            errors.append("two_network_workers_unavailable")
        if int(limits.get("browser_workers") or 0) != 0:
            errors.append("browser_limit_not_zero")
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        errors.append("capacity_metrics_missing")
    else:
        if int(metrics.get("scrape_processes") or 0) != 0:
            errors.append("matching_network_process_already_running")
        if int(metrics.get("browser_processes") or 0) != 0:
            errors.append("matching_browser_process_already_running")
    return errors


def _startup_capacity_errors(report: object) -> list[str]:
    if not isinstance(report, Mapping):
        return ["startup_capacity_missing"]
    errors: list[str] = []
    if report.get("launch_allowed") is not True or report.get("status") == "blocked":
        errors.append("startup_capacity_blocked")
    limits = report.get("sequence_limits")
    if not isinstance(limits, Mapping):
        errors.append("startup_sequence_limits_missing")
    else:
        if int(limits.get("network_workers") or 0) < NETWORK_CEILING:
            errors.append("startup_two_network_workers_unavailable")
        if int(limits.get("browser_workers") or 0) != BROWSER_CEILING:
            errors.append("startup_browser_limit_not_zero")
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        errors.append("startup_capacity_metrics_missing")
    else:
        try:
            scrape_count = int(metrics.get("scrape_processes"))
            browser_count = int(metrics.get("browser_processes"))
        except (TypeError, ValueError):
            errors.append("startup_process_metrics_invalid")
        else:
            # The controller has already started when it writes this receipt,
            # so the broad scrape-process count normally includes that owned
            # process.  Ownership is established by the exact post-start
            # classifier; this gate still rejects malformed counts and every
            # browser process.
            if scrape_count < 0 or browser_count < 0:
                errors.append("startup_process_metrics_invalid")
            if browser_count != BROWSER_CEILING:
                errors.append("startup_conflicting_browser_process")
    return errors


def _post_start_capacity_errors(
    report: Mapping[str, object], process_snapshot: Mapping[str, object],
) -> list[str]:
    errors: list[str] = []
    if report.get("launch_allowed") is not True or report.get("status") == "blocked":
        errors.append("post_start_capacity_blocked")
    limits = report.get("sequence_limits")
    if not isinstance(limits, Mapping):
        errors.append("post_start_sequence_limits_missing")
    else:
        if int(limits.get("network_workers") or 0) < NETWORK_CEILING:
            errors.append("post_start_two_network_workers_unavailable")
        if int(limits.get("browser_workers") or 0) != BROWSER_CEILING:
            errors.append("post_start_browser_limit_not_zero")
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        errors.append("post_start_capacity_metrics_missing")
    else:
        try:
            scrape_count = int(metrics.get("scrape_processes"))
            browser_count = int(metrics.get("browser_processes"))
        except (TypeError, ValueError):
            errors.append("post_start_process_metrics_invalid")
        else:
            if scrape_count < 0 or browser_count < 0:
                errors.append("post_start_process_metrics_invalid")
    if process_snapshot.get("scan_ok") is not True:
        errors.append("post_start_process_scan_failed")
    if int(process_snapshot.get("foreign_network_processes") or 0) != 0:
        errors.append("post_start_foreign_network_process_detected")
    if int(process_snapshot.get("browser_processes") or 0) != BROWSER_CEILING:
        errors.append("post_start_browser_process_detected")
    return errors


def build_controller_command(
    *, python: Path, controller_path: Path, plan_path: Path,
    plan_sha256: str, runtime: RuntimePaths,
) -> list[str]:
    command = [
        str(python), str(controller_path),
        "--plan", str(plan_path),
        "--plan-sha256", plan_sha256,
        "--deployment-root", str(runtime.deployment_root),
        "--automation-root", str(runtime.automation_root),
        "--output-root", str(runtime.output_root),
        "--receipt-root", str(runtime.receipt_root),
        "--host-lock-path", str(runtime.host_lock_path),
    ]
    for parent in runtime.allowed_parent_roots:
        command.extend(("--allowed-parent-root", str(parent)))
    command.extend(("--python", str(runtime.python)))
    return command


def detached_command(
    controller_command: Sequence[str], *, use_caffeinate: bool = True,
) -> list[str]:
    caffeinate = shutil.which("caffeinate")
    if use_caffeinate and platform.system() == "Darwin" and caffeinate:
        return [caffeinate, "-i", "-s", *controller_command]
    return list(controller_command)


def controller_environment(source: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Return the small non-secret environment needed by the controller."""

    allowed = tuple(CHILD_ENV_ALLOWLIST) + (
        "LC_CTYPE", "TZ", "PYTHONUTF8", "PYTHONIOENCODING",
    )
    return {name: source[name] for name in allowed if source.get(name)}


def matching_runtime_process_groups(match_tokens: Sequence[str]) -> set[int]:
    """Find related controller/observer groups using portable ``ps`` output."""

    if not match_tokens:
        return set()
    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    ignored = {os.getpid(), os.getppid()}
    current_group = os.getpgrp()
    groups: set[int] = set()
    for raw in completed.stdout.splitlines():
        fields = raw.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, group = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        command = fields[2]
        if (
            pid not in ignored
            and group > 1
            and group != current_group
            and any(marker in command for marker in RUNTIME_PROCESS_MARKERS)
            and any(token and token in command for token in match_tokens)
        ):
            groups.add(group)
    return groups


def classify_post_start_processes(
    expected_group: int, match_tokens: Sequence[str],
) -> dict[str, object]:
    """Classify broad process matches without mistaking this launch for a peer."""

    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    ignored = {os.getpid(), os.getppid()}
    scrape_terms = tuple(value.casefold() for value in SCRAPE_PATTERNS)
    browser_terms = tuple(value.casefold() for value in BROWSER_PATTERNS)
    own_network = 0
    foreign_network = 0
    browsers = 0
    for raw in completed.stdout.splitlines():
        fields = raw.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, group = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pid in ignored:
            continue
        command = fields[2]
        folded = command.casefold()
        if any(term in folded for term in browser_terms):
            browsers += 1
        elif any(term in folded for term in scrape_terms):
            # The wrapper/controller remains in ``expected_group``.  Each
            # capped lane intentionally starts a new session, so its exact
            # output/receipt token proves ownership across its distinct PGID.
            has_token = any(token and token in command for token in match_tokens)
            is_main = "unattended_exact_observation_sequence.py" in folded
            belongs = has_token and (not is_main or group == expected_group)
            if belongs:
                own_network += 1
            else:
                foreign_network += 1
    return {
        "scan_ok": True,
        "own_network_processes": own_network,
        "foreign_network_processes": foreign_network,
        "browser_processes": browsers,
    }


def process_group_present(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(
    process: subprocess.Popen[object], match_tokens: Sequence[str] = (),
) -> dict[str, object]:
    scan_error = None
    try:
        matching_before = matching_runtime_process_groups(match_tokens)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        matching_before = set()
        scan_error = type(exc).__name__
    present_before = process_group_present(process.pid)
    groups = set(matching_before)
    if present_before:
        groups.add(process.pid)
    signals: list[str] = []
    # The wrapper may already have exited while a descendant still owns the
    # detached process group.  Probe and signal the group independently of the
    # wrapper's poll state so cleanup also works on Darwin without /proc.
    for group in sorted(groups):
        try:
            os.killpg(group, signal.SIGTERM)
            signals.append("SIGTERM:%s" % group)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(
        process_group_present(group) for group in groups
    ):
        time.sleep(0.1)
    try:
        groups.update(matching_runtime_process_groups(match_tokens))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        scan_error = scan_error or type(exc).__name__
    live_groups = {group for group in groups if process_group_present(group)}
    for group in sorted(live_groups):
        try:
            os.killpg(group, signal.SIGKILL)
            signals.append("SIGKILL:%s" % group)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(
        process_group_present(group) for group in groups
    ):
        time.sleep(0.1)
    try:
        groups.update(matching_runtime_process_groups(match_tokens))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        scan_error = scan_error or type(exc).__name__
    groups_after = {group for group in groups if process_group_present(group)}
    present_after = process.pid in groups_after
    return {
        "pid": process.pid,
        "process_group_present_before": present_before,
        "matching_process_groups_before": sorted(matching_before),
        "signals": signals,
        "process_group_present_after": present_after,
        "matching_process_groups_after": sorted(groups_after),
        "process_scan_error": scan_error,
        "exit_code": process.poll(),
        "quiescent": (
            process.poll() is not None and not groups_after and scan_error is None
        ),
    }


def process_belongs_to_group(pid: object, expected_group: int) -> bool:
    try:
        value = int(pid)
        return value > 1 and os.getpgid(value) == expected_group
    except (OSError, TypeError, ValueError):
        return False


@contextmanager
def launch_lock(path: Path) -> Iterator[None]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LaunchError("local exact-sequence launch lock is unsafe")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LaunchError("local exact-sequence launch lock is not regular")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LaunchError("local exact-sequence launch lock is held") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _inspect_startup(
    process: subprocess.Popen[object], startup_path: Path, terminal_path: Path,
) -> tuple[Optional[dict[str, object]], Optional[dict[str, object]], Optional[int]]:
    deadline = time.monotonic() + STARTUP_INSPECTION_SECONDS
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    startup = _load_json(startup_path) if startup_path.is_file() else None
    terminal = _load_json(terminal_path) if terminal_path.is_file() else None
    return startup, terminal, process.poll()


def _log_facts(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"sha256": None, "bytes": 0}
    return {"sha256": sha256_path(path), "bytes": path.stat().st_size}


def _runtime_paths(args: argparse.Namespace) -> tuple[Path, RuntimePaths, dict[str, Path]]:
    raw_workspace = args.workspace_root.expanduser()
    if raw_workspace.is_symlink():
        raise LaunchError("workspace_root must not be a symbolic link")
    workspace = raw_workspace.resolve()
    if not workspace.is_dir():
        raise LaunchError("workspace_root must be a real local directory")
    deployment = _local_path(
        args.deployment_root, workspace, "deployment_root", kind="directory",
    )
    automation = _local_path(
        args.automation_root, workspace, "automation_root", kind="directory",
    )
    if automation != (deployment / "automation").resolve():
        raise LaunchError("automation_root must be the deployed automation directory")
    output = _local_path(args.output_root, workspace, "output_root", kind="directory")
    receipt = _local_path(args.receipt_root, workspace, "receipt_root", kind="directory")
    operation = _local_path(
        args.operation_root, workspace, "operation_root", kind="directory",
    )
    controller_log = _local_path(
        args.controller_log, workspace, "controller_log", kind="future",
    )
    log_root = controller_log.parent
    for field, path in (
        ("output_root", output), ("receipt_root", receipt),
        ("operation_root", operation), ("log_root", log_root),
    ):
        _private_directory(path, field)
    host_lock = _local_path(
        args.host_lock_path, workspace, "host_lock_path", kind="future",
    )
    lock_root = host_lock.parent
    _private_directory(lock_root, "lock_root")
    parents = tuple(
        _local_path(path, workspace, "allowed_parent_root", kind="directory")
        for path in args.allowed_parent_root
    )
    if not parents:
        raise LaunchError("at least one local allowed_parent_root is required")
    for parent in parents:
        _private_directory(parent, "allowed_parent_root")
    immutable_roots = (deployment, *parents)
    writable_roots = (output, receipt, operation, log_root, lock_root)
    for roots, message in (
        (immutable_roots, "immutable roots must be distinct and non-nested"),
        (writable_roots, "runtime roots must be distinct and non-nested"),
    ):
        for index, left in enumerate(roots):
            for right in roots[index + 1:]:
                if _is_within(left, right) or _is_within(right, left):
                    raise LaunchError(message)
    for immutable in immutable_roots:
        for writable in writable_roots:
            if _is_within(immutable, writable) or _is_within(writable, immutable):
                raise LaunchError("immutable evidence and runtime roots must be disjoint")
    # Preserve the absolute executable path pinned by the immutable deployment
    # manifest (the reviewed runtime may intentionally be a stable symlink),
    # while independently proving that its resolved target is executable.
    python = Path(os.path.abspath(str(args.python.expanduser())))
    resolved_python = python.resolve()
    if not resolved_python.is_file() or not os.access(resolved_python, os.X_OK):
        raise LaunchError("python must be a local executable file")
    runtime = RuntimePaths(
        python=python,
        deployment_root=deployment,
        automation_root=automation,
        output_root=output,
        receipt_root=receipt,
        host_lock_path=host_lock,
        allowed_parent_roots=parents,
    )
    paths = {
        "operation_root": operation,
        "launch_lock": operation / "launch.lock",
        "attempt_marker": operation / "attempt_receipt.json",
        "launch_receipt": operation / "launch_receipt.json",
        "controller_log": controller_log,
        "log_root": log_root,
        "lock_root": lock_root,
    }
    return workspace, runtime, paths


def _validated_launch_lock_path(args: argparse.Namespace) -> Path:
    """Return the lock path without creating anything outside the workspace."""

    raw_workspace = args.workspace_root.expanduser()
    if raw_workspace.is_symlink():
        raise LaunchError("workspace_root must not be a symbolic link")
    workspace = raw_workspace.resolve()
    if not workspace.is_dir():
        raise LaunchError("workspace_root must be a real local directory")
    operation = _local_path(
        args.operation_root, workspace, "operation_root", kind="directory",
    )
    _private_directory(operation, "operation_root")
    return operation / "launch.lock"


def prepare(args: argparse.Namespace) -> dict[str, object]:
    workspace, runtime, paths = _runtime_paths(args)
    expected_launcher = _expected_hash(
        LAUNCHER_PATH, args.launcher_sha256, "launcher",
    )
    expected_controller = _expected_hash(
        CONTROLLER_PATH, args.controller_sha256, "workspace controller",
    )
    expected_guard = _expected_hash(
        CAPACITY_GUARD_PATH, args.capacity_guard_sha256, "workspace capacity guard",
    )
    deployed_controller = runtime.automation_root / "ops/unattended_exact_observation_sequence.py"
    deployed_guard = runtime.automation_root / "ops/host_capacity_guard.py"
    _expected_hash(deployed_controller, expected_controller, "deployed controller")
    _expected_hash(deployed_guard, expected_guard, "deployed capacity guard")
    plan_path = _local_path(args.plan, workspace, "plan", kind="file")
    if not _is_within(plan_path, runtime.deployment_root):
        raise LaunchError("plan must be inside the local deployment root")
    _private_file(plan_path, "plan")
    plan = load_plan(
        plan_path, str(args.plan_sha256).lower(), runtime.deployment_root,
        runtime.allowed_parent_roots,
    )
    if plan.log_max_bytes != OBSERVER_LOG_MAX_BYTES:
        raise LaunchError("plan child-log cap is not exactly 50 MiB")
    sequence_dir = runtime.receipt_root / plan.sequence_id
    sequence_output = runtime.output_root / plan.sequence_id
    if sequence_dir.exists() or sequence_output.exists():
        raise LaunchError("local exact sequence already has runtime artifacts")
    for name in ("attempt_marker", "launch_receipt", "controller_log"):
        if paths[name].exists():
            raise LaunchError("local single-use runtime artifact already exists")
    controller = SequenceController(plan, runtime, resume=False)
    global_pins = controller.verify_global_pins()
    lane_pins: dict[str, object] = {}
    for phase in plan.phases:
        for lane in phase.lanes:
            if lane.exact_input is not None:
                _private_file(lane.exact_input.path, "exact_input")
            if lane.parent is not None:
                _private_directory(lane.parent.run_dir, "parent_run_dir")
                for artifact_name in PARENT_ARTIFACTS:
                    _private_file(
                        lane.parent.run_dir / artifact_name,
                        "parent_artifact",
                    )
            lane_pins[lane.lane_id] = controller.verify_lane_pins(lane)
    deployment = verify_deployment_manifest(
        manifest_path=args.deployment_manifest,
        expected_manifest_sha256=args.deployment_manifest_sha256,
        expected_deployment_sha256=args.deployment_sha256,
        workspace=workspace,
        runtime=runtime,
        paths=paths,
        plan=plan,
        plan_path=plan_path,
        controller_sha256=expected_controller,
    )
    capacity = local_capacity_report(runtime)
    blockers = _capacity_errors(capacity)
    if blockers:
        raise LaunchError("local capacity gate closed: %s" % ",".join(blockers))
    controller_command = build_controller_command(
        python=runtime.python,
        controller_path=deployed_controller,
        plan_path=plan_path,
        plan_sha256=plan.plan_sha256,
        runtime=runtime,
    )
    wrapped = detached_command(
        controller_command, use_caffeinate=not args.no_caffeinate,
    )
    return {
        "workspace": workspace,
        "runtime": runtime,
        "paths": paths,
        "plan": plan,
        "plan_path": plan_path,
        "controller": controller,
        "controller_path": deployed_controller,
        "controller_sha256": expected_controller,
        "launcher_sha256": expected_launcher,
        "capacity_guard_sha256": expected_guard,
        "global_pins": global_pins,
        "lane_pins": lane_pins,
        "deployment": deployment,
        "capacity": capacity,
        "command": wrapped,
    }


def public_preflight(prepared: Mapping[str, object]) -> dict[str, object]:
    plan = prepared["plan"]
    assert hasattr(plan, "phases")
    lane_count = sum(len(phase.lanes) for phase in plan.phases)
    return {
        "schema_version": 1,
        "kind": "local_exact_sequence_preflight",
        "status": "pass",
        "sequence_id": plan.sequence_id,
        "plan_sha256": plan.plan_sha256,
        "code_git_commit": plan.code_git_commit,
        "controller_sha256": prepared["controller_sha256"],
        "launcher_sha256": prepared["launcher_sha256"],
        "capacity_guard_sha256": prepared["capacity_guard_sha256"],
        "deployment_manifest_sha256": prepared["deployment"]["manifest_sha256"],
        "deployment_sha256": prepared["deployment"]["deployment_sha256"],
        "deployment_file_count": prepared["deployment"]["file_count"],
        "phase_count": len(plan.phases),
        "lane_count": lane_count,
        "capacity": prepared["capacity"],
        "network_ceiling": NETWORK_CEILING,
        "browser_ceiling": BROWSER_CEILING,
        "log_max_bytes": OBSERVER_LOG_MAX_BYTES,
        "startup_inspection_seconds": int(STARTUP_INSPECTION_SECONDS),
        "production_writes": False,
        "resume_supported": False,
    }


def _failure_receipt(
    prepared: Mapping[str, object], error: BaseException,
    cleanup: Optional[Mapping[str, object]],
) -> dict[str, object]:
    plan = prepared["plan"]
    paths = prepared["paths"]
    return {
        "schema_version": 1,
        "kind": "local_exact_sequence_launch_terminal",
        "status": "launch_failed",
        "failed_at_utc": iso_now(),
        "sequence_id": plan.sequence_id,
        "plan_sha256": plan.plan_sha256,
        "controller_sha256": prepared["controller_sha256"],
        "launcher_sha256": prepared["launcher_sha256"],
        "capacity_guard_sha256": prepared["capacity_guard_sha256"],
        "deployment_manifest_sha256": prepared["deployment"]["manifest_sha256"],
        "deployment_sha256": prepared["deployment"]["deployment_sha256"],
        "production_writes": False,
        "error_type": type(error).__name__,
        "cleanup": cleanup,
        "controller_log": _log_facts(paths["controller_log"]),
        "automatic_resume": False,
    }


def launch(args: argparse.Namespace) -> dict[str, object]:
    launch_lock_path = _validated_launch_lock_path(args)
    # Refuse a changed launcher before creating even the reusable lock file.
    _expected_hash(LAUNCHER_PATH, args.launcher_sha256, "launcher")
    with launch_lock(launch_lock_path):
        prepared = prepare(args)
        plan = prepared["plan"]
        runtime = prepared["runtime"]
        paths = prepared["paths"]
        controller = prepared["controller"]
        command = prepared["command"]
        startup_path = runtime.receipt_root / plan.sequence_id / "startup_receipt.json"
        terminal_path = runtime.receipt_root / plan.sequence_id / "terminal_receipt.json"
        cleanup_tokens = (
            str(prepared["plan_path"]),
            str(runtime.output_root / plan.sequence_id),
            str(runtime.receipt_root / plan.sequence_id),
        )
        marker = {
            "schema_version": 1,
            "kind": "local_exact_sequence_attempt",
            "sequence_id": plan.sequence_id,
            "attempted_at_utc": iso_now(),
            "single_use": True,
            "initial_attempt_only": True,
            "automatic_resume": False,
            "plan_sha256": plan.plan_sha256,
            "code_git_commit": plan.code_git_commit,
            "controller_sha256": prepared["controller_sha256"],
            "launcher_sha256": prepared["launcher_sha256"],
            "capacity_guard_sha256": prepared["capacity_guard_sha256"],
            "deployment_manifest_sha256": prepared["deployment"]["manifest_sha256"],
            "deployment_sha256": prepared["deployment"]["deployment_sha256"],
            "command_sha256": _command_sha256(command),
            "host_capacity_guard": prepared["capacity"],
            "network_ceiling": NETWORK_CEILING,
            "browser_ceiling": BROWSER_CEILING,
            "production_writes": False,
        }
        _create_json_exclusive(paths["attempt_marker"], marker)
        process: Optional[subprocess.Popen[object]] = None
        cleanup: Optional[Mapping[str, object]] = None
        try:
            descriptor = os.open(
                paths["controller_log"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                previous_umask = os.umask(0o077)
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=str(runtime.deployment_root),
                        env=controller_environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
                finally:
                    os.umask(previous_umask)
            startup, terminal, exit_code = _inspect_startup(
                process, startup_path, terminal_path,
            )
            if startup is None:
                cleanup = terminate_process_group(process, cleanup_tokens)
                if cleanup.get("quiescent") is not True:
                    raise LaunchError("startup missing and process group did not quiesce")
                raise LaunchError("startup receipt missing after bounded inspection")
            validated_startup = controller.validate_sequence_startup(startup_path)
            startup_capacity_blockers = _startup_capacity_errors(
                validated_startup.get("host_capacity_guard"),
            )
            if startup_capacity_blockers:
                cleanup = terminate_process_group(process, cleanup_tokens)
                if cleanup.get("quiescent") is not True:
                    raise LaunchError(
                        "unsafe startup capacity and process group did not quiesce"
                    )
                raise LaunchError(
                    "startup capacity gate closed: %s"
                    % ",".join(startup_capacity_blockers)
                )
            validated_terminal = None
            if terminal is not None:
                validated_terminal = controller.validate_sequence_terminal(terminal_path)
            if (
                validated_terminal is None
                and exit_code is None
                and not process_belongs_to_group(
                    validated_startup.get("pid"), process.pid,
                )
            ):
                cleanup = terminate_process_group(process, cleanup_tokens)
                if cleanup.get("quiescent") is not True:
                    raise LaunchError(
                        "startup PID mismatch and runtime did not quiesce"
                    )
                raise LaunchError("startup PID is outside the detached process group")
            if exit_code is not None and validated_terminal is None:
                cleanup = terminate_process_group(process, cleanup_tokens)
                if cleanup.get("quiescent") is not True:
                    raise LaunchError("early exit process group did not quiesce")
                raise LaunchError("controller exited during startup without terminal")
            if validated_terminal is not None:
                expected_exit = (
                    0 if validated_terminal.get("status") == "terminal_observation_sequence"
                    else 1
                )
                if exit_code is not None and exit_code != expected_exit:
                    cleanup = terminate_process_group(process, cleanup_tokens)
                    if cleanup.get("quiescent") is not True:
                        raise LaunchError(
                            "terminal exit mismatch and runtime did not quiesce"
                        )
                    raise LaunchError("controller exit code differs from terminal state")
            post_start_capacity = local_capacity_report(runtime)
            post_start_processes = classify_post_start_processes(
                process.pid, cleanup_tokens,
            )
            post_start_blockers = _post_start_capacity_errors(
                post_start_capacity, post_start_processes,
            )
            if post_start_blockers:
                cleanup = terminate_process_group(process, cleanup_tokens)
                if cleanup.get("quiescent") is not True:
                    raise LaunchError(
                        "post-start capacity failed and runtime did not quiesce"
                    )
                raise LaunchError(
                    "post-start capacity gate closed: %s"
                    % ",".join(post_start_blockers)
                )
            status = "completed_fast" if validated_terminal is not None else "launched_unattended"
            receipt = {
                "schema_version": 1,
                "kind": "local_exact_sequence_launch_terminal",
                "status": status,
                "launched_at_utc": iso_now(),
                "sequence_id": plan.sequence_id,
                "plan_sha256": plan.plan_sha256,
                "code_git_commit": plan.code_git_commit,
                "controller_sha256": prepared["controller_sha256"],
                "launcher_sha256": prepared["launcher_sha256"],
                "capacity_guard_sha256": prepared["capacity_guard_sha256"],
                "deployment_manifest_sha256": prepared["deployment"]["manifest_sha256"],
                "deployment_sha256": prepared["deployment"]["deployment_sha256"],
                "attempt_marker_sha256": sha256_path(paths["attempt_marker"]),
                "startup_receipt_sha256": sha256_path(startup_path),
                "terminal_receipt_sha256": (
                    sha256_path(terminal_path) if validated_terminal is not None else None
                ),
                "controller_log": _log_facts(paths["controller_log"]),
                "post_start_capacity": post_start_capacity,
                "post_start_processes": post_start_processes,
                "detached_pid": process.pid,
                "controller_pid": startup.get("pid"),
                "startup_inspection_seconds": int(STARTUP_INSPECTION_SECONDS),
                "network_ceiling": NETWORK_CEILING,
                "browser_ceiling": BROWSER_CEILING,
                "production_writes": False,
                "automatic_resume": False,
            }
            _create_json_exclusive(paths["launch_receipt"], receipt)
            return receipt
        except BaseException as exc:
            if process is not None and cleanup is None:
                cleanup = terminate_process_group(process, cleanup_tokens)
            failure = _failure_receipt(prepared, exc, cleanup)
            if not paths["launch_receipt"].exists():
                _create_json_exclusive(paths["launch_receipt"], failure)
            if cleanup is not None and cleanup.get("quiescent") is not True:
                raise LaunchError("launch failed and process group did not quiesce") from exc
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--deployment-manifest", required=True, type=Path)
    parser.add_argument("--deployment-manifest-sha256", required=True)
    parser.add_argument("--deployment-sha256", required=True)
    parser.add_argument("--controller-sha256", required=True)
    parser.add_argument("--launcher-sha256", required=True)
    parser.add_argument("--capacity-guard-sha256", required=True)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--deployment-root", required=True, type=Path)
    parser.add_argument("--automation-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--host-lock-path", required=True, type=Path)
    parser.add_argument("--allowed-parent-root", action="append", required=True, type=Path)
    parser.add_argument("--operation-root", required=True, type=Path)
    parser.add_argument("--controller-log", required=True, type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--no-caffeinate", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.preflight_only:
            print(json.dumps(public_preflight(prepare(args)), indent=2, sort_keys=True))
            return 0
        print(json.dumps(launch(args), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, PlanError, LaunchError, subprocess.SubprocessError) as exc:
        print(json.dumps({
            "status": "refused", "error": type(exc).__name__,
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
