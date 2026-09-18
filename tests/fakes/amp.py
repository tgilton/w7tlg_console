"""Fake AcomSerial — same public call signatures as amplifier/acom_serial.py's
AcomSerial, minus the real pyserial connection to the amplifier.

AcomBridge (amplifier/acom_bridge.py) only ever touches `amp` through:
on_telemetry/on_fault/on_antenna_change/on_raw_frame/on_connection_change
(registration), `connected`, `start`/`stop`, and `send(frame) -> bool`
(confirmed by grepping acom_bridge.py's `self.amp.` call sites) — this fake
covers exactly that surface, plus emit_*/simulate_connect test helpers to
drive those registered callbacks the way real frames arriving over the wire
would.
"""
from __future__ import annotations

from amplifier.acom_protocol import AmpTelemetry, FaultStatus


class FakeAcomSerial:

    def __init__(self, port: str, baud: int = 9600, reconnect_interval: float = 5.0):
        self.port = port
        self.baud = baud
        self.reconnect_interval = reconnect_interval

        self._connected = False
        self.started = False
        self.stopped = False

        self._telemetry_callbacks = []
        self._fault_callbacks = []
        self._raw_callbacks = []
        self._connection_callbacks = []
        self._ant_callbacks = []

        # Frames handed to send(), in order — assert against this instead of
        # a real serial write.
        self.sent_frames: list[bytes] = []

        # Test hook: set False to make the next send() report failure, same
        # as a real pyserial write error.
        self.send_ok = True

    # ------------------------------------------------------------------
    # Public API — mirrors AcomSerial exactly
    # ------------------------------------------------------------------

    def on_telemetry(self, cb):
        self._telemetry_callbacks.append(cb)

    def on_fault(self, cb):
        self._fault_callbacks.append(cb)

    def on_antenna_change(self, cb):
        self._ant_callbacks.append(cb)

    def on_raw_frame(self, cb):
        self._raw_callbacks.append(cb)

    def on_connection_change(self, cb):
        self._connection_callbacks.append(cb)

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True
        self._connected = False

    async def send(self, frame: bytes) -> bool:
        if not self._connected:
            return False
        if self.send_ok:
            self.sent_frames.append(frame)
        return self.send_ok

    # ------------------------------------------------------------------
    # Test helpers — not part of the real driver's API
    # ------------------------------------------------------------------

    async def simulate_connect(self, connected: bool = True):
        """Drive the same connection-change callback the real driver fires
        on serial open/close."""
        self._connected = connected
        for cb in list(self._connection_callbacks):
            await cb(connected)

    async def emit_telemetry(self, t: AmpTelemetry):
        """Simulate a parsed 0x2F full-telemetry frame arriving."""
        for cb in list(self._telemetry_callbacks):
            await cb(t)

    async def emit_fault(self, f: FaultStatus):
        """Simulate a parsed 0x21 fault-code frame arriving."""
        for cb in list(self._fault_callbacks):
            await cb(f)

    async def emit_antenna_change(self, ant_num: int, ant_type_byte: int):
        """Simulate a 0x27 ANT_BAND_INFO frame arriving."""
        for cb in list(self._ant_callbacks):
            await cb(ant_num, ant_type_byte)

    async def emit_raw_frame(self, address: int, data: bytes):
        for cb in list(self._raw_callbacks):
            await cb(address, data)
