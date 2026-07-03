"""
Minimal ADIF parser.

ADIF (Amateur Data Interchange Format) records look like:

    <call:5>W7TLG<gridsquare:6>DN13WN<qso_date:8>20260702<eor>

Field tag: <name:length[:type]>, followed by exactly `length` bytes of
value (length is a byte count, not necessarily character count for
non-ASCII, but WSJT-X/ADIF data here is effectively always ASCII).
Case-insensitive field names; this parser lowercases them on the way out.
Records end with <eor> (case-insensitive); an optional header before the
first record ends with <eoh> and is skipped entirely — this console has
no use for it and some fields in the header aren't valid tag syntax.

Deliberately not a full ADIF spec implementation (no APP_-vendor-field
type coercion, no multi-line value handling beyond what length-prefixing
already covers) — just enough to read what WSJT-X and RUMLogNG actually
write, which is a small, well-behaved subset.
"""

from __future__ import annotations

import re
from typing import Iterator

_TAG_RE = re.compile(r"<(\w+):(\d+)(?::\w+)?>", re.IGNORECASE)
_EOR_RE = re.compile(r"<eor>", re.IGNORECASE)
_EOH_RE = re.compile(r"<eoh>", re.IGNORECASE)


def parse_records(adif_text: str) -> list[dict[str, str]]:
    """Parse all QSO records from an ADIF string. Returns a list of
    dicts (field name -> value, names lowercased). Skips any header."""
    eoh = _EOH_RE.search(adif_text)
    body = adif_text[eoh.end():] if eoh else adif_text
    return list(_parse_body(body))


def _parse_body(body: str) -> Iterator[dict[str, str]]:
    pos = 0
    n = len(body)
    record: dict[str, str] = {}

    while pos < n:
        eor = _EOR_RE.match(body, pos)
        if eor:
            if record:
                yield record
                record = {}
            pos = eor.end()
            continue

        tag = _TAG_RE.match(body, pos)
        if not tag:
            # Skip stray whitespace/junk between tags rather than aborting
            # the whole parse over one malformed record.
            pos += 1
            continue

        name = tag.group(1).lower()
        length = int(tag.group(2))
        value_start = tag.end()
        value_end = value_start + length
        record[name] = body[value_start:value_end]
        pos = value_end

    if record:
        # Trailing record with no terminating <eor> — still usable.
        yield record


def read_adif_file(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_records(f.read())
