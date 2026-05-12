#!/bin/bash

# Boot-loop guard: if the gpio-shutdown-daemon has been started N times
# within WINDOW seconds (whether from OS reboots or service crashes),
# hold off arming the shutdown trigger so a runaway loop can't cycle
# indefinitely without giving the user a chance to intervene.
_STAMP_FILE="/home/pollen/.gpio_shutdown_starts"
_GUARD_COUNT=3         # starts within the window that trigger the hold
_GUARD_WINDOW_SECS=300 # rolling window: 5 minutes
_GUARD_PAUSE_SECS=180  # how long to hold before arming: 3 minutes
_LOG_FILE="/home/pollen/gpio_shutdown.log"

_log_guard() {
    local ts="$(date -Iseconds)"
    echo "$ts [boot-loop-guard] $*" | tee -a "$_LOG_FILE" >&2
    logger -t gpio-shutdown-guard -p daemon.warning "$*"
    wall "Reachy Mini gpio-shutdown-guard: $*" 2>/dev/null || true
}

now=$(date +%s)
cutoff=$(( now - _GUARD_WINDOW_SECS ))

# Record this start, then prune entries outside the window.
echo "$now" >> "$_STAMP_FILE"
tmp=$(mktemp)
awk -v c="$cutoff" '$1 >= c' "$_STAMP_FILE" > "$tmp" && mv "$tmp" "$_STAMP_FILE"

recent=$(wc -l < "$_STAMP_FILE")

if [ "$recent" -ge "$_GUARD_COUNT" ]; then
    _log_guard "WARNING: started ${recent}x in last $(( _GUARD_WINDOW_SECS / 60 )) min — holding ${_GUARD_PAUSE_SECS}s before arming shutdown trigger"
    sleep "$_GUARD_PAUSE_SECS"
    _log_guard "Boot-loop guard pause complete — arming shutdown monitor"
fi

source /venvs/mini_daemon/bin/activate

# Run the monitor file directly instead of `python -m reachy_mini...`:
# module execution imports the whole reachy_mini SDK (several seconds of CPU
# and I/O during boot, tens of MB of RSS) while the script itself only needs
# gpiozero and the stdlib.
exec python "$(dirname "$(readlink -f "$0")")/shutdown_monitor.py"
