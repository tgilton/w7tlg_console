"""Audio-domain NOISE SUB stage: per-bin STFT gain on RX1's 16 kHz baseband.

Runs on the complex demod baseband, before the SSB filter and the audio AGC, so
the AGC does not pump on the removed lines. Only flagged bins are attenuated;
every other bin has gain 1, and the sub-threshold floor is never touched.

Structure
  * sqrt-Hann analysis and synthesis, window N, hop H, overlap-add.
  * Output sample t is emitted at input index t + N. That is the earliest point
    at which every frame covering t has been added, so the latency is exactly N
    samples (64 ms at N=1024) and constant, for every sample and every gain.
  * With all gains 1, the output is the input delayed by N, to rounding error.
  * Gains come from a mask provider: a RF-bin mask and power ratios from the
    NOISE SUB processor. Each RF bin maps to a baseband frequency
    f = rf - target, which is sideband-independent. The sideband selects the
    audible half afterwards, so LSB needs no sign flip. Flagged runs are widened
    by the window's main-lobe half-width, rounded up to whole mask bins.
  * Gain per flagged bin: G = sqrt(max(1 - k*E2/P1, beta)). beta is a power
    floor, -20 dB by default. G is smoothed across frequency (3 bins) and in time
    (first-order, 200 ms attack and release).

tx_active is a guard of the stage's own: while it is set, process() takes nothing in,
returns nothing and changes no state. The demodulator sets it on TX and the server
clears it on the falling edge; reset() and prime() restart the stage's state.
"""

import math
from typing import Callable, Optional

import numpy as np

DEFAULT_N = 1024
DEFAULT_HOP = 256
DEFAULT_FS = 16000.0
BETA_DB_DEFAULT = -20.0
BETA_DB_MIN, BETA_DB_MAX = -40.0, -6.0
TAU_S_DEFAULT = 0.2
MAIN_LOBE_HALF_BINS = 2          # Hann main-lobe half-width, in STFT bins (2 x 15.6 Hz)
FREQ_SMOOTH_BINS = 3


def sqrt_hann(n: int) -> np.ndarray:
    return np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n))


class MaskProvider:
    """Holds the latest mask and power ratios from NOISE SUB, as RF-bin arrays.

    publish() replaces them as a whole (one tuple assignment, GIL-atomic), so the
    stage reads a consistent set. version changes on every publish, and the stage
    rebuilds its bin mapping only when it changes."""

    def __init__(self):
        self.version = 0
        self._state: Optional[dict] = None

    def publish(self, mask: np.ndarray, power_ratio: np.ndarray, center_hz: float,
                span_hz: float):
        self.version += 1
        self._state = {"mask": np.asarray(mask, dtype=bool),
                       "r": np.asarray(power_ratio, dtype=np.float64),
                       "center_hz": float(center_hz), "span_hz": float(span_hz),
                       "version": self.version}

    def clear(self):
        self.version += 1
        self._state = None

    def get(self) -> Optional[dict]:
        return self._state


