"""Tests for daemon instrumentation helpers."""

from __future__ import annotations

import json
import logging
import sys
import types
from collections.abc import Generator
from pathlib import Path

import pytest

import reachy_mini.daemon.instrumentation as _instr_mod
from reachy_mini.daemon.backend.mockup_sim.backend import MockupSimBackend
from reachy_mini.daemon.daemon import Daemon
from reachy_mini.daemon.instrumentation import (
    INSTRUMENT_ENV_VAR,
    InstrumentMode,
    configure_daemon_logging,
    get_instrument_mode,
    log_event,
    timing_event,
)
from reachy_mini.io.protocol import MotorControlMode, SetMotorModeCmd


@pytest.fixture()
def restore_root_logging() -> Generator[None, None, None]:
    """Restore root logger handlers and cached mode after instrumentation tests."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers.clear()
    root.handlers.extend(handlers)
    root.setLevel(level)
    _instr_mod._active_mode = None


def test_get_instrument_mode_defaults_to_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default instrumentation mode is basic."""
    monkeypatch.delenv(INSTRUMENT_ENV_VAR, raising=False)

    assert get_instrument_mode() is InstrumentMode.BASIC


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", InstrumentMode.OFF),
        ("basic", InstrumentMode.BASIC),
        ("trace", InstrumentMode.TRACE),
        ("remote", InstrumentMode.REMOTE),
        (" TRACE ", InstrumentMode.TRACE),
    ],
)
def test_get_instrument_mode_parses_known_modes(raw: str, expected: InstrumentMode) -> None:
    """Known instrumentation mode values are normalized."""
    assert get_instrument_mode(raw) is expected


def test_get_instrument_mode_falls_back_to_basic() -> None:
    """Unknown instrumentation modes fall back to basic."""
    assert get_instrument_mode("surprise") is InstrumentMode.BASIC


def test_configure_daemon_logging_writes_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Structured modes write JSONL log files."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"

    mode = configure_daemon_logging("INFO", str(log_file))
    logging.getLogger("reachy_mini.test").info(
        "hello daemon",
        extra={
            "event": "test.event",
            "attrs": {"answer": 42},
        },
    )

    assert mode is InstrumentMode.BASIC
    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    payload = json.loads(lines[-1])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "reachy_mini.test"
    assert payload["event"] == "test.event"
    assert payload["message"] == "hello daemon"
    assert payload["attrs"] == {"answer": 42}


def test_configure_daemon_logging_off_raises_level_to_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Off mode suppresses info logs and keeps plain warning output."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "off")
    log_file = tmp_path / "daemon.log"

    mode = configure_daemon_logging("INFO", str(log_file))
    logging.getLogger("reachy_mini.test").info("hidden")
    logging.getLogger("reachy_mini.test").warning("visible")

    assert mode is InstrumentMode.OFF
    assert log_file.read_text().splitlines() == [
        "reachy_mini.test - WARNING - visible",
    ]


def test_trace_mode_writes_timing_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Trace mode writes span-style timing events."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "trace")
    log_file = tmp_path / "daemon.jsonl"

    configure_daemon_logging("INFO", str(log_file))
    with timing_event("daemon.test", stage="unit-test"):
        pass

    payload = json.loads(log_file.read_text().splitlines()[-1])
    assert payload["event"] == "span.end"
    assert payload["attrs"]["name"] == "daemon.test"
    assert payload["attrs"]["stage"] == "unit-test"
    assert payload["attrs"]["duration_ms"] >= 0
    assert payload["attrs"]["outcome"] == "ok"


def test_log_event_writes_basic_structured_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Basic mode writes explicit lifecycle events."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"

    configure_daemon_logging("INFO", str(log_file))
    log_event("daemon.test.event", phase="unit-test")

    payload = json.loads(log_file.read_text().splitlines()[-1])
    assert payload["event"] == "daemon.test.event"
    assert payload["attrs"] == {"phase": "unit-test"}


