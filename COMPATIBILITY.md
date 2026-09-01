# Fork patch stack (TheKentuckian/reachy_mini)

`main` in this fork is **upstream `v1.10.0` + the patches below**, one commit
each, re-based on 2026-09-01 (`rebase/stock-1.10`). The previous divergent
history is preserved as the `legacy/fork-1.8.4` tag. To move to a newer
upstream release, start a branch from the new tag and re-apply this list;
`git log v1.10.0..main --no-merges` is the authoritative version.

## Behaviour changes vs stock

| Patch | What | Why / consumer |
|---|---|---|
| SLEEP antennas ±2.6 rad | `Backend.SLEEP_ANTENNAS_JOINT_POSITIONS` (stock ±3.05) | ±3.05 is past the reachable range and overloads the antenna servo (#62) |
| Antenna PID P=50 | `hardware_config` P 200→50 | kills the idle limit-cycle hunt (#43, #66) |
| `GET /api/motors/voltage` | bus voltage + LiFePO4 % estimate | read by robot_comic's console battery readout |
| `MediaBackend.AUDIO_ONLY` | GStreamer audio without the camera pipeline | robot_comic / Maestro run their own camera service |
| `REACHY_MINI_CAMERA_ENABLED=0` | daemon skips the camera/video branch entirely | voice-only apps on the CM4 |
| Power button → safe shutdown | GPIO monitor: active-LOW polling, 1 s debounce, 5 s boot ignore, SIGUSR1 to the daemon, wait ≤25 s, `shutdown --poweroff`; boot-loop guard in the launcher; daemon `TimeoutStopSec=20s` | stock monitor halts 200 ms after release without parking the head |
| Exit non-zero on failed startup | daemon shuts down (parking the robot) and exits 1 if `--autostart` ends non-RUNNING | so `Restart=on-failure` actually recovers the robot |
| Slower wake-up | `REACHY_WAKE_UP_MIN_DURATION_S` (3.0), `REACHY_WAKE_UP_DURATION_SCALE` (1.5), `REACHY_WAKE_UP_FLOURISH_DURATION_S` (0.4) | stock ~2 s snap (robot_comic#310) |
| Skip redundant motor writes | position packets only sent when the target changed; cache invalidated on mode switches / re-enable | idle CPU on the CM4 (#63) |
| Central relay opt-in | `--central-relay` / `REACHY_CENTRAL_RELAY=1` in `/etc/reachy-mini/relay.env`; **off by default** | stock auto-starts it whenever an HF token is cached, exposing camera + mic remotely. JS apps and Pollen's remote Spaces need it — `scripts/relay_on.sh` / `relay_off.sh` |
| WiFi init | skip the ~4 s rescan when already connected; init thread started from the lifespan instead of at router import | boot time |
| `/api/autostart/restart`, `/service_status` | stop → reset-failed → start of `reachy-app-autostart.service` | avoids the auto-restart race (#25); wireless only |
| Tests | `wireless`-marked tests auto-skip unless `-m wireless` | a plain `pytest` on a LAN with the robot otherwise plays sounds on it |

Python / LAN apps are fully compatible with stock: the public SDK and REST
surface is a superset of upstream's.

## Deliberately dropped at the re-base

Present in the 1.8.4-era fork, not carried (upstream has an equivalent, or no
consumer remained):

- antenna sleep/wake **gesture** state machine — ran disabled on the robot
  (`REACHY_ANTENNA_SLEEP=0`); upstream's antenna-touch → startup-app watcher is
  inert unless a `startup_app` is configured
- `SystemdNotifier` / `Type=notify` + watchdog — daemon unit is stock `Type=simple`
- daemon instrumentation (`REACHY_DAEMON_INSTRUMENT`, JSONL logs, startup
  parser), `/api/media/ipc-stats` — no consumer
- WebRTC consumer gate (`REACHY_WEBRTC_CONSUMER_GATE`) — upstream now stops
  broadcasting when there are no clients; re-measure idle CPU on the CM4
  before considering it again
- FK-skip / control-loop-Hz knobs — opt-in experiments, never enabled long-term
- loopback no-compression, ReSpeaker Pollen VID/PID `find()` — identical upstream
- fork autostart dashboard/router config half — the app repo's launcher owns
  `/etc/reachy-mini/autostart.json`
- lazy heavy imports (−51% import time on 1.8.4) — re-measure on 1.10 on the
  robot (`python -X importtime -m reachy_mini.daemon.app.main --help`) before
  re-porting; 0.7 s total on an M-series Mac

## Out-of-repo robot state

See `MAINTENANCE.md` for the systemd drop-ins, `/etc/reachy-mini/*`, and the
post-reflash checklist.
