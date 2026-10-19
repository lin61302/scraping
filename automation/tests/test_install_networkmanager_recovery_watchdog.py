import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "automation" / "ops" / "install_networkmanager_recovery_watchdog.py"
WATCHDOG_PATH = ROOT / "automation" / "ops" / "networkmanager_recovery_watchdog.py"
SYSTEMD_DIR = ROOT / "automation" / "ops" / "systemd"
SPEC = importlib.util.spec_from_file_location("install_networkmanager_recovery_watchdog", MODULE_PATH)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


UUID = "11111111-2222-4333-8444-555555555555"


class FakeRunner:
    def __init__(self, responses=None):
        self.commands = []
        self.responses = {
            tuple(key): (list(value) if isinstance(value, list) else value)
            for key, value in (responses or {}).items()
        }

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        for key in sorted(self.responses, key=len, reverse=True):
            if tuple(command[: len(key)]) == key:
                response = self.responses[key]
                if isinstance(response, list):
                    if not response:
                        raise AssertionError("no fake response left for {}".format(key))
                    return response.pop(0)
                return response
        if len(command) > 1 and command[1] == "show":
            return quiescent_unit_result()
        if len(command) > 1 and command[1] == "is-enabled":
            return installer.CommandResult(1)
        return installer.CommandResult(0)


def unit_show_command(unit):
    return (
        "/usr/bin/systemctl",
        "show",
        "--no-pager",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=Job",
        unit,
    )


def unit_state_result(active_state="inactive", sub_state="dead", job="", load_state="loaded"):
    return installer.CommandResult(
        0,
        "LoadState={}\nActiveState={}\nSubState={}\nJob={}\n".format(
            load_state,
            active_state,
            sub_state,
            job,
        ),
    )


def quiescent_unit_result():
    return unit_state_result()


def test_embedded_units_exactly_match_reviewable_files():
    assert installer.SERVICE_CONTENT == (
        SYSTEMD_DIR / "mlp-network-recovery-watchdog.service"
    ).read_text(encoding="utf-8")
    assert installer.TIMER_CONTENT == (
        SYSTEMD_DIR / "mlp-network-recovery-watchdog.timer"
    ).read_text(encoding="utf-8")


def test_service_orders_after_but_does_not_pull_in_networkmanager():
    assert "After=NetworkManager.service" in installer.SERVICE_CONTENT
    assert "Wants=NetworkManager.service" not in installer.SERVICE_CONTENT
    assert "Requires=NetworkManager.service" not in installer.SERVICE_CONTENT
    assert "EnvironmentFile=" not in installer.SERVICE_CONTENT
    assert "PYTHONPATH=" in installer.SERVICE_CONTENT
    assert "LD_PRELOAD=" in installer.SERVICE_CONTENT
    assert "ExecStart=/usr/bin/python3 -I " in installer.SERVICE_CONTENT


def test_profile_requires_exactly_one_safe_selector():
    with pytest.raises(installer.InstallError, match="exactly one"):
        installer.validate_profile("eno1", None, None, 15)
    with pytest.raises(installer.InstallError, match="exactly one"):
        installer.validate_profile("eno1", UUID, "wired", 15)
    with pytest.raises(installer.InstallError, match="unsafe"):
        installer.validate_profile("eno1", None, "bad'name", 15)
    with pytest.raises(installer.InstallError, match="invalid"):
        installer.validate_profile("not/an/interface", UUID, None, 15)
    profile = installer.validate_profile("eno1", UUID.upper(), None, 12)
    assert profile.selector == "uuid"
    assert profile.value == UUID
    assert profile.command_timeout_seconds == 12


def test_rendered_config_is_root_private_content_without_identity_defaults():
    profile = installer.validate_profile("eth0", None, "Example wired profile", 15)
    config = installer.render_config(profile).decode("utf-8")
    assert "NM_WATCHDOG_INTERFACE='eth0'" in config
    assert "NM_WATCHDOG_CONNECTION_NAME='Example wired profile'" in config
    assert "NM_WATCHDOG_CONNECTION_UUID" not in config


def test_source_is_hash_pinned_regular_file_and_symlink_is_rejected(tmp_path):
    source = tmp_path.resolve() / "watchdog.py"
    content = b"#!/usr/bin/env python3\nprint('safe')\n"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    assert installer.read_hash_pinned_source(source, digest) == content
    with pytest.raises(installer.InstallError, match="required SHA-256"):
        installer.read_hash_pinned_source(source, "0" * 64)
    link = source.parent / "watchdog-link.py"
    link.symlink_to(source)
    with pytest.raises(installer.InstallError, match="symlink"):
        installer.read_hash_pinned_source(link, digest)


