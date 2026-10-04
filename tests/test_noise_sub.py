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
