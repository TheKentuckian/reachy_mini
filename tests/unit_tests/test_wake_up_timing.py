"""Tests for the daemon Backend.wake_up animation timing knobs.

These guard the fix for TheKentuckian/robot_comic#310: the default
``wake_up()`` animation used to snap the head to neutral in ~2 s at boot,
which felt jarring. The new defaults stretch it to ~5 s and expose three
env-var knobs so operators can dial it back to the legacy snap if desired.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from reachy_mini.daemon.backend import abstract as abstract_module
from reachy_mini.daemon.backend.mockup_sim.backend import MockupSimBackend


def _make_backend() -> MockupSimBackend:
    """Build a lightweight backend with no audio, no kinematics warmup."""
    return MockupSimBackend(use_audio=False)


def _patch_wake_up_deps(
    backend: MockupSimBackend, magic_distance: float
) -> dict[str, Any]:
    """Mock goto_target / play_sound / pose helpers; record goto durations.

    Returns a dict with a ``goto_calls`` list (each entry is a dict of kwargs)
    and a ``sound_calls`` list.
    """
    recorded: dict[str, Any] = {"goto_calls": [], "sound_calls": []}

    async def fake_goto_target(
        head_pose: Any = None,
        antennas: Any = None,
        duration: float = 0.0,
        **kwargs: Any,
    ) -> None:
        recorded["goto_calls"].append(
            {
                "head_pose": head_pose,
                "antennas": antennas,
                "duration": duration,
                "kwargs": kwargs,
            }
        )

    def fake_play_sound(name: str) -> None:
        recorded["sound_calls"].append(name)

    def fake_get_current_head_pose() -> np.ndarray:
        return np.eye(4)

    def fake_distance_between_poses(_a: Any, _b: Any) -> tuple[float, float, float]:
        # wake_up only uses the third element.
        return (0.0, 0.0, float(magic_distance))

    backend.goto_target = fake_goto_target  # type: ignore[assignment]
    backend.play_sound = fake_play_sound  # type: ignore[assignment]
    backend.get_current_head_pose = fake_get_current_head_pose  # type: ignore[assignment]

    # distance_between_poses is module-level, monkeypatch the import site.
    return recorded


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no REACHY_WAKE_UP_* env var leaks in from the surrounding shell."""
    for name in (
        "REACHY_WAKE_UP_MIN_DURATION_S",
        "REACHY_WAKE_UP_DURATION_SCALE",
        "REACHY_WAKE_UP_FLOURISH_DURATION_S",
    ):
        monkeypatch.delenv(name, raising=False)


def _run_wake_up(backend: MockupSimBackend, magic_distance: float) -> dict[str, Any]:
    """Run wake_up() with all hardware-touching deps mocked."""
    recorded = _patch_wake_up_deps(backend, magic_distance)
    with patch.object(
        abstract_module,
        "distance_between_poses",
        return_value=(0.0, 0.0, float(magic_distance)),
    ):
        asyncio.run(backend.wake_up())
    return recorded


def test_default_timing_uses_floor_when_distance_small(clean_env: None) -> None:
    """A typical boot (magic_distance=100) hits the 3.0 s floor."""
    backend = _make_backend()
    recorded = _run_wake_up(backend, magic_distance=100.0)

    # 3 goto_target calls: main rise, flourish left, flourish back.
    assert len(recorded["goto_calls"]) == 3

    main_call, flourish_left, flourish_back = recorded["goto_calls"]
    # scaled = 100 * 20/1000 * 1.5 = 3.0, floor = 3.0 -> max = 3.0
    assert main_call["duration"] == pytest.approx(3.0)
    assert flourish_left["duration"] == pytest.approx(0.4)
    assert flourish_back["duration"] == pytest.approx(0.4)

    assert recorded["sound_calls"] == ["wake_up.wav"]


def test_default_timing_scales_for_large_distance(clean_env: None) -> None:
    """When scaled duration exceeds the floor, the scaled value wins."""
    backend = _make_backend()
    recorded = _run_wake_up(backend, magic_distance=400.0)

    main_call = recorded["goto_calls"][0]
    # scaled = 400 * 20/1000 * 1.5 = 12.0 > floor 3.0
    assert main_call["duration"] == pytest.approx(12.0)


def test_legacy_env_restores_pre_fix_timing(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy operators can opt back into the old snappy behaviour."""
    monkeypatch.setenv("REACHY_WAKE_UP_MIN_DURATION_S", "0")
    monkeypatch.setenv("REACHY_WAKE_UP_DURATION_SCALE", "1")
    monkeypatch.setenv("REACHY_WAKE_UP_FLOURISH_DURATION_S", "0.2")

    backend = _make_backend()
    recorded = _run_wake_up(backend, magic_distance=100.0)

    main_call, flourish_left, flourish_back = recorded["goto_calls"]
    # Legacy formula: magic_distance * 20 / 1000 = 2.0; no floor.
    assert main_call["duration"] == pytest.approx(2.0)
    assert flourish_left["duration"] == pytest.approx(0.2)
    assert flourish_back["duration"] == pytest.approx(0.2)


def test_invalid_env_values_fall_back_to_defaults(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage / negative env values must not crash wake_up()."""
    monkeypatch.setenv("REACHY_WAKE_UP_MIN_DURATION_S", "not-a-number")
    monkeypatch.setenv("REACHY_WAKE_UP_DURATION_SCALE", "-1.5")
    monkeypatch.setenv("REACHY_WAKE_UP_FLOURISH_DURATION_S", "")

    backend = _make_backend()
    recorded = _run_wake_up(backend, magic_distance=100.0)

    main_call, flourish_left, flourish_back = recorded["goto_calls"]
    # All three fell back to defaults: 3.0 floor wins, flourish=0.4.
    assert main_call["duration"] == pytest.approx(3.0)
    assert flourish_left["duration"] == pytest.approx(0.4)
    assert flourish_back["duration"] == pytest.approx(0.4)


def test_read_float_env_empty_string_uses_default(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty string is treated as "unset", not as a parse error."""
    monkeypatch.setenv("REACHY_WAKE_UP_MIN_DURATION_S", "")
    value = abstract_module._read_float_env(
        "REACHY_WAKE_UP_MIN_DURATION_S",
        abstract_module._WAKE_UP_MIN_DURATION_DEFAULT,
    )
    assert value == pytest.approx(abstract_module._WAKE_UP_MIN_DURATION_DEFAULT)


def test_read_float_env_negative_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative values are rejected and the default is returned."""
    monkeypatch.setenv("REACHY_WAKE_UP_DURATION_SCALE", "-0.5")
    value = abstract_module._read_float_env(
        "REACHY_WAKE_UP_DURATION_SCALE",
        abstract_module._WAKE_UP_DURATION_SCALE_DEFAULT,
    )
    assert value == pytest.approx(abstract_module._WAKE_UP_DURATION_SCALE_DEFAULT)


def test_read_float_env_accepts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is a valid value (used by the legacy-restore recipe)."""
    monkeypatch.setenv("REACHY_WAKE_UP_MIN_DURATION_S", "0")
    value = abstract_module._read_float_env(
        "REACHY_WAKE_UP_MIN_DURATION_S",
        abstract_module._WAKE_UP_MIN_DURATION_DEFAULT,
    )
    assert value == pytest.approx(0.0)
