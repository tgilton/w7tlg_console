"""T4 — WS/REST error-contract consistency in dashboard/server.py's
handle_ws_command: the bridge=None guard now sends a client-visible error
(matching the REST endpoints' HTTPException(503, ...) contract) instead of
silently returning nothing, and the trailing exception handler no longer
echoes raw Python exception text back to the WebSocket client. Same
fake-bridge/FakeWebSocket pattern as test_ws_frequency_mode_validation.py —
no live hardware, no real FastAPI/WebSocket connection."""
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


async def test_ws_command_with_no_bridge_sends_client_visible_error(monkeypatch):
    monkeypatch.setattr(server, "bridge", None)
    ws = FakeWebSocket()

    await server.handle_ws_command(json.dumps({"cmd": "set_mode"}), ws)

    assert ws.sent == [{"type": "error", "message": "Bridge not initialized"}]


async def test_ws_command_internal_exception_does_not_leak_raw_message(
        ws_bridge, caplog):
    ws = FakeWebSocket()

    # set_mode_op with no "mode" key raises a KeyError inside the try block —
    # exercises the trailing `except Exception` handler.
    await server.handle_ws_command(json.dumps({"cmd": "set_mode_op"}), ws)

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["message"] == "Internal error processing command"
    # the raw exception text must never reach the client...
    assert "mode" not in ws.sent[0]["message"]
    # ...but it's still logged server-side for diagnosis.
    assert "WebSocket command error" in caplog.text
