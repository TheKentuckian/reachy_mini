"""Tests for wifi_config startup logic."""

from __future__ import annotations

import sys
import types
from threading import Thread
from unittest.mock import MagicMock, patch


def _make_fake_nmcli() -> types.ModuleType:
    """Build a minimal stub for the nmcli package."""
    mod = types.ModuleType("nmcli")
    mod.device = MagicMock()
    mod.connection = MagicMock(return_value=[])

    data_mod = types.ModuleType("nmcli.data")
    device_mod = types.ModuleType("nmcli.data.device")
    conn_mod = types.ModuleType("nmcli.data.connection")

    device_mod.DeviceWifi = object
    conn_mod.Connection = object

    # Upstream's wifi_config imports `from nmcli._exception import
    # NotExistException`; provide it so the module imports cleanly.
    exception_mod = types.ModuleType("nmcli._exception")

    class NotExistException(Exception):
        pass

    exception_mod.NotExistException = NotExistException

    sys.modules["nmcli"] = mod
    sys.modules["nmcli.data"] = data_mod
    sys.modules["nmcli.data.device"] = device_mod
    sys.modules["nmcli.data.connection"] = conn_mod
    sys.modules["nmcli._exception"] = exception_mod
    mod.data = data_mod
    mod._exception = exception_mod
    data_mod.device = device_mod
    data_mod.connection = conn_mod
    return mod


def _import_wifi_config():
    """Import wifi_config with nmcli stubbed out, returning a fresh module each call."""
    _make_fake_nmcli()

    # Drop any cached copy so module-level code re-executes with the stub.
    for key in list(sys.modules):
        if "wifi_config" in key:
            del sys.modules[key]

    import importlib

    return importlib.import_module("reachy_mini.daemon.app.routers.wifi_config")


def _make_connection(name: str, device: str = "--"):
    conn = MagicMock()
    conn.name = name
    conn.conn_type = "wifi"
    conn.device = device
    return conn


class TestEnsureWifiOnStartup:
    """ensure_wifi_on_startup() should skip wifi_rescan when already connected."""

    def test_skips_rescan_when_wlan_active(self) -> None:
        wc = _import_wifi_config()
        active_conn = _make_connection("HomeWifi", device="wlan0")

        with (
            patch.object(
                sys.modules["nmcli"],
                "connection",
                return_value=[active_conn],
            ),
            patch.object(
                sys.modules["nmcli"].device,
                "wifi_rescan",
            ) as mock_rescan,
        ):
            wc.ensure_wifi_on_startup()

        mock_rescan.assert_not_called()

    def test_skips_rescan_when_hotspot_active(self) -> None:
        wc = _import_wifi_config()
        hotspot_conn = _make_connection("Hotspot", device="wlan0")

        with (
            patch.object(
                sys.modules["nmcli"],
                "connection",
                return_value=[hotspot_conn],
            ),
            patch.object(
                sys.modules["nmcli"].device,
                "wifi_rescan",
            ) as mock_rescan,
        ):
            wc.ensure_wifi_on_startup()

        mock_rescan.assert_not_called()

    def test_runs_rescan_when_disconnected(self) -> None:
        wc = _import_wifi_config()

        with (
            patch.object(
                sys.modules["nmcli"],
                "connection",
                return_value=[],
            ),
            patch.object(
                sys.modules["nmcli"].device,
                "wifi_rescan",
            ) as mock_rescan,
            patch.object(
                sys.modules["nmcli"].device,
                "wifi",
                return_value=[],
            ),
            patch.object(wc, "setup_wifi_connection"),
        ):
            wc.ensure_wifi_on_startup()

        mock_rescan.assert_called_once()

    def test_sets_up_hotspot_when_still_disconnected_after_rescan(self) -> None:
        wc = _import_wifi_config()

        with (
            patch.object(sys.modules["nmcli"], "connection", return_value=[]),
            patch.object(sys.modules["nmcli"].device, "wifi_rescan"),
            patch.object(sys.modules["nmcli"].device, "wifi", return_value=[]),
            patch.object(wc, "setup_wifi_connection") as mock_setup,
        ):
            wc.ensure_wifi_on_startup()

        mock_setup.assert_called_once_with(
            name="Hotspot",
            ssid=wc.HOTSPOT_SSID,
            password=wc.HOTSPOT_PASSWORD,
            is_hotspot=True,
        )
