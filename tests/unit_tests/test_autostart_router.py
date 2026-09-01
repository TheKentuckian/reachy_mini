"""Unit tests for POST /api/autostart/restart endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reachy_mini.daemon.app.routers.autostart import router


@pytest.fixture()
def client() -> TestClient:
    """TestClient with only the autostart router mounted."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_proc(rc: int, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = rc
    proc.stdout = ""
    proc.stderr = stderr
    return proc


class TestRestartEndpoint:
    """POST /api/autostart/restart."""

    def test_happy_path_all_stages_succeed(self, client: TestClient) -> None:
        """Stop -> reset-failed -> start, all rc=0, in that order."""
        ok_proc = _make_proc(0)

        with (
            patch(
                "reachy_mini.daemon.app.routers.autostart.subprocess.run",
                return_value=ok_proc,
            ) as mock_run,
            patch(
                "reachy_mini.daemon.app.routers.autostart.shutil.which",
                return_value="/usr/bin/systemctl",
            ),
            patch("reachy_mini.daemon.app.routers.autostart.time.sleep"),
        ):
            resp = client.post("/api/autostart/restart")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert len(body["stages"]) == 3
        assert [s["name"] for s in body["stages"]] == ["stop", "reset-failed", "start"]
        assert all(s["rc"] == 0 for s in body["stages"])

        # Verify the exact systemctl commands issued
        expected_calls = [
            call(
                ["sudo", "systemctl", "stop", "reachy-app-autostart.service"],
                capture_output=True,
                text=True,
                timeout=10,
            ),
            call(
                ["sudo", "systemctl", "reset-failed", "reachy-app-autostart.service"],
                capture_output=True,
                text=True,
                timeout=10,
            ),
            call(
                ["sudo", "systemctl", "start", "reachy-app-autostart.service"],
                capture_output=True,
                text=True,
                timeout=10,
            ),
        ]
        mock_run.assert_has_calls(expected_calls)

    def test_stop_fails_remaining_stages_still_run(self, client: TestClient) -> None:
        """A failing stop is reported but does not short-circuit the later stages."""
        fail_proc = _make_proc(1, stderr="unit not found")
        ok_proc = _make_proc(0)

        side_effects = [fail_proc, ok_proc, ok_proc]

        with (
            patch(
                "reachy_mini.daemon.app.routers.autostart.subprocess.run",
                side_effect=side_effects,
            ),
            patch(
                "reachy_mini.daemon.app.routers.autostart.shutil.which",
                return_value="/usr/bin/systemctl",
            ),
            patch("reachy_mini.daemon.app.routers.autostart.time.sleep"),
        ):
            resp = client.post("/api/autostart/restart")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert len(body["stages"]) == 3
        assert body["stages"][0]["name"] == "stop"
        assert body["stages"][0]["rc"] == 1
        assert body["stages"][0]["stderr"] == "unit not found"
        # reset-failed and start still attempted
        assert body["stages"][1]["rc"] == 0
        assert body["stages"][2]["rc"] == 0
