"""T1 — Tier A enforcement in dashboard/server.py's set_frequency/set_mode/
set_panadapter_freq WS handlers. Drives handle_ws_command directly against
the real AcomBridge wired to T0's fake rig (same pattern as
test_bridge_with_fakes.py) plus T0's fake SDR, with dashboard.server's
module globals monkeypatched to point at them — no live hardware, no real
FastAPI/WebSocket connection."""
import json

import pytest

import dashboard.server as server
from amplifier.acom_bridge import AcomBridge


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))


@pytest.fixture
def ws_bridge(fake_rig, fake_amp, monkeypatch):
    bridge = AcomBridge(rig=fake_rig, amp=fake_amp)
    monkeypatch.setattr(server, "bridge", bridge)
    return bridge


@pytest.fixture
async def ws_sdr(fake_sdr, monkeypatch):
    await fake_sdr.start()  # available=True, matches a live SdrClient session
    monkeypatch.setattr(server, "sdr", fake_sdr)
    return fake_sdr


async def test_manual_set_frequency_accepts_legitimate_out_of_band_wwv(
        ws_bridge, fake_rig):
    """The operator's own manual-tuning need: WWV at 10 MHz is off any
    amateur band but within hardware range — Tier A must let it through
    on the manual UI path (only the advisor's qsy path gets Tier B)."""
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_frequency", "freq_hz": 10_000_000}), ws)

    assert ws.sent == [{"type": "cmd_response", "cmd": "set_frequency", "ok": True}]
    assert fake_rig.calls == [("set_frequency", (10_000_000,), {})]


async def test_manual_set_frequency_rejects_out_of_hardware_range(
        ws_bridge, fake_rig):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_frequency", "freq_hz": 900_000_000}), ws)

    assert ws.sent[0]["ok"] is False
    assert "error" in ws.sent[0]
    assert fake_rig.calls == []  # never reached RigctldClient


async def test_manual_set_mode_accepts_valid_mode(ws_bridge, fake_rig):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_mode", "mode": "USB"}), ws)

    assert ws.sent == [{"type": "cmd_response", "cmd": "set_mode", "ok": True}]
    assert fake_rig.calls == [("set_mode", ("USB", 0), {})]


async def test_manual_set_mode_rejects_unknown_mode_string(ws_bridge, fake_rig):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_mode", "mode": "GARBAGE"}), ws)

    assert ws.sent[0]["ok"] is False
    assert "error" in ws.sent[0]
    assert fake_rig.calls == []


async def test_panadapter_freq_accepts_out_of_band_but_in_hardware_range(
        ws_bridge, ws_sdr):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_panadapter_freq", "channel": "B",
                    "freq_hz": 10_000_000}), ws)

    assert ws.sent == [{"type": "cmd_response", "cmd": "set_panadapter_freq", "ok": True}]
    assert ws_sdr.rf_freq_hz_b == 10_000_000.0


async def test_panadapter_freq_rejects_out_of_hardware_range(ws_bridge, ws_sdr):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_panadapter_freq", "channel": "B",
                    "freq_hz": 900_000_000}), ws)

    assert ws.sent[0]["ok"] is False
    assert "error" in ws.sent[0]
    # untouched — still at FakeSdrClient's constructor default
    assert ws_sdr.rf_freq_hz_b == 14_074_000.0
