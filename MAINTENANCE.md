# Reachy Mini — Maintenance & Reflash Guide

This file documents the customisations on this robot that live **outside the Git
repo**.  Everything in the repo (service files, launcher, fork-specific patches)
is captured by `git log`; this file exists only to cover system-level state that
would be lost on a reflash.

---

## What's in the repo vs. what isn't

| Item | Lives in repo? | Notes |
|------|---------------|-------|
| `reachy-mini-daemon.service` | Yes — `src/.../services/wireless/` | Reinstall via `install_service.sh` |
| `launcher.sh` patches (instrumentation, rfkill, etc.) | Yes | Reinstalled with the package |
| `reachy-app-autostart.service` | **No** | See §2 below |
| `/usr/local/bin/reachy-app-autostart.py` | **No** | See §2 below |
| `/etc/reachy-mini/autostart.json` | **No** | See §3 below |
| `/etc/polkit-1/rules.d/50-reachy.rules` | **No** | See §4 below |
| Central relay disabled by default | Yes — commit `32dac0c0` | See `agents.local.md` to re-enable |

---

## Post-reflash checklist

1. Install the fork from source:
   ```bash
   cd ~/reachy_mini
   pip install -e .
   ```

2. Install the daemon service:
   ```bash
   src/reachy_mini/daemon/app/services/wireless/install_service.sh
   ```

3. Recreate the autostart service and launcher (§2).

4. Recreate the autostart config (§3).

5. Recreate the polkit rule (§4).

6. Reload systemd and restart:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now reachy-mini-daemon reachy-app-autostart
   ```

---

## §2 — Autostart service and launcher

Two files that are not tracked in the repo.

### `/etc/systemd/system/reachy-app-autostart.service`

```bash
sudo tee /etc/systemd/system/reachy-app-autostart.service << 'EOF'
[Unit]
Description=Reachy Mini app autostart (config-driven)
After=reachy-mini-daemon.service
Wants=reachy-mini-daemon.service
StartLimitIntervalSec=120
# Raised from 3 → 10: operator-initiated restarts shouldn't consume the crash-loop budget.
# The boot-loop guard in launcher.sh (commit e92a9c89) provides the real brake.
StartLimitBurst=10

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/reachy-app-autostart.py
Restart=on-failure
RestartSec=5

User=pollen
Group=pollen
SupplementaryGroups=audio gpio i2c spi

TimeoutStartSec=60

MemoryHigh=1500M
MemoryMax=1800M

KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=reachy-app-autostart

