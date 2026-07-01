"""Daemon entry point for the Reachy Mini robot.

This script serves as the command-line interface (CLI) entry point for the Reachy Mini daemon.
It initializes the daemon with specified parameters such as simulation mode, serial port,
scene to load, and logging level. The daemon runs indefinitely, handling requests and
managing the robot's state.

"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import types
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from reachy_mini.apps.manager import AppManager
from reachy_mini.daemon.app.middleware import MaxBodySizeMiddleware
from reachy_mini.daemon.app.routers import (
    apps,
    audio_config,
    camera,
    daemon,
    hf_auth,
    kinematics,
    logs,
    media,
    motors,
    move,
    sdk_ws,
    state,
    volume,
)
from reachy_mini.daemon.daemon import Daemon
from reachy_mini.daemon.instrumentation import (
    configure_daemon_logging,
    log_event,
    timing_event,
)
from reachy_mini.daemon.systemd import SystemdNotifier
from reachy_mini.daemon.utils import SimulationMode
from reachy_mini.io.protocol import DaemonState, MotorControlMode
from reachy_mini.media.audio_utils import (
    check_reachymini_asoundrc,
    write_asoundrc_to_home,
)
from reachy_mini.motion.recorded_move import preload_default_datasets
from reachy_mini.utils.discovery import MdnsServiceRegistration
from reachy_mini.utils.wireless_version.startup_check import (
    check_and_fix_restore_venv,
    check_and_fix_venvs_ownership,
    check_and_sync_apps_venv_sdk,
    check_and_update_bluetooth_service,
    check_and_update_wireless_launcher,
)

logger = logging.getLogger(__name__)

# Origins allowed to call the unauthenticated API cross-origin: localhost tooling
# plus the native app webview schemes (Tauri/Capacitor), which a browser cannot
# forge, so the drive-by protection of GHSA-p4cp-8gwf-3fgv holds.
CORS_ORIGIN_REGEX = r"(https?://(localhost|127\.0\.0\.1)(:\d+)?|tauri://localhost|https?://tauri\.localhost|capacitor://localhost)"


@dataclass
class Args:
    """Arguments for configuring the Reachy Mini daemon."""

    log_level: str = "INFO"
    log_file: str | None = None

    wireless_version: bool = False
    desktop_app_daemon: bool = False

    serialport: str = "auto"
    hardware_config_filepath: str | None = None

    sim: bool = False
    mockup_sim: bool = False
    scene: str = "empty"
    headless: bool = False
    no_media: bool = False

    kinematics_engine: str = "AnalyticalKinematics"
    check_collision: bool = False

    autostart: bool = True
    timeout_health_check: float | None = None

    wake_up_on_start: bool = True
    goto_sleep_on_stop: bool = True
    startup_app: str | None = None  # app name to auto-start after wake-up
    preload_datasets: bool = False
    dataset_update_interval_hours: float = 24.0  # 0 to disable periodic updates

    robot_name: str = "reachy_mini"

    # None means "auto": bind 0.0.0.0 on the wireless version (must be reachable
    # over Wi-Fi) and 127.0.0.1 everywhere else. See _resolve_bind_host().
    fastapi_host: str | None = None
    fastapi_port: int = 8000

    central_relay: bool = False


def _resolve_bind_host(args: Args) -> str:
    """Resolve the address the HTTP API binds to.

    An explicit ``--fastapi-host`` always wins. Otherwise the daemon binds all
    interfaces only on the wireless version (the robot has to be reachable on
    the LAN); every other configuration (Lite, desktop, simulation) stays on
    loopback so the unauthenticated API is not exposed to the network.
    """
    if args.fastapi_host:
        return args.fastapi_host
    return "0.0.0.0" if args.wireless_version else "127.0.0.1"


# Self-motion cooldown for the antenna gesture detector (issue #33).
# After the daemon itself drives the antennas (goto_sleep / wake_up) or kicks
# the autostart unit, gesture detection is suspended for this many seconds so
# the daemon's own motion (or post-SIGKILL head slam) cannot satisfy the
# wake-gesture threshold and re-fire _start_autostart_app() in a loop.
# Module-level so tests can monkeypatch it to a short value.
_SELF_MOTION_COOLDOWN_S = 10.0


def create_app(
    args: Args,
    health_check_event: asyncio.Event | None = None,
    systemd_notifier: SystemdNotifier | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    _ANTENNA_SLEEP_THRESHOLD = 0.4  # radians — inward from neutral / outward from sleep
    _ANTENNA_SLEEP_HOLD_S = 0.5   # s both antennas must stay displaced
    # Sleep pose constants (must match AbstractBackend.SLEEP_ANTENNAS_JOINT_POSITIONS)
    _ANTENNA_LEFT_SLEEP = -3.05   # radians
    _ANTENNA_RIGHT_SLEEP = 3.05   # radians

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Lifespan context manager for the FastAPI application."""
        args = app.state.args  # type: Args
        dataset_updater_task: asyncio.Task[None] | None = None
        antenna_sleep_task: asyncio.Task[None] | None = None

        mdns = MdnsServiceRegistration(
            args.robot_name,
            args.fastapi_port,
            wireless_version=args.wireless_version,
        )

        def preload_with_logging() -> None:
            """Download datasets with logging."""
            try:
                preload_default_datasets()
                logger.info("Recorded move datasets pre-loaded successfully")
            except Exception as e:
                logger.warning(f"Failed to pre-load some datasets: {e}")

        async def dataset_updater(interval_hours: float) -> None:
            """Background task that periodically checks for dataset updates."""
            interval_seconds = interval_hours * 3600
            while True:
                try:
                    await asyncio.sleep(interval_seconds)
                    logger.info("Checking for dataset updates...")
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, preload_with_logging)
                except asyncio.CancelledError:
                    logger.info("Dataset updater task cancelled")
                    break
                except Exception as e:
                    logger.warning(f"Error in dataset updater: {e}")

        async def antenna_sleep_monitor() -> None:
            """Two-gesture antenna state machine: sleep and wake.

            Sleep gesture (awake state): hold left < -threshold AND
            right > threshold for _ANTENNA_SLEEP_HOLD_S seconds. Stops any
            running app, then calls goto_sleep(). Daemon keeps running.

            Wake gesture (sleeping state): hold both antennas pushed outward
            from their sleep positions by at least _ANTENNA_SLEEP_THRESHOLD
            (left > _ANTENNA_LEFT_SLEEP + threshold, right < _ANTENNA_RIGHT_SLEEP
            - threshold). Calls wake_up(), then launches the autostart app if one
            is configured in /etc/reachy-mini/autostart.json.

            Disable entirely via REACHY_ANTENNA_SLEEP=0 in the environment.
            """
            if os.environ.get("REACHY_ANTENNA_SLEEP", "1") == "0":
                logger.info("Antenna sleep gesture disabled via REACHY_ANTENNA_SLEEP=0")
                return

            _AUTOSTART_UNIT = "reachy-app-autostart.service"

            async def _autostart_service_active() -> bool:
                """Return True if the autostart systemd service is currently running."""
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "systemctl", "is-active", "--quiet", _AUTOSTART_UNIT,
                    )
                    await proc.wait()
                    return proc.returncode == 0
                except Exception:
                    return False

            async def _start_autostart_app() -> None:
                """Start the autostart app via systemctl so only one managed instance runs."""
                logger.info(f"Antenna wake gesture: starting {_AUTOSTART_UNIT}")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", "systemctl", "start", _AUTOSTART_UNIT,
                    )
                    await proc.wait()
                    if proc.returncode != 0:
                        logger.warning(f"systemctl start {_AUTOSTART_UNIT} returned {proc.returncode}")
                except Exception:
                    logger.warning("Failed to start autostart service after wake gesture", exc_info=True)

            async def _stop_autostart_app() -> None:
                """Stop the autostart service, covering apps launched outside the app_manager."""
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", "systemctl", "stop", _AUTOSTART_UNIT,
                    )
                    await proc.wait()
                except Exception:
                    logger.warning("Failed to stop autostart service", exc_info=True)

            awake = True
            held_since: float | None = None
            _service_active_cache: bool = False
            _service_active_last_checked: float = -999.0
            _motor_error_count: int = 0
            _MOTOR_ERROR_BACKOFF_AFTER = 5
            _MOTOR_ERROR_BACKOFF_S = 3.0
            # Consecutive good reads required after an error burst before gesture
            # detection re-arms.  Prevents a bus recovering mid-gesture from
            # immediately firing a spurious wake/sleep transition.
            _MOTOR_RECOVERY_READS = 3
            _good_reads_since_error: int = 0
            # Minimum seconds between successive state transitions.  A genuine
            # human antenna gesture cannot repeat faster than this.
            _MIN_TRANSITION_INTERVAL_S = 2.0
            _last_transition_t: float = -999.0
            # Self-motion cooldown: see module-level _SELF_MOTION_COOLDOWN_S
            # for the rationale.  We read the constant via the module each
            # arm call so tests can monkeypatch it to a short value.
            _self_motion_until: float = 0.0

            def _arm_self_motion_cooldown(reason: str) -> None:
                """Arm the self-motion cooldown after a daemon-initiated motion.

                Logs INFO once per *new* cooldown window — if the cooldown is
                already active (e.g. a gesture branch makes two motion calls
                back-to-back), bump the window silently to avoid log spam.
                The per-iteration suppression inside the monitor loop is also
                silent.
                """
                nonlocal _self_motion_until
                t = asyncio.get_event_loop().time()
                already_active = t < _self_motion_until
                cooldown_s = _SELF_MOTION_COOLDOWN_S
                _self_motion_until = t + cooldown_s
                if not already_active:
                    logger.info(
                        "Antenna gesture detection suspended for %.1fs after self-motion (%s)",
                        cooldown_s,
                        reason,
                    )

            logger.info("Antenna sleep monitor started")

            while True:
                await asyncio.sleep(0.1)
                try:
                    backend = app.state.daemon.backend
                    if backend is None:
                        held_since = None
                        continue
                    positions = backend.get_present_antenna_joint_positions()
                    if positions is None:
                        held_since = None
                        continue
                    left, right = float(positions[0]), float(positions[1])
                    now = asyncio.get_event_loop().time()

                    # Refresh service-active cache at most once per second.
                    if now - _service_active_last_checked >= 1.0:
                        _service_active_cache = await _autostart_service_active()
                        _service_active_last_checked = now

                    # Require N consecutive clean reads after an error burst before
                    # re-arming the gesture detector.  Stale or glitched positions
                    # returned on bus recovery cannot satisfy the hold timer while
                    # this gate is open.
                    if _good_reads_since_error < _MOTOR_RECOVERY_READS:
                        _good_reads_since_error += 1
                        held_since = None
                        continue

                    # Self-motion cooldown: suppress gesture detection while
                    # the daemon's own recent motion (or post-kill head slam)
                    # could still be settling.  Resetting held_since prevents a
                    # partial hold accumulated before the cooldown started from
                    # firing the instant the cooldown expires.  We reset
                    # _motor_error_count inline because `continue` inside the
                    # try block bypasses the trailing `else` clause; the read
                    # itself succeeded so the error counter should still clear.
                    if now < _self_motion_until:
                        held_since = None
                        _motor_error_count = 0
                        continue

                    if awake:
                        # Sync: if motors were disabled externally (e.g. app called goto_sleep),
                        # immediately enter sleeping state rather than waiting for antenna droop.
                        if backend.get_motor_control_mode() == MotorControlMode.Disabled:
                            logger.info("Motors disabled externally — entering sleeping state")
                            awake = False
                            held_since = None
                        elif (
                            app.state.app_manager.current_app is not None
                            or _service_active_cache
                        ):
                            # App is running and owns the antennas; don't interfere.
                            held_since = None
                        else:
                            # Sleep gesture: both antennas squeezed inward from neutral
                            if left < -_ANTENNA_SLEEP_THRESHOLD and right > _ANTENNA_SLEEP_THRESHOLD:
                                if held_since is None:
                                    held_since = now
                                elif now - held_since >= _ANTENNA_SLEEP_HOLD_S:
                                    held_since = None
                                    if now - _last_transition_t < _MIN_TRANSITION_INTERVAL_S:
                                        logger.warning(
                                            "Antenna sleep gesture suppressed: repeated transition "
                                            "within %.2fs (last was %.2fs ago)",
                                            _MIN_TRANSITION_INTERVAL_S,
                                            now - _last_transition_t,
                                        )
                                    else:
                                        _last_transition_t = now
                                        logger.info("Antenna sleep gesture — stopping app and going to sleep")
                                        try:
                                            await app.state.app_manager.stop_current_app()
                                        except Exception:
                                            logger.warning("Error stopping app during antenna sleep gesture", exc_info=True)
                                        await _stop_autostart_app()
                                        _arm_self_motion_cooldown("stop autostart")
                                        try:
                                            await backend.goto_sleep()
                                            backend.set_motor_control_mode(MotorControlMode.Disabled)
                                        except Exception:
                                            logger.warning("Error in goto_sleep during antenna gesture", exc_info=True)
                                        _arm_self_motion_cooldown("goto_sleep")
                                        awake = False
                            else:
                                held_since = None
                    else:
                        # Symmetric to the awake-branch gate (lines above):
                        # the wake gesture exists for "robot is asleep, no app
                        # running, human lifts antennas to wake it".  If an
                        # app is already up — or the autostart unit is active —
                        # the wake intent is moot and firing wake_up() +
                        # _start_autostart_app() can self-trigger from a
                        # daemon-driven motion or post-SIGKILL head slam,
                        # producing the crash loop in issue #33.  This is the
                        # common-case path so we log at DEBUG, not WARNING.
                        if (
                            _service_active_cache
                            or app.state.app_manager.current_app is not None
                        ):
                            held_since = None
                            logger.debug(
                                "Antenna wake gesture suppressed: app already active "
                                "(service_active=%s, current_app=%s)",
                                _service_active_cache,
                                app.state.app_manager.current_app,
                            )
                        else:
                            # Wake gesture: both antennas pushed outward from sleep positions
                            wake_l = left > _ANTENNA_LEFT_SLEEP + _ANTENNA_SLEEP_THRESHOLD
                            wake_r = right < _ANTENNA_RIGHT_SLEEP - _ANTENNA_SLEEP_THRESHOLD
                            if wake_l and wake_r:
                                if held_since is None:
                                    held_since = now
                                elif now - held_since >= _ANTENNA_SLEEP_HOLD_S:
                                    held_since = None
                                    if now - _last_transition_t < _MIN_TRANSITION_INTERVAL_S:
                                        logger.warning(
                                            "Antenna wake gesture suppressed: repeated transition "
                                            "within %.2fs (last was %.2fs ago)",
                                            _MIN_TRANSITION_INTERVAL_S,
                                            now - _last_transition_t,
                                        )
                                    else:
                                        _last_transition_t = now
                                        logger.info("Antenna wake gesture — waking up")
                                        try:
                                            backend.set_motor_control_mode(MotorControlMode.Enabled)
                                            await backend.wake_up()
                                        except Exception:
                                            logger.warning("Error in wake_up during antenna gesture", exc_info=True)
                                        _arm_self_motion_cooldown("wake_up")
                                        await _start_autostart_app()
                                        _arm_self_motion_cooldown("start autostart")
                                        awake = True
                            else:
                                held_since = None
                except Exception:
                    _motor_error_count += 1
                    if _motor_error_count == 1:
                        logger.warning("Antenna sleep monitor error", exc_info=True)
                    else:
                        logger.debug(
                            "Antenna sleep monitor error (burst count=%d)", _motor_error_count,
                            exc_info=True,
                        )
                    held_since = None
                    _good_reads_since_error = 0
                    if _motor_error_count >= _MOTOR_ERROR_BACKOFF_AFTER:
                        await asyncio.sleep(_MOTOR_ERROR_BACKOFF_S)
                else:
                    _motor_error_count = 0

        # Pre-download recorded move datasets in background to avoid delays on first play
        # This runs in asyncio's default ThreadPoolExecutor (fire and forget)
        if args.preload_datasets:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, preload_with_logging)

        # Start periodic dataset updater if enabled (interval > 0)
        if args.dataset_update_interval_hours > 0:
            dataset_updater_task = asyncio.create_task(
                dataset_updater(args.dataset_update_interval_hours)
            )
            logger.info(
                f"Dataset updater started (interval: {args.dataset_update_interval_hours}h)"
            )

        # Fire WiFi init as a background thread so it runs concurrently with
        # daemon startup rather than blocking create_app() at import time.
        if args.wireless_version:
            with timing_event("daemon.wifi.startup_schedule"):
                wifi_config.start_wifi_init_on_startup()

        try:
            with timing_event(
                "fastapi.lifespan.startup",
                autostart=args.autostart,
                wireless_version=args.wireless_version,
                no_media=args.no_media,
            ):
                if args.autostart:
                    if systemd_notifier:
                        systemd_notifier.status("Starting Reachy Mini backend")
                    with timing_event("daemon.autostart"):
                        await app.state.daemon.start(
                            serialport=args.serialport,
                            sim=args.sim,
                            mockup_sim=args.mockup_sim,
                            scene=args.scene,
                            headless=args.headless,
                            use_audio=not args.no_media,
                            kinematics_engine=args.kinematics_engine,
                            check_collision=args.check_collision,
                            wake_up_on_start=args.wake_up_on_start,
                            hardware_config_filepath=args.hardware_config_filepath,
                        )

                # Register mDNS service only after the daemon is ready
                if systemd_notifier:
                    systemd_notifier.status("Registering Reachy Mini mDNS service")
                with timing_event("daemon.mdns.register", robot_name=args.robot_name):
                    mdns.register()

            antenna_sleep_task = asyncio.create_task(antenna_sleep_monitor())

            yield
        finally:
            # Cancel dataset updater task if running
            if systemd_notifier:
                systemd_notifier.stopping()
            if dataset_updater_task and not dataset_updater_task.done():
                dataset_updater_task.cancel()
                try:
                    await dataset_updater_task
                except asyncio.CancelledError:
                    pass

            if antenna_sleep_task and not antenna_sleep_task.done():
                antenna_sleep_task.cancel()
                try:
                    await antenna_sleep_task
                except asyncio.CancelledError:
                    pass

            # Unregister mDNS service
            mdns.unregister()

            # Ensure cleanup happens even if there's an exception
            try:
                logger.info("Shutting down app manager...")
                await app.state.app_manager.close()
            except Exception as e:
                logger.exception(f"Error closing app manager: {e}")

            try:
                logger.info("Shutting down daemon...")
                # SIGUSR1 from the GPIO shutdown daemon (power-button release)
                # forces goto_sleep regardless of the CLI flag, so a user-set
                # --no-goto-sleep-on-stop never causes a head-drop on the
                # consumer power-button path. The flag remains honored for
                # other shutdown triggers (systemctl stop, Ctrl-C, etc.).
                force_safe = getattr(app.state, "force_safe_shutdown", False)
                goto_sleep = True if force_safe else args.goto_sleep_on_stop
                await app.state.daemon.stop(
                    goto_sleep_on_stop=goto_sleep,
                )
            except Exception as e:
                logger.exception(f"Error stopping daemon: {e}")

    with timing_event("daemon.create_app.fastapi"):
        app = FastAPI(
            lifespan=lifespan,
        )

    app.state.args = args
    sim_mode = (
        SimulationMode.MUJOCO
        if args.sim
        else SimulationMode.MOCKUP
        if args.mockup_sim
        else SimulationMode.NONE
    )
    with timing_event(
        "daemon.create_app.daemon_construct",
        wireless_version=args.wireless_version,
        no_media=args.no_media,
        sim_mode=sim_mode.value,
    ):
        app.state.daemon = Daemon(
            robot_name=args.robot_name,
            wireless_version=args.wireless_version,
            desktop_app_daemon=args.desktop_app_daemon,
            log_level=args.log_level,
            no_media=args.no_media,
            sim_mode=sim_mode,
            central_relay=args.central_relay,
        )
    with timing_event(
        "daemon.create_app.app_manager_construct",
        wireless_version=args.wireless_version,
        desktop_app_daemon=args.desktop_app_daemon,
    ):
        app.state.app_manager = AppManager(
            wireless_version=args.wireless_version,
            desktop_app_daemon=args.desktop_app_daemon,
            daemon=app.state.daemon,
        )

    with timing_event("daemon.create_app.api_router_construct"):
        router = APIRouter(prefix="/api")

    api_routers = (
        ("apps", apps.router),
        ("audio_config", audio_config.router),
        ("camera", camera.router),
        ("daemon", daemon.router),
        ("hf_auth", hf_auth.router),
        ("kinematics", kinematics.router),
        ("media", media.router),
        ("motors", motors.router),
        ("move", move.router),
        ("state", state.router),
        ("volume", volume.router),
    )
    with timing_event("daemon.create_app.api_router_includes", count=len(api_routers)):
        for _, api_router in api_routers:
            router.include_router(api_router)

    if args.wireless_version:
        with timing_event("daemon.create_app.wireless_router_imports"):
            from .routers import autostart, cache, update, wifi_config

        wireless_routers = (
            ("autostart", autostart.router),
            ("cache", cache.router),
            ("logs", logs.router),
            ("update", update.router),
            ("wifi_config", wifi_config.router),
        )
        with timing_event(
            "daemon.create_app.wireless_router_includes",
            count=len(wireless_routers),
        ):
            for _, wireless_router in wireless_routers:
                app.include_router(wireless_router)
    app_routers = (
        ("api", router),
        ("sdk_ws", sdk_ws.router),
    )
    with timing_event("daemon.create_app.app_router_includes", count=len(app_routers)):
        for _, app_router in app_routers:
            app.include_router(app_router)

    if health_check_event is not None:

        @app.post("/health-check")
        async def health_check() -> dict[str, str]:
            """Health check endpoint to reset the health check timer."""
            health_check_event.set()
            return {"status": "ok"}

    # Cap the size of sound uploads before the body is read, so a large file
    # cannot be streamed to disk (see GHSA-m2pc-3q4q-w6jr). Added before CORS
    # so CORS remains the outermost middleware and even a 413 carries its
    # headers.
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_body_size=media.MAX_SOUND_UPLOAD_BYTES,
        paths={"/api/media/sounds/upload"},
    )

    # Restrict cross-origin access to local browser tooling and the native app
    # webviews (see CORS_ORIGIN_REGEX); everything else is same-origin or WebRTC.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    STATIC_DIR = Path(__file__).parent / "dashboard" / "static"
    TEMPLATES_DIR = Path(__file__).parent / "dashboard" / "templates"

    with timing_event("daemon.create_app.static_mounts"):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/")
    async def dashboard(request: Request) -> HTMLResponse:
        """Render the dashboard."""
        return templates.TemplateResponse(
            "index.html", {"request": request, "args": args}
        )

    if args.wireless_version:

        @app.get("/settings")
        async def settings(request: Request) -> HTMLResponse:
            """Render the settings page."""
            return templates.TemplateResponse("settings.html", {"request": request})

        @app.get("/logs")
        async def logs_page(request: Request) -> HTMLResponse:
            """Render the logs page."""
            return templates.TemplateResponse("logs.html", {"request": request})

    return app


