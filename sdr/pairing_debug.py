"""Sample-pairing and RX0 CPU diagnostics — TEMPORARY, env-gated.

Enabled only when the environment variable DEBUG_SAMPLE_PAIRING=1 is set
when the console starts. With the flag off, SdrClient wires the plain
callbacks and this module is never constructed, so there is no per-call
cost at all.

With the flag on:
  * Each native stream callback is wrapped. The wrapper timestamps the call
    (monotonic_ns on arrival, perf_counter_ns and thread_time_ns around the
    real body) and writes one fixed-width row into a preallocated numpy ring.
    No allocation, no I/O, no lock on the callback side. One ring per tuner,
    so the two vendor callbacks never share a write index.
  * A writer thread wakes once a second, drains both rings, and appends:
      logs/pairing_debug.csv  — one row per callback (stream, firstSampleNum,
                                numSamples, arrival_ns, body_ns, cpu_ns)
      logs/rx0_cpu_debug.csv  — one row per second: process CPU %, per-stream
                                callback body mean/max, RX0 state (enabled,
                                weight, sample_delay, levels, null depth),
                                queue depths and drop counters.

Known limits, to read the numbers correctly:
  * The writer thread itself uses the GIL while formatting rows, so its cost
    shows up in process CPU and can perturb what it is measuring. Keep it in
    mind when comparing RX0 on vs off.
  * Per-thread CPU is measured for the two vendor callback threads only
    (thread_time_ns inside the wrapper). The FFT, demod and combiner threads
    are NOT timed individually here; use process CPU % and compare
    combiner-on vs combiner-off runs instead.
  * The per-second "null depth" is a ratio of mean batch power, not a
    calibrated null measurement, and it includes RX1's own receiver noise.
  * Ring capacity is 65536 rows per stream. If the writer falls more than
    that behind, the oldest rows are discarded and counted in
    lost_rows_*.
"""

import csv
import logging
import os
import threading
import time
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

_CAP = 1 << 16
_MASK = _CAP - 1
_COLS = 5   # first, n, arrival_ns, body_ns, cpu_ns

_REPO_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

_PAIRING_HEADER = ["stream", "first_sample", "num_samples",
                   "arrival_ns", "body_ns", "cpu_ns"]
_PER_SEC_HEADER = [
    "ts", "proc_cpu_pct",
    "a_cb_count", "a_body_mean_us", "a_body_max_us", "a_cpu_mean_us",
    "b_cb_count", "b_body_mean_us", "b_body_max_us", "b_cpu_mean_us",
    "lost_rows_a", "lost_rows_b",
    "combine_enabled", "combine_gain", "combine_phase_deg",
    "combine_sample_delay", "combine_level_dbfs",
    "rx1_power_db", "rx2_power_db", "rx0_power_db", "null_depth_db",
    "combine_batches",
    "dropped_a", "dropped_b", "dropped_0a", "dropped_0b",
    "qsize_a", "qsize_b", "qsize_0a", "qsize_0b",
    "rf_gain_pct", "rf_gain_pct_b",
]


