"""Unit tests for /api/media/ipc-stats endpoint and GstMediaServer frame probe.

Strategy
--------
All tests run without a real GStreamer pipeline.  We use two techniques:

1. ``_FakeMediaServer`` — a minimal stand-in that carries only the attributes
   and methods touched by the probe logic and ``get_ipc_stats()``.  We call
   those methods directly (same pattern as ``test_webrtc_consumer_gate.py``).

2. FastAPI ``TestClient`` with the media router mounted against a minimal
   ``FastAPI`` app that overrides the ``get_daemon`` dependency to return a
   mock directly, bypassing the ``isinstance`` assertion in the real
   dependency.

Hardware-level verification (probe really increments on live camera frames)
must be confirmed manually on ricci; it is not covered here.
"""

from __future__ import annotations

import threading
import time
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reachy_mini.daemon.app.dependencies import get_daemon
from reachy_mini.daemon.app.routers.media import router

# ---------------------------------------------------------------------------
# Minimal stand-in for GstMediaServer (probe + stats only)
# ---------------------------------------------------------------------------


class _FakeMediaServer:
    """Carry only the attributes used by the IPC stats probe and getter."""

    def __init__(self) -> None:
        import logging

        self._logger = logging.getLogger("test_ipc_stats")
        self._ipc_stats_lock: threading.Lock = threading.Lock()
        self._ipc_frames_published: int = 0
        self._ipc_last_buffer_time: Optional[float] = None
        self._pipeline_sender: object = None

    # Borrow the real implementations from the production class.
    from reachy_mini.media.media_server import GstMediaServer  # noqa: PLC0415

    get_ipc_stats = GstMediaServer.get_ipc_stats
    _install_ipc_frame_probe = GstMediaServer._install_ipc_frame_probe

    @property
    def socket_path(self) -> str:
        """Return a stable test path regardless of platform."""
        return "/tmp/reachymini_camera_socket"


# ---------------------------------------------------------------------------
# Helpers for the route tests
# ---------------------------------------------------------------------------


def _make_daemon_with_server(server: object) -> MagicMock:
    """Return a Daemon mock whose _media_server is *server*."""
    daemon = MagicMock()
    daemon._media_server = server
    return daemon


