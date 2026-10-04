"""NOISE SUB — display-only spectrum processing (rx2-cancel, 2026-10-04).

Works on the averaged spectra the console already publishes, in linear power
on the bins of the displayed span. It never touches IQ, demodulation, audio,
WSJT-X or TX gating, and nothing here changes what RX1's audio carries.

Per frame, for each receiver:
  1. Floor F and scatter sigma per bin: a sliding window of W bins (default
     about 5% of the span, at least 101), mean and standard deviation of the
     non-masked bins. Bins above F + 3 sigma are masked, three iterations.
     The median is not used: it would need correcting for the averaging in use.
  2. A noise line on RX2 is a bin with P2 > F2 + n*sigma2 in at least 3 of the
     last 5 frames (a fixed rule, not a setting).
  3. Excess E2 = P2 - F2 on those bins, 0 elsewhere.
  4. P_out = F1 + max(P1 - F1 - k*E2, 0), with k = 10^(scale_db/10). This is
     never below F1.
  5. Optional CLAMP draws bins below F1 + n*sigma1 at F1, so only lines show.
  6. Markers: bins still above F1 + n*sigma1 after subtraction, persistent in 3
     of the last 5 frames, so noise peaks do not mark.

The processor refuses (status, no frame) when RX2 is older than 250 ms, or
when the two frames do not share a bin grid (length, center, span).
"""

from collections import deque
from math import comb
from typing import Optional

import numpy as np

BLOCK_FRAMES = 11           # raw FFT frames per non-overlapping block (sdr_client)
BLOCK_CLEAR_EMPTY = 3       # a held line clears after this many empty blocks
WINDOW_FRACTION = 0.05
MIN_WINDOW_BINS = 101
MASK_SIGMA = 3.0
MASK_ITERATIONS = 3
PERSIST_FRAMES = 5
PERSIST_MIN = 3
RX2_MAX_AGE_S = 0.250
MAX_MARKERS = 200
MAX_GROUPS = 4000     # contiguous detected runs sent per frame
SCALE_DB_MIN, SCALE_DB_MAX = -40.0, 40.0
THRESH_MIN, THRESH_MAX = 1.0, 6.0


def window_bins(n_bins: int) -> int:
    w = max(MIN_WINDOW_BINS, int(round(WINDOW_FRACTION * n_bins)))
    if w % 2 == 0:
        w += 1
    return min(w, n_bins if n_bins % 2 else n_bins - 1)


def runs(mask: np.ndarray):
    """Contiguous True runs as (starts, ends), end exclusive."""
    m8 = np.asarray(mask, dtype=np.int8)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], m8, [0]))))
    return edges[0::2], edges[1::2]


def _box_sum(x: np.ndarray, width: int) -> np.ndarray:
    """Centered sliding sum, clipped at the edges. O(N), any width."""
    n = len(x)
    half = width // 2
    c = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    idx = np.arange(n)
    lo = np.clip(idx - half, 0, n)
    hi = np.clip(idx + half + 1, 0, n)
    return c[hi] - c[lo]


def _window_stats(p: np.ndarray, keep: np.ndarray, width: int):
    cnt = _box_sum(keep, width)
    s1 = _box_sum(p * keep, width)
    s2 = _box_sum(p * p * keep, width)
    safe = np.maximum(cnt, 1.0)
    mean = s1 / safe
    var = np.maximum(s2 / safe - mean * mean, 0.0)
    # A window with nothing kept falls back to the whole-span mean of what is kept.
    if np.any(cnt == 0):
        total = np.sum(keep) or 1.0
        g_mean = np.sum(p * keep) / total
        mean = np.where(cnt > 0, mean, g_mean)
        var = np.where(cnt > 0, var, 0.0)
    return mean, np.sqrt(var)


def local_floor(p: np.ndarray, width: Optional[int] = None):
    """Floor F and sigma per bin, linear power. Returns (F, sigma)."""
    n = len(p)
    w = width or window_bins(n)
    keep = np.ones(n, dtype=np.float64)
    for _ in range(MASK_ITERATIONS):
        f, s = _window_stats(p, keep, w)
        keep = (p <= f + MASK_SIGMA * s).astype(np.float64)
    return _window_stats(p, keep, w)


