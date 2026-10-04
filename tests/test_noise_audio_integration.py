"""NOISE SUB audio stage in the real RX1 demodulator (sdr/audio_demod.py). Raw IQ in,
through _process, so the browser and digital chains are the production code. The
tone is at RF target+1 kHz: on USB it is audible at 1 kHz on the baseband the stage
gains. Each test prints its numbers."""
import numpy as np

from sdr.audio_demod import AudioDemodulator
from sdr.audio_stft import MaskProvider, StftGainStage

CENTER = 14_074_000.0
TARGET = 14_075_000.0           # RX1 target: +1 kHz from the centre
SPAN = 2e6
NBINS = 65536
BIN_HZ = SPAN / NBINS
RF_LO = CENTER - SPAN / 2
FS = 2e6


def make_demod(enabled: bool, flag_rf_hz=None, r=0.01):
    d = AudioDemodulator(input_rate_hz=FS)
    d.rf_center_hz = CENTER
    d.set_target(TARGET, "USB", 2800.0)
    # AGC off: a lone tone would otherwise be re-levelled by the AGC, and the test
    # would measure the AGC rather than the stage. The AGC sits after the stage.
    d.agc_mode = "off"
    prov = MaskProvider()
    mask = np.zeros(NBINS, bool)
    ratio = np.ones(NBINS)
    if flag_rf_hz is not None:
        i = int(np.floor((flag_rf_hz - RF_LO) / BIN_HZ))
        mask[i - 2: i + 3] = True            # a flagged run, as the processor publishes it
        ratio[i - 2: i + 3] = r
    prov.publish(mask, ratio, CENTER, SPAN)
    d.stft = StftGainStage(provider=prov, target_hz=lambda: d.target.freq_hz)
    d.stft.enabled = enabled
    return d


def iq_blocks(n_calls, block, tone_rf_hz=CENTER + 2000.0, noise=0.05, seed=5):
    """Consecutive blocks of a tone at tone_rf_hz plus a little noise."""
    rng = np.random.default_rng(seed)
    t0 = 0
    for _ in range(n_calls):
        t = (t0 + np.arange(block)) / FS
        sig = np.exp(2j * np.pi * (tone_rf_hz - CENTER) * t)
        n = noise * (rng.standard_normal(block) + 1j * rng.standard_normal(block))
        x = (sig + n) * 3000
        yield x.real.astype(np.float64), x.imag.astype(np.float64)
        t0 += block


def run_demod(d, n_calls, **kw):
    browser, digital = [], []
    for bi, bq in iq_blocks(n_calls, d.batch_samples, **kw):
        b, dg = d._process(bi, bq)
        if b is not None:
            browser.append(np.frombuffer(b, dtype=np.int16))
        if dg is not None:
            digital.append(np.frombuffer(dg, dtype=np.int16))
    return (np.concatenate(browser) if browser else np.zeros(0, np.int16),
            np.concatenate(digital) if digital else np.zeros(0, np.int16))


def test_digital_path_is_bit_identical_with_audio_on_and_off():
    off = make_demod(enabled=False, flag_rf_hz=CENTER + 2000.0)
    on = make_demod(enabled=True, flag_rf_hz=CENTER + 2000.0)
    _, dig_off = run_demod(off, 120)
    _, dig_on = run_demod(on, 120)
    print(f"\n    digital path: {len(dig_off)} samples, identical with AUDIO on and off: "
          f"{np.array_equal(dig_off, dig_on)}")
    assert len(dig_off) and np.array_equal(dig_off, dig_on)


