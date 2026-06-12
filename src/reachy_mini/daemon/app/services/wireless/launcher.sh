#!/bin/bash
# Journal entry timestamped by journald — compare with first Python log line
# to measure venv-activation cost without needing an RTC.
echo "launcher.sh started at $(date -Iseconds)"
source /venvs/mini_daemon/bin/activate
export GST_PLUGIN_PATH=$GST_PLUGIN_PATH:/opt/gst-plugins-rs/lib/aarch64-linux-gnu/:/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0/
export PATH=$PATH:/opt/uv
export REACHY_DAEMON_INSTRUMENT=trace
export LIBCAMERA_LOG_LEVELS=*:ERROR
export GST_DEBUG=1
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib/aarch64-linux-gnu/
export LIBCAMERA_IPA_MODULE_PATH=/usr/local/lib/aarch64-linux-gnu/libcamera/ipa
export LIBCAMERA_IPA_CONFIG_PATH=/usr/local/share/libcamera/ipa

# Cap glibc malloc arenas. On a quad core Pi 4 the default (8 x ncpu) lets the
# multithreaded daemon spread allocations across many 64MB arenas, which grows
# RSS over time on the 4GB Wireless unit. 2 keeps it bounded. See issue #1165.
export MALLOC_ARENA_MAX=2

# Ensure WiFi is not soft-blocked (can happen after a crash or kernel module reload)
sudo rfkill unblock wifi

# Central signaling relay toggle (default OFF — see COMPATIBILITY.md).
# Flip with scripts/relay_on.sh / scripts/relay_off.sh ON THE ROBOT instead of
# editing this file: the robot runs an editable install from a git checkout,
# so in-place edits here make subsequent `git pull` deploys fail.
# The switch is read from /etc/reachy-mini/relay.env (or a pre-set
# REACHY_CENTRAL_RELAY environment variable, e.g. via systemd Environment=).
RELAY_ENV_FILE="/etc/reachy-mini/relay.env"
if [ -f "$RELAY_ENV_FILE" ]; then
    # shellcheck source=/dev/null
    source "$RELAY_ENV_FILE"
fi
RELAY_ARGS=()
if [ "${REACHY_CENTRAL_RELAY:-0}" = "1" ]; then
    echo "Central signaling relay ENABLED (REACHY_CENTRAL_RELAY=1)"
    RELAY_ARGS+=(--central-relay)
fi

# Run Python in unbuffered mode (-u) to ensure logs are immediately forwarded to systemd.
# exec makes Python the systemd main process for Type=notify and watchdog heartbeats.
exec python -u -m reachy_mini.daemon.app.main --wireless-version "${RELAY_ARGS[@]}" --log-file /tmp/daemon.jsonl
