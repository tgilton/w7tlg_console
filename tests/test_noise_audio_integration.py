"""NOISE SUB audio stage in the real demodulators (sdr/audio_demod.py), driven with raw
IQ through _process, so every chain is the production code.

The stage is in RX1's path only while NOISE SUB mode is on (stage_request). Outside the
mode the output must be bit-identical to main: the tests load main's audio_demod.py from
git and compare bytes. The test vector is synthetic and deterministic (FT8-like tones
plus noise); no recorded baseband exists in the repo. Each test prints its numbers."""
import asyncio
import importlib.util
import subprocess
from pathlib import Path

import numpy as np
import pytest

import sdr.audio_demod as branch_mod
from sdr.audio_demod import AudioDemodulator
from sdr.audio_stft import MaskProvider, PcmDelay, StftGainStage

REPO = Path(__file__).resolve().parent.parent
CENTER = 14_074_000.0
TARGET = 14_075_000.0           # RX1 target: +1 kHz from the centre
SPAN = 2e6
NBINS = 65536
BIN_HZ = SPAN / NBINS
RF_LO = CENTER - SPAN / 2
FS = 2e6
N = 1024                         # stage latency, samples at 16 kHz


@pytest.fixture(scope="module")
def main_mod(tmp_path_factory):
    """main's sdr/audio_demod.py, loaded from git as its own module."""
    try:
        src = subprocess.run(["git", "show", "main:sdr/audio_demod.py"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
    except Exception:
        pytest.skip("main:sdr/audio_demod.py is not available from git")
    path = tmp_path_factory.mktemp("main_demod") / "audio_demod_main.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("audio_demod_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def configure(d, session="voice", agc=None, eq_db=0.0):
    d.rf_center_hz = CENTER
    d.set_target(TARGET, "USB", 2800.0)
    if session == "digital":
        d.enter_digital_mode()               # what an FT8 / JS8 / DATA session applies
    if agc is not None:
        d.agc_mode = agc
    d.eq_bass_db = eq_db
    return d


def attach_stage(d, flag_rf_hz=None, r=0.01, audio_on=False, mode_on=False):
    prov = MaskProvider()
    mask = np.zeros(NBINS, bool)
    ratio = np.ones(NBINS)
    if flag_rf_hz is not None:
        i = int(np.floor((flag_rf_hz - RF_LO) / BIN_HZ))
        mask[i - 2: i + 3] = True
        ratio[i - 2: i + 3] = r
    prov.publish(mask, ratio, CENTER, SPAN)
    d.stft = StftGainStage(provider=prov, target_hz=lambda: d.target.freq_hz)
    d.stft.enabled = audio_on
    d.stage_request = mode_on
    return d


def vector(n_calls, block, tones=(2000.0, 2300.0, 2706.25, 3400.0), noise=0.05, seed=5, amp=3000):
    """Deterministic IQ blocks: tones at CENTER + offset (audio = offset - 1 kHz) plus noise."""
    rng = np.random.default_rng(seed)
    t0 = 0
    for _ in range(n_calls):
        t = (t0 + np.arange(block)) / FS
        sig = sum(np.exp(2j * np.pi * f * t) for f in tones) / len(tones)
        nz = noise * (rng.standard_normal(block) + 1j * rng.standard_normal(block))
        x = (sig + nz) * amp
        yield x.real.astype(np.float64), x.imag.astype(np.float64)
        t0 += block


def run(d, n_calls, events=None, **kw):
    """Returns (browser, digital) int16 arrays. events: {call index: fn(d)}."""
    events = events or {}
    browser, digital = [], []
    for k, (bi, bq) in enumerate(vector(n_calls, d.batch_samples, **kw)):
        if k in events:
            events[k](d)
        out = d._process(bi, bq)
        b, dg = out if isinstance(out, tuple) else (out, out)
        if b is not None:
            browser.append(np.frombuffer(b, dtype=np.int16))
        if dg is not None:
            digital.append(np.frombuffer(dg, dtype=np.int16))
    cat = lambda xs: np.concatenate(xs) if xs else np.zeros(0, np.int16)
    return cat(browser), cat(digital)


def maxdiff(a, b):
    n = min(len(a), len(b))
    return int(np.abs(a[:n].astype(np.int64) - b[:n].astype(np.int64)).max()), n


def on(d):
    d.stage_request = True


def off(d):
    d.stage_request = False


# ------------------------------------------------- 1. bypass: bit-identical to main
@pytest.mark.parametrize("session,eq_db", [("voice", 0.0), ("voice", 6.0), ("digital", 0.0)])
def test_noise_sub_off_browser_and_digital_are_bit_identical_to_main(main_mod, session, eq_db):
    m = configure(main_mod.AudioDemodulator(input_rate_hz=FS), session, eq_db=eq_db)
    b = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), session, eq_db=eq_db),
                     flag_rf_hz=CENTER + 2000.0, audio_on=True, mode_on=False)
    m_out, _ = run(m, 300)
    b_browser, b_digital = run(b, 300)
    d1, n1 = maxdiff(m_out, b_browser)
    d2, n2 = maxdiff(m_out, b_digital)
    print(f"\n    NOISE SUB off, {session} session, EQ {eq_db:+.0f} dB: browser max diff {d1} over {n1} "
          f"samples, digital max diff {d2}; lengths {len(m_out)} / {len(b_browser)} / {len(b_digital)}")
    assert len(m_out) == len(b_browser) == len(b_digital) > 30000
    assert d1 == 0 and d2 == 0


