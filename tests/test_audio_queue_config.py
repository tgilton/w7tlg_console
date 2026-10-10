"""Audio input queue: W7TLG_AUDIO_QUEUE sizing, and drop warnings tagged per receiver.

The "Audio queue drops" warning used to be identical for both receivers, so a
burst could not be attributed to RX1 or RX2 from the log. Each demodulator
already counts its own drops; the line now says whose they are.
"""
import logging
from pathlib import Path

import numpy as np
import pytest

from sdr.audio_demod import AUDIO_QUEUE_DEFAULT, AudioDemodulator
from tests.fakes.sdr import FakeSdrClient


def test_queue_default_is_512(monkeypatch):
    monkeypatch.delenv("W7TLG_AUDIO_QUEUE", raising=False)
    assert AUDIO_QUEUE_DEFAULT == 512
    assert AudioDemodulator()._q.maxsize == 512


@pytest.mark.parametrize("value,expected", [
    ("1024", 1024), ("1", 1), ("2048", 2048),
    ("0", 512), ("-5", 512), ("abc", 512), ("", 512), ("12.5", 512),
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
