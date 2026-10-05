"""STEP 0 of the audio-domain NOISE SUB work: entering NOISE SUB must not change
anything RX1 hears or controls. Snapshots RX1's audio chain, gains, notches,
frequency and target before and after entering NOISE SUB, processing frames and
applying the RX2 follow. Uses the fake SDR, which holds the real
AudioDemodulators, so the audio state is the production state."""
import asyncio

import numpy as np

import dashboard.server as server
from sdr.canceller import apply_rx2_follow


def _rx1_state(sdr):
    a = sdr.audio
    return {
        "rf_freq_hz": sdr.rf_freq_hz,
        "rf_gain_pct": sdr.rf_gain_pct,
        "rf_notch": sdr.rf_notch_enabled,
        "dab_notch": sdr.dab_notch_enabled,
        "target": (a.target.freq_hz, a.target.mode, a.target.bandwidth_hz),
        "agc_mode": a.agc_mode, "agc_gain": a.agc_gain, "manual_gain": a.manual_gain,
        "tx_active": a.tx_active, "enabled": a.enabled, "nr": a.nr_enabled,
        "eq": (a.eq_enabled, a.eq_bass_db, a.eq_mid_db, a.eq_treble_db),
        "phase_offset": a.phase_offset_deg, "low_cut": a.low_cut_hz,
        "queue": a._q.qsize(), "combiner_enabled": sdr.combiner.enabled,
        "canceller_coherent": sdr.canceller.enabled,
    }


def test_noise_sub_leaves_rx1_audio_and_controls_untouched(fake_sdr, monkeypatch):
    fake_sdr.available = True
    fake_sdr.rf_freq_hz = 14_074_000.0
    fake_sdr.audio.set_target(14_074_000.0, "USB", 2800.0)
    fake_sdr.audio.enabled = True
    fake_sdr.audio.manual_gain = 1.0
    fake_sdr.rf_gain_pct = 80.0
    fake_sdr.rf_freq_hz_b = 14_010_000.0
    fake_sdr.audio_b.set_target(14_010_000.0, "LSB", 2400.0)
    fake_sdr.rf_gain_pct_b = 40.0
    monkeypatch.setattr(server, "sdr", fake_sdr)
    monkeypatch.setattr(server, "_cancel_snapshot", None)
    monkeypatch.setattr(server, "_latest_b_frame", None)

    before = _rx1_state(fake_sdr)
    asyncio.run(server._handle_cancel_command("set_cancel_mode", {"mode": "noise"}))

    rng = np.random.default_rng(1)
    now = server.time.time()
    frame_a = {"data": (10 * np.log10(rng.exponential(1.0, 4096))).astype(np.float32),
               "center_freq_hz": 14.074e6, "span_hz": 2e6, "sample_rate_hz": 2e6,
               "ts": now, "kind": "wide", "channel": "A"}
    server._latest_b_frame = dict(frame_a, ts=now)
    for _ in range(3):
        asyncio.run(server.on_spectrum_frame_noise(frame_a))

    after = _rx1_state(fake_sdr)
    # RX1 is the same, except for the mode flags that NOISE SUB is meant to set.
    assert after["combiner_enabled"] == before["combiner_enabled"]
    assert after["canceller_coherent"] is False
    for key in ("rf_freq_hz", "rf_gain_pct", "rf_notch", "dab_notch", "target", "agc_mode",
                "agc_gain", "manual_gain", "tx_active", "enabled", "nr", "eq", "phase_offset",
                "low_cut", "queue"):
        assert after[key] == before[key], f"RX1 {key} changed: {before[key]} -> {after[key]}"


def test_rx2_follow_writes_only_rx2_fields(fake_sdr):
    fake_sdr.available = True
    fake_sdr.rf_freq_hz = 14_074_000.0
    fake_sdr.audio.set_target(14_074_000.0, "USB", 2800.0)
    fake_sdr.rf_gain_pct = 80.0
    fake_sdr.rf_freq_hz_b = 14_010_000.0
    fake_sdr.audio_b.set_target(14_010_000.0, "LSB", 2400.0)
    fake_sdr.rf_gain_pct_b = 40.0
    fake_sdr.canceller.noise_enabled = True
    before = _rx1_state(fake_sdr)
    apply_rx2_follow(fake_sdr)
    after = _rx1_state(fake_sdr)
    for key in before:
        if key in ("queue",):
            continue
        assert after[key] == before[key], f"follow changed RX1 {key}"
    assert fake_sdr.rf_freq_hz_b == 14_074_000.0 and fake_sdr.rf_gain_pct_b == 80.0