class StftGainStage:
    def __init__(self, fs: float = DEFAULT_FS, n: int = DEFAULT_N, hop: int = DEFAULT_HOP,
                 provider: Optional[MaskProvider] = None, target_hz: Callable[[], float] = lambda: 0.0,
                 beta_db: float = BETA_DB_DEFAULT, tau_s: float = TAU_S_DEFAULT):
        if n % hop:
            raise ValueError("hop must divide the window")
        self.fs = float(fs)
        self.n = int(n)
        self.hop = int(hop)
        self.provider = provider
        self.target_hz = target_hz
        self.enabled = False                       # the AUDIO switch: off means G = 1
        self.beta_db = float(beta_db)
        self.tau_s = float(tau_s)
        self.tx_active = False
        self._win = sqrt_hann(self.n)
        # Overlap-add normalisation: the sum of periodic Hann over shifts of hop is
        # constant (2 for hop = n/4); sqrt-Hann analysis x synthesis is Hann.
        hann = self._win ** 2
        self._ola_norm = float(sum(hann[(-k * self.hop) % self.n] for k in range(self.n // self.hop)))
        self._alpha = 1.0 - math.exp(-self.hop / (self.fs * self.tau_s))
        self._reset_state()
        self._map_key = None
        self._map_idx = None          # per baseband bin: RF-bin index, or -1 for none
        self._map_r = None            # per baseband bin: sqrt power ratio after widening
        self.latency_samples = self.n

    # ---------------------------------------------------------------- state
    def _reset_state(self):
        self._inbuf = np.zeros(0, dtype=np.complex128)
        self._inbase = 0
        self._acc = np.zeros(0, dtype=np.complex128)
        self._accbase = 0
        self._n_in = 0
        self._next_frame = 0
        self._emitted = 0
        self._g = np.ones(self.n, dtype=np.float64)     # time-smoothed gain per STFT bin

    def reset(self):
        """TX gate and mode changes: forget the buffers and the gain state. The next
        output is the start of a fresh stream, delayed by the constant latency."""
        self._reset_state()

    def prime(self, history: np.ndarray):
        """Reset, then run 2N samples of history through and discard what comes out.
        After this, every process(x) returns exactly len(x) samples: x delayed by N,
        with the start-up fill already behind it."""
        self.reset()
        h = np.asarray(history, dtype=np.complex128)
        if len(h) < 2 * self.n:
            h = np.concatenate([np.zeros(2 * self.n - len(h), dtype=np.complex128), h])
        self._run(h[-2 * self.n:])     # the restart itself is not subject to the TX guard

    # ------------------------------------------------------------- mapping
    def _rebuild_mapping(self, state: dict, target: float):
        """RF-bin index and widened sqrt gain for every baseband bin."""
        n, fs = self.n, self.fs
        j = np.arange(n)
        f_bb = np.where(j < n // 2, j, j - n) * fs / n          # baseband Hz per STFT bin
        rf_bin_hz = state["span_hz"] / len(state["mask"])
        rf_lo = state["center_hz"] - state["span_hz"] / 2.0
        idx = np.floor((target + f_bb - rf_lo) / rf_bin_hz).astype(np.int64)
        valid = (idx >= 0) & (idx < len(state["mask"]))
        idx = np.where(valid, idx, -1)

        # Widen flagged runs by the main-lobe half-width (in RF mask bins), taking the
        # smallest power ratio in the widened neighbourhood (the strongest attenuation).
        mask = state["mask"]
        r_prime = np.where(mask, state["r"], 1.0)
        half = MAIN_LOBE_HALF_BINS
        r_wide = r_prime.copy()
        for s in range(1, half + 1):
            r_wide[s:] = np.minimum(r_wide[s:], r_prime[:-s])
            r_wide[:-s] = np.minimum(r_wide[:-s], r_prime[s:])
        beta_lin = 10.0 ** (self.beta_db / 10.0)
        r_wide = np.maximum(r_wide, beta_lin)
        self._map_idx = idx
        self._map_r = np.where(valid, np.sqrt(np.clip(r_wide[np.clip(idx, 0, None)], 0.0, 1.0)), 1.0)
        self._map_key = (state["version"], round(target, 3))

    def _target_gain(self) -> np.ndarray:
        if not self.enabled or self.provider is None:
            return np.ones(self.n)
        state = self.provider.get()
        if state is None:
            return np.ones(self.n)
        target = float(self.target_hz())
        if self._map_key != (state["version"], round(target, 3)):
            self._rebuild_mapping(state, target)
        g = self._map_r
        # 3-bin smoother across frequency (circular, on the STFT bins)
        g = (np.roll(g, 1) + g + np.roll(g, -1)) / 3.0
        return g

    # ------------------------------------------------------------ process
    def process(self, x: np.ndarray) -> np.ndarray:
        """Complex baseband in, complex baseband out. The output is the input delayed
        by latency_samples; the first calls return fewer samples while the delay fills,
        and the total count stays input minus latency."""
        if self.tx_active:
            # TX guard: take nothing in, put nothing out, and leave every buffer, the
            # frame counter and the smoothed gains exactly as they are.
            return np.zeros(0, dtype=np.complex128)
        return self._run(x)

    def _run(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.complex128)
        self._inbuf = np.concatenate([self._inbuf, x])
        self._n_in += len(x)
        n, hop = self.n, self.hop
        while self._next_frame * hop + n <= self._n_in:
            s = self._next_frame * hop
            frame = self._inbuf[s - self._inbase: s - self._inbase + n]
            spec = np.fft.fft(frame * self._win)
            target = self._target_gain()
            self._g += self._alpha * (target - self._g)
            y = np.fft.ifft(spec * self._g) * self._win / self._ola_norm
            self._add_acc(s, y)
            self._next_frame += 1
        # Samples t < next_frame*hop are final; t is emitted once input t+N has arrived.
        upto = min(self._next_frame * hop, self._n_in - n)
        out = np.zeros(0, dtype=np.complex128)
        if upto > self._emitted:
            out = self._acc[self._emitted - self._accbase: upto - self._accbase].copy()
            self._emitted = upto
        self._trim()
        return out

    def _add_acc(self, s: int, y: np.ndarray):
        n = self.n
        need_end = s + n
        if need_end - self._accbase > len(self._acc):
            self._acc = np.concatenate([self._acc, np.zeros(need_end - self._accbase - len(self._acc),
                                                            dtype=np.complex128)])
        self._acc[s - self._accbase: s - self._accbase + n] += y

    def _trim(self):
        # keep what the next frame and the next emission still need
        keep_in_from = self._next_frame * self.hop
        drop = keep_in_from - self._inbase
        if drop > 0:
            self._inbuf = self._inbuf[drop:]
            self._inbase += drop
        drop_acc = self._emitted - self._accbase
        if drop_acc > 0:
            self._acc = self._acc[drop_acc:]
            self._accbase += drop_acc


def fade_weights(pos: int, n: int, length: int) -> np.ndarray:
    """Raised-cosine weights 0 -> 1 for samples pos .. pos+n-1 of a fade of `length`."""
    k = np.minimum(pos + np.arange(n), length)
    return 0.5 - 0.5 * np.cos(np.pi * k / length)


class PcmDelay:
    """A constant delay of n samples on int16 PCM, switched in and out by a crossfade.

    Used on RX2's browser audio so the stereo pair stays time-aligned with RX1 while
    RX1's STFT stage is in the path. Off, process() returns the bytes it was given
    (the same object), and keeps the last n samples so the delayed signal exists at the
    moment it is switched in. On, the output is the input delayed by exactly n samples.
    Entry and exit crossfade between the live and the delayed signal over `fade` samples.
    The cost is one n-sample copy per block."""

    def __init__(self, n: int = DEFAULT_N, fade: int = DEFAULT_N):
        self.n = int(n)
        self.fade = int(fade)
        self.request = False
        self.state = "bypass"            # bypass | entering | in | exiting
        self._pos = 0
        self._buf = np.zeros(self.n, dtype=np.int16)

    def reset(self):
        """TX: forget the history, and finish any fade in progress."""
        self._buf = np.zeros(self.n, dtype=np.int16)
        self.state = "in" if self.state in ("entering", "in") and self.request else "bypass"
        self._pos = 0

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16)
        joined = np.concatenate([self._buf, x])
        delayed = joined[:len(x)]
        self._buf = joined[len(x):]
        if self.state == "bypass":
            if not self.request:
                return pcm
            self.state, self._pos = "entering", 0
        elif self.state == "in" and not self.request:
            self.state, self._pos = "exiting", 0
        if self.state == "in":
            return delayed.tobytes()
        w = fade_weights(self._pos, len(x), self.fade)
        self._pos += len(x)
        if self.state == "entering":
            out = (1.0 - w) * x + w * delayed
            if self._pos >= self.fade:
                self.state = "in"
        else:
            out = (1.0 - w) * delayed + w * x
            if self._pos >= self.fade:
                self.state = "bypass"
        return np.rint(out).astype(np.int16).tobytes()
