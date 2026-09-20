"""Proves the fake SDR works and matches SdrClient's real call surface —
T0's own validation bar (see REFACTOR_PLAN.md's T0 section).

Only checks the methods this fake actually implements (SdrClient's own
public surface is much larger — internal device-lifecycle helpers and the
antenna/notch-sweep private methods are intentionally out of scope for T0;
later packages extend this fake as they need more of it)."""
import inspect

from sdr.sdr_client import SdrClient
from tests.fakes.sdr import FakeSdrClient


def _public_methods(cls):
    return {
        name: member
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
        and (inspect.isfunction(member) or isinstance(member, property))
    }


def test_fake_signatures_match_real_where_implemented():
    """For every method this fake implements, its signature matches the
    real SdrClient's exactly — does not require the fake to implement
    100% of SdrClient's public surface (see module docstring)."""
    real_methods = _public_methods(SdrClient)
    fake_methods = _public_methods(FakeSdrClient)

    for name, fake_member in fake_methods.items():
        if name not in real_methods:
            continue  # test-only helper method, not part of the real API
        real_member = real_methods[name]
        if isinstance(real_member, property):
            continue
        real_params = list(inspect.signature(real_member).parameters)
        fake_params = list(inspect.signature(fake_member).parameters)
        assert real_params == fake_params, (
            f"{name} signature mismatch: real={real_params} fake={fake_params}")


async def test_fake_sdr_reports_samples(fake_sdr):
    """The concrete bar T0 sets: a fake SDR reports samples."""
    await fake_sdr.start()
    assert fake_sdr.available is True

    frames = []

    async def on_spectrum(frame):
        frames.append(frame)

    fake_sdr.on_spectrum(on_spectrum)
    synthetic_frame = {"kind": "wide", "bins": [0.0, -20.0, -40.0]}
    await fake_sdr.emit_spectrum(synthetic_frame)
    assert frames == [synthetic_frame]


async def test_channel_b_alive_tracks_emit_spectrum_b(fake_sdr):
    assert fake_sdr.channel_b_alive is False
    await fake_sdr.start()
    assert fake_sdr.channel_b_alive is False  # no frame yet
    await fake_sdr.emit_spectrum_b({"kind": "wide_b"})
    assert fake_sdr.channel_b_alive is True


def test_set_center_freq_is_a_noop_before_start(fake_sdr):
    fake_sdr.set_center_freq_hz(7_100_000)
    assert fake_sdr.rf_freq_hz != 7_100_000  # unavailable — real client no-ops too


async def test_gate_tx_delegates_to_real_subcomponents(fake_sdr):
    """audio/audio_b/combiner/digital_audio are the REAL production
    classes, not reimplemented fakes — gate_tx() proves the wiring."""
    fake_sdr.audio.tx_active = False
    fake_sdr.gate_tx()
    assert fake_sdr.audio.tx_active is True
    assert fake_sdr.audio_b.tx_active is True
    assert fake_sdr.combiner.tx_active is True
