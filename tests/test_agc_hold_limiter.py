"""AGC hold and output peak limiter on the browser chain (sdr/audio_demod.py).

The fault: with DNR on, the pauses in speech are near-silent, the RMS AGC ran
its gain toward 6x during each pause, and the next syllable hit the hard clip
at 0.95 — clicks, mostly on RX1. Two changes, both in _browser_chain only:
the AGC gain no longer rises on a block well under the recent speech level,
and the clip is a peak limiter.

"Today's behaviour" in these tests is the chain with both switched off on the
instance (legacy()): no hold, and np.clip for the limiter — the code they
replaced.
"""
import logging

import numpy as np
import pytest

from sdr import audio_demod
from sdr.audio_demod import (AudioDemodulator, INTERMEDIATE_RATE_HZ, LIMIT_PEAK)

BLOCK = 128                                   # ~8 ms at 16 kHz, the no-NR cadence
PEAK_I16 = int(LIMIT_PEAK * 32767)            # what 0.95 becomes in the PCM


def make(agc="fast", manual_gain=1.0):
    d = AudioDemodulator()
    d.set_target(14_200_000.0, "USB", 2800.0)
    d.agc_mode = agc
    d.manual_gain = manual_gain
    d._ssb_filter = d._design_ssb_filter(2800.0, "USB", d.low_cut_hz)
    d._ssb_overlap = np.zeros(len(d._ssb_filter) - 1, dtype=np.complex64)
    return d


def legacy(d):
    """The chain as it was: AGC never held, hard clip at the output."""
    d._agc_hold = lambda *a: False
    d._limit = lambda audio: np.clip(audio, -0.95, 0.95)
    return d


class Source:
    """A 1 kHz tone plus noise at the 16 kHz intermediate rate, in int16-scale units
    (the chain divides by 32768). amp is the tone's audio amplitude, 0..1."""

    def __init__(self, seed=3):
        self.rng = np.random.default_rng(seed)
        self.n = 0

    def blocks(self, seconds, amp, noise=0.0, block=BLOCK):
        for _ in range(int(seconds * INTERMEDIATE_RATE_HZ / block)):
            t = (self.n + np.arange(block)) / INTERMEDIATE_RATE_HZ
            self.n += block
            x = amp * np.exp(2j * np.pi * 1000.0 * t)
            if noise:
                x = x + noise * (self.rng.standard_normal(block)
                                 + 1j * self.rng.standard_normal(block))
            yield (x * 32768.0).astype(np.complex64)


def run(d, blocks):
    return [np.frombuffer(d._browser_chain(b), dtype=np.int16) for b in blocks]


# ── below the threshold: nothing changes ────────────────────────────────

@pytest.mark.parametrize("agc", ["fast", "slow", "off"])
def test_output_is_bit_identical_below_the_threshold(agc):
    """A steady signal that never reaches 0.95: byte for byte the old chain's output."""
    new, old = make(agc), legacy(make(agc))
    a = run(new, Source().blocks(3.0, amp=0.05, noise=0.005))
    b = run(old, Source().blocks(3.0, amp=0.05, noise=0.005))
    assert all(np.array_equal(x, y) for x, y in zip(a, b))
    assert max(int(np.abs(x).max()) for x in a) < PEAK_I16
    assert new.limited_count == 0 and new.agc_gain == old.agc_gain


def test_idle_limiter_returns_its_input_untouched():
    d = make()
    audio = (np.linspace(-0.95, 0.95, 400)).astype(np.float32)
    assert d._limit(audio) is audio
    assert d.limited_count == 0 and d._lim_gain == 1.0


def test_digital_mode_keeps_the_old_agc_and_clip():
    """In digital mode the browser output is also WSJT-X's input: no hold, hard clip."""
    new, old = make(), legacy(make())
    new.enter_digital_mode()
    old.enter_digital_mode()
    for d in (new, old):       # enter_digital_mode changes the passband
        d._ssb_filter = d._design_ssb_filter(3000.0, "USB", d.low_cut_hz)
        d._ssb_overlap = np.zeros(len(d._ssb_filter) - 1, dtype=np.complex64)
        d.manual_gain = 8.0
    def vec():
        s = Source()
        yield from s.blocks(1.0, amp=0.1)
        yield from s.blocks(1.0, amp=0.0005)
        yield from s.blocks(1.0, amp=0.5)
    a, b = run(new, vec()), run(old, vec())
    assert all(np.array_equal(x, y) for x, y in zip(a, b))
    assert new.limited_count == 0


