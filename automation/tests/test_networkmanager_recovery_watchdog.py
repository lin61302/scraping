import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "automation" / "ops" / "networkmanager_recovery_watchdog.py"
SPEC = importlib.util.spec_from_file_location("networkmanager_recovery_watchdog", MODULE_PATH)
assert SPEC and SPEC.loader
watchdog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)


UUID = "11111111-2222-4333-8444-555555555555"


class FakeRunner:
    def __init__(
        self,
        networking="enabled",
        profile="activated",
        devices="eno1",
        profile_uuid=UUID,
        static_interface="",
        active_rows=None,
        action_result=None,
        events=None,
    ):
        self.networking = networking
        self.profile = profile
        self.devices = devices
        self.profile_uuid = profile_uuid
        self.static_interface = static_interface
        self.active_rows = (
            ("" if profile == "deactivated" else "{}:{}:{}\n".format(profile_uuid, profile, devices))
            if active_rows is None
            else active_rows
        )
        self.action_result = action_result or watchdog.CommandResult(0)
        self.commands = []
        self.events = events

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command[-1:] == ["networking"]:
            return watchdog.CommandResult(0, self.networking + "\n")
        if "connection.uuid" in command:
            return watchdog.CommandResult(0, self.profile_uuid + "\n")
        if "connection.interface-name" in command:
            return watchdog.CommandResult(0, self.static_interface + "\n")
        if "--active" in command:
            return watchdog.CommandResult(0, self.active_rows)
        if self.events is not None:
            self.events.append(("action", command))
        return self.action_result


def config(timeout=15):
    return watchdog.Config("eno1", "uuid", UUID, timeout)


def prime_failure_state(state, failure_class, count=2):
    state["config_fingerprint"] = watchdog.config_fingerprint(config())
    state["failure_class"] = failure_class
    state["consecutive_failures"] = count


def state_writer_snapshots():
    snapshots = []

    def writer(state):
        snapshots.append(dict(state))

    return snapshots, writer


def carrier_tree(tmp_path, value="1"):
    root = tmp_path / "sys" / "class" / "net"
    interface = root / "eno1"
    interface.mkdir(parents=True)
    (interface / "carrier").write_text(value + "\n", encoding="ascii")
    return root


def run_and_save(tmp_path, runner, now, carrier="1"):
    state_path = tmp_path / "state" / "state.json"
    state = watchdog.load_state(state_path, now)
    observation = watchdog.observe(config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path, carrier), runner)
    result = watchdog.evaluate_cycle(
        config(), state, observation, now, Path("/usr/bin/nmcli"), runner
    )
    watchdog.atomic_write_state(state_path, state)
    return result, state_path


def test_root_only_config_rejects_public_mode_and_symlink(tmp_path):
    protected = tmp_path / "watchdog.conf"
    protected.write_text("NM_WATCHDOG_INTERFACE='eno1'\n", encoding="utf-8")
    protected.chmod(0o644)
    with pytest.raises(watchdog.WatchdogError, match="group- or world"):
        watchdog.validate_root_only_config(protected, expected_uid=os.getuid())

    protected.chmod(0o600)
    watchdog.validate_root_only_config(protected, expected_uid=os.getuid())
    link = tmp_path / "link.conf"
    link.symlink_to(protected)
    with pytest.raises(watchdog.WatchdogError, match="cannot open"):
        watchdog.validate_root_only_config(link, expected_uid=os.getuid())


def test_strict_config_file_rejects_unknown_or_shell_style_keys(tmp_path):
    protected = tmp_path / "watchdog.conf"
    protected.write_text("LD_PRELOAD='/tmp/not-allowed.so'\n", encoding="utf-8")
    protected.chmod(0o600)
    with pytest.raises(watchdog.WatchdogError, match="unknown key"):
        watchdog.read_root_only_config(protected, expected_uid=os.getuid())
    protected.write_text("NM_WATCHDOG_INTERFACE=eno1\n", encoding="utf-8")
    with pytest.raises(watchdog.WatchdogError, match="strict"):
        watchdog.read_root_only_config(protected, expected_uid=os.getuid())


