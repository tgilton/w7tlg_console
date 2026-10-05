"""NOISE SUB step 1: line detection on non-overlapping block averages of 11 raw
frames (sdr/noise_sub.py BlockMaskTracker). The displayed trace stays on the EMA;
the detector never sees it, because EMA outputs are correlated. Each test prints
the measured numbers."""
import numpy as np
import pytest

from sdr import noise_sub as ns

K_RAW = 1          # one raw FFT frame: exponential per bin
B = ns.BLOCK_FRAMES
N_EMA = 10
D = (N_EMA - 1) / (N_EMA + 1)


def _raw_blocks(rng, n_blocks, n_bins, line_bins=None, line_db=0.0, drift=None):
    """Yields block-average dicts. Each block averages B raw exponential frames.
    A line (if given) adds power on its bins; drift(t) gives its bin at raw frame t."""
    t = 0
    for _ in range(n_blocks):
        acc = np.zeros(n_bins)
        for _ in range(B):
            x = rng.exponential(1.0, n_bins)
            if drift is not None:
                b = int(round(drift(t)))
                x[b] += 10 ** (line_db / 10)
            elif line_bins is not None:
                x[line_bins] += 10 ** (line_db / 10)
            acc += x
            t += 1
        yield {"data": 10 * np.log10(acc / B), "center_freq_hz": 14.074e6,
               "span_hz": 2e6, "ts": 0.0}


def test_a_false_detection_fraction_and_runs_per_2800_bins():
    """Independent blocks of 11 raw frames, sigma = F/sqrt(11): the false rate of
    the 3-of-5 detector, and how many runs fall in 2800 bins."""
    rng = np.random.default_rng(21)
    n_bins, n_blocks, warm = 16384, 400, 10
    print()
    rows = []
    for n in (2.0, 3.0, 4.0):
        tr = ns.BlockMaskTracker(n)
        det_frac, mask_frac, runs = [], [], []
        for i, blk in enumerate(_raw_blocks(rng, n_blocks, n_bins)):
            tr.update(blk)
            if i >= warm:
                det = ns.NoiseSubCore._persistent(tr._hist)
                det_frac.append(det.mean())
                mask_frac.append(tr.mask.mean())
                st, _ = ns.runs(det[:2800])
                runs.append(len(st))
        d, m, r = np.mean(det_frac), np.mean(mask_frac), np.mean(runs)
        rows.append((n, d, m, r))
        print(f"    n={n:.0f}: 3-of-5 detection {d*100:.3f}% of bins, held mask {m*100:.3f}%, "
              f"runs per 2800 bins {r:.2f}")
    d2 = rows[0][1]
    assert 0.0005 < d2 < 0.004           # the expected 0.13-0.2% at n=2
    assert rows[0][3] < 15               # about 6 runs per 2800 bins


def test_b_floor_estimator_on_the_ema_stream_within_0p3_db():
    rng = np.random.default_rng(22)
    p = np.empty((200, 16384))
    x = rng.exponential(1.0, (200, 16384))
    p[0] = x[0]
    for k in range(1, 200):
        p[k] = D * p[k - 1] + (1 - D) * x[k]
    errs = []
    for k in range(20, 200):
        f, _ = ns.local_floor(p[k])
        errs.append(10 * np.log10(np.mean(f)))
    err = float(np.mean(errs))
    print(f"\n    EMA stream: floor estimate {err:+.3f} dB against the true 0 dB")
    assert abs(err) < 0.3


