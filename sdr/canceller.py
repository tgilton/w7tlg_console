"""RX1 CANCEL — y = x1 - w*x2 on the 2 Msps IQ (2026-10-04).

x1 is tuner A (RX1), x2 is tuner B (RX2), and

    w = 10^(gain_db/20) * exp(j * phase_deg)

Phase invert is +180 deg, applied by the front end as a phase change, not
as a separate flag. With CANCEL on, y replaces raw RX1 downstream of the
callback (spectrum queue, RX1 FFT, S-meter, RX1 demod and audio). RX2 stays
raw, and the RX0 combiner keeps its own raw RX1 feed.

Threading. Vendor callbacks only copy a block and enqueue it with its
firstSampleNum (see SdrClient._on_stream_data*). A worker thread pairs the
A and B blocks by equal firstSampleNum, processes them in batches of
BATCH_PAIRS, and delivers y through the same entry points raw RX1 uses. The
queues are drop-oldest, never blocking. A counter that is present on only
one side is discarded (the older block), and the pair is resynced by
counter. Nothing unpaired is ever subtracted.

Output format. Downstream (spectrum queue, AudioDemodulator, FFT) is int16,
so y is rounded to int16 and saturated, and both saturation and resync counts
are kept. The effective floor is about -100 dBFS per sample. Deep
cancellation is limited by that rounding plus the ADC noise already in x1.

Weight changes are first-order: w approaches its target with a 50 ms time
constant, and within each batch w is interpolated linearly per sample, so
the step between samples is always bounded by the step in w.

The ghost trace is an FFT of raw x1 in the same worker. The residual
readout compares its averaged power with the averaged power of y (the RX1
panadapter's own averaged frame), over the whole span and the audio passband.
"""

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

WRAP = 1 << 32
GAIN_MIN_DB = -40.0
GAIN_MAX_DB = 80.0
PHASE_STEP_DEG = 0.1
GAIN_STEP_DB = 0.1
RAMP_TAU_S = 0.050          # 50 ms first-order approach to a new weight
BATCH_PAIRS = 16            # 16 blocks x 252 samples = 4032 samples per batch
MAX_PENDING_BLOCKS = 2048   # per side; beyond this the oldest is dropped
QUEUE_BLOCKS = 512          # per side, drop-oldest on overflow
RESIDUAL_EMA = 0.2          # per ghost frame; ~0.3 s at 18 fps
RESYNC_LOG_INTERVAL_S = 5.0

ContextFn = Callable[[], dict]


def _seq_diff(a: int, b: int) -> int:
    """Signed a - b on the uint32 sample counter."""
    d = (a - b) % WRAP
    return d - WRAP if d >= WRAP // 2 else d


def _quantize(v: float, step: float) -> float:
    return round(v / step) * step


