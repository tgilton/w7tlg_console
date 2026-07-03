"""
QSO Performance Logger — one rich record per QSO, joining the ADIF details
WSJT-X logs with a station-telemetry summary covering the exact QSO window.

This is deliberately NOT a logbook and does not compete with RUMLogNG,
which the operator has said is the authoritative log. This is a secondary,
diagnostic record for studying station/rig/band performance over time —
"what was my SWR/drive/PA temp actually doing during this specific
contact" — data RUMLogNG has no reason to track.

Design:
  1. A throttled ~1Hz rolling sample buffer of station telemetry, fed by
     the *existing* AcomBridge.on_state_change() stream (which fires far
     more often than 1Hz — every rig poll and every amp telemetry frame)
     rather than a second independent poller.
  2. On each WSJT-X LoggedAdif event, parse the ADIF record for
     QSO_DATE/TIME_ON and QSO_DATE_OFF/TIME_OFF (UTC, per ADIF/WSJT-X
     convention), slice the buffer to that window, and compute min/avg/max
     for the numeric telemetry fields.
  3. Append one JSON object (not a whole-file array) per QSO to a .jsonl
     file — chosen specifically because the operator expects the field
     list to keep growing as new instrumentation ideas come up, and JSONL
     tolerates that gracefully (each line is independent; old lines don't
     need to be rewritten when new fields are added), unlike CSV where
     every row must share one fixed column set.

Output: data/qso_performance_log.jsonl
CSV conversion: tools/qso_log_to_csv.py (separate script, run on demand —
sizes a CSV header from the union of every key seen across all records).
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from amplifier.acom_bridge import StationState
from config.station_profile import station_profile
from wsjtx.adif import parse_records
from wsjtx.protocol import LoggedAdif

logger = logging.getLogger(__name__)

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "qso_performance_log.jsonl"

# ~1Hz over a generous 20-minute window comfortably covers any realistic
# QSO (including slow CW ragchews), with headroom to spare.
SAMPLE_INTERVAL_S = 1.0
BUFFER_MAXLEN = 20 * 60


@dataclass
class TelemetrySample:
    ts: float
    fwd_w: float
    refl_w: float
    swr: float
    temp_c: float
    drive_w: float
    hv_v: float
    current_ma: float
    freq_hz: int
    band: str
    mode: str
    preamp_name: str
    agc: int
    antenna: int
    operating_mode: str
    atu_tuned: bool
    ptt: bool
    sdr_strength_db: Optional[float]


# Numeric fields worth a min/avg/max summary. Everything else (band, mode,
# antenna, ...) is categorical — summarized as "value at start" plus a
# "changed mid-QSO" flag instead, since averaging a band name makes no sense.
_NUMERIC_FIELDS = [
    "fwd_w", "refl_w", "swr", "temp_c", "drive_w",
    "hv_v", "current_ma", "sdr_strength_db",
]
_CATEGORICAL_FIELDS = [
    "band", "mode", "preamp_name", "agc", "antenna",
    "operating_mode", "atu_tuned",
]


class QsoTelemetryLogger:
    def __init__(self):
        self._buffer: deque[TelemetrySample] = deque(maxlen=BUFFER_MAXLEN)
        self._last_sample_ts: float = 0.0

    async def record_sample(self, state: StationState):
        """Register directly via AcomBridge.on_state_change() — async to
        match that callback's contract (it does `await cb(state)`), even
        though everything here is synchronous; throttles internally, safe
        to call at whatever rate the bridge actually fires."""
        now = time.time()
        if now - self._last_sample_ts < SAMPLE_INTERVAL_S:
            return
        self._last_sample_ts = now

        rig = state.rig or {}
        sample = TelemetrySample(
            ts=now,
            fwd_w=state.amp_fwd_w,
            refl_w=state.amp_refl_w,
            swr=state.amp_swr,
            temp_c=state.amp_temp_c,
            drive_w=state.amp_drive_w,
            hv_v=state.amp_hv_v,
            current_ma=state.amp_current_ma,
            freq_hz=int(rig.get("freq_hz") or 0),
            band=str(rig.get("band") or "??"),
            mode=str(rig.get("mode") or ""),
            preamp_name=str(rig.get("preamp_name") or ""),
            agc=int(rig.get("agc") or 0),
            antenna=state.selected_antenna,
            operating_mode=state.operating_mode,
            atu_tuned=state.amp_atu_tuned,
            ptt=bool(rig.get("ptt", False)),
            sdr_strength_db=rig.get("sdr_strength_db"),
        )
        self._buffer.append(sample)

    def _slice(self, start_ts: float, end_ts: float) -> list[TelemetrySample]:
        # Buffer is append-ordered by ts, but a plain linear scan is fine —
        # at most BUFFER_MAXLEN (1200) samples, called once per logged QSO,
        # not a hot path.
        return [s for s in self._buffer if start_ts <= s.ts <= end_ts]

    async def on_logged_adif(self, msg: LoggedAdif):
        records = parse_records(msg.adif_text)
        if not records:
            logger.warning(f"LoggedAdif with no parseable ADIF record: {msg.adif_text!r}")
            return
        for record in records:
            self._log_one_qso(record)

    def _log_one_qso(self, adif: dict[str, str]):
        start_ts, end_ts = _adif_qso_window(adif)
        samples = self._slice(start_ts, end_ts) if start_ts and end_ts else []

        telemetry_summary: dict = {}
        for field_name in _NUMERIC_FIELDS:
            values = [getattr(s, field_name) for s in samples
                      if getattr(s, field_name) is not None]
            if values:
                telemetry_summary[field_name] = {
                    "min": round(min(values), 2),
                    "avg": round(statistics.fmean(values), 2),
                    "max": round(max(values), 2),
                    "n": len(values),
                }
        for field_name in _CATEGORICAL_FIELDS:
            values = [getattr(s, field_name) for s in samples]
            if values:
                telemetry_summary[field_name] = {
                    "start": values[0],
                    "changed": len(set(values)) > 1,
                }

        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "qso": adif,
            "station_profile": station_profile.current.id,
            "telemetry_sample_count": len(samples),
            "telemetry_window_s": (
                round(end_ts - start_ts, 1) if start_ts and end_ts else None
            ),
            "telemetry": telemetry_summary,
        }

        self._append(record)
        logger.info(
            f"QSO logged: {adif.get('call', '?')} on {adif.get('band', '?')} "
            f"({len(samples)} telemetry samples over "
            f"{record['telemetry_window_s']}s)"
        )

    def _append(self, record: dict):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.error(f"Could not write QSO performance log: {e}")


def _adif_qso_window(adif: dict[str, str]) -> tuple[Optional[float], Optional[float]]:
    """Parse QSO_DATE/TIME_ON and QSO_DATE_OFF/TIME_OFF (or QSO_DATE as a
    fallback for the off-date) into a (start_epoch, end_epoch) tuple, UTC.
    Returns (None, None) if the required fields aren't present/parseable —
    callers should treat that as "no telemetry available", not an error;
    a QSO still gets logged, just without a telemetry summary."""
    try:
        date_on = adif.get("qso_date", "")
        time_on = adif.get("time_on", "")
        date_off = adif.get("qso_date_off") or date_on
        time_off = adif.get("time_off", "")
        if not (date_on and time_on and time_off):
            return None, None

        start = _parse_adif_datetime(date_on, time_on)
        end = _parse_adif_datetime(date_off, time_off)
        return start, end
    except (ValueError, KeyError) as e:
        logger.debug(f"Could not parse ADIF QSO window: {e}")
        return None, None


def _parse_adif_datetime(date_str: str, time_str: str) -> float:
    """ADIF date is YYYYMMDD, time is HHMM or HHMMSS, always UTC."""
    time_str = time_str.ljust(6, "0")  # HHMM -> HHMM00
    dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


# Module-level singleton, matching the rest of the codebase's convention.
qso_telemetry_logger = QsoTelemetryLogger()
