"""Contracts for the local, observation-only ten-country controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from ops import launch_local_country_observation_once as launcher  # noqa: E402
from ops import local_unattended_country_observation as controller  # noqa: E402
from ops.local_unattended_country_observation import (  # noqa: E402
    CampaignController,
    ExecutionResult,
    PlanError,
    PureObserverExecutor,
    RuntimePaths,
    adapter_bundle_sha256,
    build_observer_command,
    execution_bundle_sha256,
    external_dependency_errors,
    load_plan,
    sha256_path,
)


def test_controller_source_is_compatible_with_python_3_8_runtime_surface():
    source = Path(controller.__file__).read_text(encoding="utf-8")
    assert ".removeprefix(" not in source
    assert "sys.stdlib_module_names" not in source


OBSERVER = r'''#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--automation-root")
p.add_argument("--source-file")
p.add_argument("--start")
p.add_argument("--end")
p.add_argument("--output-root")
p.add_argument("--workers", type=int)
a = p.parse_args()
if os.environ.get("SCRAPER_BOUNDED_OBSERVATION") != "1":
    raise SystemExit(41)
source = Path(a.source_file)
text = source.read_text()
if "LOUD = True" in text:
    sys.stdout.buffer.write(b"x" * 100_000)
    sys.stdout.buffer.flush()
if "SPAWN_AND_SLEEP = True" in text:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (Path(a.output_root) / "child.pid").write_text(str(child.pid))
    time.sleep(60)
if "FAIL = True" in text:
    raise SystemExit(42)
if "MUTATE_DURING_RUN = True" in text:
    source.write_text(text + "\n# mutated by observer\n")
run = Path(a.output_root) / ("observe-parallel-test-" + uuid.uuid4().hex[:8])
run.mkdir(parents=True)
source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
bundle = hashlib.sha256(source.name.encode() + b"\0" + source.read_bytes() + b"\0").hexdigest()
manifest = {
    "status": "observed",
    "mode": "observation_no_production_writes",
    "adapter_contract": "pure_network_only",
    "source_sha256": source_hash,
    "source_bundle_sha256": bundle,
    "requested_start": a.start,
    "requested_end": a.end,
    "observer_execution": {"workers": a.workers},
    "counts": {"passed": 1, "exceptions": 0, "quarantined": 0},
    "bounded_observation_environment": os.environ["SCRAPER_BOUNDED_OBSERVATION"],
}
(run / "manifest.json").write_text(json.dumps(manifest) + "\n")
'''


def adapter_text(domain: str, country: str, extra: str = "") -> str:
    return f'''from datetime import date

FETCH_START = ACCEPT_START = date(2026, 8, 1)
FIRST_PASS_TARGET = date(2026, 9, 30)

class Config:
    source_name = "{domain}"
    country_code = "{country}"
    is_env = False
    production_writes = False

{extra}

def discover_urls(start, end, evidence):
    return []

def scrape_url(url):
    return {{"url": url}}
'''


class Fixture:
    def __init__(self, root: Path, *, two_first: bool = False, first_extra: str = ""):
        self.root = root
        self.auto = root / "automation"
        self.auto.mkdir()
        scripts = self.auto / "scripts"
        scripts.mkdir()
        self.observer = scripts / "parallel_observe_pure.py"
        self.observer.write_text(OBSERVER, encoding="utf-8")
        self.sessions: list[dict] = []
        self.sources = []
        for index in range(10):
            country = f"C{index:02d}"
            directory = self.auto / "adapters" / country
            directory.mkdir(parents=True)
            rows = []
            count = 2 if index == 0 and two_first else 1
            for item in range(count):
                domain = f"source-{index}-{item}.example"
                adapter = directory / f"source_{index}_{item}.py"
                extra = first_extra if index == 0 and item == 0 else ""
                adapter.write_text(adapter_text(domain, country, extra), encoding="utf-8")
                files = [adapter]
                row = {
                    "source_id": f"mlp:{country}:{domain}",
                    "adapter_repository_path": adapter.relative_to(self.auto).as_posix(),
                    "adapter_sha256": sha256_path(adapter),
                    "source_bundle_sha256": adapter_bundle_sha256(adapter),
                    "execution_bundle_files": [
                        {
                            "repository_path": path.relative_to(self.auto).as_posix(),
                            "sha256": sha256_path(path),
                        }
                        for path in files
                    ],
                    "execution_bundle_sha256": execution_bundle_sha256(files, self.auto),
                    "start": "2026-08-01", "end": "2026-09-30",
                    "resource_class": "network", "production_writes": False,
                    "timeout_seconds": 1,
                }
                rows.append(row)
                self.sources.append(row)
            self.sessions.append({
                "session_id": f"country-{index}", "country": country,
                "execution": "sequential_sources", "sources": rows,
            })
        observer_files = [self.observer]
        self.payload = {
            "schema_version": 1, "campaign_id": "local-fixture-campaign",
            "immutable": True, "mode": "pure_observation_only",
            "production_writes": False, "first_pass_target_end": "2026-09-30",
            "source_count": len(self.sources), "max_wall_seconds": 30,
            "concurrency": {
                "network": 4, "browser": 0, "max_active_country_sessions": 4,
            },
            "observer": {
                "repository_path": self.observer.relative_to(self.auto).as_posix(),
                "sha256": sha256_path(self.observer),
                "execution_bundle_files": [
                    {
                        "repository_path": path.relative_to(self.auto).as_posix(),
                        "sha256": sha256_path(path),
                    }
                    for path in observer_files
                ],
                "execution_bundle_sha256": execution_bundle_sha256(observer_files, self.auto),
            },
            "sessions": self.sessions,
        }
        self.runtime = RuntimePaths(
            python=Path(sys.executable), automation_root=self.auto,
            output_root=root / "runs", receipt_root=root / "receipts",
            host_lock_path=root / "locks/controller.lock", min_free_gb=0,
            network_sessions=4,
        )

    def write_plan(self, payload: dict | None = None) -> tuple[Path, str]:
        path = self.root / "plan.json"
        path.write_text(json.dumps(payload or self.payload, indent=2) + "\n", encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def plan(self):
        path, digest = self.write_plan()
        return path, digest, load_plan(path, digest, self.auto)


def green_report(network: int = 4) -> dict[str, object]:
    return {
        "status": "green" if network == 4 else "reduced",
        "launch_allowed": True,
        "recommended_limits": {"network_workers": network, "browser_workers": 0},
        "reasons": [],
    }


def test_capacity_report_uses_automation_browser_markers_only():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        metrics = {"fixture": True}
        expected = green_report(4)
        with (
            patch.object(controller, "collect_metrics", return_value=metrics) as collect,
            patch.object(controller, "evaluate_capacity", return_value=expected),
        ):
            assert controller.host_capacity_report(fixture.runtime) == expected
        patterns = collect.call_args.kwargs["browser_patterns"]
        assert patterns == (
            "chromedriver", "geckodriver", "playwright", "--headless",
            "--remote-debugging-port", "selenium",
        )
        assert "Google Chrome" not in patterns


def test_external_dependency_check_ignores_local_relative_imports_and_unreachable_legacy_core():
    with tempfile.TemporaryDirectory() as directory:
        automation = Path(directory) / "automation"
        control = automation / "scraper_control"
        control.mkdir(parents=True)
        (control / "validation.py").write_text("VALUE = 1\n", encoding="utf-8")
        observer = control / "observe.py"
        observer.write_text(
            "from .validation import VALUE\n"
            "def legacy_fallback():\n"
            "    import core.base_pipeline\n",
            encoding="utf-8",
        )
        assert external_dependency_errors([observer], automation) == []

        adapter = automation / "adapter.py"
        adapter.write_text("import dependency_that_does_not_exist_1018\n", encoding="utf-8")
        assert external_dependency_errors([adapter], automation) == [
            "missing_python_dependency:dependency_that_does_not_exist_1018"
        ]


def test_plan_requires_exact_ten_country_network_only_contract():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        path, digest, plan = fixture.plan()
        assert len(plan.sessions) == 10
        assert all(session.country != plan.sessions[0].country for session in plan.sessions[1:])

        for mutate in (
            lambda payload: payload["sessions"].pop(),
            lambda payload: payload["concurrency"].update(browser=1),
            lambda payload: payload["concurrency"].update(network=3),
            lambda payload: payload.update(production_writes=True),
            lambda payload: payload["sessions"][0]["sources"][0].update(resource_class="browser"),
        ):
            payload = json.loads(json.dumps(fixture.payload))
            mutate(payload)
            bad, bad_digest = fixture.write_plan(payload)
            with pytest.raises(PlanError):
                load_plan(bad, bad_digest, fixture.auto)
        with pytest.raises(PlanError):
            load_plan(path, "0" * 64, fixture.auto)


def test_plan_window_must_match_static_adapter_dates():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        payload = json.loads(json.dumps(fixture.payload))
        payload["sessions"][0]["sources"][0]["start"] = "2026-08-02"
        path, digest = fixture.write_plan(payload)
        with pytest.raises(PlanError, match="ACCEPT_START/FIRST_PASS_TARGET"):
            load_plan(path, digest, fixture.auto)


def test_observer_command_and_environment_are_bounded_and_write_disabled():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        _, _, plan = fixture.plan()
        source = plan.sessions[0].sources[0]
        command = build_observer_command(fixture.runtime, plan, source, fixture.root / "out")
        assert command[command.index("--workers") + 1] == "1"
        assert not any(token in {"--commit", "--promote"} for token in command)
        source_dir = fixture.root / "source-receipt"
        source_dir.mkdir()
        result = PureObserverExecutor(fixture.runtime, plan)(
            source, source_dir, time.monotonic() + 10,
        )
        assert result.state == "observed_terminal"
        manifest = json.loads(Path(result.details["manifest"]["path"]).read_text())
        assert manifest["bounded_observation_environment"] == "1"
        assert result.details["command_contract"]["bounded_observation_environment"] == {
            "SCRAPER_BOUNDED_OBSERVATION": "1"
        }


def test_child_environment_is_allowlisted_and_excludes_credentials():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        _, _, plan = fixture.plan()
        source = plan.sessions[0].sources[0]
        source_dir = fixture.root / "source-receipt"
        source_dir.mkdir()
        captured: dict[str, str] = {}
        real_popen = controller.subprocess.Popen

        def popen(*args, **kwargs):
            captured.update(kwargs["env"])
            return real_popen(*args, **kwargs)

        with (
            patch.dict(os.environ, {
                "DATABASE_URL": "must-not-pass",
                "OPENAI_API_KEY": "must-not-pass",
                "HTTPS_PROXY": "http://proxy.example:8080",
                "PYTHONPATH": "/unreviewed/path",
            }),
            patch.object(controller.subprocess, "Popen", side_effect=popen),
        ):
            result = PureObserverExecutor(fixture.runtime, plan)(
                source, source_dir, time.monotonic() + 10,
            )
        assert result.state == "observed_terminal"
        assert captured["HTTPS_PROXY"] == "http://proxy.example:8080"
        assert captured["PYTHONPATH"] == str(fixture.auto)
        assert captured["SCRAPER_BOUNDED_OBSERVATION"] == "1"
        assert "DATABASE_URL" not in captured
        assert "OPENAI_API_KEY" not in captured


def test_observer_log_is_capped_while_all_output_is_drained():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory), first_extra="LOUD = True")
        _, _, plan = fixture.plan()
        source = plan.sessions[0].sources[0]
        source_dir = fixture.root / "source-receipt"
        source_dir.mkdir()
        with patch.object(controller, "OBSERVER_LOG_MAX_BYTES", 1024):
            result = PureObserverExecutor(fixture.runtime, plan)(
                source, source_dir, time.monotonic() + 10,
            )
        log = result.details["observer_log"]
        assert result.state == "observed_terminal"
        assert log["max_bytes"] == 1024
        assert log["bytes_seen"] == 100_000
        assert log["bytes_kept"] == 1024
        assert log["bytes_truncated"] == 100_000 - 1024
        assert Path(log["path"]).stat().st_size == 1024


def test_execution_pin_drift_fails_before_spawning_child():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        _, _, plan = fixture.plan()
        source = plan.sessions[0].sources[0]
        source.adapter.write_text(source.adapter.read_text() + "\n# drift\n", encoding="utf-8")
        source_dir = fixture.root / "source-receipt"
        source_dir.mkdir()
        with patch.object(controller.subprocess, "Popen") as popen:
            result = PureObserverExecutor(fixture.runtime, plan)(
                source, source_dir, time.monotonic() + 10,
            )
        popen.assert_not_called()
        assert result.state == "isolated_source_failure"
        assert result.details["contract_error"] == "execution_pin_verification_failed_before_run"
        assert result.details["execution_pin_verification"]["before_run"]["passed"] is False


def test_execution_pins_are_rechecked_after_child_exits():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory), first_extra="MUTATE_DURING_RUN = True")
        _, _, plan = fixture.plan()
        source = plan.sessions[0].sources[0]
        source_dir = fixture.root / "source-receipt"
        source_dir.mkdir()
        result = PureObserverExecutor(fixture.runtime, plan)(
            source, source_dir, time.monotonic() + 10,
        )
        assert result.state == "isolated_source_failure"
        assert result.details["contract_error"] == "execution_pin_verification_failed_after_run"
        assert result.details["execution_pin_verification"]["before_run"]["passed"] is True
        assert result.details["execution_pin_verification"]["after_run"]["passed"] is False


def test_timeout_kills_observer_process_group_and_isolates_source():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory), first_extra="SPAWN_AND_SLEEP = True")
        _, _, plan = fixture.plan()
        source = plan.sessions[0].sources[0]
        source_dir = fixture.root / "source-receipt"
        source_dir.mkdir()
        started = time.monotonic()
        result = PureObserverExecutor(fixture.runtime, plan)(
            source, source_dir, time.monotonic() + 5,
        )
        assert result.state == "isolated_source_timeout"
        assert time.monotonic() - started < 15


def test_four_country_sessions_run_concurrently_and_sources_stay_sequential():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory), two_first=True)
        _, _, plan = fixture.plan()
        lock = threading.Lock()
        active = 0
        peak = 0
        periods: dict[str, tuple[float, float]] = {}
        capacity_checks: list[int] = []

        def execute(source, source_dir, deadline):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                began = time.monotonic()
            time.sleep(0.04)
            ended = time.monotonic()
            with lock:
                periods[source.source_id] = (began, ended)
                active -= 1
            if source.source_id.endswith("source-1-0.example"):
                return ExecutionResult("isolated_source_failure", {"fixture": True})
            return ExecutionResult("observed_terminal", {"fixture": True})

        def capacity_report():
            with lock:
                sequence = len(capacity_checks) + 1
                capacity_checks.append(sequence)
            return {**green_report(4), "sequence": sequence}

        controller = CampaignController(
            plan, fixture.runtime, executor=execute,
            capacity_reporter=capacity_report,
        )
        terminal = controller.run()
        assert terminal["status"] == "terminal_with_isolated_failures"
        assert terminal["counts"]["sources"] == 11
        assert terminal["counts"]["isolated_failures"] == 1
        assert 2 <= peak <= 4
        first = "mlp:C00:source-0-0.example"
        second = "mlp:C00:source-0-1.example"
        assert periods[second][0] >= periods[first][1]
        assert len(capacity_checks) == 12  # one per source, then one terminal report
        assert terminal["terminal_capacity_report"]["sequence"] == 12
        startup = json.loads(
            (fixture.runtime.receipt_root / plan.campaign_id / "startup_receipt.json").read_text()
        )
        assert startup["capacity"] == {
            "country_sessions": 4, "network": 4, "browser": 0,
            "plan_network_ceiling": 4,
        }


def test_reduced_capacity_uses_two_country_sessions_and_blocked_starts_none():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        fixture.runtime = RuntimePaths(
            **{**fixture.runtime.__dict__, "network_sessions": 2}
        )
        _, _, plan = fixture.plan()
        active = 0
        peak = 0
        lock = threading.Lock()

        def execute(source, source_dir, deadline):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return ExecutionResult("observed_terminal", {})

        result = CampaignController(
            plan, fixture.runtime, executor=execute,
            capacity_reporter=lambda: green_report(2),
        ).run()
        assert result["status"] == "terminal_observation_campaign"
        assert peak == 2

    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        _, _, plan = fixture.plan()
        invoked = []
        blocked = {
            "status": "blocked", "launch_allowed": False,
            "recommended_limits": {"network_workers": 0, "browser_workers": 0},
            "reasons": [{"code": "fixture"}],
        }
        result = CampaignController(
            plan, fixture.runtime,
            executor=lambda *args: invoked.append(args),
            capacity_reporter=lambda: blocked,
        ).run()
        assert not invoked
        assert result["status"] == "terminal_with_isolated_failures"
        assert result["counts"]["isolated_failures"] == 10


def test_macos_ps_parser_finds_only_campaign_workers():
    snapshot = launcher.parse_ps_snapshot(
        """
          101     1 S       00:02 /usr/bin/python local_unattended_country_observation.py --plan x
          102   101 S       00:01 /usr/bin/python parallel_observe_pure.py --workers 1
          103     1 S       00:01 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
          104     1 S       00:01 /usr/local/bin/chromedriver
          105     1 S       00:01 /Applications/Google Chrome --headless --remote-debugging-port=0
        """,
        ignored_pids=(101,),
    )
    assert snapshot["local_controllers"] == []
    assert [row["pid"] for row in snapshot["network_observers"]] == [102]
    assert [row["pid"] for row in snapshot["browser_workers"]] == [104, 105]


class FakeProcess:
    def __init__(self, pid: int = 8123):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.parametrize("recommended", (4, 2))
def test_launcher_is_single_use_and_passes_guard_network_limit(recommended):
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan_path, plan_sha, plan = fixture.plan()
        attempt = fixture.root / "launch/attempt.json"
        receipt = fixture.root / "launch/receipt.json"
        log = fixture.root / "launch/controller.log"
        args = argparse.Namespace(
            plan=plan_path, plan_sha256=plan_sha,
            controller_sha256=sha256_path(launcher.CONTROLLER_PATH),
            launcher_sha256=sha256_path(launcher.LAUNCHER_PATH),
            capacity_guard_sha256=sha256_path(launcher.CAPACITY_GUARD_PATH),
            automation_root=fixture.auto, output_root=fixture.runtime.output_root,
            receipt_root=fixture.runtime.receipt_root,
            host_lock_path=fixture.runtime.host_lock_path,
            launch_lock_path=fixture.root / "locks/launch.lock",
            attempt_marker=attempt, launch_receipt=receipt, controller_log=log,
            python=Path(sys.executable), min_free_gb=0,
            startup_timeout_seconds=2.0, no_caffeinate=True,
        )
        startup = fixture.runtime.receipt_root / plan.campaign_id / "startup_receipt.json"

        def popen(*popen_args, **popen_kwargs):
            startup.parent.mkdir(parents=True, exist_ok=True)
            startup.write_text(json.dumps({
                "campaign_id": plan.campaign_id, "plan_sha256": plan.plan_sha256,
                "production_writes": False, "pid": 991,
                "capacity": {
                    "country_sessions": recommended, "network": recommended,
                    "browser": 0, "plan_network_ceiling": 4,
                },
            }) + "\n")
            return FakeProcess()

        snapshot = {
            "storage": {"free_bytes": 100 * 1024**3}, "load_averages": [0.1, 0.1, 0.1],
            "cpu_count": 10,
            "processes": {
                "local_controllers": [], "network_observers": [], "browser_workers": [],
            },
        }
        with (
            patch.object(launcher, "host_capacity_report", return_value=green_report(recommended)),
            patch.object(launcher, "capacity_snapshot", return_value=snapshot),
            patch.object(launcher.subprocess, "Popen", side_effect=popen),
        ):
            result = launcher.launch(args)
        assert result["status"] == "launched_unattended"
        assert result["network_sessions"] == recommended
        assert result["command"][-2:] == ["--network-sessions", str(recommended)]
        assert attempt.is_file() and receipt.is_file()
        with pytest.raises(launcher.LaunchError, match="attempt marker already exists"):
            with patch.object(launcher, "host_capacity_report", return_value=green_report(recommended)):
                launcher.launch(args)


def test_launcher_refuses_blocked_shared_capacity_guard_before_attempt():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        plan_path, plan_sha, _ = fixture.plan()
        attempt = fixture.root / "launch/attempt.json"
        args = argparse.Namespace(
            plan=plan_path, plan_sha256=plan_sha,
            controller_sha256=sha256_path(launcher.CONTROLLER_PATH),
            launcher_sha256=sha256_path(launcher.LAUNCHER_PATH),
            capacity_guard_sha256=sha256_path(launcher.CAPACITY_GUARD_PATH),
            automation_root=fixture.auto, output_root=fixture.runtime.output_root,
            receipt_root=fixture.runtime.receipt_root,
            host_lock_path=fixture.runtime.host_lock_path,
            launch_lock_path=fixture.root / "locks/launch.lock",
            attempt_marker=attempt, launch_receipt=fixture.root / "launch/receipt.json",
            controller_log=fixture.root / "launch/controller.log",
            python=Path(sys.executable), min_free_gb=0,
            startup_timeout_seconds=2.0, no_caffeinate=True,
        )
        report = {
            "status": "blocked", "launch_allowed": False,
            "recommended_limits": {"network_workers": 0, "browser_workers": 0},
            "reasons": [{"code": "fixture_block"}],
        }
        original_guard_hash = args.capacity_guard_sha256
        args.capacity_guard_sha256 = "0" * 64
        with pytest.raises(launcher.LaunchError, match="capacity guard .* changed"):
            launcher.launch(args)
        args.capacity_guard_sha256 = original_guard_hash
        with patch.object(launcher, "host_capacity_report", return_value=report):
            with pytest.raises(launcher.LaunchError, match="blocked launch"):
                launcher.launch(args)
        assert not attempt.exists()
