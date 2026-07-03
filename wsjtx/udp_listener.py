"""
WSJT-X UDP Listener — passive third listener on the same multicast group
RUMLogNG and GridTracker2 already use.

Confirmed from this station's own ~/Library/Preferences/WSJT-X.ini:

    UDPServer=224.0.0.1
    UDPServerPort=2237
    UDPInterface=lo0

WSJT-X already broadcasts to a multicast group (not a single unicast
destination) — that's exactly why RUMLogNG and GridTracker2 both work off
it simultaneously today. Any number of additional sockets can join the
same multicast group and receive identical copies of every datagram, with
zero configuration changes to WSJT-X and zero effect on existing
listeners. This module only ever reads; it never sends anything back to
WSJT-X (no Reply/HaltTx/FreeText/Configure etc.) — a deliberate scope
limit so there's no way for this to interfere with actual operation.

lo0's address is always 127.0.0.1 — hardcoded to match WSJT-X's own
UDPInterface=lo0 setting rather than resolved dynamically, since this is
this station's specific, already-working configuration, not a general
network-discovery problem.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable, Coroutine, Optional

from wsjtx.protocol import (
    ParsedMessage, Heartbeat, Status, Decode, LoggedAdif, Clear, Close,
    ProtocolError, parse_message,
)

logger = logging.getLogger(__name__)

MULTICAST_GROUP = "224.0.0.1"
MULTICAST_PORT = 2237
INTERFACE_ADDR = "127.0.0.1"  # lo0

HeartbeatCallback = Callable[[Heartbeat], Coroutine]
StatusCallback = Callable[[Status], Coroutine]
DecodeCallback = Callable[[Decode], Coroutine]
LoggedAdifCallback = Callable[[LoggedAdif], Coroutine]


def _make_multicast_socket(group: str, port: int, iface_addr: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # SO_REUSEPORT (not on all platforms) lets multiple independent
    # processes each bind the same multicast port — belt-and-suspenders
    # alongside SO_REUSEADDR, since RUMLogNG/GridTracker2/this console are
    # three separate processes all wanting the same port.
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    mreq = socket.inet_aton(group) + socket.inet_aton(iface_addr)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    return sock


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, listener: "WsjtxListener"):
        self._listener = listener

    def datagram_received(self, data: bytes, addr):
        self._listener._on_datagram(data, addr)

    def error_received(self, exc: Exception):
        logger.warning(f"WSJT-X UDP socket error: {exc}")


class WsjtxListener:
    """Owns the multicast socket and dispatches parsed messages to
    registered callbacks. Mirrors AcomBridge's on_state_change() /
    on_trend_sample() registration style."""

    def __init__(self,
                 group: str = MULTICAST_GROUP,
                 port: int = MULTICAST_PORT,
                 iface_addr: str = INTERFACE_ADDR):
        self._group = group
        self._port = port
        self._iface_addr = iface_addr
        self._transport: Optional[asyncio.DatagramTransport] = None

        self._heartbeat_callbacks: list[HeartbeatCallback] = []
        self._status_callbacks: list[StatusCallback] = []
        self._decode_callbacks: list[DecodeCallback] = []
        self._logged_adif_callbacks: list[LoggedAdifCallback] = []

        # Last-seen state, useful for anything that wants a snapshot rather
        # than subscribing to the callback stream (e.g. a REST endpoint).
        self.last_status: Optional[Status] = None
        self.last_heartbeat: Optional[Heartbeat] = None
        self.connected = False

    def on_heartbeat(self, cb: HeartbeatCallback):
        self._heartbeat_callbacks.append(cb)

    def on_status(self, cb: StatusCallback):
        self._status_callbacks.append(cb)

    def on_decode(self, cb: DecodeCallback):
        self._decode_callbacks.append(cb)

    def on_logged_adif(self, cb: LoggedAdifCallback):
        self._logged_adif_callbacks.append(cb)

    async def start(self):
        loop = asyncio.get_event_loop()
        sock = _make_multicast_socket(self._group, self._port, self._iface_addr)
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), sock=sock,
        )
        logger.info(
            f"WSJT-X UDP listener joined {self._group}:{self._port} "
            f"on interface {self._iface_addr}"
        )

    async def stop(self):
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.connected = False
        logger.info("WSJT-X UDP listener stopped")

    def _on_datagram(self, data: bytes, addr):
        try:
            msg = parse_message(data)
        except ProtocolError as e:
            # A malformed/foreign packet on this multicast group shouldn't
            # take the listener down — log and move on.
            logger.debug(f"Dropped malformed WSJT-X UDP packet from {addr}: {e}")
            return

        if msg is None:
            return  # message type we don't consume — not an error, see protocol.py

        self.connected = True
        asyncio.create_task(self._dispatch(msg))

    async def _dispatch(self, msg: ParsedMessage):
        if isinstance(msg, Heartbeat):
            self.last_heartbeat = msg
            for cb in self._heartbeat_callbacks:
                await self._safe_call(cb, msg)
        elif isinstance(msg, Status):
            self.last_status = msg
            for cb in self._status_callbacks:
                await self._safe_call(cb, msg)
        elif isinstance(msg, Decode):
            for cb in self._decode_callbacks:
                await self._safe_call(cb, msg)
        elif isinstance(msg, LoggedAdif):
            for cb in self._logged_adif_callbacks:
                await self._safe_call(cb, msg)
        elif isinstance(msg, (Clear, Close)):
            pass  # nothing currently needs these; parsed for completeness

    @staticmethod
    async def _safe_call(cb, msg):
        try:
            await cb(msg)
        except Exception as e:
            logger.error(f"WSJT-X listener callback error ({cb!r}): {e}")


# Module-level singleton — mirrors the config/station_profile.py pattern.
wsjtx_listener = WsjtxListener()
