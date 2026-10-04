"""RX1 CANCEL (sdr/canceller.py): math, pairing, ramp, TX gate, RX2 lock.

No hardware. The canceller is driven with pump() directly, not its worker
thread, so every result is deterministic. Synthetic streams: x2 is a
complex Gaussian n, and x1 = w0*n (plus a small independent term where
needed), with the same firstSampleNum counters on both sides."""
import asyncio
import ctypes as C

import numpy as np
import pytest

import dashboard.server as server
from amplifier.acom_bridge import StationState
from sdr import sdrplay_capi as capi
from sdr.canceller import (
    BATCH_PAIRS, Canceller, WRAP, apply_rx2_follow, restore_rx2, snapshot_rx2)
from sdr.sdr_client import SdrClient

BLOCK = 252          # samples per native callback at 2 Msps


def _pair_blocks(rng, count, w0=0.0 + 0.0j, first0=0, sigma=2000.0, extra=0.0):
    """Return (A, B) block lists. A = w0*n + extra, B = n, same counters."""
    A, B = [], []
    f = first0
    for _ in range(count):
        n = rng.normal(0, sigma, BLOCK) + 1j * rng.normal(0, sigma, BLOCK)
        x1 = w0 * n + extra * (rng.normal(0, 1, BLOCK) + 1j * rng.normal(0, 1, BLOCK))
        A.append((f, _i16(x1.real), _i16(x1.imag)))
        B.append((f, _i16(n.real), _i16(n.imag)))
        f = (f + BLOCK) % WRAP
    return A, B


def _i16(x):
    return np.clip(np.rint(x), -32768, 32767).astype(np.int16)


def _feed_and_pump(c, A, B, chunk=256):
    """Feed in chunks, pumping after each, so the 512-block queues never
    overflow unless a test wants them to."""
    # The two streams are fed independently: they may have different lengths
    # after a deliberate gap, and pairing must come from the counters alone.
    for k in range(0, max(len(A), len(B)), chunk):
        for fa, ia, qa in A[k:k + chunk]:
            c.feed_a(ia, qa, fa)
        for fb, ib, qb in B[k:k + chunk]:
            c.feed_b(ib, qb, fb)
        c.pump()
    c.pump()   # idle pump: flushes a final partial batch, as the worker does when input stops


def _collector(c, out):
    c._deliver = lambda i, q: out.append((i.copy(), q.copy()))


def _y(out):
    if not out:
        return np.zeros(0, dtype=np.complex128)
    return np.concatenate([i.astype(np.float64) + 1j * q.astype(np.float64) for i, q in out])


def _spy_pairs(c):
    """Record every (A counter, B counter) pair handed to the DSP stage."""
    seen = []
    orig = c._process

    def spy(batch):
        seen.extend((fa, fb) for (fa, fb, *_rest) in batch)
        return orig(batch)
    c._process = spy
    return seen


def _enabled(**kw):
    c = Canceller(**kw)
    c.enabled = True
    return c


# ----------------------------------------------------------------------
# Math: cancellation, invert and phase conventions
# ----------------------------------------------------------------------

def test_cancel_at_true_weight_residual_better_than_60db():
    """The spec's synthetic case: x1 = s + w0*n, x2 = n, with the canceller
    at w0. The residual is y - s, measured against the signal s."""
    rng = np.random.default_rng(1)
    gain_db, phase = -10.5, 70.0                          # both on the 0.1 dB / 0.1 deg grid
    w0 = 10 ** (gain_db / 20) * np.exp(1j * np.radians(phase))
    k = np.arange(64 * BLOCK)
    s = 1000.0 * np.exp(1j * 2 * np.pi * 0.01234 * k)     # the wanted signal, 1000 LSB
    n = rng.normal(0, 2000, k.size) + 1j * rng.normal(0, 2000, k.size)
    x1 = s + w0 * n
    x2 = n
    A, B = [], []
    f = 0
    for b in range(64):
        sl = slice(b * BLOCK, (b + 1) * BLOCK)
        A.append((f, _i16(x1[sl].real), _i16(x1[sl].imag)))
        B.append((f, _i16(x2[sl].real), _i16(x2[sl].imag)))
        f = (f + BLOCK) % WRAP
    c = _enabled()
    c.set_gain_db(gain_db)
    c.set_phase_deg(phase)
    c._w_now = c.w_target                                 # steady state; the ramp has its own test
    out = []
    _collector(c, out)
    _feed_and_pump(c, A, B)

    y = _y(out)
    assert len(y) == len(s)
    ratio_db = 10 * np.log10(np.sum(np.abs(y - s) ** 2) / np.sum(np.abs(s) ** 2))
    assert ratio_db < -60.0


