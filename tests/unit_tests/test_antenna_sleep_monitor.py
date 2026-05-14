"""Unit tests for antenna_sleep_monitor state-machine hardening (issue #22).

Tests verify that motor comms errors are handled safely:
- After an error burst, gesture detection does not re-arm until N consecutive
  good reads have passed (_MOTOR_RECOVERY_READS = 3).
- Rapid repeated state transitions within _MIN_TRANSITION_INTERVAL_S are
  suppressed.
- Only the first error in a burst is logged at WARNING level.
"""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn

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
