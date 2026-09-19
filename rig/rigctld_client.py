"""
Rigctld Client — FT-991A State Polling and Control

All controls and meters use standard Hamlib 'l' (get_level) and
'L' (set_level) commands — no raw CAT passthrough needed.

Verified working levels on FT-991A via Hamlib 4.7.1:
  STRENGTH  S-meter (dB above S9, negative = below S9)
  RFPOWER   TX power 0.0-1.0
  ALC       ALC 0.0-1.0
  SWR       SWR 1.0+
  PREAMP    0=IPO, 1=AMP1, 2=AMP2
  COMP      Compression 0.0-1.0
  MICGAIN   Mic gain 0.0-1.0
  IF        IF shift Hz
  NB        Noise blanker level
  NR        Noise reduction 0.0-1.0
  ATT       Attenuator (0=off)
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Coroutine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Band / frequency definitions
# ---------------------------------------------------------------------------

class Band(Enum):
    B160M   = "160m"
    B80M    = "80m"
    B60M    = "60m"
    B40M    = "40m"
    B30M    = "30m"
    B20M    = "20m"
    B17M    = "17m"
    B15M    = "15m"
    B12M    = "12m"
    B10M    = "10m"
    B6M     = "6m"
    B2M     = "2m"
    B70CM   = "70cm"
    UNKNOWN = "??"

BAND_EDGES = [
    (1_800_000,   2_000_000,   Band.B160M),
    (3_500_000,   4_000_000,   Band.B80M),
    (5_000_000,   5_500_000,   Band.B60M),
    (7_000_000,   7_300_000,   Band.B40M),
    (10_100_000,  10_150_000,  Band.B30M),
    (14_000_000,  14_350_000,  Band.B20M),
    (18_068_000,  18_168_000,  Band.B17M),
    (21_000_000,  21_450_000,  Band.B15M),
    (24_890_000,  24_990_000,  Band.B12M),
    (28_000_000,  29_700_000,  Band.B10M),
    (50_000_000,  54_000_000,  Band.B6M),
    (144_000_000, 148_000_000, Band.B2M),
    (432_000_000, 450_000_000, Band.B70CM),
]

# Each band lists every standard-calling-frequency digital mode this
# station operates there — FT8 always, plus JS8Call's published dial
# frequencies (js8call.com) on the HF bands where JS8 has one. Tuple
# rather than a single value so the "near digital freq" badge lights up
# for either mode's calling channel, not just FT8's.
DIGITAL_FREQS: dict[Band, tuple[int, ...]] = {
    Band.B160M: (1_840_000, 1_842_000),
    Band.B80M:  (3_573_000, 3_578_000),
    Band.B60M:  (5_357_000,),
    Band.B40M:  (7_074_000, 7_078_000),
    Band.B30M:  (10_136_000, 10_130_000),
    Band.B20M:  (14_074_000, 14_078_000),
    Band.B17M:  (18_100_000, 18_104_000),
    Band.B15M:  (21_074_000, 21_078_000),
    Band.B12M:  (24_915_000, 24_922_000),
    Band.B10M:  (28_074_000, 28_078_000),
    Band.B6M:   (50_313_000, 50_318_000),
    Band.B2M:   (144_174_000,),
    Band.B70CM: (432_174_000,),
}

BAND_DEFAULT_FREQ = {
    Band.B160M: 1_840_000,
    Band.B80M:  3_573_000,
    Band.B60M:  5_357_000,
    Band.B40M:  7_074_000,
    Band.B30M:  10_136_000,
    Band.B20M:  14_074_000,
    Band.B17M:  18_100_000,
    Band.B15M:  21_074_000,
    Band.B12M:  24_915_000,
    Band.B10M:  28_074_000,
    Band.B6M:   50_313_000,
    Band.B2M:   144_200_000,
    Band.B70CM: 432_100_000,
}

# Bands where this rig's Hamlib backend doesn't implement ATT-level or
# NR/ANF-function queries at all — confirmed live 2026-07-22 via direct
# rigctld query: RPRT -9 (ENAVAIL) in BOTH FM and USB while on 2m, so this
# is a band capability gap (likely a separate VHF/UHF front-end module
# without an attenuator), not a mode-specific one.
NO_ATT_NR_ANF_BANDS = {Band.B2M.value, Band.B70CM.value}

def freq_to_band(freq_hz: int) -> Band:
    for lo, hi, band in BAND_EDGES:
        if lo <= freq_hz <= hi:
            return band
    return Band.UNKNOWN

# ---------------------------------------------------------------------------
# Tier A: hardware-range capability check (T1) — NOT a band-plan restriction.
#
# This bounds what the FT-991A can physically tune to at all, per Yaesu's
# service-manual RX coverage spec — three separate, non-contiguous bands,
# NOT one contiguous span from lowest to highest:
#   30 kHz  -  56 MHz
#   118 MHz - 164 MHz
#   420 MHz - 470 MHz
# (76-108 MHz WFM broadcast coverage is intentionally excluded — this
# console never sets WFM.) A single min/max envelope previously used here
# silently accepted the two dead zones between these bands (56-118 MHz,
# 164-420 MHz) that the rig cannot actually receive on — Tier A is a
# hardware-*capability* check, so it must reject those too, not just
# obvious garbage. The point is still to reject a value the rig cannot
# honor, not to restrict where the operator can listen or transmit within
# what it CAN honor — WWV/WWVH/CHU and other reference/beacon work all
# fall inside the first (HF/6m) band here. Deliberately a separate table
# from FREQ_SANITY_MIN_HZ/MAX_HZ above: that pair exists to catch a
# desynced *reply* stream on the polling/read side and is a single padded
# span on purpose; this table exists to reject a *write* the rig cannot
# honor. Deliberately NOT merged with BAND_EDGES/freq_to_band either —
# that table is the amateur band-plan used for display and, separately,
# for the advisor-only Tier B guard below.
HW_RX_COVERAGE_BANDS = (
    (30_000, 56_000_000),
    (118_000_000, 164_000_000),
    (420_000_000, 470_000_000),
)

def is_valid_hw_frequency(freq_hz: int) -> bool:
    """Tier A frequency check — pure hardware-capability bound, applies to
    every write path (manual UI and the advisor alike)."""
    return any(lo <= freq_hz <= hi for lo, hi in HW_RX_COVERAGE_BANDS)

# Every mode string this codebase actually sends to rig.set_mode today
# (dashboard/console.html's mode buttons, RigState.update_derived's
# digital_modes) plus CWR, which the advisor's own tool schema already
# offers as an option. Deliberately excludes "DATA-U"/"DATA-L" from that
# same schema (advisor/claude_advisor.py's QSY_TOOL enum) — those are this
# console's own UI button labels, not real rigctld mode strings, and were
# never valid input to rig.set_mode in the first place.
HW_VALID_MODES = frozenset({
    "USB", "LSB", "CW", "CWR", "AM", "FM", "PKTUSB", "PKTLSB",
})

def is_valid_hw_mode(mode: str) -> bool:
    """Tier A mode check — pure hardware-capability bound, applies to every
    write path (manual UI and the advisor alike)."""
    return mode in HW_VALID_MODES

def freq_display(freq_hz: int) -> str:
    mhz = freq_hz // 1_000_000
    khz = (freq_hz % 1_000_000) // 1_000
    hz  = freq_hz % 1_000
    return f"{mhz:3d}.{khz:03d}.{hz:03d}"

def smeter_label(strength_db: float) -> str:
    """Convert Hamlib STRENGTH (dB re S9) to label like S7, S9+20."""
    # STRENGTH is dB above S9. S9 = 0, S8 = -6, S7 = -12, etc.
    # Above S9: +10 = 10dB, +20 = 20dB, etc.
    if strength_db >= 0:
        over = round(strength_db / 10) * 10
        return f"S9+{over}" if over > 0 else "S9"
    else:
        s = max(0, min(9, 9 + int(strength_db / 6)))
        return f"S{s}"

# ---------------------------------------------------------------------------
# Rig state
# ---------------------------------------------------------------------------

@dataclass
class RigState:
    # Connection
    connected: bool     = False

    # VFO
    freq_hz: int        = 0
    freq_display: str   = "  0.000.000"
    band: str           = "??"
    mode: str           = ""
    passband_hz: int    = 0

    # TX state
    ptt: bool           = False
    split: bool         = False
    tx_freq_hz: int     = 0

    # Meters — RX
    strength_db: float  = -54.0   # dB re S9 (negative = below S9)
    smeter_label: str   = "S0"

    # Meters — TX
    alc: float          = 0.0     # 0.0-1.0
    rf_power_out: float = 0.0     # 0.0-1.0 (radio's own PO meter)
    swr_radio: float    = 1.0

    # Controls
    rf_power_pct: int   = 50      # 0-100 (derived from RFPOWER 0.0-1.0)
    preamp: int         = 0       # 0=IPO, 1=AMP1, 2=AMP2
    preamp_name: str    = "IPO"
    att_db: int         = 0       # 0=off
    if_shift_hz: int    = 0
    nb_level: float     = 0.0
    nr_level: float     = 0.0
    comp_level: float   = 0.0
    mic_gain: float     = 0.0
    agc: int            = 3       # 0=OFF, 2=FAST, 3=SLOW
    nb_on: bool         = False   # NB func on/off
    nr_on: bool         = False   # NR func on/off
    dnf_on: bool        = False   # ANF (auto-notch) on/off
    dt_gain: Optional[int] = None  # DATA OUT LEVEL (CAT menu 073), 0-100; None until first poll
    ssb_tx_bpf: Optional[int] = None  # SSB TX BPF (CAT menu 110), 0-4; None until first poll

    # Derived
    is_digital: bool        = False
    near_digital_freq: bool = False

    def to_dict(self) -> dict:
        return {
            "connected":        self.connected,
            "freq_hz":          self.freq_hz,
            "freq_display":     self.freq_display,
            "band":             self.band,
            "mode":             self.mode,
            "passband_hz":      self.passband_hz,
            "ptt":              self.ptt,
            "split":            self.split,
            "tx_freq_hz":       self.tx_freq_hz,
            "strength_db":      self.strength_db,
            "smeter_label":     self.smeter_label,
            "alc":              self.alc,
            "rf_power_out":     self.rf_power_out,
            "swr_radio":        self.swr_radio,
            "rf_power_pct":     self.rf_power_pct,
            "preamp":           self.preamp,
            "preamp_name":      self.preamp_name,
            "att_db":           self.att_db,
            "if_shift_hz":      self.if_shift_hz,
            "nb_level":         self.nb_level,
            "nr_level":         self.nr_level,
            "comp_level":       self.comp_level,
            "mic_gain":         self.mic_gain,
            "agc":              self.agc,
            "nb_on":            self.nb_on,
            "nr_on":            self.nr_on,
            "dnf_on":           self.dnf_on,
            "dt_gain":          self.dt_gain,
            "ssb_tx_bpf":       self.ssb_tx_bpf,
            "is_digital":       self.is_digital,
            "near_digital_freq": self.near_digital_freq,
        }

    def update_derived(self):
        band_enum = freq_to_band(self.freq_hz)
        self.band = band_enum.value
        self.freq_display = freq_display(self.freq_hz)
        self.smeter_label = smeter_label(self.strength_db)
        self.preamp_name = {0: "IPO", 1: "AMP1", 2: "AMP2"}.get(
            self.preamp, "IPO")

        # Real Hamlib mode strings for this rig's DATA-U/DATA-L (confirmed via
        # `rigctl --dump-caps -m 1035`) — plain USB/LSB are voice, not digital.
        digital_modes = {"PKTUSB", "PKTLSB"}
        self.is_digital = self.mode in digital_modes

        std_freqs = DIGITAL_FREQS.get(band_enum, ())
        self.near_digital_freq = bool(
            self.freq_hz > 0
            and any(abs(self.freq_hz - f) < 2000 for f in std_freqs))


# ---------------------------------------------------------------------------
# Rigctld client
# ---------------------------------------------------------------------------

StateCallback = Callable[[RigState], Coroutine]

# How often to poll each group (in poll cycles, each cycle = poll_interval)
# PTT is always first; freq and mode are skipped during TX (see _poll_state).
FREQ_EVERY   = 1   # Every cycle (RX only)
MODE_EVERY   = 2   # Every 2 cycles (RX only)
PTT_EVERY    = 1   # Every cycle
METER_EVERY  = 1   # Every cycle
CONTROL_EVERY = 10  # Every 10 cycles (~5s)

# Sanity bounds for the frequency reading — wide enough to cover this
# rig's full general-coverage receive range (HF/6m/2m/70cm) with margin,
# narrow enough to catch an implausible value like "1 Hz" that can only
# mean the reply stream has drifted out of alignment (see _poll_state).
FREQ_SANITY_MIN_HZ = 10_000
FREQ_SANITY_MAX_HZ = 500_000_000

# Strict PTT reply validation — SAFETY CRITICAL. See IMPLEMENTATION_PLAN_U2.md
# §6b finding I (phantom PTT).
#
# Hamlib's `t` reply is a single character from rig.h's ptt_t enum:
#   0 = RIG_PTT_OFF   1 = RIG_PTT_ON   2 = RIG_PTT_ON_MIC   3 = RIG_PTT_ON_DATA
# Nothing else is a PTT reply. The old code ran the raw line through
# float() and then bool(int(...)), which accepted plenty of things that
# are not PTT at all — and when the reply stream is shifted (the whole
# point of finding G/I), the value sitting in the `t` slot is some OTHER
# command's reply. Every one of these was previously read as "TX ON":
#
#   "1.0"       l SWR / l RFPOWER reply    -> float 1.0  -> True   PHANTOM
#   "-73"       l STRENGTH reply (dB)      -> float -73  -> True   PHANTOM
#   "14074000"  f (frequency) reply        -> float      -> True   PHANTOM
#
# A phantom TX is not cosmetic. It gates the SDR audio, freezes the
# panadapter, drives the TX meters, pushes a TX-start to the amp bridge,
# and — because `f` is only polled while PTT is false — switches OFF the
# frequency sanity check, which is the only other desync guard in the
# poll loop. Observed live 2026-09-19 holding a fake TX for up to 37 s.
#
# fullmatch, not match: `$` in Python also matches just before a trailing
# newline, so match(r'^[0-3]$', '1\n') would succeed.
_PTT_REPLY_RE = re.compile(r'[0-3]')


def parse_ptt_reply(raw: Optional[str]) -> Optional[bool]:
    """Parse a raw rigctld `t` reply into a PTT boolean.

    Returns True/False for a well-formed reply, and None for anything
    else — None means "no reading", NOT "receive". Callers must hold the
    previous PTT state on None rather than treating it as RX: forcing RX
    on a bad read would drop the TX gate mid-transmission, exposing the
    SDR front end during real RF, which is the opposite failure and a
    worse one."""
    if raw is None:
        return None
    if not _PTT_REPLY_RE.fullmatch(raw.strip()):
        return None
    return raw.strip() != '0'

# rigctld's own daemon-level response cache (see _set_daemon_cache_timeout) —
# default 1000ms made knob tuning feel like it updated once a second. Low
# enough to track the knob smoothly, well above 0 so a burst of near-
# simultaneous queries within one poll cycle can still share one real serial
# read rather than each forcing its own.
RIGCTLD_DAEMON_CACHE_MS = 50


class RigctldClient:

    def __init__(
        self,
        host: str = '127.0.0.1',
        port: int = 4532,
        poll_interval: float = 0.5,
        reconnect_interval: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.reconnect_interval = reconnect_interval

        self.state = RigState()
        self._running = False
        self._lock = asyncio.Lock()
        self._state_callbacks: list[StateCallback] = []
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._cycle = 0
        # Count of PTT replies rejected as implausible since the last
        # connect (finding I). Reset per connection so the number in the
        # log describes the current link, not the whole session.
        self._ptt_rejects = 0
        self._dt_gain_task: Optional[asyncio.Task] = None
        self._ssb_bpf_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_state_change(self, cb: StateCallback):
        self._state_callbacks.append(cb)

    async def start(self):
        self._running = True
        asyncio.create_task(self._run())
        logger.info(f"RigctldClient started → {self.host}:{self.port}")

    async def stop(self):
        self._running = False
        await self._disconnect()

    # Standard VFO controls
    async def set_frequency(self, freq_hz: int) -> bool:
        return await self._send_set(f"F {freq_hz}\n")

    async def set_mode(self, mode: str, passband: int = 0) -> bool:
        return await self._send_set(f"M {mode} {passband}\n")

    async def set_ptt(self, active: bool) -> bool:
        return await self._send_set(f"T {1 if active else 0}\n")

    # Level controls (Hamlib 'L' command)
    async def set_rf_power(self, pct: int) -> bool:
        """Set TX power 0-100%."""
        val = max(0.0, min(1.0, pct / 100.0))
        ok = await self._send_set(f"L RFPOWER {val:.3f}\n")
        if ok:
            self.state.rf_power_pct = pct
            await self._fire_callbacks()
        return ok

    async def set_preamp(self, level: int) -> bool:
        """Set preamp: 0=IPO, 1=AMP1, 2=AMP2."""
        db = {0: 0, 1: 10, 2: 20}.get(level, 0)
        ok = await self._send_set(f"L PREAMP {db}\n")
        if ok:
            self.state.preamp = level
            self.state.update_derived()
            await self._fire_callbacks()
        return ok

    async def set_att(self, db: int) -> bool:
        """Set attenuator. FT-991A/hamlib only accepts 0 (off) or 12 (12dB)
        — confirmed by live testing 2026-07-14; 6 and 18 are RPRT-rejected
        by rigctld despite being generic attenuator values on other rigs."""
        ok = await self._send_set(f"L ATT {db}\n")
        if ok:
            self.state.att_db = db
            await self._fire_callbacks()
        return ok

    async def set_if_shift(self, hz: int) -> bool:
        ok = await self._send_set(f"L IF {hz}\n")
        if ok:
            self.state.if_shift_hz = hz
            await self._fire_callbacks()
        return ok

    async def set_nb(self, level: float) -> bool:
        ok = await self._send_set(f"L NB {level:.2f}\n")
        if ok:
            self.state.nb_level = level
            await self._fire_callbacks()
        return ok

    async def set_nr(self, level: float) -> bool:
        ok = await self._send_set(f"L NR {level:.3f}\n")
        if ok:
            self.state.nr_level = level
            await self._fire_callbacks()
        return ok

    async def set_mic_gain(self, level: float) -> bool:
        """Set mic gain, 0.0-1.0."""
        ok = await self._send_set(f"L MICGAIN {level:.3f}\n")
        if ok:
            self.state.mic_gain = level
            await self._fire_callbacks()
        return ok

    async def set_comp(self, level: float) -> bool:
        """Set speech compression level, 0.0-1.0."""
        ok = await self._send_set(f"L COMP {level:.3f}\n")
        if ok:
            self.state.comp_level = level
            await self._fire_callbacks()
        return ok

    async def set_agc(self, value: int) -> bool:
        """Set AGC: 0=OFF, 2=FAST, 3=SLOW."""
        ok = await self._send_set(f"L AGC {value}\n")
        if ok:
            self.state.agc = value
            await self._fire_callbacks()
        return ok

    async def set_nb_on(self, on: bool) -> bool:
        """Toggle Noise Blanker on/off."""
        ok = await self._send_set(f"U NB {1 if on else 0}\n")
        if ok:
            self.state.nb_on = on
            await self._fire_callbacks()
        return ok

    async def set_nr_on(self, on: bool) -> bool:
        """Toggle Noise Reduction on/off."""
        ok = await self._send_set(f"U NR {1 if on else 0}\n")
        if ok:
            self.state.nr_on = on
            await self._fire_callbacks()
        return ok

    async def set_dnf_on(self, on: bool) -> bool:
        """Toggle Auto-Notch Filter on/off."""
        ok = await self._send_set(f"U ANF {1 if on else 0}\n")
        if ok:
            self.state.dnf_on = on
            await self._fire_callbacks()
        return ok

    # Raw CAT passthrough — for params Hamlib doesn't expose as a standard
    # level on this rig (confirmed via `rigctl --dump-caps -m 1035`).
    async def send_raw_cmd(self, cmd: str) -> Optional[str]:
        """Send a raw CAT command via Hamlib's passthrough ('w'). Unlike
        cached Hamlib level/func GETs, passthrough always forces a fresh
        serial round-trip to the radio, so it can take longer under load
        from rigctld's own internal polling and other clients (e.g.
        WSJT-X) sharing the same serial link — give it more slack than the
        regular 2s GET timeout. Returns the radio's raw reply line (e.g.
        "EX073030;") or None."""
        lines = await self._send_get(f"w {cmd}\n", n_lines=1, timeout=5.0)
        return lines[0] if lines else None

    async def get_dt_gain(self) -> Optional[int]:
        """Read CAT menu 073 ("DATA OUT LEVEL", the digital-mode TX audio
        drive level operators call "DT GAIN" — not menu 049 "AM DATA GAIN",
        which is unrelated/AM-only). Range 0-100.

        Live-verified against the real FT-991A over a raw socket: `w
        EX073;` replies with the rig's own echo terminated by a NUL byte
        — e.g. b'EX073010;\\x00' — NOT a newline, and rigctld does not
        append its own RPRT line for this passthrough. `readline()` waits
        for '\\n' and never sees one here, so it silently timed out on
        every call; `readuntil(b'\\x00')` is what actually matches the
        wire format.

        Uses its OWN short-lived connection rather than the shared poll
        connection/lock: this passthrough can take several seconds under
        contention with WSJT-X on the radio's serial link (see
        send_raw_cmd), and blocking the shared connection that long would
        delay the PTT/frequency broadcasts the SDR Switch freeze and
        panadapter depend on being prompt — confirmed live as a multi-
        second delay in the panadapter showing real (leakage) signal after
        a transmission actually started."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=2.0)
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            writer.write(b"w EX073;\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b'\x00'), timeout=5.0)
            reply = raw.decode(errors='replace').rstrip('\x00').strip()
        except (asyncio.TimeoutError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError, ConnectionResetError, OSError):
            return None
        finally:
            writer.close()
        if reply and reply.startswith("EX073"):
            try:
                return int(reply[5:8])
            except ValueError:
                pass
        return None

    async def set_dt_gain(self, value: int) -> bool:
        """Set DT GAIN (CAT menu 073, "DATA OUT LEVEL"), 0-100.

        Uses _send_raw_ex_set, NOT _send_set — see that method's docstring.
        _send_set reads until an "RPRT" line, which never arrives for this
        passthrough (see get_dt_gain), so every call used to hang for the
        full 2s timeout and then mark the whole rig disconnected on every
        DT GAIN change (silently, since a timed-out slider drag doesn't
        show an error — but it was tearing down and reconnecting the
        rigctld link every time)."""
        value = max(0, min(100, int(value)))
        ok = await self._send_raw_ex_set(f"w EX073{value:03d};\n")
        if ok:
            self.state.dt_gain = value
            await self._fire_callbacks()
        return ok

    # SSB TX BPF (CAT menu 110) — the radio's built-in TX audio bandpass
    # presets. Confirmed against the FT-991A CAT Operation Reference Manual:
    #   0: 50-3000 Hz   1: 100-2900 Hz   2: 200-2800 Hz
    #   3: 300-2700 Hz  4: 400-2600 Hz
    # Single P2 digit, unlike DT GAIN's three — `EX110{n};` not `EX110{n:03d};`.
    SSB_TX_BPF_PRESETS = {
        0: (50, 3000),
        1: (100, 2900),
        2: (200, 2800),
        3: (300, 2700),
        4: (400, 2600),
    }

    async def get_ssb_tx_bpf(self) -> Optional[int]:
        """Read CAT menu 110 ("SSB TX BPF"). Returns 0-4 or None.
        Own short-lived connection, same rationale as get_dt_gain. Reply
        framing is the same NUL-terminated-no-newline echo as EX073 —
        confirmed live: `w EX110;` → b'EX1100;\\x00' — see get_dt_gain."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=2.0)
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            writer.write(b"w EX110;\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b'\x00'), timeout=5.0)
            reply = raw.decode(errors='replace').rstrip('\x00').strip()
        except (asyncio.TimeoutError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError, ConnectionResetError, OSError):
            return None
        finally:
            writer.close()
        if reply and reply.startswith("EX110"):
            try:
                return int(reply[5:6])
            except ValueError:
                pass
        return None

    async def set_ssb_tx_bpf(self, value: int) -> bool:
        """Set SSB TX BPF (CAT menu 110) to one of the 5 radio presets, 0-4.
        Uses _send_raw_ex_set, NOT _send_set — see that method's docstring
        (this passthrough never sends an RPRT line, so _send_set always
        timed out and tore down the rig connection on every click)."""
        if value not in self.SSB_TX_BPF_PRESETS:
            return False
        ok = await self._send_raw_ex_set(f"w EX110{value:01d};\n")
        if ok:
            self.state.ssb_tx_bpf = value
            await self._fire_callbacks()
        return ok

    # ------------------------------------------------------------------
    # Internal: run / connect / disconnect
    # ------------------------------------------------------------------

    async def _run(self):
        while self._running:
            if await self._connect():
                await self._poll_loop()
            if self._running:
                if self.state.connected:
                    self.state.connected = False
                    await self._fire_callbacks()
                logger.info(
                    f"Reconnecting to rigctld in {self.reconnect_interval}s...")
                await asyncio.sleep(self.reconnect_interval)

    async def _connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=3.0)
            self.state.connected = True
            self._cycle = 0
            self._ptt_rejects = 0
            logger.info(f"Connected to rigctld at {self.host}:{self.port}")
            await self._set_daemon_cache_timeout()
            await self._fire_callbacks()
            return True
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"Cannot connect to rigctld: {e}")
            return False

    async def _set_daemon_cache_timeout(self):
        """Hamlib's rigctld daemon caches get_freq/get_mode/etc. replies for
        --get_cache/--set_cache msecs (distinct from, and layered on top of,
        the per-rig-backend 'cache_timeout' conf param) — default 1000ms on
        this rigctld build. That cache is what made the knob feel like it
        was updating once a second while a console-initiated SET (which
        writes straight through the cache) felt instant — confirmed live
        2026-08-30 by measuring get_freq cadence directly against rigctld,
        bypassing this app's own poll loop entirely. There's no startup CLI
        flag for it, so it has to be set over the wire on every connection —
        it resets to the daemon default whenever rigctld itself restarts."""
        try:
            self._writer.write(f"\\set_cache {RIGCTLD_DAEMON_CACHE_MS}\n".encode())
            await self._writer.drain()
            reply = await asyncio.wait_for(self._reader.readline(), timeout=2.0)
            if reply.decode(errors='replace').strip() != "RPRT 0":
                logger.warning(f"rigctld set_cache reply unexpected: {reply!r}")
        except (asyncio.TimeoutError, ConnectionResetError, OSError) as e:
            logger.warning(f"Could not set rigctld daemon cache timeout: {e}")

    async def _disconnect(self):
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    # ------------------------------------------------------------------
    # Internal: polling
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        # _disconnect() always runs on the way out, however the loop ends —
        # whether via this try/except (an exception mid-cycle) or via the
        # while-condition itself going false (connected was set False by a
        # GET/SET's own ConnectionResetError/OSError handler, which returns
        # normally rather than raising). Either way the socket needs
        # closing before _run() tries to reconnect, not left dangling.
        try:
            while self._running and self.state.connected:
                try:
                    changed = await self._poll_state()
                    if changed:
                        await self._fire_callbacks()
                    await asyncio.sleep(self.poll_interval)
                except Exception as e:
                    logger.error(f"Poll error: {e}")
                    self.state.connected = False
                    return
        finally:
            await self._disconnect()

    async def _poll_state(self) -> bool:
        self._cycle += 1
        changed = False

        # PTT — polled first every cycle so that the frequency and mode
        # freezes below can gate on the current state of PTT without waiting
        # for the next cycle.
        #
        # Validated strictly (see parse_ptt_reply): a reply that is not a
        # bare 0-3 is some other command's reply that landed here after a
        # desync, and must NOT be allowed to fake a transmission. An
        # unparseable reply holds the previous PTT state rather than
        # forcing RX — see parse_ptt_reply's docstring for why that
        # direction matters.
        lines = await self._send_get("t\n", n_lines=1)
        raw_ptt = lines[0] if lines else None
        ptt = parse_ptt_reply(raw_ptt)
        if ptt is None:
            if raw_ptt is not None:
                # WARNING, not debug: this is the phantom-PTT guard firing,
                # and how often it fires is the measurement that tells us
                # whether the underlying desync is getting better or worse.
                self._ptt_rejects += 1
                logger.warning(
                    f"Rejected implausible PTT reply {raw_ptt!r} — holding "
                    f"PTT={self.state.ptt} (reply stream likely desynced; "
                    f"{self._ptt_rejects} rejected since connect)")
        elif ptt != self.state.ptt:
            self.state.ptt = ptt
            changed = True

        # Frequency — frozen during TX.
        # In WSJT-X split mode, the TX VFO-B can be on a different frequency
        # (and even a different band) than the RX VFO-A.  Broadcasting VFO-B
        # during TX would update the console display, trigger a SDR retune,
        # and — most critically — trigger an amp band-select command to the
        # wrong band while RF is live.  Hold the last known RX frequency.
        if not self.state.ptt:
            val = await self._get_float("f\n", n_lines=1)
            if val is not None:
                freq = int(val)
                if not (FREQ_SANITY_MIN_HZ <= freq <= FREQ_SANITY_MAX_HZ):
                    # An implausible reading (e.g. a stray single-digit value)
                    # means a reply meant for a different command — most
                    # likely a late one _drain_stale_reply() failed to fully
                    # mop up — landed here instead, and every read after it
                    # would stay silently shifted by the same offset forever.
                    # Force a clean reconnect rather than keep broadcasting
                    # (and acting on) garbage indefinitely.
                    raise RuntimeError(
                        f"Implausible frequency reading ({freq} Hz) — "
                        f"rig reply stream likely desynced")
                if freq != self.state.freq_hz:
                    self.state.freq_hz = freq
                    changed = True

        # Mode and split — every other cycle; frozen during TX.
        # The FT-991A reports a spurious mode (often CW for VFO-B) while
        # PTT is active in WSJT-X split operation.  If we let that through,
        # is_digital flips False, the audio chain exits digital mode, and
        # the panadapter drops to low-resolution wideband rendering for the
        # whole transmission.  Hold the last known pre-TX values instead.
        if self._cycle % MODE_EVERY == 0 and not self.state.ptt:
            lines = await self._send_get("m\n", 2)
            if lines and len(lines) >= 2:
                try:
                    mode = lines[0].strip()
                    pb = int(lines[1].strip())
                    if mode != self.state.mode or pb != self.state.passband_hz:
                        self.state.mode = mode
                        self.state.passband_hz = pb
                        changed = True
                except (ValueError, IndexError):
                    pass

            lines = await self._send_get("s\n", 2)
            if lines:
                try:
                    split = bool(int(lines[0]))
                    if split != self.state.split:
                        self.state.split = split
                        changed = True
                except (ValueError, IndexError):
                    pass

        # Meters — every cycle
        # S-meter during RX, ALC/SWR during TX
        if not self.state.ptt:
            val = await self._get_level("STRENGTH")
            if val is not None and abs(val - self.state.strength_db) > 1.0:
                self.state.strength_db = val
                changed = True
        else:
            val = await self._get_level("ALC")
            if val is not None and abs(val - self.state.alc) > 0.01:
                self.state.alc = val
                changed = True

            val = await self._get_level("RFPOWER")
            if val is not None and abs(val - self.state.rf_power_out) > 0.01:
                self.state.rf_power_out = val
                changed = True

            val = await self._get_level("SWR")
            if val is not None and abs(val - self.state.swr_radio) > 0.05:
                self.state.swr_radio = round(val, 2)
                changed = True

        # Controls — slow poll every ~5s
        if self._cycle % CONTROL_EVERY == 0:
            ctrl_changed = await self._poll_controls()
            changed = changed or ctrl_changed

        if changed:
            self.state.update_derived()

        return changed

    async def _poll_controls(self) -> bool:
        changed = False

        # RF power setting
        val = await self._get_level("RFPOWER")
        if val is not None:
            pct = round(val * 100)
            if pct != self.state.rf_power_pct:
                self.state.rf_power_pct = pct
                changed = True

        # Preamp — Hamlib returns dB: 0=IPO, 10=AMP1, 20=AMP2
        val = await self._get_level("PREAMP")
        if val is not None:
            db = int(val)
            # Map dB to index: 0→0, 10→1, 20→2
            preamp = {0: 0, 10: 1, 20: 2}.get(db, 0)
            if preamp != self.state.preamp:
                self.state.preamp = preamp
                changed = True

        # ATT — see NO_ATT_NR_ANF_BANDS: this rig's Hamlib backend returns
        # RPRT -9 (ENAVAIL) for this on 2m/70cm regardless of mode, same
        # "unsupported here" class as the MICGAIN/COMP-in-digital-mode skip
        # above; skip it rather than eat a client-side timeout waiting on
        # an answer that will always be "not available" on these bands.
        val = None if self.state.band in NO_ATT_NR_ANF_BANDS else await self._get_level("ATT")
        if val is not None:
            att = int(val)
            if att != self.state.att_db:
                self.state.att_db = att
                changed = True

        # IF shift
        val = await self._get_level("IF")
        if val is not None:
            if_hz = int(val)
            if if_hz != self.state.if_shift_hz:
                self.state.if_shift_hz = if_hz
                changed = True

        # NB, NR — always applicable.
        # COMP, MICGAIN — voice-mode controls; skip in digital mode: the
        # FT-991A doesn't respond to MICGAIN queries in PKTUSB/DATA-U and
        # times out every poll, wasting ~2s of serial-link time and blocking
        # the PTT poll behind it.
        for attr, level_name in [
            ('nb_level', 'NB'),
            ('nr_level', 'NR'),
            ('comp_level', 'COMP'),
            ('mic_gain', 'MICGAIN'),
        ]:
            if level_name in ('COMP', 'MICGAIN') and self.state.is_digital:
                continue
            val = await self._get_level(level_name)
            if val is not None and abs(val - getattr(self.state, attr)) > 0.01:
                setattr(self.state, attr, val)
                changed = True

        # AGC level
        val = await self._get_level("AGC")
        if val is not None:
            agc = int(val)
            if agc != self.state.agc:
                self.state.agc = agc
                changed = True

        # NB and ANF funcs — NR/ANF confirmed live (2026-07-22) RPRT -9
        # (ENAVAIL) on 2m/70cm on this rig, same skip rationale as ATT
        # above. NB's func query works fine on these bands, so it isn't
        # skipped — see NO_ATT_NR_ANF_BANDS.
        for attr, func_name in [('nb_on', 'NB'), ('nr_on', 'NR'), ('dnf_on', 'ANF')]:
            if func_name in ('NR', 'ANF') and self.state.band in NO_ATT_NR_ANF_BANDS:
                continue
            val = await self._get_func(func_name)
            if val is not None:
                on = val > 0
                if on != getattr(self.state, attr):
                    setattr(self.state, attr, on)
                    changed = True

        # DT GAIN — only meaningful in digital modes, and it's a raw CAT
        # passthrough (no Hamlib level equivalent), so skip it otherwise
        # rather than spend a round-trip on every slow-poll cycle. Fired as
        # a detached background task rather than awaited here: even on its
        # own connection (see get_dt_gain), awaiting it inline would still
        # make THIS poll cycle's return — and therefore this cycle's
        # PTT/frequency broadcast — wait on it. It fires its own
        # _fire_callbacks() once it actually resolves, decoupled from the
        # main poll cadence entirely.
        if self.state.is_digital and (
                self._dt_gain_task is None or self._dt_gain_task.done()):
            self._dt_gain_task = asyncio.create_task(self._poll_dt_gain())

        # SSB TX BPF — mirrors DT GAIN above but for voice modes (the menu
        # item is meaningless in digital modes); same detached-task rationale.
        if not self.state.is_digital and (
                self._ssb_bpf_task is None or self._ssb_bpf_task.done()):
            self._ssb_bpf_task = asyncio.create_task(self._poll_ssb_bpf())

        return changed

    async def _poll_dt_gain(self):
        val = await self.get_dt_gain()
        if val is not None and val != self.state.dt_gain:
            self.state.dt_gain = val
            await self._fire_callbacks()

    async def _poll_ssb_bpf(self):
        val = await self.get_ssb_tx_bpf()
        if val is not None and val != self.state.ssb_tx_bpf:
            self.state.ssb_tx_bpf = val
            await self._fire_callbacks()

    # ------------------------------------------------------------------
    # Internal: I/O helpers
    # ------------------------------------------------------------------

    async def _get_level(self, level_name: str) -> Optional[float]:
        """Get a Hamlib level value. Returns float or None."""
        lines = await self._send_get(f"l {level_name}\n", 1)
        if lines:
            try:
                return float(lines[0])
            except ValueError:
                pass
        return None

    async def _get_func(self, func_name: str) -> Optional[int]:
        """Get a Hamlib func value (0 or 1). Returns int or None."""
        lines = await self._send_get(f"u {func_name}\n", 1)
        if lines:
            try:
                return int(lines[0])
            except ValueError:
                pass
        return None

    async def _get_float(self, cmd: str, n_lines: int) -> Optional[float]:
        lines = await self._send_get(cmd, n_lines)
        if lines:
            try:
                return float(lines[0])
            except ValueError:
                pass
        return None

    async def _send_get(
            self, cmd: str, n_lines: int, timeout: float = 2.0
    ) -> Optional[list[str]]:
        """Send GET command. Read exactly n_lines (no RPRT terminator)."""
        async with self._lock:
            if not self._writer or self._writer.is_closing():
                return None
            try:
                self._writer.write(cmd.encode())
                await self._writer.drain()
                lines = []
                for _ in range(n_lines):
                    line = await asyncio.wait_for(
                        self._reader.readline(), timeout=timeout)
                    decoded = line.decode(errors='replace').strip()
                    if not decoded or decoded.startswith('RPRT'):
                        break
                    lines.append(decoded)
                return lines if lines else None
            except asyncio.TimeoutError:
                # A read (not the socket) timed out — the rig's serial link
                # is shared with rigctld's own internal polling and other
                # clients, so a slow reply doesn't mean the connection is
                # dead. Treat it as a miss, not a disconnect, but swallow
                # any late reply now so it can't desync the next command's
                # read.
                logger.warning(
                    f"GET '{cmd.strip()}' timed out (no reply within {timeout}s)")
                await self._drain_stale_reply()
                return None
            except (ConnectionResetError, OSError) as e:
                logger.warning(f"GET '{cmd.strip()}' failed: {e}")
                self.state.connected = False
                return None

    async def _drain_stale_reply(self):
        """Swallow any reply that arrives just after we gave up on it, so it
        doesn't get mistaken for the response to the next command sent on
        this connection. Loops rather than reading once: a single multi-
        line command (e.g. mode's 2-line reply) can leave more than one
        line backed up if it was the one that ran long. Stops as soon as
        a read itself times out — that means nothing more is coming."""
        for _ in range(3):
            try:
                await asyncio.wait_for(self._reader.readline(), timeout=1.0)
            except (asyncio.TimeoutError, ConnectionResetError, OSError):
                break

    async def _send_set(self, cmd: str) -> bool:
        """Send SET command. Read until RPRT."""
        async with self._lock:
            if not self._writer or self._writer.is_closing():
                return False
            try:
                self._writer.write(cmd.encode())
                await self._writer.drain()
                while True:
                    line = await asyncio.wait_for(
                        self._reader.readline(), timeout=2.0)
                    decoded = line.decode(errors='replace').strip()
                    if decoded.startswith("RPRT"):
                        return decoded == "RPRT 0"
                    if not decoded:
                        return False
            except (asyncio.TimeoutError, ConnectionResetError, OSError) as e:
                logger.warning(f"SET '{cmd.strip()}' failed: {e}")
                self.state.connected = False
                return False

    async def _send_raw_ex_set(self, cmd: str) -> bool:
        """Send a raw CAT passthrough SET command (`w EXnnnv...;`) on the
        shared connection.

        This is NOT _send_set with different framing — it's a genuinely
        different reply shape, live-verified over a raw socket against
        this FT-991A:
          - GET-style passthrough (`w EX110;`, no parameter) — the radio
            echoes the current value, NUL-terminated: b'EX1100;\\x00'.
            No newline, no RPRT. (See get_dt_gain/get_ssb_tx_bpf.)
          - SET-style passthrough (`w EX1101;`, parameter included) —
            the radio sends back NOTHING AT ALL. Zero bytes, confirmed
            over a 6-second wait. But the write DOES take effect —
            confirmed by immediately reading the value back afterward.
        So there is nothing to await here: any attempt to read a reply
        (readline for an RPRT, or readuntil for a NUL) just blocks for
        the full timeout on every single call, since no bytes ever
        arrive — that's exactly what was breaking this: _send_set's
        readline()-until-RPRT loop was hanging 2s and then marking the
        whole rig disconnected, on every DT GAIN or SSB TX BPF change.
        This is a fire-and-forget write, matching what the radio
        actually does with these commands."""
        async with self._lock:
            if not self._writer or self._writer.is_closing():
                return False
            try:
                self._writer.write(cmd.encode())
                await self._writer.drain()
                return True
            except (ConnectionResetError, OSError) as e:
                logger.warning(f"Raw SET '{cmd.strip()}' failed: {e}")
                return False

    async def _fire_callbacks(self):
        for cb in self._state_callbacks:
            try:
                await cb(self.state)
            except Exception as e:
                logger.error(f"State callback error: {e}")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

async def _test(host='127.0.0.1', port=4532):
    print(f"Testing rigctld → {host}:{port}")
    print("=" * 50)
    n = 0

    async def on_state(s: RigState):
        nonlocal n
        n += 1
        print(f"\nUpdate #{n}:")
        print(f"  Freq:    {s.freq_display}  {s.band}  {s.mode}")
        print(f"  S-meter: {s.smeter_label} ({s.strength_db:.1f} dB)")
        print(f"  Preamp:  {s.preamp_name}  ATT: {s.att_db}dB")
        print(f"  RF Pwr:  {s.rf_power_pct}%")
        print(f"  PTT:     {'TX' if s.ptt else 'RX'}")
        if s.ptt:
            print(f"  ALC:     {s.alc:.2f}  PO: {s.rf_power_out:.2f}  SWR: {s.swr_radio:.2f}")

    client = RigctldClient(host=host, port=port)
    client.on_state_change(on_state)
    await client.start()
    print("Polling 15s — tune VFO, observe S-meter...")
    await asyncio.sleep(15)
    await client.stop()
    print(f"\nTotal updates: {n}")


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    asyncio.run(_test(
        sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1',
        int(sys.argv[2]) if len(sys.argv) > 2 else 4532
    ))