def test_phase_invert_plus_180_cancels_and_no_invert_does_not():
    """x1 = -w0*x2 needs +180 deg on the canceller's phase; without that the
    residual is about 2*w0*x2, i.e. +6 dB against x1 at unit gain."""
    rng = np.random.default_rng(2)
    w0 = np.exp(1j * np.radians(30.0))
    A, B = _pair_blocks(rng, 32, w0=-w0)                  # the sign is inside x1
    x1 = np.concatenate([a[1].astype(float) + 1j * a[2].astype(float) for a in A])

    inv = _enabled()
    inv.set_gain_db(0.0)
    inv.set_phase_deg(30.0 + 180.0)                       # the front end's +180 button
    inv._w_now = inv.w_target
    out_inv = []
    _collector(inv, out_inv)
    _feed_and_pump(inv, A, B)
    ratio_inv = 10 * np.log10(np.sum(np.abs(_y(out_inv)) ** 2) / np.sum(np.abs(x1) ** 2))
    assert ratio_inv < -60.0

    raw = _enabled()
    raw.set_gain_db(0.0)
    raw.set_phase_deg(30.0)                               # no invert: adds, does not cancel
    raw._w_now = raw.w_target
    out_raw = []
    _collector(raw, out_raw)
    _feed_and_pump(raw, A, B)
    ratio_raw = 10 * np.log10(np.sum(np.abs(_y(out_raw)) ** 2) / np.sum(np.abs(x1) ** 2))
    assert ratio_raw == pytest.approx(6.0, abs=0.5)


def test_gain_and_phase_are_clamped_and_quantized(tmp_path):
    c = Canceller()
    c.set_gain_db(100.0)
    assert c.gain_db == 80.0
    c.set_gain_db(-100.0)
    assert c.gain_db == -40.0
    c.set_gain_db(12.34)
    assert c.gain_db == 12.3
    c.set_phase_deg(-10.0)
    assert c.phase_deg == 350.0
    c.set_phase_deg(400.07)
    assert c.phase_deg == 40.1
    with pytest.raises(ValueError):
        c.set_gain_db(float("nan"))


def test_settings_persist_but_enabled_does_not(tmp_path):
    p = str(tmp_path / "cancel.json")
    c1 = Canceller(settings_path=p)
    c1.enabled = True                                     # must not be saved
    c1.set_gain_db(12.3)
    c1.set_phase_deg(40.1)
    c1.set_ghost(True)
    c1.set_follow_gain(False)
    c2 = Canceller(settings_path=p)
    assert (c2.gain_db, c2.phase_deg, c2.ghost, c2.follow_gain) == (12.3, 40.1, True, False)
    assert c2.enabled is False
    c1.reset_weight()
    assert Canceller(settings_path=p).gain_db == 0.0


# ----------------------------------------------------------------------
# Bypass: CANCEL off is the existing raw path
# ----------------------------------------------------------------------

def _ctypes_block(values):
    arr = (C.c_short * len(values))(*values)
    return arr


def test_cancel_off_raw_path_is_unchanged():
    s = SdrClient()
    assert s.canceller.enabled is False
    i_vals = list(range(-126, 126))
    q_vals = list(range(126, -126, -1))
    xi, xq = _ctypes_block(i_vals), _ctypes_block(q_vals)

    s._on_stream_data(xi, xq, None, BLOCK, 0, None)
    s._on_stream_data_b(xi, xq, None, BLOCK, 0, None)

    i_out, q_out = s._q.get_nowait()
    assert i_out.dtype == np.int16 and q_out.dtype == np.int16
    assert i_out.tolist() == i_vals and q_out.tolist() == q_vals
    assert s._q.empty()
    assert s._q_b.qsize() == 1                            # RX2 raw queue, as before
    assert len(s.canceller._qa.queue) == 0 and len(s.canceller._qb.queue) == 0
    assert s.canceller._thread is None                    # no worker when off


def test_cancel_on_callback_enqueues_with_first_sample_and_skips_raw_rx1():
    s = SdrClient()
    s.canceller.enabled = True
    params = C.pointer(capi.StreamCbParamsT(firstSampleNum=12345, numSamples=BLOCK))
    xi, xq = _ctypes_block([1] * BLOCK), _ctypes_block([2] * BLOCK)

    s._on_stream_data(xi, xq, params, BLOCK, 0, None)
    s._on_stream_data_b(xi, xq, params, BLOCK, 0, None)

    assert s._q.empty()                                   # raw RX1 replaced by the canceller
    fa, ia, qa = s.canceller._qa.get_nowait()
    fb, ib, qb = s.canceller._qb.get_nowait()
    assert fa == fb == 12345
    assert s._q_b.qsize() == 1                            # RX2 still raw


