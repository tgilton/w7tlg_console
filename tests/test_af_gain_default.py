"""AF Gain (AudioDemodulator.manual_gain) defaults to 1.0 on both receivers.

It was 4.0 (400%) until 2026-09-20. That value multiplies straight into
_demodulate's np.clip(..., -0.95, 0.95) limiter, and in practice ordinary
signals rode it continuously. Terry's decision: default 1.0, raised by
hand when a weak band needs it.

This matters beyond listening comfort — the same samples go to
DigitalAudioOutput and out over BlackHole to WSJT-X, so the default is
also the decoders' input level. A future change here should be a
deliberate one, which is what this test is for.
"""
from sdr.audio_demod import AudioDemodulator
from tests.fakes.sdr import FakeSdrClient


def test_audio_demodulator_default_manual_gain_is_unity():
    assert AudioDemodulator().manual_gain == 1.0


def test_both_receivers_start_at_unity_gain():
    """Neither channel is constructed with a gain argument, so both take
    the class default — this pins that they are actually built that way."""
    sdr = FakeSdrClient()
    assert sdr.audio.manual_gain == 1.0
    assert sdr.audio_b.manual_gain == 1.0


def test_nothing_rescales_the_default_on_start():
    """Guards the other direction: a later 'helpful' initialisation in the
    client or at startup would silently reintroduce the clipping."""
    sdr = FakeSdrClient()
    assert sdr.audio.manual_gain == AudioDemodulator().manual_gain
    assert sdr.audio_b.manual_gain == AudioDemodulator().manual_gain
