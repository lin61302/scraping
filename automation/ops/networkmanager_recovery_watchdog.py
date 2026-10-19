#!/usr/bin/env python3
"""Conservative NetworkManager connectivity recovery watchdog.

The watchdog is intended to run from a systemd oneshot service.  It observes one
interface and one NetworkManager connection profile.  Recovery is deliberately
narrow:

* a link with no carrier is recorded but never manipulated;
* disabled NetworkManager networking may be enabled once;
* an inactive configured profile may be activated once;
* driver reloads, device resets, and reboots are never attempted.

State is kept across timer invocations so recovery is attempted only after three
consecutive failed checks, no more often than every 15 minutes, and at most
three times per UTC day.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
FAILURES_BEFORE_ACTION = 3
COOLDOWN_SECONDS = 15 * 60
MAX_ACTIONS_PER_UTC_DAY = 3
DEFAULT_COMMAND_TIMEOUT_SECONDS = 15
MAX_COMMAND_TIMEOUT_SECONDS = 60
DEFAULT_STATE_FILE = Path("/var/lib/mlp-network-recovery-watchdog/state.json")
DEFAULT_LOCK_FILE = Path("/run/mlp-network-recovery-watchdog/watchdog.lock")
DEFAULT_INHIBIT_FILE = Path("/run/mlp-network-recovery-watchdog.inhibit")
DEFAULT_CONFIG_FILE = Path("/etc/mlp-network-recovery-watchdog.conf")
DEFAULT_NMCLI = Path("/usr/bin/nmcli")
DEFAULT_SYS_CLASS_NET = Path("/sys/class/net")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONFIG_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)='([^']*)'$")
ALLOWED_CONFIG_KEYS = frozenset(
    {
        "NM_WATCHDOG_INTERFACE",
        "NM_WATCHDOG_CONNECTION_NAME",
        "NM_WATCHDOG_CONNECTION_UUID",
        "NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS",
    }
)
MAX_CONFIG_BYTES = 8192

LOG = logging.getLogger("networkmanager-recovery-watchdog")


class WatchdogError(RuntimeError):
    """Raised when configuration, state, or observation cannot be trusted."""


@dataclasses.dataclass(frozen=True)
class Config:
    interface: str
    connection_selector: str
    connection_value: str
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclasses.dataclass(frozen=True)
class Observation:
    state: str
    healthy: bool
    recoverable_action: Optional[str]
    detail: str
    connection_uuid: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CycleResult:
    state: str
    outcome: str
    exit_code: int
    consecutive_failures: int
    actions_today: int
    action: Optional[str]
    action_attempted: bool
    action_succeeded: Optional[bool]

    def as_dict(self) -> Dict[str, object]:
        return dataclasses.asdict(self)


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def read_root_only_config(path: Path, expected_uid: int = 0) -> Dict[str, str]:
    """Read a root-only, non-symlink config with a strict non-shell grammar."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise WatchdogError("cannot open protected config file: {}".format(exc)) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WatchdogError("config file is not a regular file")
        if info.st_uid != expected_uid:
            raise WatchdogError("config file is not owned by the required uid")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise WatchdogError("config file must not be group- or world-accessible")
        if info.st_size > MAX_CONFIG_BYTES:
            raise WatchdogError("config file exceeds the size limit")
        chunks: List[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_CONFIG_BYTES:
            raise WatchdogError("config file exceeds the size limit")
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WatchdogError("config file is not valid UTF-8") from exc
    values: Dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = CONFIG_LINE_RE.fullmatch(stripped)
        if not match:
            raise WatchdogError(
                "config file line {} does not use strict KEY='VALUE' syntax".format(
                    line_number
                )
            )
        key, value = match.groups()
        if key not in ALLOWED_CONFIG_KEYS:
            raise WatchdogError("config file contains an unknown key")
        if key in values:
            raise WatchdogError("config file contains a duplicate key")
        values[key] = value
    return values


def validate_root_only_config(path: Path, expected_uid: int = 0) -> None:
    """Compatibility wrapper used by tests and preflight checks."""

    read_root_only_config(path, expected_uid=expected_uid)


def load_config(environment: Mapping[str, str]) -> Config:
    interface = environment.get("NM_WATCHDOG_INTERFACE", "").strip()
    connection_name = environment.get("NM_WATCHDOG_CONNECTION_NAME", "").strip()
    connection_uuid = environment.get("NM_WATCHDOG_CONNECTION_UUID", "").strip()
    timeout_text = environment.get(
        "NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS", str(DEFAULT_COMMAND_TIMEOUT_SECONDS)
    ).strip()

    if not INTERFACE_RE.fullmatch(interface):
        raise WatchdogError("NM_WATCHDOG_INTERFACE is missing or invalid")
    if bool(connection_name) == bool(connection_uuid):
        raise WatchdogError(
            "set exactly one of NM_WATCHDOG_CONNECTION_NAME or NM_WATCHDOG_CONNECTION_UUID"
        )
    if connection_uuid:
        if not UUID_RE.fullmatch(connection_uuid):
            raise WatchdogError("NM_WATCHDOG_CONNECTION_UUID is not a canonical UUID")
        selector = "uuid"
        value = connection_uuid.lower()
    else:
        if (
            len(connection_name) > 256
            or connection_name.startswith("-")
            or _has_control_character(connection_name)
        ):
            raise WatchdogError("NM_WATCHDOG_CONNECTION_NAME is invalid")
        selector = "id"
        value = connection_name
    try:
        timeout = int(timeout_text)
    except ValueError as exc:
        raise WatchdogError("NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS is not an integer") from exc
    if timeout < 1 or timeout > MAX_COMMAND_TIMEOUT_SECONDS:
        raise WatchdogError(
            "NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS must be between 1 and {}".format(
                MAX_COMMAND_TIMEOUT_SECONDS
            )
        )
    return Config(interface, selector, value, timeout)


def default_state(now: float) -> Dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "consecutive_failures": 0,
        "actions_day_utc": utc_day(now),
        "actions_today": 0,
        "last_action_at_epoch": None,
        "last_action": None,
        "last_action_succeeded": None,
        "last_action_outcome_at_epoch": None,
        "failure_class": None,
        "config_fingerprint": None,
        "maintenance_inhibited": False,
        "last_observed_state": None,
        "last_transition_at_epoch": None,
        "updated_at_epoch": None,
    }