# ------------------------------------------- 3. digital path regression against main
def test_digital_feed_in_a_digital_session_is_bit_identical_to_main_in_every_case(main_mod):
    m = configure(main_mod.AudioDemodulator(input_rate_hz=FS), "digital")
    ref, _ = run(m, 300)
    cases = {
        "NOISE SUB off": dict(mode_on=False, audio_on=False, events=None),
        "NOISE SUB on from the start": dict(mode_on=True, audio_on=False, events=None),
        "NOISE SUB + AUDIO on, line flagged": dict(mode_on=True, audio_on=True, events=None),
        "enter at call 80, exit at call 200, AUDIO on": dict(mode_on=False, audio_on=True,
                                                            events={80: on, 200: off}),
    }
    print()
    for name, c in cases.items():
        d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), "digital"),
                         flag_rf_hz=CENTER + 2000.0, audio_on=c["audio_on"], mode_on=c["mode_on"])
        _, dig = run(d, 300, events=c["events"])
        diff, n = maxdiff(ref, dig)
        print(f"    digital feed vs main, {name}: max sample difference {diff} over {n} samples")
        assert len(dig) == len(ref) and diff == 0


def test_voice_session_digital_feed_loses_eq_only_while_noise_sub_is_on(main_mod):
    m = configure(main_mod.AudioDemodulator(input_rate_hz=FS), "voice", eq_db=6.0)
    ref, _ = run(m, 300)
    d_off = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), "voice", eq_db=6.0))
    d_on = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), "voice", eq_db=6.0), mode_on=True)
    _, dig_off = run(d_off, 300)
    _, dig_on = run(d_on, 300)
    diff_off, _ = maxdiff(ref, dig_off)
    diff_on, _ = maxdiff(ref, dig_on)
    print(f"\n    voice session, bass EQ +6 dB, digital feed vs main: NOISE SUB off {diff_off}, "
          f"NOISE SUB on {diff_on} (the raw chain has no EQ or NR)")
    assert diff_off == 0 and diff_on > 0


# ------------------------------------------------------ 1. latency and crossfades
def test_in_the_mode_the_browser_audio_is_the_raw_audio_delayed_by_exactly_n():
    raw = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"))
    mode = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"), mode_on=True)
    b_raw, _ = run(raw, 300)
    b_mode, d_mode = run(mode, 300)
    assert len(b_mode) == len(b_raw)
    skip = 4 * N
    diff = np.abs(b_mode[skip + N:].astype(int) - b_raw[skip:len(b_raw) - N].astype(int)).max()
    dd, _ = maxdiff(d_mode, b_raw)
    print(f"\n    AUDIO off in the mode: browser = raw delayed by {N} samples (64 ms), max diff {diff} LSB; "
          f"digital undelayed, max diff {dd}")
    assert diff <= 1 and dd == 0


def test_toggling_audio_inside_the_mode_does_not_shift_timing():
    """An unflagged tone: AUDIO on and off give the same samples at the same positions."""
    a = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                     flag_rf_hz=CENTER - 3000.0, mode_on=True, audio_on=False)
    b = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                     flag_rf_hz=CENTER - 3000.0, mode_on=True, audio_on=False)
    flip = lambda d: setattr(d.stft, "enabled", not d.stft.enabled)
    ba, _ = run(a, 300)
    bb, _ = run(b, 300, events={60: flip, 140: flip, 220: flip})
    diff, n = maxdiff(ba[8 * N:], bb[8 * N:])
    print(f"\n    AUDIO flipped three times vs never: {len(ba)} and {len(bb)} samples, max diff {diff} LSB")
    assert len(ba) == len(bb) and diff <= 2


