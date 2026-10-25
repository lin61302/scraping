#!/usr/bin/env python3
"""Take one bounded, read-only review snapshot of a local exact sequence.

The reviewer never starts, stops, signals, or resumes work.  Its receipt
allowlist is derived from the pinned plan, it takes one process snapshot, and
it records only hashes and bounded accounting.  Observation payloads and logs
are hashed in place only after a sequence terminal receipt is present.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import sys
from typing import Any, Dict, Iterable, Mapping, Sequence

try:
    from . import host_capacity_guard as capacity_guard
except ImportError:  # direct execution
    import host_capacity_guard as capacity_guard


UTC = timezone.utc
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_REPORT_TEXT = re.compile(r"^[A-Za-z0-9 ._:+-]{0,256}$")
SAFE_COUNTER_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
EXACT_ARTIFACTS = frozenset({
    "manifest.json", "urls.jsonl", "articles.jsonl", "field_checks.jsonl",
    "issues.jsonl", "errors.jsonl", "summary.md",
})
DIAGNOSTIC_ARTIFACTS = frozenset({"diagnostic_manifest.private.json"})
ALLOWED_STATES = frozenset({
    "observed_acceptance_passed", "isolated_lane_failure",
    "isolated_lane_timeout", "observed_contract_shortfall",
    "not_started_capacity_blocked", "stale_start_without_terminal_isolated",
})
SCRAPE_PATTERNS = (
    "parallel_observe_pure.py", "multiprocess_observe_pure.py",
    "run_pure_discovery_diagnostic.py", "unattended_exact_observation_sequence.py",
    "local_unattended_country_observation.py", "unattended_country_sessions.py",
)
BROWSER_PATTERNS = (
    "chromedriver", "geckodriver", "playwright", "--headless",
    "--remote-debugging-port", "selenium",
)


class ReviewError(RuntimeError):
    """The requested snapshot is unsafe, unstable, or inconsistent."""


def _reject_symlink_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ReviewError("path_component_unavailable:%s" % current.name) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ReviewError("symlink_path_component:%s" % current.name)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_object(payload: bytes) -> Dict[str, Any]:
    def pairs(values: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ReviewError("duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ReviewError("json_object_required")
    return value


def _stable_read(path: Path, *, maximum: int = 256 * 1024 * 1024) -> tuple[bytes, os.stat_result]:
    """Read one regular file without following links and reject concurrent drift."""
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReviewError("safe_open_failed:%s" % path.name) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > maximum:
            raise ReviewError("unsafe_or_oversized_file:%s" % path.name)
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if identity_before != identity_after or len(payload) != before.st_size:
            raise ReviewError("file_changed_during_read:%s" % path.name)
        return payload, after
    finally:
        os.close(descriptor)


def _stable_json(path: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    payload, info = _stable_read(path, maximum=16 * 1024 * 1024)
    return _json_object(payload), {
        "sha256": _sha(payload),
        "bytes": len(payload),
        "mtime_utc": datetime.fromtimestamp(info.st_mtime, UTC).isoformat().replace("+00:00", "Z"),
        "mtime_ns": info.st_mtime_ns,
    }


def _stable_hash(path: Path) -> Dict[str, Any]:
    payload, info = _stable_read(path)
    return {"sha256": _sha(payload), "bytes": len(payload), "mtime_ns": info.st_mtime_ns}


def _receipt_stat(path: Path) -> os.stat_result:
    """Take the sole enumeration stat for an allowlisted receipt."""
    return os.stat(path, follow_symlinks=False)


def _safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")[:80]
    return "%s-%s" % (clean, _sha(value.encode("utf-8"))[:10])


def _parse_cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewError("capture_cutoff_must_be_iso8601") from exc
    if parsed.tzinfo is None:
        raise ReviewError("capture_cutoff_requires_timezone")
    return parsed.astimezone(UTC)


def _within(path: Path, root: Path) -> bool:
    candidate = path.resolve(strict=False)
    base = root.resolve(strict=False)
    return candidate == base or base in candidate.parents


def _paths_overlap(first: Path, second: Path) -> bool:
    return _within(first, second) or _within(second, first)


def _load_plan(path: Path, expected_sha: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if not HEX64.fullmatch(expected_sha):
        raise ReviewError("invalid_plan_sha256")
    plan, facts = _stable_json(path)
    if facts["sha256"] != expected_sha:
        raise ReviewError("plan_sha256_mismatch")
    if (
        plan.get("schema_version") != 1
        or plan.get("mode") != "unattended_exact_observation_sequence"
        or plan.get("production_writes") is not False
    ):
        raise ReviewError("unsafe_plan_header")
    sequence_id = str(plan.get("sequence_id", ""))
    if not SAFE_ID.fullmatch(sequence_id):
        raise ReviewError("invalid_sequence_id")
    phases = plan.get("phases")
    if not isinstance(phases, list) or len(phases) != 3:
        raise ReviewError("exact_three_phases_required")
    lane_count = 0
    seen_phases: set[str] = set()
    seen_lanes: set[str] = set()
    for phase in phases:
        if not isinstance(phase, dict) or phase.get("production_writes") is not False:
            raise ReviewError("unsafe_phase")
        phase_id = str(phase.get("phase_id", ""))
        lanes = phase.get("lanes")
        if not SAFE_ID.fullmatch(phase_id) or phase_id in seen_phases or not isinstance(lanes, list):
            raise ReviewError("invalid_phase")
        seen_phases.add(phase_id)
        for lane in lanes:
            if not isinstance(lane, dict) or lane.get("production_writes") is not False:
                raise ReviewError("unsafe_lane")
            lane_id = str(lane.get("lane_id", ""))
            kind = str(lane.get("kind", ""))
            source_id = str(lane.get("source_id", ""))
            if (
                not SAFE_ID.fullmatch(lane_id) or lane_id in seen_lanes
                or kind not in {"exact_urls", "discovery_diagnostic"}
                or not source_id.startswith("mlp:")
            ):
                raise ReviewError("invalid_lane")
            seen_lanes.add(lane_id)
            lane_count += 1
    if lane_count != 9:
        raise ReviewError("exact_nine_lanes_required")
    return plan, facts


def _receipt_allowlist(plan: Mapping[str, Any]) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    allowed: Dict[str, Dict[str, Any]] = {
        "attempt_receipt.json": {"level": "sequence", "kind": "attempt"},
        "startup_receipt.json": {"level": "sequence", "kind": "startup"},
        "terminal_receipt.json": {"level": "sequence", "kind": "terminal"},
    }
    phases: Dict[str, Any] = {}
    lanes: Dict[str, Any] = {}
    for phase in plan["phases"]:
        phase_id = phase["phase_id"]
        phase_rel = "phases/%s/terminal_receipt.json" % _safe_name(phase_id)
        allowed[phase_rel] = {"level": "phase", "phase_id": phase_id, "kind": "terminal"}
        phases[phase_id] = {"relative_path": phase_rel, "lanes": []}
        for lane in phase["lanes"]:
            lane_id = lane["lane_id"]
            base = "lanes/%s" % _safe_name(lane_id)
            allowed[base + "/start_receipt.json"] = {
                "level": "lane", "phase_id": phase_id, "lane_id": lane_id, "kind": "start",
            }
            terminal_rel = base + "/terminal_receipt.json"
            allowed[terminal_rel] = {
                "level": "lane", "phase_id": phase_id, "lane_id": lane_id, "kind": "terminal",
            }
            lane_info = dict(lane)
            lane_info["terminal_relative_path"] = terminal_rel
            lane_info["start_relative_path"] = base + "/start_receipt.json"
            lanes[lane_id] = lane_info
            phases[phase_id]["lanes"].append(lane_id)
    return allowed, phases, lanes


def _run_ps() -> list[Dict[str, Any]]:
    executable = "/bin/ps" if Path("/bin/ps").exists() else "/usr/bin/ps"
    result = subprocess.run(
        [executable, "-axo", "pid=,ppid=,pgid=,command="],
        capture_output=True, text=True, timeout=5, check=False,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if result.returncode != 0:
        raise ReviewError("ps_snapshot_failed")
    rows = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", line)
        if match:
            rows.append({
                "pid": int(match.group(1)), "ppid": int(match.group(2)),
                "pgid": int(match.group(3)), "command": match.group(4),
            })
    return rows


def _matches(command: str, patterns: Iterable[str]) -> bool:
    folded = command.casefold()
    return any(pattern.casefold() in folded for pattern in patterns)


def _review_process_family(process_rows: Sequence[Mapping[str, Any]]) -> set[int]:
    """Return the reviewer PID and captured ancestors so wrapper shells are ignored."""
    parents = {int(row["pid"]): int(row["ppid"]) for row in process_rows}
    family = {os.getpid()}
    current = os.getpid()
    while current in parents:
        parent = parents[current]
        if parent <= 1 or parent in family:
            break
        family.add(parent)
        current = parent
    return family


def _capacity_snapshot(
    filesystem_path: Path, receipt_root: Path, output_root: Path,
    process_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    errors: list[str] = []
    try:
        disk = shutil.disk_usage(filesystem_path)
        fs_total, fs_free = disk.total, disk.free
    except OSError:
        fs_total = fs_free = None
        errors.append("filesystem_stat_failed")
    try:
        if sys.platform.startswith("linux"):
            mem_total, mem_available, swap_total, swap_used = capacity_guard._read_linux_memory()
        elif sys.platform == "darwin":
            mem_total, mem_available, swap_total, swap_used = capacity_guard._read_macos_memory()
        else:
            raise RuntimeError("unsupported_platform")
    except Exception:
        mem_total = mem_available = swap_total = swap_used = None
        errors.append("memory_or_swap_collection_failed")
    try:
        load5 = float(os.getloadavg()[1])
    except (AttributeError, OSError):
        load5 = None
        errors.append("load_collection_failed")
    cpu_count = os.cpu_count()
    if cpu_count is None:
        errors.append("cpu_count_collection_failed")
    reviewer_family = _review_process_family(process_rows)
    browser_count = sum(
        1 for row in process_rows
        if int(row["pid"]) not in reviewer_family
        and _matches(str(row["command"]), BROWSER_PATTERNS)
    )
    scrape_count = sum(
        1 for row in process_rows
        if int(row["pid"]) not in reviewer_family
        and not _matches(str(row["command"]), BROWSER_PATTERNS)
        and _matches(str(row["command"]), SCRAPE_PATTERNS)
    )
    scan = capacity_guard.scan_roots([receipt_root], [output_root])
    errors.extend(scan.errors)
    try:
        fd_soft = int(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    except (OSError, ValueError):
        fd_soft = None
        errors.append("fd_limit_collection_failed")
    metrics = capacity_guard.HostMetrics(
        filesystem_total_bytes=fs_total, filesystem_free_bytes=fs_free,
        memory_total_bytes=mem_total, memory_available_bytes=mem_available,
        load5=load5, cpu_count=cpu_count, swap_total_bytes=swap_total,
        swap_used_bytes=swap_used, scrape_processes=scrape_count,
        browser_processes=browser_count, max_log_bytes=scan.max_log_bytes,
        configured_roots_total_bytes=scan.total_bytes, fd_soft_limit=fd_soft,
        configured_log_roots=1, configured_output_roots=1,
        inspected_roots=scan.inspected_roots,
        collection_errors=tuple(sorted(set(errors))),
        collection_warnings=scan.warnings,
    )
    return dict(capacity_guard.evaluate_capacity(metrics, include_paths=False))


def _process_facts(
    rows: Sequence[Mapping[str, Any]], launch_pid: int, sequence_id: str,
    controller_path: Path, plan_path: Path, plan_sha: str,
) -> Dict[str, Any]:
    reviewer_family = _review_process_family(rows)
    relevant = [
        row for row in rows
        if int(row["pid"]) not in reviewer_family and sequence_id in str(row["command"])
    ]
    launch = next((row for row in relevant if int(row["pid"]) == launch_pid), None)
    exact = False
    if launch is not None:
        command = str(launch["command"])
        exact = all(token in command for token in (
            str(controller_path), "--plan", str(plan_path),
            "--plan-sha256", plan_sha, sequence_id,
        ))
    children = [
        row for row in relevant
        if int(row["pid"]) != launch_pid
        and int(row["ppid"]) == launch_pid
        and _matches(str(row["command"]), (
            "parallel_observe_pure.py", "run_pure_discovery_diagnostic.py",
        ))
        and not _matches(str(row["command"]), BROWSER_PATTERNS)
    ]
    sequence_browsers = [
        row for row in relevant if _matches(str(row["command"]), BROWSER_PATTERNS)
    ]
    recognized_pids = ({launch_pid} if exact else set()) | {
        int(row["pid"]) for row in children
    }
    foreign = [row for row in relevant if int(row["pid"]) not in recognized_pids]
    return {
        "launch_pid": launch_pid,
        "launch_pid_present": launch is not None,
        "launch_controller_exact_match": exact,
        "sequence_process_count": len(relevant),
        "sequence_controller_count": 1 if exact else 0,
        "sequence_child_count": len(children),
        "sequence_browser_count": len(sequence_browsers),
        "foreign_sequence_process_count": len(foreign),
        "scrape_process_count": sum(
            1 for row in rows if int(row["pid"]) not in reviewer_family
            and not _matches(str(row["command"]), BROWSER_PATTERNS)
            and _matches(str(row["command"]), SCRAPE_PATTERNS)
        ),
        "browser_process_count": sum(
            1 for row in rows if int(row["pid"]) not in reviewer_family
            and _matches(str(row["command"]), BROWSER_PATTERNS)
        ),
    }


def _receipt_summary(relative: str, value: Mapping[str, Any], facts: Mapping[str, Any], *, dependency: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "relative_path": relative, "sha256": facts["sha256"], "bytes": facts["bytes"],
        "mtime_utc": facts["mtime_utc"], "chain_dependency": dependency,
    }
    for key in (
        "kind", "terminal", "status", "state", "sequence_id", "phase_id",
        "lane_id", "source_id", "production_writes", "verified_completion",
        "tracker_boundary_advancement", "finished_at_utc", "started_at_utc",
        "state_counts", "exact_identity_accounting", "remaining_failure_classes",
        "acceptance_checks", "timed_out", "exit_code", "contract_error",
    ):
        if key in value:
            if key == "contract_error":
                result["contract_error_present"] = value[key] is not None
            elif key in {
                "state_counts", "exact_identity_accounting",
                "remaining_failure_classes", "acceptance_checks",
            }:
                result[key] = _safe_counter_tree(value[key])
            elif isinstance(value[key], str):
                text = value[key]
                if not SAFE_REPORT_TEXT.fullmatch(text) or "://" in text:
                    raise ReviewError("unsafe_receipt_summary_text:%s" % key)
                result[key] = text
            else:
                result[key] = value[key]
    return result


def _safe_counter_tree(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ReviewError("receipt_summary_tree_too_deep")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, dict) and len(value) <= 256:
        result: Dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not SAFE_COUNTER_KEY.fullmatch(key):
                raise ReviewError("unsafe_receipt_summary_key")
            result[key] = _safe_counter_tree(child, depth=depth + 1)
        return result
    raise ReviewError("unsafe_receipt_summary_value")


def _validate_terminal_chain(
    plan: Mapping[str, Any], plan_sha: str, receipt_dir: Path,
    phases: Mapping[str, Any], lanes: Mapping[str, Any], cutoff_ns: int,
    launch_pid: int,
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    summaries: list[Dict[str, Any]] = []
    loaded: Dict[str, tuple[Dict[str, Any], Dict[str, Any]]] = {}

    def read(relative: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        if relative not in loaded:
            loaded[relative] = _stable_json(receipt_dir / relative)
        return loaded[relative]

    sequence, sequence_facts = read("terminal_receipt.json")
    expected_sequence = {
        "schema_version": 1, "kind": "exact_observation_sequence_terminal",
        "terminal": True, "sequence_id": plan["sequence_id"],
        "plan_sha256": plan_sha, "lane_count": 9, "production_writes": False,
        "verified_completion": False, "tracker_boundary_advancement": False,
    }
    if any(sequence.get(key) != value for key, value in expected_sequence.items()):
        raise ReviewError("invalid_sequence_terminal")

    phase_hashes: Dict[str, str] = {}
    lane_hashes: Dict[str, str] = {}
    state_counts: Dict[str, int] = {}
    artifact_receipts: Dict[str, Mapping[str, Any]] = {}
    for phase_id, phase_info in phases.items():
        relative = str(phase_info["relative_path"])
        value, facts = read(relative)
        expected = {
            "schema_version": 1, "kind": "exact_observation_phase_terminal",
            "terminal": True, "sequence_id": plan["sequence_id"], "phase_id": phase_id,
            "plan_sha256": plan_sha, "lane_count": len(phase_info["lanes"]),
            "production_writes": False, "verified_completion": False,
            "tracker_boundary_advancement": False,
        }
        if any(value.get(key) != wanted for key, wanted in expected.items()):
            raise ReviewError("invalid_phase_terminal:%s" % phase_id)
        phase_lane_hashes: Dict[str, str] = {}
        phase_counts: Dict[str, int] = {}
        for lane_id in phase_info["lanes"]:
            lane = lanes[lane_id]
            lane_relative = str(lane["terminal_relative_path"])
            lane_value, lane_facts = read(lane_relative)
            lane_expected = {
                "schema_version": 1, "kind": "exact_observation_lane_terminal",
                "terminal": True, "sequence_id": plan["sequence_id"],
                "phase_id": phase_id, "lane_id": lane_id,
                "source_id": lane["source_id"], "plan_sha256": plan_sha,
                "production_writes": False, "verified_completion": False,
                "tracker_boundary_advancement": False,
            }
            if any(lane_value.get(key) != wanted for key, wanted in lane_expected.items()):
                raise ReviewError("invalid_lane_terminal:%s" % lane_id)
            state = str(lane_value.get("state", ""))
            proof = lane_value.get("process_quiescence_proof")
            if state not in ALLOWED_STATES or not isinstance(proof, dict) or proof.get("quiescent") is not True:
                raise ReviewError("invalid_lane_state_or_quiescence:%s" % lane_id)
            if state == "observed_acceptance_passed":
                checks = lane_value.get("acceptance_checks")
                if not isinstance(checks, dict) or not checks or not all(
                    value is True for value in checks.values()
                ):
                    raise ReviewError("passing_lane_acceptance_invalid:%s" % lane_id)
                if lane["kind"] == "exact_urls" and lane.get("exact_input_count") is not None:
                    accounting = lane_value.get("exact_identity_accounting")
                    if not isinstance(accounting, dict):
                        raise ReviewError("passing_lane_accounting_missing:%s" % lane_id)
                    expected_count = int(lane["exact_input_count"])
                    try:
                        accounted = int(accounting.get("accounted", -1))
                        selected = int(accounting.get("selected", -1))
                        expected = int(accounting.get("expected", -1))
                        parts = sum(int(accounting.get(name, -1)) for name in (
                            "passed", "quarantined", "skipped", "exceptions",
                        ))
                    except (TypeError, ValueError) as exc:
                        raise ReviewError("passing_lane_accounting_invalid:%s" % lane_id) from exc
                    if not (
                        accounted == selected == expected == expected_count
                        and parts == expected_count
                    ):
                        raise ReviewError("passing_lane_accounting_invalid:%s" % lane_id)
            phase_lane_hashes[lane_id] = str(lane_facts["sha256"])
            lane_hashes[lane_id] = str(lane_facts["sha256"])
            phase_counts[state] = phase_counts.get(state, 0) + 1
            state_counts[state] = state_counts.get(state, 0) + 1
            summaries.append(_receipt_summary(
                lane_relative, lane_value, lane_facts,
                dependency=int(lane_facts["mtime_ns"]) <= cutoff_ns,
            ))
            artifact_receipts[lane_id] = lane_value
            start_hash = lane_value.get("start_receipt_sha256")
            if start_hash is not None:
                start_relative = str(lane["start_relative_path"])
                start_value, start_facts = read(start_relative)
                if start_facts["sha256"] != start_hash:
                    raise ReviewError("lane_start_hash_mismatch:%s" % lane_id)
                summaries.append(_receipt_summary(
                    start_relative, start_value, start_facts,
                    dependency=int(start_facts["mtime_ns"]) <= cutoff_ns,
                ))
        if value.get("lane_terminal_sha256") != phase_lane_hashes or value.get("state_counts") != phase_counts:
            raise ReviewError("phase_hash_chain_mismatch:%s" % phase_id)
        phase_hashes[phase_id] = str(facts["sha256"])
        summaries.append(_receipt_summary(relative, value, facts, dependency=int(facts["mtime_ns"]) <= cutoff_ns))
    if (
        sequence.get("phase_terminal_sha256") != phase_hashes
        or sequence.get("lane_terminal_sha256") != lane_hashes
        or sequence.get("state_counts") != state_counts
    ):
        raise ReviewError("sequence_hash_chain_mismatch")
    for name, field in (("startup_receipt.json", "startup_receipt_sha256"), ("attempt_receipt.json", "attempt_receipt_sha256")):
        value, facts = read(name)
        if facts["sha256"] != sequence.get(field):
            raise ReviewError("sequence_support_hash_mismatch:%s" % name)
        expected_kind = (
            "exact_observation_sequence_startup" if name.startswith("startup")
            else "exact_observation_sequence_attempt"
        )
        expected_support = {
            "schema_version": 1, "kind": expected_kind,
            "sequence_id": plan["sequence_id"], "plan_sha256": plan_sha,
            "production_writes": False,
        }
        if any(value.get(key) != wanted for key, wanted in expected_support.items()):
            raise ReviewError("invalid_sequence_support_receipt:%s" % name)
        if name.startswith("startup") and value.get("pid") != launch_pid:
            raise ReviewError("startup_launch_pid_mismatch")
        summaries.append(_receipt_summary(name, value, facts, dependency=int(facts["mtime_ns"]) <= cutoff_ns))
    summaries.append(_receipt_summary(
        "terminal_receipt.json", sequence, sequence_facts,
        dependency=int(sequence_facts["mtime_ns"]) <= cutoff_ns,
    ))
    return summaries, {"sequence": sequence, "lane_receipts": artifact_receipts}


def _verify_terminal_outputs(
    output_dir: Path, receipt_dir: Path, lanes: Mapping[str, Any],
    lane_receipts: Mapping[str, Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    verified: list[Dict[str, Any]] = []
    for lane_id, lane in lanes.items():
        receipt = lane_receipts[lane_id]
        declared = receipt.get("manifest_and_artifact_hashes", {})
        if declared is None:
            declared = {}
        if not isinstance(declared, dict):
            raise ReviewError("invalid_artifact_hash_map:%s" % lane_id)
        permitted = DIAGNOSTIC_ARTIFACTS if lane["kind"] == "discovery_diagnostic" else EXACT_ARTIFACTS
        if not set(declared).issubset(permitted):
            raise ReviewError("unallowlisted_artifact:%s" % lane_id)
        lane_root = output_dir / _safe_name(lane_id)
        artifact_root = lane_root
        if lane["kind"] == "exact_urls" and declared:
            runs_root = lane_root / "runs"
            try:
                run_dirs = [
                    Path(entry.path) for entry in os.scandir(runs_root)
                    if entry.name.startswith("observe-parallel-")
                    and entry.is_dir(follow_symlinks=False)
                ]
            except OSError as exc:
                raise ReviewError("exact_run_root_unavailable:%s" % lane_id) from exc
            if len(run_dirs) != 1:
                raise ReviewError("exact_run_directory_count:%s" % lane_id)
            artifact_root = run_dirs[0]
        for name, expected in sorted(declared.items()):
            if not isinstance(expected, dict) or not HEX64.fullmatch(str(expected.get("sha256", ""))):
                raise ReviewError("invalid_artifact_pin:%s" % lane_id)
            facts = _stable_hash(artifact_root / name)
            if facts["sha256"] != expected.get("sha256") or facts["bytes"] != expected.get("bytes"):
                raise ReviewError("artifact_hash_mismatch:%s:%s" % (lane_id, name))
            verified.append({"lane_id": lane_id, "name": name, "sha256": facts["sha256"], "bytes": facts["bytes"]})
        log = receipt.get("log_hash_and_byte_accounting")
        if isinstance(log, dict) and HEX64.fullmatch(str(log.get("sha256", ""))):
            log_path = receipt_dir / "lanes" / _safe_name(lane_id) / "child.log"
            facts = _stable_hash(log_path)
            if facts["sha256"] != log.get("sha256") or facts["bytes"] != log.get("bytes_kept"):
                raise ReviewError("child_log_hash_mismatch:%s" % lane_id)
            verified.append({"lane_id": lane_id, "name": "child.log", "sha256": facts["sha256"], "bytes": facts["bytes"]})
    return verified


def _exclusive_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def review_once(args: argparse.Namespace) -> Dict[str, Any]:
    for raw in (
        args.plan, args.runtime_root, args.deployment_root,
        args.controller, args.review_dir,
    ):
        _reject_symlink_components(raw)
    plan_path = args.plan.resolve(strict=True)
    runtime_root = args.runtime_root.resolve(strict=True)
    deployment_root = args.deployment_root.resolve(strict=True)
    controller_path = args.controller.resolve(strict=True)
    review_dir = args.review_dir.resolve(strict=True)
    if stat.S_IMODE(review_dir.stat().st_mode) != 0o700:
        raise ReviewError("review_dir_must_be_real_mode_0700")
    if _paths_overlap(review_dir, runtime_root) or _paths_overlap(review_dir, deployment_root):
        raise ReviewError("review_dir_must_be_disjoint_from_runtime_and_deployment")
    controller_facts = _stable_hash(controller_path)
    if controller_facts["sha256"] != args.controller_sha256:
        raise ReviewError("controller_sha256_mismatch")
    plan, plan_facts = _load_plan(plan_path, args.plan_sha256)
    sequence_id = str(plan["sequence_id"])
    receipt_dir = runtime_root / "receipts" / sequence_id
    output_dir = runtime_root / "outputs" / sequence_id
    if not _within(receipt_dir, runtime_root) or not _within(output_dir, runtime_root):
        raise ReviewError("runtime_path_escape")
    allowed, phases, lanes = _receipt_allowlist(plan)
    cutoff = _parse_cutoff(args.capture_cutoff)
    cutoff_ns = int(cutoff.timestamp() * 1_000_000_000)

    ps_rows = _run_ps()  # exactly one process-table call
    process_facts = _process_facts(
        ps_rows, args.launch_pid, sequence_id, controller_path, plan_path, args.plan_sha256,
    )
    capacity = _capacity_snapshot(deployment_root, receipt_dir, output_dir, ps_rows)

    newer_receipts: set[str] = set()
    terminal_new = False
    terminal_present = False
    for relative in sorted(allowed):
        path = receipt_dir / relative
        try:
            info = _receipt_stat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ReviewError("allowlisted_receipt_is_not_regular:%s" % relative)
        if relative == "terminal_receipt.json":
            terminal_present = True
        if info.st_mtime_ns <= cutoff_ns:
            continue
        newer_receipts.add(relative)
        if relative == "terminal_receipt.json":
            terminal_new = True

    classification = "inconsistent"
    incremental: list[Dict[str, Any]] = []
    chain: list[Dict[str, Any]] = []
    artifacts: list[Dict[str, Any]] = []
    errors: list[str] = []
    if terminal_new:
        try:
            chain, terminal_values = _validate_terminal_chain(
                plan, args.plan_sha256, receipt_dir, phases, lanes, cutoff_ns,
                args.launch_pid,
            )
            artifacts = _verify_terminal_outputs(
                output_dir, receipt_dir, lanes, terminal_values["lane_receipts"],
            )
            incremental = [
                dict(row, chain_dependency=False)
                for row in chain if row["relative_path"] in newer_receipts
            ]
            if process_facts["sequence_process_count"] == 0:
                classification = "terminal"
            else:
                errors.append("terminal_with_live_sequence_process")
        except ReviewError as exc:
            errors.append(str(exc))
    else:
        if terminal_present:
            errors.append("terminal_receipt_not_newer_than_capture_cutoff")
        else:
            for relative in sorted(newer_receipts):
                value, facts = _stable_json(receipt_dir / relative)
                incremental.append(_receipt_summary(relative, value, facts, dependency=False))
            if (
                process_facts["launch_pid_present"]
                and process_facts["launch_controller_exact_match"]
                and process_facts["sequence_controller_count"] == 1
                and process_facts["sequence_child_count"] <= 2
                and process_facts["sequence_browser_count"] == 0
                and process_facts["foreign_sequence_process_count"] == 0
                and process_facts["sequence_process_count"]
                == 1 + process_facts["sequence_child_count"]
            ):
                classification = "active_unattended"
            elif process_facts["sequence_process_count"] == 0:
                classification = "interrupted_quiescent"
            else:
                errors.append("unexpected_or_unpinned_sequence_process")

    report: Dict[str, Any] = {
        "schema_version": 1,
        "kind": "private_local_exact_sequence_review_snapshot",
        "generated_at_utc": _iso_now(),
        "classification": classification,
        "sequence_id": sequence_id,
        "capture_cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "production_writes": False,
        "plan": {"sha256": plan_facts["sha256"], "phase_count": 3, "lane_count": 9},
        "controller": {"sha256": controller_facts["sha256"]},
        "receipt_allowlist": sorted(allowed),
        "incremental_receipts": incremental,
        "terminal_chain_receipts": chain,
        "terminal_artifact_hashes": artifacts,
        "process_snapshot": process_facts,
        "capacity_snapshot": capacity,
        "source_outputs_read": terminal_new,
        "raw_urls_articles_logs_stored": False,
        "errors": errors,
    }
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    report_path = review_dir / "sequence_review.private.json"
    sidecar_path = review_dir / "sequence_review.private.json.sha256"
    _exclusive_write(report_path, encoded)
    digest = _sha(encoded)
    _exclusive_write(sidecar_path, ("%s  %s\n" % (digest, report_path.name)).encode("ascii"))
    report["report_sha256"] = digest
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", required=True, type=Path)
    result.add_argument("--plan-sha256", required=True)
    result.add_argument("--deployment-root", required=True, type=Path)
    result.add_argument("--runtime-root", required=True, type=Path)
    result.add_argument("--controller", required=True, type=Path)
    result.add_argument("--controller-sha256", required=True)
    result.add_argument("--launch-pid", required=True, type=int)
    result.add_argument("--capture-cutoff", required=True)
    result.add_argument("--review-dir", required=True, type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.launch_pid <= 1:
        raise SystemExit("launch PID must be greater than one")
    try:
        report = review_once(args)
    except ReviewError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "reviewed", "classification": report["classification"],
        "report_sha256": report["report_sha256"],
    }, sort_keys=True))
    return 0 if report["classification"] != "inconsistent" else 3


if __name__ == "__main__":
    raise SystemExit(main())
