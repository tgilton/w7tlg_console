"""Audio stage (sdr/audio_stft.py): mapping per sideband, unity where unflagged,
floor unchanged, reconstruction, constant latency, A/B switching, TX reset, and
the FT8-like case. Tones are complex baseband, as the stage sees them. The RF mask
is on the 65536-bin, 2 MHz grid (30.52 Hz per bin) the NOISE SUB processor uses."""
import numpy as np
import pytest

from sdr.audio_stft import MaskProvider, StftGainStage

FS = 16000.0
TARGET = 14_074_000.0
CENTER = TARGET
SPAN = 2e6
NBINS = 65536
BIN_HZ = SPAN / NBINS
RF_LO = CENTER - SPAN / 2


def rf_index(f_bb):
    return int(np.floor((TARGET + f_bb - RF_LO) / BIN_HZ))


def make_stage(flag_bb_freqs=(), r=0.01, beta_db=-20.0):
    prov = MaskProvider()
    mask = np.zeros(NBINS, bool)
    ratio = np.ones(NBINS)
    for f in flag_bb_freqs:
        mask[rf_index(f)] = True
        ratio[rf_index(f)] = r
    prov.publish(mask, ratio, CENTER, SPAN)
    st = StftGainStage(provider=prov, target_hz=lambda: TARGET, beta_db=beta_db)
    st.enabled = True
    return st


def run(st, x, chunk=131):
    return np.concatenate([st.process(x[i:i + chunk]) for i in range(0, len(x), chunk)])


def tone(f, n, amp=1.0):
    t = np.arange(n) / FS
    return amp * np.exp(2j * np.pi * f * t)


def steady_gain(y, x, skip=8192):
    """Amplitude gain over the steady part of the output (emitted sample k = input k)."""
    return np.abs(y[skip:]).mean() / np.abs(x[skip:len(y)]).mean()


# ------------------------------------------------------------- reconstruction
def test_reconstruction_with_unity_gain_is_near_identical_in_steady_state():
    rng = np.random.default_rng(31)
    x = rng.standard_normal(40000) + 1j * rng.standard_normal(40000)
    st = StftGainStage()
    y = run(st, x)
    err = np.max(np.abs(y[2048:] - x[2048:len(y)])) / np.max(np.abs(x))
    print(f"\n    G=1 reconstruction, steady state: max error {err:.2e} of full scale")
    assert err < 1e-9


def test_startup_transient_is_the_only_error_after_reset():
    x = np.ones(4096, dtype=complex)
    st = StftGainStage()
    y = run(st, x)
    early = np.abs(y[:200] - x[:200]).max()
    print(f"\n    start-up transient, first 200 samples: max error {early:.3f} (fill of the overlap)")
    assert early > 0.01 and np.abs(y[2048:] - x[2048:len(y)]).max() < 1e-9


def test_latency_is_exactly_n_samples():
    st = StftGainStage()
    impulse_at = 5000
    x = np.zeros(12000, dtype=complex)
    x[impulse_at] = 1.0
    seen_at = None
    n_in = 0
    for i in range(0, len(x), 131):
        chunk = x[i:i + 131]
        out = st.process(chunk)
        n_in += len(chunk)
        if seen_at is None and len(out) and np.abs(out).max() > 1e-6:
            # the output sample index equals the input sample index, emitted when input arrives
            seen_at = n_in
            break
    print(f"\n    impulse at input {impulse_at} emitted when input reached {seen_at}: "
          f"latency {seen_at - impulse_at} samples = {(seen_at - impulse_at) / FS * 1000:.1f} ms")
    assert seen_at is not None and seen_at - impulse_at <= st.n + st.hop


# ------------------------------------------------------- mapping per sideband
@pytest.mark.parametrize("sideband,f_flag,f_tone", [
    ("USB", +1000.0, +1000.0),     # RF target+1 kHz, audible on USB at 1 kHz
    ("LSB", -1000.0, -1000.0),     # RF target-1 kHz, audible on LSB at 1 kHz
])
def test_flagged_tone_attenuated_at_its_audio_frequency_in_each_sideband(sideband, f_flag, f_tone):
    st = make_stage(flag_bb_freqs=[f_flag], r=0.01)
    x = tone(f_tone, 32000)
    y = run(st, x)
    g = steady_gain(y, x)
    print(f"\n    {sideband}: tone at {f_tone:+.0f} Hz, line flagged at the same RF bin: "
          f"gain {20 * np.log10(g):.2f} dB (target -20 dB)")
    assert -21.0 < 20 * np.log10(g) < -19.0


