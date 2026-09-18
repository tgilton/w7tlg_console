"""Fake SdrClient — same public call signatures as sdr/sdr_client.py's
SdrClient, minus the real SDRplay hardware session.

The hardware boundary in the real class is narrow and lazy: only
sdrplay_capi.load_library() (called from _open_and_init, itself only ever
called from start()) touches the vendor library, and none of SdrClient's
composed sub-objects — AudioDemodulator, Combiner, DigitalAudioOutput — do
any hardware I/O in their own __init__ (confirmed by reading each class).
So this fake reuses the REAL sub-objects unmodified — identical attribute
names/behavior to production, no separate fake to keep in sync — and only
fakes the device-facing parts: no executor calls, no consumer/audio
threads (their own .start() is never called), and start()/stop() just flip
available/status the way a successful/torn-down device session would.

Use emit_spectrum()/emit_spectrum_b() to simulate a computed FFT frame
reaching subscribers, the way the real consumer thread's _publish/
_publish_b would.
"""
from __future__ import annotations

import time
from typing import Optional

from sdr.audio_demod import AudioDemodulator
from sdr.combiner import Combiner
from sdr.virtual_audio_output import DigitalAudioOutput


class FakeSdrClient:

    def __init__(
        self,
        rf_freq_hz: float = 14_074_000.0,
        sample_rate_hz: float = 2_000_000.0,
        fft_size: int = 65536,
        display_fps: float = 18.0,
        spectrum_avg_frames: float = 10.0,
        rf_gain_pct: float = 80.0,
        lib_path: str = "",
    ):
        self.rf_freq_hz = rf_freq_hz
        self.sample_rate_hz = sample_rate_hz
        self.fft_size = fft_size
        self.display_fps = display_fps
        self.spectrum_avg_frames = spectrum_avg_frames
        self.rf_gain_pct = rf_gain_pct
        # Channel B starts parked on the same freq/gain as Channel A —
        # matches SdrClient.__init__.
        self.rf_freq_hz_b = rf_freq_hz
        self.rf_gain_pct_b = rf_gain_pct
        self.rf_notch_enabled = False
        self.rf_notch_enabled_b = False
        self.dab_notch_enabled = False
        self.dab_notch_enabled_b = False
        self.lib_path = lib_path

        self.available = False
        self.status = "stopped"   # stopped | live | unavailable
        self.dropped_count = 0
        self.dropped_count_b = 0

        self._spectrum_callbacks = []
        self._spectrum_callbacks_b = []
        self._last_sample_at_b = 0.0
        self._stall_timeout_s = 3.0

        self.audio = AudioDemodulator(input_rate_hz=sample_rate_hz)
        self.audio_b = AudioDemodulator(input_rate_hz=sample_rate_hz)
        self.combiner = Combiner(input_rate_hz=sample_rate_hz)
        self.digital_audio = DigitalAudioOutput()
        self.audio.on_audio(self.digital_audio.on_audio_frame)

        self.started = False
        self.stopped = False

    # ------------------------------------------------------------------
    # Public API — mirrors SdrClient exactly
    # ------------------------------------------------------------------

    @property
    def antenna_label(self) -> str:
        return "A"

    @property
    def antenna_label_b(self) -> str:
        return "B"

    @property
    def channel_b_alive(self) -> bool:
        if not self.available or self._last_sample_at_b == 0.0:
            return False
        return time.monotonic() - self._last_sample_at_b < self._stall_timeout_s

    def on_spectrum(self, cb):
        self._spectrum_callbacks.append(cb)

    def on_spectrum_b(self, cb):
        self._spectrum_callbacks_b.append(cb)

    def gate_tx(self):
        self.audio.gate_tx()
        self.audio_b.gate_tx()
        self.combiner.gate_tx()
        self.digital_audio.flush()

    async def start(self):
        """Fake device bring-up: always succeeds, no hardware, no threads."""
        self.started = True
        self.available = True
        self.status = "live"

    async def stop(self):
        if not self.available:
            return
        self.stopped = True
        self.available = False
        self.status = "stopped"

    def set_center_freq_hz(self, freq_hz: float):
        if not self.available:
            return
        self.rf_freq_hz = freq_hz
        self.audio.rf_center_hz = freq_hz

    def set_rf_gain_pct(self, pct: float):
        if not self.available:
            return
        self.rf_gain_pct = max(0.0, min(100.0, pct))

    def set_center_freq_hz_b(self, freq_hz: float):
        if not self.available:
            return
        self.rf_freq_hz_b = freq_hz
        self.audio_b.rf_center_hz = freq_hz

    def set_rf_gain_pct_b(self, pct: float):
        if not self.available:
            return
        self.rf_gain_pct_b = max(0.0, min(100.0, pct))

    def set_rf_notch(self, enabled: bool):
        if not self.available:
            return
        self.rf_notch_enabled = bool(enabled)

    def set_rf_notch_b(self, enabled: bool):
        if not self.available:
            return
        self.rf_notch_enabled_b = bool(enabled)

    def set_dab_notch(self, enabled: bool):
        if not self.available:
            return
        self.dab_notch_enabled = bool(enabled)

    def set_dab_notch_b(self, enabled: bool):
        if not self.available:
            return
        self.dab_notch_enabled_b = bool(enabled)

    def passband_strength_db(self, center_hz: float, bandwidth_hz: float) -> Optional[float]:
        return None

    def passband_strength_db_b(self, center_hz: float, bandwidth_hz: float) -> Optional[float]:
        return None

    def iq_phase_diff_deg(self) -> Optional[float]:
        return None

    # ------------------------------------------------------------------
    # Test helpers — not part of the real client's API
    # ------------------------------------------------------------------

    async def emit_spectrum(self, frame: dict):
        """Simulate Channel A's consumer thread having computed and
        published a spectrum/waterfall frame."""
        for cb in list(self._spectrum_callbacks):
            await cb(frame)

    async def emit_spectrum_b(self, frame: dict):
        """Simulate Channel B's consumer thread publishing a frame — also
        stamps the liveness clock channel_b_alive reads."""
        self._last_sample_at_b = time.monotonic()
        for cb in list(self._spectrum_callbacks_b):
            await cb(frame)
