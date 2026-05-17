"""Tests for Reachy Mini audio control helpers."""

from unittest.mock import MagicMock, patch

import pytest

from reachy_mini.media.audio_control_utils import PARAMETERS, find, init_respeaker_usb
from reachy_mini.media.media_manager import MediaBackend, MediaManager

AUDIO_CONFIG_PARAMETER_NAMES = ("PP_MIN_NS", "PP_NLATTENONOFF", "PP_MGSCALE")

# ---------------------------------------------------------------------------
# Unit tests for find() — no hardware required
# ---------------------------------------------------------------------------

_POLLEN_VID = 0x38FB
_POLLEN_PID = 0x1001
_XMOS_VID = 0x2886
_XMOS_PID = 0x001A


def _mock_dev() -> MagicMock:
    """Return a minimal usb.core.Device stand-in."""
    return MagicMock(name="usb_device")


def test_find_no_args_tries_pollen_ids_first() -> None:
    """find() with no arguments must probe Pollen IDs (0x38FB:0x1001) first."""
    fake_dev = _mock_dev()
    call_log: list[tuple[int, int]] = []

    def fake_usb_find(
        idVendor: int, idProduct: int, backend: object = None
    ) -> MagicMock | None:
        call_log.append((idVendor, idProduct))
        if idVendor == _POLLEN_VID and idProduct == _POLLEN_PID:
            return fake_dev
        return None

    with patch("reachy_mini.media.audio_control_utils.usb.core.find", fake_usb_find):
        result = find()

    assert call_log[0] == (_POLLEN_VID, _POLLEN_PID), "Pollen IDs must be probed first"
    assert result is not None


def test_find_no_args_falls_back_to_xmos_when_pollen_absent() -> None:
    """find() must fall back to XMOS IDs when the Pollen device is not present."""
    fake_dev = _mock_dev()

    def fake_usb_find(
        idVendor: int, idProduct: int, backend: object = None
    ) -> MagicMock | None:
        if idVendor == _XMOS_VID and idProduct == _XMOS_PID:
            return fake_dev
        return None

    with patch("reachy_mini.media.audio_control_utils.usb.core.find", fake_usb_find):
        result = find()

    assert result is not None


def test_find_no_args_returns_none_when_no_device_present() -> None:
    """find() must return None when neither VID/PID pair is found."""
    with patch(
        "reachy_mini.media.audio_control_utils.usb.core.find", return_value=None
    ):
        result = find()

    assert result is None


def test_find_explicit_override_uses_only_given_ids() -> None:
    """find(vid=..., pid=...) must try exactly the provided IDs, not the probe list."""
    call_log: list[tuple[int, int]] = []

    def fake_usb_find(
        idVendor: int, idProduct: int, backend: object = None
    ) -> MagicMock | None:
        call_log.append((idVendor, idProduct))
        if idVendor == _XMOS_VID and idProduct == _XMOS_PID:
            return _mock_dev()
        return None

    with patch("reachy_mini.media.audio_control_utils.usb.core.find", fake_usb_find):
        result = find(vid=_XMOS_VID, pid=_XMOS_PID)

    assert result is not None
    assert call_log == [(_XMOS_VID, _XMOS_PID)], (
        "Only the requested IDs should be tried"
    )


@pytest.mark.audio
def test_respeaker_read_values_reads_board_parameters() -> None:
    """Numeric readback should be normalized from the real audio board."""
    respeaker = init_respeaker_usb()
    assert respeaker is not None, "Reachy Mini Audio board is required."
    try:
        for name in AUDIO_CONFIG_PARAMETER_NAMES:
            values = respeaker.read_values(name)
            assert values is not None
            assert len(values) == PARAMETERS[name][2]
            assert all(isinstance(value, (float, int)) for value in values)
    finally:
        respeaker.close()


@pytest.mark.audio
def test_respeaker_apply_audio_config_writes_current_board_values() -> None:
    """Custom config writes should be verified against real board readback."""
    respeaker = init_respeaker_usb()
    assert respeaker is not None, "Reachy Mini Audio board is required."
    try:
        config = []
        for name in AUDIO_CONFIG_PARAMETER_NAMES:
            values = respeaker.read_values(name)
            assert values is not None
            config.append((name, values))

        assert respeaker.apply_audio_config(tuple(config))
        for name, expected_values in config:
            assert respeaker.read_values(name) == pytest.approx(expected_values)
    finally:
        respeaker.close()


@pytest.mark.audio
def test_respeaker_apply_audio_config_changes_value_and_restores_it() -> None:
    """Custom config writes should change a real value and restore it."""
    parameter_name = "PP_NLATTENONOFF"
    respeaker = init_respeaker_usb()
    assert respeaker is not None, "Reachy Mini Audio board is required."
    original_values = None
    try:
        original_values = respeaker.read_values(parameter_name)
        assert original_values is not None
        original_value = int(original_values[0])
        changed_value = 0 if original_value else 1

        assert respeaker.apply_audio_config(((parameter_name, (changed_value,)),))
        assert respeaker.read_values(parameter_name) == (changed_value,)
    finally:
        if original_values is not None:
            assert respeaker.apply_audio_config(((parameter_name, original_values),))
            assert respeaker.read_values(parameter_name) == original_values
        respeaker.close()


@pytest.mark.audio
def test_media_audio_apply_audio_config_uses_real_board() -> None:
    """Media audio should apply caller-provided config through the real board."""
    respeaker = init_respeaker_usb()
    assert respeaker is not None, "Reachy Mini Audio board is required."
    try:
        values = respeaker.read_values("PP_MIN_NS")
        assert values is not None
    finally:
        respeaker.close()

    media = MediaManager(backend=MediaBackend.LOCAL)
    try:
        assert media.audio.apply_audio_config((("PP_MIN_NS", values),))
    finally:
        media.close()
