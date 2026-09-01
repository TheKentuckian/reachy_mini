#!/bin/bash
source /venvs/mini_daemon/bin/activate
export GST_PLUGIN_PATH=$GST_PLUGIN_PATH:/opt/gst-plugins-rs/lib/aarch64-linux-gnu/:/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0/
export PATH=$PATH:/opt/uv
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib/aarch64-linux-gnu/
export LIBCAMERA_IPA_MODULE_PATH=/usr/local/lib/aarch64-linux-gnu/libcamera/ipa
export LIBCAMERA_IPA_CONFIG_PATH=/usr/local/share/libcamera/ipa

# Cap glibc malloc arenas. On a quad core Pi 4 the default (8 x ncpu) lets the
# multithreaded daemon spread allocations across many 64MB arenas, which grows
# RSS over time on the 4GB Wireless unit. 2 keeps it bounded. See issue #1165.
export MALLOC_ARENA_MAX=2

# Ensure WiFi is not soft-blocked (can happen after a crash or kernel module reload)
sudo rfkill unblock wifi

# Fork: daemon env file + central signaling relay toggle (default OFF).
# Flip the relay with scripts/relay_on.sh / scripts/relay_off.sh ON THE ROBOT
# instead of editing this file: the robot runs an editable install from a git
# checkout, so in-place edits here make subsequent `git pull` deploys fail.
# `set -a` exports everything the file defines, so it doubles as a general
# daemon env file: any REACHY_* knob placed there (e.g.
# REACHY_WAKE_UP_MIN_DURATION_S=2) reaches the Python process.
RELAY_ENV_FILE="/etc/reachy-mini/relay.env"
if [ -f "$RELAY_ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$RELAY_ENV_FILE"
    set +a
fi
RELAY_ARGS=()
if [ "${REACHY_CENTRAL_RELAY:-0}" = "1" ]; then
    echo "Central signaling relay ENABLED (REACHY_CENTRAL_RELAY=1)"
    RELAY_ARGS+=(--central-relay)
fi

# Run Python in unbuffered mode (-u) to ensure logs are immediately forwarded to systemd.
# exec makes Python the systemd main process (signals reach it directly).
exec python -u -m reachy_mini.daemon.app.main --wireless-version --no-wake-up-on-start "${RELAY_ARGS[@]}"
