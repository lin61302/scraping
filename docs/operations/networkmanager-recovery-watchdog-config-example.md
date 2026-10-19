# Install as /etc/mlp-network-recovery-watchdog.conf, owned by root with mode 0600.
# The interface must be a NetworkManager-managed wired interface.
NM_WATCHDOG_INTERFACE='<wired-interface>'

# Set exactly one connection selector. UUID is preferred because names can change.
NM_WATCHDOG_CONNECTION_UUID='00000000-0000-0000-0000-000000000000'
# NM_WATCHDOG_CONNECTION_NAME='Example wired profile'

# Every nmcli invocation is killed after this many seconds. Allowed range: 1..60.
NM_WATCHDOG_COMMAND_TIMEOUT_SECONDS='15'
