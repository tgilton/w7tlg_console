"""
ACOM 600S / 1200S Serial Protocol Library
Based on: RF Amplifier ACOM 600S Serial Port Communication Protocol v1.1

Frame structure:
  Byte 0: Start byte 0x55
  Byte 1: Address (message type)
  Byte 2: Length (total bytes including start and checksum)
  Byte 3..N-1: Data
  Byte N: Checksum = (256 - SUM(all previous bytes)) & 0xFF
  Verify: SUM(all bytes including checksum) == 0 (mod 256)
"""

import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

START_BYTE = 0x55

# Amplifier → Computer message addresses
class AmpMsg(IntEnum):
    ERROR_REPLY        = 0x01  # Bad receive error
    REQUEST_MSG        = 0x02  # Request for specific message
    SYS_CONFIG         = 0x11  # PCB versions, serial number
    SETTINGS           = 0x12  # CAT settings, user flags
    SERIAL_NUMBER      = 0x13  # Serial number only
    ERROR_CODES        = 0x21  # Full fault bitmask (24 bytes)
    AMP_STATE_LEGACY   = 0x23  # State + temps (obsolete, use 0x2F)
    POWER_LEGACY       = 0x24  # Power measurements (obsolete, use 0x2F)
    VOLTAGES_LEGACY    = 0x25  # PSU voltages, frequency (obsolete, use 0x2F)
    BIAS_LEGACY        = 0x26  # Bias voltages (obsolete, use 0x2F)
    ANT_BAND_INFO      = 0x27  # Antenna/band change notification
    BAND_SEG_STATUS    = 0x28  # Band segment tune status
    FULL_TELEMETRY     = 0x2F  # United message — primary telemetry (72 bytes)
    HF_RECORD_1        = 0x31  # Hard fault event record 1
    HF_RECORD_2        = 0x32  # Hard fault event record 2
    HF_RECORD_3        = 0x33  # Hard fault event record 3
    HF_RECORD_4        = 0x34  # Hard fault event record 4

# Computer → Amplifier message addresses
class CmdMsg(IntEnum):
    AMP_COMMAND        = 0x81  # Main command message
    SET_SERIAL         = 0x82  # Set serial number
    SET_HELLO_1        = 0x83  # Set hello message part 1
    SET_HELLO_2        = 0x84  # Set hello message part 2
    SET_ANT_NAME       = 0x85  # Set antenna name
    ACK                = 0x86  # Acknowledge (sent after every amp→computer msg)
    DISABLE_TELEMETRY  = 0x91  # Stop automatic telemetry stream
    ENABLE_TELEMETRY   = 0x92  # Start automatic telemetry stream

# Amplifier command codes (Byte 3 of 0x81 message)
class AmpCmd(IntEnum):
    EMPTY              = 0x00
    REQUEST_MSG        = 0x01  # Request specific message; Byte4/5 = msg address
    MODE_CHANGE        = 0x02  # Byte5 = desired mode
    REQUEST_HF_RECORD  = 0x03  # Byte5 = record number
    UPDATE_USER_FLAGS  = 0x04  # Byte4/5 = user flags
    SET_CAT_1          = 0x05
    SET_CAT_2          = 0x06
    SET_CAT_3          = 0x07
    CLEAR_SOFT_FAULTS  = 0x08
    ANT_BAND_SELECT    = 0x09  # Byte4 = antenna#, Byte5 = band#
    BUZZER             = 0x0A
    SEND_LOG           = 0x0B
    CLEAR_FAULTS       = 0x08

# Amplifier mode codes (used with MODE_CHANGE)
class AmpMode(IntEnum):
    STB                = 0x05  # Standby
    OPR_RX             = 0x06  # Operate / Receive
    OPR_TX             = 0x07  # Operate / Transmit
    ATAC               = 0x08  # ATU Tune / Antenna Change
    TURN_OFF           = 0x0A  # Power off
    TX_PROHIBIT        = 0x40  # Prohibit TX
    TX_ALLOW           = 0x80  # Allow TX

