"""Audio input queue: W7TLG_AUDIO_QUEUE sizing, and drop warnings tagged per receiver.

The "Audio queue drops" warning used to be identical for both receivers, so a
burst could not be attributed to RX1 or RX2 from the log. Each demodulator
already counts its own drops; the line now says whose they are.
"""
import logging
from pathlib import Path

import numpy as np
import pytest

from sdr.audio_demod import AUDIO_QUEUE_DEFAULT, NR_HOP_CHUNKS_DEFAULT, AudioDemodulator
from tests.fakes.sdr import FakeSdrClient


def test_queue_default_is_2048(monkeypatch):
    monkeypatch.delenv("W7TLG_AUDIO_QUEUE", raising=False)
    assert AUDIO_QUEUE_DEFAULT == 2048
    assert AudioDemodulator()._q.maxsize == 2048


@pytest.mark.parametrize("value,expected", [
    ("1024", 1024), ("1", 1), ("4096", 4096),
    ("512", 512),                                          # the depth before 2026-10-10
    ("0", 2048), ("-5", 2048), ("abc", 2048), ("", 2048), ("12.5", 2048),
])
def test_queue_size_from_env(monkeypatch, value, expected):
    monkeypatch.setenv("W7TLG_AUDIO_QUEUE", value)
    assert AudioDemodulator()._q.maxsize == expected


def test_receivers_are_labelled():
    sdr = FakeSdrClient()
    assert (sdr.audio.rx_label, sdr.audio_b.rx_label) == ("RX1", "RX2")
    # The real client labels them the same way (it cannot be built without the device).
    src = Path("sdr/sdr_client.py").read_text()
    assert 'self.audio.rx_label = "RX1"' in src and 'self.audio_b.rx_label = "RX2"' in src


def test_drop_warning_names_the_receiver_and_counts_per_receiver(monkeypatch, caplog):
    monkeypatch.setenv("W7TLG_AUDIO_QUEUE", "4")
    sdr = FakeSdrClient()
    x = np.zeros(16, dtype=np.int16)
    for d, n in ((sdr.audio, 7), (sdr.audio_b, 5)):
        d.enabled = True
        for _ in range(n):
            d.feed(x, x)
    assert (sdr.audio.dropped_count, sdr.audio_b.dropped_count) == (3, 1)
    with caplog.at_level(logging.WARNING, logger="sdr.audio_demod"):
        assert sdr.audio._log_queue_drops(0) == 3
        assert sdr.audio_b._log_queue_drops(0) == 1
        assert sdr.audio._log_queue_drops(3) == 3        # nothing new: no line
    assert [r.getMessage() for r in caplog.records] == [
        "Audio queue drops RX1: 3 total (+3 in last 5s)",
        "Audio queue drops RX2: 1 total (+1 in last 5s)",
    ]


# ── W7TLG_NR_HOP_CHUNKS ─────────────────────────────────────────────────

def test_nr_hop_default_is_4_chunks(monkeypatch):
    monkeypatch.delenv("W7TLG_NR_HOP_CHUNKS", raising=False)
    assert NR_HOP_CHUNKS_DEFAULT == 4
    assert AudioDemodulator()._nr_hop_samples == 4 * 131   # 33 ms


@pytest.mark.parametrize("value,expected", [
    ("13", 13 * 131), ("7", 7 * 131), ("4", 4 * 131), ("2", 2 * 131), ("1", 131),
    ("48", 48 * 131),                                      # the longest that fits the window
    ("0", 524), ("-3", 524), ("x", 524), ("", 524), ("2.5", 524),
    ("49", 524),                                           # longer than the 0.4 s NR window
])
def test_nr_hop_from_env(monkeypatch, value, expected):
    monkeypatch.setenv("W7TLG_NR_HOP_CHUNKS", value)
    assert AudioDemodulator()._nr_hop_samples == expected


@pytest.mark.parametrize("chunks", [None, 13, 7, 4, 2])
def test_nr_releases_audio_once_per_hop(monkeypatch, chunks):
    """Through _apply_nr with the model stubbed out: one output per `chunks` batches,
    carrying exactly the samples that arrived since the last one."""
    from sdr import audio_demod
    if chunks is None:
        monkeypatch.delenv("W7TLG_NR_HOP_CHUNKS", raising=False)
    else:
        monkeypatch.setenv("W7TLG_NR_HOP_CHUNKS", str(chunks))
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(audio_demod, "torch", torch)
    monkeypatch.setattr(audio_demod, "_df_enhance", lambda model, state, t, atten_lim_db=None: t)
    monkeypatch.setattr(audio_demod._nr_latency_logger, "disabled", True)
    d = AudioDemodulator()
    d._nr_model = object()
    sizes, calls = [], []
    for i in range(52):
        y = d._apply_nr(np.full(131, 0.01, dtype=np.float32))
        if y is not None:
            sizes.append(len(y))
            calls.append(i + 1)
    n = chunks or 4
    assert calls == list(range(n, 53, n))
    assert set(sizes) == {n * 131}


def test_profiling_is_off_unless_asked_for(monkeypatch):
    from sdr import audio_profile
    assert audio_profile.parse_mode(None) == (False, False)
    assert audio_profile.parse_mode("") == (False, False)
    assert audio_profile.parse_mode("0") == (False, False)
    assert audio_profile.parse_mode("1") == (True, False)
    assert audio_profile.parse_mode("verbose") == (True, True)
