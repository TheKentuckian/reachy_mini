#!/bin/bash
# Repoint systemd units at the editable git checkout. Run this ON THE ROBOT.
#
# Fixes issue #51: two units exec STALE COPIES of files that live in this
# repo, so `git pull` deploys silently don't take effect for them:
#
#   * reachy-mini-daemon     ExecStart -> site-packages copy of
#                            daemon/app/services/wireless/launcher.sh
#                            (pre-editable-install leftover)
#   * reachy-mini-bluetooth  ExecStart -> /bluetooth/bluetooth_service.py
#                            (hand-copied)
#   * gpio-shutdown-daemon   ExecStart -> site-packages copy of
#                            daemon/app/services/gpio_shutdown/launcher.sh
#                            (re-copied on every boot by startup_check.py)
#
# Instead of editing the unit files in place (lost on reinstall, invisible
# to `systemctl cat`'s provenance), this writes systemd drop-in overrides
# under /etc/systemd/system/<unit>.service.d/ that point ExecStart (and
# WorkingDirectory for the daemon) at this checkout. Idempotent: rerunning
# rewrites the same overrides.
#
# The script does NOT restart the units — restart at the next planned
# bring-up, or manually:
#     sudo systemctl restart reachy-mini-daemon reachy-mini-bluetooth gpio-shutdown-daemon
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIRELESS_DIR="$REPO_ROOT/src/reachy_mini/daemon/app/services/wireless"
LAUNCHER="$WIRELESS_DIR/launcher.sh"
GPIO_DIR="$REPO_ROOT/src/reachy_mini/daemon/app/services/gpio_shutdown"
GPIO_LAUNCHER="$GPIO_DIR/launcher.sh"
BLUETOOTH_SERVICE="$REPO_ROOT/src/reachy_mini/daemon/app/services/bluetooth/bluetooth_service.py"

if [ ! -f "$LAUNCHER" ]; then
    echo "ERROR: $LAUNCHER not found — is this script inside the checkout?" >&2
    exit 1
fi
chmod +x "$LAUNCHER" "$GPIO_LAUNCHER"

write_override() {
    local unit="$1" content="$2"
    local dir="/etc/systemd/system/${unit}.service.d"
    if ! systemctl list-unit-files "${unit}.service" --no-legend | grep -q .; then
        echo "SKIP: unit ${unit}.service not installed on this machine"
        return 0
    fi
    sudo mkdir -p "$dir"
    printf '%s' "$content" | sudo tee "$dir/checkout-paths.conf" > /dev/null
    echo "Wrote $dir/checkout-paths.conf"
}

# The empty ExecStart= line clears the packaged value before setting ours
# (systemd appends ExecStart otherwise).
write_override reachy-mini-daemon "[Service]
ExecStart=
ExecStart=$LAUNCHER
WorkingDirectory=$WIRELESS_DIR
"

write_override gpio-shutdown-daemon "[Service]
ExecStart=
ExecStart=$GPIO_LAUNCHER
WorkingDirectory=$GPIO_DIR
"

if [ -f "$BLUETOOTH_SERVICE" ]; then
    # Keep the packaged unit's rfkill-unblock + settle wrapper — only the
    # script path changes (caught on first live run 2026-06-12: a bare
    # python ExecStart dropped the unblock and would break BLE when
    # bluetooth comes up soft-blocked).
    write_override reachy-mini-bluetooth "[Service]
ExecStart=
ExecStart=/bin/bash -c 'sudo /usr/sbin/rfkill unblock bluetooth && sleep 2 && /usr/bin/python3 $BLUETOOTH_SERVICE'
"
else
    echo "SKIP: $BLUETOOTH_SERVICE not in checkout; bluetooth unit untouched"
fi

sudo systemctl daemon-reload
echo
echo "Units now exec the editable checkout. Verify with:"
echo "    systemctl cat reachy-mini-daemon gpio-shutdown-daemon reachy-mini-bluetooth | grep -A1 ExecStart"
echo "Restart at the next planned bring-up to pick the overrides up:"
echo "    sudo systemctl restart reachy-mini-daemon reachy-mini-bluetooth gpio-shutdown-daemon"
