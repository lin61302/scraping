from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import pytest


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from ops import review_local_exact_sequence_once as reviewer  # noqa: E402


PLAN_SHA_PLACEHOLDER = "0" * 64


def sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    return sha_bytes(payload)


def fixture(tmp_path: Path):
    deployment = tmp_path / "deployment"
    runtime = tmp_path / "runtime.private"
    review = tmp_path / "review"
    deployment.mkdir()
    runtime.mkdir()
    review.mkdir(mode=0o700)
    controller = deployment / "automation/ops/unattended_exact_observation_sequence.py"
    controller.parent.mkdir(parents=True)
    controller.write_text("# controller\n")
    controller_sha = sha_bytes(controller.read_bytes())
    phase_layout = (2, 1, 6)
    phases = []
    index = 0
    for phase_number, count in enumerate(phase_layout, 1):
        lanes = []
        for _ in range(count):
            index += 1
            kind = "discovery_diagnostic" if index <= 2 else "exact_urls"
            lanes.append({
                "lane_id": "lane-%02d" % index,
                "source_id": "mlp:T%02d:source%d.example" % (index, index),
                "kind": kind,
                "production_writes": False,
            })
        phases.append({
            "phase_id": "phase-%d" % phase_number,
            "production_writes": False,
            "lanes": lanes,
        })
    plan = {
        "schema_version": 1,
        "mode": "unattended_exact_observation_sequence",
        "production_writes": False,
        "sequence_id": "exact-review-fixture",
        "phases": phases,
    }
    plan_path = tmp_path / "plan.json"
    plan_sha = write_json(plan_path, plan)
    args = argparse.Namespace(
        plan=plan_path,
        plan_sha256=plan_sha,
        deployment_root=deployment,
        runtime_root=runtime,
        controller=controller,
        controller_sha256=controller_sha,
        launch_pid=43210,
        capture_cutoff="2026-01-01T00:00:00Z",
        review_dir=review,
    )
    return plan, args


def capacity():
    return {
        "schema_version": 1,
        "kind": "host_capacity_guard",
        "status": "reduced",
        "launch_allowed": True,
        "metrics": {"scrape_processes": 1, "browser_processes": 0},
        "recommended_limits": {"network_workers": 2, "browser_workers": 0},
        "thresholds": {},
        "reasons": [],
    }


def exact_process(args):
    return [{
        "pid": args.launch_pid,
        "ppid": 1,
        "pgid": args.launch_pid,
        "command": "%s %s --plan %s --plan-sha256 %s --sequence %s" % (
            sys.executable, args.controller, args.plan, args.plan_sha256,
            "exact-review-fixture",
        ),
    }]


