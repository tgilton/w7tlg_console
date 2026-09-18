"""T2b — ACOM serial port rediscovery fix.

Covers AUDIT.md Finding 4: `ACOM_PORT or find_acom_port()` never actually
calls find_acom_port() because ACOM_PORT is always a non-empty string, so a
stale hardcoded path (e.g. after a macOS USB-serial re-enumeration) leaves
amp control permanently disabled. Two things are tested:

1. dashboard.server.resolve_acom_port() actually falls back to
   find_acom_port() when the configured path doesn't exist.
2. AcomSerial's new port_resolver hook re-resolves the port on each
   reconnect attempt instead of retrying a dead path forever.
"""
import pytest

import dashboard.server as server
from amplifier.acom_serial import AcomSerial


# ---------------------------------------------------------------------------
# dashboard.server.resolve_acom_port
# ---------------------------------------------------------------------------

def test_resolve_uses_configured_port_when_it_exists(monkeypatch):
    monkeypatch.setattr(server, "ACOM_PORT", "/dev/fake-configured")
    monkeypatch.setattr(server.os.path, "exists", lambda p: p == "/dev/fake-configured")
    monkeypatch.setattr(server, "find_acom_port", lambda: pytest.fail(
        "find_acom_port() should not run when the configured port exists"))

    assert server.resolve_acom_port() == "/dev/fake-configured"


def test_resolve_falls_back_to_discovery_when_configured_port_is_stale(monkeypatch):
    """This is the dead-fallback bug itself: a stale ACOM_PORT must not
    permanently shadow find_acom_port()."""
    monkeypatch.setattr(server, "ACOM_PORT", "/dev/cu.usbserial-STALE")
    monkeypatch.setattr(server.os.path, "exists", lambda p: False)
    monkeypatch.setattr(server, "find_acom_port", lambda: "/dev/cu.usbserial-NEW")

    assert server.resolve_acom_port() == "/dev/cu.usbserial-NEW"


def test_resolve_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(server, "ACOM_PORT", "/dev/cu.usbserial-STALE")
    monkeypatch.setattr(server.os.path, "exists", lambda p: False)
    monkeypatch.setattr(server, "find_acom_port", lambda: None)

    assert server.resolve_acom_port() is None


# ---------------------------------------------------------------------------
# AcomSerial.port_resolver — reconnect-time rediscovery
# ---------------------------------------------------------------------------

async def test_connect_without_resolver_keeps_fixed_port():
    """No resolver configured (matches every pre-existing construction
    site) -> behavior is unchanged: _connect() attempts self.port as-is.
    There's no real serial device at this path, so the open fails (caught
    internally, same as any other unreachable port) — the point here is
    only that self.port is untouched."""
    driver = AcomSerial(port="/dev/cu.usbserial-FIXED")

    ok = await driver._connect()

    assert ok is False
    assert driver.port == "/dev/cu.usbserial-FIXED"


async def test_connect_re_resolves_port_before_each_attempt():
    calls = []

    def resolver():
        calls.append(1)
        return "/dev/cu.usbserial-REENUMERATED"

    driver = AcomSerial(port="/dev/cu.usbserial-STALE", port_resolver=resolver)

    ok = await driver._connect()  # real open fails (no such device); that's fine

    assert ok is False
    assert driver.port == "/dev/cu.usbserial-REENUMERATED"
    assert len(calls) == 1


async def test_connect_keeps_current_port_when_resolver_finds_nothing():
    """If discovery comes up empty (e.g. device unplugged), keep retrying
    the last known port rather than clearing it."""
    driver = AcomSerial(port="/dev/cu.usbserial-LASTKNOWN", port_resolver=lambda: None)

    ok = await driver._connect()

    assert ok is False
    assert driver.port == "/dev/cu.usbserial-LASTKNOWN"
