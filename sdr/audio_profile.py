"""
Audio-thread profiling for the queue-drop investigation: why AudioDemodulator's
input queue overflows in bursts.

Off unless W7TLG_AUDIO_PROFILE is set. Off, AudioDemodulator holds no profiler
(self._prof is None) and every hook is a single `is not None` test.

  W7TLG_AUDIO_PROFILE=1        one `sec` line per second per receiver
  W7TLG_AUDIO_PROFILE=verbose  that, plus a `slow` line for each _run iteration
                               that took more than SLOW_OVER_S longer than
                               expected, and a `drop` line for each feed() drop

Written to data/audio_profile.log, rotated to audio_profile.log.1 at 50 MB (one
old file kept). The audio and native-callback threads only take timestamps and
update a few maxima; a separate writer thread formats and writes once a second,
so no file I/O or string formatting lands in the threads being measured.

`sec` fields (times in ms, each the maximum over the second unless noted):

  fed, chunk      feed() calls, and the size of the last chunk in samples
  iters, batches  _run iterations that dequeued a chunk; demod batches processed
  drops, drops_total
  depth_max       highest queue depth seen by feed()
  feed_gap_max    longest time between two feed() calls (the native callback side)
  iter_max        longest iteration, work only (after the queue.get)
  wait_max        longest time blocked in queue.get
  demod_max … publish_max
                  longest single-iteration time in each stage: demod (mix,
                  decimation, SSB filter), fine (fine spectrum), noise_sub and
                  digital (RX1's NOISE SUB stage and raw digital chain), eq, nr,
                  nr_load (model load), agc (AGC, limiter, PCM conversion),
                  publish (hand-off to the asyncio loop)
  ws_skip, ws_skip_ms
                  browser audio frames skipped by the server's 50 ms websocket
                  send timeout this second, and the audio they held
  gc_max, gc_gen2 longest Python garbage-collection pause this second and the
                  number of full (generation 2) collections. Process-wide: a
                  collection stops every Python thread, whichever one started it
  writer_late_ms  how late the writer thread woke from its 1 s sleep. It only
                  sleeps, so lateness means it could not get the GIL or a core

An iteration is expected to take one chunk period (queue wait plus work), so
`slow` means wait + work > chunk period + SLOW_OVER_S.

Safe to delete once the investigation concludes.
"""

import atexit
import collections
import gc
import os
import sys
import threading
import time
import weakref

def parse_mode(value):
    """(enabled, verbose) for a W7TLG_AUDIO_PROFILE value. Anything but 1 or verbose is off."""
    mode = (value or "").strip().lower()
    return mode in ("1", "verbose"), mode == "verbose"


ENABLED, VERBOSE = parse_mode(os.environ.get("W7TLG_AUDIO_PROFILE"))

LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio_profile.log")
MAX_LOG_BYTES = 50 * 1024 * 1024
SLOW_OVER_S = 0.030

# Stage indices for AudioProfiler.lap().
DEMOD, FINE, NOISE_SUB, DIGITAL, EQ, NR, NR_LOAD, AGC, PUBLISH = range(9)
_STAGE_NAMES = ("demod", "fine", "noise_sub", "digital", "eq", "nr", "nr_load", "agc", "publish")
_N_STAGES = len(_STAGE_NAMES)

# Browser audio frames skipped by the server's websocket send timeout, by receiver
# label: [frames, ms of audio]. Counted whether or not profiling is on (server.py
# also warns about them); the `sec` line reports the change each second.
ws_skips: dict = {}


def note_ws_skip(label: str, audio_ms: float):
    entry = ws_skips.setdefault(label, [0, 0.0])
    entry[0] += 1
    entry[1] += audio_ms


def _stamp(ts: float) -> str:
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            + f".{int((ts % 1) * 1e6):06d}")


class _Window:
    """One second of one receiver. The writer swaps in a fresh one each tick."""
    __slots__ = ("fed", "chunk", "depth_max", "feed_gap_max",
                 "iters", "batches", "iter_max", "wait_max", "stage_max")

    def __init__(self):
        self.fed = 0
        self.chunk = 0
        self.depth_max = 0
        self.feed_gap_max = 0.0
        self.iters = 0
        self.batches = 0
        self.iter_max = 0.0
        self.wait_max = 0.0
        self.stage_max = [0.0] * _N_STAGES


