"""IMU read decimation knob (REACHY_MINI_IMU_HZ): resolver semantics.

Two-tier motor-safety policy, same as FK-skip and the control-loop knob:
default = stock (read the IMU every control-loop tick, resolver returns None),
opt-in via env, and the emergency master kill forces stock regardless.

Profiling context (2026-07-07, ricci at idle): stock every-tick IMU reads are
6 blocking I2C transactions + a Madgwick update per 50 Hz tick — ~12 ms of
every 20 ms tick, ~60% of the daemon backend thread's wall time.
"""

import pytest

from reachy_mini.daemon.backend.abstract import _resolve_imu_read_period

STOCK_HZ = 50.0


# --- default / opt-in / master kill ---


def test_stock_when_unset():
    assert _resolve_imu_read_period(None, False, STOCK_HZ) is None


def test_valid_override_returns_period():
    assert _resolve_imu_read_period("10", False, STOCK_HZ) == pytest.approx(0.1)


def test_master_kill_forces_stock():
    assert _resolve_imu_read_period("10", True, STOCK_HZ) is None


# --- clamping ---


def test_clamps_above_control_loop_hz():
    # Reading faster than the loop tick is meaningless -> clamp to loop rate.
    assert _resolve_imu_read_period("999", False, STOCK_HZ) == pytest.approx(
        1.0 / STOCK_HZ
    )


def test_clamps_below_one_hz():
    # Below 1 Hz the SDK's cached imu property gets too stale.
    assert _resolve_imu_read_period("0.1", False, STOCK_HZ) == pytest.approx(1.0)


def test_clamp_follows_downclocked_loop():
    # With the control loop downclocked, the ceiling follows it.
    assert _resolve_imu_read_period("50", False, 20.0) == pytest.approx(1.0 / 20.0)


# --- bad input falls back to stock ---


def test_unparseable_falls_back_to_stock():
    assert _resolve_imu_read_period("abc", False, STOCK_HZ) is None


def test_empty_string_falls_back_to_stock():
    assert _resolve_imu_read_period("", False, STOCK_HZ) is None
