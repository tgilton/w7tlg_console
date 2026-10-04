"""NOISE SUB (sdr/noise_sub.py): floor estimator, detection, subtraction, the
output floor, the untouched-RX1 case, the freshness and grid fallbacks, and the
per-frame cost at 65536 bins. Synthetic data: the console averages 10 independent
frames, so the noise per bin is Gamma(K=10, mean 1) — the mean of 10 unit
exponentials. Pure numpy; nothing here touches the radio."""
import time

import numpy as np
import pytest

from sdr import noise_sub as ns

K_AVG = 10
N = 65536


def gamma_noise(rng, n=N, mean=1.0, k=K_AVG):
    return rng.gamma(shape=k, scale=mean / k, size=n)


def db(x):
    return 10 * np.log10(x)


def test_a_floor_within_0p3_db_with_5pct_lines():
    rng = np.random.default_rng(11)
    p = gamma_noise(rng)
    lines = rng.choice(N, size=N // 20, replace=False)     # 5% of bins are lines
    p[lines] *= 10 ** (10 / 10)                            # +10 dB lines
    f, s = ns.local_floor(p)
    err = db(np.mean(f)) - db(1.0)
    print(f"\n(a) true floor 0 dB, estimated {db(np.mean(f)):+.3f} dB, error {err:+.3f} dB")
    assert abs(err) < 0.3


def test_b_false_detection_with_and_without_3_of_5_rule():
    rng = np.random.default_rng(12)
    frames = 40
    above_frames = []
    for _ in range(frames):
        p = gamma_noise(rng)
        f, s = ns.local_floor(p)
        above_frames.append(p > f + 2.0 * s)
    per_frame = np.mean([a.mean() for a in above_frames])
    persisted = []
    for i in range(4, frames):
        hist = above_frames[i - 4:i + 1]
        persisted.append(ns.NoiseSubCore._persistent(list(hist)).mean())
    rule = float(np.mean(persisted))
    print(f"\n(b) pure noise, K={K_AVG}, n=2: per-frame false rate {per_frame:.5f}, "
          f"with 3-of-5 rule {rule:.7f}")
    assert per_frame < 0.1             # a sanity bound, not a claim: the measured rate is reported
    assert rule < per_frame / 10       # the rule buys at least an order of magnitude


def _line_scene(rng, bins=None, line_db=20.0, ratio=0.1):
    """RX2 has a steady line 20 dB over its floor; RX1 carries a copy at `ratio`
    of the power. Both have independent noise. The line stays on the same bins
    frame to frame (a steady line), so pass `bins` to keep them fixed."""
    if bins is None:
        bins = rng.choice(N, size=60, replace=False)
    p2 = gamma_noise(rng)
    p1 = gamma_noise(rng)
    p2[bins] += 10 ** (line_db / 10)
    p1[bins] += ratio * 10 ** (line_db / 10)
    return p1, p2, bins


def test_c_copy_removed_to_floor_at_the_right_k_and_rx1_floor_unchanged():
    rng = np.random.default_rng(13)
    core = ns.NoiseSubCore(scale_db=-10.0, n=2.0)
    bins = rng.choice(N, size=60, replace=False)
    for _ in range(6):                       # persistence needs 3 of 5 frames
        p1, p2, _ = _line_scene(rng, bins=bins)
        r = core.process(p1, p2)
    f1, _ = ns.local_floor(p1)
    line_db_excess = db(r["draw"][bins] / f1[bins])
    print(f"\n(c) line bins above RX1 floor, k=-10 dB: mean {line_db_excess.mean():+.2f} dB "
          f"(raw RX1 was ~+10 dB)")
    assert abs(line_db_excess.mean()) < 0.5


@pytest.mark.xfail(strict=True, reason=(
    "The spec's P_out = F1 + max(P1 - F1 - k*E2, 0) rectifies the noise on every "
    "non-line bin, which raises the RX1 floor by about 0.5 dB at K=10. Measured, "
    "not fixed: the formula is the spec's, and the fix is a decision for the owner."))
def test_c_rx1_floor_off_the_lines_is_unchanged_by_processing():
    rng = np.random.default_rng(13)
    core = ns.NoiseSubCore(scale_db=-10.0, n=2.0)
    bins = rng.choice(N, size=60, replace=False)
    for _ in range(6):
        p1, p2, _ = _line_scene(rng, bins=bins)
        r = core.process(p1, p2)
    f1, _ = ns.local_floor(p1)
    off = np.ones(N, bool)
    off[bins] = False
    shift = db(np.mean(r["draw"][off])) - db(np.mean(f1[off]))
    print(f"\n    RX1 floor off the lines: shift from processing {shift:+.3f} dB")
    assert abs(shift) < 0.3


def test_c_wrong_k_leaves_the_line_standing():
    rng = np.random.default_rng(14)
    core = ns.NoiseSubCore(scale_db=-20.0, n=2.0)        # 10 dB too little
    bins = rng.choice(N, size=60, replace=False)
    for _ in range(6):
        p1, p2, _ = _line_scene(rng, bins=bins)
        r = core.process(p1, p2)
    f1, _ = ns.local_floor(p1)
    excess = db(r["draw"][bins] / f1[bins])
    print(f"\n(c') k=-20 dB (wrong): residual line {excess.mean():+.2f} dB over the RX1 floor")
    assert excess.mean() > 3.0


def test_d_output_never_below_rx1_floor():
    rng = np.random.default_rng(15)
    core = ns.NoiseSubCore(scale_db=40.0, n=2.0)          # strongest subtraction
    worst = np.inf
    for _ in range(8):
        p1, p2, _ = _line_scene(rng, line_db=30.0, ratio=1.0)   # new bins each frame is fine here
        r = core.process(p1, p2)
        f1, _ = ns.local_floor(p1)
        worst = min(worst, float(np.min(r["draw"] - f1)))
    print(f"\n(d) smallest P_out minus F1 over 8 frames at +40 dB: {worst:.3e} (must be >= 0)")
    assert worst >= -1e-12


def test_e_strong_line_only_in_rx1_is_untouched():
    rng = np.random.default_rng(16)
    core = ns.NoiseSubCore(scale_db=-10.0, n=2.0)
    bins = rng.choice(N, size=40, replace=False)
    for _ in range(6):
        p1 = gamma_noise(rng)
        p1[bins] += 100.0                                 # 20 dB line, RX1 only
        p2 = gamma_noise(rng)
        r = core.process(p1, p2)
    altered = np.mean(np.abs(r["draw"][bins] - p1[bins]) > 1e-9 * p1[bins])
    print(f"\n(e) RX1-only line bins altered: {altered * 100:.1f}% "
          f"(RX2 false lines: {r['lines']} bins in the frame)")
    assert altered < 0.01


def test_f_stale_rx2_and_mismatched_grid_fall_back():
    rng = np.random.default_rng(17)
    proc = ns.NoiseSubProcessor()
    a = {"data": 10 * np.log10(gamma_noise(rng, 4096)), "center_freq_hz": 14.074e6,
         "span_hz": 2e6, "sample_rate_hz": 2e6, "ts": 100.0}
    b_ok = dict(a, ts=100.0)
    now = 100.1
    assert proc.process_pair(a, b_ok, now)["status"] == "active"
    b_stale = dict(a, ts=99.5)
    r = proc.process_pair(a, b_stale, now)
    print(f"\n(f) RX2 500 ms old: status {r['status']}")
    assert r["status"] == "stale" and r["frame"] is None
    b_len = dict(a, data=b_ok["data"][:-1])
    assert proc.process_pair(a, b_len, now)["status"] == "grid"
    b_ctr = dict(a, center_freq_hz=14.080e6)
    assert proc.process_pair(a, b_ctr, now)["status"] == "grid"
    assert proc.process_pair(a, None, now)["status"] == "no_rx2"


def test_g_processing_time_per_frame_at_65536_bins():
    rng = np.random.default_rng(18)
    proc = ns.NoiseSubProcessor(scale_db=-10.0, n=2.0)
    a = {"data": 10 * np.log10(gamma_noise(rng)), "center_freq_hz": 14.074e6,
         "span_hz": 2e6, "sample_rate_hz": 2e6, "ts": 100.0}
    b = dict(a, data=10 * np.log10(gamma_noise(rng)))
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        proc.process_pair(a, b, 100.05)
        times.append((time.perf_counter() - t0) * 1e3)
    med, mx = float(np.median(times)), float(np.max(times))
    print(f"\n(g) 65536 bins, full process_pair: median {med:.1f} ms, max {mx:.1f} ms "
          f"(frame budget at 18 fps: 55.6 ms)")
    assert med < 55.6


# ----------------------------------------------------------------------
# Server wiring: mode transitions, the RX2 lock and the noise path
# ----------------------------------------------------------------------
import asyncio  # noqa: E402

import dashboard.server as server  # noqa: E402


def _rx2_fake(fake):
    fake.available = True
    fake.rf_freq_hz = 14_074_000.0
    fake.audio.set_target(14_074_000.0, "USB", 2800.0)
    fake.rf_gain_pct = 80.0
    fake.rf_freq_hz_b = 14_010_000.0
    fake.audio_b.set_target(14_010_000.0, "LSB", 2400.0)
    fake.rf_gain_pct_b = 40.0


def _mode(server_mod, mode):
    return asyncio.run(server._handle_cancel_command("set_cancel_mode", {"mode": mode}))


def test_mode_transitions_keep_the_rx2_lock_and_restore_on_off(fake_sdr, monkeypatch):
    _rx2_fake(fake_sdr)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)

    assert _mode(server, "noise") == (True, None)
    c = fake_sdr.canceller
    assert c.mode == "noise" and c.enabled is False and c.noise_enabled is True
    assert fake_sdr.rf_freq_hz_b == 14_074_000.0               # RX2 follows RX1
    assert server.rx2_locked(fake_sdr) is True

    assert _mode(server, "coherent") == (True, None)           # switch modes: lock stays
    assert c.mode == "coherent" and c.noise_enabled is False and c.enabled is True
    assert server.rx2_locked(fake_sdr) is True
    assert server._cancel_snapshot is not None

    assert _mode(server, "off") == (True, None)
    assert c.mode == "off" and server.rx2_locked(fake_sdr) is False
    assert fake_sdr.rf_freq_hz_b == 14_010_000.0               # RX2 restored
    assert fake_sdr.audio_b.target.mode == "LSB"
    assert server._cancel_snapshot is None