@pytest.mark.parametrize("sideband,f_flag,f_tone", [
    ("USB", +1000.0, -1000.0),     # the mirror image is outside the USB passband
    ("LSB", -1000.0, +1000.0),
])
def test_mirror_image_is_not_attenuated(sideband, f_flag, f_tone):
    st = make_stage(flag_bb_freqs=[f_flag], r=0.01)
    x = tone(f_tone, 32000)
    y = run(st, x)
    db = 20 * np.log10(steady_gain(y, x))
    print(f"\n    {sideband}: mirror tone at {f_tone:+.0f} Hz, unflagged: gain {db:+.3f} dB")
    assert abs(db) < 0.1


def test_unflagged_tone_passes_with_unity_gain_within_0p1_db():
    st = make_stage(flag_bb_freqs=[+1000.0], r=0.01)
    for f in (+3000.0, -2500.0, +200.0):
        x = tone(f, 32000)
        y = run(st, x)
        db = 20 * np.log10(steady_gain(y, x))
        print(f"\n    unflagged tone {f:+.0f} Hz: gain {db:+.4f} dB")
        assert abs(db) < 0.1
        st.reset()


def test_empty_mask_is_unity_everywhere():
    st = make_stage(flag_bb_freqs=[])
    x = tone(1234.0, 32000)
    y = run(st, x)
    assert abs(20 * np.log10(steady_gain(y, x))) < 0.01


# ------------------------------------------------------------- sub-threshold floor
def test_sub_threshold_floor_rms_unchanged_with_empty_mask_and_with_lines_elsewhere():
    rng = np.random.default_rng(32)
    x = rng.standard_normal(60000) + 1j * rng.standard_normal(60000)
    # Floor measure: the power in STFT bins that are not flagged, after the delay.
    def floor_power(y, flagged_bb):
        spec = np.abs(np.fft.fft(y[8192:8192 + 1024] * np.hanning(1024))) ** 2
        return spec, flagged_bb

    st0 = make_stage(flag_bb_freqs=[])
    y0 = run(st0, x)
    st1 = make_stage(flag_bb_freqs=[+3000.0, -4500.0, +6000.0], r=0.01)
    y1 = run(st1, x)
    seg = slice(16384, 16384 + 8192)
    p0 = np.mean(np.abs(y0[seg]) ** 2)
    p1 = np.mean(np.abs(y1[seg]) ** 2)
    db_total = 10 * np.log10(p1 / p0)
    # floor region: the same region, excluding STFT bins near the three lines
    def floor_mask_power(y):
        out = []
        for s in range(16384, 16384 + 8192 - 1024, 512):
            X = np.abs(np.fft.fft(y[s:s + 1024] * np.hanning(1024))) ** 2
            out.append(X)
        X = np.mean(out, axis=0)
        bins = np.fft.fftfreq(1024, 1 / FS)
        keep = np.ones(1024, bool)
        for f in (3000.0, -4500.0, 6000.0):
            keep &= np.abs(bins - f) > 200.0
        return X[keep].mean()
    floor_db = 10 * np.log10(floor_mask_power(y1) / floor_mask_power(y0))
    print(f"\n    empty mask: RMS identical; with 3 lines: total RMS {db_total:+.3f} dB, "
          f"floor away from the lines {floor_db:+.3f} dB")
    assert abs(floor_db) < 0.1