def test_maintenance_inhibit_requires_private_regular_owned_file(tmp_path):
    inhibit = tmp_path / "maintenance.inhibit"
    assert watchdog.maintenance_inhibit_active(inhibit, expected_uid=os.getuid()) is False
    inhibit.write_bytes(b"")
    inhibit.chmod(0o600)
    assert watchdog.maintenance_inhibit_active(inhibit, expected_uid=os.getuid()) is True
    inhibit.chmod(0o644)
    with pytest.raises(watchdog.WatchdogError, match="mode 0600"):
        watchdog.maintenance_inhibit_active(inhibit, expected_uid=os.getuid())
    inhibit.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"")
    target.chmod(0o600)
    inhibit.symlink_to(target)
    with pytest.raises(watchdog.WatchdogError, match="symlink"):
        watchdog.maintenance_inhibit_active(inhibit, expected_uid=os.getuid())
    inhibit.unlink()
    os.mkfifo(inhibit, 0o600)
    with pytest.raises(watchdog.WatchdogError, match="not a regular"):
        watchdog.maintenance_inhibit_active(inhibit, expected_uid=os.getuid())


def test_config_requires_exactly_one_profile_selector():
    base = {"NM_WATCHDOG_INTERFACE": "eno1"}
    with pytest.raises(watchdog.WatchdogError, match="exactly one"):
        watchdog.load_config(base)
    both = dict(base, NM_WATCHDOG_CONNECTION_NAME="wired", NM_WATCHDOG_CONNECTION_UUID=UUID)
    with pytest.raises(watchdog.WatchdogError, match="exactly one"):
        watchdog.load_config(both)
    loaded = watchdog.load_config(dict(base, NM_WATCHDOG_CONNECTION_UUID=UUID))
    assert loaded.connection_selector == "uuid"
    assert loaded.connection_value == UUID

    with pytest.raises(watchdog.WatchdogError, match="NAME is invalid"):
        watchdog.load_config(dict(base, NM_WATCHDOG_CONNECTION_NAME="--wait"))


def test_subprocess_runner_enforces_timeout_and_never_uses_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    result = watchdog.SubprocessRunner(7)(["/usr/bin/nmcli", "networking"])
    assert result.timed_out is True
    assert result.returncode == 124
    assert captured["command"] == ["/usr/bin/nmcli", "networking"]
    assert captured["kwargs"]["timeout"] == 7
    assert "shell" not in captured["kwargs"]
    assert "PYTHONPATH" not in captured["kwargs"]["env"]
    assert "LD_PRELOAD" not in captured["kwargs"]["env"]


def test_carrier_down_never_calls_nmcli_or_recovers(tmp_path):
    runner = FakeRunner()
    state = watchdog.default_state(1_000)
    root = carrier_tree(tmp_path, "0")
    for index in range(4):
        observation = watchdog.observe(config(), Path("/usr/bin/nmcli"), root, runner)
        result = watchdog.evaluate_cycle(
            config(), state, observation, 1_000 + index * 120, Path("/usr/bin/nmcli"), runner
        )
    assert runner.commands == []
    assert state["consecutive_failures"] == 4
    assert state["actions_today"] == 0
    assert result.outcome == "recorded_no_recovery"


def test_networking_disabled_requires_three_checks_then_only_enables_networking(tmp_path):
    runner = FakeRunner(networking="disabled", static_interface="eno1")
    state = watchdog.default_state(2_000)
    root = carrier_tree(tmp_path)
    results = []
    snapshots, writer = state_writer_snapshots()
    for index in range(3):
        observation = watchdog.observe(config(), Path("/usr/bin/nmcli"), root, runner)
        results.append(
            watchdog.evaluate_cycle(
                config(),
                state,
                observation,
                2_000 + index * 120,
                Path("/usr/bin/nmcli"),
                runner,
                state_writer=writer,
            )
        )
    assert [result.action_attempted for result in results] == [False, False, True]
    assert runner.commands[-1] == ["/usr/bin/nmcli", "networking", "on"]
    assert state["actions_today"] == 1
    assert state["consecutive_failures"] == 0