def test_noise_mode_never_turns_on_the_coherent_path(fake_sdr, monkeypatch):
    _rx2_fake(fake_sdr)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    _mode(server, "noise")
    assert fake_sdr.canceller._thread is None                  # no worker, no IQ path
    assert fake_sdr.canceller.enabled is False


def test_noise_path_refuses_without_rx2_and_publishes_when_fresh(fake_sdr, monkeypatch):
    _rx2_fake(fake_sdr)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    monkeypatch.setattr(server, "_latest_b_frame", None)
    published = []

    class Spy:
        async def broadcast_frame(self, frame):
            published.append(frame)
    monkeypatch.setattr(server, "spectrum_manager_proc", Spy())
    _mode(server, "noise")

    rng = np.random.default_rng(19)
    now = server.time.time()
    frame_a = {"data": (10 * np.log10(gamma_noise(rng, 8192))).astype(np.float32),
               "center_freq_hz": 14.074e6, "span_hz": 2e6, "sample_rate_hz": 2e6,
               "ts": now, "kind": "wide", "channel": "A"}
    asyncio.run(server.on_spectrum_frame_noise(frame_a))
    assert fake_sdr.canceller.noise_status == "no_rx2" and published == []

    server._latest_b_frame = dict(frame_a, ts=now, data=(10 * np.log10(gamma_noise(rng, 8192))).astype(np.float32))
    asyncio.run(server.on_spectrum_frame_noise(frame_a))
    assert fake_sdr.canceller.noise_status == "active"
    assert len(published) == 1 and published[0]["kind"] == "proc"

    server._latest_b_frame = dict(server._latest_b_frame, ts=now - 1.0)     # now 1 s old
    asyncio.run(server.on_spectrum_frame_noise(frame_a))
    assert fake_sdr.canceller.noise_status == "stale" and len(published) == 1
    _mode(server, "off")