# Band numbers (used for frequency→band classification, not for any
# serial command — the A1200S has no direct band-select command, see
# cmd_next_antenna() below)
class Band(IntEnum):
    B160M  = 0x01
    B80M   = 0x02
    B60M   = 0x03
    B40M   = 0x04
    B30M   = 0x05
    B20M   = 0x06
    B17M   = 0x07
    B15M   = 0x08
    B12M   = 0x09
    B10M   = 0x0A

# Frequency (Hz) → Band mapping
FREQ_TO_BAND = [
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
]


# ---------------------------------------------------------------------------
# Frame builder / parser
# ---------------------------------------------------------------------------

def checksum(data: bytes) -> int:
    """Compute ACOM checksum: (256 - SUM(data)) & 0xFF"""
    return (256 - sum(data)) & 0xFF


def build_frame(address: int, data: bytes = b'') -> bytes:
    """Build a complete ACOM protocol frame."""
    length = 3 + len(data) + 1  # start + addr + length_byte + data + chksum
    frame = bytes([START_BYTE, address, length]) + data
    return frame + bytes([checksum(frame)])


def verify_frame(frame: bytes) -> bool:
    """Verify frame integrity: sum of all bytes == 0 mod 256."""
    return sum(frame) & 0xFF == 0


def parse_frame(frame: bytes) -> Optional[tuple[int, bytes]]:
    """
    Parse a frame. Returns (address, data) or None if invalid.
    'data' excludes start, address, length, and checksum bytes.
    """
    if len(frame) < 4:
        return None
    if frame[0] != START_BYTE:
        return None
    if not verify_frame(frame):
        return None
    address = frame[1]
    data = frame[3:-1]
    return (address, data)


# ---------------------------------------------------------------------------
# Pre-built command frames
# ---------------------------------------------------------------------------

def cmd_ack(received_address: int) -> bytes:
    """ACK frame — must be sent after every amp→computer message."""
    return build_frame(CmdMsg.ACK, bytes([received_address]))


def cmd_enable_telemetry() -> bytes:
    """Tell amp to start streaming telemetry automatically."""
    return build_frame(CmdMsg.ENABLE_TELEMETRY)


def cmd_disable_telemetry() -> bytes:
    """Tell amp to stop streaming telemetry."""
    return build_frame(CmdMsg.DISABLE_TELEMETRY)


