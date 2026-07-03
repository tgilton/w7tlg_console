"""
Manual callsign lookup — for SSB/CW operation, where there is no automated
decode/grid feed the way WSJT-X provides for digital modes. The operator
hears a callsign by ear or copies it on CW and types it in; this looks it
up against HamQTH and/or QRZ (whichever credentials are configured) and
also checks it against the watch list / worked-before data, the same
checks the live WSJT-X decode path does automatically.

Both services use a session-key XML API, verified directly against their
own current documentation (not guessed):
  HamQTH: https://www.hamqth.com/developers.php
  QRZ:    https://www.qrz.com/docs/xml/current_spec.html

HamQTH is tried first — it's free with no subscription tier gating which
fields come back, whereas QRZ restricts field coverage for non-Logbook-
Data-subscriber accounts. QRZ is used as a fallback/supplement, not the
primary source, given that.

Credentials come from HAMQTH_USERNAME/PASSWORD and QRZ_USERNAME/PASSWORD
in the environment (.env, loaded by main.py's load_dotenv()) — either or
both may be absent, in which case that service is silently skipped rather
than erroring, so this still works if only one account exists.
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HAMQTH_URL = "https://www.hamqth.com/xml.php"
QRZ_URL = "https://xmldata.qrz.com/xml/current/"
HAMQTH_NS = "{https://www.hamqth.com}"  # both services' responses are namespaced
QRZ_NS = "{http://xmldata.qrz.com}"

HAMQTH_SESSION_TTL_S = 55 * 60   # docs say 1hr; refresh a little early
QRZ_SESSION_TTL_S = 23 * 60 * 60  # docs say up to 24hr; refresh a little early

LOOKUP_CACHE_TTL_S = 300  # avoid re-querying the same callsign repeatedly in one session


@dataclass
class CallsignInfo:
    call: str
    found: bool
    name: str = ""
    grid: str = ""
    us_state: str = ""
    country: str = ""
    county: str = ""
    source: str = ""       # "hamqth" | "qrz" | "hamqth+qrz"
    error: str = ""


class _HamQthClient:
    def __init__(self):
        self._username = os.environ.get("HAMQTH_USERNAME")
        self._password = os.environ.get("HAMQTH_PASSWORD")
        self._session_id: Optional[str] = None
        self._session_at: float = 0.0

    @property
    def available(self) -> bool:
        return bool(self._username and self._password)

    def _tag(self, elem: ET.Element, name: str) -> Optional[str]:
        found = elem.find(f".//{HAMQTH_NS}{name}")
        return found.text if found is not None else None

    async def _login(self, client: httpx.AsyncClient) -> Optional[str]:
        if self._session_id and (time.time() - self._session_at) < HAMQTH_SESSION_TTL_S:
            return self._session_id
        resp = await client.get(HAMQTH_URL, params={"u": self._username, "p": self._password})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        session_id = self._tag(root, "session_id")
        error = self._tag(root, "error")
        if error:
            logger.warning(f"HamQTH login failed: {error}")
            return None
        self._session_id = session_id
        self._session_at = time.time()
        return session_id

    async def lookup(self, callsign: str) -> Optional[CallsignInfo]:
        if not self.available:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            session_id = await self._login(client)
            if not session_id:
                return None
            resp = await client.get(HAMQTH_URL, params={
                "id": session_id, "callsign": callsign, "prg": "w7tlg-console",
            })
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            error = self._tag(root, "error")
            if error:
                return CallsignInfo(call=callsign, found=False, source="hamqth", error=error)
            return CallsignInfo(
                call=callsign, found=True, source="hamqth",
                name=self._tag(root, "nick") or "",
                grid=self._tag(root, "grid") or "",
                us_state=self._tag(root, "us_state") or "",
                country=self._tag(root, "country") or "",
            )


class _QrzClient:
    def __init__(self):
        self._username = os.environ.get("QRZ_USERNAME")
        self._password = os.environ.get("QRZ_PASSWORD")
        self._session_key: Optional[str] = None
        self._session_at: float = 0.0

    @property
    def available(self) -> bool:
        return bool(self._username and self._password)

    def _tag(self, elem: ET.Element, name: str) -> Optional[str]:
        found = elem.find(f".//{QRZ_NS}{name}")
        return found.text if found is not None else None

    async def _login(self, client: httpx.AsyncClient) -> Optional[str]:
        if self._session_key and (time.time() - self._session_at) < QRZ_SESSION_TTL_S:
            return self._session_key
        resp = await client.get(QRZ_URL, params={
            "username": self._username, "password": self._password, "agent": "w7tlg-console",
        })
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        key = self._tag(root, "Key")
        error = self._tag(root, "Error")
        if error:
            logger.warning(f"QRZ login failed: {error}")
            return None
        self._session_key = key
        self._session_at = time.time()
        return key

    async def lookup(self, callsign: str) -> Optional[CallsignInfo]:
        if not self.available:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            session_key = await self._login(client)
            if not session_key:
                return None
            resp = await client.get(QRZ_URL, params={"s": session_key, "callsign": callsign})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            error = self._tag(root, "Error")
            if error:
                return CallsignInfo(call=callsign, found=False, source="qrz", error=error)
            fname = self._tag(root, "fname") or ""
            lname = self._tag(root, "name") or ""
            return CallsignInfo(
                call=callsign, found=True, source="qrz",
                name=(fname + " " + lname).strip(),
                grid=self._tag(root, "grid") or "",
                us_state=self._tag(root, "state") or "",
                country=self._tag(root, "country") or "",
                county=self._tag(root, "county") or "",
            )


class CallsignLookup:
    def __init__(self):
        self._hamqth = _HamQthClient()
        self._qrz = _QrzClient()
        self._cache: dict[str, tuple[float, CallsignInfo]] = {}

    @property
    def any_available(self) -> bool:
        return self._hamqth.available or self._qrz.available

    async def lookup(self, callsign: str) -> CallsignInfo:
        callsign = callsign.upper().strip()
        cached = self._cache.get(callsign)
        if cached and (time.time() - cached[0]) < LOOKUP_CACHE_TTL_S:
            return cached[1]

        result: Optional[CallsignInfo] = None
        try:
            hq = await self._hamqth.lookup(callsign)
        except Exception as e:
            logger.warning(f"HamQTH lookup error for {callsign}: {e}")
            hq = None

        if hq and hq.found:
            result = hq
            # Supplement with QRZ's county field if HamQTH didn't have one
            # and QRZ is available — best-effort, not required.
            if self._qrz.available and not hq.county:
                try:
                    qz = await self._qrz.lookup(callsign)
                    if qz and qz.found and qz.county:
                        result.county = qz.county
                        result.source = "hamqth+qrz"
                except Exception as e:
                    logger.debug(f"QRZ supplement lookup error for {callsign}: {e}")
        else:
            try:
                qz = await self._qrz.lookup(callsign)
            except Exception as e:
                logger.warning(f"QRZ lookup error for {callsign}: {e}")
                qz = None
            result = qz or CallsignInfo(
                call=callsign, found=False,
                error="Not found (or no lookup service configured)",
            )

        self._cache[callsign] = (time.time(), result)
        return result


# Module-level singleton, matching the rest of the codebase's convention.
callsign_lookup = CallsignLookup()
