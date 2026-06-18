"""FK recompute gate: update_head_kinematics_model should skip the forward-
kinematics recompute when the head joints are unchanged (within tolerance) since
the last computation — the short-circuit the docstring already promises. Cuts the
daemon's idle CPU (FK + scipy Euler ran 50x/sec on a stationary head)."""
import numpy as np

from reachy_mini.daemon.backend.abstract import (
    _fk_recompute_needed,
    _fk_skip_active,
    _resolve_control_loop_hz,
)

POSE = np.eye(4)


# --- 2-tier motor-safety gating: opt-in default + emergency kill switch ---

def test_fk_skip_inactive_by_default():
    # Opt-in: with the feature flag off, FK-skip is inactive -> stock (always recompute).
    assert _fk_skip_active(False, False) is False


def test_fk_skip_active_when_opted_in():
    assert _fk_skip_active(True, False) is True


def test_master_kill_overrides_opt_in():
    # Emergency switch wins even if the feature flag is on.
    assert _fk_skip_active(True, True) is False


# --- control-loop frequency knob (opt-in; default = stock 50Hz; master kill) ---

def test_control_hz_defaults_to_stock_when_unset():
    assert _resolve_control_loop_hz(None, False) == 50.0


def test_control_hz_honors_valid_override():
    assert _resolve_control_loop_hz("30", False) == 30.0


def test_control_hz_master_kill_forces_stock():
    assert _resolve_control_loop_hz("30", True) == 50.0


def test_control_hz_clamps_out_of_range():
    assert _resolve_control_loop_hz("5", False) == 20.0     # below floor
    assert _resolve_control_loop_hz("999", False) == 100.0  # above ceiling


def test_control_hz_invalid_falls_back_to_stock():
    assert _resolve_control_loop_hz("abc", False) == 50.0


def test_first_call_recomputes():
    # No prior joints/pose -> must compute.
    assert _fk_recompute_needed(np.zeros(7), None, None, 1e-3) is True


def test_unchanged_within_tolerance_skips():
    j = np.full(7, 0.1)
    assert _fk_recompute_needed(j + 1e-4, j, POSE, 1e-3) is False


def test_changed_beyond_tolerance_recomputes():
    j = np.full(7, 0.1)
    assert _fk_recompute_needed(j + 1e-2, j, POSE, 1e-3) is True


def test_missing_prior_pose_recomputes():
    # Joints match but we have no cached pose to reuse -> must compute.
    j = np.full(7, 0.1)
    assert _fk_recompute_needed(j, j, None, 1e-3) is True