def cmd_request_message(msg_address: int) -> bytes:
    """Request a specific message from the amp by address."""
    data = bytes([AmpCmd.REQUEST_MSG, 0x00, msg_address, 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_set_mode(mode: AmpMode) -> bytes:
    """Request amplifier mode change."""
    data = bytes([AmpCmd.MODE_CHANGE, 0x00, int(mode), 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_standby() -> bytes:
    return cmd_set_mode(AmpMode.STB)


def cmd_operate() -> bytes:
    return cmd_set_mode(AmpMode.OPR_RX)


def cmd_power_off() -> bytes:
    return cmd_set_mode(AmpMode.TURN_OFF)


def cmd_tx_prohibit() -> bytes:
    """Software TX inhibit — use for safety interlocks."""
    data = bytes([AmpCmd.MODE_CHANGE, 0x00, AmpMode.TX_PROHIBIT, 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_tx_allow() -> bytes:
    """Remove software TX inhibit."""
    data = bytes([AmpCmd.MODE_CHANGE, 0x00, AmpMode.TX_ALLOW, 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_next_antenna() -> bytes:
    """
    Cycle to the next antenna — the exact action of the amp's front-panel
    ANT button. Per protocol v1.3 (the version that actually documents the
    A1200S), command 0x09's Byte4 (antenna number) is ignored by the
    firmware and Byte5 only accepts a small set of relative cycle codes
    (0x30 = Next Antenna). There is no direct "select antenna N" command,
    and no "previous antenna" either — only forward cycling, same as the
    physical button.
    """
    data = bytes([AmpCmd.ANT_BAND_SELECT, 0x00, 0x30, 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_select_band(band: Band) -> bytes:
    """
    Tell the amp which band to track (for its LPF/display), independent of
    antenna selection. Byte4 is zeroed since it's the (ignored) antenna
    slot; only Byte5 = raw band number matters here. This raw-number form
    of Byte5 isn't in the documented v1.3 cycle-code list (0x10/0x20/0x30/
    0x40/0x80), but is confirmed against real hardware — it's the only way
    the amp's band/LPF stays in sync with the radio while in STANDBY, since
    its own RF frequency counter has nothing to detect without drive power.
    """
    data = bytes([AmpCmd.ANT_BAND_SELECT, 0x00, int(band), 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_clear_soft_faults() -> bytes:
    """Clear soft fault errors."""
    data = bytes([AmpCmd.CLEAR_FAULTS, 0x00, 0x00, 0x00])
    return build_frame(CmdMsg.AMP_COMMAND, data)


def cmd_request_full_telemetry() -> bytes:
    """Request the full 0x2F telemetry message."""
    return cmd_request_message(AmpMsg.FULL_TELEMETRY)


def cmd_atac() -> bytes:
    """Initiate ATU Tune / Antenna Change procedure."""
    return cmd_set_mode(AmpMode.ATAC)


# ---------------------------------------------------------------------------
# Frequency → Band utility
# ---------------------------------------------------------------------------

def freq_to_band(freq_hz: int) -> Optional[Band]:
    """Convert frequency in Hz to ACOM band number. Returns None if out of range."""
    for lo, hi, band in FREQ_TO_BAND:
        if lo <= freq_hz <= hi:
            return band
    return None


def band_name(band: Band) -> str:
    names = {
        Band.B160M: "160m", Band.B80M: "80m", Band.B60M: "60m",
        Band.B40M: "40m",  Band.B30M: "30m", Band.B20M: "20m",
        Band.B17M: "17m",  Band.B15M: "15m", Band.B12M: "12m",
        Band.B10M: "10m",
    }
    return names.get(band, "??m")


# ---------------------------------------------------------------------------
# Telemetry parsers
# ---------------------------------------------------------------------------

@dataclass
class AmpTelemetry:
    """Parsed full telemetry from message 0x2F (72 bytes)."""
    # Amplifier state
    mode: int = 0
    mode_name: str = ""

    # Power measurements
    input_power_w: float = 0.0    # Drive power from transceiver
    fwd_power_w: float = 0.0      # Forward (output) power
    refl_power_w: float = 0.0     # Reflected power
    swr: float = 0.0              # SWR (1.00 = perfect)
    dc_power_w: float = 0.0       # DC power consumption PAM1

    # Thermal
    pam1_temp_c: float = 0.0      # LDMOS final temperature °C

    # Supply voltages
    hv1_v: float = 0.0            # High voltage rail
    vcc26_v: float = 0.0          # 26V rail
    vcc5_mv: float = 0.0          # 5V rail (mV)
    id1_ma: float = 0.0           # Drain current PAM1 (mA)

    # Frequency (as detected by amp's counter)
    freq_khz: int = 0

    # Flags
    flag_atac_in_progress: bool = False
    flag_hv_on: bool = False
    flag_keyin: bool = False       # Key line active (TX in progress)
    flag_outr: bool = False        # Output relay closed
    flag_atu_tuned: bool = False
    flag_tx_access: bool = False   # TX permitted

    # System
    system_clock_s: int = 0
    fan_speed: int = 0
    active_lpf: int = 0

    # Error display
    error_code: int = 0
    error_param: int = 0

    # Raw flags for dashboard
    flags1_lo: int = 0
    flags1_hi: int = 0
    flags2_lo: int = 0
    flags2_hi: int = 0


def parse_full_telemetry(data: bytes) -> Optional[AmpTelemetry]:
    """
    Parse message 0x2F data field — 68 bytes of payload for the ACOM 1200S.
    Offsets verified against live 1200S telemetry data.

    Note on frame size: this 68-byte figure is the DATA payload only (after
    stripping the 4 envelope bytes: start/address/length/checksum). The full
    on-wire frame is therefore 68 + 4 = 72 bytes total — consistent with
    third-party ACOM control software (e.g. bjornekelund/ACOM-Controller)
    which reads a fixed 72-byte buffer including the envelope. The earlier
    "600S uses 69 data bytes, 1200S uses 68" note refers to the data-only
    count; total wire size is 72 bytes either way.
    """
    if len(data) < 68:
        return None

    t = AmpTelemetry()

    # Byte 0: mode
    t.mode = data[0]
    mode_map = {
        0x10: "Reset", 0x20: "Init", 0x30: "Debug", 0x40: "Service",
        0x50: "Standby", 0x60: "OPR/RX", 0x70: "OPR/TX",
        0x80: "ATAC", 0x90: "Menu", 0xA0: "Turning Off", 0xA2: "Extra Cooling"
    }
    t.mode_name = mode_map.get(t.mode & 0xF0, f"Unknown(0x{t.mode:02X})")

    # Bytes 1-4: Amplifier flags
    t.flags1_lo = data[1]
    t.flags1_hi = data[2]
    t.flags2_lo = data[3]
    t.flags2_hi = data[4]

    t.flag_atac_in_progress = bool(t.flags1_hi & 0x40)
    t.flag_hv_on            = bool(t.flags1_hi & 0x10)
    t.flag_keyin            = bool(t.flags2_lo & 0x02)
    t.flag_outr             = bool(t.flags2_lo & 0x40)
    t.flag_atu_tuned        = bool(t.flags2_hi & 0x04)
    t.flag_tx_access        = bool(t.flags2_lo & 0x10)

    # Bytes 5-6: DC power PAM1 [10xW] — confirmed 0W in standby
    t.dc_power_w = struct.unpack_from('<H', data, 5)[0] / 10.0

    # Bytes 9-12: System clock [s]
    hw = struct.unpack_from('<H', data, 9)[0]
    lw = struct.unpack_from('<H', data, 11)[0]
    t.system_clock_s = (hw << 16) | lw

    # Bytes 13-14: PAM1 temperature [deg K] — confirmed 35°C
    temp_k = struct.unpack_from('<H', data, 13)[0]
    t.pam1_temp_c = temp_k - 273.15

    # Bytes 17-18: Input power [10xW]
    t.input_power_w = struct.unpack_from('<H', data, 17)[0] / 10.0

    # Bytes 19-20: Forward power [W]
    t.fwd_power_w = float(struct.unpack_from('<H', data, 19)[0])

    # Bytes 21-22: Reflected power [W]
    t.refl_power_w = float(struct.unpack_from('<H', data, 21)[0])

    # Bytes 23-24: SWR [100x]
    t.swr = struct.unpack_from('<H', data, 23)[0] / 100.0

    # Bytes 35-36: VCC26 [10xV] — confirmed 24.8V
    t.vcc26_v = struct.unpack_from('<H', data, 35)[0] / 10.0

    # Bytes 33-34: VCC5 [mV]
    t.vcc5_mv = float(struct.unpack_from('<H', data, 33)[0])

    # Bytes 37-38: HV1 [10xV]
    t.hv1_v = struct.unpack_from('<H', data, 37)[0] / 10.0

    # Bytes 41-42: ID1 drain current [mA]
    t.id1_ma = float(struct.unpack_from('<H', data, 41)[0])

    # Bytes 45-46: Carrier frequency [kHz]
    t.freq_khz = struct.unpack_from('<H', data, 45)[0]

    # Byte 63: Fan speed and active LPF — confirmed 0x86 in live data
    t.fan_speed = (data[63] >> 4) & 0x0F
    t.active_lpf = data[63] & 0x0F

    # Bytes 60-62: Error code display — confirmed 0x00 (no error)
    t.error_code  = data[60]
    t.error_param = struct.unpack_from('<H', data, 61)[0]

    # Bytes 63-65: Error code display
    t.error_code  = data[63]
    t.error_param = struct.unpack_from('<H', data, 64)[0]

    return t


@dataclass
class FaultStatus:
    """Parsed fault bitmask from message 0x21 (24 bytes)."""
    warnings: list[str] = field(default_factory=list)
    soft_faults: list[str] = field(default_factory=list)
    hard_faults: list[str] = field(default_factory=list)

    @property
    def has_hard_fault(self) -> bool:
        return len(self.hard_faults) > 0

    @property
    def has_soft_fault(self) -> bool:
        return len(self.soft_faults) > 0

    @property
    def has_warning(self) -> bool:
        return len(self.warnings) > 0

    @property
    def severity(self) -> str:
        if self.has_hard_fault:
            return "HARD_FAULT"
        if self.has_soft_fault:
            return "SOFT_FAULT"
        if self.has_warning:
            return "WARNING"
        return "OK"


# Fault bit definitions: (byte_index, bit, message, severity)
# severity: 'H'=HARD, 'S'=SOFT, 'W'=WARNING
FAULT_BITS = [
    # Byte 0 (Error Codes 0 Low)
    (0, 7, "EXCESSIVE DRIVE POWER", 'S'),
    (0, 6, "DRIVE POWER TOO HIGH", 'W'),
    (0, 5, "EXCESSIVE REFLECTED POWER", 'S'),
    (0, 4, "REFLECTED POWER WARNING", 'W'),
    (0, 3, "DRIVE POWER AT WRONG TIME", 'H'),
    (0, 2, "OUTPUT RELAY OPEN SHOULD BE CLOSED", 'H'),
    (0, 1, "OUTPUT RELAY CLOSED SHOULD BE OPEN", 'W'),
    (0, 0, "HOT SWITCHING ATTEMPT", 'W'),
    # Byte 1 (Error Codes 0 High)
    (1, 7, "REMOVE DRIVE POWER IMMEDIATELY", 'W'),
    (1, 6, "STOP TRANSMISSION FIRST", 'W'),
    (1, 5, "PA LOAD SWR TOO HIGH", 'S'),
    (1, 4, "RF DETECTED AT WRONG TIME", 'W'),
    (1, 3, "OUTPUT DISBALANCE", 'S'),
    (1, 2, "FREQUENCY VIOLATION", 'S'),
    (1, 1, "DRIVE FREQUENCY OUT OF RANGE", 'S'),
    (1, 0, "HOT SWITCHING ATTEMPT", 'S'),
    # Byte 2 (Error Codes 1 Low)
    (2, 7, "LPF FAN SPEED TOO LOW", 'H'),
    (2, 6, "PAM2 FAN SPEED TOO LOW", 'H'),
    (2, 5, "PAM1 FAN SPEED TOO LOW", 'H'),
    (2, 3, "26V TOO HIGH", 'H'),
    (2, 2, "26V TOO LOW", 'H'),
    (2, 1, "5V TOO HIGH", 'H'),
    (2, 0, "5V TOO LOW", 'H'),
    # Byte 3 (Error Codes 1 High)
    (3, 7, "PAM2 EXCESSIVE TEMPERATURE", 'S'),
    (3, 6, "PAM1 EXCESSIVE TEMPERATURE", 'S'),
    (3, 5, "PAM2 TEMPERATURE TOO HIGH", 'W'),
    (3, 4, "PAM1 TEMPERATURE TOO HIGH", 'W'),
    (3, 3, "PAM2 DISSIPATION POWER WARNING", 'W'),
    (3, 2, "PAM1 DISSIPATION POWER WARNING", 'W'),
    (3, 1, "PAM2 DISSIPATION POWER TOO HIGH", 'S'),
    (3, 0, "PAM1 DISSIPATION POWER TOO HIGH", 'S'),
    # Bytes 4-5: PAM1 current/voltage faults
    (4, 5, "PAM1 EXCESSIVE CURRENT", 'S'),
    (4, 4, "PAM1 CURRENT WARNING", 'W'),
    (4, 3, "PAM1 IDLE CURRENT TOO LOW", 'S'),
    (4, 1, "PAM1 HV TOO HIGH", 'H'),
    (4, 0, "PAM1 HV TOO LOW", 'H'),
    (5, 1, "PAM1 EXCESSIVE CURRENT CHECK SWR", 'S'),
    # Bytes 8-9: PAM2 current/voltage faults
    (8, 5, "PAM2 EXCESSIVE CURRENT", 'S'),
    (8, 4, "PAM2 CURRENT WARNING", 'W'),
    (8, 3, "PAM2 IDLE CURRENT TOO LOW", 'S'),
    (8, 1, "PAM2 HV TOO HIGH", 'H'),
    (8, 0, "PAM2 HV TOO LOW", 'H'),
    # ATU / ASEL errors (bytes 12-13)
    (12, 7, "ATU POWER SWITCH ALARM AT POWER ON", 'W'),
    (12, 6, "ATU POWER SWITCH ALARM", 'W'),
    (12, 5, "ATU MODEM EXCESSIVE TEMPERATURE", 'W'),
    (12, 3, "PSU2 EXCESSIVE TEMPERATURE", 'S'),
    (12, 2, "PSU1 EXCESSIVE TEMPERATURE", 'S'),
    (12, 1, "PSU2 CONTROL MALFUNCTION", 'H'),
    (12, 0, "PSU1 CONTROL MALFUNCTION", 'H'),
    # Communication errors (bytes 16-17)
    (16, 7, "NO ANTENNA SETTINGS PREPARED", 'W'),
    (16, 5, "AMP-ASEL COMMUNICATION ERROR", 'W'),
    (16, 4, "ASEL-AMP COMMUNICATION ERROR", 'W'),
    (16, 3, "ASEL NOT RESPONDING", 'W'),
    (16, 2, "AMP-ATU COMMUNICATION ERROR", 'W'),
    (16, 1, "ATU-AMP COMMUNICATION ERROR", 'W'),
    (16, 0, "ATU NOT RESPONDING", 'W'),
    (17, 2, "ATU TUNING CYCLE UNSUCCESSFUL", 'W'),
    (17, 0, "ATU CANNOT RE-TUNE UNTIL RF ABSENT", 'W'),
    # CAT error (byte 14)
    (14, 0, "CAT ERROR", 'W'),
]


def parse_fault_codes(data: bytes) -> FaultStatus:
    """
    Parse message 0x21 data field (20 bytes of error codes).
    data here is the raw data field from the frame.
    """
    status = FaultStatus()
    for byte_idx, bit, msg, severity in FAULT_BITS:
        if byte_idx < len(data) and (data[byte_idx] >> bit) & 1:
            if severity == 'H':
                status.hard_faults.append(msg)
            elif severity == 'S':
                status.soft_faults.append(msg)
            else:
                status.warnings.append(msg)
    return status


# ---------------------------------------------------------------------------
# Frame reader (state machine for async serial reading)
# ---------------------------------------------------------------------------

MIN_FRAME_LEN = 4    # start + address + length + checksum, zero data bytes
MAX_FRAME_LEN = 96   # largest real frame is FULL_TELEMETRY at 72 bytes


class FrameReader:
    """
    Stateful frame reader for serial input.
    Feed bytes one at a time via feed(); get complete frames from frames list.
    """
    def __init__(self):
        self._buf = bytearray()
        self._expecting = 0   # bytes remaining in current frame
        self.frames: list[bytes] = []

    def _resync(self, reason: str):
        """Drop the in-progress frame and go back to hunting for a start
        byte. Without this, a single corrupted length byte (e.g. < 3, from
        a noise hit on the serial line) sends _expecting negative — and
        counting down by 1 from a negative number never lands back on
        exactly 0, so the old code would never reset and would swallow
        every byte for the rest of the process's life."""
        logger.warning(
            f"ACOM frame resync: {reason} "
            f"(buf={bytes(self._buf).hex(' ').upper()})")
        self._buf = bytearray()
        self._expecting = 0

    def feed(self, data: bytes):
        for byte in data:
            if not self._buf:
                # Waiting for start byte
                if byte == START_BYTE:
                    self._buf.append(byte)
            elif len(self._buf) == 1:
                # Got address
                self._buf.append(byte)
            elif len(self._buf) == 2:
                # Got length — now we know total frame size
                length = byte
                if length < MIN_FRAME_LEN or length > MAX_FRAME_LEN:
                    self._resync(f"bad length byte 0x{length:02X}")
                    # This byte might itself be a start byte of the next
                    # real frame — give it a chance rather than discarding it.
                    if byte == START_BYTE:
                        self._buf.append(byte)
                    continue
                self._buf.append(byte)
                self._expecting = length - 3  # already have 3 bytes
            else:
                self._buf.append(byte)
                self._expecting -= 1
                if len(self._buf) > MAX_FRAME_LEN:
                    # Backstop in case of any other path that fails to
                    # converge — never accumulate unbounded garbage.
                    self._resync("frame exceeded max length")
                elif self._expecting == 0:
                    frame = bytes(self._buf)
                    if verify_frame(frame):
                        self.frames.append(frame)
                    else:
                        logger.warning(
                            f"ACOM checksum failed, discarding frame: "
                            f"{frame.hex(' ').upper()}")
                    # Reset regardless — discard corrupt frames
                    self._buf = bytearray()
                    self._expecting = 0


# ---------------------------------------------------------------------------
# Unit tests (run directly: python acom_protocol.py)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("ACOM Protocol Library — Self Test")
    print("=" * 50)

    # Test checksum
    ack = cmd_ack(0x2F)
    assert verify_frame(ack), "ACK frame checksum failed"
    print(f"ACK(0x2F):            {ack.hex(' ').upper()}")

    # Test enable/disable telemetry
    en = cmd_enable_telemetry()
    dis = cmd_disable_telemetry()
    assert verify_frame(en)
    assert verify_frame(dis)
    print(f"Enable telemetry:     {en.hex(' ').upper()}")
    print(f"Disable telemetry:    {dis.hex(' ').upper()}")

    # Test mode commands
    stb = cmd_standby()
    opr = cmd_operate()
    off = cmd_power_off()
    txp = cmd_tx_prohibit()
    txa = cmd_tx_allow()
    assert all(verify_frame(f) for f in [stb, opr, off, txp, txa])
    print(f"Standby:              {stb.hex(' ').upper()}")
    print(f"Operate:              {opr.hex(' ').upper()}")
    print(f"Power off:            {off.hex(' ').upper()}")
    print(f"TX prohibit:          {txp.hex(' ').upper()}")
    print(f"TX allow:             {txa.hex(' ').upper()}")

    # Test next-antenna cycle command
    next_ant = cmd_next_antenna()
    assert verify_frame(next_ant)
    print(f"Next antenna:         {next_ant.hex(' ').upper()}")

    # Test frequency → band conversion
    tests = [
        (14_074_000, Band.B20M, "20m FT8"),
        (7_074_000,  Band.B40M, "40m FT8"),
        (3_573_000,  Band.B80M, "80m FT8"),
        (50_000_000, None,      "6m (out of range for 1200S)"),
    ]
    print("\nFrequency → Band:")
    for freq, expected, label in tests:
        result = freq_to_band(freq)
        status = "✓" if result == expected else "✗"
        name = band_name(result) if result else "None"
        print(f"  {status} {label}: {freq/1e6:.3f} MHz → {name}")

    # Test FrameReader
    print("\nFrameReader test:")
    reader = FrameReader()
    test_frame = cmd_operate()
    # Feed byte by byte
    for b in test_frame:
        reader.feed(bytes([b]))
    assert len(reader.frames) == 1
    assert reader.frames[0] == test_frame
    print(f"  ✓ Single frame assembled correctly from byte stream")

    # Test with noise prefix
    reader2 = FrameReader()
    noise = bytes([0x00, 0xFF, 0x12, 0x34])
    reader2.feed(noise + test_frame)
    assert len(reader2.frames) == 1
    print(f"  ✓ Frame correctly extracted after noise bytes")

    print("\nAll tests passed.")