class NoiseSubCore:
    """Stateful per-frame processing. The 3-of-5 history lives here."""

    def __init__(self, scale_db: float = -10.0, n: float = 2.0, clamp: bool = False):
        self.scale_db = float(scale_db)
        self.n = float(n)
        self.clamp = bool(clamp)
        self.reset_history()

    def reset_history(self):
        self._line_hist = deque(maxlen=PERSIST_FRAMES)
        self._marker_hist = deque(maxlen=PERSIST_FRAMES)

    @staticmethod
    def _persistent(hist) -> np.ndarray:
        if len(hist) < PERSIST_MIN:
            return np.zeros(len(hist[-1]), dtype=bool) if hist else np.zeros(0, dtype=bool)
        return np.sum(np.array(hist), axis=0) >= PERSIST_MIN

    def process(self, p1: np.ndarray, p2: np.ndarray, mask: Optional[np.ndarray] = None) -> dict:
        """mask: the held line mask from BlockMaskTracker (the live path). When
        None, the EMA-frame 3-of-5 detection runs instead: the reference path the
        unit tests use, not the live one, because EMA outputs are correlated."""
        f1, s1 = local_floor(p1)
        f2, s2 = local_floor(p2)
        if mask is None:
            above2 = p2 > f2 + self.n * s2
            self._line_hist.append(above2)
            detected = self._persistent(self._line_hist)
        else:
            detected = np.asarray(mask, dtype=bool)
        excess = np.where(detected, np.maximum(p2 - f2, 0.0), 0.0)
        k = 10.0 ** (self.scale_db / 10.0)
        p_out = f1 + np.maximum(p1 - f1 - k * excess, 0.0)
        thr1 = f1 + self.n * s1
        draw = np.where(self.clamp & (p_out < thr1), f1, p_out) if self.clamp else p_out
        above1 = p_out > thr1
        self._marker_hist.append(above1)
        markers_mask = self._persistent(self._marker_hist) & above1
        idx = np.flatnonzero(markers_mask)
        if len(idx) > MAX_MARKERS:
            order = np.argsort(p_out[idx])[::-1]
            idx = np.sort(idx[order[:MAX_MARKERS]])
        # Contiguous runs of detected bins over the WHOLE frame (the browser
        # counts the runs that fall in its displayed span). [start, end) pairs.
        starts, ends = runs(detected)
        groups_total = len(starts)
        flat = np.stack([starts, ends], axis=1)[:MAX_GROUPS].ravel().tolist()
        return {
            "draw": draw,
            "markers": idx.tolist(),
            "groups": flat,
            "groups_total": int(groups_total),
            "line_bins": int(np.count_nonzero(detected)),
            "lines": int(groups_total),
            "floor1_db": float(10 * np.log10(np.mean(f1) + 1e-30)),
            "floor2_db": float(10 * np.log10(np.mean(f2) + 1e-30)),
        }


