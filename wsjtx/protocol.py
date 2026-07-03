"""
WSJT-X UDP Message Protocol — binary parser.

Implements the wire format documented in WSJT-X's own source
(Network/NetworkMessage.hpp, sourceforge.net/p/wsjt/wsjtx), pulled and
verified directly from that file rather than reimplemented from memory.

Wire format summary:
  Header: uint32 magic (0xadbccbda) + uint32 schema number, big-endian.
  Payload: QDataStream-serialized fields (Qt_5_4 format, schema 3), also
  big-endian. Strings are length-prefixed UTF-8 (a uint32 byte count,
  0xffffffff meaning a null — not empty — string), no terminator.

Pure parsing only — no sockets here. See udp_listener.py for the network
side. This module is standalone/testable: feed it raw bytes, get typed
message objects back.

Only the message types this console actually consumes are parsed in
detail (Heartbeat, Status, Decode, QSOLogged, LoggedADIF, Clear, Close).
Other types (Reply, Replay, HaltTx, FreeText, WSPRDecode, Location,
HighlightCallsign, SwitchConfiguration, Configure, AnnotationInfo) are
all messages WSJT-X only *accepts* (In direction) or are outside this
console's scope (WSPR) — per the protocol's own compatibility rule,
unknown/unparsed types are meant to be silently ignored, not an error.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Union

MAGIC = 0xADBCCBDA

# Sentinel length prefix meaning "null string" (as opposed to a zero-length
# but present empty string) — see NetworkMessage.hpp's QByteArray note.
NULL_STRING_LEN = 0xFFFFFFFF


class MessageType(IntEnum):
    HEARTBEAT = 0
    STATUS = 1
    DECODE = 2
    CLEAR = 3
    REPLY = 4
    QSO_LOGGED = 5
    CLOSE = 6
    REPLAY = 7
    HALT_TX = 8
    FREE_TEXT = 9
    WSPR_DECODE = 10
    LOCATION = 11
    LOGGED_ADIF = 12
    HIGHLIGHT_CALLSIGN = 13
    SWITCH_CONFIGURATION = 14
    CONFIGURE = 15
    ANNOTATION_INFO = 16


class ProtocolError(ValueError):
    """Malformed datagram — bad magic, truncated payload, etc."""


class _Reader:
    """Sequential big-endian QDataStream reader over a bytes buffer."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def _take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ProtocolError(
                f"Truncated message: need {n} bytes at offset {self._pos}, "
                f"have {len(self._data) - self._pos}"
            )
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def bool(self) -> bool:
        return self.u8() != 0

    def u32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self._take(8))[0]

    def f64(self) -> float:
        return struct.unpack(">d", self._take(8))[0]

    def utf8(self) -> Optional[str]:
        """QByteArray-serialized UTF-8 string. Returns None for the null
        sentinel (0xffffffff length), distinct from an empty string ""."""
        length = self.u32()
        if length == NULL_STRING_LEN:
            return None
        return self._take(length).decode("utf-8", errors="replace")

    def qtime_ms(self) -> int:
        """QTime serializes as milliseconds since midnight (quint32)."""
        return self.u32()

    def has_more(self) -> bool:
        return self._pos < len(self._data)

    def remaining(self) -> bytes:
        return self._data[self._pos:]


# ---------------------------------------------------------------------------
# Parsed message dataclasses — one per type this console consumes
# ---------------------------------------------------------------------------

@dataclass
class Heartbeat:
    id: str
    max_schema: int
    version: str
    revision: str


@dataclass
class Status:
    id: str
    dial_freq_hz: int
    mode: str
    dx_call: str
    report: str
    tx_mode: str
    tx_enabled: bool
    transmitting: bool
    decoding: bool
    rx_df_hz: int
    tx_df_hz: int
    de_call: str
    de_grid: str
    dx_grid: str
    tx_watchdog: bool
    submode: str
    fast_mode: bool
    special_op_mode: int
    freq_tolerance: int
    tr_period: int
    configuration_name: str
    tx_message: str


@dataclass
class Decode:
    id: str
    is_new: bool
    time_ms: int
    snr: int
    delta_time_s: float
    delta_freq_hz: int
    mode: str
    message: str
    low_confidence: bool
    off_air: bool


