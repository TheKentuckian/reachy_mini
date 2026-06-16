"""Tests for MediaBackend enum and backward compatibility."""

import warnings

import pytest

from reachy_mini.media.media_manager import MediaBackend, _resolve_backend


class TestMediaBackend:
    """Test MediaBackend enum backward compatibility."""

    def test_default_alias(self) -> None:
        """DEFAULT points to LOCAL."""
        assert MediaBackend.DEFAULT == MediaBackend.LOCAL

    def test_default_no_video_alias(self) -> None:
        """DEFAULT_NO_VIDEO points to GSTREAMER_NO_VIDEO."""
        assert MediaBackend.DEFAULT_NO_VIDEO == MediaBackend.GSTREAMER_NO_VIDEO

    @pytest.mark.parametrize(
        "deprecated, expected",
        [
            (MediaBackend.GSTREAMER, MediaBackend.LOCAL),
            (MediaBackend.GSTREAMER_NO_VIDEO, MediaBackend.LOCAL),
            (MediaBackend.SOUNDDEVICE_NO_VIDEO, MediaBackend.LOCAL),
            (MediaBackend.SOUNDDEVICE_OPENCV, MediaBackend.LOCAL),
        ],
    )
    def test_deprecated_backend_resolves_with_warning(
        self, deprecated: MediaBackend, expected: MediaBackend
    ) -> None:
        """Deprecated backends resolve to their replacement and emit FutureWarning."""
        with pytest.warns(FutureWarning):
            resolved = _resolve_backend(deprecated)
        assert resolved == expected

    @pytest.mark.parametrize(
        "backend",
        [
            MediaBackend.LOCAL,
            MediaBackend.AUDIO_ONLY,
            MediaBackend.WEBRTC,
            MediaBackend.NO_MEDIA,
        ],
    )
    def test_current_backends_no_warning(self, backend: MediaBackend) -> None:
        """Current backends pass through _resolve_backend unchanged, no warning."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            resolved = _resolve_backend(backend)
        assert resolved == backend

    def test_audio_only_string_resolves(self) -> None:
        """The "audio_only" string maps to MediaBackend.AUDIO_ONLY."""
        assert MediaBackend("audio_only") == MediaBackend.AUDIO_ONLY

    def test_invalid_backend_raises(self) -> None:
        """Unknown backend string raises ValueError."""
        with pytest.raises(ValueError):
            MediaBackend("invalid")


class TestAudioOnlyBackend:
    """AUDIO_ONLY initializes audio but skips the camera GStreamer pipeline."""

    def test_audio_only_inits_audio_not_camera(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AUDIO_ONLY calls _init_audio and NOT _init_camera (skips the camera)."""
        from reachy_mini.media.media_manager import MediaManager

        calls: list[str] = []
        monkeypatch.setattr(
            MediaManager, "_init_camera", lambda self, *a, **k: calls.append("camera")
        )
        monkeypatch.setattr(
            MediaManager, "_init_audio", lambda self, *a, **k: calls.append("audio")
        )

        mgr = MediaManager(backend=MediaBackend.AUDIO_ONLY)

        assert calls == ["audio"]
        assert mgr.camera is None  # camera never initialized

    def test_local_inits_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity: LOCAL still initializes both camera and audio (unchanged)."""
        from reachy_mini.media.media_manager import MediaManager

        calls: list[str] = []
        monkeypatch.setattr(
            MediaManager, "_init_camera", lambda self, *a, **k: calls.append("camera")
        )
        monkeypatch.setattr(
            MediaManager, "_init_audio", lambda self, *a, **k: calls.append("audio")
        )

        MediaManager(backend=MediaBackend.LOCAL)

        assert calls == ["camera", "audio"]
