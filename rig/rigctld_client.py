"""
Rigctld Client — FT-991A State Polling and Control

Connects to a running rigctld daemon via TCP (default localhost:4532).
Polls rig state at configurable interval, fires callbacks on any change.
Also supports sending commands (set frequency, set mode, PTT).

Designed for UI integration: all state is held in RigState and broadcast
to subscribers whenever anything changes.

rigctld protocol notes:
  GET commands (lowercase: f, m, t, s): return value line(s), NO RPRT terminator
  SET commands (uppercase: F, M, T, S): return "RPRT 0" on success
  These must be handled differently.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Coroutine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Band definitions
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

# Standard FT8 frequencies by band
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


def freq_to_band(freq_hz: int) -> Band:
    for lo, hi, band in BAND_EDGES:
        if lo <= freq_hz <= hi:
            return band
    return Band.UNKNOWN


def freq_display(freq_hz: int) -> str:
    """Format frequency for UI. e.g. 14.074.000"""
    mhz = freq_hz // 1_000_000
    khz = (freq_hz % 1_000_000) // 1_000
    hz  = freq_hz % 1_000
    return f"{mhz:3d}.{khz:03d}.{hz:03d}"


# ---------------------------------------------------------------------------
# Rig state — single source of truth for UI
# ---------------------------------------------------------------------------

@dataclass
class RigState:
    """
    Complete state of the FT-991A as known to the system.
    Serializes to dict for WebSocket broadcast.
    """
    connected: bool       = False
    freq_hz: int          = 0
    freq_display: str     = "  0.000.000"
    band: str             = "??"
    mode: str             = ""
    passband_hz: int      = 0
    ptt: bool             = False
    split: bool           = False
    tx_freq_hz: int       = 0
    rf_power_pct: int     = 0
    alc: int              = 0
    swr: float            = 0.0
    is_digital: bool      = False
    near_digital_freq: bool = False

    def to_dict(self) -> dict:
        return {
            "connected":         self.connected,
            "freq_hz":           self.freq_hz,
            "freq_display":      self.freq_display,
            "band":              self.band,
            "mode":              self.mode,
            "passband_hz":       self.passband_hz,
            "ptt":               self.ptt,
            "split":             self.split,
            "tx_freq_hz":        self.tx_freq_hz,
            "rf_power_pct":      self.rf_power_pct,
            "alc":               self.alc,
            "swr":               self.swr,
            "is_digital":        self.is_digital,
            "near_digital_freq": self.near_digital_freq,
        }

    def update_derived(self):
        """Recompute derived fields after freq/mode update."""
        band_enum = freq_to_band(self.freq_hz)
        self.band = band_enum.value
        self.freq_display = freq_display(self.freq_hz)

        digital_modes = {"DATA-U", "DATA-L", "PKT-U", "PKT-L",
                         "PKTUSB", "PKTLSB", "USB", "LSB"}
        self.is_digital = self.mode in digital_modes

        std_freq = DIGITAL_FREQS.get(band_enum)
        if std_freq and self.freq_hz > 0:
            self.near_digital_freq = abs(self.freq_hz - std_freq) < 1000
        else:
            self.near_digital_freq = False


# ---------------------------------------------------------------------------
# Rigctld TCP client
# ---------------------------------------------------------------------------

StateCallback = Callable[[RigState], Coroutine]


class RigctldClient:
    """
    Async client for rigctld daemon.

    Connects via TCP, polls state at poll_interval seconds,
    fires on_state_change callbacks whenever anything changes.

    rigctld protocol:
      GET commands (f, m, t, s ...): return value line(s), no RPRT
      SET commands (F, M, T, S ...): return "RPRT 0" on success
    """

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_state_change(self, cb: StateCallback):
        """Register async callback fired whenever rig state changes."""
        self._state_callbacks.append(cb)

    async def start(self):
        """Start polling loop in background."""
        self._running = True
        asyncio.create_task(self._run())
        logger.info(f"RigctldClient started → {self.host}:{self.port}")

    async def stop(self):
        """Stop polling and close connection."""
        self._running = False
        await self._disconnect()
        logger.info("RigctldClient stopped")

    async def set_frequency(self, freq_hz: int) -> bool:
        """Set VFO frequency in Hz. Returns True on success."""
        return await self._send_set(f"F {freq_hz}\n")

    async def set_mode(self, mode: str, passband: int = 0) -> bool:
        """Set mode and passband. mode e.g. 'USB', 'LSB', 'CW', 'DATA-U'"""
        return await self._send_set(f"M {mode} {passband}\n")

    async def set_ptt(self, active: bool) -> bool:
        """Key/unkey transmitter via CAT."""
        return await self._send_set(f"T {1 if active else 0}\n")

    async def get_frequency(self) -> Optional[int]:
        """One-shot frequency query. Returns Hz or None."""
        lines = await self._send_get("f\n", 1)
        if lines:
            try:
                return int(lines[0])
            except ValueError:
                pass
        return None

    async def get_mode(self) -> Optional[tuple[str, int]]:
        """One-shot mode query. Returns (mode, passband) or None."""
        lines = await self._send_get("m\n", 2)
        if lines and len(lines) >= 2:
            try:
                return lines[0].strip(), int(lines[1].strip())
            except ValueError:
                pass
        return None

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
                timeout=3.0
            )
            self.state.connected = True
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
    # Internal: poll loop
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
        """Query all rig parameters. Returns True if anything changed."""
        changed = False

        # Frequency (1 line)
        lines = await self._send_get("f\n", 1)
        if lines:
            try:
                freq = int(lines[0])
                if freq != self.state.freq_hz:
                    self.state.freq_hz = freq
                    changed = True
            except ValueError:
                pass

        # Mode + passband (2 lines)
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

        # PTT (1 line)
        lines = await self._send_get("t\n", 1)
        if lines:
            try:
                ptt = bool(int(lines[0]))
                if ptt != self.state.ptt:
                    self.state.ptt = ptt
                    changed = True
            except ValueError:
                pass

        # Split (2 lines: split_state, tx_vfo)
        lines = await self._send_get("s\n", 2)
        if lines:
            try:
                split = bool(int(lines[0].strip()))
                if split != self.state.split:
                    self.state.split = split
                    changed = True
                if split and len(lines) >= 2:
                    tx_freq = int(lines[1].strip())
                    if tx_freq != self.state.tx_freq_hz:
                        self.state.tx_freq_hz = tx_freq
                        changed = True
            except (ValueError, IndexError):
                pass

        if changed:
            self.state.update_derived()

        return changed

    # ------------------------------------------------------------------
    # Internal: rigctld I/O — GET vs SET handled separately
    # ------------------------------------------------------------------

    async def _send_get(self, cmd: str, n_lines: int) -> Optional[list[str]]:
        """
        Send a GET command (lowercase: f, m, t, s).
        Reads exactly n_lines — rigctld returns values only, no RPRT.
        """
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
                    if not decoded:
                        break
                    lines.append(decoded)
                return lines if lines else None
            except (asyncio.TimeoutError, ConnectionResetError, OSError) as e:
                logger.warning(f"GET '{cmd.strip()}' failed: {e}")
                self.state.connected = False
                return None

    async def _send_set(self, cmd: str) -> bool:
        """
        Send a SET command (uppercase: F, M, T, S).
        Reads until RPRT line. Returns True if RPRT 0.
        """
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

    # ------------------------------------------------------------------
    # Internal: callbacks
    # ------------------------------------------------------------------

    async def _fire_callbacks(self):
        for cb in self._state_callbacks:
            try:
                await cb(self.state)
            except Exception as e:
                logger.error(f"State callback error: {e}")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

async def _test(host: str = '127.0.0.1', port: int = 4532):
    print(f"Testing rigctld connection → {host}:{port}")
    print("=" * 50)

    update_count = 0

    async def on_state(state: RigState):
        nonlocal update_count
        update_count += 1
        print(f"\nRig State Update #{update_count}:")
        print(f"  Connected:    {state.connected}")
        print(f"  Frequency:    {state.freq_display}")
        print(f"  Band:         {state.band}")
        print(f"  Mode:         {state.mode} ({state.passband_hz} Hz pb)")
        print(f"  PTT:          {'TX' if state.ptt else 'RX'}")
        print(f"  Split:        {state.split}")
        print(f"  FT8 freq:     {state.near_digital_freq}")

    client = RigctldClient(host=host, port=port, poll_interval=0.5)
    client.on_state_change(on_state)
    await client.start()

    print("Polling for 15 seconds — tune the VFO to see updates...")
    await asyncio.sleep(15)
    await client.stop()
    print(f"\nTotal state updates: {update_count}")


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 4532
    asyncio.run(_test(host, port))