def utc_day(now: float) -> str:
    return dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).date().isoformat()


def _validate_state(state: object) -> Dict[str, object]:
    if not isinstance(state, dict):
        raise WatchdogError("state must be a JSON object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise WatchdogError("state schema version is unsupported")
    for key in ("consecutive_failures", "actions_today"):
        value = state.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WatchdogError("state field {} is invalid".format(key))
    day = state.get("actions_day_utc")
    if not isinstance(day, str):
        raise WatchdogError("state action day is invalid")
    try:
        dt.date.fromisoformat(day)
    except ValueError as exc:
        raise WatchdogError("state action day is invalid") from exc
    for key in (
        "last_action_at_epoch",
        "last_action_outcome_at_epoch",
        "last_transition_at_epoch",
        "updated_at_epoch",
    ):
        timestamp = state.get(key)
        if timestamp is not None and (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0
        ):
            raise WatchdogError("state timestamp {} is invalid".format(key))
    fingerprint = state.get("config_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint)
    ):
        raise WatchdogError("state config fingerprint is invalid")
    failure_class = state.get("failure_class")
    if failure_class is not None and (
        not isinstance(failure_class, str)
        or len(failure_class) > 64
        or _has_control_character(failure_class)
    ):
        raise WatchdogError("state failure class is invalid")
    maintenance_inhibited = state.get("maintenance_inhibited", False)
    if not isinstance(maintenance_inhibited, bool):
        raise WatchdogError("state maintenance inhibit flag is invalid")
    return dict(state)


def load_state(path: Path, now: float) -> Dict[str, object]:
    if not path.exists():
        return default_state(now)
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Do not reset a corrupt state: doing so could erase the recovery budget.
        raise WatchdogError("cannot trust persisted watchdog state: {}".format(exc)) from exc
    state = _validate_state(state)
    if state["actions_day_utc"] != utc_day(now):
        state["actions_day_utc"] = utc_day(now)
        state["actions_today"] = 0
    return state


def atomic_write_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(path.parent), 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".state.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self) -> "ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.path.parent), 0o700)
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(self.fd, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise WatchdogError("another watchdog cycle is already running") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class SubprocessRunner:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    def __call__(self, command: Sequence[str]) -> CommandResult:
        # Do not forward Python, loader, or shell environment variables into a
        # root nmcli subprocess.
        environment = {
            "LC_ALL": "C",
            "LANG": "C",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        }
        try:
            completed = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, timed_out=True)
        return CommandResult(
            completed.returncode,
            completed.stdout[:4096],
            completed.stderr[:4096],
            False,
        )


