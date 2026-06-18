"""
ACOM 1200S Async Serial Driver

Handles:
- Async serial port I/O (non-blocking, runs in asyncio event loop)
- Automatic ACK after every amp→computer message
- 10ms inter-message pause (per protocol spec)
- Telemetry dispatch to registered callbacks
- Reconnection on serial error
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Optional, Any

import serial
import serial.tools.list_ports

from .acom_protocol import (
    AmpMsg, CmdMsg,
    FrameReader, parse_frame,
    parse_full_telemetry, parse_fault_codes,
    cmd_ack, cmd_enable_telemetry,
    AmpTelemetry, FaultStatus,
)

logger = logging.getLogger(__name__)

# Inter-message pause required by protocol
MSG_PAUSE_S = 0.010  # 10ms


# ---------------------------------------------------------------------------
# Callback type aliases
# ---------------------------------------------------------------------------

TelemetryCallback = Callable[[AmpTelemetry], Coroutine]
FaultCallback     = Callable[[FaultStatus], Coroutine]
RawFrameCallback  = Callable[[int, bytes], Coroutine]  # (address, data)
ConnectionCallback = Callable[[bool], Coroutine]        # (connected)


# ---------------------------------------------------------------------------
# Serial driver
# ---------------------------------------------------------------------------

@dataclass
class AcomSerial:
    """
    Async serial driver for the ACOM 600S/1200S.

    Usage:
        driver = AcomSerial(port='/dev/cu.usbserial-XXXX')
        driver.on_telemetry(my_telemetry_handler)
        driver.on_fault(my_fault_handler)
        await driver.start()
        ...
        await driver.send(cmd_standby())
        await driver.stop()
    """

    port: str
    baud: int = 9600
    reconnect_interval: float = 5.0   # seconds between reconnect attempts

    # Internal state
    _serial: Optional[serial.Serial] = field(default=None, init=False, repr=False)
    _reader: FrameReader = field(default_factory=FrameReader, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _send_lock: Optional[asyncio.Lock] = field(default=None, init=False, repr=False)
    _last_send_time: float = field(default=0.0, init=False, repr=False)

    # Callbacks
    _telemetry_callbacks: list[TelemetryCallback] = field(
        default_factory=list, init=False, repr=False)
    _fault_callbacks: list[FaultCallback] = field(
        default_factory=list, init=False, repr=False)
    _raw_callbacks: list[RawFrameCallback] = field(
        default_factory=list, init=False, repr=False)
    _connection_callbacks: list[ConnectionCallback] = field(
        default_factory=list, init=False, repr=False)
    _ant_callbacks: list = field(
        default_factory=list, init=False, repr=False)

    def __post_init__(self):
        self._send_lock = asyncio.Lock()
        self._reader = FrameReader()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_telemetry(self, cb: TelemetryCallback):
        """Register async callback for full telemetry (0x2F) messages."""
        self._telemetry_callbacks.append(cb)

    def on_fault(self, cb: FaultCallback):
        """Register async callback for fault code (0x21) messages."""
        self._fault_callbacks.append(cb)

    def on_antenna_change(self, cb):
        """Register async callback for ANT_BAND_INFO (0x27) messages.
        cb(antenna: int, band_byte: int) -> None
        """
        self._ant_callbacks.append(cb)

    def on_raw_frame(self, cb: RawFrameCallback):
        """Register async callback for all frames (address, data)."""
        self._raw_callbacks.append(cb)

    def on_connection_change(self, cb: ConnectionCallback):
        """Register async callback for connect/disconnect events."""
        self._connection_callbacks.append(cb)

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self):
        """Start the driver. Runs the read loop in background."""
        self._running = True
        asyncio.create_task(self._run())
        logger.info(f"AcomSerial started on {self.port} at {self.baud} baud")

    async def stop(self):
        """Stop the driver and close serial port."""
        self._running = False
        if self._serial and self._serial.is_open:
            self._serial.close()
        logger.info("AcomSerial stopped")

    async def send(self, frame: bytes) -> bool:
        """
        Send a command frame to the amplifier.
        Enforces 10ms inter-message pause.
        Returns True if sent successfully.
        """
        if not self._connected:
            logger.warning("Cannot send — not connected")
            return False

        async with self._send_lock:
            # Enforce 10ms pause between messages
            elapsed = time.monotonic() - self._last_send_time
            if elapsed < MSG_PAUSE_S:
                await asyncio.sleep(MSG_PAUSE_S - elapsed)

            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._serial.write, frame)
                self._last_send_time = time.monotonic()
                logger.debug(f"TX: {frame.hex(' ').upper()}")
                return True
            except serial.SerialException as e:
                logger.error(f"Serial write error: {e}")
                await self._handle_disconnect()
                return False

    # ------------------------------------------------------------------
    # Internal: connection management
    # ------------------------------------------------------------------

    async def _run(self):
        """Main loop: connect, read, reconnect on error."""
        while self._running:
            if await self._connect():
                await self._read_loop()
            if self._running:
                logger.info(
                    f"Reconnecting in {self.reconnect_interval}s...")
                await asyncio.sleep(self.reconnect_interval)

    async def _connect(self) -> bool:
        """Open serial port. Returns True on success."""
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0,
                write_timeout=1.0,
                dsrdtr=False,   # Disable DSR/DTR — prevents amp disturbance
                rtscts=False,   # Disable RTS/CTS hardware flow control
            )
            # Explicitly deassert DTR and RTS
            self._serial.setDTR(False)
            self._serial.setRTS(False)

            self._reader = FrameReader()
            self._connected = True
            logger.info(f"Connected to ACOM on {self.port}")
            await self._fire_connection_callbacks(True)

            # Wait 5s for amp to stabilize before sending anything
            logger.info("Waiting for ACOM to stabilize...")
            await asyncio.sleep(5.0)
            await self.send(cmd_enable_telemetry())
            logger.info("Telemetry enabled")
            return True

        except serial.SerialException as e:
            logger.error(f"Cannot open {self.port}: {e}")
            self._connected = False
            return False

    async def _handle_disconnect(self):
        """Handle unexpected serial disconnect."""
        if self._connected:
            self._connected = False
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            logger.warning("ACOM serial disconnected")
            await self._fire_connection_callbacks(False)

    # ------------------------------------------------------------------
    # Internal: read loop
    # ------------------------------------------------------------------

    async def _read_loop(self):
        """Continuously read bytes from serial, dispatch complete frames."""
        loop = asyncio.get_event_loop()
        while self._running and self._connected:
            try:
                # Non-blocking read — get whatever is available
                data = await loop.run_in_executor(
                    None, self._read_available)

                if data:
                    self._reader.feed(data)
                    # Process any complete frames
                    while self._reader.frames:
                        frame = self._reader.frames.pop(0)
                        await self._dispatch_frame(frame)
                else:
                    # Nothing available — yield to event loop
                    await asyncio.sleep(0.005)  # 5ms poll interval

            except serial.SerialException as e:
                logger.error(f"Serial read error: {e}")
                await self._handle_disconnect()
                return

    def _read_available(self) -> bytes:
        """Read all available bytes from serial (runs in executor)."""
        if not self._serial or not self._serial.is_open:
            return b''
        waiting = self._serial.in_waiting
        if waiting > 0:
            return self._serial.read(waiting)
        return b''

    # ------------------------------------------------------------------
    # Internal: frame dispatch
    # ------------------------------------------------------------------

    async def _dispatch_frame(self, frame: bytes):
        """Parse frame, ACK it, dispatch to callbacks."""
        result = parse_frame(frame)
        if result is None:
            logger.warning(f"Bad frame: {frame.hex(' ').upper()}")
            return

        address, data = result
        logger.debug(f"RX addr=0x{address:02X} data={data.hex(' ').upper()}")

        # ACK every amp→computer message immediately
        if address < 0x80:  # Amp→computer messages have addresses < 0x80
            ack = cmd_ack(address)
            await self.send(ack)

        # Fire raw frame callbacks
        for cb in self._raw_callbacks:
            try:
                await cb(address, data)
            except Exception as e:
                logger.error(f"Raw callback error: {e}")

        # Dispatch to typed callbacks
        if address == AmpMsg.FULL_TELEMETRY:
            telemetry = parse_full_telemetry(data)
            if telemetry:
                for cb in self._telemetry_callbacks:
                    try:
                        await cb(telemetry)
                    except Exception as e:
                        logger.error(f"Telemetry callback error: {e}")

        elif address == AmpMsg.ERROR_CODES:
            faults = parse_fault_codes(data)
            for cb in self._fault_callbacks:
                try:
                    await cb(faults)
                except Exception as e:
                    logger.error(f"Fault callback error: {e}")

        elif address == AmpMsg.ANT_BAND_INFO:
            # data[0]=antenna#, data[1]=band byte (per ACOM protocol)
            if len(data) >= 2:
                ant_num  = data[0]
                band_byte = data[1]
                for cb in self._ant_callbacks:
                    try:
                        await cb(ant_num, band_byte)
                    except Exception as e:
                        logger.error(f"Antenna callback error: {e}")

    # ------------------------------------------------------------------
    # Internal: connection callbacks
    # ------------------------------------------------------------------

    async def _fire_connection_callbacks(self, connected: bool):
        for cb in self._connection_callbacks:
            try:
                await cb(connected)
            except Exception as e:
                logger.error(f"Connection callback error: {e}")


# ---------------------------------------------------------------------------
# Utility: find ACOM serial port
# ---------------------------------------------------------------------------

def find_acom_port() -> Optional[str]:
    """
    Scan available serial ports and return the most likely ACOM candidate.
    Looks for FTDI devices that aren't already the FT-991A SiLabs ports.
    Returns port path or None.
    """
    candidates = []
    for port in serial.tools.list_ports.comports():
        desc = (port.description or "").lower()
        mfr  = (port.manufacturer or "").lower()
        # FTDI chips commonly used for ACOM connection
        if 'ftdi' in mfr or 'ft232' in desc or 'usb serial' in desc:
            # Exclude SiLabs (that's the FT-991A)
            if 'silabs' not in mfr and 'cp210' not in desc:
                candidates.append(port.device)
                logger.info(f"ACOM candidate port: {port.device} "
                           f"({port.description})")
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------------------
# Simple connection test (run directly to verify cable)
# ---------------------------------------------------------------------------

async def _test_connection(port: str):
    """Quick test: connect, request full telemetry, print result."""
    from .acom_protocol import cmd_request_full_telemetry

    print(f"Testing ACOM connection on {port}...")
    print("=" * 50)

    received = asyncio.Event()
    telemetry_result = {}

    async def on_telemetry(t: AmpTelemetry):
        telemetry_result['data'] = t
        received.set()

    async def on_fault(f: FaultStatus):
        print(f"Fault status: {f.severity}")
        for msg in f.hard_faults:
            print(f"  HARD FAULT: {msg}")
        for msg in f.soft_faults:
            print(f"  SOFT FAULT: {msg}")
        for msg in f.warnings:
            print(f"  WARNING: {msg}")

    async def on_connect(connected: bool):
        status = "CONNECTED" if connected else "DISCONNECTED"
        print(f"Serial: {status}")
        if connected:
            # Request telemetry explicitly
            await asyncio.sleep(0.5)

    driver = AcomSerial(port=port)
    driver.on_telemetry(on_telemetry)
    driver.on_fault(on_fault)
    driver.on_connection_change(on_connect)

    await driver.start()

    try:
        # Wait up to 10 seconds for telemetry
        await asyncio.wait_for(received.wait(), timeout=10.0)
        t = telemetry_result['data']
        print(f"\nAmplifier State:    {t.mode_name}")
        print(f"Forward Power:      {t.fwd_power_w:.0f} W")
        print(f"Reflected Power:    {t.refl_power_w:.0f} W")
        print(f"SWR:                {t.swr:.2f}")
        print(f"Drive Power:        {t.input_power_w:.1f} W")
        print(f"PAM1 Temperature:   {t.pam1_temp_c:.1f} °C")
        print(f"HV Rail:            {t.hv1_v:.1f} V")
        print(f"Drain Current:      {t.id1_ma:.0f} mA")
        print(f"Frequency:          {t.freq_khz} kHz")
        print(f"TX Access:          {'Yes' if t.flag_tx_access else 'No'}")
        print(f"Key In:             {'Active' if t.flag_keyin else 'Inactive'}")
        print(f"ATU Tuned:          {'Yes' if t.flag_atu_tuned else 'No'}")
        print("\nConnection test PASSED")

    except asyncio.TimeoutError:
        print("\nNo telemetry received within 10 seconds.")
        print("Check: cable connected? Amp powered on? Correct port?")

    finally:
        await driver.stop()


if __name__ == '__main__':
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )

    if len(sys.argv) > 1:
        test_port = sys.argv[1]
    else:
        test_port = find_acom_port()
        if not test_port:
            print("No FTDI port found. Specify port as argument:")
            print(f"  python {sys.argv[0]} /dev/cu.usbserial-XXXX")
            # List available ports
            print("\nAvailable ports:")
            for p in serial.tools.list_ports.comports():
                print(f"  {p.device}: {p.description}")
            sys.exit(1)

    asyncio.run(_test_connection(test_port))