def test_mode_entry_and_exit_crossfade_without_a_click():
    """A step is a sample-to-sample change larger than the raw signal ever makes."""
    raw = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"))
    b_raw, _ = run(raw, 300)
    d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                     flag_rf_hz=CENTER - 3000.0, audio_on=True)
    b, _ = run(d, 300, events={80: on, 200: off})
    raw_step = np.abs(np.diff(b_raw.astype(np.int64))).max()
    step = np.abs(np.diff(b.astype(np.int64))).max()
    before, _ = maxdiff(b[:80 * 131], b_raw[:80 * 131])
    tail = slice(200 * 131 + 2 * N, None)
    after = np.abs(b[tail].astype(int) - b_raw[tail].astype(int)).max()
    print(f"\n    entry at call 80, exit at call 200: largest step {step} LSB (raw signal {raw_step} LSB); "
          f"{len(b)} samples out for {len(b_raw)} raw; before entry diff {before}, after exit diff {after}")
    assert len(b) == len(b_raw)                   # no samples dropped or added: no gap
    assert step <= raw_step * 1.05 + 2
    assert before == 0 and after <= 1             # raw before entry, and raw again after the exit fade
    assert d._stage_state == "bypass"


def test_exit_with_the_agc_running_hands_the_gain_over_without_a_jump():
    """A removed line lets the AGC rise, so the two chains' gains differ at exit. The gain
    at each block boundary must move no faster across the exit than the AGC itself moves."""
    kw = dict(tones=(2000.0, 2706.25), noise=0.05)
    d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="fast"),
                     flag_rf_hz=CENTER + 2000.0, audio_on=True)
    gains = []
    def probe(dd):
        gains.append((dd.agc_gain, dd._dig_agc_gain))

    def probe_and_exit(dd):
        probe(dd)
        off(dd)
    events = {k: probe for k in range(295, 330)}
    events.update({80: on, 300: probe_and_exit})
    b, _ = run(d, 400, events=events, **kw)
    g = np.array([x[0] for x in gains])
    before = gains[4]
    jump = np.abs(np.diff(g)).max() / g.max()
    print(f"\n    AGC gain before exit: browser {before[0]:.2f}, raw {before[1]:.2f}; largest block-to-block "
          f"change across the exit {jump * 100:.1f}% (one jump would be {abs(before[0] - before[1]) / before[0] * 100:.0f}%)")
    assert before[0] > before[1] * 1.2            # the case is real: the gains do differ
    assert jump < 0.12
    assert d._stage_state == "bypass"
    assert abs(g[-1] - gains[-1][1]) / g[-1] < 0.02   # and the plain chain ends on the raw gain


def test_flagged_tone_is_attenuated_in_the_mode_with_audio_on_only():
    kw = dict(tones=(2000.0,), noise=0.01)
    base = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                        flag_rf_hz=CENTER + 2000.0, mode_on=True, audio_on=False)
    proc = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                        flag_rf_hz=CENTER + 2000.0, mode_on=True, audio_on=True)
    b0, _ = run(base, 300, **kw)
    b1, dig = run(proc, 300, **kw)
    half = len(b0) // 2
    p = lambda v: np.mean(v[half:].astype(float) ** 2)
    print(f"\n    flagged 1 kHz tone: AUDIO on vs off {10 * np.log10(p(b1) / p(b0)):+.1f} dB on the browser "
          f"path; digital feed {10 * np.log10(p(dig) / p(b0)):+.2f} dB")
    assert -21.0 < 10 * np.log10(p(b1) / p(b0)) < -18.5
    assert abs(10 * np.log10(p(dig) / p(b0))) < 0.1


# ------------------------------------------------------------------ RX2 delay
def test_pcm_delay_is_exact_constant_and_passes_bytes_through_when_off():
    pd = PcmDelay()
    rng = np.random.default_rng(3)
    x = rng.integers(-20000, 20000, 20000).astype(np.int16)
    blk = x[:131].tobytes()
    assert pd.process(blk) is blk                       # off: the same object, untouched
    pd2 = PcmDelay()
    pd2.request = True
    out = np.concatenate([np.frombuffer(pd2.process(x[i:i + 131].tobytes()), np.int16)
                          for i in range(0, len(x), 131)])
    assert len(out) == len(x)
    assert np.array_equal(out[4 * N:], x[3 * N:len(x) - N])
    print(f"\n    RX2 PCM delay: output = input delayed by exactly {N} samples, bit-exact, one "
          f"{N}-sample copy per block")


