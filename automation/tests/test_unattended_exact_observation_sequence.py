from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import unattended_exact_observation_sequence as sequence


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deployment_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Return a byte, mode, and directory-closure snapshot without imports."""

    rows: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            rows.append((relative, "symlink", mode, os.readlink(path)))
        elif path.is_dir():
            rows.append((relative, "directory", mode))
        elif path.is_file():
            rows.append((relative, "file", mode, info.st_size, sha(path)))
        else:
            rows.append((relative, "other", mode))
    return tuple(rows)


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.deployment = root / "deployment"
        self.automation = self.deployment / "automation"
        self.ops = self.automation / "ops"
        self.scripts = self.automation / "scripts"
        self.adapters = self.automation / "adapters/TST"
        for path in (self.ops, self.scripts, self.adapters):
            path.mkdir(parents=True, exist_ok=True)
        self.execution = []
        for relative in (
            "automation/ops/unattended_exact_observation_sequence.py",
            "automation/ops/run_pure_discovery_diagnostic.py",
            "automation/ops/host_capacity_guard.py",
            "automation/scripts/parallel_observe_pure.py",
        ):
            path = self.deployment / relative
            path.write_text("# pinned fixture %s\n" % path.name, encoding="utf-8")
            self.execution.append({"path": relative, "sha256": sha(path)})
        self.adapter = self.adapters / "source.py"
        self.adapter.write_text(
            "class Config:\n source_name='example.test'; country_code='TST'; production_writes=False\n"
            "PRODUCTION_WRITES=False\n"
            "def discover_urls(start,end,evidence): return []\n"
            "def scrape_url(url): return {}\n",
            encoding="utf-8",
        )
        self.input = self.deployment / "private/exact.txt"
        self.input.parent.mkdir(parents=True)
        self.input.write_text(
            "https://example.test/a\nhttps://example.test/b\n", encoding="utf-8"
        )
        self.parent = root / "parent/observe-parallel-parent"
        self.parent.mkdir(parents=True)
        (self.parent / "manifest.json").write_text(json.dumps({
            "run_id": "parent", "status": "observed", "source": "example.test",
        }) + "\n", encoding="utf-8")
        for name in ("urls.jsonl", "field_checks.jsonl", "issues.jsonl"):
            (self.parent / name).write_text("{}\n", encoding="utf-8")
        self.parent_hashes = {
            name: sha(self.parent / name) for name in sequence.PARENT_ARTIFACTS
        }
        self.payload = {
            "schema_version": 1,
            "mode": "unattended_exact_observation_sequence",
            "sequence_id": "fixture-exact-sequence",
            "production_writes": False,
            "continue_after_isolated_lane_failure": True,
            "provenance": {
                "repository": "lin61302/scraping", "branch": "main",
                "code_git_commit": "1" * 40,
            },
            "capacity": {
                "network_ceiling": 2, "browser_ceiling": 0,
                "max_wall_seconds": 100, "log_max_bytes": 50 * 1024 * 1024,
            },
            "execution_files": self.execution,
            "phases": [{
                "phase_id": "exact-phase", "production_writes": False,
                "max_active_lanes": 1,
                "lanes": [{
                    "lane_id": "exact-two", "kind": "exact_urls",
                    "source_id": "mlp:TST:example.test", "production_writes": False,
                    "adapter": {
                        "path": "automation/adapters/TST/source.py",
                        "sha256": sha(self.adapter),
                    },
                    "start": "2026-09-01", "end": "2026-09-30",
                    "workers": 2, "timeout_seconds": 30, "rediscovery": False,
                    "repair_class": "parser_exact",
                    "exact_input": {"path": "private/exact.txt", "sha256": sha(self.input)},
                    "exact_input_count": 2,
                    "parent": {
                        "run_dir": str(self.parent), "artifacts": self.parent_hashes,
                    },
                    "derivation_reason": "exact_fixture_repair",
                    "acceptance": {
                        "manifest_status": "observed", "exceptions_max": 0,
                        "issue_code_max": {"date_missing": 0},
                    },
                }],
            }],
        }
        self.runtime = sequence.RuntimePaths(
            python=Path(sys.executable), deployment_root=self.deployment,
            automation_root=self.automation, output_root=root / "output",
            receipt_root=root / "receipts", host_lock_path=root / "lock/controller.lock",
            allowed_parent_roots=(root / "parent",),
        )

    def write_plan(self, payload=None):
        path = self.deployment / "plan.json"
        path.write_text(json.dumps(payload or self.payload, sort_keys=True) + "\n", encoding="utf-8")
        return path, sha(path)

    def load(self, payload=None):
        path, digest = self.write_plan(payload)
        return sequence.load_plan(path, digest, self.deployment, (self.root / "parent",))


def add_sealed_classification_parent(
    fixture: Fixture, payload: dict, *, invalid_url_set: bool = False,
    summary_count: int | None = None, parent_classified_count: int | None = None,
    overlap_with_worklist: bool = False, path_injection: bool = False,
) -> dict:
    lane = payload["phases"][0]["lanes"][0]
    lane.pop("parent", None)
    root = fixture.deployment / "private/sealed-parent"
    root.mkdir(parents=True, exist_ok=True)
    worklist = fixture.input
    exact_urls = worklist.read_text(encoding="utf-8").splitlines()
    preserved_urls = [f"https://example.test/pass-{index}" for index in range(11)]
    if overlap_with_worklist:
        preserved_urls[0] = exact_urls[0]
    preserved = root / "preserved_passes.private.ndjson"
    preserved.write_text(''.join(
        json.dumps({"source_id": lane["source_id"], "url": url}) + "\n"
        for url in preserved_urls
    ), encoding="utf-8")
    exclusions = root / "verified_exclusions.private.ndjson"
    exclusions.write_text(''.join(
        json.dumps({"source_id": lane["source_id"], "url": f"https://example.test/exclusion-{index}"}) + "\n"
        for index in range(5)
    ), encoding="utf-8")
    parent_count = parent_classified_count or lane["exact_input_count"]
    classified_urls = list(exact_urls) + [
        f"https://example.test/classified-extra-{index}"
        for index in range(parent_count - len(exact_urls))
    ]
    classified = root / "classified_source_ledger.private.ndjson"
    classified.write_text(''.join(
        json.dumps({"source_id": lane["source_id"], "url": url}) + "\n"
        for url in classified_urls
    ), encoding="utf-8")
    summary = root / "source_summary.private.json"
    summary_payload = {
        "source_id": lane["source_id"],
        "classified_count": parent_count if summary_count is None else summary_count,
        "preserved_passes": {"count": 11, "sha256": sha(preserved)},
        "verified_exclusions": {"count": 5, "sha256": sha(exclusions)},
    }
    if path_injection:
        summary_payload["runtime_root"] = "/private/closed-runtime"
    summary.write_text(json.dumps(summary_payload) + "\n", encoding="utf-8")
    seal = root / "classification_seal.private.json"
    seal.write_text(json.dumps({
        "kind": "private_offline_classification_seal",
        "status": "sealed_pending_independent_audit",
        "output_artifacts": [{"sha256": sha(summary)}, {"sha256": sha(classified)}],
    }) + "\n", encoding="utf-8")
    audit = root / "independent_audit.private.json"
    audit.write_text(json.dumps({
        "kind": "private_independent_offline_source_local_classification_audit",
        "result": "approved", "failed_checks": [],
        "check_summary": {"failed": 0, "passed": 3, "total": 3},
        "audited_artifacts": {"classification_seal_sha256": sha(seal)},
        "sources": {lane["source_id"].split(":", 2)[1]: {
            "classified_count": parent_count, "preserved_pass_count": 11,
            "verified_exclusion_count": 5,
        }},
    }) + "\n", encoding="utf-8")
    worklist_urls = worklist.read_text(encoding="utf-8").splitlines()
    url_set_sha = sequence.target_url_sha256(worklist_urls)
    handoff = root / "handoff_manifest.private.json"
    handoff.write_text(json.dumps({
        "schema_version": 1,
        "kind": "sealed_classification_exact_handoff",
        "status": "sealed",
        "source_id": lane["source_id"],
        "source_name": lane["source_id"].split(":", 2)[-1],
        "worklist": {
            "count": lane["exact_input_count"],
            "sha256": sha(worklist),
            "url_set_sha256": "0" * 64 if invalid_url_set else url_set_sha,
        },
        "classification_seal_sha256": sha(seal),
        "independent_audit_sha256": sha(audit),
        "source_summary_sha256": sha(summary),
        "preserved_passes": {
            "count": 11, "sha256": sha(preserved),
            "url_set_sha256": sequence.target_url_sha256(preserved_urls),
        },
        "verified_exclusions": {
            "count": 5, "sha256": sha(exclusions),
            "url_set_sha256": sequence.target_url_sha256(
                [f"https://example.test/exclusion-{index}" for index in range(5)]
            ),
        },
        "classified_source_ledger": {
            "count": parent_count, "sha256": sha(classified),
            "url_set_sha256": sequence.target_url_sha256(classified_urls),
        },
    }) + "\n", encoding="utf-8")
    lane["sealed_classification_parent"] = {
        name: {
            "path": path.relative_to(fixture.deployment).as_posix(),
            "sha256": sha(path),
        }
        for name, path in {
            "handoff_manifest": handoff,
            "worklist": worklist,
            "classification_seal": seal,
            "independent_audit": audit,
            "source_summary": summary,
            "preserved_passes": preserved,
            "verified_exclusions": exclusions,
            "classified_source_ledger": classified,
        }.items()
    }
    lane["acceptance"].update({
        "targeted_issue_codes_zero": ["adapter_parser_error"],
        "allowed_unrelated_issue_codes": ["title_missing"],
    })
    return lane


def green_report(workers=2):
    return {
        "status": "reduced", "launch_allowed": True,
        "recommended_limits": {"network_workers": workers, "browser_workers": 0},
        "sequence_limits": {"network_workers": workers, "browser_workers": 0},
        "reasons": [],
    }


def blocked_report():
    return {
        "status": "blocked", "launch_allowed": False,
        "recommended_limits": {"network_workers": 0, "browser_workers": 0},
        "sequence_limits": {"network_workers": 0, "browser_workers": 0},
        "reasons": [{"code": "fixture"}],
    }


def test_plan_rejects_production_writes_rediscovery_and_parent_drift():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        for mutate in (
            lambda value: value.update(production_writes=True),
            lambda value: value["phases"][0]["lanes"][0].update(rediscovery=True),
            lambda value: value["capacity"].update(network_ceiling=3),
        ):
            payload = json.loads(json.dumps(fixture.payload))
            mutate(payload)
            with pytest.raises(sequence.PlanError):
                fixture.load(payload)
        fixture.parent.joinpath("issues.jsonl").write_text('{"drift":true}\n', encoding="utf-8")
        with pytest.raises(sequence.PlanError, match="parent artifact"):
            fixture.load()


def test_exact_command_has_private_url_and_parent_pins_but_no_discovery_mode():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        lane = plan.phases[0].lanes[0]
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(),
        )
        facts = controller.exact_input_facts(lane)
        command = controller.build_command(lane, fixture.root / "lane-output")
        assert command[:3] == [
            str(fixture.runtime.python),
            sequence.PYTHON_NO_BYTECODE_FLAG,
            str(fixture.scripts / "parallel_observe_pure.py"),
        ]
        assert (
            controller.child_environment()[sequence.PYTHON_DONT_WRITE_BYTECODE]
            == "1"
        )
        assert facts["count"] == 2
        assert "--urls-file" in command
        assert "--parent-run-dir" in command
        assert "--derivation-reason" in command
        assert command.index("--urls-file") < command.index("--parent-run-dir") < command.index(
            "--derivation-reason"
        )
        assert command[command.index("--workers") + 1] == "2"
        assert not any(value in {"--commit", "--promote"} for value in command)
        fixture.input.write_text("https://example.test/a\n", encoding="utf-8")
        with pytest.raises(sequence.PlanError, match="SHA-256"):
            controller.exact_input_facts(lane)


def test_sealed_classification_parent_is_mutually_exclusive_verified_and_passed_to_observer():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        payload = json.loads(json.dumps(fixture.payload))
        add_sealed_classification_parent(fixture, payload)
        plan = fixture.load(payload)
        lane = plan.phases[0].lanes[0]
        assert lane.parent is None
        assert lane.sealed_classification_parent is not None
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(),
        )
        pins = controller.verify_lane_pins(lane)
        provenance = pins["sealed_classification_parent"]
        assert provenance["target_url_count"] == 2
        assert provenance["preserved_pass_count"] == 11
        assert provenance["verified_exclusion_count"] == 5
        assert "run_dir" not in provenance
        command = controller.build_command(lane, fixture.root / "lane-output")
        assert "--parent-run-dir" not in command
        assert "--sealed-classification-handoff-manifest" in command
        assert "--sealed-classification-worklist-sha256" in command
        assert "--sealed-classification-independent-audit-sha256" in command

        missing_issue_contract = json.loads(json.dumps(payload))
        missing_acceptance = missing_issue_contract["phases"][0]["lanes"][0]["acceptance"]
        missing_acceptance.pop("targeted_issue_codes_zero")
        missing_acceptance.pop("allowed_unrelated_issue_codes")
        with pytest.raises(sequence.PlanError, match="targeted issue acceptance"):
            fixture.load(missing_issue_contract)

        both = json.loads(json.dumps(payload))
        both["phases"][0]["lanes"][0]["parent"] = fixture.payload["phases"][0]["lanes"][0]["parent"]
        with pytest.raises(sequence.PlanError, match="exactly one"):
            fixture.load(both)

        weak_audit = json.loads(json.dumps(payload))
        sealed = weak_audit["phases"][0]["lanes"][0]["sealed_classification_parent"]
        audit_path = fixture.deployment / sealed["independent_audit"]["path"]
        audit_path.write_text('{"result":"approved","sources":{"TST":{}}}\n', encoding="utf-8")
        sealed["independent_audit"]["sha256"] = sha(audit_path)
        handoff_path = fixture.deployment / sealed["handoff_manifest"]["path"]
        handoff = json.loads(handoff_path.read_text())
        handoff["independent_audit_sha256"] = sha(audit_path)
        handoff_path.write_text(json.dumps(handoff) + "\n", encoding="utf-8")
        sealed["handoff_manifest"]["sha256"] = sha(handoff_path)
        with pytest.raises(sequence.PlanError, match="independent audit"):
            fixture.load(weak_audit)


@pytest.mark.parametrize(
    "options, message",
    [
        ({"invalid_url_set": True}, "URL set"),
        ({"summary_count": 1}, "source summary"),
        ({"overlap_with_worklist": True}, "partitions overlap"),
        ({"path_injection": True}, "forbidden local/runtime path"),
    ],
)
def test_sealed_classification_parent_rejects_url_set_and_summary_drift(options, message):
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        payload = json.loads(json.dumps(fixture.payload))
        add_sealed_classification_parent(fixture, payload, **options)
        with pytest.raises(sequence.PlanError, match=message):
            fixture.load(payload)


@pytest.mark.parametrize("kind", ("exact_urls", "discovery_diagnostic"))
def test_controller_child_python_preserves_deployment_closure(kind):
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        lane = plan.phases[0].lanes[0]
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(),
        )
        if kind == "discovery_diagnostic":
            lane = replace(
                lane,
                lane_id="discovery-one",
                kind="discovery_diagnostic",
                expected_requests={"total": 1, "robots": 1, "inventory": 0},
                exact_input=None,
                exact_input_count=None,
                parent=None,
                sealed_classification_parent=None,
                derivation_reason=None,
                repair_class=None,
            )
            runner = fixture.ops / "run_pure_discovery_diagnostic.py"
        else:
            runner = fixture.scripts / "parallel_observe_pure.py"
        runner.write_text(
            "from adapters.TST import source\n"
            "assert source.Config.source_name == 'example.test'\n",
            encoding="utf-8",
        )
        command = controller.build_command(lane, fixture.root / "lane-output")
        environment = controller.child_environment()
        deployment_before = deployment_snapshot(fixture.deployment)
        child = sequence.CappedChild(command, environment, fixture.root / "child.log")
        process = child.start()
        assert process.wait(timeout=10) == 0
        proof = child.finish()
        assert proof["drain_thread_quiescent"] is True
        assert command[:2] == [
            str(fixture.runtime.python), sequence.PYTHON_NO_BYTECODE_FLAG,
        ]
        assert environment[sequence.PYTHON_DONT_WRITE_BYTECODE] == "1"
        assert deployment_snapshot(fixture.deployment) == deployment_before
        assert not list(fixture.deployment.rglob("__pycache__"))
        assert not list(fixture.deployment.rglob("*.py[co]"))


def test_capped_child_drains_all_output_but_keeps_only_the_cap():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        command = [sys.executable, "-c", "import sys; sys.stdout.write('x'*100000)"]
        child = sequence.CappedChild(command, {"PATH": ""}, root / "child.log")
        with mock.patch.object(sequence, "OBSERVER_LOG_MAX_BYTES", 1024):
            process = child.start()
            assert process.wait(timeout=10) == 0
            proof = child.finish()
        assert proof["bytes_seen"] == 100000
        assert proof["bytes_kept"] == 1024
        assert proof["bytes_truncated"] == 100000 - 1024
        assert (root / "child.log").stat().st_size == 1024


def test_process_group_cleanup_stops_a_timed_out_child():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        child = sequence.CappedChild(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            {"PATH": ""}, root / "sleep.log",
        )
        process = child.start()
        proof = sequence.terminate_child(process)
        child.finish()
        assert proof["quiescent"] is True
        assert process.poll() is not None


def test_log_finish_is_bounded_when_descendant_inherits_stdout():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        parent_code = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print('parent-exit')"
        )
        child = sequence.CappedChild(
            [sys.executable, "-c", parent_code],
            {"PATH": ""}, root / "inherited.log",
        )
        process = child.start()
        assert process.wait(timeout=10) == 0
        started = time.monotonic()
        proof = child.finish(join_timeout_seconds=0.5)
        elapsed = time.monotonic() - started
        assert elapsed < 8
        assert proof["drain_thread_quiescent"] is True
        assert proof["forced_process_group_cleanup"]["quiescent"] is True


def test_blocked_start_creates_isolated_terminals_without_spawning_children():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=blocked_report,
        )
        started = time.monotonic()
        with mock.patch.object(sequence.subprocess, "Popen") as popen:
            terminal = controller.run()
        assert time.monotonic() - started < plan.max_wall_seconds
        popen.assert_not_called()
        assert terminal["status"] == "terminal_with_isolated_failures"
        assert terminal["state_counts"] == {"not_started_capacity_blocked": 1}
        lane = plan.phases[0].lanes[0]
        receipt = json.loads(
            (controller.lane_dir(lane) / "terminal_receipt.json").read_text()
        )
        assert receipt["production_writes"] is False
        assert receipt["process_quiescence_proof"]["quiescent"] is True


def test_phase_publication_gate_requires_exact_review_and_test_receipts():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        payload = json.loads(json.dumps(fixture.payload))
        phase = payload["phases"][0]
        phase["requires_reviewed_publication"] = True
        with pytest.raises(sequence.PlanError, match="publication gate"):
            fixture.load(payload)

        review = fixture.deployment / "receipts/review.json"
        tests = fixture.deployment / "receipts/tests.json"
        review.parent.mkdir(parents=True)
        review.write_text('{}\n', encoding="utf-8")
        tests.write_text('{}\n', encoding="utf-8")
        phase["publication_gate"] = {
            "status": "reviewed_published", "repository": "lin61302/scraping",
            "branch": "main", "commit": "2" * 40,
            "review_receipt": {"path": "receipts/review.json", "sha256": sha(review)},
            "test_receipt": {"path": "receipts/tests.json", "sha256": sha(tests)},
        }
        plan = fixture.load(payload)
        assert plan.phases[0].phase_id == "exact-phase"
        review.write_text('{"drift":true}\n', encoding="utf-8")
        with pytest.raises(sequence.PlanError, match="SHA-256"):
            fixture.load(payload)


def test_redirect_exact_class_requires_gate_and_reviewed_count_and_hash():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        payload = json.loads(json.dumps(fixture.payload))
        lane = payload["phases"][0]["lanes"][0]
        lane["repair_class"] = "redirect_exact"
        lane["source_id"] = "mlp:LBR:liberianinvestigator.com"
        with pytest.raises(sequence.PlanError, match="publication gate"):
            fixture.load(payload)


def test_redirect_exact_subset_requires_explicit_publication_pinned_allowed_set():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        fixture.input.write_text("https://example.test/a\n", encoding="utf-8")
        payload = json.loads(json.dumps(fixture.payload))
        lane = payload["phases"][0]["lanes"][0]
        lane["source_id"] = "mlp:SSD:eyeradio.org"
        lane["repair_class"] = "redirect_exact"
        lane["exact_input"]["sha256"] = sha(fixture.input)
        lane["exact_input_count"] = 1
        phase = payload["phases"][0]
        phase["requires_reviewed_publication"] = True
        review = fixture.deployment / "receipts/review.json"
        tests = fixture.deployment / "receipts/tests.json"
        review.parent.mkdir(parents=True)
        review.write_text('{}\n', encoding="utf-8")
        tests.write_text('{}\n', encoding="utf-8")
        phase["publication_gate"] = {
            "status": "reviewed_published", "repository": "lin61302/scraping",
            "branch": "main", "commit": "1" * 40,
            "review_receipt": {"path": "receipts/review.json", "sha256": sha(review)},
            "test_receipt": {"path": "receipts/tests.json", "sha256": sha(tests)},
            "redirect_exact_sets": [{
                "source_id": lane["source_id"], "count": 1, "sha256": sha(fixture.input),
            }],
        }
        add_sealed_classification_parent(fixture, payload, parent_classified_count=144)
        assert fixture.load(payload).phases[0].lanes[0].exact_input_count == 1
        phase["publication_gate"]["redirect_exact_sets"][0]["sha256"] = "f" * 64
        with pytest.raises(sequence.PlanError, match="reviewed exact set"):
            fixture.load(payload)


def test_acceptance_targets_parser_error_and_allows_only_listed_unrelated_codes():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        payload = json.loads(json.dumps(fixture.payload))
        add_sealed_classification_parent(fixture, payload)
        acceptance = payload["phases"][0]["lanes"][0]["acceptance"]
        acceptance["targeted_issue_codes_zero"] = ["adapter_parser_error"]
        acceptance["allowed_unrelated_issue_codes"] = ["title_missing"]
        plan = fixture.load(payload)
        lane = plan.phases[0].lanes[0]
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(),
        )
        facts = controller.exact_input_facts(lane)
        sealed_provenance = controller.verify_lane_pins(lane)["sealed_classification_parent"]
        run = fixture.root / "observed"
        run.mkdir()
        (run / "manifest.json").write_text(json.dumps({
            "status": "observed", "mode": "observation_no_production_writes",
            "source_sha256": lane.adapter.sha256,
            "counts": {"selected": 2, "passed": 0, "quarantined": 2, "skipped": 0, "exceptions": 0},
            "derived_from": sealed_provenance,
        }) + "\n", encoding="utf-8")
        (run / "urls.jsonl").write_text(
            ''.join(json.dumps({"url": url}) + "\n" for url in fixture.input.read_text().splitlines()),
            encoding="utf-8",
        )
        (run / "errors.jsonl").write_text("", encoding="utf-8")
        for name in ("articles.jsonl", "field_checks.jsonl"):
            (run / name).write_text("", encoding="utf-8")

        def write_issues(codes):
            (run / "issues.jsonl").write_text(
                ''.join(json.dumps({"code": code}) + "\n" for code in codes), encoding="utf-8",
            )

        write_issues(["title_missing"])
        accepted, details = controller.acceptance_exact(lane, run, facts)
        assert accepted is True
        assert details["checks"]["issue_code_allowlist"] is True
        write_issues(["title_missing", "adapter_parser_error"])
        accepted, details = controller.acceptance_exact(lane, run, facts)
        assert accepted is False
        assert details["checks"]["targeted_issue_code_zero:adapter_parser_error"] is False
        write_issues(["title_missing", "unexpected_issue"])
        accepted, details = controller.acceptance_exact(lane, run, facts)
        assert accepted is False
        assert details["checks"]["issue_code_allowlist"] is False
        (run / "issues.jsonl").unlink()
        accepted, details = controller.acceptance_exact(lane, run, facts)
        assert accepted is False
        assert details["checks"]["issues_artifact_present"] is False


def test_phase_batches_refill_after_isolated_failure():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        base = plan.phases[0].lanes[0]
        lanes = tuple(
            replace(base, lane_id="exact-%s" % index, workers=1)
            for index in range(3)
        )
        phase = sequence.PhaseSpec("batch-phase", 2, lanes)
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(),
        )
        controller.deadline = time.monotonic() + 30
        called = []

        def run_lane(lane, capacity):
            called.append(lane.lane_id)
            state = "isolated_lane_failure" if lane.lane_id == "exact-0" else "observed_acceptance_passed"
            return sequence.LaneResult(state, {"state": state})

        # This scheduling test supplies synthetic LaneResult objects and does
        # not create immutable lane receipts.  Receipt/accounting behavior is
        # covered separately, so isolate refill behavior from finalization.
        with mock.patch.object(controller, "run_lane", side_effect=run_lane), \
                mock.patch.object(
                    controller, "finalize_phase",
                    side_effect=lambda _phase, results, _path: list(results),
                ):
            results = controller.run_phase(phase)
        assert set(called) == {"exact-0", "exact-1", "exact-2"}
        assert called[-1] == "exact-2"
        assert [result.state for result in results].count("isolated_lane_failure") == 1
        assert [result.state for result in results].count("observed_acceptance_passed") == 2


def test_duplicate_json_keys_are_rejected_before_plan_admission():
    with pytest.raises(sequence.PlanError, match="duplicate JSON key"):
        sequence.json_no_duplicates('{"production_writes":false,"production_writes":true}')


def test_resumed_lane_terminal_rejects_partial_content_and_accounting():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        lane = plan.phases[0].lanes[0]
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(), resume=True,
        )
        terminal = controller.lane_dir(lane) / "terminal_receipt.json"
        terminal.parent.mkdir(parents=True)
        terminal.write_text(json.dumps({
            "schema_version": 1,
            "kind": "exact_observation_lane_terminal",
            "terminal": True,
            "state": "observed_acceptance_passed",
            "sequence_id": plan.sequence_id,
            "phase_id": lane.phase_id,
            "lane_id": lane.lane_id,
            "source_id": lane.source_id,
            "plan_sha256": plan.plan_sha256,
            "production_writes": False,
            "verified_completion": False,
            "tracker_boundary_advancement": False,
            "process_quiescence_proof": {"quiescent": True},
        }) + "\n", encoding="utf-8")
        with pytest.raises(sequence.PlanError, match="start evidence"):
            controller.run_lane(lane, green_report())


def test_resumed_phase_and_sequence_terminals_reject_partial_hashes():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=blocked_report,
        )
        finished = controller.run()
        assert finished["status"] == "terminal_with_isolated_failures"
        phase = plan.phases[0]
        phase_path = (
            controller.sequence_dir / "phases" / sequence.safe_name(phase.phase_id)
            / "terminal_receipt.json"
        )
        phase_original = phase_path.read_text(encoding="utf-8")
        phase_receipt = json.loads(phase_original)
        phase_receipt.pop("lane_terminal_sha256")
        phase_path.write_text(json.dumps(phase_receipt) + "\n", encoding="utf-8")
        with pytest.raises(sequence.PlanError, match="accounting or hashes"):
            controller.validate_phase_terminal(phase, phase_path)
        phase_path.write_text(phase_original, encoding="utf-8")
        sequence_path = controller.sequence_dir / "terminal_receipt.json"
        sequence_receipt = json.loads(sequence_path.read_text(encoding="utf-8"))
        sequence_receipt["lane_count"] = 0
        sequence_path.write_text(json.dumps(sequence_receipt) + "\n", encoding="utf-8")
        with pytest.raises(sequence.PlanError):
            controller.validate_sequence_terminal(sequence_path)


def test_stale_start_is_validated_before_process_cleanup_or_terminalization():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan = fixture.load()
        lane = plan.phases[0].lanes[0]
        controller = sequence.SequenceController(
            plan, fixture.runtime, capacity_reporter=lambda: green_report(), resume=True,
        )
        start = controller.lane_dir(lane) / "start_receipt.json"
        start.parent.mkdir(parents=True)
        start.write_text(json.dumps({
            "schema_version": 1,
            "kind": "exact_observation_lane_start",
            "sequence_id": plan.sequence_id,
            "lane_id": lane.lane_id,
            "pid": 999999,
            "production_writes": False,
        }) + "\n", encoding="utf-8")
        with mock.patch.object(sequence, "quiescence_proof") as proof:
            with pytest.raises(sequence.PlanError, match="identity or write contract"):
                controller.run_lane(lane, green_report())
        proof.assert_not_called()
        assert not (controller.lane_dir(lane) / "terminal_receipt.json").exists()


def test_stale_start_does_not_treat_an_orphan_process_group_as_quiescent():
    with mock.patch.object(sequence, "process_command", side_effect=[None, None]), \
            mock.patch.object(sequence, "process_group_present", side_effect=[True, True]), \
            mock.patch.object(sequence, "terminate_group") as terminate:
        proof = sequence.quiescence_proof(43210, ("observer.py", "lane-output"))
    terminate.assert_not_called()
    assert proof["process_group_present_after"] is True
    assert proof["quiescent"] is False
