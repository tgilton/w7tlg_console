"""
Audio-thread profiling for the queue-drop investigation (2026-10-10): why
AudioDemodulator's input queue overflows on the M5 with noise reduction on.

Off unless W7TLG_AUDIO_PROFILE=1. Off, AudioDemodulator holds no profiler
(self._prof is None) and every hook is a single `is not None` test.

On, the audio and native-callback threads only take timestamps and append
tuples to a deque. A separate writer thread formats them and appends to
data/audio_profile.log once per second, so no file I/O or string formatting
lands in the threads being measured.

Line kinds (all times in ms):

  start  pid, torch thread counts — once per process
  iter   one _run iteration that dequeued a chunk: wait (blocked in
         queue.get), total (work after the get), then the stages; `other`
         is total minus the stages (accumulator copy, PCM delay, TX reset)
  queue  once per second per instance: depth now, high-water mark over the
         last second and since start, feed() calls in the last second, total
         drops, and how late this writer thread woke up (it only sleeps, so
         lateness means it could not get the GIL or a core)
  drop   every feed() drop: queue depth, time since the previous feed()
         call, chunk size in samples

Safe to delete once the investigation concludes.
"""

import atexit
import collections
import os
import sys
import threading
import time
import weakref

ENABLED = os.environ.get("W7TLG_AUDIO_PROFILE") == "1"

LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio_profile.log")

# Stage indices for AudioProfiler.lap().
DEMOD, FINE, NOISE_SUB, DIGITAL, NR_LOAD, NR, AGC_EQ, PUBLISH = range(8)
_STAGE_NAMES = ("demod", "fine", "noise_sub", "digital", "nr_load", "nr", "agc_eq", "publish")


def _stamp(ts: float) -> str:
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            + f".{int((ts % 1) * 1e6):06d}")


class _Sink:
    """Collects events from every profiler and writes them from its own thread."""

    def __init__(self):
        self.events: collections.deque = collections.deque()
        self._profilers: "weakref.WeakSet" = weakref.WeakSet()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._thread = None

    def register(self, prof: "AudioProfiler"):
        with self._lock:
            self._profilers.add(prof)
            if self._thread is None:
                torch = sys.modules.get("torch")
                threads = (f"torch_threads={torch.get_num_threads()} "
                           f"torch_interop_threads={torch.get_num_interop_threads()}"
                           if torch is not None else "torch=absent")
                self.events.append(("raw", time.time(), f"start pid={os.getpid()} {threads}"))
                self._thread = threading.Thread(
                    target=self._loop, name="audio-profile-writer", daemon=True)
                self._thread.start()
                atexit.register(self.flush)

    def _loop(self):
        next_tick = time.monotonic() + 1.0
        while True:
            time.sleep(max(0.0, next_tick - time.monotonic()))
            late_s = time.monotonic() - next_tick
            next_tick += 1.0
            if late_s > 1.0:
                next_tick = time.monotonic() + 1.0   # stalled past a whole tick: resync
            try:
                self.flush(late_s)
            except Exception:
                pass   # profiling must never take the process down

    def flush(self, late_s=None):
        if late_s is not None:
            with self._lock:
                profilers = list(self._profilers)
            for prof in profilers:
                line = prof.queue_line(late_s)
                if line is not None:
                    self.events.append(("raw", time.time(), line))
        lines = []
        while True:
            try:
                ev = self.events.popleft()
            except IndexError:
                break
            lines.append(self._format(ev))
        if not lines:
            return
        with self._write_lock:
            with open(LOG_PATH, "a") as f:
                f.write("\n".join(lines) + "\n")

    @staticmethod
    def _format(ev) -> str:
        kind, ts = ev[0], ev[1]
        if kind == "raw":
            return f"{_stamp(ts)} {ev[2]}"
        if kind == "drop":
            _, _, prof, depth, gap_s, chunk = ev
            gap = f"{gap_s * 1e3:.3f}" if gap_s is not None else "na"
            return (f"{_stamp(ts)} drop {prof.tag()} depth={depth} "
                    f"since_feed_ms={gap} chunk={chunk}")
        _, _, prof, chunk, batches, wait_s, total_s, acc = ev
        stages = " ".join(f"{name}={v * 1e3:.3f}" for name, v in zip(_STAGE_NAMES, acc))
        return (f"{_stamp(ts)} iter {prof.tag()} chunk={chunk} batches={batches} "
                f"wait={wait_s * 1e3:.3f} total={total_s * 1e3:.3f} {stages} "
                f"other={(total_s - sum(acc)) * 1e3:.3f}")


_sink = _Sink()


class AudioProfiler:
    """One per AudioDemodulator. The iteration methods run on that instance's audio
    thread, the feed methods on the native callback thread that feeds it."""

    def __init__(self, demod):
        self._demod = weakref.ref(demod)
        self._id = id(demod)   # same id nr_latency.log uses
        self._acc = [0.0] * len(_STAGE_NAMES)
        self._open = False
        self._t_start = 0.0
        self._wall = 0.0
        self._wait = 0.0
        self._chunk = 0
        self.batches = 0
        self._last_feed = None
        self._feeds = 0
        self._hwm = 0
        self._hwm_1s = 0
        _sink.register(self)

    def tag(self) -> str:
        demod = self._demod()
        if demod is None:
            role = "gone"
        elif demod.stft is not None:
            role = "rx1"
        elif demod.pcm_delay is not None:
            role = "rx2"
        else:
            role = "-"
        return f"id={self._id} role={role}"

    # --- audio thread ---

    def end_iter(self) -> float:
        """Top of the _run loop: closes the iteration in progress, if any. Returns now,
        which is also the start of the next queue wait."""
        now = time.perf_counter()
        if self._open:
            self._open = False
            _sink.events.append(("iter", self._wall, self, self._chunk, self.batches,
                                 self._wait, now - self._t_start, tuple(self._acc)))
        return now

    def begin_iter(self, t_wait: float, chunk: int):
        now = time.perf_counter()
        self._wait = now - t_wait
        self._t_start = now
        self._wall = time.time()
        self._chunk = chunk
        self.batches = 0
        acc = self._acc
        for i in range(len(acc)):
            acc[i] = 0.0
        self._open = True

    def lap(self, stage: int, t0: float) -> float:
        """Charge the time since t0 to a stage. Returns now, to chain into the next lap."""
        now = time.perf_counter()
        self._acc[stage] += now - t0
        return now

    # --- native callback thread ---

    def on_feed(self):
        """Every feed() call. Returns seconds since the previous one (None on the first)."""
        now = time.perf_counter()
        gap = None if self._last_feed is None else now - self._last_feed
        self._last_feed = now
        self._feeds += 1
        return gap

    def on_depth(self, depth: int):
        if depth > self._hwm_1s:
            self._hwm_1s = depth
            if depth > self._hwm:
                self._hwm = depth

    def on_drop(self, depth: int, gap, chunk: int):
        _sink.events.append(("drop", time.time(), self, depth, gap, chunk))

    # --- writer thread ---

    def queue_line(self, late_s: float):
        demod = self._demod()
        if demod is None:
            return None
        depth = demod._q.qsize()
        hwm_1s, self._hwm_1s = max(self._hwm_1s, depth), depth
        feeds, self._feeds = self._feeds, 0
        return (f"queue {self.tag()} depth={depth} hwm_1s={hwm_1s} hwm={max(self._hwm, depth)} "
                f"feeds_1s={feeds} drops={demod.dropped_count} "
                f"writer_late_ms={late_s * 1e3:.3f}")