def read_carrier(sys_class_net: Path, interface: str) -> int:
    path = sys_class_net / interface / "carrier"
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise WatchdogError("cannot read interface carrier state: {}".format(exc)) from exc
    if value not in ("0", "1"):
        raise WatchdogError("interface carrier state is neither 0 nor 1")
    return int(value)


def maintenance_inhibit_active(path: Path, expected_uid: int = 0) -> bool:
    """Return true only for a regular, private inhibit file owned by root."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(before.st_mode):
        raise WatchdogError("maintenance inhibit path must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise WatchdogError("maintenance inhibit path is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise WatchdogError("cannot safely open maintenance inhibit file: {}".format(exc)) from exc
    try:
        info = os.fstat(descriptor)
        if info.st_dev != before.st_dev or info.st_ino != before.st_ino:
            raise WatchdogError("maintenance inhibit file changed during validation")
        if not stat.S_ISREG(info.st_mode):
            raise WatchdogError("maintenance inhibit path is not a regular file")
        if info.st_uid != expected_uid:
            raise WatchdogError("maintenance inhibit file has the wrong owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise WatchdogError("maintenance inhibit file must have mode 0600 or stricter")
    finally:
        os.close(descriptor)
    return True


def observe(
    config: Config,
    nmcli: Path,
    sys_class_net: Path,
    runner: Callable[[Sequence[str]], CommandResult],
) -> Observation:
    carrier = read_carrier(sys_class_net, config.interface)
    if carrier == 0:
        return Observation("carrier_down", False, None, "physical carrier is absent")

    # Resolve and bind the configured static profile before considering the
    # global ``nmcli networking on`` action. Static profile inspection works
    # while NetworkManager networking is disabled; a stale, ambiguous, or
    # wrong-interface selector must never authorize a global state change.
    profile_uuid_result = runner(
        [
            str(nmcli),
            "--escape",
            "no",
            "-g",
            "connection.uuid",
            "connection",
            "show",
            config.connection_selector,
            config.connection_value,
        ]
    )
    if profile_uuid_result.timed_out:
        return Observation("inspection_error", False, None, "static profile query timed out")
    if profile_uuid_result.returncode != 0:
        return Observation("inspection_error", False, None, "static profile query failed")
    uuid_lines = [line.strip().lower() for line in profile_uuid_result.stdout.splitlines() if line.strip()]
    if len(uuid_lines) != 1 or not UUID_RE.fullmatch(uuid_lines[0]):
        return Observation("inspection_error", False, None, "static profile UUID is ambiguous")
    profile_uuid = uuid_lines[0]

    profile_interface_result = runner(
        [
            str(nmcli),
            "--escape",
            "no",
            "-g",
            "connection.interface-name",
            "connection",
            "show",
            "uuid",
            profile_uuid,
        ]
    )
    if profile_interface_result.timed_out:
        return Observation("inspection_error", False, None, "static interface query timed out")
    if profile_interface_result.returncode != 0:
        return Observation("inspection_error", False, None, "static interface query failed")
    static_interfaces = [line.strip() for line in profile_interface_result.stdout.splitlines()]
    if len(static_interfaces) > 1:
        return Observation("inspection_error", False, None, "static interface is ambiguous")
    static_interface = static_interfaces[0] if static_interfaces else ""
    if static_interface not in ("", "--", config.interface):
        return Observation(
            "profile_interface_mismatch",
            False,
            None,
            "static profile is bound to another interface",
            profile_uuid,
        )

    networking = runner([str(nmcli), "networking"])
    if networking.timed_out:
        return Observation("inspection_error", False, None, "networking query timed out")
    if networking.returncode != 0:
        return Observation("inspection_error", False, None, "networking query failed")
    networking_state = networking.stdout.strip().lower()
    if networking_state == "disabled":
        if static_interface != config.interface:
            return Observation(
                "profile_interface_unbound",
                False,
                None,
                "global networking recovery requires an exact target-interface binding",
                profile_uuid,
            )
        return Observation(
            "networking_disabled",
            False,
            "networking_on",
            "NetworkManager networking is disabled for a validated target profile",
            profile_uuid,
        )
    if networking_state != "enabled":
        return Observation("inspection_error", False, None, "unexpected networking state")

    active_result = runner(
        [
            str(nmcli),
            "--escape",
            "no",
            "-t",
            "-f",
            "UUID,STATE,DEVICE",
            "connection",
            "show",
            "--active",
        ]
    )
    if active_result.timed_out:
        return Observation("inspection_error", False, None, "active profile query timed out")
    if active_result.returncode != 0:
        return Observation("inspection_error", False, None, "active profile query failed")
    matching_rows: List[Tuple[str, str]] = []
    for line in active_result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split(":", 2)
        if len(fields) != 3:
            return Observation("inspection_error", False, None, "active profile row is malformed")
        row_uuid, row_state, row_device = fields
        if row_uuid.strip().lower() == profile_uuid:
            matching_rows.append((row_state.strip().lower(), row_device.strip()))

    if not matching_rows:
        return Observation(
            "profile_inactive",
            False,
            "connection_up",
            "target profile has no active instance",
            profile_uuid,
        )
    if any(state == "activated" and device == config.interface for state, device in matching_rows):
        return Observation(
            "healthy",
            True,
            None,
            "carrier and target profile are active on the configured interface",
            profile_uuid,
        )
    if any(state in ("activating", "deactivating") for state, _device in matching_rows):
        return Observation(
            "profile_transitioning",
            False,
            None,
            "target profile is already transitioning",
            profile_uuid,
        )
    if all(state == "activated" for state, _device in matching_rows):
        return Observation(
            "profile_other_interface",
            False,
            None,
            "target profile is active on another interface",
            profile_uuid,
        )
    return Observation(
        "inspection_error", False, None, "active profile state is unexpected", profile_uuid
    )


def action_allowed(state: Mapping[str, object], now: float) -> Tuple[bool, str]:
    if int(state["consecutive_failures"]) < FAILURES_BEFORE_ACTION:
        return False, "failure_threshold"
    if int(state["actions_today"]) >= MAX_ACTIONS_PER_UTC_DAY:
        return False, "daily_limit"
    last_action = state.get("last_action_at_epoch")
    if last_action is not None:
        elapsed = max(0.0, now - float(last_action))
        if elapsed < COOLDOWN_SECONDS:
            return False, "cooldown"
    return True, "allowed"


def recovery_command(
    config: Config,
    nmcli: Path,
    action: str,
    connection_uuid: Optional[str] = None,
) -> List[str]:
    if action == "networking_on":
        return [str(nmcli), "networking", "on"]
    if action == "connection_up":
        exact_uuid = connection_uuid
        if exact_uuid is None and config.connection_selector == "uuid":
            exact_uuid = config.connection_value
        if exact_uuid is None or not UUID_RE.fullmatch(exact_uuid):
            raise WatchdogError("an exact validated profile UUID is required for activation")
        return [
            str(nmcli),
            "connection",
            "up",
            "uuid",
            exact_uuid,
            "ifname",
            config.interface,
        ]
    raise WatchdogError("unsupported recovery action")


def evaluate_cycle(
    config: Config,
    state: Dict[str, object],
    observation: Observation,
    now: float,
    nmcli: Path,
    runner: Callable[[Sequence[str]], CommandResult],
    dry_run: bool = False,
    state_writer: Optional[Callable[[Mapping[str, object]], None]] = None,
) -> CycleResult:
    synchronize_config_state(config, state)
    if state.get("maintenance_inhibited", False):
        LOG.info("maintenance_transition active=false interface=%s", config.interface)
        state["maintenance_inhibited"] = False
    previous_observed_state = state.get("last_observed_state")
    if previous_observed_state != observation.state:
        LOG.info(
            "state_transition previous=%s current=%s interface=%s",
            previous_observed_state or "none",
            observation.state,
            config.interface,
        )
        state["last_transition_at_epoch"] = now
    state["last_observed_state"] = observation.state
    state["updated_at_epoch"] = now

    if observation.healthy:
        state["consecutive_failures"] = 0
        state["failure_class"] = None
        return CycleResult(observation.state, "healthy", 0, 0, int(state["actions_today"]), None, False, None)

    if observation.state in ("profile_transitioning", "profile_other_interface"):
        state["consecutive_failures"] = 0
        state["failure_class"] = None
        return CycleResult(
            observation.state,
            "observed_no_recovery",
            0,
            0,
            int(state["actions_today"]),
            None,
            False,
            None,
        )

    failure_class = observation.recoverable_action or observation.state
    if state.get("failure_class") == failure_class:
        state["consecutive_failures"] = min(
            int(state["consecutive_failures"]) + 1, 1_000_000
        )
    else:
        state["failure_class"] = failure_class
        state["consecutive_failures"] = 1
    if observation.recoverable_action is None:
        outcome = "recorded_no_recovery" if observation.state == "carrier_down" else "inspection_failed_closed"
        exit_code = 0 if observation.state == "carrier_down" else 2
        return CycleResult(
            observation.state,
            outcome,
            exit_code,
            int(state["consecutive_failures"]),
            int(state["actions_today"]),
            None,
            False,
            None,
        )

    allowed, reason = action_allowed(state, now)
    if not allowed:
        return CycleResult(
            observation.state,
            reason,
            0,
            int(state["consecutive_failures"]),
            int(state["actions_today"]),
            observation.recoverable_action,
            False,
            None,
        )
    if dry_run:
        return CycleResult(
            observation.state,
            "dry_run_action_due",
            0,
            int(state["consecutive_failures"]),
            int(state["actions_today"]),
            observation.recoverable_action,
            False,
            None,
        )

    if state_writer is None:
        raise WatchdogError("durable state writer is required before a recovery action")

    # Reserve the daily budget and cooldown durably before invoking nmcli. If
    # the process or host dies during nmcli, the persisted reservation prevents
    # an immediate repeated action after restart.
    state["actions_today"] = int(state["actions_today"]) + 1
    state["last_action_at_epoch"] = now
    state["last_action"] = observation.recoverable_action
    state["last_action_succeeded"] = None
    state["last_action_outcome_at_epoch"] = None
    state["consecutive_failures"] = 0
    state["failure_class"] = None
    state_writer(state)

    command = recovery_command(
        config,
        nmcli,
        observation.recoverable_action,
        connection_uuid=observation.connection_uuid,
    )
    result = runner(command)
    succeeded = result.returncode == 0 and not result.timed_out
    state["last_action_succeeded"] = succeeded
    state["last_action_outcome_at_epoch"] = now
    state_writer(state)
    LOG.warning(
        "recovery_action action=%s succeeded=%s interface=%s daily_count=%s",
        observation.recoverable_action,
        str(succeeded).lower(),
        config.interface,
        state["actions_today"],
    )
    return CycleResult(
        observation.state,
        "action_succeeded" if succeeded else "action_failed",
        0 if succeeded else 1,
        0,
        int(state["actions_today"]),
        observation.recoverable_action,
        True,
        succeeded,
    )


def config_fingerprint(config: Config) -> str:
    canonical = json.dumps(
        {
            "interface": config.interface,
            "connection_selector": config.connection_selector,
            "connection_value": config.connection_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def synchronize_config_state(config: Config, state: Dict[str, object]) -> None:
    fingerprint = config_fingerprint(config)
    if state.get("config_fingerprint") != fingerprint:
        state["config_fingerprint"] = fingerprint
        state["consecutive_failures"] = 0
        state["failure_class"] = None
        state["last_observed_state"] = None


def record_maintenance_inhibit(
    config: Config, state: Dict[str, object], now: float
) -> CycleResult:
    synchronize_config_state(config, state)
    if not state.get("maintenance_inhibited", False):
        LOG.info("maintenance_transition active=true interface=%s", config.interface)
        state["last_transition_at_epoch"] = now
    state["maintenance_inhibited"] = True
    state["last_observed_state"] = "maintenance_inhibited"
    state["consecutive_failures"] = 0
    state["failure_class"] = None
    state["updated_at_epoch"] = now
    return CycleResult(
        "maintenance_inhibited",
        "maintenance_inhibited",
        0,
        0,
        int(state["actions_today"]),
        None,
        False,
        None,
    )


def status_result(
    config: Config,
    state: Dict[str, object],
    observation: Observation,
) -> Dict[str, object]:
    return {
        "mode": "status",
        "interface": config.interface,
        "profile_selector": config.connection_selector,
        "observed_state": observation.state,
        "healthy": observation.healthy,
        "detail": observation.detail,
        "persisted": {
            "consecutive_failures": state["consecutive_failures"],
            "actions_day_utc": state["actions_day_utc"],
            "actions_today": state["actions_today"],
            "last_action_at_epoch": state.get("last_action_at_epoch"),
            "last_action": state.get("last_action"),
            "last_action_succeeded": state.get("last_action_succeeded"),
            "last_observed_state": state.get("last_observed_state"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--inhibit-file", type=Path, default=DEFAULT_INHIBIT_FILE)
    parser.add_argument("--nmcli", type=Path, default=DEFAULT_NMCLI)
    parser.add_argument("--sys-class-net", type=Path, default=DEFAULT_SYS_CLASS_NET)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="evaluate without action or state writes")
    modes.add_argument("--status", action="store_true", help="inspect and print state without changing it")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        if os.geteuid() != 0:
            raise WatchdogError("watchdog must run as root")
        config = load_config(read_root_only_config(args.config_file))
        if not args.nmcli.is_absolute():
            raise WatchdogError("nmcli path must be absolute")
        now = time.time()
        runner = SubprocessRunner(config.command_timeout_seconds)
        with ExclusiveLock(args.lock_file):
            state = load_state(args.state_file, now)
            if maintenance_inhibit_active(args.inhibit_file):
                working_state = dict(state)
                result = record_maintenance_inhibit(config, working_state, now)
                if args.status or args.dry_run:
                    output = result.as_dict()
                    output["mode"] = "status" if args.status else "dry-run"
                    print(json.dumps(output, sort_keys=True))
                else:
                    atomic_write_state(args.state_file, working_state)
                return 3 if args.status else 0
            observation = observe(config, args.nmcli, args.sys_class_net, runner)
            if args.status:
                print(json.dumps(status_result(config, state, observation), sort_keys=True))
                return 0 if observation.healthy else 1
            working_state = dict(state)
            result = evaluate_cycle(
                config,
                working_state,
                observation,
                now,
                args.nmcli,
                runner,
                dry_run=args.dry_run,
                state_writer=(
                    None
                    if args.dry_run
                    else lambda reserved: atomic_write_state(args.state_file, reserved)
                ),
            )
            if args.dry_run:
                output = result.as_dict()
                output["mode"] = "dry-run"
                print(json.dumps(output, sort_keys=True))
            elif not result.action_attempted:
                atomic_write_state(args.state_file, working_state)
            return result.exit_code
    except WatchdogError as exc:
        LOG.error("fail_closed reason=%s", exc)
        return 2
    except OSError as exc:
        LOG.error("fail_closed reason=operating-system-error detail=%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
