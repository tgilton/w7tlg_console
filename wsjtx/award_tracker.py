"""
Award/worked-before tracker — "what states am I missing on 30m?"

Primary data source: RUMLogNG's own SQLite database, found via its
title-bar path in a screenshot the operator shared —
~/Library/CloudStorage/Dropbox/Research/Amateur_Radio/Logs/TG Log 001.rlog
— a plain SQLite 3 file living in a normal Dropbox-synced folder, NOT
inside the app's sandboxed container (~/Library/Containers/...) that
blocked direct access earlier with "Operation not permitted". Confirmed
readable with a plain read-only sqlite3 connection, no special
permissions needed.

This is a dramatic upgrade over the WSJT-X-local-ADIF interim source it
replaces: 1108 QSOs vs. 660, 100% DXCC-entity coverage (WSJT-X's log had
zero — that's why "missing countries" wasn't implemented before), plus
ITU/CQ zone and county fields WSJT-X never captured.

Deliberately NOT used: the logbook table's qsl/lotwqsl/eqsl columns.
Checked directly — every single one of the 1108 rows has the same value
('W') in all three columns, so whatever they encode, it isn't a per-QSO
confirmed-vs-worked flag the way their names suggest. Real confirmation
tracking appears to live in the separate dxlist table as a per-band
letter-grid per DXCC entity (RUMLogNG's own award-progress bookkeeping),
which hasn't been decoded here — guessing at its meaning risks reporting
a QSO as "confirmed" when it isn't, which matters for actual award
submissions. Only unambiguous fields are used: worked status (a QSO
record existing), band, DXCC entity, US state.

Also deliberately never touched: the prefs table. It contains the
operator's actual LoTW/eQSL account credentials in plain columns
(lotwuser/lotwpassword/eqsluser/eqslpassword) — completely out of scope
for an award tracker and not something this module should ever read.

Falls back to WSJT-X's local ADIF log if the RUMLogNG database can't be
opened (moved, renamed, Dropbox not synced, etc.) — keeps the console
working, just with less complete data, rather than failing outright.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from wsjtx.adif import read_adif_file

logger = logging.getLogger(__name__)

RUMLOGNG_DB_PATH = (
    Path.home() / "Library" / "CloudStorage" / "Dropbox" / "Research"
    / "Amateur_Radio" / "Logs" / "TG Log 001.rlog"
)
WSJTX_LOCAL_ADIF = Path.home() / "Library" / "Application Support" / "WSJT-X" / "wsjtx_log.adi"

# All 50 US states, USPS 2-letter codes — the standard WAS (Worked All
# States) award set. DC/territories deliberately excluded; WAS is the 50
# states only.
ALL_US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
})

ALL_BANDS = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]


@dataclass
class WorkedQso:
    call: str
    band: str
    mode: str
    state: str = ""
    grid: str = ""
    dxcc_adif: Optional[int] = None
    dxcc_prefix: str = ""


@dataclass
class DxccEntity:
    dxcc_adif: int
    prefix: str
    worked_count: int = 0
    bands: set = field(default_factory=set)


class AwardTracker:
    def __init__(self,
                 rumlogng_path: Path = RUMLOGNG_DB_PATH,
                 wsjtx_adif_path: Path = WSJTX_LOCAL_ADIF):
        self._rumlogng_path = rumlogng_path
        self._wsjtx_adif_path = wsjtx_adif_path
        self._worked: list[WorkedQso] = []
        self.source: str = ""   # "rumlogng" | "wsjtx_adif" | "" (nothing loaded)
        self.reload()

    def reload(self):
        """Re-read the data source. Cheap enough (a couple thousand
        records at most) to call on every relevant UI action rather than
        needing a file-watcher."""
        if self._load_from_rumlogng():
            return
        logger.warning(
            f"RUMLogNG database unavailable at {self._rumlogng_path}, "
            f"falling back to WSJT-X local ADIF"
        )
        self._load_from_wsjtx_adif()

    def _load_from_rumlogng(self) -> bool:
        if not self._rumlogng_path.exists():
            return False
        try:
            # Read-only URI connection — this file is RUMLogNG's live,
            # actively-open database; never risk a write or lock conflict
            # against it.
            uri = f"file:{self._rumlogng_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT callsign, band, mode, state, locator, dxcc, dxccadif "
                "FROM logbook WHERE callsign IS NOT NULL AND callsign != ''"
            )
            rows = cur.fetchall()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Could not read RUMLogNG database: {e}")
            return False

        self._worked = [
            WorkedQso(
                call=(r["callsign"] or "").upper(),
                band=(r["band"] or "").lower(),
                mode=(r["mode"] or "").upper(),
                state=(r["state"] or "").upper(),
                grid=(r["locator"] or "").upper(),
                dxcc_adif=r["dxccadif"] if r["dxccadif"] else None,
                dxcc_prefix=(r["dxcc"] or "").upper(),
            )
            for r in rows
        ]
        self.source = "rumlogng"
        logger.info(f"Award tracker loaded {len(self._worked)} QSOs from RUMLogNG ({self._rumlogng_path})")
        return True

    def _load_from_wsjtx_adif(self):
        if not self._wsjtx_adif_path.exists():
            logger.warning(f"WSJT-X ADIF log not found at {self._wsjtx_adif_path} either")
            self._worked = []
            self.source = ""
            return
        records = read_adif_file(str(self._wsjtx_adif_path))
        self._worked = [
            WorkedQso(
                call=(r.get("call") or "").upper(),
                band=(r.get("band") or "").lower(),
                mode=(r.get("mode") or "").upper(),
                state=(r.get("state") or "").upper(),
                grid=(r.get("gridsquare") or "").upper(),
            )
            for r in records
            if r.get("call")
        ]
        self.source = "wsjtx_adif"
        logger.info(f"Award tracker loaded {len(self._worked)} QSOs from WSJT-X ADIF ({self._wsjtx_adif_path})")

    # -- States (WAS) --------------------------------------------------

    def worked_states(self, band: Optional[str] = None) -> set[str]:
        return {
            w.state for w in self._worked
            if w.state and (band is None or w.band == band.lower())
        }

    def missing_states(self, band: Optional[str] = None) -> set[str]:
        return ALL_US_STATES - self.worked_states(band)

    def states_summary_by_band(self) -> dict[str, dict]:
        out = {}
        for band in ALL_BANDS:
            worked = self.worked_states(band)
            out[band] = {
                "worked_count": len(worked),
                "missing_count": 50 - len(worked),
                "missing": sorted(ALL_US_STATES - worked),
            }
        return out

    # -- DXCC entities ----------------------------------------------------
    # No "missing" computation here (unlike states) — that needs a
    # complete, current DXCC entity reference list, which isn't bundled.
    # Worked-entity reporting only; a follow-up if a full missing-DXCC
    # view is wanted.

    def worked_dxcc_entities(self, band: Optional[str] = None) -> list[DxccEntity]:
        by_entity: dict[int, DxccEntity] = {}
        for w in self._worked:
            if w.dxcc_adif is None:
                continue
            if band is not None and w.band != band.lower():
                continue
            entry = by_entity.setdefault(
                w.dxcc_adif, DxccEntity(dxcc_adif=w.dxcc_adif, prefix=w.dxcc_prefix)
            )
            entry.worked_count += 1
            entry.bands.add(w.band)
        return sorted(by_entity.values(), key=lambda e: e.prefix)

    def dxcc_summary_by_band(self) -> dict[str, dict]:
        out = {}
        for band in ALL_BANDS:
            entities = self.worked_dxcc_entities(band)
            out[band] = {"worked_count": len(entities)}
        return out

    # -- General ------------------------------------------------------

    def have_worked_call(self, call: str, band: Optional[str] = None) -> bool:
        call = call.upper()
        return any(
            w.call == call and (band is None or w.band == band.lower())
            for w in self._worked
        )

    def qso_count(self) -> int:
        return len(self._worked)


# Module-level singleton, matching the rest of the codebase's convention.
award_tracker = AwardTracker()
