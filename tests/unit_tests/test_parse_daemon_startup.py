"""Tests for daemon startup log parser."""

import json

from scripts.parse_daemon_startup import format_table, load_records, summarize


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
