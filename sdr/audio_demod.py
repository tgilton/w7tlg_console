"""
SSB Audio Demodulator

Taps the same wideband IQ stream as the spectrum FFT pipeline (fed via
SdrClient's native callback) and continuously demodulates a single SSB
channel to mono audio for browser playback. Unlike the FFT path — which
deliberately samples one block per display tick and discards the rest —
audio needs every sample, gapless, in order, so it runs its own dedicated
queue/thread fed in parallel from the same callback.

First cut: real-world listening surfaced several real bugs in turn
(severe non-stateful-filter distortion, a units mismatch in the AGC,
clipping from a partial-correction attack). The decimation stage and the
AGC below were both rewritten after repeated regressions to favor the
simplest, most predictable design over a cleverer one that's hard to
verify without being able to listen directly.
"""

import asyncio
import logging
import queue
import threading
from collections.abc import Callable, Coroutine
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], Coroutine]

INTERMEDIATE_RATE_HZ = 16_000   # final output audio rate


class AudioDemodulator:
    def __init__(self, input_rate_hz: float = 2_000_000.0, batch_samples: int = 16384):
        self.input_rate_hz = input_rate_hz
        self.decim_factor = round(input_rate_hz / INTERMEDIATE_RATE_HZ)
        # Snapped to an exact multiple of decim_factor so the decimation
        # phase never drifts at batch boundaries (16384 isn't a multiple of
        # 125 — left a 9-sample phase slip every batch otherwise).
        self.batch_samples = (batch_samples // self.decim_factor) * self.decim_factor

        self.enabled = False
        self.target_freq_hz: Optional[float] = None
        self.rf_center_hz: Optional[float] = None
        self.mode = "USB"          # USB | LSB
        self.bandwidth_hz = 2800.0
        self.agc_gain = 1.0

        self._q: "queue.Queue" = queue.Queue(maxsize=64)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._audio_callbacks: list[AudioCallback] = []
        self._sample_counter = 0
        self._acc_i = np.empty(0, dtype=np.int16)
        self._acc_q = np.empty(0, dtype=np.int16)

        self._ssb_filter: Optional[np.ndarray] = None
        self._ssb_filter_key = None
        self._ssb_overlap: Optional[np.ndarray] = None

        self._decim_filter = self._design_decim_filter()
        self._decim_overlap = np.zeros(len(self._decim_filter) - 1, dtype=np.complex64)

    def on_audio(self, cb: AudioCallback):
        self._audio_callbacks.append(cb)

    def feed(self, xi: np.ndarray, xq: np.ndarray):
        """Called from SdrClient's native callback thread — must be fast."""
        if not self.enabled:
            return
        try:
            self._q.put_nowait((xi, xq))
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait((xi, xq))
            except queue.Full:
                pass

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._stop_event.clear()
        self._sample_counter = 0
        self._acc_i = np.empty(0, dtype=np.int16)
        self._acc_q = np.empty(0, dtype=np.int16)
        self._ssb_filter = None
        self._ssb_filter_key = None
        self._ssb_overlap = None
        self._decim_overlap = np.zeros(len(self._decim_filter) - 1, dtype=np.complex64)
        self.agc_gain = 1.0
        self._thread = threading.Thread(target=self._run, name="sdr-audio", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _design_decim_filter(self) -> np.ndarray:
        """Anti-alias lowpass for decimating input_rate_hz -> INTERMEDIATE_RATE_HZ,
        applied via overlap-save (stateful across batches). Replaces
        scipy's resample_poly, whose own internal filter is NOT stateful
        across independent per-batch calls — the same class of bug as the
        original severe SSB-filter distortion, just smaller in magnitude.
        Real-valued filter (decimation doesn't need sideband selection)."""
        cutoff_hz = INTERMEDIATE_RATE_HZ / 2.4   # comfortably inside the new Nyquist
        num_taps = 401
        n = np.arange(num_taps) - (num_taps - 1) / 2.0
        h = np.sinc(2 * cutoff_hz / self.input_rate_hz * n)
        h *= np.hamming(num_taps)
        h /= np.sum(h)
        return h.astype(np.complex64)

    def _design_ssb_filter(self, bandwidth_hz: float, mode: str) -> np.ndarray:
        """Complex FIR that passes only the desired sideband — a real
        windowed-sinc lowpass frequency-shifted by a complex exponential so
        its passband sits entirely above (USB) or below (LSB) 0Hz, instead
        of symmetric around it. Filtering with this and taking the real
        part IS the SSB demodulation — no separate detector needed.

        Passband is [low_cut_hz, low_cut_hz + bandwidth_hz], not [0, bandwidth_hz]
        — a real voice passband excludes near-DC content (rumble/hum)."""
        num_taps = 161
        low_cut_hz = 300.0
        high_cut_hz = low_cut_hz + bandwidth_hz
        center_hz = (low_cut_hz + high_cut_hz) / 2.0
        half_width_hz = (high_cut_hz - low_cut_hz) / 2.0
        n = np.arange(num_taps) - (num_taps - 1) / 2.0
        lpf = np.sinc(2 * half_width_hz / INTERMEDIATE_RATE_HZ * n)
        lpf *= np.hamming(num_taps)
        lpf /= np.sum(lpf)
        shift_hz = center_hz if mode == "USB" else -center_hz
        shifted = lpf * np.exp(1j * 2 * np.pi * shift_hz / INTERMEDIATE_RATE_HZ * n)
        return shifted.astype(np.complex64)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                xi, xq = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._acc_i = np.concatenate([self._acc_i, xi])
            self._acc_q = np.concatenate([self._acc_q, xq])
            if len(self._acc_i) < self.batch_samples:
                continue

            block_i, self._acc_i = self._acc_i[:self.batch_samples], self._acc_i[self.batch_samples:]
            block_q, self._acc_q = self._acc_q[:self.batch_samples], self._acc_q[self.batch_samples:]

            if self.target_freq_hz is None or self.rf_center_hz is None:
                self._sample_counter += self.batch_samples
                continue

            try:
                audio_bytes = self._process(block_i, block_q)
            except Exception:
                logger.exception("Audio demod error")
                continue
            if audio_bytes is not None and self._loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(self._publish(audio_bytes), self._loop)
                except RuntimeError:
                    pass   # loop closing/closed during shutdown

    def _process(self, block_i: np.ndarray, block_q: np.ndarray) -> bytes:
        n = len(block_i)
        offset_hz = self.target_freq_hz - self.rf_center_hz
        t = self._sample_counter + np.arange(n)
        self._sample_counter += n

        mix = np.exp(-1j * 2 * np.pi * offset_hz / self.input_rate_hz * t).astype(np.complex64)
        baseband = (block_i.astype(np.float32) + 1j * block_q.astype(np.float32)) * mix

        # Stateful decimation (overlap-save) — see _design_decim_filter.
        decim_extended = np.concatenate([self._decim_overlap, baseband])
        decim_filtered = np.convolve(decim_extended, self._decim_filter, mode="valid")
        self._decim_overlap = decim_extended[-(len(self._decim_filter) - 1):]
        intermediate = decim_filtered[::self.decim_factor]

        filter_key = (self.bandwidth_hz, self.mode)
        if self._ssb_filter is None or self._ssb_filter_key != filter_key:
            self._ssb_filter = self._design_ssb_filter(self.bandwidth_hz, self.mode)
            self._ssb_filter_key = filter_key
            self._ssb_overlap = np.zeros(len(self._ssb_filter) - 1, dtype=np.complex64)

        # Stateful SSB channel filter (overlap-save) — each batch (~131
        # samples at 16kHz) is shorter than the 161-tap filter, so a plain
        # mode="same" convolution was dominated by zero-padded edge
        # artifacts on almost every sample, every ~8ms. That was the
        # original, most severe source of distortion.
        ssb_extended = np.concatenate([self._ssb_overlap, intermediate])
        filtered = np.convolve(ssb_extended, self._ssb_filter, mode="valid")
        self._ssb_overlap = ssb_extended[-(len(self._ssb_filter) - 1):]

        # Normalize to [-1, 1] BEFORE the AGC math — filtered audio is
        # still in raw int16-derived units (peaks in the thousands), not
        # the normalized scale the AGC target assumes.
        audio = np.real(filtered).astype(np.float32) / 32768.0

        # AGC: single slow time constant on RMS (not peak — less jittery
        # batch to batch), same rate both directions. Several rounds of
        # asymmetric fast-attack/slow-decay tuning each fixed one failure
        # mode while introducing another (pumping, on/off, clipping on
        # transients) — without being able to listen directly, a slower,
        # symmetric, predictable design plus a hard safety limiter is more
        # trustworthy than continuing to chase attack/decay parameters.
        rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-6
        target_rms = 0.1
        desired_gain = target_rms / rms
        self.agc_gain += (desired_gain - self.agc_gain) * 0.03
        self.agc_gain = float(np.clip(self.agc_gain, 0.1, 6.0))
        audio = np.clip(audio * self.agc_gain, -0.95, 0.95)   # hard safety backstop, not the primary leveler

        pcm16 = (audio * 32767).astype(np.int16)
        return pcm16.tobytes()

    async def _publish(self, audio_bytes: bytes):
        for cb in self._audio_callbacks:
            await cb(audio_bytes)