# ── the limiter ─────────────────────────────────────────────────────────

def speech_after_pause(noise=0.0005):
    """Speech, a long near-silent pause, then a loud syllable: the clipping case."""
    s = Source()
    yield from s.blocks(1.0, amp=0.05, noise=noise)
    yield from s.blocks(1.5, amp=0.0, noise=noise)
    yield from s.blocks(1.0, amp=0.6, noise=noise)


@pytest.mark.parametrize("agc,manual_gain", [("fast", 1.0), ("fast", 4.0), ("slow", 10.0), ("off", 10.0)])
def test_no_sample_over_the_limit(agc, manual_gain):
    d = make(agc, manual_gain)
    out = np.concatenate(run(d, speech_after_pause()))
    assert int(np.abs(out).max()) <= PEAK_I16


def test_the_limiter_scales_where_the_clip_flattened():
    """Same overload through both: the old chain pins thousands of samples to the rail,
    the limiter leaves a waveform (only a block's own peak sits at the limit)."""
    new, old = make("off", 4.0), legacy(make("off", 4.0))
    a = np.concatenate(run(new, Source().blocks(1.0, amp=0.5)))
    b = np.concatenate(run(old, Source().blocks(1.0, amp=0.5)))
    assert int(np.count_nonzero(np.abs(b) >= PEAK_I16)) > 5000
    assert int(np.count_nonzero(np.abs(a) >= PEAK_I16)) < 300
    assert new.limited_count > 5000          # counted: the samples that needed it


def test_the_limiter_releases_in_a_few_hundred_ms():
    d = make("off", 4.0)
    s = Source()
    run(d, s.blocks(0.2, amp=0.5))
    held = d._lim_gain
    assert held < 0.6
    gains = []
    for b in s.blocks(1.5, amp=0.01):
        d._browser_chain(b)
        gains.append(d._lim_gain)
    assert all(g2 >= g1 for g1, g2 in zip(gains, gains[1:]))     # only ever recovers
    t63 = next(i for i, g in enumerate(gains) if 1 - g <= (1 - held) * np.exp(-1)) * BLOCK / INTERMEDIATE_RATE_HZ
    assert 0.1 <= t63 <= 0.2
    assert gains[-1] == 1.0                   # and gets all the way back
    quiet = (0.01 * np.ones(BLOCK)).astype(np.float32)
    assert d._limit(quiet) is quiet           # idle again: the untouched path


# ── AGC hold ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("agc", ["fast", "slow"])
def test_agc_gain_does_not_rise_in_silence_after_speech(agc):
    new, old = make(agc), legacy(make(agc))
    for d in (new, old):
        run(d, Source().blocks(2.0, amp=0.05, noise=0.0002))
    at_speech = new.agc_gain
    assert at_speech == old.agc_gain
    s_new, s_old = Source(seed=9), Source(seed=9)
    peak_gain = 0.0
    for b in s_new.blocks(1.5, amp=0.0, noise=0.0002):
        new._browser_chain(b)
        peak_gain = max(peak_gain, new.agc_gain)
    run(old, s_old.blocks(1.5, amp=0.0, noise=0.0002))
    # The one block that straddles the end of the speech (the SSB filter's tail) is not
    # yet "well below" it, so the gain may creep a few percent there, then stops.
    assert peak_gain <= at_speech * 1.05
    assert new.agc_gain == peak_gain
    assert old.agc_gain == 6.0                # what it used to do: run to the ceiling


