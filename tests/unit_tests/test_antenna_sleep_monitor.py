"""Unit tests for antenna_sleep_monitor state-machine hardening.

Issue #22 coverage:
- After an error burst, gesture detection does not re-arm until N consecutive
  good reads have passed (_MOTOR_RECOVERY_READS = 3).
- Rapid repeated state transitions within _MIN_TRANSITION_INTERVAL_S are
  suppressed.
- Only the first error in a burst is logged at WARNING level.

Issue #33 coverage:
- Wake gesture is suppressed when the autostart systemd unit is active.
- Wake gesture is suppressed when app_manager.current_app is set.
- Wake gesture still fires when both gates are clean (happy-path regression).
- After a daemon-initiated motion (wake/sleep), the self-motion cooldown
  blocks both subsequent wake and sleep gestures for _SELF_MOTION_COOLDOWN_S.
- The cooldown expires correctly so gestures re-arm after the window.
"""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn

from reachy_mini.daemon.app import main as daemon_main
from reachy_mini.daemon.app.main import Args, create_app
from reachy_mini.io.protocol import MotorControlMode


async def _start_app(
    **args_overrides: Any,
) -> tuple[Any, uvicorn.Server, threading.Thread]:
    """Start a full FastAPI + daemon server.  Returns (app, server, thread)."""
    base: dict[str, Any] = dict(
        mockup_sim=True,
        headless=True,
        wake_up_on_start=False,
        no_media=True,
        autostart=True,
        fastapi_port=0,
    )
    base.update(args_overrides)
    args = Args(**base)

    app = create_app(args)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    while not server.started:
        await asyncio.sleep(0.05)

    return app, server, thread


async def _stop_app(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)


# ──────────────────────────────────────────────────────────────────────────────
# Recovery-gate: N good reads required before gesture detection re-arms
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_wake_during_recovery_reads() -> None:
    """After error burst, wake_up must NOT fire until _MOTOR_RECOVERY_READS
    consecutive clean reads have passed — even if the first good read is already
    in wake-gesture territory.

    Scenario:
      - daemon starts sleeping (awake=False because motors disabled)
      - backend raises RuntimeError 3 times (error burst)
      - next 2 good reads return wake-gesture positions  ← still in recovery gate
      - 3rd good read returns wake-gesture position      ← gate opens, held_since set
      - hold timer requires _ANTENNA_SLEEP_HOLD_S more seconds to elapse
      → wake_up must NOT have been called during the 5 reads tested here
    """
    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None

        # Start in sleeping state.
        backend.set_motor_control_mode(MotorControlMode.Disabled)
        # Give the monitor one cycle to sync its awake flag to the disabled state.
        await asyncio.sleep(0.25)

        # Wake-gesture positions: antennas pushed outward from sleep pose.
        # _ANTENNA_LEFT_SLEEP = -3.05, threshold = 0.4  → left > -2.65
        # _ANTENNA_RIGHT_SLEEP = 3.05, threshold = 0.4  → right < 2.65
        import numpy as np
        wake_positions = np.array([-2.0, 2.0], dtype=float)

        error_call_count = 0
        wake_up_called = False

        original_get = backend.get_present_antenna_joint_positions

        def patched_get() -> Any:
            nonlocal error_call_count
            error_call_count += 1
            if error_call_count <= 3:
                raise RuntimeError("Motor communication error!")
            return wake_positions

        original_wake_up = backend.wake_up

        async def spy_wake_up() -> None:
            nonlocal wake_up_called
            wake_up_called = True
            await original_wake_up()

        backend.get_present_antenna_joint_positions = patched_get
        backend.wake_up = spy_wake_up

        # At 10 Hz: 3 errors (0.3s) + 3 recovery reads (0.3s, gate still closed)
        # + hold timer needs _ANTENNA_SLEEP_HOLD_S (0.5s) after the gate opens.
        # Total minimum before wake_up could legitimately fire: ~1.1s.
        # We check at 0.8s — inside the gate+hold window — so wake_up must be
        # suppressed at this point regardless of exact scheduling jitter.
        await asyncio.sleep(0.8)

        assert not wake_up_called, (
            "wake_up was called during recovery-gate window — "
            "spurious transition possible after motor comms error"
        )
    finally:
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


