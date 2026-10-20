"""Offline contracts for the single-use local exact-sequence launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from ops import launch_local_exact_sequence_once as launcher  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def green_capacity() -> dict[str, object]:
    return {
        "status": "reduced",
        "launch_allowed": True,
        "recommended_limits": {"network_workers": 2, "browser_workers": 0},
        "sequence_limits": {"network_workers": 2, "browser_workers": 0},
        "metrics": {"scrape_processes": 0, "browser_processes": 0},
        "reasons": [],
    }


def green_process_snapshot(own: int = 2) -> dict[str, object]:
    return {
        "scan_ok": True,
        "own_network_processes": own,
        "foreign_network_processes": 0,
        "browser_processes": 0,
    }


def inspect_now(process, startup_path: Path, terminal_path: Path):
    startup = launcher._load_json(startup_path) if startup_path.is_file() else None
    terminal = launcher._load_json(terminal_path) if terminal_path.is_file() else None
    return startup, terminal, process.poll()


class FakeProcess:
    def __init__(self, pid: int = 424242, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return self.exit_code


class LauncherFixture:
    """One exact URL and one parent, with no runnable adapter behavior."""

    def __init__(self, root: Path):
        self.root = private_dir(root)
        self.deployment_sha256 = "d" * 64
        self.deployment = private_dir(
            root / "deployments" / self.deployment_sha256,
        )
        self.automation = private_dir(self.deployment / "automation")
        self.ops = private_dir(self.automation / "ops")
        self.scripts = private_dir(self.automation / "scripts")
        self.adapters = private_dir(self.automation / "adapters/TST")
        self.output = private_dir(root / "runtime/output")
        self.receipts = private_dir(root / "runtime/receipts")
        self.operation = private_dir(root / "runtime/operation")
        self.logs = private_dir(root / "runtime/logs")
        self.locks = private_dir(root / "runtime/locks")
        self.parents = private_dir(root / "parents")

        deployed_controller = self.ops / launcher.CONTROLLER_PATH.name
        deployed_guard = self.ops / launcher.CAPACITY_GUARD_PATH.name
        shutil.copyfile(launcher.CONTROLLER_PATH, deployed_controller)
        shutil.copyfile(launcher.CAPACITY_GUARD_PATH, deployed_guard)
        diagnostic = self.ops / "run_pure_discovery_diagnostic.py"
        observer = self.scripts / "parallel_observe_pure.py"
        diagnostic.write_text("# pinned offline diagnostic\n", encoding="utf-8")
        observer.write_text("# pinned offline observer\n", encoding="utf-8")
        self.execution_paths = (
            deployed_controller, diagnostic, deployed_guard, observer,
        )
        self.non_execution_import = self.automation / "adapters/__init__.py"
        self.non_execution_import.write_text(
            '"""Deployed package marker outside the plan execution list."""\n',
            encoding="utf-8",
        )

        self.adapter = self.adapters / "source.py"
        self.adapter.write_text(
            "class Config:\n"
            "    source_name = 'example.test'\n"
            "    country_code = 'TST'\n"
            "    production_writes = False\n"
            "def discover_urls(start, end, evidence):\n"
            "    return []\n"
            "def scrape_url(url):\n"
            "    return {}\n",
            encoding="utf-8",
        )
        self.exact_input = write_private(
            self.deployment / "private/exact.txt", "https://example.test/one\n",
        )
        self.parent = private_dir(self.parents / "observe-parallel-parent")
        write_private(
            self.parent / "manifest.json",
            json.dumps({"status": "observed", "run_id": "parent-fixture"}) + "\n",
        )
        for name in ("urls.jsonl", "field_checks.jsonl", "issues.jsonl"):
            write_private(self.parent / name, "{}\n")
        parent_pins = {
            name: sha(self.parent / name)
            for name in ("manifest.json", "urls.jsonl", "field_checks.jsonl", "issues.jsonl")
        }
        execution = [
            {
                "path": path.relative_to(self.deployment).as_posix(),
                "sha256": sha(path),
            }
            for path in self.execution_paths
        ]
        payload = {
            "schema_version": 1,
            "mode": "unattended_exact_observation_sequence",
            "sequence_id": "local-exact-launcher-fixture",
            "production_writes": False,
            "continue_after_isolated_lane_failure": True,
            "provenance": {
                "repository": "lin61302/scraping",
                "branch": "main",
                "code_git_commit": "1" * 40,
            },
            "capacity": {
                "network_ceiling": 2,
                "browser_ceiling": 0,
                "max_wall_seconds": 60,
                "log_max_bytes": 50 * 1024 * 1024,
            },
            "execution_files": execution,
            "phases": [{
                "phase_id": "exact-phase",
                "production_writes": False,
                "max_active_lanes": 1,
                "lanes": [{
                    "lane_id": "exact-one",
                    "kind": "exact_urls",
                    "source_id": "mlp:TST:example.test",
                    "production_writes": False,
                    "adapter": {
                        "path": self.adapter.relative_to(self.deployment).as_posix(),
                        "sha256": sha(self.adapter),
                    },
                    "start": "2026-09-01",
                    "end": "2026-09-30",
                    "workers": 2,
                    "timeout_seconds": 30,
                    "rediscovery": False,
                    "repair_class": "parser_exact",
                    "exact_input": {
                        "path": self.exact_input.relative_to(self.deployment).as_posix(),
                        "sha256": sha(self.exact_input),
                    },
                    "exact_input_count": 1,
                    "parent": {
                        "run_dir": str(self.parent),
                        "artifacts": parent_pins,
                    },
                    "derivation_reason": "offline launcher fixture",
                    "acceptance": {"manifest_status": "observed"},
                }],
            }],
        }
        self.plan = write_private(
            self.deployment / "plan.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        self.plan_sha256 = sha(self.plan)
        for directory, _, _ in os.walk(self.deployment):
            Path(directory).chmod(0o700)
        for path in self.deployment.rglob("*"):
            if path.is_file():
                path.chmod(0o600 if path in (self.plan, self.exact_input) else 0o644)
        files = []
        for path in sorted(item for item in self.deployment.rglob("*") if item.is_file()):
            mode = stat.S_IMODE(path.stat().st_mode)
            files.append({
                "source_path": path.relative_to(self.root).as_posix(),
                "deployment_relative_path": path.relative_to(self.deployment).as_posix(),
                "sha256": sha(path),
                "bytes": path.stat().st_size,
                "private": mode & 0o077 == 0,
                "mode": "%04o" % mode,
                "deployed_path": str(path),
            })
        old_deployment = self.deployment
        self.deployment_sha256 = launcher.deployment_digest(files)
        self.deployment = old_deployment.with_name(self.deployment_sha256)
        old_deployment.rename(self.deployment)

        def remap(path: Path) -> Path:
            return self.deployment / path.relative_to(old_deployment)

        self.automation = remap(self.automation)
        self.ops = remap(self.ops)
        self.scripts = remap(self.scripts)
        self.adapters = remap(self.adapters)
        self.execution_paths = tuple(remap(path) for path in self.execution_paths)
        self.non_execution_import = remap(self.non_execution_import)
        self.adapter = remap(self.adapter)
        self.exact_input = remap(self.exact_input)
        self.plan = remap(self.plan)
        deployed_controller = self.ops / launcher.CONTROLLER_PATH.name
        files = []
        for path in sorted(item for item in self.deployment.rglob("*") if item.is_file()):
            mode = stat.S_IMODE(path.stat().st_mode)
            files.append({
                "source_path": path.relative_to(self.root).as_posix(),
                "deployment_relative_path": path.relative_to(self.deployment).as_posix(),
                "sha256": sha(path),
                "bytes": path.stat().st_size,
                "private": mode & 0o077 == 0,
                "mode": "%04o" % mode,
                "deployed_path": str(path),
            })
        self.controller_log = self.logs / "controller.log"
        manifest_payload = {
            "schema_version": 1,
            "kind": "private_local_exact_sequence_fallback_deployment",
            "mode": "content_addressed_local_exact_sequence_deployment",
            "sequence_id": payload["sequence_id"],
            "host_class": "local_darwin",
            "transport": "local",
            "production_writes": False,
            "repository": "lin61302/scraping",
            "branch": "main",
            "code_git_commit": payload["provenance"]["code_git_commit"],
            "network_ceiling": 2,
            "browser_ceiling": 0,
            "deployment_sha256": self.deployment_sha256,
            "controller_sha256": sha(deployed_controller),
            "plan_sha256": self.plan_sha256,
            "deployment_root_local": str(self.deployment),
            "automation_root_local": str(self.automation),
            "controller_local": str(deployed_controller),
            "plan_local": str(self.plan),
            "python_local": str(Path(sys.executable).absolute()),
            "output_root_local": str(self.output),
            "receipt_root_local": str(self.receipts),
            "log_root_local": str(self.logs),
            "lock_root_local": str(self.locks),
            "attempt_root_local": str(self.operation),
            "host_lock_local": str(self.locks / "exact.lock"),
            "attempt_marker_local": str(self.operation / "attempt_receipt.json"),
            "launch_receipt_local": str(self.operation / "launch_receipt.json"),
            "controller_log_local": str(self.controller_log),
            "allowed_parent_roots_local": [str(self.parents)],
            "files": files,
        }
        self.deployment_manifest = write_private(
            self.root / "local_deployment_manifest.private.json",
            json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        )
        self.deployment_manifest_sha256 = sha(self.deployment_manifest)

    def args(self, **updates) -> argparse.Namespace:
        values = {
            "plan": self.plan,
            "plan_sha256": self.plan_sha256,
            "deployment_manifest": self.deployment_manifest,
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
            "deployment_sha256": self.deployment_sha256,
            "controller_sha256": sha(launcher.CONTROLLER_PATH),
            "launcher_sha256": sha(launcher.LAUNCHER_PATH),
            "capacity_guard_sha256": sha(launcher.CAPACITY_GUARD_PATH),
            "workspace_root": self.root,
            "deployment_root": self.deployment,
            "automation_root": self.automation,
            "output_root": self.output,
            "receipt_root": self.receipts,
            "host_lock_path": self.locks / "exact.lock",
            "allowed_parent_root": [self.parents],
            "operation_root": self.operation,
            "controller_log": self.controller_log,
            "python": Path(sys.executable),
            "preflight_only": False,
            "no_caffeinate": True,
        }
        values.update(updates)
        return argparse.Namespace(**values)

    def argv(self) -> list[str]:
        args = self.args()
        return [
            "--plan", str(args.plan),
            "--plan-sha256", args.plan_sha256,
            "--deployment-manifest", str(args.deployment_manifest),
            "--deployment-manifest-sha256", args.deployment_manifest_sha256,
            "--deployment-sha256", args.deployment_sha256,
            "--controller-sha256", args.controller_sha256,
            "--launcher-sha256", args.launcher_sha256,
            "--capacity-guard-sha256", args.capacity_guard_sha256,
            "--workspace-root", str(args.workspace_root),
            "--deployment-root", str(args.deployment_root),
            "--automation-root", str(args.automation_root),
            "--output-root", str(args.output_root),
            "--receipt-root", str(args.receipt_root),
            "--host-lock-path", str(args.host_lock_path),
            "--allowed-parent-root", str(args.allowed_parent_root[0]),
            "--operation-root", str(args.operation_root),
            "--controller-log", str(args.controller_log),
            "--python", str(args.python),
            "--no-caffeinate",
        ]

    def startup_receipt(self, pid: int = 424242) -> dict[str, object]:
        execution = {
            str(index): {"sha256": sha(path), "bytes": path.stat().st_size}
            for index, path in enumerate(self.execution_paths)
        }
        return {
            "schema_version": 1,
            "kind": "exact_observation_sequence_startup",
            "sequence_id": "local-exact-launcher-fixture",
            "plan_sha256": self.plan_sha256,
            "code_git_commit": "1" * 40,
            "execution_pin_verification": execution,
            "capacity": {
                "network": 2,
                "browser": 0,
                "log_max_bytes": 50 * 1024 * 1024,
            },
            "host_capacity_guard": green_capacity(),
            "production_writes": False,
            "pid": pid,
        }


def test_preflight_is_read_only_and_pins_exact_contract(tmp_path):
    fixture = LauncherFixture(tmp_path)
    with mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()):
        preflight = launcher.public_preflight(launcher.prepare(fixture.args()))
    assert preflight["status"] == "pass"
    assert preflight["network_ceiling"] == 2
    assert preflight["browser_ceiling"] == 0
    assert preflight["log_max_bytes"] == 50 * 1024 * 1024
    assert preflight["production_writes"] is False
    assert preflight["resume_supported"] is False
    assert list(fixture.operation.iterdir()) == []
    assert not (fixture.receipts / preflight["sequence_id"]).exists()
    assert not (fixture.output / preflight["sequence_id"]).exists()


def test_preflight_only_cli_never_marks_or_spawns(tmp_path, capsys):
    fixture = LauncherFixture(tmp_path)
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen") as popen,
    ):
        assert launcher.main([*fixture.argv(), "--preflight-only"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    popen.assert_not_called()
    assert list(fixture.operation.iterdir()) == []


def test_controller_command_is_local_exact_and_never_resume(tmp_path):
    fixture = LauncherFixture(tmp_path)
    with mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()):
        prepared = launcher.prepare(fixture.args())
    command = prepared["command"]
    assert str(fixture.ops / "unattended_exact_observation_sequence.py") in command
    assert command[command.index("--plan-sha256") + 1] == fixture.plan_sha256
    assert command[command.index("--deployment-root") + 1] == str(fixture.deployment)
    assert command[command.index("--allowed-parent-root") + 1] == str(fixture.parents)
    assert "--resume" not in command
    assert "ssh" not in command


def test_single_use_detached_launch_is_private_and_redacted(tmp_path, monkeypatch):
    fixture = LauncherFixture(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setenv("MONGODB_URI", "mongodb://do-not-forward")

    def fake_popen(command, **kwargs):
        assert (fixture.operation / "attempt_receipt.json").is_file()
        assert kwargs["start_new_session"] is True
        assert kwargs["close_fds"] is True
        assert "MONGODB_URI" not in kwargs["env"]
        captured["command"] = command
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(fixture.startup_receipt(), sort_keys=True) + "\n",
        )
        return FakeProcess()

    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher, "process_belongs_to_group", return_value=True),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen) as popen,
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(
            launcher, "classify_post_start_processes",
            return_value=green_process_snapshot(own=4),
        ),
    ):
        receipt = launcher.launch(fixture.args())
        with pytest.raises(launcher.LaunchError):
            launcher.launch(fixture.args())
    assert receipt["status"] == "launched_unattended"
    assert receipt["network_ceiling"] == 2
    assert receipt["browser_ceiling"] == 0
    assert receipt["production_writes"] is False
    assert receipt["automatic_resume"] is False
    assert popen.call_count == 1
    assert "--resume" not in captured["command"]
    artifacts = (
        fixture.operation / "attempt_receipt.json",
        fixture.operation / "launch_receipt.json",
        fixture.controller_log,
        fixture.operation / "launch.lock",
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in artifacts)
    receipt_text = (fixture.operation / "launch_receipt.json").read_text()
    marker_text = (fixture.operation / "attempt_receipt.json").read_text()
    assert str(fixture.root) not in receipt_text + marker_text
    assert "https://example.test" not in receipt_text + marker_text


def test_capacity_block_refuses_before_marker_and_spawn(tmp_path):
    fixture = LauncherFixture(tmp_path)
    blocked = green_capacity()
    blocked.update(status="blocked", launch_allowed=False)
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=blocked),
        mock.patch.object(launcher.subprocess, "Popen") as popen,
        pytest.raises(launcher.LaunchError, match="capacity gate closed"),
    ):
        launcher.launch(fixture.args())
    popen.assert_not_called()
    assert not (fixture.operation / "attempt_receipt.json").exists()


def test_hash_and_parent_drift_fail_before_marker(tmp_path):
    fixture = LauncherFixture(tmp_path)
    args = fixture.args(controller_sha256="0" * 64)
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        pytest.raises(launcher.LaunchError, match="SHA-256 mismatch"),
    ):
        launcher.launch(args)
    assert not (fixture.operation / "attempt_receipt.json").exists()

    # A fresh fixture shows that immutable parent drift is independently fatal.
    other = LauncherFixture(tmp_path / "other")
    (other.parent / "issues.jsonl").write_text('{"drift": true}\n', encoding="utf-8")
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        pytest.raises(launcher.PlanError, match="parent artifact"),
    ):
        launcher.launch(other.args())
    assert not (other.operation / "attempt_receipt.json").exists()


def test_missing_startup_keeps_marker_and_fails_closed(tmp_path):
    fixture = LauncherFixture(tmp_path)
    cleanup = {
        "pid": 424242,
        "process_group_present_before": True,
        "signals": ["SIGTERM"],
        "process_group_present_after": False,
        "exit_code": -15,
        "quiescent": True,
    }
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()),
        mock.patch.object(launcher, "_inspect_startup", return_value=(None, None, None)),
        mock.patch.object(launcher, "terminate_process_group", return_value=cleanup),
        pytest.raises(launcher.LaunchError, match="startup receipt missing"),
    ):
        launcher.launch(fixture.args())
    assert (fixture.operation / "attempt_receipt.json").is_file()
    failure = json.loads((fixture.operation / "launch_receipt.json").read_text())
    assert failure["status"] == "launch_failed"
    assert failure["automatic_resume"] is False
    assert failure["cleanup"]["quiescent"] is True


def test_invalid_startup_capacity_cleans_up_and_closes_attempt(tmp_path):
    fixture = LauncherFixture(tmp_path)

    def fake_popen(command, **kwargs):
        startup = fixture.startup_receipt()
        startup["host_capacity_guard"] = {
            "status": "blocked", "launch_allowed": False,
            "sequence_limits": {"network_workers": 0, "browser_workers": 0},
        }
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(startup, sort_keys=True) + "\n",
        )
        return FakeProcess()

    cleanup = {"pid": 424242, "quiescent": True}
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(launcher, "terminate_process_group", return_value=cleanup) as stop,
        pytest.raises(launcher.LaunchError, match="startup capacity gate closed"),
    ):
        launcher.launch(fixture.args())
    stop.assert_called_once()
    assert (fixture.operation / "attempt_receipt.json").is_file()


@pytest.mark.parametrize(
    ("terminal_status", "exit_code"),
    (("terminal_observation_sequence", 0), ("terminal_with_isolated_failures", 1)),
)
def test_fast_valid_terminal_is_accepted_without_resume(
    tmp_path, terminal_status, exit_code,
):
    fixture = LauncherFixture(tmp_path)

    def fake_popen(command, **kwargs):
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(fixture.startup_receipt(), sort_keys=True) + "\n",
        )
        write_private(sequence_dir / "terminal_receipt.json", "{}\n")
        return FakeProcess(exit_code=exit_code)

    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(
            launcher, "classify_post_start_processes",
            return_value=green_process_snapshot(),
        ),
        mock.patch.object(
            launcher.SequenceController,
            "validate_sequence_terminal",
            return_value={"status": terminal_status},
        ),
    ):
        receipt = launcher.launch(fixture.args())
    assert receipt["status"] == "completed_fast"
    assert receipt["terminal_receipt_sha256"] == sha(
        fixture.receipts
        / "local-exact-launcher-fixture"
        / "terminal_receipt.json"
    )


def test_launch_receipt_write_failure_forces_quiescence(tmp_path):
    fixture = LauncherFixture(tmp_path)
    real_create = launcher._create_json_exclusive
    failed_once = False

    def fake_create(path, value):
        nonlocal failed_once
        if path.name == "launch_receipt.json" and not failed_once:
            failed_once = True
            raise OSError("fixture receipt failure")
        return real_create(path, value)

    def fake_popen(command, **kwargs):
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(fixture.startup_receipt(), sort_keys=True) + "\n",
        )
        return FakeProcess()

    cleanup = {"pid": 424242, "quiescent": True}
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher, "process_belongs_to_group", return_value=True),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(
            launcher, "classify_post_start_processes",
            return_value=green_process_snapshot(),
        ),
        mock.patch.object(launcher, "terminate_process_group", return_value=cleanup) as stop,
        mock.patch.object(launcher, "_create_json_exclusive", side_effect=fake_create),
        pytest.raises(OSError, match="fixture receipt failure"),
    ):
        launcher.launch(fixture.args())
    stop.assert_called_once()
    failure = json.loads((fixture.operation / "launch_receipt.json").read_text())
    assert failure["status"] == "launch_failed"
    assert failure["cleanup"]["quiescent"] is True


def test_cleanup_signals_group_even_if_wrapper_already_exited():
    process = FakeProcess(pid=45678, exit_code=7)
    with (
        mock.patch.object(
            launcher, "process_group_present",
            side_effect=[True, False, False, False, False],
        ),
        mock.patch.object(launcher.os, "killpg") as killpg,
    ):
        proof = launcher.terminate_process_group(process)
    killpg.assert_called_once_with(45678, launcher.signal.SIGTERM)
    assert proof["quiescent"] is True
    assert proof["signals"] == ["SIGTERM:45678"]


def test_cleanup_finds_and_stops_detached_observer_groups():
    process = FakeProcess(pid=9001, exit_code=7)
    alive = {9001, 9002}

    def fake_kill(group, sent_signal):
        assert sent_signal == launcher.signal.SIGTERM
        alive.discard(group)

    with (
        mock.patch.object(
            launcher, "matching_runtime_process_groups",
            side_effect=[{9001, 9002}, set(), set()],
        ),
        mock.patch.object(
            launcher, "process_group_present", side_effect=lambda group: group in alive,
        ),
        mock.patch.object(launcher.os, "killpg", side_effect=fake_kill) as killpg,
    ):
        proof = launcher.terminate_process_group(process, ("fixture-sequence",))
    assert {call.args[0] for call in killpg.call_args_list} == {9001, 9002}
    assert proof["matching_process_groups_before"] == [9001, 9002]
    assert proof["matching_process_groups_after"] == []
    assert proof["quiescent"] is True


def test_unquiesced_cleanup_is_a_hard_failure(tmp_path):
    fixture = LauncherFixture(tmp_path)
    cleanup = {"pid": 424242, "quiescent": False}
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()),
        mock.patch.object(launcher, "_inspect_startup", return_value=(None, None, None)),
        mock.patch.object(launcher, "terminate_process_group", return_value=cleanup),
        pytest.raises(launcher.LaunchError, match="did not quiesce"),
    ):
        launcher.launch(fixture.args())
    failure = json.loads((fixture.operation / "launch_receipt.json").read_text())
    assert failure["cleanup"]["quiescent"] is False


def test_launcher_has_no_resume_surface_and_fixed_four_second_inspection():
    parser = launcher.build_parser()
    assert all(action.dest != "resume" for action in parser._actions)
    assert launcher.STARTUP_INSPECTION_SECONDS == 4.0
    assert launcher.OBSERVER_LOG_MAX_BYTES == 50 * 1024 * 1024


def test_operation_outside_workspace_refuses_without_creating_lock(tmp_path):
    fixture = LauncherFixture(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o700)
    with pytest.raises(launcher.LaunchError, match="inside the local workspace"):
        launcher.launch(fixture.args(operation_root=outside))
    assert list(outside.iterdir()) == []


def test_deployment_manifest_closes_nonexecution_files_and_modes(tmp_path):
    fixture = LauncherFixture(tmp_path / "content")
    fixture.non_execution_import.write_text("# drift outside execution list\n")
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        pytest.raises(launcher.LaunchError, match="source mapping|manifest file hash"),
    ):
        launcher.prepare(fixture.args())
    assert not (fixture.operation / "attempt_receipt.json").exists()

    mode_fixture = LauncherFixture(tmp_path / "mode")
    mode_fixture.non_execution_import.chmod(0o664)
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        pytest.raises(launcher.LaunchError, match="manifest file hash"),
    ):
        launcher.prepare(mode_fixture.args())
    assert not (mode_fixture.operation / "attempt_receipt.json").exists()


def test_deployment_digest_and_source_mapping_are_not_labels(tmp_path):
    fixture = LauncherFixture(tmp_path / "digest")
    manifest = json.loads(fixture.deployment_manifest.read_text())
    assert launcher.deployment_digest(manifest["files"]) == fixture.deployment_sha256
    altered = [dict(row) for row in manifest["files"]]
    altered[0]["bytes"] += 1
    assert launcher.deployment_digest(altered) != fixture.deployment_sha256

    fake_digest = "e" * 64
    old_root = fixture.deployment
    fake_root = old_root.with_name(fake_digest)
    old_root.rename(fake_root)
    old_text = str(old_root)
    fake_text = str(fake_root)
    manifest["deployment_sha256"] = fake_digest
    for field in (
        "deployment_root_local", "automation_root_local", "controller_local",
        "plan_local",
    ):
        manifest[field] = str(manifest[field]).replace(old_text, fake_text)
    for row in manifest["files"]:
        row["deployed_path"] = str(row["deployed_path"]).replace(old_text, fake_text)
        row["source_path"] = str(row["source_path"]).replace(
            fixture.deployment_sha256, fake_digest,
        )
    fixture.deployment_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    fixture.deployment_manifest.chmod(0o600)
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        pytest.raises(launcher.LaunchError, match="content-addressed deployment digest"),
    ):
        launcher.prepare(fixture.args(
            plan=fake_root / "plan.json",
            deployment_root=fake_root,
            automation_root=fake_root / "automation",
            deployment_sha256=fake_digest,
            deployment_manifest_sha256=sha(fixture.deployment_manifest),
        ))

    unsafe = LauncherFixture(tmp_path / "source")
    payload = json.loads(unsafe.deployment_manifest.read_text())
    payload["files"][0]["source_path"] = "../outside.py"
    unsafe.deployment_manifest.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    unsafe.deployment_manifest.chmod(0o600)
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        pytest.raises(launcher.LaunchError, match="unsafe source path"),
    ):
        launcher.prepare(unsafe.args(
            deployment_manifest_sha256=sha(unsafe.deployment_manifest),
        ))


def test_bounded_inspection_observes_full_four_seconds_while_alive(tmp_path):
    startup = write_private(tmp_path / "startup.json", "{}\n")
    terminal = tmp_path / "terminal.json"
    clock = [0.0]
    process = FakeProcess(exit_code=None)

    def monotonic():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds

    with (
        mock.patch.object(launcher.time, "monotonic", side_effect=monotonic),
        mock.patch.object(launcher.time, "sleep", side_effect=sleep),
    ):
        observed_startup, observed_terminal, exit_code = launcher._inspect_startup(
            process, startup, terminal,
        )
    assert observed_startup == {}
    assert observed_terminal is None
    assert exit_code is None
    assert clock[0] == pytest.approx(launcher.STARTUP_INSPECTION_SECONDS)


def test_darwin_process_group_parser_filters_unrelated_and_current_groups():
    output = "\n".join((
        " 101  501 /python unattended_exact_observation_sequence.py --plan token-plan",
        " 102  502 /python parallel_observe_pure.py --input other-plan",
        " 103  503 /python unrelated.py token-plan",
        " 104  504 /python run_pure_discovery_diagnostic.py token-plan",
        " 105  700 /python parallel_observe_pure.py token-plan",
        " malformed row",
    ))
    completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    with (
        mock.patch.object(launcher.subprocess, "run", return_value=completed),
        mock.patch.object(launcher.os, "getpid", return_value=104),
        mock.patch.object(launcher.os, "getppid", return_value=999),
        mock.patch.object(launcher.os, "getpgrp", return_value=700),
    ):
        assert launcher.matching_runtime_process_groups(("token-plan",)) == {501}


def test_post_start_process_classifier_accepts_own_wrapper_and_lanes_only():
    output = "\n".join((
        " 201  500 /usr/bin/caffeinate unattended_exact_observation_sequence.py token-plan",
        " 202  500 /python unattended_exact_observation_sequence.py token-plan",
        " 203  601 /python parallel_observe_pure.py token-output",
        " 204  602 /python run_pure_discovery_diagnostic.py token-output",
    ))
    completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    with (
        mock.patch.object(launcher.subprocess, "run", return_value=completed),
        mock.patch.object(launcher.os, "getpid", return_value=9998),
        mock.patch.object(launcher.os, "getppid", return_value=9999),
    ):
        snapshot = launcher.classify_post_start_processes(
            500, ("token-plan", "token-output"),
        )
    assert snapshot == {
        "scan_ok": True,
        "own_network_processes": 4,
        "foreign_network_processes": 0,
        "browser_processes": 0,
    }
    assert launcher._post_start_capacity_errors(green_capacity(), snapshot) == []
    conflicting = subprocess.CompletedProcess([], 0, stdout=output + "\n" + "\n".join((
        " 205  700 /python unattended_country_sessions.py foreign-plan",
        " 206  701 /usr/bin/chromedriver --headless",
    )), stderr="")
    with (
        mock.patch.object(launcher.subprocess, "run", return_value=conflicting),
        mock.patch.object(launcher.os, "getpid", return_value=9998),
        mock.patch.object(launcher.os, "getppid", return_value=9999),
    ):
        unsafe = launcher.classify_post_start_processes(
            500, ("token-plan", "token-output"),
        )
    assert unsafe["foreign_network_processes"] == 1
    assert unsafe["browser_processes"] == 1
    errors = launcher._post_start_capacity_errors(green_capacity(), unsafe)
    assert "post_start_foreign_network_process_detected" in errors
    assert "post_start_browser_process_detected" in errors


def test_process_scan_failure_is_never_reported_quiescent():
    process = FakeProcess(pid=45678, exit_code=7)
    error = subprocess.CalledProcessError(1, ["ps"])
    with (
        mock.patch.object(
            launcher, "matching_runtime_process_groups", side_effect=error,
        ),
        mock.patch.object(launcher, "process_group_present", return_value=False),
    ):
        proof = launcher.terminate_process_group(process, ("token-plan",))
    assert proof["process_scan_error"] == "CalledProcessError"
    assert proof["quiescent"] is False


def test_keyboard_interrupt_after_spawn_closes_and_keeps_marker(tmp_path):
    fixture = LauncherFixture(tmp_path)
    cleanup = {"pid": 424242, "quiescent": True, "exit_code": -15}
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen", return_value=FakeProcess()),
        mock.patch.object(launcher, "_inspect_startup", side_effect=KeyboardInterrupt),
        mock.patch.object(
            launcher, "terminate_process_group", return_value=cleanup,
        ) as stop,
        pytest.raises(KeyboardInterrupt),
    ):
        launcher.launch(fixture.args())
    stop.assert_called_once()
    assert (fixture.operation / "attempt_receipt.json").is_file()
    failure = json.loads((fixture.operation / "launch_receipt.json").read_text())
    assert failure["status"] == "launch_failed"
    assert failure["error_type"] == "KeyboardInterrupt"
    assert failure["cleanup"]["quiescent"] is True


def test_startup_pid_mismatch_is_cleaned_and_closed(tmp_path):
    fixture = LauncherFixture(tmp_path)

    def fake_popen(command, **kwargs):
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(fixture.startup_receipt(pid=99111), sort_keys=True) + "\n",
        )
        return FakeProcess()

    cleanup = {"pid": 424242, "quiescent": True, "exit_code": -15}
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(launcher, "process_belongs_to_group", return_value=False),
        mock.patch.object(
            launcher, "terminate_process_group", return_value=cleanup,
        ) as stop,
        pytest.raises(launcher.LaunchError, match="outside the detached process group"),
    ):
        launcher.launch(fixture.args())
    stop.assert_called_once()
    failure = json.loads((fixture.operation / "launch_receipt.json").read_text())
    assert failure["cleanup"]["quiescent"] is True


def test_post_start_capacity_conflict_forces_quiescence(tmp_path):
    fixture = LauncherFixture(tmp_path)

    def fake_popen(command, **kwargs):
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(fixture.startup_receipt(), sort_keys=True) + "\n",
        )
        return FakeProcess()

    conflict = {
        "scan_ok": True,
        "own_network_processes": 4,
        "foreign_network_processes": 1,
        "browser_processes": 0,
    }
    cleanup = {"pid": 424242, "quiescent": True, "exit_code": -15}
    with (
        mock.patch.object(launcher, "local_capacity_report", return_value=green_capacity()),
        mock.patch.object(
            launcher, "classify_post_start_processes", return_value=conflict,
        ),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(launcher, "process_belongs_to_group", return_value=True),
        mock.patch.object(
            launcher, "terminate_process_group", return_value=cleanup,
        ) as stop,
        pytest.raises(launcher.LaunchError, match="post-start capacity gate closed"),
    ):
        launcher.launch(fixture.args())
    stop.assert_called_once()


def test_post_start_resource_block_forces_quiescence(tmp_path):
    fixture = LauncherFixture(tmp_path)

    def fake_popen(command, **kwargs):
        sequence_dir = private_dir(fixture.receipts / "local-exact-launcher-fixture")
        write_private(
            sequence_dir / "startup_receipt.json",
            json.dumps(fixture.startup_receipt(), sort_keys=True) + "\n",
        )
        return FakeProcess()

    blocked = green_capacity()
    blocked.update(status="blocked", launch_allowed=False)
    cleanup = {"pid": 424242, "quiescent": True, "exit_code": -15}
    with (
        mock.patch.object(
            launcher, "local_capacity_report",
            side_effect=[green_capacity(), blocked],
        ),
        mock.patch.object(
            launcher, "classify_post_start_processes",
            return_value=green_process_snapshot(own=4),
        ),
        mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(launcher, "_inspect_startup", side_effect=inspect_now),
        mock.patch.object(launcher, "process_belongs_to_group", return_value=True),
        mock.patch.object(
            launcher, "terminate_process_group", return_value=cleanup,
        ) as stop,
        pytest.raises(launcher.LaunchError, match="post-start capacity gate closed"),
    ):
        launcher.launch(fixture.args())
    stop.assert_called_once()
