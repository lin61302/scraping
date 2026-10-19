#!/usr/bin/env python3
"""Install the conservative NetworkManager recovery watchdog.

This installer never invokes NetworkManager. It installs a hash-pinned watchdog
script, a root-only strictly parsed config file, and reproducibly embedded
systemd units. The timer is enabled and started only when ``--enable`` and an
explicit preflight attestation are supplied for an identical prior install.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Sequence


WATCHDOG_DESTINATION = Path("/usr/local/libexec/mlp-network-recovery-watchdog")
CONFIG_DESTINATION = Path("/etc/mlp-network-recovery-watchdog.conf")
SERVICE_DESTINATION = Path("/etc/systemd/system/mlp-network-recovery-watchdog.service")
TIMER_DESTINATION = Path("/etc/systemd/system/mlp-network-recovery-watchdog.timer")
MAX_WATCHDOG_BYTES = 2 * 1024 * 1024
MAX_BACKUP_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 30
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRUSTED_SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


SERVICE_CONTENT = """[Unit]
Description=Conservative NetworkManager connectivity recovery watchdog
After=NetworkManager.service

[Service]
Type=oneshot
User=root
Group=root
Environment=PYTHONDONTWRITEBYTECODE=1 PYTHONPATH= PYTHONHOME= PYTHONINSPECT= PYTHONSTARTUP= LD_PRELOAD= LD_LIBRARY_PATH=
ExecStart=/usr/bin/python3 -I /usr/local/libexec/mlp-network-recovery-watchdog --config-file /etc/mlp-network-recovery-watchdog.conf --state-file /var/lib/mlp-network-recovery-watchdog/state.json --lock-file /run/mlp-network-recovery-watchdog/watchdog.lock
StateDirectory=mlp-network-recovery-watchdog
StateDirectoryMode=0700
RuntimeDirectory=mlp-network-recovery-watchdog
RuntimeDirectoryMode=0700
UMask=0077
TimeoutStartSec=75s
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
PrivateDevices=true
LockPersonality=true
MemoryDenyWriteExecute=true
"""


TIMER_CONTENT = """[Unit]
Description=Check NetworkManager connectivity every two minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=2min
AccuracySec=15s
RandomizedDelaySec=15s
Persistent=true
Unit=mlp-network-recovery-watchdog.service