def install_terminal_chain(plan, args, *, corrupt_sequence_hash=False, artifacts=False):
    receipt_root = args.runtime_root / "receipts" / plan["sequence_id"]
    output_root = args.runtime_root / "outputs" / plan["sequence_id"]
    attempt = {
        "schema_version": 1, "kind": "exact_observation_sequence_attempt",
        "sequence_id": plan["sequence_id"], "plan_sha256": args.plan_sha256,
        "production_writes": False,
    }
    startup = {
        "schema_version": 1, "kind": "exact_observation_sequence_startup",
        "sequence_id": plan["sequence_id"], "plan_sha256": args.plan_sha256,
        "production_writes": False, "pid": args.launch_pid,
    }
    attempt_sha = write_json(receipt_root / "attempt_receipt.json", attempt)
    startup_sha = write_json(receipt_root / "startup_receipt.json", startup)
    phase_hashes = {}
    lane_hashes = {}
    total_counts = {}
    for phase in plan["phases"]:
        phase_lane_hashes = {}
        phase_counts = {}
        for lane in phase["lanes"]:
            lane_id = lane["lane_id"]
            state = "observed_acceptance_passed"
            artifact_map = {}
            if artifacts and lane_id == "lane-01":
                name = "diagnostic_manifest.private.json"
                payload = b'{"status":"observed"}\n'
                path = output_root / reviewer._safe_name(lane_id) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                artifact_map[name] = {"sha256": sha_bytes(payload), "bytes": len(payload)}
            elif artifacts and lane_id == "lane-03":
                name = "manifest.json"
                payload = b'{"status":"observed"}\n'
                path = (
                    output_root / reviewer._safe_name(lane_id) / "runs"
                    / "observe-parallel-fixture" / name
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                artifact_map[name] = {"sha256": sha_bytes(payload), "bytes": len(payload)}
            terminal = {
                "schema_version": 1,
                "kind": "exact_observation_lane_terminal",
                "terminal": True,
                "state": state,
                "sequence_id": plan["sequence_id"],
                "phase_id": phase["phase_id"],
                "lane_id": lane_id,
                "source_id": lane["source_id"],
                "plan_sha256": args.plan_sha256,
                "production_writes": False,
                "verified_completion": False,
                "tracker_boundary_advancement": False,
                "process_quiescence_proof": {"quiescent": True},
                "acceptance_checks": {"fixture": True},
                "manifest_and_artifact_hashes": artifact_map,
            }
            relative = "lanes/%s/terminal_receipt.json" % reviewer._safe_name(lane_id)
            digest = write_json(receipt_root / relative, terminal)
            lane_hashes[lane_id] = digest
            phase_lane_hashes[lane_id] = digest
            phase_counts[state] = phase_counts.get(state, 0) + 1
            total_counts[state] = total_counts.get(state, 0) + 1
        phase_terminal = {
            "schema_version": 1,
            "kind": "exact_observation_phase_terminal",
            "terminal": True,
            "sequence_id": plan["sequence_id"],
            "phase_id": phase["phase_id"],
            "plan_sha256": args.plan_sha256,
            "lane_count": len(phase["lanes"]),
            "lane_terminal_sha256": phase_lane_hashes,
            "state_counts": phase_counts,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
        }
        relative = "phases/%s/terminal_receipt.json" % reviewer._safe_name(phase["phase_id"])
        phase_hashes[phase["phase_id"]] = write_json(receipt_root / relative, phase_terminal)
    if corrupt_sequence_hash:
        lane_hashes["lane-09"] = "f" * 64
    terminal = {
        "schema_version": 1,
        "kind": "exact_observation_sequence_terminal",
        "terminal": True,
        "status": "terminal_observation_sequence",
        "sequence_id": plan["sequence_id"],
        "plan_sha256": args.plan_sha256,
        "lane_count": 9,
        "lane_terminal_sha256": lane_hashes,
        "phase_terminal_sha256": phase_hashes,
        "state_counts": total_counts,
        "startup_receipt_sha256": startup_sha,
        "attempt_receipt_sha256": attempt_sha,
        "production_writes": False,
        "verified_completion": False,
        "tracker_boundary_advancement": False,
    }
    write_json(receipt_root / "terminal_receipt.json", terminal)


def patch_snapshots(monkeypatch, rows):
    calls = {"ps": 0, "capacity": 0}

    def ps():
        calls["ps"] += 1
        return rows

    def cap(*_args):
        calls["capacity"] += 1
        return capacity()

    monkeypatch.setattr(reviewer, "_run_ps", ps)
    monkeypatch.setattr(reviewer, "_capacity_snapshot", cap)
    return calls


def test_active_unattended_uses_one_process_and_capacity_snapshot_and_skips_outputs(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    receipt_root = args.runtime_root / "receipts" / plan["sequence_id"]
    write_json(receipt_root / "startup_receipt.json", {"kind": "startup"})
    sentinel = args.runtime_root / "outputs" / plan["sequence_id"] / "do-not-read"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("secret article https://example.invalid/")
    calls = patch_snapshots(monkeypatch, exact_process(args))
    monkeypatch.setattr(
        reviewer, "_verify_terminal_outputs",
        lambda *_args, **_kwargs: pytest.fail("active snapshot read source outputs"),
    )

    report = reviewer.review_once(args)

    assert report["classification"] == "active_unattended"
    assert report["source_outputs_read"] is False
    assert report["terminal_artifact_hashes"] == []
    assert calls == {"ps": 1, "capacity": 1}
    on_disk = json.loads((args.review_dir / "sequence_review.private.json").read_text())
    assert "https://" not in json.dumps(on_disk)
    assert stat.S_IMODE((args.review_dir / "sequence_review.private.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((args.review_dir / "sequence_review.private.json.sha256").stat().st_mode) == 0o600


def test_terminal_verifies_three_phase_nine_lane_chain_and_allowlisted_artifacts(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    install_terminal_chain(plan, args, artifacts=True)
    calls = patch_snapshots(monkeypatch, [])

    report = reviewer.review_once(args)

    assert report["classification"] == "terminal"
    assert len([row for row in report["terminal_chain_receipts"] if row.get("lane_id")]) == 9
    assert len([row for row in report["terminal_chain_receipts"] if row.get("phase_id") and not row.get("lane_id")]) == 3
    assert {(row["lane_id"], row["name"]) for row in report["terminal_artifact_hashes"]} == {
        ("lane-01", "diagnostic_manifest.private.json"),
        ("lane-03", "manifest.json"),
    }
    assert calls == {"ps": 1, "capacity": 1}
    serialized = json.dumps(report)
    assert "https://" not in serialized and "secret article" not in serialized


def test_terminal_reads_each_receipt_at_most_once(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    install_terminal_chain(plan, args)
    patch_snapshots(monkeypatch, [])
    receipt_root = args.runtime_root / "receipts" / plan["sequence_id"]
    original = reviewer._stable_json
    counts = {}

    def counted(path):
        if reviewer._within(path, receipt_root):
            relative = str(path.relative_to(receipt_root))
            counts[relative] = counts.get(relative, 0) + 1
        return original(path)

    monkeypatch.setattr(reviewer, "_stable_json", counted)

    report = reviewer.review_once(args)

    assert report["classification"] == "terminal"
    assert counts
    assert max(counts.values()) == 1


def test_dead_controller_without_terminal_is_interrupted_quiescent(tmp_path, monkeypatch):
    _plan, args = fixture(tmp_path)
    patch_snapshots(monkeypatch, [])
    report = reviewer.review_once(args)
    assert report["classification"] == "interrupted_quiescent"
    assert report["terminal_chain_receipts"] == []


def test_broken_terminal_hash_chain_is_inconsistent(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    install_terminal_chain(plan, args, corrupt_sequence_hash=True)
    patch_snapshots(monkeypatch, [])
    report = reviewer.review_once(args)
    assert report["classification"] == "inconsistent"
    assert report["errors"] == ["sequence_hash_chain_mismatch"]


def test_symlink_receipt_fails_closed(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    receipt_root = args.runtime_root / "receipts" / plan["sequence_id"]
    target = tmp_path / "outside.json"
    write_json(target, {"kind": "startup"})
    receipt_root.mkdir(parents=True)
    (receipt_root / "startup_receipt.json").symlink_to(target)
    patch_snapshots(monkeypatch, exact_process(args))
    with pytest.raises(reviewer.ReviewError, match="allowlisted_receipt_is_not_regular"):
        reviewer.review_once(args)
    assert not (args.review_dir / "sequence_review.private.json").exists()


def test_duplicate_keys_and_exclusive_snapshot_fail_closed(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    receipt_root = args.runtime_root / "receipts" / plan["sequence_id"]
    receipt_root.mkdir(parents=True)
    (receipt_root / "startup_receipt.json").write_text('{"kind":"a","kind":"b"}\n')
    patch_snapshots(monkeypatch, exact_process(args))
    with pytest.raises(reviewer.ReviewError, match="duplicate_json_key"):
        reviewer.review_once(args)

    (receipt_root / "startup_receipt.json").unlink()
    first = reviewer.review_once(args)
    assert first["classification"] == "active_unattended"
    with pytest.raises(FileExistsError):
        reviewer.review_once(args)


def test_receipt_summary_rejects_nested_strings_that_could_leak_urls_or_paths(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    receipt_root = args.runtime_root / "receipts" / plan["sequence_id"]
    write_json(receipt_root / "startup_receipt.json", {
        "kind": "startup",
        "remaining_failure_classes": {"leak": "https://credential.invalid/private"},
    })
    patch_snapshots(monkeypatch, exact_process(args))

    with pytest.raises(reviewer.ReviewError, match="unsafe_receipt_summary_value"):
        reviewer.review_once(args)
    assert not (args.review_dir / "sequence_review.private.json").exists()


def test_terminal_receipt_at_or_before_cutoff_is_not_reread(tmp_path, monkeypatch):
    plan, args = fixture(tmp_path)
    install_terminal_chain(plan, args)
    args.capture_cutoff = "2099-01-01T00:00:00Z"
    patch_snapshots(monkeypatch, [])
    original_stat = reviewer._receipt_stat
    terminal = (
        args.runtime_root / "receipts" / plan["sequence_id"]
        / "terminal_receipt.json"
    )
    terminal_stats = 0

    def counted(path):
        nonlocal terminal_stats
        if path == terminal:
            terminal_stats += 1
        return original_stat(path)

    monkeypatch.setattr(reviewer, "_receipt_stat", counted)
    report = reviewer.review_once(args)
    assert report["classification"] == "inconsistent"
    assert report["incremental_receipts"] == []
    assert report["errors"] == ["terminal_receipt_not_newer_than_capture_cutoff"]
    assert terminal_stats == 1


def test_reviewer_and_ancestor_wrappers_are_excluded_from_process_classification(tmp_path, monkeypatch):
    _plan, args = fixture(tmp_path)
    own = os.getpid()
    parent = own + 100000
    rows = [
        {
            "pid": own,
            "ppid": parent,
            "pgid": own,
            "command": "review_local_exact_sequence_once.py exact-review-fixture",
        },
        {
            "pid": parent,
            "ppid": 1,
            "pgid": parent,
            "command": "shell wrapper exact-review-fixture unattended_exact_observation_sequence.py",
        },
        exact_process(args)[0],
    ]
    patch_snapshots(monkeypatch, rows)

    report = reviewer.review_once(args)

    assert report["classification"] == "active_unattended"
    assert report["process_snapshot"]["sequence_process_count"] == 1
    assert report["process_snapshot"]["scrape_process_count"] == 1


@pytest.mark.parametrize("extra_command", [
    "unattended_exact_observation_sequence.py exact-review-fixture --duplicate",
    "parallel_observe_pure.py exact-review-fixture --one --headless",
])
def test_active_rejects_duplicate_or_browser_sequence_process(tmp_path, monkeypatch, extra_command):
    _plan, args = fixture(tmp_path)
    rows = exact_process(args) + [{
        "pid": args.launch_pid + 1,
        "ppid": args.launch_pid,
        "pgid": args.launch_pid + 1,
        "command": extra_command,
    }]
    patch_snapshots(monkeypatch, rows)

    report = reviewer.review_once(args)

    assert report["classification"] == "inconsistent"
    assert report["errors"] == ["unexpected_or_unpinned_sequence_process"]


def test_active_allows_at_most_two_exact_sequence_children(tmp_path, monkeypatch):
    _plan, args = fixture(tmp_path)
    rows = exact_process(args)
    for offset, name in enumerate((
        "parallel_observe_pure.py", "run_pure_discovery_diagnostic.py",
    ), 1):
        rows.append({
            "pid": args.launch_pid + offset,
            "ppid": args.launch_pid,
            "pgid": args.launch_pid + offset,
            "command": "%s exact-review-fixture" % name,
        })
    patch_snapshots(monkeypatch, rows)

    report = reviewer.review_once(args)

    assert report["classification"] == "active_unattended"
    assert report["process_snapshot"]["sequence_child_count"] == 2
    assert report["process_snapshot"]["foreign_sequence_process_count"] == 0


def test_active_rejects_allowlisted_child_name_without_direct_controller_parent(tmp_path, monkeypatch):
    _plan, args = fixture(tmp_path)
    rows = exact_process(args) + [{
        "pid": args.launch_pid + 1,
        "ppid": 777,
        "pgid": args.launch_pid + 1,
        "command": "parallel_observe_pure.py exact-review-fixture",
    }]
    patch_snapshots(monkeypatch, rows)

    report = reviewer.review_once(args)

    assert report["classification"] == "inconsistent"
    assert report["process_snapshot"]["sequence_child_count"] == 0
    assert report["process_snapshot"]["foreign_sequence_process_count"] == 1


def test_capacity_patterns_match_reviewed_launcher_and_browser_count_is_exclusive(tmp_path, monkeypatch):
    _plan, args = fixture(tmp_path)
    assert reviewer.SCRAPE_PATTERNS == (
        "parallel_observe_pure.py", "multiprocess_observe_pure.py",
        "run_pure_discovery_diagnostic.py", "unattended_exact_observation_sequence.py",
        "local_unattended_country_observation.py", "unattended_country_sessions.py",
    )
    rows = exact_process(args) + [{
        "pid": args.launch_pid + 1,
        "ppid": args.launch_pid,
        "pgid": args.launch_pid + 1,
        "command": "parallel_observe_pure.py exact-review-fixture --headless",
    }]
    patch_snapshots(monkeypatch, rows)
    facts = reviewer._process_facts(
        rows, args.launch_pid, "exact-review-fixture",
        args.controller, args.plan, args.plan_sha256,
    )
    assert facts["browser_process_count"] == 1
    assert facts["scrape_process_count"] == 1  # controller only; browser row is exclusive


def test_review_directory_must_be_disjoint_from_runtime(tmp_path, monkeypatch):
    _plan, args = fixture(tmp_path)
    nested = args.runtime_root / "review"
    nested.mkdir(mode=0o700)
    args.review_dir = nested
    patch_snapshots(monkeypatch, exact_process(args))

    with pytest.raises(
        reviewer.ReviewError,
        match="review_dir_must_be_disjoint_from_runtime_and_deployment",
    ):
        reviewer.review_once(args)
    assert not (nested / "sequence_review.private.json").exists()
