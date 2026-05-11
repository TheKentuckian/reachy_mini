"""Monitor GPIO23 for shutdown signal.

On release, stop reachy-mini-daemon first so it can move the head to a
safe sleep pose before motor power is cut, then halt the system.
"""

import os
import signal
import subprocess
import sys
import time
from subprocess import call

from gpiozero import Button

DAEMON_UNIT = "reachy-mini-daemon.service"
_LOG_FILE = "/home/pollen/gpio_shutdown.log"


_LOG_MAX_BYTES = 10_240  # 10 KB — truncate rather than grow unboundedly


def _log(msg: str) -> None:
    """Write a timestamped line to both stdout (→ journald) and a file on disk."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        mode = "a"
        try:
            if os.path.getsize(_LOG_FILE) > _LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        with open(_LOG_FILE, mode) as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
# Upper bound for graceful daemon shutdown. Keep this strictly greater than
# TimeoutStopSec in reachy-mini-daemon.service (currently 20s): if the daemon
# hangs mid-goto_sleep, systemd will SIGKILL it at TimeoutStopSec and we want
# to see the PID disappear before halting, not race past it. If you raise the
# unit's TimeoutStopSec, raise this too.
DAEMON_STOP_TIMEOUT_S = 25.0

shutdown_button = Button(23, pull_up=True)


def _daemon_main_pid() -> int:
    """Return the MainPID of the daemon service, or 0 if not running."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", DAEMON_UNIT],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        _log(f"Could not query {DAEMON_UNIT} MainPID: {exc}")
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def _stop_daemon_and_wait() -> None:
    """Force a safe shutdown of reachy-mini-daemon and block until it exits.

    SIGUSR1 (not SIGTERM) is the contract for "power-button shutdown": the
    daemon overrides --no-goto-sleep-on-stop and always moves the head to
    SLEEP_HEAD_POSE before exiting. SIGTERM stays available for dev-style
    stops that should respect the CLI flag.

    Runs as the same user as the daemon (pollen), so we can signal it
    directly without sudo.
    """
    pid = _daemon_main_pid()
    if pid <= 0:
        _log(f"{DAEMON_UNIT} is not running; skipping graceful stop")
        return

    _log(f"Sending SIGUSR1 (force safe shutdown) to {DAEMON_UNIT} (PID {pid})")
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        _log(f"Could not signal daemon: {exc}")
        return

    deadline = time.monotonic() + DAEMON_STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _log(f"{DAEMON_UNIT} exited cleanly")
            return
        time.sleep(0.2)

    _log(f"{DAEMON_UNIT} did not exit within {DAEMON_STOP_TIMEOUT_S}s; halting anyway")


def pressed() -> None:
    """Handle shutdown button pressed.

    GPIO 23 is active-LOW: the hardware holds the pin HIGH at rest and drives it
    LOW when the button is pressed. With pull_up=True, gpiozero treats LOW as
    pressed (is_pressed=True). On Wireless Reachy Mini the hardware cuts power
    while the button is still held, so the release edge never arrives. We poll
    is_pressed rather than using when_pressed callbacks because the lgpio backend
    does not reliably fire edge callbacks on this hardware.

    The debounce window must be long enough to outlast power-rail transients
    caused by servo motors being disabled (inductive kick). 50 ms is too short;
    empirically 1 s filters motor-induced glitches while still feeling
    responsive to a deliberate button hold.
    """
    _DEBOUNCE_S = 1.0
    deadline = time.monotonic() + _DEBOUNCE_S
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if not shutdown_button.is_pressed:
            return  # transient / noise — pin went high again before deadline

    _log("Shutdown button pressed, stopping daemon...")
    _stop_daemon_and_wait()
    _log("Shutting down...")
    call(["sudo", "shutdown", "-h", "now"])


# Ignore button state for the first few seconds after boot. On Wireless
# Reachy Mini the user holds the power button to wake the device; if we
# start polling before they release it we immediately trigger a shutdown.
_STARTUP_IGNORE_S = 5.0
_log("Monitoring GPIO23 for shutdown signal...")
time.sleep(_STARTUP_IGNORE_S)

while True:
    time.sleep(0.1)
    if shutdown_button.is_pressed:
        pressed()