# ──────────────────────────────────────────────────────────────────────────────
# Transition cap: repeated transitions within _MIN_TRANSITION_INTERVAL_S blocked
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rapid_wake_transition_suppressed() -> None:
    """A second wake gesture within _MIN_TRANSITION_INTERVAL_S must be suppressed.

    This is the direct cap on the robot_comic#265 scenario where wake_up fired
    immediately after a legitimate wake_up, causing a violent head snap.
    """
    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None

        import numpy as np
        # Start sleeping.
        backend.set_motor_control_mode(MotorControlMode.Disabled)
        await asyncio.sleep(0.25)

        wake_positions = np.array([-2.0, 2.0], dtype=float)
        neutral_positions = np.array([0.0, 0.0], dtype=float)

        wake_up_call_count = 0
        original_wake_up = backend.wake_up
        original_get = backend.get_present_antenna_joint_positions

        async def spy_wake_up() -> None:
            nonlocal wake_up_call_count
            wake_up_call_count += 1
            await original_wake_up()

        # Phase 1: hold wake gesture long enough to trigger first wake_up.
        backend.wake_up = spy_wake_up
        backend.get_present_antenna_joint_positions = lambda: wake_positions

        # _ANTENNA_SLEEP_HOLD_S = 0.5s, so 0.7s guarantees the first transition.
        await asyncio.sleep(0.9)
        assert wake_up_call_count == 1, (
            f"Expected exactly 1 wake_up call after held gesture, got {wake_up_call_count}"
        )

        # Phase 2: immediately re-enter sleeping state (motor disabled) then try
        # to wake again within the min-transition window.
        backend.set_motor_control_mode(MotorControlMode.Disabled)
        # Keep returning wake-gesture positions — second wake attempt.
        await asyncio.sleep(0.9)

        assert wake_up_call_count == 1, (
            f"wake_up was called a second time within the min-transition window "
            f"(total calls: {wake_up_call_count}); spurious gesture misfire possible"
        )
    finally:
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


# ──────────────────────────────────────────────────────────────────────────────
# Log demotion: subsequent errors in a burst must drop to DEBUG
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_burst_log_levels(caplog: pytest.LogCaptureFixture) -> None:
    """First error in a burst → WARNING; subsequent errors → DEBUG."""
    import logging

    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None

        call_count = 0
        original_get = backend.get_present_antenna_joint_positions

        def failing_get() -> Any:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Motor communication error!")

        backend.get_present_antenna_joint_positions = failing_get

        with caplog.at_level(logging.DEBUG, logger="reachy_mini.daemon.app.main"):
            await asyncio.sleep(0.7)  # enough for >5 error iterations at 10 Hz

        antenna_warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "Antenna sleep monitor error" in r.message
        ]
        antenna_debugs = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG
            and "Antenna sleep monitor error" in r.message
        ]

        assert len(antenna_warns) == 1, (
            f"Expected exactly 1 WARNING for error burst, got {len(antenna_warns)}"
        )
        assert len(antenna_debugs) >= 1, (
            "Expected subsequent burst errors at DEBUG level, got none"
        )
    finally:
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


# ──────────────────────────────────────────────────────────────────────────────
# Issue #33: wake-gesture symmetric app-active gate + self-motion cooldown
# ──────────────────────────────────────────────────────────────────────────────


async def _put_to_sleep(backend: Any) -> None:
    """Move the monitor into sleeping state by disabling motors and waiting
    one cycle for the awake-flag sync.
    """
    backend.set_motor_control_mode(MotorControlMode.Disabled)
    await asyncio.sleep(0.25)


