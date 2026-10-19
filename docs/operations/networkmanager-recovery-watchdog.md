# NetworkManager recovery watchdog

This watchdog checks one wired interface and one NetworkManager connection every
two minutes. It waits for three consecutive failed checks before taking an
action, waits at least 15 minutes between actions, and permits at most three
actions per UTC day.

The only possible recovery actions are:

1. `nmcli networking on` when carrier is present and NetworkManager networking
   is disabled, after one exact profile is proven statically bound to the
   configured interface.
2. `nmcli connection up uuid <resolved-UUID> ifname <wired-interface>` when
   carrier is present, networking is enabled, and the configured profile is
   inactive. A configured name is resolved to one exact UUID first.

When physical carrier is absent, the watchdog records the state and leaves the
interface untouched. It never reloads a driver, resets a device or PCI function,
or reboots the host. Inspection errors fail closed without a recovery action.

## Installation outline

Review the watchdog, installer, unit, timer, and example config first. Run
`install_networkmanager_recovery_watchdog.py --dry-run` with the intended
watchdog source hash, `'<wired-interface>'`, and exactly one connection name or
UUID. Installer dry-run performs no writes and invokes no external command.

Run the installer again without `--enable` for the initial installation. It
stages and hash-checks every artifact, verifies the staged unit files with
bounded `systemd-analyze verify` when available, rejects an active watchdog
timer or service, then atomically installs:

- `/usr/local/libexec/mlp-network-recovery-watchdog`
- `/etc/mlp-network-recovery-watchdog.conf`
- the service and timer under `/etc/systemd/system`

The config is a regular root-owned file with mode `0600`. The watchdog parses
it with a strict key and value allowlist; systemd never imports it into the
Python environment. Destination directories must be root-owned and must not be
group- or world-writable. If a live write, daemon reload, or enablement fails,
the installer restores every prior artifact and reloads the restored units.

Before enabling, invoke the installed watchdog once with `--status` and once
with `--dry-run`. Both perform bounded observations without changing private
state or NetworkManager; status exits nonzero when the target is not healthy.
After reviewing both results, rerun the installer with the same inputs plus
`--enable --preflight-confirmed`. Enablement is rejected unless the existing
installation is byte-for-byte and mode-for-mode identical. For an update,
first run `systemctl disable --now mlp-network-recovery-watchdog.timer` and
`systemctl stop mlp-network-recovery-watchdog.service`, install without
`--enable`, repeat the manual checks, and then use the explicit enable step.
The installer rejects a live mutation while the timer is still enabled, even
when that timer is presently inactive.

State is stored atomically at
`/var/lib/mlp-network-recovery-watchdog/state.json` with private permissions.
The service logs only transitions and recovery outcomes to journald. A separate
lock prevents overlapping manual and timer invocations.

## Intentional network maintenance

Before planned network changes, inhibit all observations and recovery actions:

```console
install -o root -g root -m 0600 /dev/null /run/mlp-network-recovery-watchdog.inhibit
```

The next timer cycle records the maintenance state, clears the actionable
failure chain, exits successfully, and invokes no `nmcli` command. Remove the
file to resume. The inhibit is intentionally stored under `/run`, so a reboot
clears it. A symlink, non-regular file, wrong owner, or any group/world mode
bits cause a fail-closed error and no NetworkManager action. `--status` returns
exit code 3 while maintenance is inhibited; ordinary timer cycles return 0.
