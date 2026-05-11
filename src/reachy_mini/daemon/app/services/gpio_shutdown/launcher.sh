#!/bin/bash
source /venvs/mini_daemon/bin/activate
exec python -u -m reachy_mini.daemon.app.services.gpio_shutdown.shutdown_monitor
