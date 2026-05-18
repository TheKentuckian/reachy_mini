"""Unit tests for the WebRTC consumer-gate state machine in GstMediaServer.

Strategy
--------
We test the three new methods directly without constructing a real GStreamer
pipeline:

* ``_install_webrtc_block_probe`` — installs a blocking probe on the pad
* ``_remove_webrtc_block_probe``  — removes the probe
* ``_consumer_added`` / ``_consumer_removed`` counter logic (exercised via
  the two helpers above, not the full signal-handler methods which call
  Gst.debug_bin_to_dot_file and webrtcbin methods that need a live pipeline)

Hardware-level verification (encoder truly idles when no consumer is
connected) must be confirmed on a Raspberry Pi 5 with the imx708 camera
and a WebRTC client; that path cannot be covered without physical hardware.
"""

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Minimal stand-in for a GstMediaServer with just the gate attributes.
# We call the methods directly so we don't need __init__ at all.
# ---------------------------------------------------------------------------


class _FakeServer:
    """Minimal stand-in holding only the attributes touched by the gate methods."""

    def __init__(self) -> None:
        import logging

        self._logger = logging.getLogger("test_webrtc_consumer_gate")
        self._active_consumers = 0
        self._webrtc_branch_pad: object = None
        self._webrtc_block_probe_id: object = None


def _make_mock_pad() -> MagicMock:
    """Return a mock Gst.Pad with a working probe counter."""
    pad = MagicMock()
    pad._next_probe_id = 1
    pad._active_probes: dict[int, object] = {}

    def _add_probe(probe_type: object, callback: object, user_data: object) -> int:
        pid = pad._next_probe_id
        pad._next_probe_id += 1
        pad._active_probes[pid] = callback
        return pid

    def _remove_probe(pid: int) -> None:
        pad._active_probes.pop(pid, None)

    pad.add_probe.side_effect = _add_probe
    pad.remove_probe.side_effect = _remove_probe
    return pad


# ---------------------------------------------------------------------------
# Import the two helper methods so we can call them on _FakeServer instances
# without triggering __init__.
# ---------------------------------------------------------------------------


from reachy_mini.media.media_server import GstMediaServer  # noqa: E402

_install = GstMediaServer._install_webrtc_block_probe
_remove = GstMediaServer._remove_webrtc_block_probe


# ---------------------------------------------------------------------------
# Helpers that replicate only the consumer-gate logic (counter + probe calls)
# from _consumer_added / _consumer_removed, without the GStreamer pipeline work.
# ---------------------------------------------------------------------------


def _gate_consumer_added(server: _FakeServer) -> None:
    server._active_consumers += 1
    if server._active_consumers == 1:
        _remove(server)  # type: ignore[arg-type]


def _gate_consumer_removed(server: _FakeServer) -> None:
    if server._active_consumers > 0:
        server._active_consumers -= 1
    if server._active_consumers == 0:
        _install(server)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: probe install / remove helpers
# ---------------------------------------------------------------------------


class TestPadProbeHelpers:
    """_install_webrtc_block_probe and _remove_webrtc_block_probe behave correctly."""

    def setup_method(self) -> None:
        self.server = _FakeServer()
        self.pad = _make_mock_pad()
        self.server._webrtc_branch_pad = self.pad

    def test_install_sets_probe_id(self) -> None:
        _install(self.server)  # type: ignore[arg-type]
        assert self.server._webrtc_block_probe_id is not None
        self.pad.add_probe.assert_called_once()

    def test_install_is_idempotent(self) -> None:
        """A second call must not stack another probe."""
        _install(self.server)  # type: ignore[arg-type]
        first_id = self.server._webrtc_block_probe_id
        _install(self.server)  # type: ignore[arg-type]
        assert self.server._webrtc_block_probe_id == first_id
        assert self.pad.add_probe.call_count == 1

    def test_remove_clears_probe_id(self) -> None:
        _install(self.server)  # type: ignore[arg-type]
        _remove(self.server)  # type: ignore[arg-type]
        assert self.server._webrtc_block_probe_id is None
        self.pad.remove_probe.assert_called_once()

    def test_remove_is_noop_when_not_installed(self) -> None:
        _remove(self.server)  # type: ignore[arg-type]
        self.pad.remove_probe.assert_not_called()

    def test_probe_removed_from_pad_dict(self) -> None:
        _install(self.server)  # type: ignore[arg-type]
        pid = self.server._webrtc_block_probe_id
        assert pid in self.pad._active_probes
        _remove(self.server)  # type: ignore[arg-type]
        assert pid not in self.pad._active_probes

    def test_no_probe_when_pad_is_none(self) -> None:
        """Must be a no-op when no pad has been saved yet."""
        self.server._webrtc_branch_pad = None
        _install(self.server)  # type: ignore[arg-type]
        assert self.server._webrtc_block_probe_id is None