# -------------------------------------------------------------- FT8-like burst
def test_ft8_like_8fsk_burst_passes_away_from_a_line_and_loses_tones_that_overlap():
    tones = 1500.0 + 6.25 * np.arange(8)               # 50 Hz wide, 6.25 Hz spacing
    n = 40000
    t = np.arange(n) / FS
    rng = np.random.default_rng(33)
    sym = rng.integers(0, 8, n // 1600 + 1)
    x = np.zeros(n, complex)
    for k, s in enumerate(sym):
        seg = slice(k * 1600, min(n, (k + 1) * 1600))
        x[seg] = np.exp(2j * np.pi * tones[s] * t[seg])

    st_away = make_stage(flag_bb_freqs=[+3500.0], r=0.01)
    y_away = run(st_away, x)
    err_away = np.abs(np.abs(y_away[12000:]) - np.abs(x[12000:len(y_away)])).max()

    st_over = make_stage(flag_bb_freqs=[+1600.0], r=0.01)
    y_over = run(st_over, x)
    per_tone = []
    for f in tones:
        g = tone(f, 32000)
        yg = run(make_stage(flag_bb_freqs=[+1600.0], r=0.01), g)
        per_tone.append((f, 20 * np.log10(steady_gain(yg, g))))
    lost = [f for f, db in per_tone if db < -3.0]
    print(f"\n    FT8-like burst, line away: max envelope error {err_away:.4f} (unity)")
    print("    line at +1600 Hz (flagged bin +/- 2 bins), per-tone gain: " +
          ", ".join(f"{f:.0f} Hz {db:+.1f} dB" for f, db in per_tone))
    print(f"    tones lost (below -3 dB): {len(lost)} of 8: {lost}")
    assert err_away < 0.05
    assert 1 <= len(lost) <= 8


# ------------------------------------------------------------------ A/B switch
def test_ab_switch_has_no_discontinuity_larger_than_the_signal_step():
    st = make_stage(flag_bb_freqs=[+1000.0], r=0.01)
    st.enabled = False                              # start raw
    x = tone(1000.0, 60000)
    chunks = []
    for i in range(0, len(x), 131):
        if i == 20000:
            st.enabled = True                       # flip to processed
        if i == 40000:
            st.enabled = False                      # and back to raw
        chunks.append(st.process(x[i:i + 131]))
    y = np.concatenate(chunks)
    dy = np.abs(np.diff(y))
    dx = np.abs(np.diff(x))
    limit = dx.max() * 1.02 + 1e-6
    worst = dy.max()
    print(f"\n    A/B flips: largest sample-to-sample step {worst:.4f} vs signal step {dx.max():.4f}")
    assert worst <= limit


# ------------------------------------------------------------------- TX reset
def test_reset_gives_a_fresh_stream_and_the_first_tx_after_enabling_is_not_silent():
    st = make_stage(flag_bb_freqs=[+1000.0], r=0.01)
    run(st, tone(1000.0, 16000))
    st.tx_active = True
    st.reset()
    st.tx_active = False
    x = tone(2000.0, 32000)
    y = run(st, x)
    energy = np.mean(np.abs(y[8192:]) ** 2)
    print(f"\n    after reset, output power after the start-up: {10 * np.log10(energy):.2f} dB")
    assert energy > 0.5


def test_mapping_survives_a_retune_of_the_target():
    st = make_stage(flag_bb_freqs=[+1000.0], r=0.01)
    st.target_hz = lambda: TARGET + 500.0           # target moved 500 Hz: the line is now at +500
    x = tone(1000.0, 32000)
    y = run(st, x)
    assert steady_gain(y, x) > 0.9                  # the flag is no longer on this tone


# ------------------------------------------------------------- TX guard
def _stage_snapshot(st):
    return (st._n_in, st._next_frame, st._emitted, st._inbase, st._accbase,
            st._inbuf.copy(), st._acc.copy(), st._g.copy(), st._map_key)


def test_tx_flag_is_a_guard_no_output_and_no_state_change():
    st = make_stage(flag_bb_freqs=[+1000.0], r=0.01)
    run(st, tone(1000.0, 8000))                       # a running stage, gains mid-way
    before = _stage_snapshot(st)
    st.tx_active = True
    outs = [st.process(tone(2000.0, 131)) for _ in range(40)]
    after = _stage_snapshot(st)
    assert all(len(o) == 0 for o in outs)
    for b, a in zip(before, after):
        assert np.array_equal(b, a) if isinstance(b, np.ndarray) else b == a
    print(f"\n    TX flag set: 40 blocks in, {sum(len(o) for o in outs)} samples out, state unchanged "
          f"(frames {before[1]} -> {after[1]})")


def test_stage_resumes_after_the_flag_clears():
    st = make_stage(flag_bb_freqs=[])
    run(st, tone(1000.0, 8000))
    st.tx_active = True
    st.process(tone(1000.0, 4000))
    st.prime(np.zeros(0))                             # the audio thread's restart on the falling edge,
    assert len(st.process(tone(1500.0, 131))) == 0    # which may come before the server's clear
    st.tx_active = False
    x = tone(1500.0, 16000)
    y = run(st, x)
    assert len(y) == len(x)                           # primed: one sample out per sample in
    g = np.abs(y[4096:]).mean() / np.abs(x[4096:len(y)]).mean()
    print(f"\n    after the clear: {len(y)} samples out for {len(x)} in, gain {20 * np.log10(g):+.3f} dB")
    assert abs(20 * np.log10(g)) < 0.01