class BlockMaskTracker:
    """Line mask from non-overlapping block averages of BLOCK_FRAMES raw frames.

    Per block: floor F2 from the RX2 block, sigma2 = F2 / sqrt(BLOCK_FRAMES) (the
    block average's own spread, so the scatter is not estimated from the data).
    A bin is detected when P2 > F2 + n*sigma2 in 3 of the last 5 blocks. The mask
    turns on at a detection and clears after BLOCK_CLEAR_EMPTY consecutive
    undetected blocks. The history and mask reset on any change of centre or
    span. single_rate is a running estimate of the per-bin exceedance rate on the
    unmasked bins, used for the live false-alarm readout."""

    def __init__(self, n: float = 2.0):
        self.n = float(n)
        self.reset()

    def reset(self):
        self._hist = deque(maxlen=PERSIST_FRAMES)
        self._empty: Optional[np.ndarray] = None
        self.mask: Optional[np.ndarray] = None
        self.grid: Optional[tuple] = None
        self.single_rate: Optional[float] = None
        self.blocks = 0

    def update(self, block: dict) -> np.ndarray:
        grid = (len(block["data"]), float(block["center_freq_hz"]), float(block["span_hz"]))
        if self.grid is not None and (grid[0] != self.grid[0]
                                      or abs(grid[1] - self.grid[1]) > 1.0
                                      or abs(grid[2] - self.grid[2]) > 1.0):
            self.reset()                       # centre or span changed: start clean
        self.grid = grid
        p2 = 10.0 ** (np.asarray(block["data"], dtype=np.float64) / 10.0)
        f2, _ = local_floor(p2)
        sig2 = f2 / np.sqrt(BLOCK_FRAMES)
        above = p2 > f2 + self.n * sig2
        self._hist.append(above)
        self.blocks += 1
        det = NoiseSubCore._persistent(self._hist)
        if self.mask is None:
            self.mask = np.zeros(len(p2), dtype=bool)
            self._empty = np.zeros(len(p2), dtype=np.int32)
        self._empty = np.where(det, 0, self._empty + 1)
        self.mask = (self.mask | det) & (self._empty < BLOCK_CLEAR_EMPTY)
        free = ~self.mask
        if np.any(free):
            frac = float(np.mean(above[free]))
            self.single_rate = frac if self.single_rate is None else 0.8 * self.single_rate + 0.2 * frac
        return self.mask


class NoiseSubProcessor:
    """Frame-level wrapper: freshness and grid checks, dB in and out."""

    def __init__(self, scale_db: float = -10.0, n: float = 2.0, clamp: bool = False):
        self.core = NoiseSubCore(scale_db, n, clamp)
        self.tracker = BlockMaskTracker(n)

    def set_thresh(self, n: float):
        self.core.n = float(n)
        self.tracker.n = float(n)

    def process_block(self, block_b: Optional[dict]):
        """RX2 block average: updates the held line mask."""
        if block_b is None:
            return
        self.tracker.update(block_b)

    def false_alarm_pct(self) -> Optional[float]:
        """Expected share of bins that are false per frame, from the measured
        single-look rate: a 3-of-5 over independent blocks."""
        p = self.tracker.single_rate
        if p is None:
            return None
        return 100.0 * sum(comb(5, k) * p ** k * (1 - p) ** (5 - k) for k in (3, 4, 5))

    def process_pair(self, a: dict, b: Optional[dict], now: float) -> dict:
        """a, b: console spectrum frames ({'data' (dB), 'center_freq_hz',
        'span_hz', 'ts'}). Returns {'status', 'frame', 'lines', ...}; frame is
        None unless status == 'active'."""
        if b is None:
            return self._refuse("no_rx2")
        if now - float(b["ts"]) > RX2_MAX_AGE_S:
            return self._refuse("stale")
        da, db = a["data"], b["data"]
        if (len(da) != len(db)
                or abs(a["center_freq_hz"] - b["center_freq_hz"]) > 1.0
                or abs(a["span_hz"] - b["span_hz"]) > 1.0):
            return self._refuse("grid")
        p1 = 10.0 ** (np.asarray(da, dtype=np.float64) / 10.0)
        p2 = 10.0 ** (np.asarray(db, dtype=np.float64) / 10.0)
        mask = self.tracker.mask if self.tracker.mask is not None else np.zeros(len(p2), dtype=bool)
        r = self.core.process(p1, p2, mask)
        frame = {
            "ts": a["ts"],
            "channel": "A",
            "kind": "proc",
            "center_freq_hz": a["center_freq_hz"],
            "span_hz": a["span_hz"],
            "sample_rate_hz": a.get("sample_rate_hz", a["span_hz"]),
            "data": (10.0 * np.log10(r["draw"] + 1e-30)).astype(np.float32),
            "markers": r["markers"],
            "groups": r["groups"],
        }
        return {"status": "active", "frame": frame, "lines": r["lines"],
                "floor1_db": r["floor1_db"], "floor2_db": r["floor2_db"]}

    def _refuse(self, status: str) -> dict:
        self.core.reset_history()
        self.tracker.reset()
        return {"status": status, "frame": None, "lines": 0,
                "floor1_db": None, "floor2_db": None}
