"""RX0 — a manual, tunable combine of RX1's and RX2's baseband (2026-09-12).

Built to test whether antenna diversity can pull a signal out of noise now
that RX1/RX2 have been shown to be genuinely phase-locked (see project
memory / DESIGN.md §10): RX0 = RX1 - z*RX2, where z = gain*e^(j*phase) is a
single complex weight the operator dials in by ear. Terry's own framing:
"I don't want an adaptive canceller, I want one I can tune." This module
is manual-only — the one automated piece (sample-delay calibration, via
calibrate()) runs once when asked, not continuously, and only corrects a
fixed timing offset between RX1's and RX2's independent processing
threads. It never tracks a moving target on its own.

Why sample-delay calibration matters: a real null across an SSB-width
passband needs the two channels' samples aligned to well under one sample
period — even a couple of samples' misalignment at 16kHz turns into tens
of degrees of phase smear across a 2.8kHz-wide channel (a time delay is a
phase ramp across frequency). RX1 and RX2 run on independent threads with
no built-in sample-level sync, and queue drops under load (confirmed to
happen in this app — see project memory) can silently shift one channel's
effective sample count relative to the other's. Cross-correlating a short
window of the two channels' actual baseband finds the true integer delay
empirically, robust to exactly that kind of silent desync, rather than
trusting internal sample-counter bookkeeping on either side.

Fresh, independent code rather than reusing AudioDemodulator directly —
same reasoning as Panadapter2 being fresh JS rather than a refactor of
Panadapter: isolates this experimental path from the live, working per-
channel RX audio, which nothing here should ever be able to disturb.
"""
import asyncio
import logging
import queue
import threading
import time
from typing import Optional

import numpy as np
from scipy.signal import correlate

logger = logging.getLogger(__name__)

INTERMEDIATE_RATE_HZ = 16_000   # matches audio_demod.INTERMEDIATE_RATE_HZ
_STAGE1_NUM_TAPS = 101           # see audio_demod._STAGE1_NUM_TAPS for why
_SSB_NUM_TAPS = 161              # matches audio_demod._design_ssb_filter

# How much decimated (16kHz) baseband each channel keeps on hand for
# calibrate() to cross-correlate against. Was 1s on the assumption that
# any 1s window is as good as any other — wrong in practice (Terry
# 2026-09-12, live splitter test): calibrating during WWV's tick/counting
# segment gave -750..-1200 repeatably, while calibrating during a voice or
# tone segment gave a visibly different, still-spread range. WWV's
# modulation content cycles on a ~1-minute period (voice, tone bursts,
# tick pulses, BCD subcarrier, silence) and a 1s window is short enough
# that which segment it happens to land on measurably biases the
# correlation. Widened to span multiple segment types per calibration
# call instead of being at the mercy of whichever second gets captured.
_CALIB_WINDOW_S = 8.0
_CALIB_WINDOW_N = int(INTERMEDIATE_RATE_HZ * _CALIB_WINDOW_S)
# Was 200 (+-12.5ms) on the "generous vs. reality" assumption that two
# threads reading off one shared ADC clock could only drift by a few
# samples. Wrong in practice (Terry 2026-09-12, live splitter test):
# repeated calibrate() calls pegged at the search boundary (-200, then a
# run of +178..+200) instead of converging on a stable interior value —
# the classic signature of the true best-alignment point sitting outside
# the search window, not of a genuinely unstable delay. Widened 8x to give
# real headroom; _LATENCY_N (below) scales with it, so this also raises
# RX0's output latency from ~12.5ms to ~100ms — irrelevant for a
# diagnostic/DX listening feature, not real-time QSO audio.
_MAX_CALIB_LAG_N = 1600           # +-100ms search range

# RX0's own spectrum (Terry 2026-09-12: "I can't detect any differences...
# can I please get a spectrum I can look at of RX0?" — a level meter and
# audio alone weren't legible enough to see the null happening). Same
# rolling-window-FFT approach as audio_demod's own FINE_FFT (matching
# constants, duplicated not imported — see this file's own module
# docstring on why everything here is independent code), run on the
# COMBINED complex baseband (dec_a_aligned - z*dec_b_aligned) before the
# SSB filter narrows it to one passband — this is deliberately NOT the
# wideband multi-bin spectrum problem flagged earlier (that one needed
# cross-channel sample alignment across a whole band with no single
# common time reference); this FFT runs entirely on RX0's own, already-
# combined, single coherent stream, so it has no alignment problem of
# its own to solve.
FINE_FFT_SIZE = 4096
FINE_FFT_FPS = 15.0
FINE_AVG_FRAMES = 4.0
FINE_AVG_DECAY = max(0.0, (FINE_AVG_FRAMES - 1.0) / (FINE_AVG_FRAMES + 1.0))
# Fixed output latency, in decimated samples — RX0 always plays content
# this far behind both channels' own most recent sample, so sample_delay
# always has real (already-arrived) history to read from in EITHER
# direction (see the history-buffer comment in Combiner.__init__).
# 12.5ms is inaudible as latency on a receive-only monitoring channel.
_LATENCY_N = _MAX_CALIB_LAG_N