@pytest.mark.asyncio
async def test_wake_suppressed_when_current_app_set() -> None:
    """If `app.state.app_manager.current_app` is not None, the wake gesture
    must not fire even with a held wake-gesture position.

    Scenario for issue #33: an autostart-launched app is up; the daemon
    drives the head (or a SIGKILL slams it), antennas swing past the wake
    threshold, and the wake branch would otherwise call
    _start_autostart_app() again — perpetuating the crash loop.
    """
    import numpy as np

    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None
        await _put_to_sleep(backend)

        # Inject a "running app" sentinel.
        app.state.app_manager.current_app = MagicMock(name="fake_running_app")

        wake_positions = np.array([-2.0, 2.0], dtype=float)
        wake_up_called = False
        original_get = backend.get_present_antenna_joint_positions
        original_wake_up = backend.wake_up

        async def spy_wake_up() -> None:
            nonlocal wake_up_called
            wake_up_called = True
            await original_wake_up()

        backend.get_present_antenna_joint_positions = lambda: wake_positions
        backend.wake_up = spy_wake_up

        # Wait well past hold + recovery window — gesture would otherwise fire.
        await asyncio.sleep(1.2)

        assert not wake_up_called, (
            "wake_up was called while current_app was set — the wake branch "
            "is missing its symmetric app-active gate (issue #33)"
        )
    finally:
        app.state.app_manager.current_app = None
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


@pytest.mark.asyncio
async def test_wake_suppressed_when_autostart_service_active() -> None:
    """If `systemctl is-active reachy-app-autostart.service` returns 0
    (service running), the wake gesture must not fire.

    Covers the case where the autostart unit has launched an app via systemd
    but app_manager.current_app is still None (the daemon process is separate
    from the app process).
    """
    import numpy as np

    # Patch the subprocess used by _autostart_service_active to "succeed".
    async def fake_subprocess_exec(*argv: str, **kwargs: Any) -> Any:
        # Any systemctl is-active call should report active (rc=0).
        if argv and argv[0] == "systemctl" and "is-active" in argv:
            mock_proc = MagicMock()
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.returncode = 0
            return mock_proc
        # All other subprocess calls fall back to the real implementation.
        return await _real_create_subprocess_exec(*argv, **kwargs)

    _real_create_subprocess_exec = asyncio.create_subprocess_exec

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_subprocess_exec):
        app, server, thread = await _start_app()
        try:
            backend = app.state.daemon.backend
            assert backend is not None
            await _put_to_sleep(backend)

            wake_positions = np.array([-2.0, 2.0], dtype=float)
            wake_up_called = False
            original_get = backend.get_present_antenna_joint_positions
            original_wake_up = backend.wake_up

            async def spy_wake_up() -> None:
                nonlocal wake_up_called
                wake_up_called = True
                await original_wake_up()

            backend.get_present_antenna_joint_positions = lambda: wake_positions
            backend.wake_up = spy_wake_up

            # service-active cache refreshes every 1s; wait long enough that at
            # least one refresh sees the active service AND a full hold timer
            # would have elapsed.
            await asyncio.sleep(1.8)

            assert not wake_up_called, (
                "wake_up was called while autostart service was active — "
                "wake branch is missing its symmetric service-active gate (issue #33)"
            )
        finally:
            backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
            backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
            await _stop_app(server, thread)


@pytest.mark.asyncio
async def test_wake_fires_when_both_gates_clean() -> None:
    """Happy-path regression: with no app running and service inactive, the
    wake gesture must still fire normally.

    Guards against the issue-#33 fix over-blocking the legitimate use case.
    """
    import numpy as np

    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None
        await _put_to_sleep(backend)

        # Explicitly assert preconditions: both gates are clean in the default
        # test harness (systemctl returns non-zero on the dev workstation; no
        # app_manager.current_app is set by mockup_sim).
        assert app.state.app_manager.current_app is None

        wake_positions = np.array([-2.0, 2.0], dtype=float)
        wake_up_called = False
        original_get = backend.get_present_antenna_joint_positions
        original_wake_up = backend.wake_up

        async def spy_wake_up() -> None:
            nonlocal wake_up_called
            wake_up_called = True
            # Fast no-op so the monitor isn't blocked on the ~4s real animation.
            return None

        backend.get_present_antenna_joint_positions = lambda: wake_positions
        backend.wake_up = spy_wake_up

        # 0.9s ≫ recovery gate (0.3s) + hold timer (0.5s).
        await asyncio.sleep(0.9)

        assert wake_up_called, (
            "wake_up did NOT fire with both gates clean — the issue-#33 fix "
            "is over-blocking the legitimate wake path"
        )
    finally:
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