def _make_app(daemon: MagicMock) -> TestClient:
    """Mount the media router on a bare FastAPI app.

    The ``get_daemon`` dependency is overridden to return *daemon* directly,
    bypassing the ``isinstance(…, Daemon)`` assertion in the real dependency.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_daemon] = lambda: daemon
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests: get_ipc_stats() on the fake server
# ---------------------------------------------------------------------------


class TestGetIpcStats:
    """Unit tests for GstMediaServer.get_ipc_stats() (no GStreamer required)."""

    def test_initial_state(self) -> None:
        """Before any buffers: frames=0, last_time=None, state=unknown."""
        srv = _FakeMediaServer()
        frames, last_time, state = srv.get_ipc_stats()

        assert frames == 0
        assert last_time is None
        assert state == "unknown"

    def test_after_manual_increment(self) -> None:
        """Simulating what the probe does: counter and timestamp are reflected."""
        srv = _FakeMediaServer()
        now = time.monotonic()

        with srv._ipc_stats_lock:
            srv._ipc_frames_published = 42
            srv._ipc_last_buffer_time = now

        frames, last_time, state = srv.get_ipc_stats()

        assert frames == 42
        assert last_time == now
        assert state == "unknown"

    def test_pipeline_state_forwarded(self) -> None:
        """When a pipeline object is present its state name is returned."""
        srv = _FakeMediaServer()

        mock_pipeline = MagicMock()
        # Simulate Gst.StateChangeReturn.SUCCESS, Gst.State.PLAYING, pending=NULL
        mock_state = MagicMock()
        mock_state.value_name = "GST_STATE_PLAYING"
        mock_pipeline.get_state.return_value = (MagicMock(), mock_state, MagicMock())
        srv._pipeline_sender = mock_pipeline

        _, _, state = srv.get_ipc_stats()
        assert state == "GST_STATE_PLAYING"

    def test_pipeline_state_exception_falls_back_to_unknown(self) -> None:
        """A broken pipeline.get_state() does not crash get_ipc_stats()."""
        srv = _FakeMediaServer()
        mock_pipeline = MagicMock()
        mock_pipeline.get_state.side_effect = RuntimeError("oops")
        srv._pipeline_sender = mock_pipeline

        _, _, state = srv.get_ipc_stats()
        assert state == "unknown"


# ---------------------------------------------------------------------------
# Tests: pad probe handler (mock Gst.Pad)
# ---------------------------------------------------------------------------


def _make_mock_pad() -> MagicMock:
    """Return a mock Gst.Pad whose add_probe() fires the callback inline."""
    pad = MagicMock()
    pad._next_probe_id = 1
    pad._active_probes: dict = {}

    def _add_probe(probe_type: object, callback: object, user_data: object) -> int:
        pid = pad._next_probe_id
        pad._next_probe_id += 1
        pad._active_probes[pid] = callback
        return pid

    pad.add_probe.side_effect = _add_probe
    return pad


class _ExplodingLock:
    """A threading.Lock stand-in whose __enter__ always raises RuntimeError.

    Used to verify the probe's defensive try/except without depending on
    patch.object() being able to monkey-patch C-extension lock attributes
    (which CPython 3.13 disallows).
    """

    def __enter__(self) -> "_ExplodingLock":
        """Raise unconditionally to simulate a runtime error in the probe."""
        raise RuntimeError("simulated lock failure")

    def __exit__(self, *args: object) -> bool:
        """No-op exit."""
        return False


class TestIpcFrameProbe:
    """Test _install_ipc_frame_probe installs a working counter probe."""

    def test_probe_increments_counter(self) -> None:
        """Calling the probe callback increments frames_published."""
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init([])

        srv = _FakeMediaServer()
        pad = _make_mock_pad()

        mock_sink = MagicMock()
        mock_sink.get_static_pad.return_value = pad

        srv._install_ipc_frame_probe(mock_sink)

        # The probe was registered
        assert len(pad._active_probes) == 1
        probe_fn = list(pad._active_probes.values())[0]

        # Fire the probe three times (simulates three frames)
        for _ in range(3):
            result = probe_fn(pad, MagicMock(), None)
            assert result == Gst.PadProbeReturn.OK  # must pass buffer through

        assert srv._ipc_frames_published == 3
        assert srv._ipc_last_buffer_time is not None

    def test_probe_exception_does_not_raise(self) -> None:
        """A crash inside the counter logic must not propagate out of the probe."""
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init([])

        srv = _FakeMediaServer()
        # Replace the lock with one that always throws on acquire.
        srv._ipc_stats_lock = _ExplodingLock()  # type: ignore[assignment]

        pad = _make_mock_pad()
        mock_sink = MagicMock()
        mock_sink.get_static_pad.return_value = pad

        srv._install_ipc_frame_probe(mock_sink)
        probe_fn = list(pad._active_probes.values())[0]

        # The probe must return OK even when the lock explodes.
        result = probe_fn(pad, MagicMock(), None)
        assert result == Gst.PadProbeReturn.OK

    def test_missing_sink_pad_logs_warning(self) -> None:
        """If get_static_pad('sink') returns None, a warning is logged and no crash."""
        srv = _FakeMediaServer()
        mock_sink = MagicMock()
        mock_sink.get_static_pad.return_value = None

        # Should not raise
        srv._install_ipc_frame_probe(mock_sink)

        # No probe was registered (nothing to register against)
        assert srv._ipc_frames_published == 0


# ---------------------------------------------------------------------------
# Tests: /api/media/ipc-stats route
# ---------------------------------------------------------------------------


class TestIpcStatsRoute:
    """HTTP-level tests for GET /api/media/ipc-stats."""

    def test_returns_200_with_expected_shape(self) -> None:
        """When media_server is present the route returns 200 with all fields."""
        srv = _FakeMediaServer()
        now = time.monotonic()
        with srv._ipc_stats_lock:
            srv._ipc_frames_published = 100
            srv._ipc_last_buffer_time = now - 0.05  # 50 ms ago

        daemon = _make_daemon_with_server(srv)
        client = _make_app(daemon)

        resp = client.get("/api/media/ipc-stats")
        assert resp.status_code == 200

        body = resp.json()
        assert body["frames_published"] == 100
        assert body["last_frame_age_s"] is not None
        assert 0.0 <= body["last_frame_age_s"] < 2.0  # sanity bound
        assert body["publisher_state"] == "unknown"
        assert body["socket_path"] == "/tmp/reachymini_camera_socket"

    def test_returns_200_with_null_age_when_no_frames(self) -> None:
        """When no frame has been observed last_frame_age_s is null."""
        srv = _FakeMediaServer()  # frames=0, last_time=None

        daemon = _make_daemon_with_server(srv)
        client = _make_app(daemon)

        resp = client.get("/api/media/ipc-stats")
        assert resp.status_code == 200

        body = resp.json()
        assert body["frames_published"] == 0
        assert body["last_frame_age_s"] is None

    def test_returns_404_when_media_server_is_none(self) -> None:
        """When daemon._media_server is None the route returns 404."""
        daemon = _make_daemon_with_server(None)
        client = _make_app(daemon)

        resp = client.get("/api/media/ipc-stats")
        assert resp.status_code == 404
        assert "not initialised" in resp.json()["detail"]
