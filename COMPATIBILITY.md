# Fork Compatibility Notes

Changes in this fork (TheKentuckian/reachy_mini) relative to upstream (pollen-robotics/reachy_mini).

## Python / LAN apps — fully compatible

All changes are backward-compatible for Python apps and any client connecting over LAN via the REST or WebSocket API. The public API surface is unchanged.

## JS / HuggingFace Space apps — broken by default

The WebRTC central relay is **disabled by default** in this fork (commit `32dac0c0`). All JS apps — including Pollen's published Spaces (`webrtc_example`, `reachy_mini_radio`, `spaceship_game`, etc.) — require it to discover and reach the robot remotely.

**To re-enable**, add `--central-relay` to the daemon launch command in `src/reachy_mini/daemon/app/services/wireless/launcher.sh`:

```diff
-exec python -u -m reachy_mini.daemon.app.main --wireless-version --log-file /tmp/daemon.jsonl
+exec python -u -m reachy_mini.daemon.app.main --wireless-version --central-relay --log-file /tmp/daemon.jsonl
```

Then reinstall the service:

```bash
sudo cp src/reachy_mini/daemon/app/services/wireless/reachy-mini-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart reachy-mini-daemon
```

See `agents.local.md` for the reason this was disabled (unattended camera/mic exposure).

## Changes with no compatibility impact

| Change | Notes |
|---|---|
| Lazy heavy imports on startup | Pure perf improvement, transparent to callers |
| Daemon instrumentation / JSONL logging | Additive only |
| SIGUSR1 for power-button shutdown | Daemon-internal signal contract, no API change |
| Shutdown debounce extended to 1 s | Filters motor-depower transients, no app-visible effect |
| Antenna sleep/wake gesture on power button | Triggers on physical GPIO pin state; does not interfere with app-level antenna control |
| `Restart=on-failure` in systemd unit | Improves reliability; apps should already handle reconnection |