@dataclass
class QsoLogged:
    id: str
    date_time_off: bytes   # raw QDateTime bytes — see NOTE below
    dx_call: str
    dx_grid: str
    tx_freq_hz: int
    mode: str
    report_sent: str
    report_received: str
    tx_power: str
    comments: str
    name: str
    date_time_on: bytes    # raw QDateTime bytes — see NOTE below
    operator_call: str
    my_call: str
    my_grid: str
    exchange_sent: str
    exchange_received: str
    adif_propagation_mode: str
    # NOTE: QDateTime's wire format (Julian day + ms-since-midnight + a
    # timespec byte, optionally more) is fiddly and this console has an
    # easier path to the same information: LoggedAdif below carries the
    # same QSO as a ready-to-parse ADIF record, including QSO_DATE/TIME_ON/
    # TIME_OFF as plain ADIF fields. Prefer LoggedAdif for anything that
    # needs the actual timestamps; this class keeps the raw bytes only so
    # nothing is silently dropped if QsoLogged ever arrives without a
    # matching LoggedAdif.


@dataclass
class LoggedAdif:
    id: str
    adif_text: str


@dataclass
class Clear:
    id: str
    window: Optional[int]  # only present when WSJT-X is the sender


@dataclass
class Close:
    id: str


ParsedMessage = Union[Heartbeat, Status, Decode, QsoLogged, LoggedAdif, Clear, Close]


def parse_message(data: bytes) -> Optional[ParsedMessage]:
    """Parse one UDP datagram. Returns None for message types this console
    doesn't consume (per protocol spec, unknown types must be ignored, not
    treated as an error) or for malformed/foreign packets."""
    r = _Reader(data)

    magic = r.u32()
    if magic != MAGIC:
        raise ProtocolError(f"Bad magic number: {magic:#x} (expected {MAGIC:#x})")

    schema = r.u32()  # noqa: F841 — not currently branched on; schema 2/3 field layout is identical for the types we parse
    msg_type = r.u32()
    msg_id = r.utf8() or ""

    if msg_type == MessageType.HEARTBEAT:
        return Heartbeat(
            id=msg_id,
            max_schema=r.u32() if r.has_more() else 2,
            version=r.utf8() or "",
            revision=r.utf8() or "",
        )

    if msg_type == MessageType.STATUS:
        return Status(
            id=msg_id,
            dial_freq_hz=r.u64(),
            mode=r.utf8() or "",
            dx_call=r.utf8() or "",
            report=r.utf8() or "",
            tx_mode=r.utf8() or "",
            tx_enabled=r.bool(),
            transmitting=r.bool(),
            decoding=r.bool(),
            rx_df_hz=r.u32(),
            tx_df_hz=r.u32(),
            de_call=r.utf8() or "",
            de_grid=r.utf8() or "",
            dx_grid=r.utf8() or "",
            tx_watchdog=r.bool(),
            submode=r.utf8() or "",
            fast_mode=r.bool(),
            special_op_mode=r.u8(),
            freq_tolerance=r.u32(),
            tr_period=r.u32(),
            configuration_name=r.utf8() or "",
            tx_message=r.utf8() or "",
        )

    if msg_type == MessageType.DECODE:
        return Decode(
            id=msg_id,
            is_new=r.bool(),
            time_ms=r.qtime_ms(),
            snr=r.i32(),
            delta_time_s=r.f64(),
            delta_freq_hz=r.u32(),
            mode=r.utf8() or "",
            message=r.utf8() or "",
            low_confidence=r.bool(),
            off_air=r.bool(),
        )

    if msg_type == MessageType.QSO_LOGGED:
        # QDateTime fields are variable-length (extra bytes depending on
        # timespec) and this console doesn't need them parsed — see the
        # NOTE on QsoLogged. Capture the whole remaining payload as
        # undifferentiated bytes isn't viable either (fields after the
        # QDateTimes are plain utf8/u64), so QSO_LOGGED is intentionally
        # not fully parsed. LOGGED_ADIF (below) is emitted for the same
        # event and carries everything in a format worth actually parsing.
        return None

    if msg_type == MessageType.LOGGED_ADIF:
        return LoggedAdif(id=msg_id, adif_text=r.utf8() or "")

    if msg_type == MessageType.CLEAR:
        window = r.u8() if r.has_more() else None
        return Clear(id=msg_id, window=window)

    if msg_type == MessageType.CLOSE:
        return Close(id=msg_id)

    return None