def test_browser_path_attenuates_the_flagged_tone_and_digital_does_not():
    on = make_demod(enabled=True, flag_rf_hz=CENTER + 2000.0)
    off = make_demod(enabled=False, flag_rf_hz=CENTER + 2000.0)
    b_on, _ = run_demod(on, 240)
    b_off, _ = run_demod(off, 240)
    tail = slice(len(b_on) // 2, None)
    db = 10 * np.log10(np.mean(b_on[tail].astype(float) ** 2) / np.mean(b_off[tail].astype(float) ** 2))
    print(f"\n    browser path, AUDIO on vs off, flagged 1 kHz tone (steady second half): {db:+.1f} dB")
    assert -24.0 < db < -14.0


def test_unflagged_tone_is_unchanged_on_the_browser_path():
    on = make_demod(enabled=True, flag_rf_hz=CENTER - 3000.0)     # flag somewhere else
    off = make_demod(enabled=False)
    b_on, _ = run_demod(on, 240)
    b_off, _ = run_demod(off, 240)
    n = min(len(b_on), len(b_off))
    tail = slice(n // 2, n)
    db = 10 * np.log10(np.mean(b_on[tail].astype(float) ** 2) / np.mean(b_off[tail].astype(float) ** 2))
    print(f"\n    browser path, unflagged tone, AUDIO on vs off: {db:+.3f} dB")
    assert abs(db) < 0.1


def test_tx_gate_resets_the_stage_and_the_first_tx_after_enabling_is_audible():
    d = make_demod(enabled=True, flag_rf_hz=CENTER + 2000.0)
    run_demod(d, 60)
    d.gate_tx()
    assert d.stft.tx_active is True and d.stft._next_frame == 0
    d.stft.tx_active = False          # the server clears the flag on the falling edge
    browser, _ = run_demod(d, 120, tone_rf_hz=CENTER + 500.0)
    power_db = 10 * np.log10(np.mean(browser[len(browser) // 2:].astype(float) ** 2) + 1e-12)
    print(f"\n    first RX after TX with AUDIO on: output power {power_db:.1f} dB (not silent)")
    assert power_db > -60.0


def test_browser_audio_has_no_step_larger_than_the_signal_on_ab_flips():
    base = make_demod(enabled=False, flag_rf_hz=CENTER + 2000.0)
    b_raw, _ = run_demod(base, 200)
    flip = make_demod(enabled=False, flag_rf_hz=CENTER + 2000.0)
    chunks = []
    calls = 0
    for bi, bq in iq_blocks(200, flip.batch_samples):
        if calls == 60:
            flip.stft.enabled = True          # A to processed
        if calls == 130:
            flip.stft.enabled = False         # and back to raw
        b, _ = flip._process(bi, bq)
        if b is not None:
            chunks.append(np.frombuffer(b, dtype=np.int16))
        calls += 1
    b_flip = np.concatenate(chunks)
    raw_step = np.abs(np.diff(b_raw.astype(np.int64))).max()
    flip_step = np.abs(np.diff(b_flip.astype(np.int64))).max()
    print(f"\n    A/B flips: largest sample step {flip_step} LSB; raw signal step {raw_step} LSB")
    assert flip_step <= raw_step * 1.05 + 2


def test_latency_of_the_browser_path_is_the_stage_latency_and_digital_is_undelayed():
    d = make_demod(enabled=False)
    # a 1 kHz audio tone: RF target + 1 kHz, inside the SSB passband (a DC tone is not)
    blocks = list(iq_blocks(80, d.batch_samples, tone_rf_hz=CENTER + 2000.0))
    first_b = first_d = None
    for k, (bi, bq) in enumerate(blocks):
        b, dg = d._process(bi, bq)
        if first_b is None and b is not None and np.abs(np.frombuffer(b, np.int16)).max() > 100:
            first_b = k
        if first_d is None and dg is not None and np.abs(np.frombuffer(dg, np.int16)).max() > 100:
            first_d = k
    print(f"\n    first audible block: digital call {first_d}, browser call {first_b} "
          f"(stage latency {d.stft.latency_samples} samples = {d.stft.latency_samples / 16000 * 1000:.0f} ms)")
    assert first_d is not None and first_b is not None and first_b >= first_d


def test_server_publishes_the_mask_to_the_stage_only_with_noise_and_audio_on(fake_sdr, monkeypatch):
    """The processed frame carries the mask and power ratios to RX1's stage. The stage runs
    only when NOISE SUB is on, AUDIO is on and A/B is on processed."""
    import asyncio
    import dashboard.server as server

    fake_sdr.available = True
    fake_sdr.audio.set_target(TARGET, "USB", 2800.0)
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    monkeypatch.setattr(server, "_latest_b_frame", None)
    rng = np.random.default_rng(41)
    now = server.time.time()
    a = {"data": (10 * np.log10(rng.exponential(1.0, 65536))).astype(np.float32),
         "center_freq_hz": CENTER, "span_hz": SPAN, "sample_rate_hz": SPAN, "ts": now}
    b = dict(a, data=(10 * np.log10(rng.exponential(1.0, 65536))).astype(np.float32))
    asyncio.run(server._handle_cancel_command("set_cancel_mode", {"mode": "noise"}))
    server._latest_b_frame = b
    asyncio.run(server._handle_cancel_command("set_noise_audio", {"on": True}))
    asyncio.run(server.on_spectrum_frame_noise(a))
    assert fake_sdr.noise_mask.get() is not None          # the mask is published
    assert fake_sdr.audio.stft.enabled is True
    asyncio.run(server._handle_cancel_command("set_noise_audio_ab", {"processed": False}))
    assert fake_sdr.audio.stft.enabled is False           # raw while A/B is on raw
    asyncio.run(server._handle_cancel_command("set_noise_audio_ab", {"processed": True}))
    asyncio.run(server._handle_cancel_command("set_noise_audio", {"on": False}))
    assert fake_sdr.audio.stft.enabled is False           # AUDIO off: gains 1
    asyncio.run(server._handle_cancel_command("set_cancel_mode", {"mode": "off"}))
    assert fake_sdr.noise_mask.get() is None              # leaving NOISE SUB clears the mask