class Canceller:
    def __init__(
        self,
        sample_rate_hz: float = 2_000_000.0,
        fft_size: int = 65536,
        display_fps: float = 18.0,
        deliver: Optional[Callable[[np.ndarray, np.ndarray], None]] = None,
        publish_ghost: Optional[Callable[[dict], None]] = None,
        cancelled_power: Optional[Callable[[], Optional[np.ndarray]]] = None,
        context: Optional[ContextFn] = None,
        settings_path: Optional[str] = None,
    ):
        self.sample_rate_hz = sample_rate_hz
        self.fft_size = fft_size
        self.display_fps = display_fps
        self._deliver = deliver
        self._publish_ghost = publish_ghost
        self._cancelled_power = cancelled_power
        self._context = context
        self.settings_path = settings_path

        # Operator settings. Gain and phase persist (settings_path); enabled
        # never does, so CANCEL starts off every session.
        self.enabled = False
        self.gain_db = 0.0
        self.phase_deg = 0.0
        self.ghost = False
        self.follow_gain = True
        # NOISE SUB (sdr/noise_sub.py): display-only, its own mode flag. It never
        # turns on the coherent IQ path (self.enabled), so that path is untouched.
        self.noise_enabled = False
        self.noise_scale_db = -10.0
        self.noise_n = 2.0
        self.noise_clamp = False
        self.noise_beta_db = -20.0       # audio stage maximum attenuation (power), -6..-40 dB
        self.noise_audio_on = False      # AUDIO switch: off each session, not persisted
        self.noise_audio_ab = True       # A/B: True = processed audio, False = raw (AUDIO on only)
        self.last_mode = "coherent"      # the mode ON and MODE go back to
        self.noise_status: Optional[str] = None
        self.noise_lines = 0
        self.noise_false_pct: Optional[float] = None
        self.noise_floor1_db: Optional[float] = None
        self.noise_floor2_db: Optional[float] = None
        self._load_settings()

        # Written only by the worker thread, read by state publishing.
        self.tx_active = False
        self.resync_count = 0
        self.clip_count = 0
        self.overflow_count = 0
        self.residual_span_db: Optional[float] = None
        self.residual_passband_db: Optional[float] = None
        self._w_now = self.w_target
        self._last_resync_log = 0.0
        self._resyncs_at_last_log = 0

        self._qa: "queue.Queue" = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._qb: "queue.Queue" = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._pa: deque = deque()
        self._pb: deque = deque()
        self._pending: list = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Ghost FFT over raw x1 and its averaged power.
        self._gwin = np.hanning(fft_size).astype(np.float32)
        self._gfullscale = 32767.0 * float(np.sum(self._gwin))
        self._gbuf = np.zeros(fft_size, dtype=np.complex64)
        self._glen = 0
        self._gavg: Optional[np.ndarray] = None
        self._gnext = 0.0
        self._gdecay = max(0.0, (4.0 - 1.0) / (4.0 + 1.0))
        self._sum_c: Optional[float] = None
        self._sum_g: Optional[float] = None
        self._sum_c_pb: Optional[float] = None
        self._sum_g_pb: Optional[float] = None

    # ------------------------------------------------------------------
    # Operator settings
    # ------------------------------------------------------------------

    @property
    def w_target(self) -> complex:
        g = 10.0 ** (self.gain_db / 20.0)
        return complex(g * np.exp(1j * np.radians(self.phase_deg)))

    def set_gain_db(self, gain_db: float):
        v = float(gain_db)
        if not np.isfinite(v):
            raise ValueError("gain_db must be a finite number")
        self.gain_db = round(min(GAIN_MAX_DB, max(GAIN_MIN_DB, _quantize(v, GAIN_STEP_DB))), 1)
        self._save_settings()

    def set_phase_deg(self, phase_deg: float):
        v = float(phase_deg)
        if not np.isfinite(v):
            raise ValueError("phase_deg must be a finite number")
        p = _quantize(v % 360.0, PHASE_STEP_DEG) % 360.0
        self.phase_deg = round(p, 1)
        self._save_settings()

    def set_ghost(self, on: bool):
        self.ghost = bool(on)
        self._save_settings()

    def set_follow_gain(self, on: bool):
        self.follow_gain = bool(on)
        self._save_settings()

    def set_noise_scale_db(self, v: float):
        v = float(v)
        if not np.isfinite(v):
            raise ValueError("scale_db must be a finite number")
        self.noise_scale_db = round(min(40.0, max(-40.0, _quantize(v, 0.1))), 1)
        self._save_settings()

    def set_noise_n(self, v: float):
        v = float(v)
        if not np.isfinite(v):
            raise ValueError("n must be a finite number")
        self.noise_n = round(min(6.0, max(1.0, _quantize(v, 0.1))), 1)
        self._save_settings()

    def set_noise_beta(self, db: float):
        v = float(db)
        if not np.isfinite(v):
            raise ValueError("beta must be a finite number")
        self.noise_beta_db = round(min(-6.0, max(-40.0, _quantize(v, 0.1))), 1)
        self._save_settings()

    def set_noise_audio(self, on: bool):
        self.noise_audio_on = bool(on)

    def set_noise_audio_ab(self, processed: bool):
        self.noise_audio_ab = bool(processed)

    def set_noise_clamp(self, on: bool):
        self.noise_clamp = bool(on)
        self._save_settings()

    def set_last_mode(self, mode: str):
        if mode not in ("coherent", "noise"):
            raise ValueError("mode must be coherent or noise")
        self.last_mode = mode
        self._save_settings()

    @property
    def mode(self) -> str:
        if self.noise_enabled:
            return "noise"
        return "coherent" if self.enabled else "off"

    def reset_weight(self):
        self.gain_db = 0.0
        self.phase_deg = 0.0
        self._save_settings()

    def _load_settings(self):
        if not self.settings_path or not os.path.exists(self.settings_path):
            return
        try:
            with open(self.settings_path) as fh:
                d = json.load(fh)
            self.gain_db = round(min(GAIN_MAX_DB, max(GAIN_MIN_DB, float(d.get("gain_db", 0.0)))), 1)
            self.phase_deg = float(d.get("phase_deg", 0.0)) % 360.0
            self.ghost = bool(d.get("ghost", False))
            self.follow_gain = bool(d.get("follow_gain", True))
            self.noise_scale_db = round(min(40.0, max(-40.0, float(d.get("noise_scale_db", -10.0)))), 1)
            self.noise_n = round(min(6.0, max(1.0, float(d.get("noise_n", 2.0)))), 1)
            self.noise_clamp = bool(d.get("noise_clamp", False))
            self.noise_beta_db = round(min(-6.0, max(-40.0, float(d.get("noise_beta_db", -20.0)))), 1)
            lm = d.get("last_mode", "coherent")
            self.last_mode = lm if lm in ("coherent", "noise") else "coherent"
        except Exception:
            logger.exception("cancel settings unreadable — using defaults")

    def _save_settings(self):
        if not self.settings_path:
            return
        try:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"gain_db": self.gain_db, "phase_deg": self.phase_deg,
                           "ghost": self.ghost, "follow_gain": self.follow_gain,
                           "noise_scale_db": self.noise_scale_db, "noise_n": self.noise_n,
                           "noise_clamp": self.noise_clamp, "noise_beta_db": self.noise_beta_db,
                           "last_mode": self.last_mode}, fh)
            os.replace(tmp, self.settings_path)
        except Exception:
            logger.exception("cancel settings not saved")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_enabled(self, on: bool):
        """Turn CANCEL on or off. Starts or joins the worker. Not called from
        a vendor callback."""
        on = bool(on)
        if on == self.enabled:
            return
        if on:
            self.enabled = True
            self.start_worker()
        else:
            self.enabled = False
            self.stop_worker()
            self._discard_all()
            self.residual_span_db = None
            self.residual_passband_db = None

    def start_worker(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cancel-worker", daemon=True)
        self._thread.start()

    def stop_worker(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None

    def _run(self):
        while not self._stop.is_set():
            try:
                self.pump(timeout=0.02)
            except Exception:
                logger.exception("cancel worker error — continuing")
                time.sleep(0.05)

    # ------------------------------------------------------------------
    # Producer side — called from the vendor callback threads
    # ------------------------------------------------------------------

    def feed_a(self, i: np.ndarray, q: np.ndarray, first: int):
        self._put(self._qa, (first, i, q))

    def feed_b(self, i: np.ndarray, q: np.ndarray, first: int):
        self._put(self._qb, (first, i, q))

    def _put(self, q: "queue.Queue", item):
        try:
            q.put_nowait(item)
        except queue.Full:
            try:
                q.get_nowait()
                self.overflow_count += 1
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # TX gating
    # ------------------------------------------------------------------

    def gate_tx(self):
        """Rising edge of PTT: flush the queues and the pairing state, and
        reset the ghost and residual state. Streams keep running; the stall
        watchdog depends on that."""
        self.tx_active = True
        self._discard_all()
        self._gbuf[:] = 0
        self._glen = 0
        self._gavg = None
        self._sum_c = self._sum_g = None
        self._sum_c_pb = self._sum_g_pb = None
        self.residual_span_db = None
        self.residual_passband_db = None

    def clear_tx(self):
        """Falling edge of PTT. Called from the same places as A, B and the
        combiner are cleared."""
        self.tx_active = False

    def _discard_all(self):
        for q in (self._qa, self._qb):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._pa.clear()
        self._pb.clear()
        self._pending.clear()

    # ------------------------------------------------------------------
    # Worker side
    # ------------------------------------------------------------------

    def pump(self, timeout: float = 0.0) -> int:
        """Move queued blocks, pair them, and process full or flushable
        batches. Returns the number of pairs processed. The worker thread
        calls this in a loop; tests call it directly."""
        if self.tx_active:
            self._discard_all()
            return 0
        moved = self._drain()
        if not moved and timeout > 0.0:
            try:
                self._pa.append(self._qa.get(timeout=timeout))
                moved = True
            except queue.Empty:
                pass
            if moved:
                moved = self._drain() or moved
        self._pair()
        processed = 0
        while len(self._pending) >= BATCH_PAIRS or (self._pending and not moved):
            batch = self._pending[:BATCH_PAIRS]
            del self._pending[:len(batch)]
            processed += len(batch)
            self._process(batch)
            if len(self._pending) < BATCH_PAIRS:
                break
        return processed

    def _drain(self) -> bool:
        moved = False
        for q, dq in ((self._qa, self._pa), (self._qb, self._pb)):
            while True:
                try:
                    dq.append(q.get_nowait())
                    moved = True
                except queue.Empty:
                    break
        return moved

    def _pair(self):
        pa, pb = self._pa, self._pb
        while pa and pb:
            fa, ia, qa = pa[0]
            fb, ib, qb = pb[0]
            d = _seq_diff(fa, fb)
            if d == 0 and len(ia) == len(ib):
                pa.popleft()
                pb.popleft()
                self._pending.append((fa, fb, ia, qa, ib, qb))
            elif d == 0:
                pa.popleft()
                pb.popleft()
                self._resync("block size mismatch at equal counter")
            elif d < 0:
                pa.popleft()
                self._resync("A counter without a B match")
            else:
                pb.popleft()
                self._resync("B counter without an A match")
        while len(pa) > MAX_PENDING_BLOCKS:
            pa.popleft()
            self._resync("A backlog overflow")
        while len(pb) > MAX_PENDING_BLOCKS:
            pb.popleft()
            self._resync("B backlog overflow")

    def _resync(self, reason: str):
        self.resync_count += 1
        now = time.monotonic()
        if now - self._last_resync_log >= RESYNC_LOG_INTERVAL_S:
            logger.warning(f"CANCEL resync ({reason}); "
                           f"{self.resync_count - self._resyncs_at_last_log} since last log, "
                           f"{self.resync_count} total")
            self._last_resync_log = now
            self._resyncs_at_last_log = self.resync_count

    def _process(self, batch: list):
        if self.tx_active:
            return
        x1 = np.concatenate([(ia.astype(np.float32) + 1j * qa.astype(np.float32))
                             for (_, _, ia, qa, _, _) in batch]).astype(np.complex64)
        x2 = np.concatenate([(ib.astype(np.float32) + 1j * qb.astype(np.float32))
                             for (_, _, _, _, ib, qb) in batch]).astype(np.complex64)
        n = len(x1)

        # First-order approach to the target weight, interpolated per sample
        # across the batch so consecutive samples differ by a bounded step.
        w_start = self._w_now
        frac = 1.0 - np.exp(-n / (self.sample_rate_hz * RAMP_TAU_S))
        w_end = w_start + (self.w_target - w_start) * frac
        w = np.linspace(w_start, w_end, n, dtype=np.complex128).astype(np.complex64)
        self._w_now = complex(w_end)

        y = x1 - w * x2
        yr = np.rint(y.real)
        yi = np.rint(y.imag)
        sat = (np.count_nonzero(np.abs(yr) > 32767) + np.count_nonzero(np.abs(yi) > 32767))
        if sat:
            self.clip_count += int(sat)
        i16 = np.clip(yr, -32768, 32767).astype(np.int16)
        q16 = np.clip(yi, -32768, 32767).astype(np.int16)

        self._ghost_update(x1)
        if self._deliver is not None:
            self._deliver(i16, q16)

    # ------------------------------------------------------------------
    # Ghost trace and residual readout
    # ------------------------------------------------------------------

    def _ghost_update(self, x1: np.ndarray):
        n = len(x1)
        N = self.fft_size
        if n >= N:
            self._gbuf[:] = x1[-N:]
            self._glen = N
        else:
            self._gbuf[:-n] = self._gbuf[n:]
            self._gbuf[-n:] = x1
            self._glen = min(N, self._glen + n)
        if self._glen < N:
            return
        now = time.monotonic()
        if now < self._gnext:
            return
        self._gnext = now + 1.0 / self.display_fps

        spec = np.fft.fftshift(np.fft.fft(self._gbuf * self._gwin))
        power = (np.abs(spec) ** 2).astype(np.float32)
        if self._gavg is None:
            self._gavg = power
        else:
            self._gavg = self._gavg * self._gdecay + power * (1.0 - self._gdecay)
        self._update_residual()
        if self.ghost and self._publish_ghost is not None:
            ctx = self._context() if self._context else {}
            frame = {
                "ts": time.time(),
                "channel": "A",
                "kind": "ghost",
                "center_freq_hz": ctx.get("center_hz", 0.0),
                "span_hz": self.sample_rate_hz,
                "sample_rate_hz": self.sample_rate_hz,
                "data": (10.0 * np.log10(self._gavg / (self._gfullscale ** 2) + 1e-12)).astype(np.float32),
            }
            self._publish_ghost(frame)

    def _update_residual(self):
        c = self._cancelled_power() if self._cancelled_power else None
        g = self._gavg
        if c is None or g is None or len(c) != len(g):
            self.residual_span_db = None
            self.residual_passband_db = None
            return
        span_c, span_g = float(np.sum(c)), float(np.sum(g))
        self._sum_c = span_c if self._sum_c is None else self._sum_c * (1 - RESIDUAL_EMA) + span_c * RESIDUAL_EMA
        self._sum_g = span_g if self._sum_g is None else self._sum_g * (1 - RESIDUAL_EMA) + span_g * RESIDUAL_EMA
        self.residual_span_db = 10.0 * float(np.log10((self._sum_c + 1e-30) / (self._sum_g + 1e-30)))

        band = self._passband_bins(len(g))
        if band is None:
            self.residual_passband_db = None
            return
        lo, hi = band
        pc, pg = float(np.sum(c[lo:hi])), float(np.sum(g[lo:hi]))
        self._sum_c_pb = pc if self._sum_c_pb is None else self._sum_c_pb * (1 - RESIDUAL_EMA) + pc * RESIDUAL_EMA
        self._sum_g_pb = pg if self._sum_g_pb is None else self._sum_g_pb * (1 - RESIDUAL_EMA) + pg * RESIDUAL_EMA
        self.residual_passband_db = 10.0 * float(
            np.log10((self._sum_c_pb + 1e-30) / (self._sum_g_pb + 1e-30)))

    def _passband_bins(self, n: int):
        """Bin range [lo, hi) of the audio passband, same convention as
        SdrClient.passband_strength_db (bin 0 is center - span/2)."""
        if self._context is None:
            return None
        ctx = self._context()
        freq = ctx.get("freq_hz")
        if freq is None:
            return None
        bw = float(ctx.get("bandwidth_hz", 0.0))
        low = float(ctx.get("low_cut_hz", 0.0))
        if ctx.get("mode", "USB") == "LSB":
            f_lo, f_hi = freq - low - bw, freq - low
        else:
            f_lo, f_hi = freq + low, freq + low + bw
        bin_hz = self.sample_rate_hz / n
        full_lo = ctx.get("center_hz", freq) - self.sample_rate_hz / 2
        lo = int(round((f_lo - full_lo) / bin_hz))
        hi = int(round((f_hi - full_lo) / bin_hz))
        lo, hi = max(0, min(n - 1, lo)), max(1, min(n, hi))
        if hi <= lo:
            return None
        return lo, hi

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "last_mode": self.last_mode,
            "noise_scale_db": self.noise_scale_db,
            "noise_n": self.noise_n,
            "noise_clamp": self.noise_clamp,
            "noise_beta_db": self.noise_beta_db,
            "noise_audio_on": self.noise_audio_on,
            "noise_audio_ab": self.noise_audio_ab,
            "noise_status": self.noise_status,
            "noise_lines": self.noise_lines,
            "noise_false_pct": _round_or_none(self.noise_false_pct),
            "noise_floor1_db": _round_or_none(self.noise_floor1_db),
            "noise_floor2_db": _round_or_none(self.noise_floor2_db),
            "gain_db": self.gain_db,
            "phase_deg": self.phase_deg,
            "ghost": self.ghost,
            "follow_gain": self.follow_gain,
            "residual_span_db": _round_or_none(self.residual_span_db),
            "residual_passband_db": _round_or_none(self.residual_passband_db),
            "resync_count": self.resync_count,
            "clip_count": self.clip_count,
            "overflow_count": self.overflow_count,
        }


def _round_or_none(v):
    return None if v is None else round(float(v), 1)


# ----------------------------------------------------------------------
# RX2 lock: RX2 follows RX1 while CANCEL is on, and its prior state is
# restored on the way off. Operates on any object with the SdrClient
# setters, so it is testable with the fake.
# ----------------------------------------------------------------------

def snapshot_rx2(sdr) -> dict:
    t = sdr.audio_b.target
    return {
        "freq_hz": sdr.rf_freq_hz_b,
        "target": (t.freq_hz, t.mode, t.bandwidth_hz),
        "gain_pct": sdr.rf_gain_pct_b,
        "notch": sdr.rf_notch_enabled_b,
        "dab": sdr.dab_notch_enabled_b,
    }


def apply_rx2_follow(sdr) -> bool:
    """Make RX2's center, audio target, notches and (optionally) RF gain
    match RX1's. Only calls a setter when the value actually differs.
    Returns True if anything changed."""
    changed = False
    t1 = sdr.audio.target
    if t1.freq_hz is not None and sdr.rf_freq_hz_b != sdr.rf_freq_hz:
        sdr.set_center_freq_hz_b(sdr.rf_freq_hz)
        changed = True
    t2 = sdr.audio_b.target
    if t1.freq_hz is not None and (t2.freq_hz, t2.mode, t2.bandwidth_hz) != (
            t1.freq_hz, t1.mode, t1.bandwidth_hz):
        sdr.audio_b.set_target(t1.freq_hz, t1.mode, t1.bandwidth_hz)
        changed = True
    if sdr.rf_notch_enabled_b != sdr.rf_notch_enabled:
        sdr.set_rf_notch_b(sdr.rf_notch_enabled)
        changed = True
    if sdr.dab_notch_enabled_b != sdr.dab_notch_enabled:
        sdr.set_dab_notch_b(sdr.dab_notch_enabled)
        changed = True
    if sdr.canceller.follow_gain and sdr.rf_gain_pct_b != sdr.rf_gain_pct:
        sdr.set_rf_gain_pct_b(sdr.rf_gain_pct)
        changed = True
    return changed


def restore_rx2(sdr, snap: dict):
    freq, (tf, tm, tb), gain, notch, dab = (
        snap["freq_hz"], snap["target"], snap["gain_pct"], snap["notch"], snap["dab"])
    if sdr.rf_freq_hz_b != freq:
        sdr.set_center_freq_hz_b(freq)
    if tf is not None:
        sdr.audio_b.set_target(tf, tm, tb)
    if sdr.rf_gain_pct_b != gain:
        sdr.set_rf_gain_pct_b(gain)
    if sdr.rf_notch_enabled_b != notch:
        sdr.set_rf_notch_b(notch)
    if sdr.dab_notch_enabled_b != dab:
        sdr.set_dab_notch_b(dab)


def rx2_locked(sdr) -> bool:
    """RX2 follows RX1 while either CANCEL mode is on."""
    return bool(sdr is not None and (sdr.canceller.enabled or sdr.canceller.noise_enabled))
