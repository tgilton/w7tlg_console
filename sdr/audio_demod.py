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
import sys
import threading
import time
import types
from collections.abc import Callable, Coroutine
from typing import Optional

import numpy as np
from scipy.signal import resample_poly, sosfilt


def _ensure_torchaudio_backend_shim():
    """deepfilternet 0.5.6's df.io unconditionally imports
    torchaudio.backend.common.AudioMetaData, which torchaudio removed in
    newer releases (the legacy backend-dispatch API). We only ever use
    df.enhance's in-memory enhance()/init_df() — never df.io's file-loading
    helpers — so a minimal stub satisfies the dead import without needing
    the real, removed functionality."""
    try:
        import torchaudio.backend.common  # noqa: F401
    except ModuleNotFoundError:
        backend_mod = types.ModuleType("torchaudio.backend")
        common_mod = types.ModuleType("torchaudio.backend.common")

        class AudioMetaData:  # never instantiated — import-satisfying shim only
            pass

        common_mod.AudioMetaData = AudioMetaData
        backend_mod.common = common_mod
        sys.modules.setdefault("torchaudio.backend", backend_mod)
        sys.modules.setdefault("torchaudio.backend.common", common_mod)


try:
    _ensure_torchaudio_backend_shim()
    import torch
    from df.enhance import enhance as _df_enhance, init_df as _df_init
except Exception as _df_import_error:  # pragma: no cover - NR deps missing/broken
    torch = None
    _df_enhance = None
    _df_init = None
    logging.getLogger(__name__).warning(
        f"DeepFilterNet unavailable, noise reduction disabled: {_df_import_error}")

logger = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], Coroutine]

INTERMEDIATE_RATE_HZ = 16_000   # final output audio rate

# Noise reduction (DeepFilterNet3) — runs on its own rolling window rather
# than per-block; see AudioDemodulator._apply_nr for why.
NR_SAMPLE_RATE_HZ = 48_000
NR_RESAMPLE_RATIO = NR_SAMPLE_RATE_HZ // INTERMEDIATE_RATE_HZ   # exact: 3
NR_WINDOW_S = 0.4
NR_HOP_S = 0.1

# AGC time constants in seconds (not per-call fractions) so behavior stays
# correct regardless of how large/small a given _process() call's block is —
# matters once NR is enabled, since NR emits larger, less-frequent blocks
# than the ~8ms blocks AGC was originally tuned against. Values reproduce
# the exact original behavior at the original ~8ms cadence: tau ≈ dt/decay.
AGC_TAU_SLOW_S = 0.273
AGC_TAU_FAST_S = 0.068