def test_groups_are_contiguous_runs_over_the_whole_frame():
    """The count is contiguous runs of detected bins, not detected bins. The runs
    are taken over the whole frame; the browser keeps the ones in its span."""
    m = np.zeros(64, bool)
    m[[10, 11, 12, 20, 30, 31]] = True
    starts, ends = ns.runs(m)
    assert list(zip(starts.tolist(), ends.tolist())) == [(10, 13), (20, 21), (30, 32)]
    assert len(ns.runs(np.zeros(8, bool))[0]) == 0
    # A run that reaches the end of the frame still closes.
    s2, e2 = ns.runs(np.array([True, True, False, True]))
    assert list(zip(s2.tolist(), e2.tolist())) == [(0, 2), (3, 4)]


# ----------------------------------------------------------------------
# Wideband only, grid changes, and the error status
# ----------------------------------------------------------------------
def _wide(rng, n=65536, kind=None, ts=None, floor_db=-100.0):
    f = {"data": (10 * np.log10(rng.exponential(1.0, n)) + floor_db).astype(np.float32),
         "center_freq_hz": 14.074e6, "span_hz": 2e6, "sample_rate_hz": 2e6,
         "ts": server.time.time() if ts is None else ts}
    if kind:
        f["kind"] = kind
    return f


def _fine(rng):
    return {"data": (10 * np.log10(rng.exponential(1.0, 4096)) - 109.0).astype(np.float32),
            "center_freq_hz": 14.0755e6, "span_hz": 16000.0, "sample_rate_hz": 16000.0,
            "kind": "fine", "ts": server.time.time()}