def test_process_command_motor_mode_writes_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """WebRTC/WS motor-mode commands emit instrumentation events."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"

    configure_daemon_logging("INFO", str(log_file))
    backend = MockupSimBackend(use_audio=False)
    responses: list[dict[str, str]] = []

    backend.process_command(
        SetMotorModeCmd(mode=MotorControlMode.Disabled.value),
        send_response=responses.append,
    )

    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert responses == [{"motor_mode": "disabled", "status": "ok"}]
    assert any(
        event["event"] == "daemon.motor.mode.set.complete"
        and event["attrs"]["source"] == "command"
        and event["attrs"]["mode"] == "disabled"
        for event in events
    )


def test_mockup_backend_setup_writes_trace_span(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Mockup backend setup emits a startup trace span."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "trace")
    log_file = tmp_path / "daemon.jsonl"

    configure_daemon_logging("INFO", str(log_file))
    daemon = Daemon(no_media=True)
    backend = daemon._setup_backend(
        wireless_version=False,
        sim=False,
        mockup_sim=True,
        serialport="auto",
        scene="empty",
        check_collision=False,
        kinematics_engine="AnalyticalKinematics",
        headless=True,
        use_audio=False,
    )
    backend.close()

    spans = [
        json.loads(line)
        for line in log_file.read_text().splitlines()
        if json.loads(line).get("event") == "span.end"
    ]
    assert any(
        span["attrs"]["name"] == "daemon.backend.construct"
        and span["attrs"]["backend_mode"] == "mockup"
        for span in spans
    )


@pytest.mark.asyncio
async def test_mockup_daemon_start_writes_backend_ready_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Daemon start records whether the backend became ready."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"

    configure_daemon_logging("INFO", str(log_file))
    daemon = Daemon(no_media=True)
    try:
        await daemon.start(
            mockup_sim=True,
            wake_up_on_start=False,
            use_audio=False,
        )
    finally:
        await daemon.stop(goto_sleep_on_stop=False)

    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert any(
        event["event"] == "daemon.backend.ready"
        and event["attrs"]["backend_mode"] == "mockup"
        and event["attrs"]["ready"] is True
        for event in events
    )


# ---------------------------------------------------------------------------
# Issue #4 — media server lifecycle instrumentation
# ---------------------------------------------------------------------------


class _FakeMediaServer:
    """Minimal media server double for instrumentation tests."""

    camera_specs = types.SimpleNamespace(name="fake-camera")

    def __init__(self, *args: object, **kwargs: object) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class _FailingMediaServer:
    """Media server double that fails during construction."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("camera boom")


def _install_fake_media_server_module(
    monkeypatch: pytest.MonkeyPatch,
    media_server_type: type,
) -> None:
    module = types.ModuleType("reachy_mini.media.media_server")
    module.GstMediaServer = media_server_type
    monkeypatch.setitem(sys.modules, "reachy_mini.media.media_server", module)


def _read_jsonl(log_file: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_file.read_text().splitlines()]


def _daemon_with_fake_media() -> Daemon:
    daemon = Daemon(no_media=True)
    daemon._media_server = _FakeMediaServer()
    daemon._media_released = False
    daemon._status.media_released = False
    return daemon


@pytest.mark.asyncio
async def test_release_media_emits_lifecycle_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """release_media emits start and complete structured events."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = _daemon_with_fake_media()
    await daemon.release_media()

    events = _read_jsonl(log_file)
    event_names = {e["event"] for e in events}
    assert "daemon.media.release.start" in event_names
    assert any(
        e["event"] == "daemon.media.release.complete"
        and e["attrs"]["media_released"] is True
        for e in events
    )


@pytest.mark.asyncio
async def test_release_media_skip_when_already_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """release_media emits a skip event when already released."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = _daemon_with_fake_media()
    daemon._media_released = True
    await daemon.release_media()

    events = _read_jsonl(log_file)
    assert any(
        e["event"] == "daemon.media.release.skip" and e["attrs"]["already_released"] is True
        for e in events
    )