# ----------------------------------------------------------------------
# Pairing: equal counters only, resync on one-sided loss, bounded queues
# ----------------------------------------------------------------------

def _assert_pairs_equal(seen):
    assert seen, "no pairs reached the DSP stage"
    assert all(fa == fb for fa, fb in seen)


def test_simultaneous_gap_in_both_streams_pairs_without_resync():
    rng = np.random.default_rng(3)
    A, B = _pair_blocks(rng, 40)
    del A[10], B[10]                                      # the same block lost on both
    c = _enabled()
    seen = _spy_pairs(c)
    _collector(c, [])
    _feed_and_pump(c, A, B)
    _assert_pairs_equal(seen)
    assert c.resync_count == 0
    assert len(seen) == len(A)


def test_one_sided_gap_resyncs_and_never_subtracts_unpaired():
    rng = np.random.default_rng(4)
    A, B = _pair_blocks(rng, 60)
    del A[20]                                             # A loses one block; B does not
    c = _enabled()
    seen = _spy_pairs(c)
    _collector(c, [])
    _feed_and_pump(c, A, B)
    _assert_pairs_equal(seen)
    assert c.resync_count >= 1
    assert len(seen) == len(A)


def test_one_sided_gap_in_b_resyncs_and_never_subtracts_unpaired():
    rng = np.random.default_rng(5)
    A, B = _pair_blocks(rng, 60)
    del B[33]
    c = _enabled()
    seen = _spy_pairs(c)
    _collector(c, [])
    _feed_and_pump(c, A, B)
    _assert_pairs_equal(seen)
    assert c.resync_count >= 1


def test_queue_overflow_drops_oldest_and_resyncs_without_unpaired_subtraction():
    rng = np.random.default_rng(6)
    A, B = _pair_blocks(rng, 700)
    c = _enabled()
    seen = _spy_pairs(c)
    _collector(c, [])
    for fa, ia, qa in A[:600]:                            # A runs ahead with no B for now
        c.feed_a(ia, qa, fa)
    assert c.overflow_count > 0                           # oldest A blocks dropped
    c.pump()
    for fb, ib, qb in B[:600]:
        c.feed_b(ib, qb, fb)
    c.pump()
    _assert_pairs_equal(seen)
    assert c.overflow_count > 0


def test_resync_logging_is_rate_limited(caplog):
    c = Canceller()
    caplog.set_level("WARNING", logger="sdr.canceller")
    for _ in range(50):
        c._resync("test")
    assert c.resync_count == 50
    assert sum("CANCEL resync" in r.message for r in caplog.records) == 1


# ----------------------------------------------------------------------
# Ramp: a weight change never steps more than the step in w
# ----------------------------------------------------------------------

def test_weight_change_ramps_without_a_step_larger_than_the_weight_step():
    x2_level = 10000.0
    c = _enabled()
    c._w_now = 1.0 + 0.0j
    c.set_gain_db(6.0206)                                 # target ~ +2.0
    c.set_phase_deg(0.0)
    A = [(f, np.full(BLOCK, 0, np.int16), np.full(BLOCK, 0, np.int16))
         for f in (k * BLOCK for k in range(BATCH_PAIRS * 2))]
    B = [(f, np.full(BLOCK, int(x2_level), np.int16), np.zeros(BLOCK, np.int16))
         for f, _, _ in A]
    out = []
    _collector(c, out)
    _feed_and_pump(c, A, B)

    y = _y(out).real
    assert len(y) == 2 * BATCH_PAIRS * BLOCK
    # w_k = -y_k / x2 (x1 is zero), so its per-sample steps are visible
    dy = np.abs(np.diff(y))
    n = BATCH_PAIRS * BLOCK
    frac0 = 1.0 - np.exp(-n / (c.sample_rate_hz * 0.050))
    max_dw = abs(c.w_target - 1.0) * frac0 / n            # largest per-sample step in w
    assert dy.max() <= max_dw * x2_level + 1.5            # +1.5 LSB for int16 rounding
    assert y[-1] < y[0]                                   # the ramp is moving toward the target