def _choose_decim_stages(total_factor: int) -> tuple:
    """Copy of audio_demod._choose_decim_stages — see there for why the
    split exists. Duplicated, not imported, to keep this module's own
    correctness independently auditable rather than coupled to the live
    per-channel demod path."""
    for coarse in (25, 20, 16, 10, 8, 5, 4, 2):
        if total_factor % coarse == 0 and total_factor // coarse > 1:
            return coarse, total_factor // coarse
    return 1, total_factor


def _design_decim_filter(input_rate_hz: float, output_rate_hz: float, num_taps: int) -> np.ndarray:
    """Copy of audio_demod.AudioDemodulator._design_decim_filter (that
    method never actually used self) — anti-alias lowpass for decimation."""
    cutoff_hz = output_rate_hz / 2.4
    n = np.arange(num_taps) - (num_taps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / input_rate_hz * n)
    h *= np.hamming(num_taps)
    h /= np.sum(h)
    return h.astype(np.complex64)


def _design_ssb_filter(bandwidth_hz: float, mode: str, low_cut_hz: float) -> np.ndarray:
    """Copy of audio_demod.AudioDemodulator._design_ssb_filter — complex
    FIR passing only the desired sideband. See there for the full
    derivation; identical math, kept independent here."""
    num_taps = _SSB_NUM_TAPS
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


class _ChannelStage:
    """Downmix + 2-stage decimation for one channel's raw IQ — everything
    Combiner needs from each side before the actual combine step. Kept as
    its own small stateful object (own overlap-save history) so channel A
    and channel B's stages never share mutable state by accident."""

    def __init__(self, input_rate_hz: float):
        self.input_rate_hz = input_rate_hz
        self.decim_factor = round(input_rate_hz / INTERMEDIATE_RATE_HZ)
        self._coarse_factor, self._fine_factor = _choose_decim_stages(self.decim_factor)
        self._coarse_rate_hz = input_rate_hz / self._coarse_factor
        self._filter_coarse = (
            _design_decim_filter(input_rate_hz, self._coarse_rate_hz, _STAGE1_NUM_TAPS)
            if self._coarse_factor > 1 else None)
        self._filter_fine = _design_decim_filter(
            self._coarse_rate_hz, INTERMEDIATE_RATE_HZ, 401)
        self._overlap_coarse = (
            np.zeros(len(self._filter_coarse) - 1, dtype=np.complex64)
            if self._filter_coarse is not None else None)
        self._overlap_fine = np.zeros(len(self._filter_fine) - 1, dtype=np.complex64)
        self._sample_counter = 0

    def reset(self):
        if self._overlap_coarse is not None:
            self._overlap_coarse[:] = 0
        self._overlap_fine[:] = 0

    def process(self, block_i: np.ndarray, block_q: np.ndarray,
                target_freq_hz: float, rf_center_hz: float) -> np.ndarray:
        """Raw int16 IQ in, decimated complex baseband (16kHz) out."""
        n = len(block_i)
        offset_hz = target_freq_hz - rf_center_hz
        t = self._sample_counter + np.arange(n)
        self._sample_counter += n
        mix = np.exp(-1j * 2 * np.pi * offset_hz / self.input_rate_hz * t).astype(np.complex64)
        baseband = (block_i.astype(np.float32) + 1j * block_q.astype(np.float32)) * mix

        if self._filter_coarse is not None:
            ext = np.concatenate([self._overlap_coarse, baseband])
            coarse = np.convolve(ext, self._filter_coarse, mode="valid")
            self._overlap_coarse = ext[-(len(self._filter_coarse) - 1):]
            coarse_out = coarse[::self._coarse_factor]
        else:
            coarse_out = baseband

        ext = np.concatenate([self._overlap_fine, coarse_out])
        fine = np.convolve(ext, self._filter_fine, mode="valid")
        self._overlap_fine = ext[-(len(self._filter_fine) - 1):]
        return fine[::self._fine_factor]


