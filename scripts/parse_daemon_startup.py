#!/usr/bin/env python3
"""Summarize Reachy Mini daemon startup spans from a JSONL log."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LOG_FILE = Path("/tmp/daemon.jsonl")

KEY_EVENTS = (
    "daemon.main.start",
    "daemon.wireless_checks.venvs_ownership",
    "daemon.wireless_checks.bluetooth_service",
    "daemon.wireless_checks.wireless_launcher",
    "daemon.wireless_checks.apps_venv_sdk",
    "daemon.wireless_checks.restore_venv",
    "daemon.wireless_checks.asoundrc",
    "daemon.media.construct",
    "daemon.create_app",
    "daemon.backend.construct",
    "daemon.media.start",
    "daemon.central_relay.start",
    "daemon.start",
    "fastapi.lifespan.startup",
    "daemon.uvicorn.startup",
)


def _event_name(record: dict[str, Any]) -> str:
    attrs = record.get("attrs") or {}
    return str(attrs.get("name") or record.get("event") or record.get("message") or "")


def load_records_from_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Load JSONL records from lines, skipping malformed entries."""
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load JSONL records from a file, skipping malformed lines."""
    return load_records_from_lines(path.read_text().splitlines())


def latest_startup_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return records from the latest startup window."""
    start_index = 0
    for index, record in enumerate(records):
        if _event_name(record) == "daemon.main.start":
            start_index = index
    return records[start_index:]


def summarize(records: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    """Return rows for the stable startup summary table."""
    latest: dict[str, dict[str, Any]] = {}
    for record in latest_startup_records(records):
        name = _event_name(record)
        if name in KEY_EVENTS:
            latest[name] = record

    rows: list[tuple[str, str, str, str]] = []
    for name in KEY_EVENTS:
        record = latest.get(name)
        if record is None:
            rows.append((name, "-", "-", "-"))
            continue

        attrs = record.get("attrs") or {}
        duration = attrs.get("duration_ms")
        duration_text = "-" if duration is None else f"{float(duration):.3f}"
        rows.append(
            (
                name,
                duration_text,
                str(attrs.get("outcome") or "-"),
                str(record.get("ts") or "-"),
            )
        )
    return rows


def format_table(rows: list[tuple[str, str, str, str]]) -> str:
    """Format summary rows as a stable plain-text table."""
    table = [("event", "duration_ms", "outcome", "timestamp"), *rows]
    widths = [max(len(row[column]) for row in table) for column in range(4)]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(table[0])),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in table[1:]
    )
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Summarize key daemon startup timings from JSONL logs."
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default=str(DEFAULT_LOG_FILE),
        help=(
            f"Path to daemon JSONL log (default: {DEFAULT_LOG_FILE}); "
            "use '-' to read JSONL from stdin"
        ),
    )
    args = parser.parse_args()

    records = (
        load_records_from_lines(sys.stdin)
        if args.log_file == "-"
        else load_records(Path(args.log_file))
    )
    rows = summarize(records)
    print(format_table(rows))


if __name__ == "__main__":
    main()
