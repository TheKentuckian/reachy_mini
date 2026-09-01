"""App-autostart unit control (fork, wireless only).

On our wireless units the boot app runs under its own systemd unit
(``reachy-app-autostart.service``, owned by the app repo), not the daemon's
startup-app mechanism. This router lets the dashboard / operators restart
that unit safely over HTTP without SSH.

Requires a polkit rule allowing user ``pollen`` to manage the unit — see
MAINTENANCE.md §4.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

from fastapi import APIRouter

AUTOSTART_UNIT = "reachy-app-autostart.service"

router = APIRouter(prefix="/api/autostart", tags=["autostart"])


def _systemctl(*args: str) -> tuple[int, str, str]:
    """Run systemctl with the given args; never raises."""
    if not shutil.which("systemctl"):
        return 1, "", "systemctl not found"
    try:
        proc = subprocess.run(
            ["systemctl", *args], capture_output=True, text=True, timeout=5
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "systemctl timed out"


def _systemctl_sudo(*args: str) -> tuple[int, str, str]:
    """Run systemctl with sudo; for privileged unit operations."""
    if not shutil.which("systemctl"):
        return 1, "", "systemctl not found"
    try:
        proc = subprocess.run(
            ["sudo", "systemctl", *args], capture_output=True, text=True, timeout=10
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "systemctl timed out"


@router.get("/service_status")
def service_status() -> dict[str, Any]:
    """Active/enabled state of reachy-app-autostart.service."""
    rc_active, active, _ = _systemctl("is-active", AUTOSTART_UNIT)
    rc_enabled, enabled, _ = _systemctl("is-enabled", AUTOSTART_UNIT)
    return {
        "active": active,
        "active_ok": rc_active == 0,
        "enabled": enabled,
        "enabled_ok": rc_enabled == 0,
    }


@router.post("/restart")
def restart_service() -> dict[str, Any]:
    """Safely restart reachy-app-autostart.

    Stops the unit, resets its failure state, then starts it. A bare
    ``systemctl restart`` races with systemd's own queued auto-restart after
    a crash and can leave the unit ``failed`` (TheKentuckian/reachy_mini#25).
    """
    stages: list[dict[str, Any]] = []

    rc, _, stderr = _systemctl_sudo("stop", AUTOSTART_UNIT)
    stages.append({"name": "stop", "rc": rc, "stderr": stderr})

    # Let systemd reap the stopped unit before reset-failed.
    time.sleep(1.0)

    rc, _, stderr = _systemctl_sudo("reset-failed", AUTOSTART_UNIT)
    stages.append({"name": "reset-failed", "rc": rc, "stderr": stderr})

    rc, _, stderr = _systemctl_sudo("start", AUTOSTART_UNIT)
    stages.append({"name": "start", "rc": rc, "stderr": stderr})

    ok = all(s["rc"] == 0 for s in stages)
    return {"ok": ok, "stages": stages}
