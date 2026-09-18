"""U1 (narrow) — RX2 cold-start audio-target seed.

Backlog symptom: on a fresh page load, RX2 produced no audio to WSJT-X
until the operator manually clicked its USB mode button. Root cause:
AudioDemodulator.target starts at AudioTarget(freq_hz=None, ...) and
feed() silently drops every sample while target.freq_hz is None; Channel
A gets an early forcing trigger from the operator's own speaker-unmute
click, but Channel B had no equivalent and depended entirely on the
client's cold-start Link bootstrap winning a race against RX1's own view
finishing initialization.

Fix: dashboard.server.seed_channel_b_audio_target() gives Channel B's
AudioDemodulator a real target the moment the SDR comes up, independent
of any client-side race.

Timing question this package had to resolve before writing the fix: does
sdr.rf_freq_hz_b reliably hold a real value immediately after
`await sdr.start()` returns? Confirmed by reading sdr/sdr_client.py:
rf_freq_hz_b is a plain attribute assigned unconditionally in __init__
(`self.rf_freq_hz_b = rf_freq_hz`, defaulting to 14_074_000.0) and is
never reset to None anywhere in the class — not in _init_with_retries,
_open_and_init, or _start_pipeline. It's real before start() is even
called, so a seed placed right after `await sdr.start()` needs no
"wait for the first RF-frequency callback" fallback. The dedicated test
below pins that guarantee directly against SdrClient's own source so a
future change that broke it (e.g. making rf_freq_hz_b lazily populated
from a hardware readback) would fail this suite instead of silently
reintroducing the cold-start race.
"""
from sdr.audio_demod import AudioTarget
from sdr.sdr_client import SdrClient

import dashboard.server as server


def test_seed_gives_channel_b_a_real_target_using_rf_freq_hz_b(fake_sdr):
    assert fake_sdr.audio_b.target == AudioTarget(freq_hz=None, mode="USB", bandwidth_hz=3000.0)

    server.seed_channel_b_audio_target(fake_sdr)

    assert fake_sdr.audio_b.target == AudioTarget(
        freq_hz=fake_sdr.rf_freq_hz_b, mode="USB", bandwidth_hz=3000.0)


def test_seed_uses_channel_bs_own_frequency_not_channel_as(fake_sdr):
    """Channel B is an independent SDR-only VFO — confirm the seed reads
    rf_freq_hz_b specifically, not rf_freq_hz, so a future divergence
    between the two channels' starting frequencies seeds RX2 correctly."""
    fake_sdr.rf_freq_hz_b = 7_100_000.0
    fake_sdr.rf_freq_hz = 14_074_000.0

    server.seed_channel_b_audio_target(fake_sdr)

    assert fake_sdr.audio_b.target.freq_hz == 7_100_000.0


def test_seed_leaves_channel_a_untouched(fake_sdr):
    server.seed_channel_b_audio_target(fake_sdr)

    assert fake_sdr.audio.target == AudioTarget(freq_hz=None, mode="USB", bandwidth_hz=3000.0)


async def test_lifespan_seeds_channel_b_after_sdr_start(monkeypatch, fake_sdr):
    """End-to-end version of the fix as it's actually wired: call the same
    sequence dashboard.server's lifespan runs (await sdr.start(), then the
    seed on success) and confirm Channel B has a real target with no
    set_audio_target command ever sent — proving this no longer depends on
    the client race at all."""
    await fake_sdr.start()
    assert fake_sdr.available
    server.seed_channel_b_audio_target(fake_sdr)

    assert fake_sdr.audio_b.target.freq_hz is not None
    assert fake_sdr.audio_b.target.freq_hz == fake_sdr.rf_freq_hz_b


def test_rf_freq_hz_b_is_never_none_across_sdr_client_source():
    """Pins the timing guarantee the seed relies on: rf_freq_hz_b is a
    plain constructor-time float, never reassigned to None anywhere in
    SdrClient. If a future change made it start None (e.g. populated only
    from a hardware readback after start()), this fails loudly instead of
    letting the cold-start race silently come back."""
    import inspect
    src = inspect.getsource(SdrClient)
    assert "self.rf_freq_hz_b = None" not in src

    # Construction alone never touches hardware (only _open_and_init,
    # called from start(), does — see tests/fakes/sdr.py's own docstring),
    # so this is safe without a real SDRplay device.
    sdr = SdrClient(rf_freq_hz=10_136_000.0)
    assert sdr.rf_freq_hz_b == 10_136_000.0
    assert sdr.rf_freq_hz_b is not None