def test_pcm_delay_entry_and_exit_crossfade_and_return_to_passthrough():
    t = np.arange(40000)
    x = (8000 * np.sin(2 * np.pi * 440 * t / 16000)).astype(np.int16)
    pd = PcmDelay()
    out = []
    for k, i in enumerate(range(0, len(x), 131)):
        if k == 60:
            pd.request = True
        if k == 180:
            pd.request = False
        out.append(np.frombuffer(pd.process(x[i:i + 131].tobytes()), np.int16))
    out = np.concatenate(out)
    step, raw_step = np.abs(np.diff(out.astype(int))).max(), np.abs(np.diff(x.astype(int))).max()
    print(f"\n    RX2 delay in and out: largest step {step} LSB (signal {raw_step} LSB), state {pd.state}")
    assert len(out) == len(x) and step <= raw_step + 2
    assert pd.state == "bypass" and np.array_equal(out[-3000:], x[-3000:])


def test_rx1_and_rx2_browser_audio_stay_aligned_in_the_mode():
    rx1 = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"), mode_on=True)
    rx2 = configure(AudioDemodulator(input_rate_hz=FS), agc="off")
    rx2.pcm_delay = PcmDelay()
    rx2.pcm_delay.request = True
    b1, _ = run(rx1, 300)
    b2, d2 = run(rx2, 300)
    diff = np.abs(b1[4 * N:].astype(int) - b2[4 * N:].astype(int)).max()
    print(f"\n    same IQ into RX1 (stage, AUDIO off) and RX2 (PCM delay): max difference {diff} LSB; "
          f"RX2 digital feed undelayed")
    assert len(b1) == len(b2) and diff <= 1
    rx2_plain = configure(AudioDemodulator(input_rate_hz=FS), agc="off")
    p, _ = run(rx2_plain, 300)
    assert np.array_equal(d2, p)


def test_rx2_without_the_mode_is_bit_identical_to_main(main_mod):
    m = configure(main_mod.AudioDemodulator(input_rate_hz=FS))
    r = configure(AudioDemodulator(input_rate_hz=FS))
    r.pcm_delay = PcmDelay()
    ref, _ = run(m, 200)
    b, dg = run(r, 200)
    assert np.array_equal(ref, b) and np.array_equal(ref, dg)


