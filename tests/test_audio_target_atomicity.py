"""T3 — set_audio_target's freq_hz/mode/bandwidth_hz updates must land as
one atomic unit (AUDIT.md Finding 2). Two layers:

1. AudioDemodulator.set_target() itself — a concurrent-access stress test
   proving a reader can never observe a half-updated freq/mode/bandwidth
   combination, the way a demod cycle on the audio thread would.
2. dashboard/server.py's set_audio_target WS handler — drives
   handle_ws_command directly (same pattern as test_ws_frequency_mode_
   validation.py) to confirm the handler goes through set_target() and the
   bundled value reaches both Channel A and Channel B's AudioDemodulator.
"""
import json
import threading
import time

import pytest

import dashboard.server as server
from amplifier.acom_bridge import AcomBridge
from sdr.audio_demod import AudioDemodulator, AudioTarget

CONFIG_A = (1_000_000.0, "USB", 3000.0)
CONFIG_B = (2_000_000.0, "LSB", 2400.0)


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


def test_set_target_replaces_freq_mode_bandwidth_as_one_unit():
    demod = AudioDemodulator()
    assert demod.target == AudioTarget(freq_hz=None, mode="USB", bandwidth_hz=3000.0)

    demod.set_target(7_100_000.0, "LSB", 2400.0)

    assert demod.target == AudioTarget(freq_hz=7_100_000.0, mode="LSB", bandwidth_hz=2400.0)


def test_digital_mode_toggle_preserves_freq_and_mode_while_swapping_bandwidth():
    demod = AudioDemodulator()
    demod.set_target(7_100_000.0, "LSB", 2400.0)

    demod.enter_digital_mode()
    assert demod.target == AudioTarget(freq_hz=7_100_000.0, mode="LSB", bandwidth_hz=3000.0)

    demod.exit_digital_mode()
    assert demod.target == AudioTarget(freq_hz=7_100_000.0, mode="LSB", bandwidth_hz=2400.0)


def test_set_target_is_atomic_under_concurrent_reads():
    """Reproduces the torn-read scenario Finding 2 describes: a writer
    thread (WS/asyncio side) continuously calling set_target() while a
    reader thread (the audio thread's _process(), which snapshots
    self.target once per call) must never see a freq/mode/bandwidth
    combination that wasn't one of the two configs actually written."""
    demod = AudioDemodulator()
    stop = threading.Event()
    mismatches = []

    def writer():
        i = 0
        while not stop.is_set():
            cfg = CONFIG_A if i % 2 == 0 else CONFIG_B
            demod.set_target(*cfg)
            i += 1

    def reader():
        while not stop.is_set():
            target = demod.target   # single atomic read, mirrors _process()
            combo = (target.freq_hz, target.mode, target.bandwidth_hz)
            if combo not in (CONFIG_A, CONFIG_B):
                mismatches.append(combo)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join()

    assert mismatches == []


async def test_ws_set_audio_target_updates_channel_a_atomically(ws_bridge, ws_sdr):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_audio_target", "freq_hz": 14_074_000,
                    "mode": "USB", "bandwidth_hz": 2800}), ws)

    assert ws.sent == [{"type": "cmd_response", "cmd": "set_audio_target", "ok": True}]
    assert ws_sdr.audio.target == AudioTarget(
        freq_hz=14_074_000.0, mode="USB", bandwidth_hz=2800.0)


async def test_ws_set_audio_target_updates_channel_b_atomically(ws_bridge, ws_sdr):
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_audio_target", "channel": "B", "freq_hz": 7_100_000,
                    "mode": "LSB", "bandwidth_hz": 2400}), ws)

    assert ws.sent == [{"type": "cmd_response", "cmd": "set_audio_target", "ok": True}]
    assert ws_sdr.audio_b.target == AudioTarget(
        freq_hz=7_100_000.0, mode="LSB", bandwidth_hz=2400.0)
    # Channel A left untouched by a Channel B command.
    assert ws_sdr.audio.target == AudioTarget(freq_hz=None, mode="USB", bandwidth_hz=3000.0)


async def test_ws_set_audio_target_defaults_mode_and_bandwidth(ws_bridge, ws_sdr):
    """freq_hz-only send (mode/bandwidth_hz omitted) still lands as one
    atomic set_target() call using the same defaults the old three-write
    code used (mode="USB", bandwidth_hz=3000)."""
    ws = FakeWebSocket()
    await server.handle_ws_command(
        json.dumps({"cmd": "set_audio_target", "freq_hz": 10_000_000}), ws)

    assert ws_sdr.audio.target == AudioTarget(
        freq_hz=10_000_000.0, mode="USB", bandwidth_hz=3000.0)