@pytest.mark.asyncio
async def test_release_media_skip_when_no_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """release_media emits a skip event when no media server is present."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = Daemon(no_media=True)
    await daemon.release_media()

    events = _read_jsonl(log_file)
    assert any(
        e["event"] == "daemon.media.release.skip" and e["attrs"]["no_server"] is True
        for e in events
    )


@pytest.mark.asyncio
async def test_acquire_media_emits_lifecycle_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """acquire_media emits start and complete structured events."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = _daemon_with_fake_media()
    daemon._media_released = True
    daemon._status.media_released = True
    await daemon.acquire_media()

    events = _read_jsonl(log_file)
    event_names = {e["event"] for e in events}
    assert "daemon.media.acquire.start" in event_names
    assert any(
        e["event"] == "daemon.media.acquire.complete"
        and e["attrs"]["media_released"] is False
        for e in events
    )


@pytest.mark.asyncio
async def test_acquire_media_skip_when_not_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """acquire_media emits a skip event when media has not been released."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = _daemon_with_fake_media()
    await daemon.acquire_media()

    events = _read_jsonl(log_file)
    assert any(
        e["event"] == "daemon.media.acquire.skip" and e["attrs"]["not_released"] is True
        for e in events
    )


@pytest.mark.asyncio
async def test_acquire_media_skip_when_no_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """acquire_media emits a skip event when no media server is present."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = Daemon(no_media=True)
    daemon._media_released = True
    daemon._status.media_released = True
    await daemon.acquire_media()

    events = _read_jsonl(log_file)
    assert any(
        e["event"] == "daemon.media.acquire.skip" and e["attrs"]["no_server"] is True
        for e in events
    )


@pytest.mark.asyncio
async def test_release_media_writes_trace_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Trace mode records the media stop span during release_media."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "trace")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = _daemon_with_fake_media()
    await daemon.release_media()

    spans = [e for e in _read_jsonl(log_file) if e.get("event") == "span.end"]
    assert any(
        s["attrs"]["name"] == "daemon.media.stop"
        and s["attrs"]["source"] == "release_media"
        and s["attrs"]["duration_ms"] >= 0
        for s in spans
    )


@pytest.mark.asyncio
async def test_acquire_media_writes_trace_spans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Trace mode records media and relay start spans during acquire_media."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "trace")
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = _daemon_with_fake_media()
    daemon._media_released = True
    daemon._status.media_released = True
    await daemon.acquire_media()

    spans = [e for e in _read_jsonl(log_file) if e.get("event") == "span.end"]
    assert any(
        s["attrs"]["name"] == "daemon.media.start"
        and s["attrs"]["source"] == "acquire_media"
        and s["attrs"]["duration_ms"] >= 0
        for s in spans
    )
    assert any(
        s["attrs"]["name"] == "daemon.central_relay.start"
        and s["attrs"]["source"] == "acquire_media"
        and s["attrs"]["duration_ms"] >= 0
        for s in spans
    )


def test_media_construct_writes_trace_span_and_camera_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Trace mode records media construction and detected camera specs."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "trace")
    _install_fake_media_server_module(monkeypatch, _FakeMediaServer)
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = Daemon(no_media=False)

    events = _read_jsonl(log_file)
    assert daemon.status().camera_specs_name == "fake-camera"
    assert any(
        e["event"] == "span.end"
        and e["attrs"]["name"] == "daemon.media.construct"
        and e["attrs"]["duration_ms"] >= 0
        for e in events
    )
    assert any(
        e["event"] == "daemon.media.camera_specs"
        and e["attrs"]["camera_specs_name"] == "fake-camera"
        for e in events
    )


def test_media_construct_failure_writes_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logging: None,
) -> None:
    """Media construction failures are visible as structured events."""
    monkeypatch.setenv(INSTRUMENT_ENV_VAR, "basic")
    _install_fake_media_server_module(monkeypatch, _FailingMediaServer)
    log_file = tmp_path / "daemon.jsonl"
    configure_daemon_logging("INFO", str(log_file))

    daemon = Daemon(no_media=False)

    events = _read_jsonl(log_file)
    assert daemon._media_server is None
    assert any(
        e["event"] == "daemon.media.construct.error"
        and e["attrs"]["error_type"] == "RuntimeError"
        and e["attrs"]["error"] == "camera boom"
        for e in events
    )