# ---------------------------------------------------------------------------
# Tests: consumer counter state machine
# ---------------------------------------------------------------------------


class TestConsumerCounter:
    """Counter increments / decrements correctly with no underflow."""

    def setup_method(self) -> None:
        self.server = _FakeServer()
        self.server._webrtc_branch_pad = _make_mock_pad()
        # Simulate start(): block probe already in place.
        _install(self.server)  # type: ignore[arg-type]

    def test_initial_count_is_zero(self) -> None:
        assert self.server._active_consumers == 0

    def test_add_increments_to_one(self) -> None:
        _gate_consumer_added(self.server)
        assert self.server._active_consumers == 1

    def test_add_twice_increments_to_two(self) -> None:
        _gate_consumer_added(self.server)
        _gate_consumer_added(self.server)
        assert self.server._active_consumers == 2

    def test_remove_decrements(self) -> None:
        _gate_consumer_added(self.server)
        _gate_consumer_added(self.server)
        _gate_consumer_removed(self.server)
        assert self.server._active_consumers == 1

    def test_remove_to_zero(self) -> None:
        _gate_consumer_added(self.server)
        _gate_consumer_removed(self.server)
        assert self.server._active_consumers == 0

    def test_no_underflow_on_spurious_remove(self) -> None:
        """Extra remove when already at zero must not go negative."""
        assert self.server._active_consumers == 0
        _gate_consumer_removed(self.server)
        assert self.server._active_consumers == 0


# ---------------------------------------------------------------------------
# Tests: probe transitions at the right consumer-count boundaries
# ---------------------------------------------------------------------------


class TestGatingTransitions:
    """Probe is installed / removed at exactly the right 0↔1 boundaries."""

    def setup_method(self) -> None:
        self.server = _FakeServer()
        self.pad = _make_mock_pad()
        self.server._webrtc_branch_pad = self.pad
        # Simulate start(): install blocking probe with no consumers.
        _install(self.server)  # type: ignore[arg-type]

    def test_probe_installed_at_start(self) -> None:
        assert self.server._webrtc_block_probe_id is not None

    def test_first_consumer_removes_probe(self) -> None:
        _gate_consumer_added(self.server)
        assert self.server._webrtc_block_probe_id is None

    def test_second_consumer_does_not_reinstall_probe(self) -> None:
        _gate_consumer_added(self.server)
        _gate_consumer_added(self.server)
        assert self.server._webrtc_block_probe_id is None
        # add_probe once (start), remove_probe once (first consumer)
        assert self.pad.add_probe.call_count == 1
        assert self.pad.remove_probe.call_count == 1

    def test_last_consumer_removed_reinstalls_probe(self) -> None:
        _gate_consumer_added(self.server)
        _gate_consumer_removed(self.server)
        assert self.server._webrtc_block_probe_id is not None

    def test_middle_consumer_remove_does_not_reinstall_probe(self) -> None:
        """2→1 transition must NOT re-block the branch."""
        _gate_consumer_added(self.server)
        _gate_consumer_added(self.server)
        _gate_consumer_removed(self.server)  # 2→1
        assert self.server._webrtc_block_probe_id is None

    def test_full_cycle_add_add_remove_remove(self) -> None:
        """0→1→2→1→0: unblocked at 1, reblocked at 0."""
        _gate_consumer_added(self.server)   # 0→1: unblock
        _gate_consumer_added(self.server)   # 1→2: no change
        _gate_consumer_removed(self.server)  # 2→1: no change
        _gate_consumer_removed(self.server)  # 1→0: block
        assert self.server._webrtc_block_probe_id is not None
        assert self.server._active_consumers == 0

    def test_spurious_remove_at_zero_does_not_block_twice(self) -> None:
        """A spurious remove when already at 0 must not call add_probe again."""
        # Start state: probe already installed once (from setup_method).
        initial_add_count = self.pad.add_probe.call_count  # == 1
        _gate_consumer_removed(self.server)  # already at 0: no underflow
        # No new probe should be stacked (idempotent install).
        assert self.pad.add_probe.call_count == initial_add_count
