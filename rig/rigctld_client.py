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
]

DIGITAL_FREQS = {
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
}

def freq_to_band(freq_hz: int) -> Band:
    for lo, hi, band in BAND_EDGES:
        if lo <= freq_hz <= hi:
            return band
    return Band.UNKNOWN

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

        digital_modes = {"DATA-U", "DATA-L", "PKT-U", "PKT-L",
                         "PKTUSB", "PKTLSB", "USB", "LSB"}
        self.is_digital = self.mode in digital_modes

        std_freq = DIGITAL_FREQS.get(band_enum)
        self.near_digital_freq = bool(
            std_freq and self.freq_hz > 0
            and abs(self.freq_hz - std_freq) < 2000)


# ---------------------------------------------------------------------------
# Rigctld client
# ---------------------------------------------------------------------------

StateCallback = Callable[[RigState], Coroutine]

# How often to poll each group (in poll cycles, each cycle = poll_interval)
FREQ_EVERY   = 1   # Every cycle
MODE_EVERY   = 2   # Every 2 cycles
PTT_EVERY    = 1   # Every cycle
METER_EVERY  = 1   # Every cycle
CONTROL_EVERY = 10  # Every 10 cycles (~5s)


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
        """Set attenuator: 0=off, 6=6dB, 12=12dB, 18=18dB."""
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
            logger.info(f"Connected to rigctld at {self.host}:{self.port}")
            await self._fire_callbacks()
            return True
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"Cannot connect to rigctld: {e}")
            return False

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
        while self._running and self.state.connected:
            try:
                changed = await self._poll_state()
                if changed:
                    await self._fire_callbacks()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"Poll error: {e}")
                self.state.connected = False
                await self._disconnect()
                return

    async def _poll_state(self) -> bool:
        self._cycle += 1
        changed = False

        # Frequency — every cycle
        val = await self._get_float("f\n", n_lines=1)
        if val is not None:
            freq = int(val)
            if freq != self.state.freq_hz:
                self.state.freq_hz = freq
                changed = True

        # Mode — every other cycle
        if self._cycle % MODE_EVERY == 0:
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

        # PTT — every cycle
        val = await self._get_float("t\n", n_lines=1)
        if val is not None:
            ptt = bool(int(val))
            if ptt != self.state.ptt:
                self.state.ptt = ptt
                changed = True

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

        # ATT
        val = await self._get_level("ATT")
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

        # NB, NR, COMP, MICGAIN
        for attr, level_name in [
            ('nb_level', 'NB'),
            ('nr_level', 'NR'),
            ('comp_level', 'COMP'),
            ('mic_gain', 'MICGAIN'),
        ]:
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

        # NB and ANF funcs
        for attr, func_name in [('nb_on', 'NB'), ('nr_on', 'NR'), ('dnf_on', 'ANF')]:
            val = await self._get_func(func_name)
            if val is not None:
                on = val > 0
                if on != getattr(self.state, attr):
                    setattr(self.state, attr, on)
                    changed = True

        return changed

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

    async def _send_get(self, cmd: str, n_lines: int) -> Optional[list[str]]:
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
                        self._reader.readline(), timeout=2.0)
                    decoded = line.decode(errors='replace').strip()
                    if not decoded or decoded.startswith('RPRT'):
                        break
                    lines.append(decoded)
                return lines if lines else None
            except (asyncio.TimeoutError, ConnectionResetError, OSError) as e:
                logger.warning(f"GET '{cmd.strip()}' failed: {e}")
                self.state.connected = False
                return None

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
