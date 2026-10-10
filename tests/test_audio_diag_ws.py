"""The audio_diag /ws command: browser audio worklet diagnostics into the server log.

console.html forwards the worklet's events (catch-up, underruns, flush, 5 s
latency stats) and AudioContext state changes, at most a line a second per
receiver. The server's part is one tagged log line per message and nothing
else: no reply, no state touched.
"""
import json
import logging
from pathlib import Path

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


async def diag(caplog, **msg):
    ws = FakeWebSocket()
    with caplog.at_level(logging.INFO, logger=server.logger.name):
        await server.handle_ws_command(json.dumps({"cmd": "audio_diag", **msg}), ws)
    lines = [r.getMessage() for r in caplog.records if "Audio worklet" in r.getMessage()]
    caplog.clear()
    return ws, lines


async def test_one_tagged_line_and_no_reply(ws_bridge, caplog):
    ws, lines = await diag(caplog, rx="RX1", text="catchup_enter lat=262 ms")
    assert lines == ["Audio worklet RX1: catchup_enter lat=262 ms"]
    assert ws.sent == []


async def test_rx2_sends_with_channel_b(ws_bridge, caplog):
    """RX2's commands all go through send2(), which adds channel:'B'."""
    ws, lines = await diag(caplog, rx="RX2", channel="B",
                           text="stats 5s lat min/mean/max=41/55/70 ms frames=611 samples=80041")
    assert lines == ["Audio worklet RX2: stats 5s lat min/mean/max=41/55/70 ms frames=611 samples=80041"]
    assert ws.sent == []


async def test_text_is_bounded_and_single_line(ws_bridge, caplog):
    _, lines = await diag(caplog, rx="RX9", text="a\nb\r\x1b[31m" + "x" * 1000)
    assert len(lines) == 1
    assert lines[0].startswith("Audio worklet RX?: ab[31m")
    assert "\n" not in lines[0] and "\x1b" not in lines[0]
    assert len(lines[0]) <= len("Audio worklet RX?: ") + 400


async def test_missing_fields_do_not_raise(ws_bridge, caplog):
    ws, lines = await diag(caplog)
    assert lines == ["Audio worklet RX?: "]
    assert ws.sent == []


def test_page_forwards_diagnostics_for_both_receivers():
    """Source-level: the helper is at top level (both receiver modules use it), and each
    receiver's worklet port and AudioContext feed it."""
    html = Path("dashboard/console.html").read_text()
    helper = html.index("function makeAudioDiag(rx, sendFn)")
    assert helper < html.index("const Panadapter = (function () {")
    assert "const audioDiag = makeAudioDiag('RX1', send);" in html
    assert "const audioDiag2 = makeAudioDiag('RX2', send2);" in html
    assert "audioDiag.fromWorklet(evt.data)" in html and "audioDiag2.fromWorklet(evt.data)" in html
    assert "audioDiag.push('context ' + audioCtx.state)" in html
    assert "audioDiag2.push('context ' + audioCtx2.state)" in html
    assert "cmd: 'audio_diag'" in html