async def _fast_noop() -> None:
    """Async no-op used to replace backend.wake_up / goto_sleep so the monitor
    loop is not blocked on multi-second real-motion animations during tests.
    """
    return None


@pytest.mark.asyncio
async def test_self_motion_cooldown_blocks_subsequent_sleep_gesture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a wake_up fires, the self-motion cooldown must block any sleep
    gesture for _SELF_MOTION_COOLDOWN_S seconds.

    Uses a short cooldown (3s) for test speed.  Verifies that goto_sleep is
    NOT called during the cooldown window even when sleep-gesture positions
    are sustained.

    Note: backend.wake_up takes ~4s in mockup_sim because it animates the
    head; we replace it with a fast no-op so the monitor coroutine isn't
    blocked during the cooldown window we're trying to observe.
    """
    import numpy as np

    monkeypatch.setattr(daemon_main, "_SELF_MOTION_COOLDOWN_S", 3.0)

    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None
        await _put_to_sleep(backend)

        wake_positions = np.array([-2.0, 2.0], dtype=float)
        sleep_positions = np.array([-1.0, 1.0], dtype=float)  # squeezed inward

        wake_up_count = 0
        goto_sleep_count = 0
        original_get = backend.get_present_antenna_joint_positions
        original_wake_up = backend.wake_up
        original_goto_sleep = backend.goto_sleep

        async def spy_wake_up() -> None:
            nonlocal wake_up_count
            wake_up_count += 1
            await _fast_noop()

        async def spy_goto_sleep() -> None:
            nonlocal goto_sleep_count
            goto_sleep_count += 1
            await _fast_noop()

        backend.wake_up = spy_wake_up
        backend.goto_sleep = spy_goto_sleep

        # Phase 1: hold wake gesture until wake_up fires (~0.9s for 0.5s hold
        # + 0.3s recovery + jitter).
        backend.get_present_antenna_joint_positions = lambda: wake_positions
        await asyncio.sleep(1.1)
        assert wake_up_count == 1, (
            f"Test setup failure: expected 1 wake_up, got {wake_up_count}"
        )

        # Phase 2: immediately switch to sleep-gesture positions.  Cooldown is
        # 3s; goto_sleep must NOT fire during this window.  Sample at 1.5s
        # (well inside the cooldown, past any normal hold timer).
        backend.get_present_antenna_joint_positions = lambda: sleep_positions
        await asyncio.sleep(1.5)

        assert goto_sleep_count == 0, (
            f"goto_sleep fired during the self-motion cooldown window "
            f"(count={goto_sleep_count}); cooldown gate is not blocking the "
            f"sleep branch"
        )
    finally:
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        backend.goto_sleep = original_goto_sleep  # type: ignore[possibly-undefined]
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


@pytest.mark.asyncio
async def test_self_motion_cooldown_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the cooldown window elapses, gesture detection must re-arm and
    a sustained sleep gesture must fire goto_sleep.

    Uses a very short cooldown (0.5s) so the test finishes quickly.  Combined
    with _MIN_TRANSITION_INTERVAL_S (2.0s) we must wait past BOTH windows
    before the second transition can fire.

    Uses fast no-op spies for wake_up / goto_sleep so the monitor loop is
    not blocked on the real ~4s wake_up animation.
    """
    import numpy as np

    monkeypatch.setattr(daemon_main, "_SELF_MOTION_COOLDOWN_S", 0.5)

    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None
        await _put_to_sleep(backend)

        wake_positions = np.array([-2.0, 2.0], dtype=float)
        sleep_positions = np.array([-1.0, 1.0], dtype=float)

        wake_up_count = 0
        goto_sleep_count = 0
        original_get = backend.get_present_antenna_joint_positions
        original_wake_up = backend.wake_up
        original_goto_sleep = backend.goto_sleep

        async def spy_wake_up() -> None:
            nonlocal wake_up_count
            wake_up_count += 1
            await _fast_noop()

        async def spy_goto_sleep() -> None:
            nonlocal goto_sleep_count
            goto_sleep_count += 1
            await _fast_noop()

        backend.wake_up = spy_wake_up
        backend.goto_sleep = spy_goto_sleep

        # Phase 1: trigger a wake_up to arm the cooldown.
        backend.get_present_antenna_joint_positions = lambda: wake_positions
        await asyncio.sleep(1.1)
        assert wake_up_count == 1

        # Phase 2: switch to sleep positions and wait past BOTH the cooldown
        # (0.5s) AND _MIN_TRANSITION_INTERVAL_S (2.0s) AND the hold timer
        # (0.5s) — generously sample at 3.0s.
        backend.get_present_antenna_joint_positions = lambda: sleep_positions
        await asyncio.sleep(3.0)

        assert goto_sleep_count >= 1, (
            f"goto_sleep did not fire after cooldown expired "
            f"(count={goto_sleep_count}); cooldown may be sticky"
        )
    finally:
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        backend.goto_sleep = original_goto_sleep  # type: ignore[possibly-undefined]
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)


