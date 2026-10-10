"""W7TLG_AUDIO_PROFILE: one `sec` line per second per receiver, detail only in verbose.

The profiler is driven here through its own Sink with tick() called by hand,
so nothing is written to data/audio_profile.log and no writer thread runs.
"""
import asyncio
import gc
import logging
import threading
import time

import numpy as np
import pytest

import dashboard.server as server
from sdr import audio_profile
from sdr.audio_profile import AudioProfiler, Sink
from tests.fakes.sdr import FakeSdrClient


def fields(line):
    """'… sec rx=RX1 fed=3 …' -> ('sec', {'rx': 'RX1', 'fed': '3', …})"""
    parts = line.split()
    return parts[2], dict(p.split("=", 1) for p in parts[3:])


def lines_of(path):
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Both receivers with a profiler on a private sink. Returns (sdr, sink, log path)."""
    monkeypatch.setattr(audio_profile, "ws_skips", {})
    def build(verbose=False):
        log = tmp_path / "audio_profile.log"
        sink = Sink(path=str(log), verbose=verbose)
        sdr = FakeSdrClient()
        for d in (sdr.audio, sdr.audio_b):
            d._prof = AudioProfiler(d, sink=sink)
            d.enabled = True
        return sdr, sink, log
    return build


def iteration(prof, wait_s, stages, chunk=1344):
    """One _run iteration as the hooks would record it, with the given stage times."""
    t = prof.end_iter()
    prof.begin_iter(t - wait_s, chunk)
    prof.batches = 1
    for stage, seconds in stages.items():
        prof._acc[stage] += seconds
    prof._t_start -= sum(stages.values())      # as if the work took that long
    prof.end_iter()


def test_mode_parsing():
    assert audio_profile.parse_mode(None) == (False, False)
    assert audio_profile.parse_mode("") == (False, False)
    assert audio_profile.parse_mode("0") == (False, False)
    assert audio_profile.parse_mode("1") == (True, False)
    assert audio_profile.parse_mode("verbose") == (True, True)
    assert audio_profile.parse_mode(" VERBOSE ") == (True, True)
    assert audio_profile.parse_mode("yes") == (False, False)


def test_off_by_default_no_profiler():
    assert audio_profile.ENABLED is False
    assert FakeSdrClient().audio._prof is None


def test_one_line_per_second_per_receiver(rig):
    sdr, sink, log = rig()
    x = np.zeros(1344, dtype=np.int16)
    for _ in range(5):
        sdr.audio.feed(x, x)
    for _ in range(2):
        sdr.audio_b.feed(x, x)
    p = sdr.audio._prof
    iteration(p, 0.002, {audio_profile.DEMOD: 0.0006, audio_profile.EQ: 0.0001})
    iteration(p, 0.004, {audio_profile.DEMOD: 0.0005, audio_profile.NR: 0.014,
                         audio_profile.AGC: 0.0002})
    sink.tick(late_s=0.0012)
    out = lines_of(log)
    assert len(out) == 2
    (k1, a), (k2, b) = fields(out[0]), fields(out[1])
    assert (k1, k2) == ("sec", "sec") and (a["rx"], b["rx"]) == ("RX1", "RX2")
    assert (a["fed"], a["chunk"], a["depth_max"], a["drops"]) == ("5", "1344", "5", "0")
    assert (b["fed"], b["depth_max"]) == ("2", "2")
    assert (a["iters"], a["batches"]) == ("2", "2")
    assert float(a["wait_max"]) == pytest.approx(4.0, abs=0.2)
    assert float(a["demod_max"]) == pytest.approx(0.6, abs=0.01)     # the max, not the sum
    assert float(a["eq_max"]) == pytest.approx(0.1, abs=0.01)
    assert float(a["nr_max"]) == pytest.approx(14.0, abs=0.01)
    assert float(a["agc_max"]) == pytest.approx(0.2, abs=0.01)
    assert float(a["iter_max"]) == pytest.approx(14.7, abs=0.2)
    assert a["writer_late_ms"] == "1.200"
    for key in ("feed_gap_max", "fine_max", "noise_sub_max", "digital_max", "nr_load_max",
                "publish_max", "ws_skip", "ws_skip_ms", "gc_max", "gc_gen2", "drops_total"):
        assert key in a

    sink.tick()                                   # the next second starts from zero
    k, a2 = fields(lines_of(log)[2])
    assert (a2["fed"], a2["iters"], a2["nr_max"]) == ("0", "0", "0.000")
    assert len(lines_of(log)) == 4


def test_drops_are_counted_per_second(rig, monkeypatch):
    monkeypatch.setenv("W7TLG_AUDIO_QUEUE", "4")
    sdr, sink, log = rig()
    x = np.zeros(16, dtype=np.int16)
    for _ in range(7):
        sdr.audio.feed(x, x)
    sink.tick()
    _, a = fields(lines_of(log)[0])
    assert (a["drops"], a["drops_total"], a["depth_max"]) == ("3", "3", "4")
    sdr.audio.feed(x, x)
    sink.tick()
    _, a = fields(lines_of(log)[2])
    assert (a["drops"], a["drops_total"]) == ("1", "4")
    assert all(fields(l)[0] == "sec" for l in lines_of(log))     # summary mode: no drop lines


def test_summary_mode_has_no_per_iteration_lines(rig):
    sdr, sink, log = rig(verbose=False)
    iteration(sdr.audio._prof, 0.5, {audio_profile.NR: 0.2})
    sink.tick()
    assert [fields(l)[0] for l in lines_of(log)] == ["sec", "sec"]


def test_verbose_logs_only_iterations_over_30ms_late(rig, monkeypatch):
    monkeypatch.setenv("W7TLG_AUDIO_QUEUE", "2")
    sdr, sink, log = rig(verbose=True)
    p = sdr.audio._prof
    period = 1344 / sdr.audio.input_rate_hz
    iteration(p, period, {audio_profile.DEMOD: 0.0006})                    # on time
    iteration(p, period, {audio_profile.NR: 0.014})                        # a normal NR hop
    iteration(p, period + 0.020, {audio_profile.DEMOD: 0.0006})            # 20 ms late: no
    iteration(p, period + 0.045, {audio_profile.DEMOD: 0.0006})            # late in the wait
    iteration(p, period, {audio_profile.NR: 0.060})                        # late in the work
    x = np.zeros(1344, dtype=np.int16)
    for _ in range(3):
        sdr.audio.feed(x, x)                                               # one drop
    sink.tick()
    kinds = [fields(l)[0] for l in lines_of(log)]
    assert kinds == ["slow", "slow", "drop", "sec", "sec"]
    _, s1 = fields(lines_of(log)[0])
    _, s2 = fields(lines_of(log)[1])
    assert float(s1["wait"]) > 45 and float(s1["over"]) == pytest.approx(45.6, abs=1.0)
    assert float(s2["nr"]) == pytest.approx(60.0, abs=0.01) and float(s2["over"]) > 30
    _, d = fields(lines_of(log)[2])
    assert (d["rx"], d["depth"], d["chunk"]) == ("RX1", "2", "1344")


def test_gc_pauses_are_on_the_line(rig):
    sdr, sink, log = rig()
    gc.callbacks.append(sink.gc_callback)
    try:
        gc.collect()          # full collection: generation 2
        gc.collect(0)
    finally:
        gc.callbacks.remove(sink.gc_callback)
    sink.tick()
    _, a = fields(lines_of(log)[0])
    _, b = fields(lines_of(log)[1])
    assert a["gc_gen2"] == "1" and float(a["gc_max"]) > 0
    assert (b["gc_gen2"], b["gc_max"]) == (a["gc_gen2"], a["gc_max"])    # process-wide
    sink.tick()
    _, a = fields(lines_of(log)[2])
    assert (a["gc_gen2"], a["gc_max"]) == ("0", "0.000")


def test_log_rotates_at_the_size_limit(rig, monkeypatch):
    sdr, sink, log = rig()
    monkeypatch.setattr(audio_profile, "MAX_LOG_BYTES", 2000)
    for _ in range(12):
        sink.tick()
    old = log.with_name(log.name + ".1")
    assert old.exists()
    assert old.stat().st_size >= 2000 and log.stat().st_size < 2000 + 2 * 700
    assert all(fields(l)[0] == "sec" for l in lines_of(log) + lines_of(old))


def test_real_audio_thread_fills_the_line(rig):
    """The hooks in _run / _process / _browser_chain, through a running demod thread."""
    sdr, sink, log = rig()
    d = sdr.audio
    d.rf_center_hz = 14_200_000.0
    d.set_target(14_210_000.0, "USB", 2700.0)
    d.eq_enabled = True
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    x = (np.random.default_rng(0).standard_normal(4096) * 500).astype(np.int16)
    d.start(loop)
    try:
        for _ in range(60):
            d.feed(x, x)
            time.sleep(0.002)
        deadline = time.time() + 3
        while d._q.qsize() and time.time() < deadline:
            time.sleep(0.01)
    finally:
        d.stop()
        loop.call_soon_threadsafe(loop.stop)
    sink.tick()
    _, a = fields(lines_of(log)[0])
    assert a["fed"] == "60" and a["iters"] == "60" and int(a["batches"]) == 60 * 4096 // d.batch_samples
    for key in ("iter_max", "wait_max", "demod_max", "eq_max", "agc_max", "publish_max", "feed_gap_max"):
        assert float(a[key]) > 0, key
    assert float(a["nr_max"]) == 0 and a["drops"] == "0"


# ── frames skipped by the websocket send timeout ────────────────────────

class SlowSocket:
    def __init__(self):
        self.delay = 0.0
        self.got = 0

    async def send_bytes(self, data):
        await asyncio.sleep(self.delay)
        self.got += 1


async def test_ws_send_timeouts_are_counted_and_warned_at_most_every_5s(rig, caplog):
    sdr, sink, log = rig()
    mgr = server.AudioConnectionManager("RX1")
    mgr.SEND_TIMEOUT_S = 0.01
    ws = SlowSocket()
    mgr.active.append(ws)
    frame_nr_on = b"\0" * (1703 * 2)            # 106 ms of audio
    frame_nr_off = b"\0" * (131 * 2)            # 8 ms

    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        await mgr.broadcast_audio(frame_nr_off)
        assert (mgr.skipped_frames, ws.got) == (0, 1) and caplog.records == []

        ws.delay = 0.05
        await mgr.broadcast_audio(frame_nr_on)
        await mgr.broadcast_audio(frame_nr_on)
        assert mgr.skipped_frames == 2 and ws in mgr.active      # skipped, not disconnected
        assert mgr.skipped_ms == pytest.approx(2 * 1703 / 16.0)
        msgs = [r.getMessage() for r in caplog.records]
        assert len(msgs) == 1                                    # second skip: rate-limited
        assert msgs[0].startswith("Audio WS RX1: 1 frame(s) skipped by the 10 ms send timeout, 106 ms of audio")

        ws.delay = 0.0
        await mgr.broadcast_audio(frame_nr_off)                  # still inside the 5 s
        assert len(caplog.records) == 1
        mgr._skip_next_warn = 0.0                                # 5 s later
        await mgr.broadcast_audio(frame_nr_off)
        msgs = [r.getMessage() for r in caplog.records]
        assert len(msgs) == 2 and "1 frame(s) skipped" in msgs[1] and "(2 frames, 213 ms total)" in msgs[1]
        await mgr.broadcast_audio(frame_nr_off)                  # nothing new: no more lines
        assert len(caplog.records) == 2

    sink.tick()
    _, a = fields(lines_of(log)[0])
    _, b = fields(lines_of(log)[1])
    assert (a["ws_skip"], a["ws_skip_ms"]) == ("2", "213")       # RX1's line only
    assert (b["ws_skip"], b["ws_skip_ms"]) == ("0", "0")
    sink.tick()
    assert fields(lines_of(log)[2])[1]["ws_skip"] == "0"


def test_each_receiver_has_its_own_manager_label():
    assert (server.audio_manager.label, server.audio_manager_b.label,
            server.audio_manager_0.label) == ("RX1", "RX2", "RX0")