def test_networking_disabled_with_missing_profile_never_enables_networking(tmp_path):
    runner = FakeRunner(networking="disabled", profile_uuid="")
    observation = watchdog.observe(
        config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    assert observation.state == "inspection_error"
    assert observation.recoverable_action is None
    assert not any(command[-2:] == ["networking", "on"] for command in runner.commands)
    assert not any(command[-1:] == ["networking"] for command in runner.commands)


def test_networking_disabled_with_mismatched_profile_never_enables_networking(tmp_path):
    runner = FakeRunner(networking="disabled", static_interface="eth9")
    observation = watchdog.observe(
        config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    assert observation.state == "profile_interface_mismatch"
    assert observation.recoverable_action is None
    assert not any(command[-2:] == ["networking", "on"] for command in runner.commands)
    assert not any(command[-1:] == ["networking"] for command in runner.commands)


def test_networking_disabled_with_unbound_profile_never_enables_networking(tmp_path):
    runner = FakeRunner(networking="disabled", static_interface="")
    observation = watchdog.observe(
        config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    assert observation.state == "profile_interface_unbound"
    assert observation.recoverable_action is None
    assert not any(command[-2:] == ["networking", "on"] for command in runner.commands)


def test_inactive_profile_uses_exact_uuid_after_three_checks(tmp_path):
    # This matches real nmcli behavior: the active summary is empty when the
    # static profile exists but has no active instance.
    runner = FakeRunner(active_rows="")
    state = watchdog.default_state(3_000)
    root = carrier_tree(tmp_path)
    snapshots, writer = state_writer_snapshots()
    for index in range(3):
        observation = watchdog.observe(config(), Path("/usr/bin/nmcli"), root, runner)
        result = watchdog.evaluate_cycle(
            config(), state, observation, 3_000 + index * 120, Path("/usr/bin/nmcli"), runner,
            state_writer=writer,
        )
    assert result.action_attempted is True
    assert runner.commands[-1] == [
        "/usr/bin/nmcli",
        "connection",
        "up",
        "uuid",
        UUID,
        "ifname",
        "eno1",
    ]


def test_cooldown_and_daily_limit_prevent_actions():
    observation = watchdog.Observation("profile_inactive", False, "connection_up", "inactive")
    state = watchdog.default_state(4_000)
    prime_failure_state(state, "connection_up")
    state["last_action_at_epoch"] = 3_500
    runner = FakeRunner()
    cooldown = watchdog.evaluate_cycle(
        config(), state, observation, 4_000, Path("/usr/bin/nmcli"), runner
    )
    assert cooldown.outcome == "cooldown"
    assert cooldown.action_attempted is False

    state["actions_today"] = watchdog.MAX_ACTIONS_PER_UTC_DAY
    state["last_action_at_epoch"] = 0
    limited = watchdog.evaluate_cycle(
        config(), state, observation, 5_000, Path("/usr/bin/nmcli"), runner
    )
    assert limited.outcome == "daily_limit"
    assert not any(command[1:3] == ["connection", "up"] for command in runner.commands)


def test_failed_or_timed_out_action_consumes_budget_and_requires_fresh_failures():
    observation = watchdog.Observation("profile_inactive", False, "connection_up", "inactive")
    state = watchdog.default_state(6_000)
    prime_failure_state(state, "connection_up")
    runner = FakeRunner(action_result=watchdog.CommandResult(124, timed_out=True))
    snapshots, writer = state_writer_snapshots()
    result = watchdog.evaluate_cycle(
        config(), state, observation, 6_000, Path("/usr/bin/nmcli"), runner,
        state_writer=writer,
    )
    assert result.exit_code == 1
    assert result.outcome == "action_failed"
    assert state["actions_today"] == 1
    assert state["consecutive_failures"] == 0
    assert state["last_action_succeeded"] is False


def test_healthy_cycle_resets_failures_without_action():
    state = watchdog.default_state(7_000)
    state["consecutive_failures"] = 12
    observation = watchdog.Observation("healthy", True, None, "active")
    runner = FakeRunner()
    result = watchdog.evaluate_cycle(
        config(), state, observation, 7_000, Path("/usr/bin/nmcli"), runner
    )
    assert result.outcome == "healthy"
    assert state["consecutive_failures"] == 0
    assert runner.commands == []


def test_dry_run_never_executes_recovery_and_caller_can_avoid_state_write(tmp_path):
    original = watchdog.default_state(8_000)
    prime_failure_state(original, "connection_up")
    working = dict(original)
    observation = watchdog.Observation("profile_inactive", False, "connection_up", "inactive")
    runner = FakeRunner()
    result = watchdog.evaluate_cycle(
        config(), working, observation, 8_000, Path("/usr/bin/nmcli"), runner, dry_run=True
    )
    assert result.outcome == "dry_run_action_due"
    assert runner.commands == []
    assert original["consecutive_failures"] == 2
    assert not (tmp_path / "state.json").exists()


def test_action_budget_is_reserved_before_nmcli_and_outcome_is_persisted():
    observation = watchdog.Observation("profile_inactive", False, "connection_up", "inactive")
    state = watchdog.default_state(8_500)
    prime_failure_state(state, "connection_up")
    events = []

    def writer(current):
        events.append(("write", dict(current)))

    runner = FakeRunner(events=events)
    result = watchdog.evaluate_cycle(
        config(),
        state,
        observation,
        8_500,
        Path("/usr/bin/nmcli"),
        runner,
        state_writer=writer,
    )
    assert result.action_succeeded is True
    assert [event[0] for event in events] == ["write", "action", "write"]
    reservation = events[0][1]
    outcome = events[2][1]
    assert reservation["actions_today"] == 1
    assert reservation["last_action_at_epoch"] == 8_500
    assert reservation["last_action_succeeded"] is None
    assert outcome["last_action_succeeded"] is True
    assert outcome["last_action_outcome_at_epoch"] == 8_500


def test_reservation_write_failure_prevents_nmcli_action():
    observation = watchdog.Observation("profile_inactive", False, "connection_up", "inactive")
    state = watchdog.default_state(8_600)
    prime_failure_state(state, "connection_up")
    runner = FakeRunner()

    def failing_writer(_state):
        raise OSError("disk unavailable")

    with pytest.raises(OSError, match="disk unavailable"):
        watchdog.evaluate_cycle(
            config(),
            state,
            observation,
            8_600,
            Path("/usr/bin/nmcli"),
            runner,
            state_writer=failing_writer,
        )
    assert runner.commands == []


def test_carrier_failures_do_not_count_toward_profile_recovery(tmp_path):
    state = watchdog.default_state(8_700)
    down_runner = FakeRunner()
    down_root = carrier_tree(tmp_path, "0")
    for index in range(4):
        observation = watchdog.observe(config(), Path("/usr/bin/nmcli"), down_root, down_runner)
        watchdog.evaluate_cycle(
            config(), state, observation, 8_700 + index * 120, Path("/usr/bin/nmcli"), down_runner
        )
    assert state["consecutive_failures"] == 4

    (down_root / "eno1" / "carrier").write_text("1\n", encoding="ascii")
    inactive_runner = FakeRunner(profile="deactivated")
    observation = watchdog.observe(config(), Path("/usr/bin/nmcli"), down_root, inactive_runner)
    result = watchdog.evaluate_cycle(
        config(), state, observation, 9_200, Path("/usr/bin/nmcli"), inactive_runner
    )
    assert result.outcome == "failure_threshold"
    assert result.consecutive_failures == 1
    assert not any(command[1:3] == ["connection", "up"] for command in inactive_runner.commands)


def test_transitioning_profile_is_observation_only_and_clears_failure_chain(tmp_path):
    state = watchdog.default_state(9_300)
    prime_failure_state(state, "carrier_down", count=20)
    runner = FakeRunner(profile="activating")
    observation = watchdog.observe(
        config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    result = watchdog.evaluate_cycle(
        config(), state, observation, 9_300, Path("/usr/bin/nmcli"), runner
    )
    assert result.state == "profile_transitioning"
    assert result.outcome == "observed_no_recovery"
    assert state["consecutive_failures"] == 0
    assert not any(command[1:3] == ["connection", "up"] for command in runner.commands)


def test_active_profile_must_be_bound_to_configured_interface(tmp_path):
    runner = FakeRunner(profile="activated", devices="eth9")
    observation = watchdog.observe(
        config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    assert observation.state == "profile_other_interface"
    assert observation.recoverable_action is None


def test_static_profile_bound_to_other_interface_is_observation_only(tmp_path):
    runner = FakeRunner(static_interface="eth9", active_rows="")
    observation = watchdog.observe(
        config(), Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    assert observation.state == "profile_interface_mismatch"
    assert observation.recoverable_action is None
    assert not any("--active" in command for command in runner.commands)


def test_name_selector_resolves_and_activates_exact_static_uuid(tmp_path):
    named_config = watchdog.Config("eno1", "id", "Example profile", 15)
    runner = FakeRunner(active_rows="")
    observation = watchdog.observe(
        named_config, Path("/usr/bin/nmcli"), carrier_tree(tmp_path), runner
    )
    assert observation.connection_uuid == UUID
    state = watchdog.default_state(9_350)
    state["config_fingerprint"] = watchdog.config_fingerprint(named_config)
    state["failure_class"] = "connection_up"
    state["consecutive_failures"] = 2
    snapshots = []
    result = watchdog.evaluate_cycle(
        named_config,
        state,
        observation,
        9_350,
        Path("/usr/bin/nmcli"),
        runner,
        state_writer=lambda current: snapshots.append(dict(current)),
    )
    assert result.action_attempted is True
    assert runner.commands[-1][3:5] == ["uuid", UUID]


def test_maintenance_inhibit_resets_actionable_chain_without_nmcli():
    state = watchdog.default_state(9_400)
    prime_failure_state(state, "connection_up", count=99)
    result = watchdog.record_maintenance_inhibit(config(), state, 9_400)
    assert result.outcome == "maintenance_inhibited"
    assert state["maintenance_inhibited"] is True
    assert state["last_observed_state"] == "maintenance_inhibited"
    assert state["consecutive_failures"] == 0
    assert state["failure_class"] is None


def test_config_identity_change_resets_failure_chain_but_not_action_budget():
    state = watchdog.default_state(9_500)
    prime_failure_state(state, "connection_up", count=2)
    state["actions_today"] = 2
    changed = watchdog.Config("eno1", "id", "different profile", 15)
    observation = watchdog.Observation("profile_inactive", False, "connection_up", "inactive")
    result = watchdog.evaluate_cycle(
        changed, state, observation, 9_500, Path("/usr/bin/nmcli"), FakeRunner()
    )
    assert result.outcome == "failure_threshold"
    assert result.consecutive_failures == 1
    assert state["actions_today"] == 2
    assert state["config_fingerprint"] == watchdog.config_fingerprint(changed)


def test_main_inhibit_returns_before_any_nmcli_observation(monkeypatch, tmp_path):
    persisted = []
    observed = []

    class Lock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(watchdog.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        watchdog,
        "read_root_only_config",
        lambda _path: {
            "NM_WATCHDOG_INTERFACE": "eno1",
            "NM_WATCHDOG_CONNECTION_UUID": UUID,
            "NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS": "15",
        },
    )
    monkeypatch.setattr(watchdog, "ExclusiveLock", Lock)
    monkeypatch.setattr(watchdog, "load_state", lambda _path, now: watchdog.default_state(now))
    monkeypatch.setattr(watchdog, "maintenance_inhibit_active", lambda _path: True)
    monkeypatch.setattr(
        watchdog,
        "observe",
        lambda *_args: observed.append(True),
    )
    monkeypatch.setattr(
        watchdog,
        "atomic_write_state",
        lambda _path, state: persisted.append(dict(state)),
    )
    arguments = [
        "--config-file",
        str(tmp_path / "config"),
        "--state-file",
        str(tmp_path / "state"),
        "--lock-file",
        str(tmp_path / "lock"),
        "--inhibit-file",
        str(tmp_path / "inhibit"),
        "--nmcli",
        "/usr/bin/nmcli",
    ]
    result = watchdog.main(arguments)
    assert result == 0
    assert observed == []
    assert persisted[0]["maintenance_inhibited"] is True
    assert watchdog.main(arguments + ["--status"]) == 3
    assert len(persisted) == 1


def test_atomic_state_is_private_and_corruption_fails_closed(tmp_path):
    path = tmp_path / "private" / "state.json"
    state = watchdog.default_state(9_000)
    watchdog.atomic_write_state(path, state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(watchdog.WatchdogError, match="cannot trust"):
        watchdog.load_state(path, 9_001)


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), True])
def test_state_rejects_negative_nonfinite_or_boolean_timestamps(tmp_path, invalid):
    path = tmp_path / "state.json"
    state = watchdog.default_state(9_100)
    state["last_action_at_epoch"] = invalid
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(watchdog.WatchdogError, match="timestamp"):
        watchdog.load_state(path, 9_101)


def test_utc_day_rollover_resets_only_daily_action_budget(tmp_path):
    path = tmp_path / "state.json"
    before = 1_700_000_000.0
    state = watchdog.default_state(before)
    state["actions_today"] = 3
    watchdog.atomic_write_state(path, state)
    later = before + 2 * 86_400
    loaded = watchdog.load_state(path, later)
    assert loaded["actions_today"] == 0
    assert loaded["actions_day_utc"] == watchdog.utc_day(later)