[Install]
WantedBy=multi-user.target
EOF
```

### `/usr/local/bin/reachy-app-autostart.py`

Reads `/etc/reachy-mini/autostart.json`, waits for the daemon, enables motors,
plays `wake_up`, then `exec()`s into the configured Python module from
`/venvs/apps_venv`.

```bash
sudo tee /usr/local/bin/reachy-app-autostart.py << 'PYEOF'
#!/usr/bin/env python3
"""Reachy Mini app autostart launcher.

Reads /etc/reachy-mini/autostart.json and, if app autostart is enabled,
waits for the Reachy Mini daemon to be ready, enables motors, plays the
wake_up animation, then exec()s into the configured Python module from
/venvs/apps_venv.

If autostart is disabled or no app is configured, exits 0 cleanly so
systemd records the service as having succeeded.

Config schema:
    {
      "app_autostart_enabled": bool,
      "app_module": "reachy_mini_conversation_app.main" | null,
      "app_args": ["--no-camera", ...]   # optional list of strings
    }
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path("/etc/reachy-mini/autostart.json")
DAEMON_BASE = "http://127.0.0.1:8000"
APPS_VENV_PYTHON = "/venvs/apps_venv/bin/python"

DAEMON_READY_TIMEOUT_S = 30.0
DAEMON_POLL_INTERVAL_S = 0.2

DEFAULT_CONFIG = {
    "app_autostart_enabled": False,
    "app_module": None,
    "app_args": [],
}


def log(msg: str) -> None:
    print(f"[reachy-app-autostart] {msg}", flush=True)


def read_config() -> dict:
    if not CONFIG_PATH.exists():
        log(f"No config at {CONFIG_PATH}; treating as disabled")
        return DEFAULT_CONFIG.copy()
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        log(f"Config malformed ({e}); treating as disabled")
        return DEFAULT_CONFIG.copy()


def http_post(path: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(f"{DAEMON_BASE}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def wait_for_daemon(timeout_s: float = DAEMON_READY_TIMEOUT_S) -> bool:
    """Poll the daemon until any HTTP response comes back (connection up)."""
    deadline = time.monotonic() + timeout_s
    last_err: str | None = None
    probe_url = f"{DAEMON_BASE}/openapi.json"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(probe_url, timeout=1.0).read(1)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            last_err = str(e)
            time.sleep(DAEMON_POLL_INTERVAL_S)
    log(f"Daemon not ready after {timeout_s}s; last error: {last_err}")
    return False


def main() -> int:
    cfg = read_config()

    if not cfg.get("app_autostart_enabled"):
        log("App autostart disabled in config; exiting 0")
        return 0

    module = cfg.get("app_module")
    if not module or not isinstance(module, str):
        log("app_module not configured; exiting 0")
        return 0

    if not Path(APPS_VENV_PYTHON).exists():
        log(f"Apps venv python not found at {APPS_VENV_PYTHON}; aborting")
        return 1

    log(f"Waiting for Reachy Mini daemon at {DAEMON_BASE}...")
    if not wait_for_daemon():
        return 1
    log("Daemon ready.")

    try:
        http_post("/api/motors/set_mode/enabled")
        log("Motors enabled.")
    except Exception as e:
        log(f"Failed to enable motors: {e}")
        return 1

    try:
        http_post("/api/move/play/wake_up")
        log("wake_up animation queued.")
    except Exception as e:
        log(f"wake_up failed (continuing): {e}")

    args = cfg.get("app_args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        log("app_args must be a list of strings; ignoring")
        args = []

    argv = [APPS_VENV_PYTHON, "-u", "-m", module, *args]
    log(f"Exec: {' '.join(shlex.quote(a) for a in argv)}")
    os.execv(APPS_VENV_PYTHON, argv)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
PYEOF
sudo chmod +x /usr/local/bin/reachy-app-autostart.py
```

---

## §3 — Autostart config

Tells the autostart service which app to launch on boot.  Currently set to
`robot_comic`.

```bash
sudo mkdir -p /etc/reachy-mini
sudo tee /etc/reachy-mini/autostart.json << 'EOF'
{
  "app_autostart_enabled": true,
  "app_module": "robot_comic.main",
  "app_args": []
}
EOF
```

To disable autostart without removing the config, set `"app_autostart_enabled": false`.

---

## §3 — Restarting the autostart app

**Prefer** the daemon endpoint — it performs `stop → reset-failed → start` atomically and avoids the auto-restart race described in [#25](https://github.com/TheKentuckian/reachy_mini/issues/25):

```bash
curl -X POST http://localhost:8000/api/autostart/restart
```

**Manual fallback (SSH):**

```bash
sudo systemctl stop reachy-app-autostart \
  && sleep 1 \
  && sudo systemctl reset-failed reachy-app-autostart \
  && sudo systemctl start reachy-app-autostart
```

> **Warning**: do not use bare `sudo systemctl restart reachy-app-autostart`.
> When the unit has just crashed, systemd already has a restart queued (`RestartSec=5`).
> The manual restart races with that queued restart, can be canceled, and burns
> `StartLimitBurst` entries — leaving the unit in `failed`.  See [#25](https://github.com/TheKentuckian/reachy_mini/issues/25).

---

## §4 — Polkit rule

Allows user `pollen` to `start`/`stop`/`reset-failed` `reachy-app-autostart.service`
without a password.  Required for the antenna wake gesture (issue #23) and the
safe-restart endpoint (issue #25).

`reset-failed` is covered by the same `org.freedesktop.systemd1.manage-units`
action as `start`/`stop`, so no rule change is needed.  The comment below has
been updated to reflect the full set of operations.

```bash
sudo tee /etc/polkit-1/rules.d/50-reachy.rules << 'EOF'
/* Allow user pollen to start/stop/reset-failed reachy-app-autostart.service without a
   password.  Required for the antenna wake gesture (#23) and the safe-restart
   endpoint (#25).  All three operations fall under manage-units. */
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "reachy-app-autostart.service" &&
        subject.user == "pollen") {
        return polkit.Result.YES;
    }
});
EOF
```

Takes effect immediately; no reload needed.

---

## Fork-specific behaviours (in the repo, not system files)

These are code changes — they survive reflash as long as you install from the
fork rather than upstream.  Listed here for awareness.

- **Central relay disabled** (`32dac0c0`) — the HuggingFace WebRTC relay is off
  by default.  See `agents.local.md` for how to re-enable it.
- **Daemon service `Type=notify`** with `TimeoutStartSec`/`TimeoutStopSec` and
  `Restart=on-failure` — the service template in `install_service.sh` is stock;
  the actual service installed on disk reflects fork patches.  Always run
  `install_service.sh` after reflash to pick up the current version.
