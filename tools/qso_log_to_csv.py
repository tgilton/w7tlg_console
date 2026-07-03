#!/usr/bin/env python3
"""
Convert the QSO performance JSONL log to CSV for spreadsheet analysis.

The JSONL log intentionally has no fixed schema — new telemetry fields get
added over time without needing to migrate old records. This tool handles
that by flattening every record's nested structure (qso.*, telemetry.*)
into dotted column names, then taking the UNION of every column seen
across the whole file as the CSV header — older records just get blank
cells for columns that didn't exist yet when they were logged, rather
than the conversion breaking or silently dropping fields.

Usage:
    python3 tools/qso_log_to_csv.py
        Converts data/qso_performance_log.jsonl -> data/qso_performance_log.csv

    python3 tools/qso_log_to_csv.py path/to/input.jsonl path/to/output.csv
        Explicit paths.
"""

import csv
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path(__file__).resolve().parent.parent / "data" / "qso_performance_log.jsonl"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "qso_performance_log.csv"


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Recursively flatten nested dicts into dotted keys.
    {"telemetry": {"fwd_w": {"min": 1, "max": 2}}} ->
    {"telemetry.fwd_w.min": 1, "telemetry.fwd_w.max": 2}"""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(flatten(v, key))
    elif isinstance(obj, list):
        # Lists (e.g. a future multi-value field) are stored as a single
        # semicolon-joined cell rather than exploded into N columns, since
        # the column count would otherwise vary row to row.
        out[prefix] = "; ".join(str(v) for v in obj)
    else:
        out[prefix] = obj
    return out


def convert(input_path: Path, output_path: Path) -> int:
    if not input_path.exists():
        print(f"No log file at {input_path} — nothing to convert.")
        return 0

    rows: list[dict[str, Any]] = []
    with open(input_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"Skipping malformed line {line_num}: {e}", file=sys.stderr)
                continue
            rows.append(flatten(record))

    if not rows:
        print("No valid records found.")
        return 0

    # Union of all columns, in first-seen order (stable/readable output
    # rather than alphabetical, so related fields stay grouped).
    columns: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows, {len(columns)} columns -> {output_path}")
    return len(rows)


if __name__ == "__main__":
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT
    convert(input_path, output_path)