class _Spy:
    def __init__(self):
        self.frames = []

    async def broadcast_frame(self, frame):
        self.frames.append(frame)


def _noise_setup(fake_sdr, monkeypatch):
    _rx2_fake(fake_sdr)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    monkeypatch.setattr(server, "_latest_b_frame", None)
    monkeypatch.setattr(server, "noise_proc", ns.NoiseSubProcessor())
    ghost, proc = _Spy(), _Spy()
    monkeypatch.setattr(server, "spectrum_manager_ghost", ghost)
    monkeypatch.setattr(server, "spectrum_manager_proc", proc)
    _mode(server, "noise")
    asyncio.run(server._handle_cancel_command("set_cancel_ghost", {"ghost": True}))
    return ghost, proc


def test_interleaved_wide_and_fine_frames_never_reach_the_processor_or_the_ghost(fake_sdr, monkeypatch):
    """A digital session: both receivers publish wide and fine frames on the same callbacks."""
    ghost, proc = _noise_setup(fake_sdr, monkeypatch)
    seen = []
    real = server.noise_proc.process_pair

    def spy(a, b, now):
        seen.append((len(a["data"]), None if b is None else len(b["data"])))
        return real(a, b, now)
    monkeypatch.setattr(server.noise_proc, "process_pair", spy)
    rng = np.random.default_rng(51)
    statuses = set()
    for k in range(12):
        for frame, is_b in ((_wide(rng), True), (_fine(rng), True), (_wide(rng), False), (_fine(rng), False)):
            if is_b:
                asyncio.run(server.on_spectrum_frame_b_store(frame))
            else:
                asyncio.run(server.on_spectrum_frame_noise(frame))
                statuses.add(fake_sdr.canceller.noise_status)
    print(f"\n    24 wide + 24 fine frames: processor saw {len(seen)} pairs {set(seen)}; "
          f"ghost got {len(ghost.frames)} frames of {set(len(f['data']) for f in ghost.frames)} bins; "
          f"statuses {statuses}")
    assert seen and set(seen) == {(65536, 65536)}
    assert len(ghost.frames) == 12 and all(len(f["data"]) == 65536 for f in ghost.frames)
    assert len(proc.frames) == 12 and statuses == {"active"}
    assert len(server._latest_b_frame["data"]) == 65536
    _mode(server, "off")