class Combiner:
    """Owns its own thread; fed raw IQ from both channels via feed_a/feed_b
    (called from SdrClient's native callback thread, same pattern as
    AudioDemodulator.feed). Produces RX0 PCM audio via on_audio callbacks,
    same shape as AudioDemodulator's own _publish."""

    def __init__(self, input_rate_hz: float = 2_000_000.0, batch_samples: int = 16384):
        self.input_rate_hz = input_rate_hz
        # Accumulate raw IQ into batch_samples-sized chunks before
        # downmix/decimate — matching AudioDemodulator's own pattern, and
        # for the same reason: native IQ callbacks arrive in very small,
        # very frequent pieces (confirmed live: ~5000+/s, only a few
        # decimated samples each). Processing each individually meant
        # calling the O(hist_len) history-roll thousands of times/sec,
        # which couldn't keep up — Channel A's queue was chronically full
        # and dropping most of its data (confirmed via a live diagnostic
        # heartbeat: tens of thousands of drops/sec) before this existed.
        decim_factor = round(input_rate_hz / INTERMEDIATE_RATE_HZ)
        self.batch_samples = (batch_samples // decim_factor) * decim_factor
        self._acc_i_a = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_q_a = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_len_a = 0
        self._acc_i_b = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_q_b = np.empty(self.batch_samples, dtype=np.int16)
        self._acc_len_b = 0

        self.enabled = False
        self.tx_active = False
        self._was_tx_active = False
        # Live RX0 level (dBFS, RMS of the last published frame) — so the
        # Diversity page has something to actually watch move while
        # dragging gain/phase, instead of needing raw audio measurements
        # to see whether a null is happening (Terry 2026-09-12).
        self.level_dbfs: Optional[float] = None

        # Manual weight z = gain*e^(j*phase_deg), applied to channel B
        # before subtracting from channel A. Pure operator dial — see
        # module docstring.
        self.gain = 1.0
        self.phase_deg = 0.0
        # Coarse alignment correction, in decimated (16kHz) samples,
        # found by calibrate() — the sign convention is whatever makes
        # its own formula correct (verified against a synthetic ground-
        # truth signal, both directions, before ever running on real
        # audio), not worth restating narratively here since it's easy
        # to get backwards by intuition alone.
        self.sample_delay = 0
        # Split A/B (Terry 2026-09-12: chasing whether the two channels
        # drop asymmetrically, which would explain sample_delay drifting
        # between calibrate() calls even after the window/filtering fixes —
        # a single combined counter can't show that).
        self.dropped_count_a = 0
        self.dropped_count_b = 0

        self._stage_a = _ChannelStage(input_rate_hz)
        self._stage_b = _ChannelStage(input_rate_hz)
        self._ssb_filter: Optional[np.ndarray] = None
        self._ssb_filter_key = None
        self._ssb_overlap = None

        # RX0's own spectrum — see FINE_FFT_SIZE's own comment.
        self._fine_window = np.hanning(FINE_FFT_SIZE).astype(np.float32)
        self._fine_fullscale_ref = 32767.0 * float(np.sum(self._fine_window))
        self._fine_buf = np.zeros(FINE_FFT_SIZE, dtype=np.complex64)
        self._fine_buf_len = 0
        self._fine_avg_power: Optional[np.ndarray] = None
        self._fine_next_tick = 0.0
        self._spectrum_callbacks = []

        # Rolling history for BOTH channels, symmetric — see _run()'s own
        # comment on why this has to be symmetric (a first version only
        # ever shifted channel B, which silently could not correct for B
        # running *behind* A, only ahead — caught by a synthetic test
        # before this ever reached real audio, not live). Every combined
        # output sample is drawn from _LATENCY_N samples behind BOTH
        # channels' own most recent sample, so sample_delay can shift
        # either channel's read point by up to _MAX_CALIB_LAG_N in either
        # direction and always land on real (already-arrived) history,
        # never on data that hasn't happened yet.
        self._hist_len = _CALIB_WINDOW_N + 2 * _MAX_CALIB_LAG_N + 4096
        self._hist_a = np.zeros(self._hist_len, dtype=np.complex64)
        self._hist_a_len = 0
        self._hist_b = np.zeros(self._hist_len, dtype=np.complex64)
        self._hist_b_len = 0

        self._q_a: "queue.Queue" = queue.Queue(maxsize=64)
        self._q_b: "queue.Queue" = queue.Queue(maxsize=64)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._audio_callbacks = []

        # What to tune/filter for — set by SdrClient from RX1's own
        # AudioDemodulator, since RX0 only makes sense when both channels
        # are looking at the same target. Read live each batch, not
        # copied once, so RX0 tracks Link-driven retunes automatically.
        self.target_freq_hz: Optional[float] = None
        self.mode = "USB"
        self.bandwidth_hz = 2800.0
        self.low_cut_hz = 300.0
        self.rf_center_hz_a: Optional[float] = None
        self.rf_center_hz_b: Optional[float] = None

    def on_audio(self, cb):
        if cb not in self._audio_callbacks:
            self._audio_callbacks.append(cb)

    def off_audio(self, cb):
        if cb in self._audio_callbacks:
            self._audio_callbacks.remove(cb)

    def on_spectrum(self, cb):
        if cb not in self._spectrum_callbacks:
            self._spectrum_callbacks.append(cb)

    def off_spectrum(self, cb):
        if cb in self._spectrum_callbacks:
            self._spectrum_callbacks.remove(cb)

    def feed_a(self, xi: np.ndarray, xq: np.ndarray):
        if not self.enabled:
            return
        try:
            self._q_a.put_nowait((xi, xq))
        except queue.Full:
            self.dropped_count_a += 1
            try:
                self._q_a.get_nowait()
            except queue.Empty:
                pass

    def feed_b(self, xi: np.ndarray, xq: np.ndarray):
        if not self.enabled:
            return
        try:
            self._q_b.put_nowait((xi, xq))
        except queue.Full:
            self.dropped_count_b += 1
            try:
                self._q_b.get_nowait()
            except queue.Empty:
                pass

    def gate_tx(self):
        """See AudioDemodulator.gate_tx — same reasoning, own queues."""
        self.tx_active = True
        for q in (self._q_a, self._q_b):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def start(self, loop):
        self._loop = loop
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="Combiner")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def calibrate(self) -> Optional[int]:
        """One-shot: cross-correlate ~1s of each channel's own decimated
        baseband (both drawn from _LATENCY_N behind their own tail, same
        as _run()'s real combine point — see the history-buffer comment
        in __init__) to find the actual integer sample delay between
        them, and store it. Returns the found delay (samples at 16kHz),
        or None if there isn't enough history yet. Not adaptive — call
        again explicitly (e.g. a "Calibrate" button) if timing ever needs
        re-checking, such as after a reconnect.

        Sign convention verified against a synthetic ground-truth signal
        (brute-force search for the exact reproducing offset vs. this
        formula, both directions and near both edges of the search range)
        before ever running against real audio — see conversation/commit
        history for that derivation; scipy's correlate() lag convention
        is easy to get backwards and this was gotten wrong on the first
        pass."""
        need = _CALIB_WINDOW_N + _LATENCY_N + _MAX_CALIB_LAG_N
        if self._hist_a_len < need or self._hist_b_len < need:
            return None
        a_end = self._hist_a_len - _LATENCY_N
        a_start = a_end - _CALIB_WINDOW_N
        a = self._hist_a[a_start:a_end]

        b_center_end = self._hist_b_len - _LATENCY_N
        b_start = b_center_end - _CALIB_WINDOW_N - _MAX_CALIB_LAG_N
        b = self._hist_b[b_start:b_start + _CALIB_WINDOW_N + 2 * _MAX_CALIB_LAG_N]

        # Bandpass both windows to the current passband before correlating
        # (Terry 2026-09-12: repeated calibrate() calls wandered wildly —
        # -1600..+1591 — and looked "very similar" whether or not an
        # antenna was even connected). Root cause: the raw 16kHz-wide
        # baseband is mostly each channel's own independent receiver
        # noise, which fills the whole band and swamps the narrow slice
        # that's actually coherent between channels — argmax was mostly
        # just finding the loudest noise-correlation peak, different every
        # run. Filtering first concentrates the correlation on the real
        # coherent content instead.
        ssb_filter = _design_ssb_filter(self.bandwidth_hz, self.mode, self.low_cut_hz)
        a = np.convolve(a, ssb_filter, mode="valid")
        b = np.convolve(b, ssb_filter, mode="valid")

        corr = correlate(a, b, mode="valid", method="fft")
        best = int(np.argmax(np.abs(corr)))
        delay = _MAX_CALIB_LAG_N - best
        self.sample_delay = delay
        logger.info(f"Combiner calibrated: sample_delay={delay} "
                    f"({delay / INTERMEDIATE_RATE_HZ * 1000:.2f}ms)")
        return delay

    def _accumulate(self, acc_i, acc_q, acc_len: int, xi: np.ndarray, xq: np.ndarray):
        """Feed raw IQ into a batch_samples-sized accumulator, in place,
        possibly completing (and needing to start another) more than one
        batch from a single call — same shape as AudioDemodulator's own
        accumulation loop. Returns (new_acc_len, list_of_completed_batches),
        where each completed batch is an (i, q) pair of length batch_samples."""
        completed = []
        pos = 0
        n = len(xi)
        while pos < n:
            space = self.batch_samples - acc_len
            take = min(space, n - pos)
            acc_i[acc_len:acc_len + take] = xi[pos:pos + take]
            acc_q[acc_len:acc_len + take] = xq[pos:pos + take]
            acc_len += take
            pos += take
            if acc_len < self.batch_samples:
                break
            completed.append((acc_i[:self.batch_samples].copy(), acc_q[:self.batch_samples].copy()))
            acc_len = 0
        return acc_len, completed

    def _roll_hist(self, hist: np.ndarray, hist_len_attr: str, dec: np.ndarray):
        n = len(dec)
        if n == 0:
            return
        cur_len = getattr(self, hist_len_attr)
        if n >= self._hist_len:
            hist[:] = dec[-self._hist_len:]
            setattr(self, hist_len_attr, self._hist_len)
        else:
            hist[:-n] = hist[n:]
            hist[-n:] = dec
            setattr(self, hist_len_attr, min(self._hist_len, cur_len + n))

    def _run(self):
        # Channel A drives output cadence (one combine attempt per A
        # chunk); channel B's queue is fully drained every pass
        # regardless of whether A had anything this iteration. A first
        # version required BOTH queues to have data on the very same
        # iteration or discarded whatever it had already dequeued from
        # A — the two channels' native callbacks don't actually arrive in
        # lockstep pairs, so that silently threw away most of A's data
        # whenever B's queue was momentarily empty (confirmed live: real
        # throughput was a small, unstable fraction of the expected
        # rate). Decoupling the two fixed it — each channel's own history
        # only needs to be reasonably current when a combine is
        # attempted, not simultaneously freshly arrived.
        next_heartbeat = time.monotonic() + 2.0
        frames_published = 0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                # debug not info (Terry 2026-09-12: cluttering the log) —
                # still there for --log-level debug if this diagnostic is
                # ever needed again.
                logger.debug(
                    f"Combiner heartbeat: enabled={self.enabled} ready="
                    f"{self.target_freq_hz is not None and self.rf_center_hz_a is not None and self.rf_center_hz_b is not None} "
                    f"qsize_a={self._q_a.qsize()} qsize_b={self._q_b.qsize()} "
                    f"hist_a_len={self._hist_a_len} hist_b_len={self._hist_b_len} "
                    f"frames_published={frames_published} "
                    f"dropped_a={self.dropped_count_a} dropped_b={self.dropped_count_b}")
                next_heartbeat = now + 2.0

            try:
                target = self.target_freq_hz
                ready = target is not None and self.rf_center_hz_a is not None and self.rf_center_hz_b is not None

                drained_b = False
                while True:
                    try:
                        i_b, q_b = self._q_b.get_nowait()
                    except queue.Empty:
                        break
                    drained_b = True
                    if ready and not self.tx_active:
                        self._acc_len_b, completed = self._accumulate(
                            self._acc_i_b, self._acc_q_b, self._acc_len_b, i_b, q_b)
                        for bi, bq in completed:
                            dec_b = self._stage_b.process(bi, bq, target, self.rf_center_hz_b)
                            self._roll_hist(self._hist_b, "_hist_b_len", dec_b)

                try:
                    i_a, q_a = self._q_a.get(timeout=0.05)
                except queue.Empty:
                    if not drained_b:
                        time.sleep(0.005)   # avoid a hot spin when both queues are empty
                    continue

                if self.tx_active:
                    self._was_tx_active = True
                    continue
                if self._was_tx_active:
                    self._was_tx_active = False
                    self._stage_a.reset()
                    self._stage_b.reset()
                    self._ssb_overlap = None
                    self._hist_a_len = 0
                    self._hist_b_len = 0
                    self._fine_buf_len = 0
                    self._fine_avg_power = None

                if not ready:
                    continue

                self._acc_len_a, completed_a = self._accumulate(
                    self._acc_i_a, self._acc_q_a, self._acc_len_a, i_a, q_a)

                for ai, aq in completed_a:
                    dec_a = self._stage_a.process(ai, aq, target, self.rf_center_hz_a)
                    self._roll_hist(self._hist_a, "_hist_a_len", dec_a)

                    # Combine point: _LATENCY_N behind both channels' own
                    # tail, so sample_delay can shift B's read point
                    # earlier OR later by up to _MAX_CALIB_LAG_N and
                    # always land on real history (never on samples that
                    # haven't arrived yet) — see __init__.
                    n_out = len(dec_a)
                    need = n_out + _LATENCY_N + _MAX_CALIB_LAG_N
                    if self._hist_a_len < need or self._hist_b_len < need:
                        continue

                    a_end = self._hist_a_len - _LATENCY_N
                    a_start = a_end - n_out
                    dec_a_aligned = self._hist_a[a_start:a_end]

                    b_end = self._hist_b_len - _LATENCY_N + self.sample_delay
                    b_start = b_end - n_out
                    if b_start < 0 or b_end > self._hist_b_len:
                        continue   # sample_delay temporarily out of range for this frame
                    dec_b_aligned = self._hist_b[b_start:b_end]

                    filter_key = (self.bandwidth_hz, self.mode, self.low_cut_hz)
                    if self._ssb_filter is None or self._ssb_filter_key != filter_key:
                        self._ssb_filter = _design_ssb_filter(self.bandwidth_hz, self.mode, self.low_cut_hz)
                        self._ssb_filter_key = filter_key
                        self._ssb_overlap = np.zeros(len(self._ssb_filter) - 1, dtype=np.complex64)

                    z = self.gain * np.exp(1j * np.radians(self.phase_deg))
                    combined = dec_a_aligned - z * dec_b_aligned
                    self._update_fine_spectrum(combined)

                    ext = np.concatenate([self._ssb_overlap, combined])
                    filtered = np.convolve(ext, self._ssb_filter, mode="valid")
                    self._ssb_overlap = ext[-(len(self._ssb_filter) - 1):]

                    audio = np.real(filtered).astype(np.float32) / 32768.0
                    audio = np.clip(audio, -0.95, 0.95)
                    rms = float(np.sqrt(np.mean(audio ** 2)))
                    self.level_dbfs = 20.0 * float(np.log10(rms + 1e-9))
                    pcm16 = (audio * 32767).astype(np.int16)
                    audio_bytes = pcm16.tobytes()
                    frames_published += 1

                    if self._loop is not None:
                        try:
                            asyncio.run_coroutine_threadsafe(self._publish(audio_bytes), self._loop)
                        except RuntimeError:
                            pass
            except Exception:
                logger.exception("Combiner._run iteration error")
                time.sleep(0.1)

    async def _publish(self, audio_bytes: bytes):
        for cb in self._audio_callbacks:
            await cb(audio_bytes)

    def _update_fine_spectrum(self, chunk: np.ndarray):
        """Rolling-window FFT on RX0's own combined complex baseband —
        see FINE_FFT_SIZE's own comment for why this has no cross-channel
        alignment problem of its own. Copy of AudioDemodulator's own
        _update_fine_spectrum, same reasoning throughout this file for
        why it's duplicated rather than shared."""
        n = len(chunk)
        if n >= FINE_FFT_SIZE:
            self._fine_buf[:] = chunk[-FINE_FFT_SIZE:]
            self._fine_buf_len = FINE_FFT_SIZE
        else:
            self._fine_buf[:-n] = self._fine_buf[n:]
            self._fine_buf[-n:] = chunk
            self._fine_buf_len = min(FINE_FFT_SIZE, self._fine_buf_len + n)
        if self._fine_buf_len < FINE_FFT_SIZE:
            return

        now = time.monotonic()
        if now < self._fine_next_tick:
            return
        self._fine_next_tick = now + 1.0 / FINE_FFT_FPS

        spectrum = np.fft.fftshift(np.fft.fft(self._fine_buf * self._fine_window))
        power = (np.abs(spectrum) ** 2).astype(np.float32)
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
                asyncio.run_coroutine_threadsafe(self._publish_spectrum(frame), self._loop)
            except RuntimeError:
                pass

    async def _publish_spectrum(self, frame: dict):
        for cb in self._spectrum_callbacks:
            await cb(frame)