def run_app(args: Args) -> None:
    """Run the FastAPI app with Uvicorn."""
    # Configure logging to ensure all logs go to stderr (captured by systemd).
    configure_daemon_logging(args.log_level, args.log_file)
    root_logger = logging.getLogger()
    systemd_notifier = SystemdNotifier.from_environment()
    systemd_notifier.status("Starting Reachy Mini daemon process")

    # Explicitly configure the apps.manager logger to ensure propagation
    apps_logger = logging.getLogger("reachy_mini.apps.manager")
    apps_logger.setLevel(args.log_level)
    apps_logger.propagate = True  # Ensure it propagates to root logger

    # Downgrade noisy polling routes to DEBUG in uvicorn access logs
    class AccessLogFilter(logging.Filter):
        _POLLING_PATHS = {"/health-check", "/api/hf-auth/relay-status"}

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if any(path in msg for path in self._POLLING_PATHS):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
            return True

    logging.getLogger("uvicorn.access").addFilter(AccessLogFilter())

    # Install exception hook to catch uncaught exceptions
    def exception_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: types.TracebackType | None,
    ) -> None:
        """Log uncaught exceptions with full traceback."""
        if issubclass(exc_type, KeyboardInterrupt):
            # Allow KeyboardInterrupt to exit normally
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        root_logger.critical(
            "Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback)
        )
        sys.stderr.flush()

    sys.excepthook = exception_hook

    async def run_server() -> None:
        # Set up asyncio exception handler to catch unhandled task exceptions
        loop = asyncio.get_running_loop()

        def asyncio_exception_handler(
            loop: asyncio.AbstractEventLoop, context: dict[str, Any]
        ) -> None:
            """Handle exceptions in asyncio tasks."""
            exception = context.get("exception")
            if exception:
                root_logger.error(
                    f"Unhandled exception in asyncio task: {context.get('message', 'No message')}",
                    exc_info=(type(exception), exception, exception.__traceback__),
                )
            else:
                root_logger.error(f"Asyncio error: {context}")
            sys.stderr.flush()

        loop.set_exception_handler(asyncio_exception_handler)

        health_check_event = asyncio.Event()
        with timing_event("daemon.create_app"):
            app = create_app(args, health_check_event, systemd_notifier)

        config = uvicorn.Config(
            app,
            host=_resolve_bind_host(args),
            port=args.fastapi_port,
            log_config=None,  # Don't override Python logging configuration
        )
        server = uvicorn.Server(config)

        # uvicorn installs its own SIGINT/SIGTERM handlers once server.serve()
        # starts running; this covers the gap before that, so a SIGTERM during
        # daemon startup still produces a graceful exit (lifespan finally runs
        # daemon.stop(goto_sleep_on_stop=True), moving the head to a safe pose
        # before motor power is cut).
        def _request_graceful_shutdown() -> None:
            if not server.should_exit:
                logger.info("Received SIGTERM, requesting graceful shutdown.")
                server.should_exit = True

        # SIGUSR1: "force safe shutdown" — used by the GPIO shutdown daemon
        # on power-button release. Overrides --no-goto-sleep-on-stop so the
        # head always reaches the sleep pose before motor power is cut.
        def _request_safe_shutdown() -> None:
            if not getattr(app.state, "force_safe_shutdown", False):
                logger.info(
                    "Received SIGUSR1, forcing safe shutdown (goto_sleep_on_stop=True)."
                )
                app.state.force_safe_shutdown = True
            if not server.should_exit:
                server.should_exit = True

        try:
            loop.add_signal_handler(signal.SIGTERM, _request_graceful_shutdown)
            loop.add_signal_handler(signal.SIGUSR1, _request_safe_shutdown)
        except NotImplementedError:
            # add_signal_handler is POSIX-only; fall back to signal.signal.
            signal.signal(signal.SIGTERM, lambda *_: _request_graceful_shutdown())
            signal.signal(signal.SIGUSR1, lambda *_: _request_safe_shutdown())

        health_check_task = None
        readiness_task: asyncio.Task[None] | None = None
        watchdog_task: asyncio.Task[None] | None = None

        async def health_check_timeout(timeout_seconds: float) -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        health_check_event.wait(),
                        timeout=timeout_seconds,
                    )
                    health_check_event.clear()
                except asyncio.TimeoutError:
                    logger.warning("Health check timeout reached, stopping app.")
                    server.should_exit = True
                    break
                except asyncio.CancelledError:
                    logger.info("Health check task cancelled.")
                    break

        try:
            _t_serve_start = time.perf_counter()
            _daemon_startup_failed = False

            async def notify_when_serving() -> None:
                # ERROR paths in daemon.start(): backend.ready.wait() timeout (2s),
                # wake_up() exception, or backend thread error. status() also flips
                # state to ERROR if backend_status.error is set at call time.
                nonlocal _daemon_startup_failed
                while not server.started and not server.should_exit:
                    await asyncio.sleep(0.05)
                if server.started:
                    log_event(
                        "daemon.uvicorn.startup",
                        duration_ms=round(
                            (time.perf_counter() - _t_serve_start) * 1000, 3
                        ),
                    )
                    daemon_status = app.state.daemon.status()
                    logger.info(
                        "notify_when_serving: state=%s%s",
                        daemon_status.state.value,
                        f", error={daemon_status.error!r}" if daemon_status.error else "",
                    )
                    if args.autostart and daemon_status.state != DaemonState.RUNNING:
                        systemd_notifier.status(
                            f"Daemon startup failed: {daemon_status.state.value}"
                        )
                        # Trigger graceful shutdown so the lifespan finally block
                        # runs (parks the robot safely) before we exit non-zero.
                        # Without this, the early return leaves systemd stuck in
                        # "activating" state until TimeoutStartSec expires (~90s).
                        _daemon_startup_failed = True
                        server.should_exit = True
                        return
                    systemd_notifier.ready(
                        "FastAPI serving; Reachy Mini daemon startup complete"
                    )

            readiness_task = asyncio.create_task(notify_when_serving())
            watchdog_task = asyncio.create_task(systemd_notifier.watchdog_loop())
            if args.timeout_health_check is not None:
                health_check_task = asyncio.create_task(
                    health_check_timeout(args.timeout_health_check)
                )
            await server.serve()
            if _daemon_startup_failed:
                raise RuntimeError(
                    f"Daemon failed to reach RUNNING state — triggering Restart=on-failure"
                )
        except KeyboardInterrupt:
            logger.info("Received Ctrl-C, shutting down gracefully.")
        except Exception as e:
            logger.exception(f"Error during server operation: {e}")
            raise
        finally:
            systemd_notifier.stopping()
            for task in (readiness_task, watchdog_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            # Cancel health check task if it exists
            if health_check_task and not health_check_task.done():
                health_check_task.cancel()
                try:
                    await health_check_task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Shutdown complete.")
    except Exception as e:
        logger.exception(f"Error during shutdown: {e}")
        sys.stderr.flush()
        raise


def main() -> None:
    """Run the FastAPI app with Uvicorn."""
    default_args = Args()

    parser = argparse.ArgumentParser(description="Run the Reachy Mini daemon.")
    parser.add_argument(
        "--wireless-version",
        action="store_true",
        default=default_args.wireless_version,
        help="Use the wireless version of Reachy Mini (default: False).",
    )
    parser.add_argument(
        "--desktop-app-daemon",
        action="store_true",
        default=default_args.desktop_app_daemon,
        help="Use the desktop version of Reachy Mini (default: False).",
    )

    parser.add_argument(
        "--robot-name",
        type=str,
        default=default_args.robot_name,
        help="Name of the robot (default: reachy_mini).",
    )

    # Real robot mode
    parser.add_argument(
        "-p",
        "--serialport",
        type=str,
        default=default_args.serialport,
        help="Serial port for real motors (default: will try to automatically find the port).",
    )
    default_hw_config_path = str(
        (
            Path(__file__).parent.parent.parent
            / "assets"
            / "config"
            / "hardware_config.yaml"
        ).resolve()
    )
    parser.add_argument(
        "--hardware-config-filepath",
        type=str,
        default=default_hw_config_path,
        help=f"Path to the hardware configuration YAML file (default: {default_hw_config_path}).",
    )
    # Simulation mode
    parser.add_argument(
        "--sim",
        action="store_true",
        default=default_args.sim,
        help="Run in simulation mode using Mujoco.",
    )
    parser.add_argument(
        "--mockup-sim",
        action="store_true",
        default=default_args.mockup_sim,
        help="Run in mockup simulation mode (no MuJoCo required).",
    )
    parser.add_argument(
        "--scene",
        type=str,
        default=default_args.scene,
        help="Name of the scene to load (default: empty)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=default_args.headless,
        help="Run the daemon in headless mode (default: False).",
    )
    parser.add_argument(
        "--no-media",
        action="store_true",
        default=default_args.no_media,
        help="Disable all media (camera, audio, WebRTC). Use if you handle media yourself.",
    )
    # Daemon options
    parser.add_argument(
        "--autostart",
        action="store_true",
        default=default_args.autostart,
        help="Automatically start the daemon on launch (default: True).",
    )
    parser.add_argument(
        "--no-autostart",
        action="store_false",
        dest="autostart",
        help="Do not automatically start the daemon on launch (default: False).",
    )
    parser.add_argument(
        "--timeout-health-check",
        type=float,
        default=None,
        help="Set the health check timeout in seconds (default: None).",
    )
    parser.add_argument(
        "--wake-up-on-start",
        action="store_true",
        default=default_args.wake_up_on_start,
        help="Wake up the robot on daemon start (default: True).",
    )
    parser.add_argument(
        "--no-wake-up-on-start",
        action="store_false",
        dest="wake_up_on_start",
        help="Do not wake up the robot on daemon start (default: False).",
    )
    parser.add_argument(
        "--startup-app",
        type=str,
        default=default_args.startup_app,
        dest="startup_app",
        help="Name of an app to start automatically after the robot wakes up "
        "(installed from the catalog first if it isn't already installed).",
    )
    parser.add_argument(
        "--goto-sleep-on-stop",
        action="store_true",
        default=default_args.goto_sleep_on_stop,
        help="Put the robot to sleep on daemon stop (default: True).",
    )
    parser.add_argument(
        "--no-goto-sleep-on-stop",
        action="store_false",
        dest="goto_sleep_on_stop",
        help="Do not put the robot to sleep on daemon stop (default: False).",
    )
    parser.add_argument(
        "--preload-datasets",
        action="store_true",
        default=default_args.preload_datasets,
        help="Pre-download recorded move datasets (emotions, dances) at startup (default: False).",
    )
    parser.add_argument(
        "--no-preload-datasets",
        action="store_false",
        dest="preload_datasets",
        help="Do not pre-download datasets at startup (default: False).",
    )
    parser.add_argument(
        "--dataset-update-interval",
        type=float,
        default=default_args.dataset_update_interval_hours,
        dest="dataset_update_interval_hours",
        help="Interval in hours for background dataset update checks (default: 24.0, 0 to disable).",
    )
    # Server options
    parser.add_argument(
        "--central-relay",
        action="store_true",
        default=default_args.central_relay,
        dest="central_relay",
        help="Enable the HuggingFace central signaling relay for remote WebRTC access (default: disabled).",
    )
    parser.add_argument(
        "--no-central-relay",
        action="store_false",
        dest="central_relay",
        help="Disable the HuggingFace central signaling relay (default).",
    )
    # Kinematics options
    parser.add_argument(
        "--check-collision",
        action="store_true",
        default=default_args.check_collision,
        help="Enable collision checking (default: False).",
    )

    parser.add_argument(
        "--kinematics-engine",
        type=str,
        default=default_args.kinematics_engine,
        choices=["Placo", "NN", "AnalyticalKinematics"],
        help="Set the kinematics engine (default: AnalyticalKinematics).",
    )
    # FastAPI server options
    parser.add_argument(
        "--fastapi-host",
        type=str,
        default=default_args.fastapi_host,
        help=(
            "Address the HTTP API binds to. Default (unset): 0.0.0.0 on the "
            "wireless version, 127.0.0.1 otherwise."
        ),
    )
    parser.add_argument(
        "--fastapi-port",
        type=int,
        default=default_args.fastapi_port,
    )
    # Logging options
    parser.add_argument(
        "--log-level",
        type=str,
        default=default_args.log_level,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO).",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=default_args.log_file,
        help="Path to a file to write logs to.",
    )

    args = parser.parse_args()

    # Configure logging early so wireless-check spans reach the log file.
    # run_app() calls this again with the same args — idempotent.
    configure_daemon_logging(args.log_level, args.log_file)
    log_event("daemon.main.start", wireless_version=args.wireless_version)

    if args.wireless_version:
        SystemdNotifier.from_environment().status("Running wireless startup checks")
        with timing_event("daemon.wireless_checks.venvs_ownership"):
            check_and_fix_venvs_ownership(custom_logger=logging.getLogger())
        with timing_event("daemon.wireless_checks.bluetooth_service"):
            check_and_update_bluetooth_service()
        with timing_event("daemon.wireless_checks.wireless_launcher"):
            check_and_update_wireless_launcher()
        with timing_event("daemon.wireless_checks.apps_venv_sdk"):
            check_and_sync_apps_venv_sdk()
        with timing_event("daemon.wireless_checks.restore_venv"):
            check_and_fix_restore_venv()
        with timing_event("daemon.wireless_checks.asoundrc") as te:
            configured = check_reachymini_asoundrc()
            te.attrs["configured"] = configured
            if configured:
                logging.getLogger().info(
                    "~/.asoundrc correctly configured for Reachy Mini Audio."
                )
            else:
                logging.getLogger().warning(
                    "~/.asoundrc not found or not correctly configured for Reachy Mini Audio. "
                    "Creating a new one."
                )
                write_asoundrc_to_home()

    run_app(Args(**vars(args)))


if __name__ == "__main__":
    main()