class Sink:
    """Collects from every profiler and writes from its own thread."""

    def __init__(self, path=None, verbose=None):
        self.path = path
        self.verbose = VERBOSE if verbose is None else verbose
        self.events: collections.deque = collections.deque()   # verbose lines, preformatted late
        self._profilers: "weakref.WeakSet" = weakref.WeakSet()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._thread = None
        self._gc_t0 = None
        self._gc_max = 0.0
        self._gc_gen2 = 0

    def register(self, prof: "AudioProfiler"):
        with self._lock:
            self._profilers.add(prof)

    def start(self):
        """Start the writer thread and GC tracking (once). Not used by tests, which
        call tick() themselves."""
        with self._lock:
            if self._thread is not None:
                return
            torch = sys.modules.get("torch")
            threads = (f"torch_threads={torch.get_num_threads()} "
                       f"torch_interop_threads={torch.get_num_interop_threads()}"
                       if torch is not None else "torch=absent")
            self.events.append(
                f"{_stamp(time.time())} start pid={os.getpid()} "
                f"mode={'verbose' if self.verbose else 'summary'} {threads}")
            gc.callbacks.append(self.gc_callback)
            self._thread = threading.Thread(
                target=self._loop, name="audio-profile-writer", daemon=True)
            self._thread.start()
            atexit.register(self._write_pending)

    def gc_callback(self, phase, info):
        # Runs in whichever thread triggered the collection, with every other Python
        # thread stopped for its duration.
        if phase == "start":
            self._gc_t0 = time.perf_counter()
        elif self._gc_t0 is not None:
            pause = time.perf_counter() - self._gc_t0
            self._gc_t0 = None
            if pause > self._gc_max:
                self._gc_max = pause
            if info.get("generation") == 2:
                self._gc_gen2 += 1

    def _loop(self):
        next_tick = time.monotonic() + 1.0
        while True:
            time.sleep(max(0.0, next_tick - time.monotonic()))
            late_s = time.monotonic() - next_tick
            next_tick += 1.0
            if late_s > 1.0:
                next_tick = time.monotonic() + 1.0   # stalled past a whole tick: resync
            try:
                self.tick(late_s)
            except Exception:
                pass   # profiling must never take the process down

    def tick(self, late_s: float = 0.0):
        """One second boundary: a `sec` line per receiver, then write everything pending."""
        gc_max, self._gc_max = self._gc_max, 0.0
        gc_gen2, self._gc_gen2 = self._gc_gen2, 0
        with self._lock:
            profilers = list(self._profilers)
        now = time.time()
        for prof in sorted(profilers, key=lambda p: p.label()):
            line = prof.sec_line()
            if line is not None:
                self.events.append(
                    f"{_stamp(now)} {line} gc_max={gc_max * 1e3:.3f} gc_gen2={gc_gen2} "
                    f"writer_late_ms={late_s * 1e3:.3f}")
        self._write_pending()

    def _write_pending(self):
        lines = []
        while True:
            try:
                lines.append(self.events.popleft())
            except IndexError:
                break
        if not lines:
            return
        path = self.path or LOG_PATH
        with self._write_lock:
            try:
                if os.path.getsize(path) >= MAX_LOG_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass   # no file yet
            with open(path, "a") as f:
                f.write("\n".join(lines) + "\n")


_sink = Sink()