def test_agc_gain_still_falls_while_held():
    """Hold only stops a rise. A louder signal pulls the gain down as fast as ever."""
    new, old = make(), legacy(make())
    def vec():
        s = Source()
        yield from s.blocks(1.0, amp=0.05)
        yield from s.blocks(0.5, amp=0.0, noise=0.0002)
        yield from s.blocks(0.5, amp=0.4)
    run(new, vec())
    run(old, vec())
    assert new.agc_gain == pytest.approx(0.15 / (0.4 / np.sqrt(2)), rel=0.05)
    assert new.agc_gain == pytest.approx(old.agc_gain, rel=0.01)


def test_a_channel_that_stays_quiet_gets_its_gain_back():
    """The speech level is forgotten over a few seconds, so band noise left behind by a
    station that has gone is brought up again (noise here is above the absolute floor)."""
    d = make()
    s = Source()
    run(d, s.blocks(2.0, amp=0.05, noise=0.002))
    at_speech = d.agc_gain
    run(d, s.blocks(1.0, amp=0.0, noise=0.002))
    assert d.agc_gain <= at_speech * 1.05     # still held a second in
    run(d, s.blocks(9.0, amp=0.0, noise=0.002))
    assert d.agc_gain > 5.5


def test_retuning_drops_the_speech_level():
    """Strong station, then a weak one on a new frequency: no hold carried across."""
    d = make()
    s = Source()
    run(d, s.blocks(2.0, amp=0.3))
    d.set_target(14_250_000.0, "USB", 2800.0)
    run(d, s.blocks(1.0, amp=0.002, noise=0.0005))
    assert d.agc_gain > 5.5


def test_with_hold_the_syllable_after_a_pause_needs_no_limiting():
    """The reported case end to end: the old chain clips the syllable after the pause;
    with the gain held through the pause it passes under the limit untouched."""
    def vec():
        s = Source()
        yield from s.blocks(2.0, amp=0.3, noise=0.0002)
        yield from s.blocks(1.5, amp=0.0, noise=0.0002)
        yield from s.blocks(0.5, amp=0.3, noise=0.0002)
    new, old = make(), legacy(make())
    a = np.concatenate(run(new, vec()))
    b = np.concatenate(run(old, vec()))
    assert int(np.count_nonzero(np.abs(b) >= PEAK_I16)) > 0
    assert new.limited_count == 0 and int(np.abs(a).max()) < PEAK_I16


# ── the summary log line ────────────────────────────────────────────────

def test_one_summary_line_per_second_and_only_when_limiting(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(audio_demod.time, "monotonic", lambda: clock[0])
    d = make("off", 4.0)
    s = Source()
    dt = BLOCK / INTERMEDIATE_RATE_HZ

    def play(seconds, amp):
        for b in s.blocks(seconds, amp=amp):
            d._browser_chain(b)
            clock[0] += dt

    with caplog.at_level(logging.WARNING, logger="sdr.audio_demod"):
        play(3.0, 0.01)                       # nothing limited: nothing logged
        assert [r for r in caplog.records if "limiter" in r.getMessage()] == []
        play(3.0, 0.5)                        # limiting throughout
        play(2.0, 0.01)                       # and quiet again
    lines = [r.getMessage() for r in caplog.records if "limiter" in r.getMessage()]
    assert 3 <= len(lines) <= 4               # one per second of limiting, none after
    assert all("samples over 0.95" in m for m in lines)
    counted = sum(int(m.split(":")[1].split()[0]) for m in lines)
    assert counted == d.limited_count > 0


def test_hold_and_limit_at_the_nr_block_size():
    """With DNR on the chain sees 100 ms blocks, not 8 ms ones. Same guarantees."""
    def vec():
        s = Source()
        yield from s.blocks(2.0, amp=0.3, noise=0.0002, block=1600)
        yield from s.blocks(1.5, amp=0.0, noise=0.0002, block=1600)
        yield from s.blocks(1.0, amp=0.9, noise=0.0002, block=1600)
    d = make("fast", 4.0)
    gains, out = [], []
    for b in vec():
        out.append(np.frombuffer(d._browser_chain(b), dtype=np.int16))
        gains.append(d.agc_gain)
    assert int(np.abs(np.concatenate(out)).max()) <= PEAK_I16
    assert max(gains[20:35]) <= gains[19] * 1.05      # the pause: blocks 20..34