def test_c_run_lengths_with_hann_bin_correlation():
    """Real FFT bins of a Hann window are correlated with their neighbours. Count
    run lengths of detections at n=2 on block averages of Hann-windowed frames."""
    rng = np.random.default_rng(23)
    n_bins = 16384
    win = np.hanning(2 * n_bins)
    tr = ns.BlockMaskTracker(2.0)
    lengths = []
    for i in range(160):
        acc = np.zeros(n_bins)
        for _ in range(B):
            seg = rng.standard_normal(2 * n_bins) + 1j * rng.standard_normal(2 * n_bins)
            spec = np.fft.fftshift(np.fft.fft(seg * win))[n_bins // 2: n_bins // 2 + n_bins]
            acc += np.abs(spec) ** 2
        tr.update({"data": 10 * np.log10(acc / B), "center_freq_hz": 14.074e6, "span_hz": 2e6})
        if i >= 10:
            det = ns.NoiseSubCore._persistent(tr._hist)
            st, en = ns.runs(det)
            lengths += (en - st).tolist()
    lengths = np.array(lengths)
    print(f"\n    Hann-correlated bins: {len(lengths)} runs, mean length {lengths.mean():.2f}, "
          f"runs longer than 1 bin: {np.mean(lengths > 1) * 100:.1f}%")
    assert 1.0 <= lengths.mean() < 2.0


def test_d_steady_line_confirmed_within_2p5_s():
    rng = np.random.default_rng(24)
    line_bin, n_bins = 5000, 16384
    tr = ns.BlockMaskTracker(2.0)
    confirmed_at = None
    for i, blk in enumerate(_raw_blocks(rng, 8, n_bins, line_bins=[line_bin], line_db=20.0)):
        tr.update(blk)
        if tr.mask[line_bin] and confirmed_at is None:
            confirmed_at = i + 1
    t = confirmed_at * B / 18.0
    print(f"\n    steady +20 dB line confirmed after {confirmed_at} blocks = {t:.2f} s")
    assert confirmed_at is not None and t <= 2.5


def test_e_drifting_carrier_20_hz_per_s_is_still_removed():
    """20 Hz/s on the real 65536-bin grid (30.5 Hz per bin). Per block the line moves
    about 0.4 bin. Coverage = share of blocks, after confirmation, with the mask on
    at the line's bin."""
    rng = np.random.default_rng(25)
    n_bins = 65536
    bin_hz = 2e6 / n_bins
    b0 = 20000.0
    drift = lambda t: b0 + (20.0 * (t / 18.0)) / bin_hz
    tr = ns.BlockMaskTracker(2.0)
    covered, total = [], 0
    t_end = 0
    for i, blk in enumerate(_raw_blocks(rng, 40, n_bins, line_db=20.0, drift=drift)):
        tr.update(blk)
        t_end = (i + 1) * B
        if i >= 4:
            centre = int(round(drift(t_end - 1 - B // 2)))
            covered.append(bool(tr.mask[centre - 1: centre + 2].any()))
            total += 1
    cov = np.mean(covered)
    span = t_end / 18.0
    print(f"\n    drift 20 Hz/s over {span:.1f} s: mask on at the line in {cov * 100:.0f}% of blocks "
          f"after confirmation (line moved {20 * span / bin_hz:.1f} bins)")
    assert cov > 0.8


def test_f_output_never_below_rx1_floor_with_a_block_mask():
    rng = np.random.default_rng(26)
    core = ns.NoiseSubCore(scale_db=40.0, n=2.0)
    mask = rng.random(16384) < 0.05
    worst = np.inf
    for _ in range(4):
        p1 = rng.exponential(1.0, 16384)
        p2 = rng.exponential(1.0, 16384)
        p2[mask] += 100.0
        r = core.process(p1, p2, mask)
        f1, _ = ns.local_floor(p1)
        worst = min(worst, float(np.min(r["draw"] - f1)))
    assert worst >= -1e-12


def test_tracker_resets_on_centre_or_span_change():
    tr = ns.BlockMaskTracker(2.0)
    rng = np.random.default_rng(27)
    blk = next(_raw_blocks(rng, 1, 4096, line_bins=[100], line_db=20.0))
    for _ in range(4):
        tr.update(blk)
    assert tr.mask[100]
    moved = dict(blk, center_freq_hz=blk["center_freq_hz"] + 1e5)
    tr.update(moved)
    assert tr.blocks == 1 and not tr.mask.any()


def test_false_alarm_estimate_is_the_3_of_5_of_the_measured_single_look():
    from math import comb
    proc = ns.NoiseSubProcessor(n=2.0)
    proc.tracker.single_rate = 0.002
    p = 0.002
    expect = 100 * sum(comb(5, k) * p ** k * (1 - p) ** (5 - k) for k in (3, 4, 5))
    assert abs(proc.false_alarm_pct() - expect) < 1e-12
    proc.tracker.reset()
    assert proc.false_alarm_pct() is None