def test_a_frame_length_change_resets_history_without_an_exception():
    rng = np.random.default_rng(52)
    proc = ns.NoiseSubProcessor()
    now = 100.0
    big = {"data": 10 * np.log10(rng.exponential(1.0, 8192)), "center_freq_hz": 14.074e6,
           "span_hz": 2e6, "ts": now}
    small = dict(big, data=10 * np.log10(rng.exponential(1.0, 4096)))
    for _ in range(3):
        assert proc.process_pair(big, dict(big), now)["status"] == "active"
    proc.tracker.mask = np.ones(8192, bool)               # a held mask from the old grid
    proc.tracker.grid = (8192, 14.074e6, 2e6)
    for _ in range(4):
        r = proc.process_pair(small, dict(small), now)    # used to raise: histories of two lengths
        assert r["status"] == "active" and len(r["frame"]["data"]) == 4096
    assert all(len(h) == 4096 for h in proc.core._marker_hist)
    moved = dict(small, center_freq_hz=14.2e6)
    proc.core._line_hist.append(np.ones(4096, bool))
    proc.process_pair(moved, dict(moved), now)            # centre change: clean start
    assert proc.tracker.blocks == 0 and len(proc.core._marker_hist) == 1


def test_a_processing_exception_sets_the_error_status_and_recovers(fake_sdr, monkeypatch, caplog):
    ghost, proc = _noise_setup(fake_sdr, monkeypatch)
    rng = np.random.default_rng(53)
    asyncio.run(server.on_spectrum_frame_b_store(_wide(rng)))
    asyncio.run(server.on_spectrum_frame_noise(_wide(rng)))
    assert fake_sdr.canceller.noise_status == "active"
    real = server.noise_proc.process_pair

    def boom(a, b, now):
        raise ValueError("synthetic failure")
    monkeypatch.setattr(server.noise_proc, "process_pair", boom)
    monkeypatch.setattr(server, "_noise_error_logged_at", 0.0)
    caplog.set_level("ERROR")
    for _ in range(5):
        asyncio.run(server.on_spectrum_frame_noise(_wide(rng)))     # must not raise
    assert fake_sdr.canceller.noise_status == "error"
    assert fake_sdr.noise_mask.get() is None and server._noise_busy is False
    assert sum("NOISE SUB processing failed" in r.message for r in caplog.records) == 1
    monkeypatch.setattr(server.noise_proc, "process_pair", real)
    asyncio.run(server.on_spectrum_frame_b_store(_wide(rng)))
    asyncio.run(server.on_spectrum_frame_noise(_wide(rng)))
    assert fake_sdr.canceller.noise_status == "active"
    _mode(server, "off")


def test_false_share_in_a_digital_session_comes_from_wideband_blocks_only(fake_sdr, monkeypatch):
    """Flat RX2 noise, wide and fine frames interleaved, and the 11-frame blocks the SDR
    publishes. The estimate should sit near the 0.05% of flat noise, not at several percent."""
    ghost, proc = _noise_setup(fake_sdr, monkeypatch)
    rng = np.random.default_rng(54)
    for k in range(30):
        block = {"data": (10 * np.log10(rng.gamma(11, 1 / 11, 65536)) - 100.0).astype(np.float32),
                 "center_freq_hz": 14.074e6, "span_hz": 2e6, "kind": "block", "n": 11,
                 "ts": server.time.time()}
        asyncio.run(server.on_block_frame_b(block))
        asyncio.run(server.on_spectrum_frame_b_store(_wide(rng)))
        asyncio.run(server.on_spectrum_frame_b_store(_fine(rng)))
        asyncio.run(server.on_spectrum_frame_noise(_fine(rng)))
        asyncio.run(server.on_spectrum_frame_noise(_wide(rng)))
    fp = fake_sdr.canceller.noise_false_pct
    single = server.noise_proc.tracker.single_rate
    print(f"\n    digital session, flat RX2 noise, n=2: single-look rate {single * 100:.2f}%, "
          f"expected false share {fp:.3f}% (blocks seen {server.noise_proc.tracker.blocks})")
    assert fake_sdr.canceller.noise_status == "active"
    assert server.noise_proc.tracker.blocks == 30          # no resets from fine frames
    assert fp is not None and fp < 0.5
    _mode(server, "off")