def test_no_single_sample_jump_at_start_of_a_weight_change():
    """Without the ramp, the first sample after a gain change would jump by
    the whole weight change times x2. With it, the first sample is still
    at the old weight."""
    c = _enabled()
    c._w_now = 1.0 + 0.0j
    c.set_gain_db(20.0)
    A = [(f * BLOCK, np.zeros(BLOCK, np.int16), np.zeros(BLOCK, np.int16)) for f in range(BATCH_PAIRS)]
    B = [(f * BLOCK, np.full(BLOCK, 1000, np.int16), np.zeros(BLOCK, np.int16)) for f in range(BATCH_PAIRS)]
    out = []
    _collector(c, out)
    _feed_and_pump(c, A, B)
    y0 = _y(out).real[0]
    assert abs(y0 - (-1.0 * 1000)) < 2.0                  # still at w=1 for the first sample


# ----------------------------------------------------------------------
# TX gate
# ----------------------------------------------------------------------

def test_tx_gate_flushes_silences_and_first_tx_after_enable_is_not_silent():
    rng = np.random.default_rng(7)
    A, B = _pair_blocks(rng, 96)
    c = _enabled()
    out = []
    _collector(c, out)
    _feed_and_pump(c, A[:48], B[:48])
    before = len(out)
    assert before > 0

    c.gate_tx()                                           # rising edge
    assert c.tx_active is True
    assert c._qa.empty() and c._qb.empty() and not c._pending
    _feed_and_pump(c, A[48:72], B[48:72])
    assert len(out) == before                             # nothing delivered during TX

    c.clear_tx()                                          # falling edge
    assert c.tx_active is False
    _feed_and_pump(c, A[72:], B[72:])
    assert len(out) > before                              # the first TX after enable is not silent


# ----------------------------------------------------------------------
# Ghost trace and residual readout
# ----------------------------------------------------------------------

def test_residual_readout_matches_cancelled_over_raw_power_in_db():
    rng = np.random.default_rng(8)
    raw = rng.normal(0, 2000, 65536) + 1j * rng.normal(0, 2000, 65536)
    cancelled_power = np.full(65536, 1e-3, dtype=np.float32)
    published = []
    c = Canceller(
        cancelled_power=lambda: cancelled_power,
        publish_ghost=published.append,
        context=lambda: {"center_hz": 14.074e6, "freq_hz": 14.0755e6, "mode": "USB",
                         "bandwidth_hz": 2800.0, "low_cut_hz": 300.0})
    c.ghost = True
    c._gnext = 0.0
    c._ghost_update(raw.astype(np.complex64))

    g = c._gavg.astype(np.float64)
    expected = 10 * np.log10(np.sum(cancelled_power.astype(np.float64)) / np.sum(g))
    assert c.residual_span_db == pytest.approx(expected, abs=0.05)
    assert c.residual_passband_db is not None
    assert c.residual_span_db < -60.0
    assert len(published) == 1
    assert published[0]["kind"] == "ghost" and len(published[0]["data"]) == 65536


def test_ghost_is_not_published_when_off_but_residual_still_computed():
    published = []
    c = Canceller(cancelled_power=lambda: np.ones(65536, np.float32), publish_ghost=published.append)
    c.ghost = False
    c._gnext = 0.0
    c._ghost_update((np.ones(65536) + 0j).astype(np.complex64))
    assert published == []
    assert c.residual_span_db is not None


# ----------------------------------------------------------------------
# RX2 lock: follows RX1 while on, restores prior state when off
# ----------------------------------------------------------------------

def _rx2_setup(fake):
    fake.available = True
    fake.rf_freq_hz = 14_074_000.0
    fake.audio.set_target(14_074_000.0, "USB", 2800.0)
    fake.rf_gain_pct = 80.0
    fake.rf_notch_enabled = False
    fake.dab_notch_enabled = False
    fake.rf_freq_hz_b = 14_010_000.0
    fake.audio_b.set_target(14_010_000.0, "LSB", 2400.0)
    fake.rf_gain_pct_b = 40.0
    fake.rf_notch_enabled_b = True
    fake.dab_notch_enabled_b = False


def test_lock_follows_rx1_and_unlock_restores_rx2(fake_sdr):
    _rx2_setup(fake_sdr)
    snap = snapshot_rx2(fake_sdr)
    fake_sdr.canceller.enabled = True
    assert apply_rx2_follow(fake_sdr) is True

    assert fake_sdr.rf_freq_hz_b == 14_074_000.0
    assert (fake_sdr.audio_b.target.freq_hz, fake_sdr.audio_b.target.mode,
            fake_sdr.audio_b.target.bandwidth_hz) == (14_074_000.0, "USB", 2800.0)
    assert fake_sdr.rf_gain_pct_b == 80.0
    assert fake_sdr.rf_notch_enabled_b is False

    fake_sdr.canceller.enabled = False
    restore_rx2(fake_sdr, snap)
    assert fake_sdr.rf_freq_hz_b == 14_010_000.0
    assert (fake_sdr.audio_b.target.freq_hz, fake_sdr.audio_b.target.mode,
            fake_sdr.audio_b.target.bandwidth_hz) == (14_010_000.0, "LSB", 2400.0)
    assert fake_sdr.rf_gain_pct_b == 40.0
    assert fake_sdr.rf_notch_enabled_b is True


