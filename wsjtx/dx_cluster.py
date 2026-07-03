"""
DX cluster / RBN listeners — real automated spotting for SSB/CW, which
WSJT-X's Decode feed can't provide (digital modes only).

Two separate networks, verified live (not guessed) by connecting directly
and reading real traffic:

1. Reverse Beacon Network (telnet.reversebeacon.net:7000) — automated CW/
   RTTY skimmers (SDR stations that auto-decode Morse/RTTY and post spots).
   Real line format, confirmed live:
     DX de RK3TD-2-#:  7031.00  CS2WWA         CW    21 dB  35 WPM  CQ      2335Z
   Reliable, machine-generated, consistent columns.

2. A DXSpider cluster node (dxspider.co.uk:7300) — the traditional,
   decades-old human-operated DX cluster network. This is the only real
   source for SSB spots, since there is no automated way to "decode" a
   voice signal into a spot — someone has to type it in. Real line format,
   confirmed live:
     DX de DL7JHN:     7074.0  ED7SUN       FT8 Special Summer Award 2026  2336Z
   No explicit mode column (unlike RBN) — this network carries spots for
   every mode humans choose to post, including digital ones already
   covered by the WSJT-X path. Mode is inferred from the frequency's
   position within the band's phone/CW/digital sub-band convention, which
   is approximate — a human-typed comment can say anything.

Both are read-only integrations: login with the operator's callsign (the
network's own convention, required just to receive spots) and never send
anything else — no posting spots, no chat. Same non-interference principle
as wsjtx/udp_listener.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

RBN_HOST, RBN_PORT = "telnet.reversebeacon.net", 7000
CLUSTER_HOST, CLUSTER_PORT = "dxspider.co.uk", 7300

RECONNECT_DELAY_S = 15

# Phone (SSB/voice) sub-bands, approximate per the US band plan — used only
# to infer mode on the DXSpider feed, which has no mode column of its own.
# Deliberately approximate: real band plans have narrower CW/data-only
# slices within these ranges (e.g. 20m phone is 14150-14350, not the whole
# 14000-14350 shown here), but a coarse per-band split is good enough for
# "probably SSB" without pretending to bandplan-level precision.
PHONE_SUBBANDS_HZ = [
    (1_843_000,  2_000_000),    # 160m phone
    (3_600_000,  4_000_000),    # 80m phone
    (7_125_000,  7_300_000),    # 40m phone
    (14_150_000, 14_350_000),   # 20m phone
    (18_110_000, 18_168_000),   # 17m phone
    (21_200_000, 21_450_000),   # 15m phone
    (24_930_000, 24_990_000),   # 12m phone
    (28_300_000, 29_700_000),   # 10m phone
    (50_100_000, 54_000_000),   # 6m phone
]


def infer_phone_mode(freq_hz: float) -> bool:
    """True if freq falls in a phone/SSB sub-band, by the approximation
    above. Used only for the DXSpider feed's mode-less spots."""
    return any(lo <= freq_hz <= hi for lo, hi in PHONE_SUBBANDS_HZ)


@dataclass
class ClusterSpot:
    source: str          # "rbn" | "cluster"
    spotter: str
    dx_call: str
    freq_hz: float
    mode: str             # "CW" | "SSB" | "" (unknown, DXSpider non-phone spot)
    comment: str
    raw: str


SpotCallback = Callable[[ClusterSpot], Coroutine]

# RBN: "DX de <spotter>-#:  <freq>  <call>         <mode>    <snr> dB  <wpm> WPM  <comment>  <time>Z"
_RBN_RE = re.compile(
    r"^DX de (?P<spotter>[A-Z0-9/-]+?)(?:-#)?:\s+"
    r"(?P<freq>[\d.]+)\s+"
    r"(?P<call>[A-Z0-9/]+)\s+"
    r"(?P<mode>CW|RTTY)\s+"
    r"(?P<snr>-?\d+)\s*dB\s+"
    r"(?P<wpm>\d+)\s*WPM\s+"
    r"(?P<comment>.*?)\s+"
    r"(?P<time>\d{4})Z\s*$"
)