def temporary_paths(tmp_path):
    root = tmp_path.resolve()
    (root / "usr" / "local").mkdir(parents=True)
    (root / "etc" / "systemd").mkdir(parents=True)
    return installer.InstallPaths(
        watchdog=root / "usr" / "local" / "libexec" / "watchdog",
        config=root / "etc" / "watchdog.conf",
        service=root / "etc" / "systemd" / "watchdog.service",
        timer=root / "etc" / "systemd" / "watchdog.timer",
    )


def dataclass_paths(paths):
    return [paths.watchdog, paths.config, paths.service, paths.timer]


def test_dry_run_has_no_writes_or_commands(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner()
    result = installer.perform_install(
        artifacts,
        enable=True,
        dry_run=True,
        runner=runner,
        systemctl_path="/bin/systemctl",
        systemd_analyze_path="/bin/systemd-analyze",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    assert result["outcome"] == "validated_no_changes"
    assert result["enable_requested"] is True
    assert runner.commands == []
    assert not paths.watchdog.exists()
    assert not paths.config.exists()
    assert not paths.service.exists()
    assert not paths.timer.exists()


def test_install_is_atomic_private_and_does_not_enable_by_default(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner()
    result = installer.perform_install(
        artifacts,
        enable=False,
        dry_run=False,
        runner=runner,
        systemctl_path="/usr/bin/systemctl",
        systemd_analyze_path="/usr/bin/systemd-analyze",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    assert paths.watchdog.read_bytes() == WATCHDOG_PATH.read_bytes()
    assert paths.service.read_text(encoding="utf-8") == installer.SERVICE_CONTENT
    assert paths.timer.read_text(encoding="utf-8") == installer.TIMER_CONTENT
    assert stat.S_IMODE(paths.watchdog.stat().st_mode) == 0o755
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.service.stat().st_mode) == 0o644
    assert stat.S_IMODE(paths.timer.stat().st_mode) == 0o644
    assert runner.commands[0][1] == "show"
    assert runner.commands[1][1] == "show"
    verify = next(command for command in runner.commands if command[0].endswith("systemd-analyze"))
    assert verify[:2] == ["/usr/bin/systemd-analyze", "verify"]
    assert verify[2] != str(paths.service)
    assert verify[3] != str(paths.timer)
    assert Path(verify[2]).name == paths.service.name
    assert Path(verify[3]).name == paths.timer.name
    assert ["/usr/bin/systemctl", "daemon-reload"] in runner.commands
    enable_commands = [command for command in runner.commands if "enable" in command]
    assert enable_commands == []
    assert result["timer_enabled_by_installer"] is False


def test_enable_requires_reviewed_identical_prior_install(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    installer.perform_install(
        artifacts,
        enable=False,
        dry_run=False,
        runner=FakeRunner(),
        systemctl_path="/usr/bin/systemctl",
        systemd_analyze_path=None,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    runner = FakeRunner()
    result = installer.perform_install(
        artifacts,
        enable=True,
        dry_run=False,
        runner=runner,
        systemctl_path="/usr/bin/systemctl",
        systemd_analyze_path="/usr/bin/systemd-analyze",
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        preflight_confirmed=True,
    )
    assert [
        "/usr/bin/systemctl",
        "enable",
        "--now",
        "mlp-network-recovery-watchdog.timer",
    ] in runner.commands
    assert result["timer_enabled_by_installer"] is True


def test_existing_destination_symlink_fails_before_any_write_or_command(tmp_path):
    paths = temporary_paths(tmp_path)
    target = paths.config.parent / "target"
    target.write_text("unchanged", encoding="utf-8")
    paths.config.symlink_to(target)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner()
    with pytest.raises(installer.InstallError, match="symlink"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert target.read_text(encoding="utf-8") == "unchanged"
    assert runner.commands == []


def test_failed_unit_verification_prevents_daemon_reload_and_enable(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner(
        {("/usr/bin/systemd-analyze", "verify"): installer.CommandResult(1, stderr="bad unit")}
    )
    with pytest.raises(installer.InstallError, match="verification failed"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path="/usr/bin/systemd-analyze",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert len(runner.commands) == 3
    assert runner.commands[:2] == [
        list(unit_show_command("mlp-network-recovery-watchdog.timer")),
        list(unit_show_command("mlp-network-recovery-watchdog.service")),
    ]
    assert runner.commands[2][:2] == ["/usr/bin/systemd-analyze", "verify"]
    assert not paths.watchdog.exists()
    assert not paths.config.exists()
    assert not paths.service.exists()
    assert not paths.timer.exists()


@pytest.mark.parametrize(
    "unit",
    [
        "mlp-network-recovery-watchdog.timer",
        "mlp-network-recovery-watchdog.service",
    ],
)
def test_active_unit_rejects_install_before_staging_or_writes(tmp_path, unit):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner(
        {
            unit_show_command(unit): unit_state_result("active", "running")
        }
    )
    with pytest.raises(installer.InstallError, match="while .* is not quiescent"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path="/usr/bin/systemd-analyze",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert not any(path.exists() for path in dataclass_paths(paths))
    assert not any(command[0].endswith("systemd-analyze") for command in runner.commands)


def test_unit_becoming_active_during_staging_rejects_before_writes(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner(
        {
            unit_show_command("mlp-network-recovery-watchdog.timer"): [
                quiescent_unit_result(),
                unit_state_result("active", "waiting"),
            ]
        }
    )
    with pytest.raises(installer.InstallError, match="timer is not quiescent"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path="/usr/bin/systemd-analyze",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert any(command[0].endswith("systemd-analyze") for command in runner.commands)
    assert not any(path.exists() for path in dataclass_paths(paths))


@pytest.mark.parametrize(
    "active_state,sub_state",
    [
        ("activating", "start"),
        ("deactivating", "stop"),
    ],
)
def test_transitioning_oneshot_unit_rejects_install_before_staging(
    tmp_path, active_state, sub_state
):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    unit = "mlp-network-recovery-watchdog.service"
    runner = FakeRunner(
        {unit_show_command(unit): unit_state_result(active_state, sub_state)}
    )
    with pytest.raises(installer.InstallError, match="service is not quiescent"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path="/usr/bin/systemd-analyze",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert not any(path.exists() for path in dataclass_paths(paths))
    assert not any(command[0].endswith("systemd-analyze") for command in runner.commands)


def test_queued_unit_job_rejects_install_before_staging(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    unit = "mlp-network-recovery-watchdog.timer"
    runner = FakeRunner({unit_show_command(unit): unit_state_result(job="981 start")})
    with pytest.raises(installer.InstallError, match="timer has a queued job"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path="/usr/bin/systemd-analyze",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert not any(path.exists() for path in dataclass_paths(paths))
    assert not any(command[0].endswith("systemd-analyze") for command in runner.commands)


def test_enable_requires_explicit_preflight_and_prior_install(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner()
    with pytest.raises(installer.InstallError, match="preflight-confirmed"):
        installer.perform_install(
            artifacts,
            enable=True,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert runner.commands == []

    with pytest.raises(installer.InstallError, match="identical completed installation"):
        installer.perform_install(
            artifacts,
            enable=True,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            preflight_confirmed=True,
        )
    assert not any(path.exists() for path in dataclass_paths(paths))


def test_group_writable_destination_parent_fails_closed(tmp_path):
    paths = temporary_paths(tmp_path)
    paths.service.parent.chmod(0o775)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner()
    with pytest.raises(installer.InstallError, match="group- or world-writable"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert runner.commands == []


def test_dangling_symlink_destination_parent_fails_closed(tmp_path):
    paths = temporary_paths(tmp_path)
    paths.watchdog.parent.symlink_to(paths.watchdog.parent.parent / "missing-libexec")
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner()
    with pytest.raises(installer.InstallError, match="parent is a symlink"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert runner.commands == []


def test_enabled_but_inactive_timer_rejects_non_enable_update(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner(
        {
            (
                "/usr/bin/systemctl",
                "is-enabled",
                "--quiet",
                "mlp-network-recovery-watchdog.timer",
            ): installer.CommandResult(0)
        }
    )
    with pytest.raises(installer.InstallError, match="timer is enabled"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert not any(path.exists() for path in dataclass_paths(paths))


def test_partial_write_failure_restores_all_prior_artifacts(tmp_path, monkeypatch):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    originals = {}
    for artifact in artifacts:
        artifact.destination.parent.mkdir(mode=0o755, exist_ok=True)
        content = ("old-{}\n".format(artifact.kind)).encode("utf-8")
        artifact.destination.write_bytes(content)
        artifact.destination.chmod(artifact.mode)
        originals[artifact.destination] = (content, artifact.mode)

    real_install = installer.atomic_install_artifact
    call_count = {"value": 0}

    def fail_once_on_third_write(artifact, owner_uid=0, owner_gid=0):
        call_count["value"] += 1
        if call_count["value"] == 3:
            raise OSError("simulated write failure")
        return real_install(artifact, owner_uid=owner_uid, owner_gid=owner_gid)

    monkeypatch.setattr(installer, "atomic_install_artifact", fail_once_on_third_write)
    runner = FakeRunner()
    with pytest.raises(installer.InstallError, match="prior artifacts restored"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    for destination, (content, mode) in originals.items():
        assert destination.read_bytes() == content
        assert stat.S_IMODE(destination.stat().st_mode) == mode
    assert runner.commands[-1] == ["/usr/bin/systemctl", "daemon-reload"]


def test_daemon_reload_failure_removes_new_artifacts(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    runner = FakeRunner(
        {
            ("/usr/bin/systemctl", "daemon-reload"): [
                installer.CommandResult(1, stderr="reload failed"),
                installer.CommandResult(0),
            ]
        }
    )
    with pytest.raises(installer.InstallError, match="prior artifacts restored"):
        installer.perform_install(
            artifacts,
            enable=False,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert not any(path.exists() for path in dataclass_paths(paths))


def test_enable_failure_disables_timer_and_restores_identical_install(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    installer.perform_install(
        artifacts,
        enable=False,
        dry_run=False,
        runner=FakeRunner(),
        systemctl_path="/usr/bin/systemctl",
        systemd_analyze_path=None,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    before = {artifact.destination: artifact.destination.read_bytes() for artifact in artifacts}
    runner = FakeRunner(
        {
            ("/usr/bin/systemctl", "enable", "--now"): installer.CommandResult(
                1, stderr="enable failed"
            )
        }
    )
    with pytest.raises(installer.InstallError, match="prior artifacts restored"):
        installer.perform_install(
            artifacts,
            enable=True,
            dry_run=False,
            runner=runner,
            systemctl_path="/usr/bin/systemctl",
            systemd_analyze_path=None,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            preflight_confirmed=True,
        )
    assert [
        "/usr/bin/systemctl",
        "disable",
        "--now",
        "mlp-network-recovery-watchdog.timer",
    ] in runner.commands
    assert {artifact.destination: artifact.destination.read_bytes() for artifact in artifacts} == before


def test_bounded_runner_converts_timeout_without_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    result = installer.BoundedRunner(9)(["/usr/bin/systemctl", "daemon-reload"])
    assert result.timed_out is True
    assert result.returncode == 124
    assert captured["kwargs"]["timeout"] == 9
    assert "shell" not in captured["kwargs"]
    assert "PYTHONPATH" not in captured["kwargs"]["env"]
    assert "LD_PRELOAD" not in captured["kwargs"]["env"]


def test_bounded_runner_converts_exec_error(monkeypatch):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("removed during invocation")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    result = installer.BoundedRunner(9)(["/usr/bin/systemctl", "daemon-reload"])
    assert result.returncode == 127
    assert result.timed_out is False
    assert "cannot execute bounded command" in result.stderr


def test_public_plan_contains_hashes_not_profile_identity(tmp_path):
    paths = temporary_paths(tmp_path)
    profile = installer.validate_profile("eno1", UUID, None, 15)
    artifacts = installer.build_artifacts(WATCHDOG_PATH.read_bytes(), profile, paths)
    serialized = json.dumps(installer.public_plan(artifacts, False, True), sort_keys=True)
    assert UUID not in serialized
    assert "sha256" in serialized


def test_main_requires_root_even_for_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 501)
    assert installer.main(["--dry-run"]) == 2
    assert "must run as root" in capsys.readouterr().err


def test_system_command_resolution_ignores_caller_path(monkeypatch):
    calls = []

    def fake_which(name, path=None):
        calls.append((name, path))
        return "/usr/bin/" + name

    monkeypatch.setattr(installer.shutil, "which", fake_which)
    assert installer.find_system_command("systemctl") == "/usr/bin/systemctl"
    assert calls == [("systemctl", installer.TRUSTED_SYSTEM_PATH)]
