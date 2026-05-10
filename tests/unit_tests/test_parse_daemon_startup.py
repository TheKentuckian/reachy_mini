"""Tests for daemon startup log parser."""

import json
import subprocess
import sys

from scripts.parse_daemon_startup import (
    format_table,
    load_records,
    load_records_from_lines,
    summarize,
)


def test_parse_daemon_startup_summarizes_latest_window(tmp_path):  # noqa: D103
    log_file = tmp_path / "daemon.jsonl"
    records = [
        {"ts": "old", "event": "daemon.main.start", "attrs": {}},
        {
            "ts": "old",
            "event": "span.end",
            "attrs": {"name": "daemon.create_app", "duration_ms": 999.0},
        },
        {"ts": "new", "event": "daemon.main.start", "attrs": {}},
        {
            "ts": "new",
            "event": "span.end",
            "attrs": {
                "name": "daemon.wireless_checks.venvs_ownership",
                "duration_ms": 1.2345,
                "outcome": "ok",
            },
        },
        {
            "ts": "new",
            "event": "daemon.uvicorn.startup",
            "attrs": {"duration_ms": 700},
        },
    ]
    log_file.write_text("\n".join(json.dumps(record) for record in records))

    rows = summarize(load_records(log_file))
    table = format_table(rows)
    row_by_name = {row[0]: row for row in rows}

    assert "daemon.create_app" in table
    assert "999.000" not in table
    assert row_by_name["daemon.wireless_checks.venvs_ownership"] == (
        "daemon.wireless_checks.venvs_ownership",
        "1.234",
        "ok",
        "new",
    )
    assert "daemon.uvicorn.startup" in table
    assert "700.000" in table


def test_parse_daemon_startup_ignores_non_json_journal_lines():  # noqa: D103
    records = load_records_from_lines(
        [
            "Ownership marker valid for /venvs; skipping recursive scan",
            json.dumps({"ts": "now", "event": "daemon.main.start", "attrs": {}}),
            "[6:00:02.094595738] [79583] INFO Camera camera_manager.cpp:340",
            json.dumps(
                {
                    "ts": "now",
                    "event": "span.end",
                    "attrs": {
                        "name": "daemon.start",
                        "duration_ms": 670.973,
                        "outcome": "ok",
                    },
                }
            ),
            json.dumps(
                {
                    "ts": "now",
                    "event": "span.end",
                    "attrs": {
                        "name": "fastapi.lifespan.startup",
                        "duration_ms": 677.781,
                        "outcome": "ok",
                    },
                }
            ),
        ]
    )

    rows = summarize(records)
    row_by_name = {row[0]: row for row in rows}

    assert row_by_name["fastapi.lifespan.startup"] == (
        "fastapi.lifespan.startup",
        "677.781",
        "ok",
        "now",
    )
    assert row_by_name["daemon.start"] == (
        "daemon.start",
        "670.973",
        "ok",
        "now",
    )


def test_parse_daemon_startup_cli_reads_stdin():  # noqa: D103
    payload = "\n".join(
        [
            "not json",
            json.dumps(
                {
                    "ts": "stdin",
                    "event": "daemon.uvicorn.startup",
                    "attrs": {"duration_ms": 739.441},
                }
            ),
        ]
    )

    result = subprocess.run(
        [sys.executable, "scripts/parse_daemon_startup.py", "-"],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "daemon.uvicorn.startup" in result.stdout
    assert "739.441" in result.stdout