class PairingDebug:
    def __init__(self, sdr, logs_dir: str = _REPO_LOGS):
        self._sdr = sdr
        os.makedirs(logs_dir, exist_ok=True)
        self._pairing_path = os.path.join(logs_dir, "pairing_debug.csv")
        self._per_sec_path = os.path.join(logs_dir, "rx0_cpu_debug.csv")
        # One ring per tuner, each written by exactly one callback thread.
        self._ring = [np.zeros((_CAP, _COLS), dtype=np.int64) for _ in range(2)]
        self._w = [0, 0]          # total rows written (producer side)
        self._r = [0, 0]          # total rows drained (writer side)
        self._lost = [0, 0]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_proc = time.process_time()
        self._last_wall = time.monotonic()

    # ------------------------------------------------------------------
    # Producer side — runs on the vendor callback threads
    # ------------------------------------------------------------------

    def wrap(self, fn: Callable, stream: int) -> Callable:
        """Return a callback with the same signature as `fn` that records
        timing and pairing data around it. `stream` is 0 for tuner 1 (A)
        and 1 for tuner 2 (B)."""
        def _timed(xi, xq, params, num_samples, reset, cb_context):
            t_arrival = time.monotonic_ns()
            p0 = time.perf_counter_ns()
            c0 = time.thread_time_ns()
            fn(xi, xq, params, num_samples, reset, cb_context)
            cpu = time.thread_time_ns() - c0
            body = time.perf_counter_ns() - p0
            try:
                first = int(params.contents.firstSampleNum) if params else -1
                self._record(stream, first, int(num_samples), t_arrival, body, cpu)
            except Exception:
                pass   # never raise into the vendor callback
        return _timed

    def _record(self, stream, first, n, t_arrival, body, cpu):
        w = self._w[stream]
        self._ring[stream][w & _MASK] = (first, n, t_arrival, body, cpu)
        self._w[stream] = w + 1

    # ------------------------------------------------------------------
    # Writer side — dedicated thread, once per second
    # ------------------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # Turn on the combiner's own debug power sums (flag-gated inside
        # combiner.py; cost only while the combiner is enabled). Set here,
        # not in __init__: SdrClient builds the combiner after this object.
        self._sdr.combiner.debug_levels = True
        self._open_files()
        self._last_proc = time.process_time()
        self._last_wall = time.monotonic()
        self._thread = threading.Thread(target=self._writer_loop,
                                        name="pairing-debug", daemon=True)
        self._thread.start()
        logger.warning(f"DEBUG_SAMPLE_PAIRING=1 — writing {self._pairing_path} "
                       f"and {self._per_sec_path}")

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3.0)
            self._thread = None
        self._flush()   # final drain after the loop has exited

    def _open_files(self):
        for path, header in ((self._pairing_path, _PAIRING_HEADER),
                             (self._per_sec_path, _PER_SEC_HEADER)):
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="") as fh:
                if new:
                    csv.writer(fh).writerow(header)

    def _writer_loop(self):
        while not self._stop.wait(1.0):
            self._flush()

    def _drain(self, stream):
        w, r = self._w[stream], self._r[stream]
        if w - r > _CAP:
            self._lost[stream] += (w - r) - _CAP
            r = w - _CAP
        if w == r:
            return np.zeros((0, _COLS), dtype=np.int64)
        rows = self._ring[stream][np.arange(r, w) & _MASK].copy()
        self._r[stream] = w
        return rows

    def _flush(self):
        rows_a = self._drain(0)
        rows_b = self._drain(1)
        now = time.time()

        # Merge both streams into arrival order, so the CSV reads as a
        # timeline (the analyzer relies on this).
        both = np.concatenate([rows_a, rows_b])
        labels = ["A"] * len(rows_a) + ["B"] * len(rows_b)
        order = np.argsort(both[:, 2], kind="stable")
        with open(self._pairing_path, "a", newline="") as fh:
            wr = csv.writer(fh)
            for i in order.tolist():
                first, n, t, body, cpu = both[i].tolist()
                wr.writerow([labels[i], first, n, t, body, cpu])

        proc_now = time.process_time()
        wall_now = time.monotonic()
        dwall = max(wall_now - self._last_wall, 1e-9)
        proc_pct = 100.0 * (proc_now - self._last_proc) / dwall
        self._last_proc, self._last_wall = proc_now, wall_now

        sdr = self._sdr
        c = sdr.combiner
        n_b = c.dbg_n
        pa, pb, po = c.dbg_pow_a, c.dbg_pow_b, c.dbg_pow_out
        c.dbg_pow_a = c.dbg_pow_b = c.dbg_pow_out = 0.0
        c.dbg_n = 0
        rx1_db = _db(pa / n_b) if n_b else None
        rx2_db = _db(pb / n_b) if n_b else None
        rx0_db = _db(po / n_b) if n_b else None
        null_db = (rx0_db - rx1_db) if (n_b and rx0_db is not None and rx1_db is not None) else None

        def _cb_stats(rows):
            if len(rows) == 0:
                return [0, "", "", ""]
            body_us = rows[:, 3] / 1000.0
            cpu_us = rows[:, 4] / 1000.0
            return [len(rows), round(float(body_us.mean()), 2),
                    round(float(body_us.max()), 2), round(float(cpu_us.mean()), 2)]

        row = [f"{now:.3f}", round(proc_pct, 1)]
        row += _cb_stats(rows_a) + _cb_stats(rows_b)
        row += [self._lost[0], self._lost[1]]
        row += [int(c.enabled), c.gain, round(c.phase_deg, 1),
                c.sample_delay, c.level_dbfs,
                _fmt(rx1_db), _fmt(rx2_db), _fmt(rx0_db), _fmt(null_db), n_b]
        row += [sdr.dropped_count, sdr.dropped_count_b,
                c.dropped_count_a, c.dropped_count_b,
                sdr._q.qsize(), sdr._q_b.qsize(),
                c._q_a.qsize(), c._q_b.qsize()]
        row += [sdr.rf_gain_pct, sdr.rf_gain_pct_b]
        with open(self._per_sec_path, "a", newline="") as fh:
            csv.writer(fh).writerow(row)


def _db(p: float):
    return 10.0 * float(np.log10(p + 1e-20))


def _fmt(v):
    return "" if v is None else round(float(v), 2)
