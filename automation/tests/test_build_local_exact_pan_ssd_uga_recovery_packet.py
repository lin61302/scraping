"""Focused offline contract tests for the fresh single-worker packet builder."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import build_local_exact_pan_ssd_uga_recovery_packet as builder  # noqa: E402
from ops import launch_local_exact_sequence_once as launcher  # noqa: E402
from ops.unattended_exact_observation_sequence import RuntimePaths  # noqa: E402


def test_scope_is_exactly_four_lanes_and_396_identities():
    assert [lane["expected_count"] for lane in builder.LANE_SPECS] == [221, 143, 1, 31]
    assert sum(int(lane["expected_count"]) for lane in builder.LANE_SPECS) == 396
    assert set(builder.SOURCE_SPECS) == {"PAN", "SSD", "UGA"}
    assert sum(int(spec["preserved_count"]) for spec in builder.SOURCE_SPECS.values()) == 1240
    assert sum(int(spec["exclusion_count"]) for spec in builder.SOURCE_SPECS.values()) == 546


def test_recovery2_packet_identity_is_fresh():
    assert builder.SEQUENCE_ID == (
        "local-exact-pan-ssd-uga-repair-recovery2-20261107-through-20260930"
    )
    assert builder.PACKET_DIRECTORY == (
        "local_exact_pan_ssd_uga_repair_recovery2_20261107_through_20260930"
    )
    assert builder.BINDING_DIRECTORY == "launcher_binding_readiness_20261107"
    assert builder.SEQUENCE_ID != builder.CONSUMED_PREDECESSOR_FAILURE_PINS["sequence_id"]


def test_exact_identity_hashes_are_frozen():
    assert [lane["exact_url_sha256"] for lane in builder.LANE_SPECS] == [
        "c45c685819fdccf1a0dde817e98ef9efeaa29cc7b31ff52774962deda7ae8b74",
        "ef3f4a55419e82e5bfd9d5c52bb7929d73d0099e3fa3488d6b49a05b6b5499e9",
        "d99891f996f5a6f79dcf32b028b181dedf9a75ee51c702994150bc5465b5186d",
        "7e35508090668c5ab54fb5dcb6872c78e7903b625409326ddedbfa5dd0854296",
    ]
    assert builder.GLOBAL_EXACT_URL_SHA256 == (
        "8574b5103e7b82761f532c5e07fcd8aa03f6297dcf20390a749383ecfddce2af"
    )


def test_target_hash_preserves_first_occurrence_order():
    values = ["https://example.invalid/a", "https://example.invalid/b", "https://example.invalid/a"]
    assert builder.target_url_sha256(values) == builder.sha256_bytes(
        b"https://example.invalid/a\nhttps://example.invalid/b\n"
    )


def test_select_rows_isolates_single_reviewed_redirect():
    parser = {"url": "parser", "offline_classification": {"reason_code": "title_disagreement"}}
    redirect = {
        "url": "redirect",
        "offline_classification": {"reason_code": "unreviewed_redirect_access_blocked"},
    }
    assert builder.select_rows([parser, redirect], "not_redirect") == [parser]
    assert builder.select_rows([parser, redirect], "redirect_only") == [redirect]
    assert builder.select_rows([parser, redirect], "all") == [parser, redirect]
    with pytest.raises(builder.BuildError):
        builder.select_rows([parser], "unsupported")


def test_no_bytecode_prefix_is_exact():
    assert builder.exact_no_bytecode_prefix(Path("/python"), Path("/launcher")) == [
        "/usr/bin/env", "PYTHONDONTWRITEBYTECODE=1", "/python", "-B", "/launcher",
    ]


def _runtime_hashes() -> dict[str, str]:
    return {relative: "a" * 64 for relative in builder.RUNTIME_CODE_FILES}


def _runtime_proof() -> dict[str, object]:
    hashes = _runtime_hashes()
    hashes["automation/ops/host_capacity_guard.py"] = "b" * 64
    hashes["automation/ops/unattended_exact_observation_sequence.py"] = "c" * 64
    hashes["automation/ops/launch_local_exact_sequence_once.py"] = "d" * 64
    return {
        "kind": "private_single_worker_runtime_code_proof",
        "status": "published_reviewed",
        "repository": "lin61302/scraping",
        "branch": "main",
        "runtime_commit": "1" * 40,
        "capacity_guard_publication_commit": "2" * 40,
        "capacity_guard_publication_receipt_sha256": "f" * 64,
        "packet_network_worker_ceiling": 1,
        "browser_worker_ceiling": 0,
        "plan_network_ceiling_supported": True,
        "runtime_publication_change_scope": "launcher_and_focused_test_only",
        "unchanged_controller_pinned_by_runtime_code_proof": True,
        "controller_publication_change_required": False,
        "runtime_code": hashes,
        "capacity_guard_sha256": "b" * 64,
        "controller_sha256": "c" * 64,
        "launcher_sha256": "d" * 64,
    }


def test_runtime_code_proof_requires_exact_local_bytes_and_single_worker_support():
    proof = _runtime_proof()
    pins = builder.validate_runtime_code_proof(
        proof, commit="1" * 40, guard_commit="2" * 40,
        guard_receipt_sha256="f" * 64, local_hashes=proof["runtime_code"],
    )
    assert pins == {
        "capacity_guard_sha256": "b" * 64,
        "controller_sha256": "c" * 64,
        "launcher_sha256": "d" * 64,
    }
    bad = dict(proof, plan_network_ceiling_supported=False)
    with pytest.raises(builder.BuildError):
        builder.validate_runtime_code_proof(
            bad, commit="1" * 40, guard_commit="2" * 40,
            guard_receipt_sha256="f" * 64, local_hashes=proof["runtime_code"],
        )


def test_runtime_code_proof_rejects_hash_drift():
    proof = _runtime_proof()
    drifted = dict(proof["runtime_code"])
    drifted["automation/ops/host_capacity_guard.py"] = "e" * 64
    with pytest.raises(builder.BuildError):
        builder.validate_runtime_code_proof(
            proof, commit="1" * 40, guard_commit="2" * 40,
            guard_receipt_sha256="f" * 64, local_hashes=drifted,
        )


def test_runtime_code_proof_requires_unchanged_controller_pin_contract():
    proof = _runtime_proof()
    for field, value in (
        ("runtime_publication_change_scope", "wrong"),
        ("unchanged_controller_pinned_by_runtime_code_proof", False),
        ("controller_publication_change_required", True),
    ):
        with pytest.raises(builder.BuildError):
            builder.validate_runtime_code_proof(
                dict(proof, **{field: value}), commit="1" * 40,
                guard_commit="2" * 40, guard_receipt_sha256="f" * 64,
                local_hashes=proof["runtime_code"],
            )


def test_runtime_code_proof_requires_separate_guard_publication_pin():
    proof = _runtime_proof()
    with pytest.raises(builder.BuildError):
        builder.validate_runtime_code_proof(
            proof, commit="1" * 40, guard_commit="3" * 40,
            guard_receipt_sha256="f" * 64, local_hashes=proof["runtime_code"],
        )


def test_publisher_receipt_requires_remote_commit_and_all_paths():
    value = {
        "outcome": "published", "repository": "lin61302/scraping", "branch": "main",
        "commit_sha": "1" * 40, "remote_sha": "1" * 40,
        "changed_paths": ["automation/ops/a.py", "automation/tests/test_a.py"],
    }
    builder.validate_publisher_receipt(
        value, commit="1" * 40,
        required_paths=["automation/ops/a.py", "automation/tests/test_a.py"], field="fixture",
    )
    with pytest.raises(builder.BuildError):
        builder.validate_publisher_receipt(
            dict(value, remote_sha="2" * 40), commit="1" * 40,
            required_paths=["automation/ops/a.py"], field="fixture",
        )


def test_launcher_only_runtime_publication_requires_launcher_and_focused_test():
    required = [
        "automation/ops/launch_local_exact_sequence_once.py",
        "automation/tests/test_launch_local_exact_sequence_once.py",
    ]
    value = {
        "outcome": "published", "repository": "lin61302/scraping", "branch": "main",
        "commit_sha": "1" * 40, "remote_sha": "1" * 40,
        "changed_paths": list(required),
    }
    builder.validate_publisher_receipt(
        value, commit="1" * 40, required_paths=required, field="runtime fixture",
    )
    assert "automation/ops/unattended_exact_observation_sequence.py" not in required
    for missing in required:
        with pytest.raises(builder.BuildError):
            builder.validate_publisher_receipt(
                {**value, "changed_paths": [path for path in required if path != missing]},
                commit="1" * 40, required_paths=required, field="runtime fixture",
            )


def test_independent_review_requires_one_network_zero_browser():
    value = {
        "kind": "private_single_worker_runtime_independent_review", "result": "approved",
        "blocker_count": 0, "runtime_commit": "1" * 40,
        "plan_network_ceiling_supported": True,
        "packet_network_worker_ceiling": 1, "browser_worker_ceiling": 0,
        "runtime_publication_change_scope": "launcher_and_focused_test_only",
        "unchanged_controller_pinned_by_runtime_code_proof": True,
        "controller_publication_change_required": False,
    }
    builder.validate_independent_review(value, commit="1" * 40)
    with pytest.raises(builder.BuildError):
        builder.validate_independent_review(
            dict(value, packet_network_worker_ceiling=2), commit="1" * 40,
        )


def test_builder_review_pins_final_builder_and_test_bytes():
    value = {
        "kind": "private_single_worker_packet_builder_independent_review",
        "result": "approved", "blocker_count": 0, "builder_commit": "2" * 40,
        "builder_sha256": "a" * 64, "builder_test_sha256": "b" * 64,
    }
    builder.validate_builder_review(
        value, builder_commit="2" * 40,
        builder_sha256="a" * 64, test_sha256="b" * 64,
    )
    with pytest.raises(builder.BuildError):
        builder.validate_builder_review(
            value, builder_commit="2" * 40,
            builder_sha256="c" * 64, test_sha256="b" * 64,
        )


def test_focused_test_record_must_be_passing_and_no_bytecode():
    value = {
        "kind": "sanitized_immutable_single_worker_recovery_test_provenance",
        "outcome": "passed", "runtime_commit": "1" * 40, "builder_commit": "2" * 40,
        "runtime_publication_change_scope": "launcher_and_focused_test_only",
        "unchanged_controller_pinned_by_runtime_code_proof": True,
        "controller_publication_change_required": False,
        "file_pins": {"builder_sha256": "a" * 64, "builder_test_sha256": "b" * 64},
        "verification": {
            "python_dont_write_bytecode": True,
            "single_worker_runtime_and_builder_tests": {"passed": 17, "failed": 0, "errors": 0},
        },
    }
    builder.validate_test_record(
        value, commit="1" * 40, builder_commit="2" * 40,
        builder_sha256="a" * 64, test_sha256="b" * 64,
    )
    with pytest.raises(builder.BuildError):
        builder.validate_test_record(
            {**value, "verification": {
                "python_dont_write_bytecode": False,
                "single_worker_runtime_and_builder_tests": {"passed": 17, "failed": 0, "errors": 0},
            }},
            commit="1" * 40, builder_commit="2" * 40,
            builder_sha256="a" * 64, test_sha256="b" * 64,
        )


def test_runtime_deployment_excludes_remote_launcher():
    assert "automation/ops/launch_exact_sequence_once.py" not in builder.RUNTIME_CODE_FILES
    assert "automation/ops/launch_local_exact_sequence_once.py" in builder.RUNTIME_CODE_FILES


def test_manifest_row_schema_passes_local_launcher_verifier(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    source_dir = workspace / "source"
    source_dir.mkdir(mode=0o700)
    source = source_dir / "plan.json"
    source.write_text("{}\n", encoding="utf-8")
    source.chmod(0o600)
    relative = "exact_observation_sequence.private.json"
    row = {
        "source_path": "source/plan.json",
        "deployment_relative_path": relative,
        "deployed_path": "PENDING",
        "sha256": builder.sha256_path(source),
        "bytes": source.stat().st_size,
        "mode": "0600",
        "private": True,
    }
    digest = builder.deployment_digest([row])
    assert digest == launcher.deployment_digest([row])
    packet = workspace / "packet"
    packet.mkdir(mode=0o700)
    deployments = packet / "deployments"
    deployments.mkdir(mode=0o700)
    deployment = deployments / digest
    deployment.mkdir(mode=0o700)
    automation = deployment / "automation"
    automation.mkdir(mode=0o700)
    deployed = deployment / relative
    deployed.write_bytes(source.read_bytes())
    deployed.chmod(0o600)
    row["deployed_path"] = str(deployed)

    runtime_root = packet / "runtime.private"
    runtime_root.mkdir(mode=0o700)
    runtime_dirs = {}
    for name in ("attempts", "locks", "logs", "outputs", "receipts"):
        runtime_dirs[name] = runtime_root / name
        runtime_dirs[name].mkdir(mode=0o700)
    allowed_parent = workspace / "sealed-parent"
    allowed_parent.mkdir(mode=0o700)
    runtime = RuntimePaths(
        python=Path("/usr/bin/python3"), deployment_root=deployment,
        automation_root=automation, output_root=runtime_dirs["outputs"],
        receipt_root=runtime_dirs["receipts"],
        host_lock_path=runtime_dirs["locks"] / "controller.lock",
        allowed_parent_roots=(allowed_parent,),
    )
    paths = {
        "log_root": runtime_dirs["logs"], "lock_root": runtime_dirs["locks"],
        "operation_root": runtime_dirs["attempts"],
        "attempt_marker": runtime_dirs["attempts"] / "attempt_receipt.json",
        "launch_receipt": runtime_dirs["attempts"] / "launch_receipt.json",
        "controller_log": runtime_dirs["logs"] / "controller.log",
    }
    plan = SimpleNamespace(
        sequence_id="fixture-single-worker", code_git_commit="1" * 40,
        network_ceiling=1, plan_sha256=builder.sha256_path(deployed),
    )
    manifest = {
        "schema_version": 1,
        "kind": "private_local_exact_sequence_fallback_deployment",
        "mode": "content_addressed_local_exact_sequence_deployment",
        "sequence_id": plan.sequence_id, "host_class": "local_darwin",
        "transport": "local", "production_writes": False,
        "repository": "lin61302/scraping", "branch": "main",
        "code_git_commit": plan.code_git_commit, "network_ceiling": 1,
        "browser_ceiling": 0, "deployment_sha256": digest,
        "controller_sha256": "c" * 64, "plan_sha256": plan.plan_sha256,
        "deployment_root_local": str(deployment), "automation_root_local": str(automation),
        "controller_local": str(automation / "ops/unattended_exact_observation_sequence.py"),
        "plan_local": str(deployed), "python_local": str(runtime.python),
        "output_root_local": str(runtime.output_root),
        "receipt_root_local": str(runtime.receipt_root),
        "log_root_local": str(paths["log_root"]), "lock_root_local": str(paths["lock_root"]),
        "attempt_root_local": str(paths["operation_root"]),
        "host_lock_local": str(runtime.host_lock_path),
        "attempt_marker_local": str(paths["attempt_marker"]),
        "launch_receipt_local": str(paths["launch_receipt"]),
        "controller_log_local": str(paths["controller_log"]),
        "allowed_parent_roots_local": [str(allowed_parent)], "files": [row],
    }
    manifest_path = packet / "manifest.json"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    result = launcher.verify_deployment_manifest(
        manifest_path=manifest_path,
        expected_manifest_sha256=builder.sha256_path(manifest_path),
        expected_deployment_sha256=digest, workspace=workspace, runtime=runtime,
        paths=paths, plan=plan, plan_path=deployed, controller_sha256="c" * 64,
    )
    assert result["deployment_sha256"] == digest
    assert result["file_count"] == 1


def test_predecessor_is_hash_pinned_without_a_path():
    assert set(builder.PREDECESSOR_PINS) == {
        "sequence_id", "full_integrity_report_sha256", "independent_packet_audit_sha256",
        "latest_completion_receipt_sha256", "latest_capacity_review_sha256",
    }
    assert not any("path" in key for key in builder.PREDECESSOR_PINS)


def test_consumed_predecessor_failure_pins_are_hash_only_and_exact():
    assert set(builder.CONSUMED_PREDECESSOR_FAILURE_PINS) == {
        "sequence_id", "full_integrity_report_sha256", "independent_packet_audit_sha256",
        "attempt_receipt_sha256", "launch_receipt_sha256",
        "independent_failure_review_sha256", "sanitized_failure_provenance_commit",
        "sanitized_failure_record_sha256",
    }
    assert builder.CONSUMED_PREDECESSOR_FAILURE_PINS["full_integrity_report_sha256"] == (
        "deeab7fdf568deb3d5b65a5844561276ce91ecf68fc0ab9571f437fa74672b01"
    )
    assert builder.CONSUMED_PREDECESSOR_FAILURE_PINS["attempt_receipt_sha256"] == (
        "c129d068433406d225830807c3d3d4f495a1185f261c709cc8baeb729894c640"
    )
    assert builder.CONSUMED_PREDECESSOR_FAILURE_PINS["launch_receipt_sha256"] == (
        "327cb164e37fb2fc7889bb454484959505b7d78e8830e939bd0fb1ab70328b32"
    )
    assert builder.CONSUMED_PREDECESSOR_FAILURE_PINS["independent_failure_review_sha256"] == (
        "65db0b1739a068df25dbd707578bdd34585cbe17c7b642b989be21e87c070cdd"
    )
    assert all(
        token not in key
        for key in builder.CONSUMED_PREDECESSOR_FAILURE_PINS
        for token in ("path", "root", "runtime", "url")
    )
    for key, value in builder.CONSUMED_PREDECESSOR_FAILURE_PINS.items():
        if key == "sequence_id":
            continue
        assert len(value) in {40, 64}
        assert all(character in "0123456789abcdef" for character in value)


def test_builder_source_freezes_one_network_zero_browser_and_offline_gates():
    text = Path(builder.__file__).read_text(encoding="utf-8")
    assert '"network_ceiling": 1' in text
    assert '"max_active_lanes": 1' in text
    assert '"workers": 1' in text
    assert '"browser_ceiling": 0' in text
    assert '"capacity_guard_run": False' in text
    assert '"preflight_run": False' in text
    assert '"launcher_run": False' in text
    assert '"consumed_predecessor_runtime_read": False' in text
    assert '"consumed_predecessor_failure_preservation.private.json"' in text


def test_source_monthly_contract_uses_date_publish_and_pending_not_zero():
    text = Path(builder.__file__).read_text(encoding="utf-8")
    assert '"publication_date_field": "date_publish"' in text
    assert '"scraped_promoted": None' in text
    assert '"tcg_complete": None' in text
    assert '"missing_history_must_remain_pending_not_zero": True' in text
