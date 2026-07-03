"""
Propagation — live band activity and solar indices.

Ported from ft991a-panel's propagation.py. Two data sources, both
verified live during that project (same real endpoints, unchanged here):

1. PSKReporter (https://pskreporter.info) — FT8 spots sent FROM the
   operator's own grid square in the last 15 minutes. This shows which
   bands currently have an open path out of this QTH, and which DXCC
   entities are hearing us.
   API docs: https://pskreporter.info/pskquery5.htm

2. NOAA Space Weather — Solar Flux Index (SFI) and planetary K-index (Kp).

The one real adaptation from ft991a-panel: MY_GRID/MY_CALL were hardcoded
constants there (fixed Indio, CA location). Here they're read from
config/station_profile.py on every fetch, so switching the La Quinta/Boise
toggle (Task 7) automatically re-targets these queries at the operator's
actual current grid square — no separate propagation-specific QTH setting
to keep in sync.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.station_profile import station_profile

logger = logging.getLogger(__name__)

BAND_MAP = [
    (1_800_000,   2_000_000,  "160m"),
    (3_500_000,   4_000_000,  "80m"),
    (7_000_000,   7_300_000,  "40m"),
    (10_100_000,  10_150_000, "30m"),
    (14_000_000,  14_350_000, "20m"),
    (18_068_000,  18_168_000, "17m"),
    (21_000_000,  21_450_000, "15m"),
    (24_890_000,  24_990_000, "12m"),
    (28_000_000,  29_700_000, "10m"),
    (50_000_000,  54_000_000, "6m"),
]
BANDS = [name for _, _, name in BAND_MAP]

PSK_TTL = 360    # seconds between PSKReporter fetches (it asks for >=5min between queries)
SOLAR_TTL = 900  # seconds between NOAA fetches — solar indices change slowly


def freq_to_band(freq_hz: int) -> Optional[str]:
    for lo, hi, name in BAND_MAP:
        if lo <= freq_hz <= hi:
            return name
    return None


class PropagationSource:
    def __init__(self):
        self._psk_cache: dict = {}
        self._psk_cache_grid: Optional[str] = None  # re-fetch immediately if QTH changes
        self._last_psk_fetch: float = 0.0
        self._solar_cache: dict = {}
        self._last_solar_fetch: float = 0.0

    async def fetch_pskreporter(self) -> dict:
        grid = station_profile.current.grid[:4]  # PSKReporter wants the 4-char field+square
        now = asyncio.get_event_loop().time()

        stale_grid = grid != self._psk_cache_grid
        if not stale_grid and (now - self._last_psk_fetch) < PSK_TTL and self._psk_cache:
            return self._psk_cache

        url = (
            "https://retrieve.pskreporter.info/query?"
            f"senderGrid={grid}"
            "&flowStartSeconds=-900"
            "&frange=1800000-54000000"
            "&mode=FT8"
            "&statistics=false"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                xml_text = resp.text

            root = ET.fromstring(xml_text)
            band_spots: dict = {b: [] for b in BANDS}

            for spot in root.findall("receptionReport"):
                try:
                    freq = int(spot.get("frequency", 0))
                    band = freq_to_band(freq)
                    if not band:
                        continue
                    band_spots[band].append({
                        "call": spot.get("senderCallsign", ""),
                        "grid": spot.get("senderLocator", ""),
                        "dxcc_code": spot.get("senderDXCCCode", ""),
                        "snr": int(spot.get("sNR", 0)),
                    })
                except (ValueError, TypeError):
                    continue  # malformed individual spot, skip it, keep the rest

            summary = {}
            for band, spots in band_spots.items():
                if not spots:
                    summary[band] = {"count": 0, "avg_snr": None, "dxcc": [], "activity": "dead"}
                    continue
                avg_snr = sum(s["snr"] for s in spots) / len(spots)
                dxcc_counts: dict = {}
                for s in spots:
                    if s["dxcc_code"]:
                        dxcc_counts[s["dxcc_code"]] = dxcc_counts.get(s["dxcc_code"], 0) + 1
                top_dxcc = sorted(dxcc_counts, key=dxcc_counts.get, reverse=True)[:6]
                count = len(spots)
                activity = "high" if count >= 20 else "moderate" if count >= 5 else "low"
                summary[band] = {
                    "count": count, "avg_snr": round(avg_snr, 1),
                    "dxcc": top_dxcc, "activity": activity,
                }

            self._psk_cache = summary
            self._psk_cache_grid = grid
            self._last_psk_fetch = now
            return summary

        except (httpx.HTTPError, ET.ParseError) as e:
            logger.warning(f"PSKReporter unavailable: {type(e).__name__}: {e}")
            return self._psk_cache or {
                b: {"count": 0, "avg_snr": None, "dxcc": [], "activity": "unknown"} for b in BANDS
            }

    async def fetch_solar(self) -> dict:
        now = asyncio.get_event_loop().time()
        if (now - self._last_solar_fetch) < SOLAR_TTL and self._solar_cache:
            return self._solar_cache

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                geomag = await client.get(
                    "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
                )
                flux = await client.get(
                    "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
                )

            sfi = None
            try:
                # Verified live: this endpoint returns a one-element list
                # of {"flux": ..., "time_tag": ...} (lowercase key), not a
                # bare {"Flux": ...} dict the ported code assumed.
                flux_data = flux.json()
                if flux_data:
                    sfi = float(flux_data[-1]["flux"])
            except (ValueError, TypeError, KeyError, IndexError):
                pass

            kp = None
            try:
                # NOAA's current response is a list of dicts with a "Kp"
                # key (verified live) — not the list-of-lists shape the
                # ported ft991a-panel code assumed, which raised KeyError
                # against real current data.
                kp_data = geomag.json()
                if kp_data:
                    kp = float(kp_data[-1]["Kp"])
            except (ValueError, TypeError, KeyError, IndexError):
                pass

            result = {"sfi": sfi, "kp": kp, "updated": datetime.now(timezone.utc).strftime("%H:%MZ")}
            self._solar_cache = result
            self._last_solar_fetch = now
            return result

        except httpx.HTTPError as e:
            logger.warning(f"Solar fetch error: {e}")
            return self._solar_cache or {"sfi": None, "kp": None, "updated": None}

    async def get_state(self) -> dict:
        bands, solar = await asyncio.gather(self.fetch_pskreporter(), self.fetch_solar())
        return {
            "bands": bands,
            "solar": solar,
            "my_grid": station_profile.current.grid,
            "my_call": station_profile.current.call,
        }


# Module-level singleton, matching the rest of the codebase's convention.
propagation_source = PropagationSource()