# Fine spectrum (digital-mode panadapter zoom) — FFT directly on this file's
# own decimated baseband (16kHz, already centered on target_freq_hz), which
# is already exactly where the audio demod needs it to be. bin_hz =
# INTERMEDIATE_RATE_HZ/FINE_FFT_SIZE ≈ 3.9Hz, vs ~30Hz/bin on the wideband
# capture — needed to actually resolve individual FT8 signals (~50Hz wide,
# 6.25Hz tone spacing) instead of a handful of blurred bins.
FINE_FFT_SIZE = 4096
FINE_FFT_FPS = 15.0
# Exponential moving average in linear power across consecutive fine-FFT
# frames — same idea (and same N-frame-equivalence formula) as SdrClient's
# wideband averaging, since a raw periodogram flickers regardless of FFT
# size and only averaging independent frames actually quiets it. WSJT-X's
# own spectrum display does the same kind of averaging.
FINE_AVG_FRAMES = 4.0
FINE_AVG_DECAY = max(0.0, (FINE_AVG_FRAMES - 1.0) / (FINE_AVG_FRAMES + 1.0))


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
        # Auto-leveling speed, driven by the console's AGC OFF/FAST/SLOW
        # buttons — repurposed to control this instead of the radio's own
        # (now-irrelevant) receiver AGC, since the RSPdx-R2 is the actual
        # receiver and the radio's CAT-commanded AGC has no audible effect.
        # "off" bypasses auto-leveling entirely (manual_gain alone sets the
        # level, same as riding a real radio's AF gain knob with AGC off).
        self.agc_mode = "slow"    # off | fast | slow
        # Separate, user-facing master volume (RX Volume slider) — kept
        # independent of the AGC's own internal gain so "still too quiet"
        # has a direct, predictable knob instead of more guessing at the
        # auto-leveling target. Default landed at 4.0 (400%) after live
        # listening still found 2.0 too quiet — slider goes to 10.0 (1000%)
        # if more is still needed.
        self.manual_gain = 4.0
        self.tx_active = False
        self._was_tx_active = False
        self.dropped_count = 0

        # Passband low cut — 300Hz excludes voice rumble/hum; digital mode
        # widens this to 0Hz (passband starts at the dial frequency itself).
        self.low_cut_hz = 300.0

        # EQ — 3-band shelf/peak (RBJ Audio EQ Cookbook biquads), inert at
        # 0dB on all bands until the operator actually moves a slider.
        self.eq_enabled = True
        self.eq_bass_db = 0.0
        self.eq_mid_db = 0.0
        self.eq_treble_db = 0.0
        self._eq_sos: Optional[np.ndarray] = None
        self._eq_key = None
        self._eq_zi: Optional[np.ndarray] = None

        # Noise reduction (DeepFilterNet3) — on by default for voice
        # listening if the model loaded; digital mode forces this off
        # (see enter_digital_mode).
        self.nr_enabled = _df_enhance is not None
        self.nr_atten_limit_db = 40.0   # higher = more aggressive suppression
        self._nr_model = None
        self._nr_df_state = None
        self._nr_load_failed = False
        self._nr_in_buf = np.zeros(0, dtype=np.float32)
        self._nr_new_samples = 0
        self._nr_window_samples = int(NR_WINDOW_S * INTERMEDIATE_RATE_HZ)
        self._nr_hop_samples = int(NR_HOP_S * INTERMEDIATE_RATE_HZ)

        # Snapshot of voice-mode settings, captured by enter_digital_mode()
        # and restored by exit_digital_mode() — None means "in voice mode".
        self._voice_profile_snapshot: Optional[dict] = None
        self.in_digital_mode = False

        # Fine spectrum — see FINE_FFT_SIZE. Only accumulated/computed in
        # digital mode (cheap either way, but no reason to spend it when
        # nobody's looking at it).
        self._fine_window = np.hanning(FINE_FFT_SIZE).astype(np.float32)
        self._fine_fullscale_ref = 32767.0 * float(np.sum(self._fine_window))
        self._fine_buf = np.zeros(FINE_FFT_SIZE, dtype=np.complex64)
        self._fine_buf_len = 0
        self._fine_avg_power: Optional[np.ndarray] = None
        self._fine_next_tick = 0.0
        self._fine_spectrum_callbacks: list[Callable[[dict], Coroutine]] = []

        # Same reasoning as SdrClient's spectrum queue: a small queue gives
        # almost no headroom against ordinary thread-scheduling/GIL jitter.
        self._q: "queue.Queue" = queue.Queue(maxsize=512)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._audio_callbacks: list[AudioCallback] = []
        self._sample_counter = 0
        # Pre-allocated and written via slice assignment, not concatenate —
        # concatenate-and-grow recopies the whole accumulated buffer on
        # every single native callback, which turned into the real
        # bottleneck once measured (see _run): O(batch size) per chunk
        # instead of O(chunk size).
        self._acc_i = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_q = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_len = 0

        self._ssb_filter: Optional[np.ndarray] = None
        self._ssb_filter_key = None
        self._ssb_overlap: Optional[np.ndarray] = None

        self._decim_filter = self._design_decim_filter()
        self._decim_overlap = np.zeros(len(self._decim_filter) - 1, dtype=np.complex64)

    def on_audio(self, cb: AudioCallback):
        self._audio_callbacks.append(cb)

    def on_fine_spectrum(self, cb: Callable[[dict], Coroutine]):
        self._fine_spectrum_callbacks.append(cb)

    def enter_digital_mode(self):
        """Reconfigure for digital-mode listening (FT8 etc.): AGC off,
        NR/EQ bypassed, passband widened to start right at the dial
        frequency instead of excluding voice rumble/hum. Snapshots the
        current voice-mode settings so exit_digital_mode() restores
        exactly what the operator had dialed in — not just fixed
        'voice defaults'. Idempotent."""
        if self._voice_profile_snapshot is not None:
            return
        self._voice_profile_snapshot = {
            "agc_mode": self.agc_mode,
            "nr_enabled": self.nr_enabled,
            "eq_enabled": self.eq_enabled,
            "low_cut_hz": self.low_cut_hz,
            "bandwidth_hz": self.bandwidth_hz,
        }
        self.agc_mode = "off"
        self.nr_enabled = False
        self.eq_enabled = False
        self.low_cut_hz = 0.0
        self.bandwidth_hz = 3000.0
        self.in_digital_mode = True
        self._fine_buf_len = 0
        self._fine_avg_power = None
        logger.info("Audio chain -> digital mode (AGC off, NR/EQ bypassed, 0-3000Hz passband)")

    def exit_digital_mode(self):
        """Restore whatever voice-mode settings were active before
        enter_digital_mode(). No-op if not currently in digital mode."""
        if self._voice_profile_snapshot is None:
            return
        snap = self._voice_profile_snapshot
        self._voice_profile_snapshot = None
        self.agc_mode = snap["agc_mode"]
        self.nr_enabled = snap["nr_enabled"]
        self.eq_enabled = snap["eq_enabled"]
        self.low_cut_hz = snap["low_cut_hz"]
        self.bandwidth_hz = snap["bandwidth_hz"]
        self.in_digital_mode = False
        self._fine_buf_len = 0
        self._fine_avg_power = None
        logger.info("Audio chain -> voice mode settings restored")

    def feed(self, xi: np.ndarray, xq: np.ndarray):
        """Called from SdrClient's native callback thread — must be fast."""
        if not self.enabled:
            return
        try:
            self._q.put_nowait((xi, xq))
        except queue.Full:
            self.dropped_count += 1
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
        self._acc_i = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_q = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_len = 0
        self._ssb_filter = None
        self._ssb_filter_key = None
        self._ssb_overlap = None
        self._decim_overlap = np.zeros(len(self._decim_filter) - 1, dtype=np.complex64)
        self.agc_gain = 1.0
        self._was_tx_active = False
        self._eq_sos = None
        self._eq_key = None
        self._eq_zi = None
        self._nr_in_buf = np.zeros(0, dtype=np.float32)
        self._nr_new_samples = 0
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

    def _design_ssb_filter(self, bandwidth_hz: float, mode: str,
                           low_cut_hz: float = 300.0) -> np.ndarray:
        """Complex FIR that passes only the desired sideband — a real
        windowed-sinc lowpass frequency-shifted by a complex exponential so
        its passband sits entirely above (USB) or below (LSB) 0Hz, instead
        of symmetric around it. Filtering with this and taking the real
        part IS the SSB demodulation — no separate detector needed.

        Passband is [low_cut_hz, low_cut_hz + bandwidth_hz], not [0, bandwidth_hz]
        — a real voice passband excludes near-DC content (rumble/hum).
        Digital mode passes low_cut_hz=0.0 so the passband starts right at
        the dial frequency, matching how WSJT-X/FT8 expect a USB-demodulated
        audio passband to be laid out."""
        num_taps = 161
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

    def _update_fine_spectrum(self, chunk: np.ndarray):
        """Rolling-window FFT directly on the decimated baseband also used
        for SSB demod (16kHz, already centered on target_freq_hz — no
        separate retuning concept needed, unlike the wideband capture).
        Sliding rather than block-aligned, same idea as SdrClient's own
        consumer loop: always FFT the latest FINE_FFT_SIZE samples, rather
        than waiting ~256ms to accumulate a fresh non-overlapping block."""
        n = len(chunk)
        if n >= FINE_FFT_SIZE:
            self._fine_buf[:] = chunk[-FINE_FFT_SIZE:]
            self._fine_buf_len = FINE_FFT_SIZE
        else:
            self._fine_buf[:-n] = self._fine_buf[n:]
            self._fine_buf[-n:] = chunk
            self._fine_buf_len = min(FINE_FFT_SIZE, self._fine_buf_len + n)
        if self._fine_buf_len < FINE_FFT_SIZE:
            return   # still warming up since the last reset

        now = time.monotonic()
        if now < self._fine_next_tick:
            return
        self._fine_next_tick = now + 1.0 / FINE_FFT_FPS

        spectrum = np.fft.fftshift(np.fft.fft(self._fine_buf * self._fine_window))
        power = (np.abs(spectrum) ** 2).astype(np.float32)
        # Averaged in linear power, not dB — same reasoning as SdrClient's
        # wideband averaging (dB-domain averaging is biased low by log
        # compression). Reset to None (see callers of _fine_buf_len = 0)
        # at the start of each RX cycle, so flicker reduction never blends
        # stale content from before a transmission into the new cycle.
        if self._fine_avg_power is None:
            self._fine_avg_power = power
        else:
            self._fine_avg_power = (self._fine_avg_power * FINE_AVG_DECAY
                                     + power * (1.0 - FINE_AVG_DECAY))
        mag_db = (10.0 * np.log10(
            self._fine_avg_power / (self._fine_fullscale_ref ** 2) + 1e-12)).astype(np.float32)
        frame = {
            "ts": time.time(),
            "center_freq_hz": self.target_freq_hz,
            "span_hz": float(INTERMEDIATE_RATE_HZ),
            "sample_rate_hz": float(INTERMEDIATE_RATE_HZ),
            "kind": "fine",
            "data": mag_db,
        }
        if self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._publish_fine(frame), self._loop)
            except RuntimeError:
                pass   # loop closing/closed during shutdown

    async def _publish_fine(self, frame: dict):
        for cb in self._fine_spectrum_callbacks:
            await cb(frame)

    def _design_eq_band(self, kind: str, freq_hz: float, gain_db: float,
                        q: float = 0.707) -> np.ndarray:
        """One RBJ Audio EQ Cookbook biquad as a [b0,b1,b2,1,a1,a2] SOS row
        (coefficients normalized by a0). kind is 'low_shelf', 'peak', or
        'high_shelf' — standard, widely-used formulas, not a novel design."""
        A = 10.0 ** (gain_db / 40.0)
        w0 = 2 * np.pi * freq_hz / INTERMEDIATE_RATE_HZ
        cos_w0, sin_w0 = np.cos(w0), np.sin(w0)
        if kind == "peak":
            alpha = sin_w0 / (2 * q)
            b0, b1, b2 = 1 + alpha * A, -2 * cos_w0, 1 - alpha * A
            a0, a1, a2 = 1 + alpha / A, -2 * cos_w0, 1 - alpha / A
        else:
            alpha = sin_w0 / 2 * np.sqrt((A + 1 / A) * (1 / q - 1) + 2)
            two_sqrtA_alpha = 2 * np.sqrt(A) * alpha
            if kind == "low_shelf":
                b0 = A * ((A + 1) - (A - 1) * cos_w0 + two_sqrtA_alpha)
                b1 = 2 * A * ((A - 1) - (A + 1) * cos_w0)
                b2 = A * ((A + 1) - (A - 1) * cos_w0 - two_sqrtA_alpha)
                a0 = (A + 1) + (A - 1) * cos_w0 + two_sqrtA_alpha
                a1 = -2 * ((A - 1) + (A + 1) * cos_w0)
                a2 = (A + 1) + (A - 1) * cos_w0 - two_sqrtA_alpha
            else:  # high_shelf
                b0 = A * ((A + 1) + (A - 1) * cos_w0 + two_sqrtA_alpha)
                b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
                b2 = A * ((A + 1) + (A - 1) * cos_w0 - two_sqrtA_alpha)
                a0 = (A + 1) - (A - 1) * cos_w0 + two_sqrtA_alpha
                a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
                a2 = (A + 1) - (A - 1) * cos_w0 - two_sqrtA_alpha
        return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])

    def _design_eq(self) -> np.ndarray:
        """3-band EQ as one SOS chain: low shelf ~300Hz (bass), mid peak
        ~1000Hz, high shelf ~2500Hz (treble) — a conventional voice tone
        stack, not a full parametric EQ."""
        return np.stack([
            self._design_eq_band("low_shelf", 300.0, self.eq_bass_db, q=0.707),
            self._design_eq_band("peak", 1000.0, self.eq_mid_db, q=1.0),
            self._design_eq_band("high_shelf", 2500.0, self.eq_treble_db, q=0.707),
        ])

    def _load_nr_model(self):
        """Loads once, in this background thread (not the asyncio loop) —
        first load can take a couple seconds and may download model
        weights. Failure disables NR for the session rather than crashing
        the audio thread."""
        try:
            logger.info("Loading DeepFilterNet3 model for noise reduction...")
            self._nr_model, self._nr_df_state, _ = _df_init()
            logger.info("DeepFilterNet3 loaded")
        except Exception:
            logger.exception("Failed to load DeepFilterNet3 — noise reduction disabled")
            self._nr_load_failed = True
            self.nr_enabled = False

    def _apply_nr(self, audio: np.ndarray) -> Optional[np.ndarray]:
        """Denoise via DeepFilterNet3. The model resets its internal hidden
        state on every enhance() call (it's built for whole-utterance use,
        not chunk-at-a-time streaming) — calling it on this file's tiny
        ~8ms blocks would reset that context constantly and degrade
        quality. Instead, run it on a rolling NR_WINDOW_S window and emit
        only the newest slice each cycle — the same overlap-discard idea as
        this file's overlap-save FIR filters, just at audio-perceptible
        timescales. Trades latency (~NR_WINDOW_S) for using only the
        stable, public enhance() API instead of undocumented internals.
        Returns None while still buffering toward the next hop."""
        self._nr_in_buf = np.concatenate([self._nr_in_buf, audio])
        self._nr_new_samples += len(audio)
        if self._nr_new_samples < self._nr_hop_samples:
            return None

        if len(self._nr_in_buf) < self._nr_window_samples:
            window = np.concatenate([
                np.zeros(self._nr_window_samples - len(self._nr_in_buf), dtype=np.float32),
                self._nr_in_buf,
            ])
        else:
            window = self._nr_in_buf[-self._nr_window_samples:]
            self._nr_in_buf = window.copy()   # bound memory growth

        hop = min(self._nr_new_samples, self._nr_window_samples)
        self._nr_new_samples = 0

        try:
            window_48k = resample_poly(window, NR_RESAMPLE_RATIO, 1).astype(np.float32)
            with torch.no_grad():
                t = torch.from_numpy(window_48k).unsqueeze(0)
                enhanced_t = _df_enhance(
                    self._nr_model, self._nr_df_state, t,
                    atten_lim_db=self.nr_atten_limit_db)
            enhanced_16k = resample_poly(
                enhanced_t.squeeze(0).numpy(), 1, NR_RESAMPLE_RATIO).astype(np.float32)
        except Exception:
            logger.exception("DeepFilterNet processing error — disabling noise reduction")
            self.nr_enabled = False
            return None

        return enhanced_16k[-hop:] if len(enhanced_16k) >= hop else enhanced_16k

    def _run(self):
        if self.nr_enabled and self._nr_model is None and not self._nr_load_failed:
            self._load_nr_model()
        next_drop_log = time.monotonic() + 5.0
        last_logged_drops = 0
        while not self._stop_event.is_set():
            now_check = time.monotonic()
            if now_check >= next_drop_log:
                if self.dropped_count != last_logged_drops:
                    logger.warning(
                        f"Audio queue drops: {self.dropped_count} total "
                        f"(+{self.dropped_count - last_logged_drops} in last 5s)")
                    last_logged_drops = self.dropped_count
                next_drop_log = now_check + 5.0
            try:
                xi, xq = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            if self.tx_active:
                # SDR Switch disconnects the antenna during TX — this IQ is
                # disconnected-input noise, not a real signal. Feeding it
                # through would perturb the AGC's slow RMS gain and the
                # filters' overlap-save state for the whole transmission.
                # Drop it, and reset state on the falling edge so the next
                # RX batch isn't convolved against stale pre-TX history.
                self._was_tx_active = True
                continue
            if self._was_tx_active:
                self._was_tx_active = False
                self._acc_len = 0
                self._decim_overlap = np.zeros(len(self._decim_filter) - 1, dtype=np.complex64)
                if self._ssb_filter is not None:
                    self._ssb_overlap = np.zeros(len(self._ssb_filter) - 1, dtype=np.complex64)
                if self._eq_sos is not None:
                    self._eq_zi = np.zeros((self._eq_sos.shape[0], 2))
                self._nr_in_buf = np.zeros(0, dtype=np.float32)
                self._nr_new_samples = 0
                self._fine_buf_len = 0
                self._fine_avg_power = None
                continue

            # Write directly into the pre-allocated buffer (slice assignment,
            # O(chunk size)) rather than concatenate-and-grow. A single
            # native chunk can in principle complete more than one batch,
            # so loop rather than assume at most one.
            pos = 0
            n = len(xi)
            while pos < n:
                space = self.batch_samples - self._acc_len
                take = min(space, n - pos)
                self._acc_i[self._acc_len:self._acc_len + take] = xi[pos:pos + take]
                self._acc_q[self._acc_len:self._acc_len + take] = xq[pos:pos + take]
                self._acc_len += take
                pos += take

                if self._acc_len < self.batch_samples:
                    break

                self._acc_len = 0
                if self.target_freq_hz is None or self.rf_center_hz is None:
                    self._sample_counter += self.batch_samples
                    continue

                try:
                    audio_bytes = self._process(self._acc_i, self._acc_q)
                except Exception:
                    logger.exception("Audio demod error")
                    continue
                if audio_bytes is not None and self._loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(self._publish(audio_bytes), self._loop)
                    except RuntimeError:
                        pass   # loop closing/closed during shutdown

    def _process(self, block_i: np.ndarray, block_q: np.ndarray) -> Optional[bytes]:
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

        if self.in_digital_mode:
            self._update_fine_spectrum(intermediate)

        filter_key = (self.bandwidth_hz, self.mode, self.low_cut_hz)
        if self._ssb_filter is None or self._ssb_filter_key != filter_key:
            self._ssb_filter = self._design_ssb_filter(
                self.bandwidth_hz, self.mode, self.low_cut_hz)
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

        # Normalize to [-1, 1] BEFORE further processing — filtered audio is
        # still in raw int16-derived units (peaks in the thousands), not
        # the normalized scale EQ/NR/AGC all assume.
        audio = np.real(filtered).astype(np.float32) / 32768.0

        # EQ — stateful biquad chain (overlap-save's IIR equivalent: sosfilt's
        # zi carries filter history across calls the same way the FIR filters'
        # overlap arrays do). Bypassed entirely when off, not just zeroed —
        # cheaper and avoids any doubt about a "0dB" pass being truly inert.
        if self.eq_enabled:
            eq_key = (self.eq_bass_db, self.eq_mid_db, self.eq_treble_db)
            if self._eq_sos is None or self._eq_key != eq_key:
                self._eq_sos = self._design_eq()
                self._eq_key = eq_key
                self._eq_zi = np.zeros((self._eq_sos.shape[0], 2))
            audio64, self._eq_zi = sosfilt(self._eq_sos, audio.astype(np.float64), zi=self._eq_zi)
            audio = audio64.astype(np.float32)

        # Noise reduction — buffered, see _apply_nr. Returns None while it's
        # still accumulating toward its next output hop; the caller (_run)
        # already treats a None return as "nothing to publish this cycle".
        if self.nr_enabled and self._nr_model is not None:
            audio = self._apply_nr(audio)
            if audio is None:
                return None

        # AGC: single time constant on RMS (not peak — less jittery batch to
        # batch), same rate both directions. Several rounds of asymmetric
        # fast-attack/slow-decay tuning each fixed one failure mode while
        # introducing another (pumping, on/off, clipping on transients) —
        # without being able to listen directly, a slower, symmetric,
        # predictable design plus a hard safety limiter is more trustworthy
        # than continuing to chase attack/decay parameters.
        # AGC leveling and overall volume are two separate stages: the AGC
        # (when not OFF) levels toward a modest, safe target_rms so peaks
        # don't ride right up against the limiter; manual_gain is the
        # user-facing RX Volume control applied on top, independent of
        # whatever the AGC decides — so "too quiet" has a direct knob
        # instead of needing the auto-leveling target chased over and over.
        # Time-based (not a fixed per-call fraction) so behavior stays
        # correct regardless of block size — matters once NR is enabled,
        # since NR emits larger, less-frequent blocks than AGC was
        # originally tuned against.
        rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-6
        if self.agc_mode == "off":
            gain = 1.0   # pure manual — same as riding a real radio's AF gain knob with AGC off
        else:
            block_duration_s = len(audio) / INTERMEDIATE_RATE_HZ
            tau = AGC_TAU_FAST_S if self.agc_mode == "fast" else AGC_TAU_SLOW_S
            alpha = 1.0 - np.exp(-block_duration_s / tau)
            target_rms = 0.15
            desired_gain = target_rms / rms
            self.agc_gain += (desired_gain - self.agc_gain) * alpha
            self.agc_gain = float(np.clip(self.agc_gain, 0.1, 6.0))
            gain = self.agc_gain
        audio = np.clip(audio * gain * self.manual_gain, -0.95, 0.95)   # hard safety backstop, not the primary leveler

        pcm16 = (audio * 32767).astype(np.int16)
        return pcm16.tobytes()

    async def _publish(self, audio_bytes: bytes):
        for cb in self._audio_callbacks:
            await cb(audio_bytes)
