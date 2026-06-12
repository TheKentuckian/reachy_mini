# Fork Compatibility Notes

Changes in this fork (TheKentuckian/reachy_mini) relative to upstream (pollen-robotics/reachy_mini).

## Python / LAN apps — fully compatible

All changes are backward-compatible for Python apps and any client connecting over LAN via the REST or WebSocket API. The public API surface is unchanged.

## JS / HuggingFace Space apps — broken by default

The WebRTC central relay is **disabled by default** in this fork (commit `32dac0c0`). All JS apps — including Pollen's published Spaces (`webrtc_example`, `reachy_mini_radio`, `spaceship_game`, etc.) — require it to discover and reach the robot remotely.

**To toggle it on the robot** (without dirtying the git checkout — the robot
runs an editable install, so in-place `launcher.sh` edits break `git pull`
deploys), use the toggle scripts:

```bash
# ON THE ROBOT
./scripts/relay_on.sh    # enable relay + restart daemon
./scripts/relay_off.sh   # disable relay (fork default) + restart daemon
```

These write `REACHY_CENTRAL_RELAY=1` (or `0`) to `/etc/reachy-mini/relay.env`,
which `launcher.sh` sources at daemon start and translates into the
`--central-relay` CLI flag. The variable can also be set via systemd
`Environment=` if you prefer. If the file is absent and the variable unset,
the relay stays **OFF**.

Two things to remember when toggling:

1. A daemon restart does **not** cascade-restart the app — restart any running
   app (e.g. `reachy-app-autostart`) too, or its SDK websocket stays dead.
2. Relay ON exposes the camera and bidirectional audio to the internet via the
   HuggingFace signaling relay whenever an HF token is cached. Turn it back
   off after testing.

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