@pytest.mark.asyncio
async def test_self_motion_cooldown_blocks_subsequent_wake_gesture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After goto_sleep fires (sleep gesture), the cooldown must also block
    a subsequent wake gesture for the cooldown window.

    Symmetric coverage of the cooldown's both-directions guarantee.
    """
    import numpy as np

    monkeypatch.setattr(daemon_main, "_SELF_MOTION_COOLDOWN_S", 3.0)

    app, server, thread = await _start_app()
    try:
        backend = app.state.daemon.backend
        assert backend is not None
        # Start awake (default).  Let the recovery-reads gate open.
        await asyncio.sleep(0.35)

        sleep_positions = np.array([-1.0, 1.0], dtype=float)
        wake_positions = np.array([-2.0, 2.0], dtype=float)

        wake_up_count = 0
        goto_sleep_count = 0
        original_get = backend.get_present_antenna_joint_positions
        original_wake_up = backend.wake_up
        original_goto_sleep = backend.goto_sleep

        async def spy_wake_up() -> None:
            nonlocal wake_up_count
            wake_up_count += 1
            await _fast_noop()

        async def spy_goto_sleep() -> None:
            nonlocal goto_sleep_count
            goto_sleep_count += 1
            await _fast_noop()

        backend.wake_up = spy_wake_up
        backend.goto_sleep = spy_goto_sleep

        # Phase 1: hold sleep gesture until goto_sleep fires.
        backend.get_present_antenna_joint_positions = lambda: sleep_positions
        await asyncio.sleep(1.1)
        assert goto_sleep_count == 1, (
            f"Test setup failure: expected 1 goto_sleep, got {goto_sleep_count}"
        )

        # Phase 2: immediately try a wake gesture — must be cooldown-blocked.
        backend.get_present_antenna_joint_positions = lambda: wake_positions
        await asyncio.sleep(1.5)

        assert wake_up_count == 0, (
            f"wake_up fired during the self-motion cooldown window "
            f"(count={wake_up_count}); cooldown gate is not blocking the "
            f"wake branch"
        )
    finally:
        backend.wake_up = original_wake_up  # type: ignore[possibly-undefined]
        backend.goto_sleep = original_goto_sleep  # type: ignore[possibly-undefined]
        backend.get_present_antenna_joint_positions = original_get  # type: ignore[possibly-undefined]
        await _stop_app(server, thread)