def test_gain_follow_toggle_off_leaves_rx2_gain_alone(fake_sdr):
    _rx2_setup(fake_sdr)
    fake_sdr.canceller.enabled = True
    fake_sdr.canceller.follow_gain = False
    apply_rx2_follow(fake_sdr)
    assert fake_sdr.rf_gain_pct_b == 40.0


def test_rx2_direct_changes_are_rejected_while_locked(fake_sdr, monkeypatch):
    _rx2_setup(fake_sdr)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    fake_sdr.canceller.enabled = True
    msg = server._rx2_lock_message("set_panadapter_freq", {"channel": "B", "freq_hz": 7.1e6})
    assert msg and "locked to RX1" in msg
    assert server._rx2_lock_message("set_panadapter_freq", {"freq_hz": 7.1e6}) is None  # RX1 is free
    assert server._rx2_lock_message("set_audio_target", {"channel": "B"}) is not None
    fake_sdr.canceller.enabled = False
    assert server._rx2_lock_message("set_panadapter_freq", {"channel": "B"}) is None


# ----------------------------------------------------------------------
# TX gate at the server level: rising and falling edges, all flags
# ----------------------------------------------------------------------

def _station(ptt: bool) -> StationState:
    return StationState(rig={"ptt": ptt})


def test_server_falling_edge_clears_all_three_tx_flags(fake_sdr, monkeypatch):
    fake_sdr.available = True
    monkeypatch.setattr(server, "sdr", fake_sdr)

    asyncio.run(server.on_station_state(_station(ptt=True)))
    assert fake_sdr.audio.tx_active and fake_sdr.audio_b.tx_active
    assert fake_sdr.combiner.tx_active and fake_sdr.canceller.tx_active

    asyncio.run(server.on_station_state(_station(ptt=False)))
    assert not fake_sdr.audio.tx_active
    assert not fake_sdr.audio_b.tx_active
    assert not fake_sdr.combiner.tx_active
    assert not fake_sdr.canceller.tx_active


# ----------------------------------------------------------------------
# The real worker thread and the real RX1 downstream entry point
# ----------------------------------------------------------------------

def test_worker_thread_delivers_then_stops_cleanly():
    import time
    rng = np.random.default_rng(9)
    A, B = _pair_blocks(rng, 64)
    out = []
    c = Canceller(deliver=lambda i, q: out.append((i.copy(), q.copy())))
    c.set_enabled(True)
    try:
        for fa, ia, qa in A:
            c.feed_a(ia, qa, fa)
        for fb, ib, qb in B:
            c.feed_b(ib, qb, fb)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(out) < 4:
            time.sleep(0.01)
        assert len(out) >= 4
    finally:
        c.set_enabled(False)
    assert c._thread is None
    assert c.enabled is False


def test_sdr_downstream_entry_point_feeds_rx1_queue_and_demod():
    s = SdrClient()
    i = np.arange(BLOCK, dtype=np.int16)
    q = -i
    s._deliver_cancelled(i, q)
    got_i, got_q = s._q.get_nowait()
    assert got_i.tolist() == i.tolist() and got_q.tolist() == q.tolist()


def test_enable_and_disable_commands_run_the_rx2_lock_round_trip(fake_sdr, monkeypatch):
    _rx2_setup(fake_sdr)
    monkeypatch.setattr(server, "sdr", fake_sdr)

    ok, err = asyncio.run(server._handle_cancel_command("set_cancel_enabled", {"enabled": True}))
    assert ok and err is None
    assert fake_sdr.canceller.enabled is True
    assert fake_sdr.rf_freq_hz_b == 14_074_000.0          # RX2 followed RX1 on the way in

    ok, _ = asyncio.run(server._handle_cancel_command("set_cancel_enabled", {"enabled": False}))
    assert ok
    assert fake_sdr.canceller.enabled is False
    assert fake_sdr.rf_freq_hz_b == 14_010_000.0          # and was restored on the way out
    assert server._cancel_snapshot is None


def test_bad_cancel_values_are_refused_with_a_reason(fake_sdr, monkeypatch):
    fake_sdr.available = True
    monkeypatch.setattr(server, "sdr", fake_sdr)
    ok, err = asyncio.run(server._handle_cancel_command("set_cancel_gain_db", {"gain_db": "abc"}))
    assert ok is False and "bad CANCEL command" in err
