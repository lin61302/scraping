#!/usr/bin/env python3
"""Launch one hash-pinned local observation campaign and return after startup.

The launcher is intentionally single use.  It performs a local plan preflight,
takes a macOS-compatible process/capacity snapshot, creates an exclusive
attempt marker, and starts the controller in a detached process group.  It
waits only for the immutable startup receipt (or a fast terminal receipt),
records the first log lines, and exits.  It never waits for source completion
and it has no database or promotion mode.
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
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence


OPS_ROOT = Path(__file__).resolve().parent
if str(OPS_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_ROOT))

from local_unattended_country_observation import (  # noqa: E402
    CampaignController,
    PlanError,
    RuntimePaths,
    host_capacity_report,
    load_plan,
    sha256_path,
)


CONTROLLER_PATH = OPS_ROOT / "local_unattended_country_observation.py"
CAPACITY_GUARD_PATH = OPS_ROOT / "host_capacity_guard.py"
LAUNCHER_PATH = Path(__file__).resolve()
UTC = timezone.utc
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class LaunchError(RuntimeError):
    """A fresh detached launch is unsafe or could not be confirmed."""


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _create_json_exclusive(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LaunchError(f"expected a JSON object: {path}")
    return value


def parse_ps_snapshot(text: str, *, ignored_pids: Sequence[int] = ()) -> dict[str, object]:
    """Parse ``ps -axo`` output without Linux ``/proc`` assumptions."""

    ignored = set(ignored_pids)
    rows: list[dict[str, object]] = []
    for raw in text.splitlines():
        fields = raw.strip().split(None, 4)
        if len(fields) < 5:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pid in ignored:
            continue
        row = {
            "pid": pid, "ppid": ppid, "stat": fields[2],
            "elapsed": fields[3], "command": fields[4],
        }
        rows.append(row)

    def matching(predicate) -> list[dict[str, object]]:
        return [row for row in rows if predicate(str(row["command"]))]

    controllers = matching(
        lambda command: "local_unattended_country_observation.py" in command
        and "launch_local_country_observation_once.py" not in command
    )
    observers = matching(lambda command: "parallel_observe_pure.py" in command)
    browsers = matching(
        lambda command: any(
            marker in command.lower()
            for marker in ("chromedriver", "geckodriver", "playwright run-driver")
        )
    )
    return {
        "process_count": len(rows),
        "local_controllers": controllers,
        "network_observers": observers,
        "browser_workers": browsers,
    }


def _mac_memory_snapshot() -> dict[str, object]:
    result: dict[str, object] = {"total_bytes": None, "available_bytes": None}
    try:
        total = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            timeout=5, check=True,
        ).stdout.strip()
        result["total_bytes"] = int(total)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        output = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        page_match = re.search(r"page size of (\d+) bytes", output)
        page_size = int(page_match.group(1)) if page_match else 4096
        pages: dict[str, int] = {}
        for line in output.splitlines():
            match = re.match(r"([^:]+):\s+([0-9]+)\.?$", line.strip())
            if match:
                pages[match.group(1)] = int(match.group(2))
        available_pages = sum(
            pages.get(name, 0)
            for name in ("Pages free", "Pages inactive", "Pages speculative")
        )
        result["available_bytes"] = available_pages * page_size
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def capacity_snapshot(storage_probe: Path) -> dict[str, object]:
    checked = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,stat=,etime=,command="],
        capture_output=True, text=True, timeout=10, check=True,
    )
    processes = parse_ps_snapshot(checked.stdout, ignored_pids=(os.getpid(), os.getppid()))
    probe = storage_probe.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    disk = shutil.disk_usage(probe)
    try:
        loads: list[Optional[float]] = [round(value, 3) for value in os.getloadavg()]
    except OSError:
        loads = [None, None, None]
    memory = _mac_memory_snapshot() if platform.system() == "Darwin" else {
        "total_bytes": None, "available_bytes": None,
    }
    return {
        "checked_at_utc": iso_now(), "platform": platform.platform(),
        "cpu_count": os.cpu_count(), "load_averages": loads,
        "storage": {"probe_path": str(probe), "free_bytes": disk.free},
        "memory": memory, "processes": processes,
    }


def capacity_errors(snapshot: Mapping[str, object], min_free_gb: float) -> list[str]:
    errors: list[str] = []
    processes = snapshot.get("processes")
    storage = snapshot.get("storage")
    if not isinstance(processes, Mapping) or not isinstance(storage, Mapping):
        return ["capacity_snapshot_incomplete"]
    if processes.get("local_controllers"):
        errors.append("local_observation_controller_already_running")
    if processes.get("network_observers"):
        errors.append("network_observer_already_running")
    if processes.get("browser_workers"):
        errors.append("browser_worker_already_running")
    if int(storage.get("free_bytes") or 0) < int(min_free_gb * 1024**3):
        errors.append("insufficient_free_space")
    loads = snapshot.get("load_averages")
    cpus = int(snapshot.get("cpu_count") or 4)
    if isinstance(loads, list) and loads and loads[0] is not None:
        if float(loads[0]) >= max(8.0, cpus * 0.9):
            errors.append("load_average_too_high")
    return errors


def build_controller_command(
    *, python: Path, plan_path: Path, plan_sha256: str, runtime: RuntimePaths,
    min_free_gb: float,
) -> list[str]:
    return [
        str(python), str(CONTROLLER_PATH),
        "--plan", str(plan_path), "--plan-sha256", plan_sha256,
        "--automation-root", str(runtime.automation_root),
        "--output-root", str(runtime.output_root),
        "--receipt-root", str(runtime.receipt_root),
        "--host-lock-path", str(runtime.host_lock_path),
        "--python", str(runtime.python), "--min-free-gb", str(min_free_gb),
        "--network-sessions", str(runtime.network_sessions),
    ]


def detached_command(controller_command: Sequence[str], *, use_caffeinate: bool = True) -> list[str]:
    caffeinate = shutil.which("caffeinate")
    if use_caffeinate and platform.system() == "Darwin" and caffeinate:
        return [caffeinate, "-i", "-s", *controller_command]
    return list(controller_command)


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


@contextmanager
def launch_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LaunchError(f"local launch lock is held: {path}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _first_log_lines(path: Path, maximum_lines: int = 40, maximum_chars: int = 16_000) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    output: list[str] = []
    size = 0
    for line in lines[:maximum_lines]:
        size += len(line) + 1
        if size > maximum_chars:
            break
        output.append(line)
    return output


def launch(args: argparse.Namespace) -> dict[str, object]:
    expected_launcher = str(args.launcher_sha256).lower()
    if not HEX64.fullmatch(expected_launcher) or sha256_path(LAUNCHER_PATH) != expected_launcher:
        raise LaunchError("launcher is missing or its SHA-256 changed")
    expected_guard = str(args.capacity_guard_sha256).lower()
    if not HEX64.fullmatch(expected_guard) or sha256_path(CAPACITY_GUARD_PATH) != expected_guard:
        raise LaunchError("capacity guard is missing or its SHA-256 changed")
    expected_controller = str(args.controller_sha256).lower()
    if not HEX64.fullmatch(expected_controller) or sha256_path(CONTROLLER_PATH) != expected_controller:
        raise LaunchError("controller is missing or its SHA-256 changed")
    runtime = RuntimePaths(
        python=args.python.resolve(), automation_root=args.automation_root.resolve(),
        output_root=args.output_root.resolve(), receipt_root=args.receipt_root.resolve(),
        host_lock_path=args.host_lock_path.resolve(), min_free_gb=args.min_free_gb,
    )
    plan_path = args.plan.resolve()
    plan = load_plan(plan_path, args.plan_sha256, runtime.automation_root)
    startup_path = runtime.receipt_root / plan.campaign_id / "startup_receipt.json"
    terminal_path = runtime.receipt_root / plan.campaign_id / "terminal_receipt.json"

    with launch_lock(args.launch_lock_path.resolve()):
        if args.attempt_marker.exists():
            raise LaunchError(f"attempt marker already exists: {args.attempt_marker}")
        if args.launch_receipt.exists():
            raise LaunchError(f"launch receipt already exists: {args.launch_receipt}")
        if args.controller_log.exists():
            raise LaunchError(f"controller log already exists: {args.controller_log}")
        if startup_path.exists() or terminal_path.exists():
            raise LaunchError("campaign already has a startup or terminal receipt")
        guard = host_capacity_report(runtime)
        guard_limits = guard.get("recommended_limits")
        guard_network = (
            int(guard_limits.get("network_workers") or 0)
            if isinstance(guard_limits, Mapping) else 0
        )
        if guard.get("launch_allowed") is not True or guard.get("status") == "blocked":
            raise LaunchError(f"shared host capacity guard blocked launch: {guard.get('reasons')}")
        if guard_network not in {2, 4}:
            raise LaunchError(f"shared host capacity guard returned unsafe network limit: {guard_network}")
        runtime = replace(runtime, network_sessions=guard_network)
        controller = CampaignController(plan, runtime)
        preflight = controller.preflight()
        if preflight.get("passed") is not True:
            raise LaunchError(f"controller preflight failed: {preflight.get('errors')}")
        command = build_controller_command(
            python=runtime.python, plan_path=plan_path, plan_sha256=plan.plan_sha256,
            runtime=runtime, min_free_gb=args.min_free_gb,
        )
        wrapped = detached_command(command, use_caffeinate=not args.no_caffeinate)
        snapshot = capacity_snapshot(runtime.output_root)
        blockers = capacity_errors(snapshot, args.min_free_gb)
        if blockers:
            raise LaunchError(f"capacity gate closed: {blockers}")
        marker = {
            "schema_version": 1, "campaign_id": plan.campaign_id,
            "attempted_at_utc": iso_now(), "single_use": True,
            "production_writes": False, "plan_sha256": plan.plan_sha256,
            "controller_sha256": expected_controller,
            "launcher_sha256": expected_launcher,
            "capacity_guard_sha256": expected_guard,
            "host_capacity_guard": guard,
            "network_sessions": runtime.network_sessions,
            "capacity_snapshot_sha256": hashlib.sha256(
                json.dumps(snapshot, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        _create_json_exclusive(args.attempt_marker.resolve(), marker)
        args.controller_log.parent.mkdir(parents=True, exist_ok=True)
        process: Optional[subprocess.Popen[object]] = None
        try:
            with args.controller_log.open("x", encoding="utf-8") as stream:
                process = subprocess.Popen(
                    wrapped, cwd=str(runtime.automation_root.parent),
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            deadline = time.monotonic() + args.startup_timeout_seconds
            while time.monotonic() < deadline:
                if startup_path.is_file() or terminal_path.is_file():
                    break
                code = process.poll()
                if code is not None:
                    break
                time.sleep(0.1)
            startup = _load_json(startup_path) if startup_path.is_file() else None
            terminal = _load_json(terminal_path) if terminal_path.is_file() else None
            code = process.poll()
            if startup is None:
                terminate_process_group(process)
                raise LaunchError(
                    f"startup receipt was not created; detached exit={code}"
                )
            if (
                startup.get("campaign_id") != plan.campaign_id
                or startup.get("plan_sha256") != plan.plan_sha256
                or startup.get("production_writes") is not False
                or startup.get("capacity") != {
                    "country_sessions": runtime.network_sessions,
                    "network": runtime.network_sessions,
                    "browser": 0, "plan_network_ceiling": 4,
                }
            ):
                terminate_process_group(process)
                raise LaunchError("startup receipt violates the reviewed launch contract")
            if code not in (None, 0):
                raise LaunchError(f"controller exited during startup with code {code}")
            if code == 0 and terminal is None:
                raise LaunchError("controller exited without a terminal receipt")
            result: dict[str, object] = {
                "schema_version": 1, "campaign_id": plan.campaign_id,
                "status": "completed_fast" if terminal is not None else "launched_unattended",
                "launched_at_utc": iso_now(), "production_writes": False,
                "plan_sha256": plan.plan_sha256,
                "controller_sha256": expected_controller,
                "launcher_sha256": expected_launcher,
                "capacity_guard_sha256": expected_guard,
                "host_capacity_guard": guard,
                "network_sessions": runtime.network_sessions,
                "attempt_marker": {
                    "path": str(args.attempt_marker.resolve()),
                    "sha256": sha256_path(args.attempt_marker.resolve()),
                },
                "detached_pid": process.pid,
                "controller_pid": startup.get("pid"),
                "command": wrapped,
                "capacity_snapshot": snapshot,
                "preflight": preflight,
                "startup_receipt": {
                    "path": str(startup_path), "sha256": sha256_path(startup_path),
                },
                "controller_log": {
                    "path": str(args.controller_log.resolve()),
                    "startup_lines": _first_log_lines(args.controller_log.resolve()),
                },
            }
            if terminal is not None:
                result["terminal_receipt"] = {
                    "path": str(terminal_path), "sha256": sha256_path(terminal_path),
                }
            _create_json_exclusive(args.launch_receipt.resolve(), result)
            return result
        except Exception as exc:
            if process is not None:
                terminate_process_group(process)
            failure = {
                "schema_version": 1, "campaign_id": plan.campaign_id,
                "status": "launch_failed", "failed_at_utc": iso_now(),
                "production_writes": False, "plan_sha256": plan.plan_sha256,
                "controller_sha256": expected_controller,
                "launcher_sha256": expected_launcher,
                "capacity_guard_sha256": expected_guard,
                "host_capacity_guard": guard,
                "network_sessions": runtime.network_sessions,
                "attempt_marker": str(args.attempt_marker.resolve()),
                "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                "capacity_snapshot": snapshot,
                "controller_log": {
                    "path": str(args.controller_log.resolve()),
                    "startup_lines": _first_log_lines(args.controller_log.resolve()),
                },
            }
            if not args.launch_receipt.exists():
                _create_json_exclusive(args.launch_receipt.resolve(), failure)
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--controller-sha256", required=True)
    parser.add_argument("--launcher-sha256", required=True)
    parser.add_argument("--capacity-guard-sha256", required=True)
    parser.add_argument("--automation-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--receipt-root", required=True, type=Path)
    parser.add_argument("--host-lock-path", required=True, type=Path)
    parser.add_argument("--launch-lock-path", required=True, type=Path)
    parser.add_argument("--attempt-marker", required=True, type=Path)
    parser.add_argument("--launch-receipt", required=True, type=Path)
    parser.add_argument("--controller-log", required=True, type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--startup-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--no-caffeinate", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.startup_timeout_seconds <= 0 or args.min_free_gb < 0:
        print(json.dumps({"status": "invalid_arguments"}), file=sys.stderr)
        return 2
    try:
        result = launch(args)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, PlanError, LaunchError, subprocess.SubprocessError) as exc:
        print(json.dumps({
            "status": "launch_failed", "error": f"{type(exc).__name__}: {exc}",
        }, indent=2, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