# ------------------------------------------------------------------------- TX
def test_tx_sets_the_flag_only_and_the_falling_edge_restarts_the_stage_audibly():
    d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                     flag_rf_hz=CENTER - 3000.0, mode_on=True, audio_on=True)
    run(d, 60)
    frames_before = d.stft._next_frame
    d.gate_tx()
    assert d.stft.tx_active is True and d.stft._next_frame == frames_before   # buffers untouched
    d._reset_after_tx()                      # the audio thread, on the falling edge
    d.stft.tx_active = False                 # the server, at both falling-edge sites
    assert d._stage_state == "in"
    b, dig = run(d, 120)
    power = 10 * np.log10(np.mean(b[len(b) // 2:].astype(float) ** 2))
    print(f"\n    first RX after TX in the mode: {len(b)} browser samples for {len(dig)} digital, "
          f"power {power:.1f} dB (not silent)")
    assert len(b) == len(dig) and power > 20.0


def test_stage_flag_guards_the_demodulator_no_audio_and_no_state_advance():
    """IQ fed straight into _process with the stage's flag set (the demodulator's own
    tx_active is left clear, so only the stage's guard is under test)."""
    d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="fast"),
                     flag_rf_hz=CENTER - 3000.0, mode_on=True, audio_on=True)
    run(d, 60)
    st = d.stft
    snap = lambda: (st._n_in, st._next_frame, st._emitted, st._g.copy(), d._bb_hist.copy(),
                    d._ssb_overlap.copy(), d._dig_ssb_overlap.copy(), d.agc_gain, d._dig_agc_gain,
                    d._stage_state, d._stage_fade_pos)
    before = snap()
    st.tx_active = True
    b, dig = run(d, 40)
    after = snap()
    print(f"\n    stage flag set, 40 IQ blocks: browser {len(b)} samples, digital {len(dig)} samples, "
          f"stage frames {before[1]} -> {after[1]}")
    assert len(b) == 0 and len(dig) == 0
    for x, y in zip(before, after):
        assert np.array_equal(x, y) if isinstance(x, np.ndarray) else x == y


def test_first_rx_after_tx_is_audible_once_the_flag_is_cleared():
    d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS), agc="off"),
                     flag_rf_hz=CENTER - 3000.0, mode_on=True, audio_on=True)
    ref, _ = run(d, 60)
    d.gate_tx()                                   # rising edge: sets both flags
    assert d.tx_active and d.stft.tx_active
    silent, _ = run(d, 10)                        # still flagged: nothing comes out
    d.tx_active = False                           # the server's falling edge ...
    d.stft.tx_active = False                      # ... clears both, at both sites
    d._reset_after_tx()                           # and the audio thread restarts the stage
    b, dig = run(d, 120)
    half = len(b) // 2
    lvl = lambda v: 10 * np.log10(np.mean(v.astype(float) ** 2))
    print(f"\n    during TX {len(silent)} samples; after the clear {len(b)} browser / {len(dig)} digital, "
          f"level {lvl(b[half:]):.1f} dB vs {lvl(ref[len(ref) // 2:]):.1f} dB before TX")
    assert len(silent) == 0
    assert len(b) == len(dig) == 120 * 131
    assert abs(lvl(b[half:]) - lvl(ref[len(ref) // 2:])) < 0.5


def test_tx_during_a_fade_finishes_the_fade():
    d = attach_stage(configure(AudioDemodulator(input_rate_hz=FS)), mode_on=False)
    run(d, 20)
    d.stage_request = True
    run(d, 2)                                # mid-entry
    assert d._stage_state == "entering"
    d._reset_after_tx()
    assert d._stage_state == "in"
    d.stage_request = False
    d._reset_after_tx()
    assert d._stage_state == "bypass"
    pd = PcmDelay()
    pd.request = True
    pd.process(np.zeros(131, np.int16).tobytes())
    pd.reset()
    assert pd.state == "in"


# --------------------------------------------------------------------- server
def test_server_mode_entry_and_exit_switch_the_stage_and_the_rx2_delay(fake_sdr, monkeypatch):
    import dashboard.server as server

    fake_sdr.available = True
    fake_sdr.audio.set_target(TARGET, "USB", 2800.0)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    monkeypatch.setattr(server, "_latest_b_frame", None)
    cmd = lambda c, m: asyncio.run(server._handle_cancel_command(c, m))
    a, b = fake_sdr.audio, fake_sdr.audio_b

    assert a.stage_request is False and b.pcm_delay.request is False
    cmd("set_noise_audio", {"on": True})                  # AUDIO alone does nothing outside the mode
    assert a.stage_request is False and a.stft.enabled is False

    cmd("set_cancel_mode", {"mode": "noise"})
    assert a.stage_request is True and b.pcm_delay.request is True and a.stft.enabled is True
    cmd("set_noise_audio", {"on": False})                 # unity gain, stage still in the path
    assert a.stage_request is True and a.stft.enabled is False
    cmd("set_noise_beta", {"beta_db": -33.3})
    assert a.stft.beta_db == -33.3 and fake_sdr.canceller.state()["noise_beta_db"] == -33.3
    cmd("set_noise_beta", {"beta_db": -99})
    assert fake_sdr.canceller.noise_beta_db == -40.0

    cmd("set_cancel_mode", {"mode": "coherent"})          # COHERENT does not use the stage
    assert a.stage_request is False and b.pcm_delay.request is False and a.stft.enabled is False
    cmd("set_cancel_mode", {"mode": "noise"})
    assert a.stage_request is True
    cmd("set_cancel_mode", {"mode": "off"})
    assert a.stage_request is False and b.pcm_delay.request is False
    assert "noise_audio_ab" not in fake_sdr.canceller.state()


def test_server_publishes_the_mask_to_the_stage_and_clears_it_on_exit(fake_sdr, monkeypatch):
    import dashboard.server as server

    fake_sdr.available = True
    fake_sdr.audio.set_target(TARGET, "USB", 2800.0)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    rng = np.random.default_rng(41)
    now = server.time.time()
    a = {"data": (10 * np.log10(rng.exponential(1.0, 65536))).astype(np.float32),
         "center_freq_hz": CENTER, "span_hz": SPAN, "sample_rate_hz": SPAN, "ts": now}
    monkeypatch.setattr(server, "_latest_b_frame",
                        dict(a, data=(10 * np.log10(rng.exponential(1.0, 65536))).astype(np.float32)))
    asyncio.run(server._handle_cancel_command("set_cancel_mode", {"mode": "noise"}))
    asyncio.run(server._handle_cancel_command("set_noise_audio", {"on": True}))
    asyncio.run(server.on_spectrum_frame_noise(a))
    assert fake_sdr.noise_mask.get() is not None and fake_sdr.audio.stft.enabled is True
    asyncio.run(server._handle_cancel_command("set_cancel_mode", {"mode": "off"}))
    assert fake_sdr.noise_mask.get() is None and fake_sdr.audio.stft.enabled is False