# DXSpider: "DX de <spotter>:     <freq>  <call>       <comment>          <time>Z"
_CLUSTER_RE = re.compile(
    r"^DX de (?P<spotter>[A-Z0-9/-]+):\s+"
    r"(?P<freq>[\d.]+)\s+"
    r"(?P<call>[A-Z0-9/]+)\s+"
    r"(?P<comment>.*?)\s*"
    r"(?P<time>\d{4})Z\s*$"
)


def parse_rbn_line(line: str) -> Optional[ClusterSpot]:
    m = _RBN_RE.match(line.strip())
    if not m:
        return None
    return ClusterSpot(
        source="rbn",
        spotter=m.group("spotter"),
        dx_call=m.group("call"),
        freq_hz=float(m.group("freq")) * 1000,   # RBN reports kHz
        mode=m.group("mode"),
        comment=m.group("comment").strip(),
        raw=line,
    )


def parse_cluster_line(line: str) -> Optional[ClusterSpot]:
    m = _CLUSTER_RE.match(line.strip())
    if not m:
        return None
    freq_hz = float(m.group("freq")) * 1000   # DXSpider reports kHz
    return ClusterSpot(
        source="cluster",
        spotter=m.group("spotter"),
        dx_call=m.group("call"),
        freq_hz=freq_hz,
        mode="SSB" if infer_phone_mode(freq_hz) else "",
        comment=m.group("comment").strip(),
        raw=line,
    )


class _TelnetSpotClient:
    """Shared connect/reconnect/read-loop logic for both networks — they
    differ only in host/port/login-prompt-handling/line-parser."""

    def __init__(self, host: str, port: int, my_call: str, parser: Callable[[str], Optional[ClusterSpot]]):
        self._host = host
        self._port = port
        self._my_call = my_call
        self._parser = parser
        self._spot_callbacks: list[SpotCallback] = []
        self._task: Optional[asyncio.Task] = None
        self.connected = False

    def on_spot(self, cb: SpotCallback):
        self._spot_callbacks.append(cb)

    async def start(self):
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.connected = False

    async def _run_loop(self):
        while True:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{self._host}:{self._port} connection error: {e}")
            self.connected = False
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _connect_and_read(self):
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            # Both networks prompt for a callsign as their login step —
            # read until we see a ':' (the "call:" / "login:" prompt),
            # then send the callsign. Not parsing the exact prompt text
            # since it differs between the two networks and isn't stable
            # enough to match precisely.
            await asyncio.wait_for(reader.read(4096), timeout=10.0)
            writer.write((self._my_call + "\r\n").encode())
            await writer.drain()

            self.connected = True
            logger.info(f"Connected to {self._host}:{self._port} as {self._my_call}")

            while True:
                raw_line = await reader.readline()
                if not raw_line:
                    break   # connection closed
                line = raw_line.decode(errors="replace").rstrip()
                if not line.startswith("DX de"):
                    continue   # banner/MOTD/chat/other node traffic, not a spot
                spot = self._parser(line)
                if spot is None:
                    continue   # didn't match the expected column format — skip, don't crash the loop
                for cb in self._spot_callbacks:
                    try:
                        await cb(spot)
                    except Exception as e:
                        logger.error(f"DX cluster spot callback error: {e}")
        finally:
            writer.close()


class RbnClient(_TelnetSpotClient):
    def __init__(self, my_call: str):
        super().__init__(RBN_HOST, RBN_PORT, my_call, parse_rbn_line)


class DxClusterClient(_TelnetSpotClient):
    def __init__(self, my_call: str):
        super().__init__(CLUSTER_HOST, CLUSTER_PORT, my_call, parse_cluster_line)
