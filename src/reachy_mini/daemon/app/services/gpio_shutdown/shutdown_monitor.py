"""Monitor GPIO23 for shutdown signal.

On release, stop reachy-mini-daemon first so it can move the head to a
safe sleep pose before motor power is cut, then halt the system.
"""

import os
import signal
import subprocess
import time
from signal import pause
from subprocess import call

from gpiozero import Button

DAEMON_UNIT = "reachy-mini-daemon.service"
# Upper bound for graceful daemon shutdown. Must be >= TimeoutStopSec in the
# daemon unit file (currently 20s) so we don't halt while the daemon is still
# moving the head to the sleep pose.
DAEMON_STOP_TIMEOUT_S = 25.0

shutdown_button = Button(23, pull_up=False)


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
        print(f"Could not query {DAEMON_UNIT} MainPID: {exc}")
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def _stop_daemon_and_wait() -> None:
    """SIGTERM reachy-mini-daemon and block until it exits or times out.

    Runs as the same user as the daemon (pollen), so we can signal it directly
    without sudo. uvicorn's signal handlers translate SIGTERM into a clean
    shutdown that runs the FastAPI lifespan finally block, which calls
    daemon.stop(goto_sleep_on_stop=True) and moves the head to SLEEP_HEAD_POSE.
    """
    pid = _daemon_main_pid()
    if pid <= 0:
        print(f"{DAEMON_UNIT} is not running; skipping graceful stop")
        return

    print(f"Sending SIGTERM to {DAEMON_UNIT} (PID {pid})")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        print(f"Could not signal daemon: {exc}")
        return

    deadline = time.monotonic() + DAEMON_STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"{DAEMON_UNIT} exited cleanly")
            return
        time.sleep(0.2)

    print(f"{DAEMON_UNIT} did not exit within {DAEMON_STOP_TIMEOUT_S}s; halting anyway")


def released() -> None:
    """Handle shutdown button released."""
    for _ in range(200):
        time.sleep(0.001)

        if shutdown_button.is_pressed:
            # probably just a bounce, ignore
            return

    print("Shutdown button released, stopping daemon...")
    _stop_daemon_and_wait()
    print("Shutting down...")
    call(["sudo", "shutdown", "-h", "now"])


shutdown_button.when_released = released

print("Monitoring GPIO23 for shutdown signal...")
pause()