[Install]
WantedBy=timers.target
"""


class InstallError(RuntimeError):
    """Raised when an installation precondition or bounded command fails."""


@dataclasses.dataclass(frozen=True)
class ProfileConfig:
    interface: str
    selector: str
    value: str
    command_timeout_seconds: int = 15


@dataclasses.dataclass(frozen=True)
class InstallPaths:
    watchdog: Path = WATCHDOG_DESTINATION
    config: Path = CONFIG_DESTINATION
    service: Path = SERVICE_DESTINATION
    timer: Path = TIMER_DESTINATION


@dataclasses.dataclass(frozen=True)
class Artifact:
    kind: str
    destination: Path
    content: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclasses.dataclass(frozen=True)
class ExistingArtifact:
    artifact: Artifact
    existed: bool
    content: bytes = b""
    mode: int = 0
    uid: int = 0
    gid: int = 0


class BoundedRunner:
    def __init__(self, timeout_seconds: int = COMMAND_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def __call__(self, command: Sequence[str]) -> CommandResult:
        environment = {
            "LC_ALL": "C",
            "LANG": "C",
            "PATH": TRUSTED_SYSTEM_PATH,
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
        except OSError as exc:
            return CommandResult(127, stderr="cannot execute bounded command: {}".format(exc))
        return CommandResult(
            completed.returncode,
            completed.stdout[:8192],
            completed.stderr[:8192],
            False,
        )


def validate_profile(
    interface: str,
    connection_uuid: Optional[str],
    connection_name: Optional[str],
    command_timeout_seconds: int,
) -> ProfileConfig:
    interface = interface.strip()
    connection_uuid = (connection_uuid or "").strip()
    connection_name = (connection_name or "").strip()
    if not INTERFACE_RE.fullmatch(interface):
        raise InstallError("interface is missing or invalid")
    if bool(connection_uuid) == bool(connection_name):
        raise InstallError("set exactly one connection UUID or connection name")
    if command_timeout_seconds < 1 or command_timeout_seconds > 60:
        raise InstallError("command timeout must be between 1 and 60 seconds")
    if connection_uuid:
        if not UUID_RE.fullmatch(connection_uuid):
            raise InstallError("connection UUID is not canonical")
        return ProfileConfig(interface, "uuid", connection_uuid.lower(), command_timeout_seconds)
    if (
        len(connection_name) > 256
        or connection_name.startswith("-")
        or "'" in connection_name
        or any(ord(character) < 32 or ord(character) == 127 for character in connection_name)
    ):
        raise InstallError("connection name is unsafe for the config file")
    return ProfileConfig(interface, "name", connection_name, command_timeout_seconds)


def render_config(profile: ProfileConfig) -> bytes:
    lines = [
        "# Managed by install_networkmanager_recovery_watchdog.py.",
        "NM_WATCHDOG_INTERFACE='{}'".format(profile.interface),
    ]
    if profile.selector == "uuid":
        lines.append("NM_WATCHDOG_CONNECTION_UUID='{}'".format(profile.value))
    else:
        # The watchdog's strict parser accepts only single-quoted allowlisted
        # values. Validation rejects an embedded single quote.
        lines.append("NM_WATCHDOG_CONNECTION_NAME='{}'".format(profile.value))
    lines.append(
        "NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS='{}'".format(profile.command_timeout_seconds)
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _path_components(path: Path) -> List[Path]:
    absolute = path if path.is_absolute() else path.absolute()
    components: List[Path] = [Path(absolute.anchor)]
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        components.append(current)
    return components


def reject_symlink_components(path: Path, allow_missing_leaf: bool = False) -> None:
    components = _path_components(path)
    for index, component in enumerate(components):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(components) - 1:
                return
            raise InstallError("required path component does not exist: {}".format(component))
        if stat.S_ISLNK(info.st_mode):
            raise InstallError("symlink path components are not allowed: {}".format(component))


def read_hash_pinned_source(path: Path, expected_sha256: str) -> bytes:
    expected_sha256 = expected_sha256.lower()
    if not SHA256_RE.fullmatch(expected_sha256):
        raise InstallError("watchdog SHA-256 is invalid")
    reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise InstallError("cannot open watchdog source: {}".format(exc)) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("watchdog source is not a regular file")
        if info.st_size > MAX_WATCHDOG_BYTES:
            raise InstallError("watchdog source exceeds the size limit")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_WATCHDOG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_WATCHDOG_BYTES:
                raise InstallError("watchdog source exceeds the size limit")
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != expected_sha256:
        raise InstallError("watchdog source does not match the required SHA-256")
    if not content.startswith(b"#!/usr/bin/env python3\n"):
        raise InstallError("watchdog source does not have the expected Python shebang")
    return content


def build_artifacts(
    watchdog_content: bytes, profile: ProfileConfig, paths: InstallPaths
) -> List[Artifact]:
    return [
        Artifact("watchdog", paths.watchdog, watchdog_content, 0o755),
        Artifact("config", paths.config, render_config(profile), 0o600),
        Artifact("service", paths.service, SERVICE_CONTENT.encode("utf-8"), 0o644),
        Artifact("timer", paths.timer, TIMER_CONTENT.encode("utf-8"), 0o644),
    ]


def validate_trusted_directory(path: Path, owner_uid: int) -> None:
    """Require every existing component to be root/trusted-owner and non-writable."""

    for component in _path_components(path):
        try:
            info = component.lstat()
        except FileNotFoundError as exc:
            raise InstallError("required directory does not exist: {}".format(component)) from exc
        if stat.S_ISLNK(info.st_mode):
            raise InstallError("symlink path components are not allowed: {}".format(component))
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError("path component is not a directory: {}".format(component))
        if info.st_uid not in {0, owner_uid}:
            raise InstallError("destination directory has an untrusted owner: {}".format(component))
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise InstallError("destination directory is group- or world-writable: {}".format(component))


def validate_destination(destination: Path, dry_run: bool, owner_uid: int = 0) -> None:
    parent = destination.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        validate_trusted_directory(parent.parent, owner_uid)
    else:
        if stat.S_ISLNK(parent_info.st_mode):
            raise InstallError("destination parent is a symlink: {}".format(parent))
        validate_trusted_directory(parent, owner_uid)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise InstallError("destination is a symlink: {}".format(destination))
    if not stat.S_ISREG(info.st_mode):
        raise InstallError("destination is not a regular file: {}".format(destination))
    if info.st_uid != owner_uid:
        raise InstallError("destination has an unexpected owner: {}".format(destination))
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise InstallError("destination is group- or world-writable: {}".format(destination))


def atomic_install_artifact(
    artifact: Artifact,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    validate_destination(artifact.destination, dry_run=False, owner_uid=owner_uid)
    if not artifact.destination.parent.exists():
        artifact.destination.parent.mkdir(parents=False, mode=0o755)
        os.chown(str(artifact.destination.parent), owner_uid, owner_gid)
        os.chmod(str(artifact.destination.parent), 0o755)
    validate_trusted_directory(artifact.destination.parent, owner_uid)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(artifact.destination.name), dir=str(artifact.destination.parent)
    )
    try:
        os.fchmod(descriptor, artifact.mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(artifact.content)
            handle.flush()
            os.fsync(handle.fileno())
        validate_destination(artifact.destination, dry_run=False, owner_uid=owner_uid)
        os.replace(temporary_name, str(artifact.destination))
        directory_descriptor = os.open(
            str(artifact.destination.parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def snapshot_artifact(artifact: Artifact, owner_uid: int) -> ExistingArtifact:
    validate_destination(artifact.destination, dry_run=False, owner_uid=owner_uid)
    try:
        before = artifact.destination.lstat()
    except FileNotFoundError:
        return ExistingArtifact(artifact, False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(artifact.destination), flags)
    try:
        info = os.fstat(descriptor)
        if info.st_dev != before.st_dev or info.st_ino != before.st_ino:
            raise InstallError("destination changed during backup")
        if info.st_size > MAX_BACKUP_BYTES:
            raise InstallError("existing artifact exceeds the backup size limit")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_BACKUP_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BACKUP_BYTES:
                raise InstallError("existing artifact exceeds the backup size limit")
    finally:
        os.close(descriptor)
    return ExistingArtifact(
        artifact=artifact,
        existed=True,
        content=b"".join(chunks),
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
    )


def remove_new_artifact(artifact: Artifact, owner_uid: int) -> None:
    try:
        artifact.destination.lstat()
    except FileNotFoundError:
        return
    validate_destination(artifact.destination, dry_run=False, owner_uid=owner_uid)
    artifact.destination.unlink()
    directory_descriptor = os.open(
        str(artifact.destination.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def restore_snapshot(snapshot: ExistingArtifact, owner_uid: int) -> None:
    if not snapshot.existed:
        remove_new_artifact(snapshot.artifact, owner_uid)
        return
    restore_artifact = Artifact(
        snapshot.artifact.kind,
        snapshot.artifact.destination,
        snapshot.content,
        snapshot.mode,
    )
    atomic_install_artifact(
        restore_artifact,
        owner_uid=snapshot.uid,
        owner_gid=snapshot.gid,
    )


def stage_artifacts(artifacts: Sequence[Artifact], directory: Path) -> Dict[str, Path]:
    staged: Dict[str, Path] = {}
    for artifact in artifacts:
        destination = directory / artifact.destination.name
        with destination.open("xb") as handle:
            handle.write(artifact.content)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(artifact.mode)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != artifact.sha256:
            raise InstallError("staged artifact hash mismatch")
        staged[artifact.kind] = destination
    return staged


def command_succeeded(result: CommandResult, description: str) -> None:
    if result.timed_out:
        raise InstallError("{} timed out".format(description))
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = ": {}".format(detail[0][:300]) if detail else ""
        raise InstallError("{} failed{}".format(description, suffix))


def require_unit_inactive(
    runner: Callable[[Sequence[str]], CommandResult], systemctl_path: str, unit: str
) -> None:
    """Require one unit to be fully quiescent with no queued systemd job.

    ``systemctl is-active`` is not sufficient for a oneshot service because it
    returns nonzero while the unit is still in ``activating``.  Query the
    machine-readable properties instead and accept only the exact idle state.
    """

    expected_keys = ("LoadState", "ActiveState", "SubState", "Job")
    result = runner(
        [
            systemctl_path,
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=Job",
            unit,
        ]
    )
    if result.timed_out:
        raise InstallError("unit activity check timed out")
    if result.returncode != 0:
        raise InstallError("cannot establish that {} is inactive".format(unit))

    properties: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line or "=" not in line:
            raise InstallError("cannot establish that {} is inactive".format(unit))
        key, value = line.split("=", 1)
        if key not in expected_keys or key in properties:
            raise InstallError("cannot establish that {} is inactive".format(unit))
        properties[key] = value
    if set(properties) != set(expected_keys):
        raise InstallError("cannot establish that {} is inactive".format(unit))
    if properties["LoadState"] not in ("loaded", "not-found"):
        raise InstallError("cannot establish that {} is inactive".format(unit))
    if properties["Job"]:
        raise InstallError("refusing installation while {} has a queued job".format(unit))
    if properties["ActiveState"] != "inactive" or properties["SubState"] != "dead":
        raise InstallError(
            "refusing installation while {} is not quiescent "
            "(ActiveState={}, SubState={})".format(
                unit,
                properties["ActiveState"],
                properties["SubState"],
            )
        )


def timer_was_enabled(
    runner: Callable[[Sequence[str]], CommandResult], systemctl_path: str
) -> bool:
    result = runner(
        [systemctl_path, "is-enabled", "--quiet", "mlp-network-recovery-watchdog.timer"]
    )
    if result.timed_out:
        raise InstallError("timer enablement check timed out")
    if result.returncode == 0:
        return True
    if result.returncode in (1, 3, 4):
        return False
    raise InstallError("cannot establish the timer enablement state")


def attempt_rollback(
    snapshots: Sequence[ExistingArtifact],
    *,
    runner: Callable[[Sequence[str]], CommandResult],
    systemctl_path: str,
    owner_uid: int,
    enable_attempted: bool,
    previously_enabled: bool,
) -> List[str]:
    errors: List[str] = []
    if enable_attempted:
        try:
            result = runner(
                [
                    systemctl_path,
                    "disable",
                    "--now",
                    "mlp-network-recovery-watchdog.timer",
                ]
            )
        except OSError as exc:
            errors.append("could not stop/disable timer: {}".format(exc))
        else:
            if result.timed_out or result.returncode != 0:
                errors.append("could not stop/disable timer")
    for snapshot in reversed(list(snapshots)):
        try:
            restore_snapshot(snapshot, owner_uid)
        except (InstallError, OSError) as exc:
            errors.append("could not restore {}: {}".format(snapshot.artifact.kind, exc))
    try:
        reload_result = runner([systemctl_path, "daemon-reload"])
    except OSError as exc:
        errors.append("could not reload restored units: {}".format(exc))
    else:
        if reload_result.timed_out or reload_result.returncode != 0:
            errors.append("could not reload restored units")
    if enable_attempted and previously_enabled:
        try:
            enabled_result = runner(
                [systemctl_path, "enable", "mlp-network-recovery-watchdog.timer"]
            )
        except OSError as exc:
            errors.append("could not restore prior enabled state: {}".format(exc))
        else:
            if enabled_result.timed_out or enabled_result.returncode != 0:
                errors.append("could not restore prior enabled state")
    return errors


def find_system_command(name: str) -> Optional[str]:
    """Resolve only from standard root-controlled system directories."""

    return shutil.which(name, path=TRUSTED_SYSTEM_PATH)


def public_plan(
    artifacts: Sequence[Artifact],
    enable: bool,
    dry_run: bool,
    preflight_confirmed: bool = False,
) -> Dict[str, object]:
    return {
        "schema_version": 1,
        "dry_run": dry_run,
        "enable_requested": enable,
        "preflight_confirmed": preflight_confirmed,
        "artifacts": [
            {
                "kind": artifact.kind,
                "destination": str(artifact.destination),
                "mode": "{:04o}".format(artifact.mode),
                "sha256": artifact.sha256,
                "bytes": len(artifact.content),
            }
            for artifact in artifacts
        ],
    }


def perform_install(
    artifacts: Sequence[Artifact],
    *,
    enable: bool,
    dry_run: bool,
    runner: Callable[[Sequence[str]], CommandResult],
    systemctl_path: Optional[str],
    systemd_analyze_path: Optional[str],
    owner_uid: int = 0,
    owner_gid: int = 0,
    preflight_confirmed: bool = False,
) -> Dict[str, object]:
    for artifact in artifacts:
        validate_destination(
            artifact.destination,
            dry_run=dry_run,
            owner_uid=owner_uid,
        )
    plan = public_plan(artifacts, enable, dry_run, preflight_confirmed)
    if dry_run:
        plan["outcome"] = "validated_no_changes"
        return plan
    if not systemctl_path:
        raise InstallError("systemctl is required")

    if enable and not preflight_confirmed:
        raise InstallError(
            "--enable requires --preflight-confirmed after manual status and dry-run checks"
        )

    # Reject a live installation before doing even the bounded staged unit
    # verification. The checks are repeated after verification to close that
    # preparation interval before any live file is replaced.
    require_unit_inactive(
        runner,
        systemctl_path,
        "mlp-network-recovery-watchdog.timer",
    )
    require_unit_inactive(
        runner,
        systemctl_path,
        "mlp-network-recovery-watchdog.service",
    )

    verification = "unavailable"
    with tempfile.TemporaryDirectory(prefix="mlp-network-watchdog-stage-") as stage_name:
        staged = stage_artifacts(artifacts, Path(stage_name))
        if systemd_analyze_path:
            result = runner(
                [
                    systemd_analyze_path,
                    "verify",
                    str(staged["service"]),
                    str(staged["timer"]),
                ]
            )
            command_succeeded(result, "systemd unit verification")
            verification = "passed"

    # Check as late as practical so an existing timer cannot execute across
    # the multi-file replacement. An active service or timer is an operator
    # action: the installer never stops one implicitly.
    require_unit_inactive(
        runner,
        systemctl_path,
        "mlp-network-recovery-watchdog.timer",
    )
    require_unit_inactive(
        runner,
        systemctl_path,
        "mlp-network-recovery-watchdog.service",
    )
    previously_enabled = timer_was_enabled(runner, systemctl_path)
    if previously_enabled and not enable:
        raise InstallError(
            "refusing live mutation while the timer is enabled; run systemctl "
            "disable --now mlp-network-recovery-watchdog.timer first"
        )

    snapshots = [snapshot_artifact(artifact, owner_uid) for artifact in artifacts]
    if enable:
        mismatched = [
            snapshot.artifact.kind
            for snapshot in snapshots
            if (
                not snapshot.existed
                or snapshot.content != snapshot.artifact.content
                or snapshot.mode != snapshot.artifact.mode
                or snapshot.uid != owner_uid
                or snapshot.gid != owner_gid
            )
        ]
        if mismatched:
            raise InstallError(
                "--enable requires an identical completed installation; rerun without "
                "--enable, perform manual status/dry-run checks, then enable (mismatch: {})".format(
                    ", ".join(mismatched)
                )
            )

    enable_attempted = False
    try:
        for artifact in artifacts:
            atomic_install_artifact(
                artifact,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )

        command_succeeded(
            runner([systemctl_path, "daemon-reload"]),
            "systemd daemon reload",
        )
        enabled = False
        if enable:
            enable_attempted = True
            command_succeeded(
                runner(
                    [
                        systemctl_path,
                        "enable",
                        "--now",
                        "mlp-network-recovery-watchdog.timer",
                    ]
                ),
                "watchdog timer enablement",
            )
            enabled = True
    except (InstallError, OSError) as exc:
        rollback_errors = attempt_rollback(
            snapshots,
            runner=runner,
            systemctl_path=systemctl_path,
            owner_uid=owner_uid,
            enable_attempted=enable_attempted,
            previously_enabled=previously_enabled,
        )
        suffix = (
            "; rollback incomplete: {}".format("; ".join(rollback_errors))
            if rollback_errors
            else "; prior artifacts restored"
        )
        raise InstallError("installation failed: {}{}".format(exc, suffix)) from exc

    plan.update(
        {
            "outcome": "installed",
            "systemd_verification": verification,
            "daemon_reloaded": True,
            "timer_enabled_by_installer": enabled,
        }
    )
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchdog-source", type=Path, required=True)
    parser.add_argument("--watchdog-sha256", required=True)
    parser.add_argument("--interface", required=True)
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--connection-uuid")
    selectors.add_argument("--connection-name")
    parser.add_argument("--command-timeout-seconds", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--enable",
        action="store_true",
        help="enable and start an identical prior install after reviewed preflight",
    )
    parser.add_argument(
        "--preflight-confirmed",
        action="store_true",
        help="attest that manual watchdog --status and --dry-run checks passed",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        if os.geteuid() != 0:
            raise InstallError("installer must run as root")
        args = build_parser().parse_args(argv)
        profile = validate_profile(
            args.interface,
            args.connection_uuid,
            args.connection_name,
            args.command_timeout_seconds,
        )
        source = read_hash_pinned_source(args.watchdog_source, args.watchdog_sha256)
        artifacts = build_artifacts(source, profile, InstallPaths())
        result = perform_install(
            artifacts,
            enable=args.enable,
            dry_run=args.dry_run,
            runner=BoundedRunner(),
            systemctl_path=find_system_command("systemctl"),
            systemd_analyze_path=find_system_command("systemd-analyze"),
            preflight_confirmed=args.preflight_confirmed,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except InstallError as exc:
        print("install failed closed: {}".format(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("install failed closed: operating-system error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