class AudioProfiler:
    """One per AudioDemodulator. The iteration methods run on that instance's audio
    thread, the feed methods on the native callback thread that feeds it."""

    def __init__(self, demod, sink=None):
        self._demod = weakref.ref(demod)
        self._id = id(demod)   # same id nr_latency.log uses
        self._sink = _sink if sink is None else sink
        self._verbose = self._sink.verbose
        self._rate = float(demod.input_rate_hz)
        self._win = _Window()
        self._acc = [0.0] * _N_STAGES
        self._open = False
        self._t_start = 0.0
        self._wall = 0.0
        self._wait = 0.0
        self._chunk = 0
        self.batches = 0
        self._last_feed = None
        self._drops_logged = 0
        self._ws_logged = (0, 0.0)
        self._sink.register(self)
        if sink is None:
            self._sink.start()

    def label(self) -> str:
        demod = self._demod()
        return getattr(demod, "rx_label", "RX") if demod is not None else "gone"

    def tag(self) -> str:
        return f"rx={self.label()} id={self._id}"

    # --- audio thread ---

    def end_iter(self) -> float:
        """Top of the _run loop: closes the iteration in progress, if any. Returns now,
        which is also the start of the next queue wait."""
        now = time.perf_counter()
        if self._open:
            self._open = False
            total = now - self._t_start
            acc = self._acc
            win = self._win
            win.iters += 1
            win.batches += self.batches
            if total > win.iter_max:
                win.iter_max = total
            if self._wait > win.wait_max:
                win.wait_max = self._wait
            stage_max = win.stage_max
            for i in range(_N_STAGES):
                if acc[i] > stage_max[i]:
                    stage_max[i] = acc[i]
            if self._verbose:
                over = self._wait + total - self._chunk / self._rate
                if over > SLOW_OVER_S:
                    self._sink.events.append(self._slow_line(total, over))
        return now

    def begin_iter(self, t_wait: float, chunk: int):
        now = time.perf_counter()
        self._wait = now - t_wait
        self._t_start = now
        self._wall = time.time()
        self._chunk = chunk
        self.batches = 0
        acc = self._acc
        for i in range(_N_STAGES):
            acc[i] = 0.0
        self._open = True

    def lap(self, stage: int, t0: float) -> float:
        """Charge the time since t0 to a stage. Returns now, to chain into the next lap."""
        now = time.perf_counter()
        self._acc[stage] += now - t0
        return now

    def _slow_line(self, total: float, over: float) -> str:
        stages = " ".join(f"{name}={v * 1e3:.3f}" for name, v in zip(_STAGE_NAMES, self._acc))
        return (f"{_stamp(self._wall)} slow {self.tag()} chunk={self._chunk} "
                f"batches={self.batches} over={over * 1e3:.3f} wait={self._wait * 1e3:.3f} "
                f"total={total * 1e3:.3f} {stages} other={(total - sum(self._acc)) * 1e3:.3f}")

    # --- native callback thread ---

    def on_feed(self, chunk: int):
        """Every feed() call. Returns seconds since the previous one (None on the first)."""
        now = time.perf_counter()
        gap = None if self._last_feed is None else now - self._last_feed
        self._last_feed = now
        win = self._win
        win.fed += 1
        win.chunk = chunk
        if gap is not None and gap > win.feed_gap_max:
            win.feed_gap_max = gap
        return gap

    def on_depth(self, depth: int):
        win = self._win
        if depth > win.depth_max:
            win.depth_max = depth

    def on_drop(self, depth: int, gap, chunk: int):
        self.on_depth(depth)
        if self._verbose:
            since = f"{gap * 1e3:.3f}" if gap is not None else "na"
            self._sink.events.append(
                f"{_stamp(time.time())} drop {self.tag()} depth={depth} "
                f"since_feed_ms={since} chunk={chunk}")

    # --- writer thread ---

    def sec_line(self):
        demod = self._demod()
        if demod is None:
            return None
        win, self._win = self._win, _Window()
        drops_total = demod.dropped_count
        drops, self._drops_logged = drops_total - self._drops_logged, drops_total
        ws_n, ws_ms = ws_skips.get(self.label(), (0, 0.0))
        ws_dn, ws_dms = ws_n - self._ws_logged[0], ws_ms - self._ws_logged[1]
        self._ws_logged = (ws_n, ws_ms)
        stages = " ".join(f"{name}_max={v * 1e3:.3f}"
                          for name, v in zip(_STAGE_NAMES, win.stage_max))
        return (f"sec {self.tag()} fed={win.fed} chunk={win.chunk} iters={win.iters} "
                f"batches={win.batches} drops={drops} drops_total={drops_total} "
                f"depth_max={max(win.depth_max, demod._q.qsize())} "
                f"feed_gap_max={win.feed_gap_max * 1e3:.3f} "
                f"iter_max={win.iter_max * 1e3:.3f} wait_max={win.wait_max * 1e3:.3f} "
                f"{stages} ws_skip={ws_dn} ws_skip_ms={ws_dms:.0f}")
