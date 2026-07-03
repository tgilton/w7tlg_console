"""
Live spot/seek — surfaces interesting stations from the real-time WSJT-X
Decode stream (Task 8's listener) plus POTA's public spot feed.

Three kinds of "interesting":
  1. Watch list — a persisted set of specific callsigns the operator always
     wants to know about the moment they're heard.
  2. Idaho detection — US callsign prefixes do NOT map to states (a call
     area like W7 spans ID/WA/OR/MT/UT/NV/WY/AZ), so there is no reliable
     way to identify "this station is in Idaho" from the callsign alone.
     What IS usable: FT8 CQ messages commonly include the caller's grid
     square, and grid squares map to real geography. This module checks a
     decoded grid against the set of Maidenhead grids whose bounding box
     overlaps Idaho — genuinely useful, but an approximation: Idaho's
     actual border is irregular, so grids near the WA/OR/MT/WY/UT/NV edges
     may false-positive. Documented, not silently assumed precise.
  3. POTA spots — polled from the public, unauthenticated
     https://api.pota.app/spot/activator endpoint (verified live, real
     schema — not guessed).

DXCC/country "missing entities" tracking is NOT here — see
award_tracker.py's module docstring for why, and what it would take.

Callsign extraction from FT8 message text is a heuristic, not a full
implementation of every WSJT-X message grammar (CQ, directed calls, RRR/
RR73/73, contest exchanges, compound calls with /P /QRP etc. all vary) —
it extracts every callsign-shaped token and lets the caller decide what to
do with all of them, rather than trying to definitively identify "the
sender" and risk silently missing a match.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Coroutine, Optional

import httpx

from wsjtx.protocol import Decode
from wsjtx.dx_cluster import ClusterSpot

logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
POTA_SPOTS_URL = "https://api.pota.app/spot/activator"
POTA_POLL_INTERVAL_S = 60  # POTA spots are minutes-granularity; no need to hammer it

# Reasonably permissive amateur callsign pattern: 1-2 letter/digit prefix
# block containing at least one letter and ending in a digit, then 1-4
# letter suffix. Deliberately over-matches slightly rather than under —
# false positives here just mean checking a non-callsign token against the
# watch list and grid set, which costs nothing; false negatives would
# silently miss a real spot.
_CALLSIGN_RE = re.compile(r"\b[A-Z0-9]{1,3}[0-9][A-Z]{1,4}\b")
_GRID_RE = re.compile(r"\b[A-R]{2}[0-9]{2}\b")

# Tokens that match the callsign regex shape but are protocol furniture,
# not callsigns — must be filtered or every CQ message "matches" CQ itself.
_NOT_CALLSIGNS = frozenset({"CQ", "DE", "QRZ", "RR73", "RRR", "DX", "TEST", "POTA", "SOTA"})

# Idaho's bounding-box grid squares (see module docstring for the
# irregular-border caveat). Computed from Idaho's approximate lat/lon
# extent (41.9-49.1N, 111.0-117.3W) via the standard Maidenhead formula.
IDAHO_GRIDS = frozenset({
    "DN11", "DN12", "DN13", "DN14", "DN15", "DN16", "DN17", "DN18",
    "DN21", "DN22", "DN23", "DN24", "DN25", "DN26", "DN27", "DN28",
    "DN31", "DN32", "DN33", "DN34", "DN35", "DN36", "DN37", "DN38",
    "DN41", "DN42", "DN43", "DN44", "DN45", "DN46", "DN47", "DN48",
})


def extract_callsigns(message: str) -> list[str]:
    return [
        c for c in _CALLSIGN_RE.findall(message.upper())
        if c not in _NOT_CALLSIGNS
    ]


def extract_grid(message: str) -> Optional[str]:
    # RR73 (QSO-completion code) has the same shape as a grid square
    # (2 letters in A-R + 2 digits) and must be excluded explicitly.
    matches = [m for m in _GRID_RE.findall(message.upper()) if m != "RR73"]
    return matches[-1] if matches else None  # grid is conventionally the last token


@dataclass
class SpotAlert:
    kind: str          # "watchlist" | "idaho" | "pota"
    call: str
    detail: str
    raw_message: str = ""


AlertCallback = Callable[[SpotAlert], Coroutine]


class WatchList:
    """Persisted set of callsigns to always alert on. Same JSON-file
    pattern as config/station_profile.py."""

    def __init__(self):
        self._calls: set[str] = self._load()

    def _load(self) -> set[str]:
        try:
            if WATCHLIST_FILE.exists():
                return set(json.loads(WATCHLIST_FILE.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load watch list: {e}")
        return set()

    def _save(self):
        try:
            WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            WATCHLIST_FILE.write_text(json.dumps(sorted(self._calls)))
        except OSError as e:
            logger.error(f"Could not persist watch list: {e}")

    def add(self, call: str):
        self._calls.add(call.upper())
        self._save()

    def remove(self, call: str):
        self._calls.discard(call.upper())
        self._save()

    def contains(self, call: str) -> bool:
        return call.upper() in self._calls

    def list(self) -> list[str]:
        return sorted(self._calls)


class Spotter:
    def __init__(self, watch_list: Optional[WatchList] = None):
        self.watch_list = watch_list or WatchList()
        self._alert_callbacks: list[AlertCallback] = []
        self._pota_task: Optional[asyncio.Task] = None
        self._seen_pota_spot_ids: set[int] = set()

    def on_alert(self, cb: AlertCallback):
        self._alert_callbacks.append(cb)

    async def _fire(self, alert: SpotAlert):
        for cb in self._alert_callbacks:
            try:
                await cb(alert)
            except Exception as e:
                logger.error(f"Spotter alert callback error: {e}")

    # -- WSJT-X decode handling ------------------------------------------

    async def on_decode(self, msg: Decode):
        calls = extract_callsigns(msg.message)
        if not calls:
            return
        grid = extract_grid(msg.message)

        for call in calls:
            if self.watch_list.contains(call):
                await self._fire(SpotAlert(
                    kind="watchlist", call=call,
                    detail=f"Watched callsign heard: {call}",
                    raw_message=msg.message,
                ))

        if grid and grid[:4].upper() in IDAHO_GRIDS and calls:
            # Grid belongs to the CQ'er, conventionally the first callsign
            # in the message (e.g. "CQ CALL GRID" or "CQ POTA CALL GRID").
            await self._fire(SpotAlert(
                kind="idaho", call=calls[0],
                detail=f"Possible Idaho station (grid {grid}) — verify, grid-boundary heuristic",
                raw_message=msg.message,
            ))

    # -- DX cluster / RBN spot handling (SSB/CW, see wsjtx/dx_cluster.py) --

    async def on_cluster_spot(self, spot: ClusterSpot):
        if self.watch_list.contains(spot.dx_call):
            source_label = "RBN (CW)" if spot.source == "rbn" else "DX cluster"
            await self._fire(SpotAlert(
                kind="watchlist", call=spot.dx_call,
                detail=(
                    f"Watched callsign heard on {spot.freq_hz/1e6:.4f} MHz "
                    f"{spot.mode or ''} via {source_label} (spotted by {spot.spotter})"
                ),
                raw_message=spot.raw,
            ))

        # Human-typed cluster comments occasionally include a grid square
        # (e.g. "FN20 QRP") — same best-effort check as the WSJT-X path,
        # RBN's comment field never has one (just "CQ"/"BEACON"/etc.) so
        # this only really fires from the DXSpider side in practice.
        grid = extract_grid(spot.comment)
        if grid and grid[:4].upper() in IDAHO_GRIDS:
            await self._fire(SpotAlert(
                kind="idaho", call=spot.dx_call,
                detail=(
                    f"Possible Idaho station (grid {grid} in spot comment) — "
                    f"verify, grid-boundary heuristic"
                ),
                raw_message=spot.raw,
            ))

    # -- POTA polling ------------------------------------------------------

    async def start_pota_polling(self):
        if self._pota_task is not None:
            return
        self._pota_task = asyncio.create_task(self._pota_poll_loop())

    async def stop_pota_polling(self):
        if self._pota_task is not None:
            self._pota_task.cancel()
            self._pota_task = None

    async def _pota_poll_loop(self):
        while True:
            try:
                await self._poll_pota_once()
            except Exception as e:
                logger.warning(f"POTA poll failed: {e}")
            await asyncio.sleep(POTA_POLL_INTERVAL_S)

    async def _poll_pota_once(self):
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                POTA_SPOTS_URL, headers={"User-Agent": "w7tlg-console/1.0"}
            )
            resp.raise_for_status()
            spots = resp.json()

        for spot in spots:
            spot_id = spot.get("spotId")
            if spot_id is None or spot_id in self._seen_pota_spot_ids:
                continue
            self._seen_pota_spot_ids.add(spot_id)
            activator = spot.get("activator", "?")
            await self._fire(SpotAlert(
                kind="pota", call=activator,
                detail=(
                    f"{activator} POTA activating {spot.get('reference','?')} "
                    f"({spot.get('name','?')}) on {spot.get('frequency','?')} kHz "
                    f"{spot.get('mode','?')}"
                ),
            ))

        # Bound memory — no need to remember spot IDs from hours ago.
        if len(self._seen_pota_spot_ids) > 2000:
            self._seen_pota_spot_ids = set(list(self._seen_pota_spot_ids)[-1000:])


# Module-level singleton, matching the rest of the codebase's convention.
spotter = Spotter()
