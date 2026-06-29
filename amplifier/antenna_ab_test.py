"""
Antenna A/B Test — automated signal/noise comparison across antennas

Picking a single comparison frequency by hand doesn't hold up on a busy
band: any one spot may go quiet between rounds, and a fixed "noise"
offset can just as easily land on someone else's QSO. And even a fresh
per-round sweep still bets the whole measurement on one frequency.

Instead, this profiles the *whole* requested band range per antenna over
a long dwell: it repeatedly sweeps every channel in range against the
SDR's already-computed spectrum, building a time history per channel —
numerically the same information a long-exposure waterfall shows
visually. Any channel whose 90th-percentile level rises well above its
own time-median is a real, currently-active signal (voice/CW/FT8 all
spend part of their time "off", so a channel that's only ever at the
noise floor never qualifies). Every antenna in a round is profiled over
the same range, then matched channel-by-channel so the comparison is
apples-to-apples even though several different stations may be the
"signal" at different points in the band.

Safety: every antenna move re-checks TX state, amp connection, and amp
fault status (via AcomBridge.goto_antenna) before switching, and a step
aborts the whole test cleanly — with a clear error message — rather than
pressing on with a switch that didn't confirm or a reading taken mid-TX.
"""

import asyncio
import csv
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Coroutine, Optional

import numpy as np

from amplifier.acom_bridge import ANTENNAS

logger = logging.getLogger(__name__)

CSV_DIR = Path(__file__).resolve().parent.parent / "data" / "ab_tests"

# A channel counts as "carrying a signal" if its time-history 90th
# percentile rises at least this many dB above its own time-median —
# distinguishes an intermittent transmission from plain band noise that
# just happens to flicker a little.
SIGNAL_PERCENTILE = 90
ACTIVE_CHANNEL_THRESHOLD_DB = 6.0

# How often to re-sweep all channels while building the time history.
PROFILE_SAMPLE_INTERVAL_S = 0.2

CSV_FIELDS = [
    "ts_iso", "round", "antenna", "antenna_name", "freq_hz",
    "signal_db_p90", "floor_db", "snr_db", "n_time_samples",
]

StatusCallback = Callable[[dict], Coroutine]


@dataclass
class StepResult:
    round: int
    antenna: int
    antenna_name: str
    freq_hz: float
    signal_db_p90: float
    floor_db: float
    snr_db: float
    n_time_samples: int
    ts: float

    def to_dict(self) -> dict:
        return {
            "round": self.round, "antenna": self.antenna,
            "antenna_name": self.antenna_name, "freq_hz": self.freq_hz,
            "signal_db_p90": self.signal_db_p90, "floor_db": self.floor_db,
            "snr_db": self.snr_db, "n_time_samples": self.n_time_samples,
            "ts": self.ts,
        }


class AntennaAbTest:
    """
    Usage:
        test = AntennaAbTest(bridge, sdr)
        test.on_status(my_broadcast_fn)
        ok, msg = await test.start(antennas=[1, 3], rounds=5,
                                    scan_start_hz=14_200_000, scan_stop_hz=14_325_000,
                                    profile_duration_s=30)
        ...
        ok, msg = await test.stop()
    """

    def __init__(self, bridge, sdr):
        self.bridge = bridge
        self.sdr = sdr
        self.running = False
        self.status = "idle"   # idle | running | done | error | stopped
        self.error_message = ""
        self.results: list[StepResult] = []
        self.summary: Optional[list[dict]] = None
        self.csv_path: Optional[Path] = None
        self.current_round = 0
        self.total_rounds = 0
        self.current_antenna: Optional[int] = None
        self.current_active_channels = 0
        self._task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._status_callbacks: list[StatusCallback] = []

    def on_status(self, cb: StatusCallback):
        self._status_callbacks.append(cb)

    async def _publish(self):
        status = self.get_status()
        for cb in self._status_callbacks:
            try:
                await cb(status)
            except Exception as e:
                logger.error(f"AB test status callback error: {e}")

    def get_status(self) -> dict:
        return {
            "status": self.status,
            "error": self.error_message,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "current_antenna": self.current_antenna,
            "current_active_channels": self.current_active_channels,
            "results": [r.to_dict() for r in self.results],
            "summary": self.summary,
            "csv_path": str(self.csv_path) if self.csv_path else None,
        }

    async def start(self, antennas: list[int], rounds: int,
                     scan_start_hz: float, scan_stop_hz: float,
                     bandwidth_hz: float = 2400.0,
                     profile_duration_s: float = 30.0) -> tuple[bool, str]:
        if self.running:
            return False, "A test is already running"
        if self.sdr is None or not self.sdr.available:
            return False, "SDR unavailable — cannot measure signal strength"
        if len(antennas) < 2:
            return False, "Need at least two antennas to compare"
        for a in antennas:
            if a not in ANTENNAS:
                return False, f"Invalid antenna number: {a}"
        if rounds < 1:
            return False, "Need at least one round"
        if scan_stop_hz <= scan_start_hz:
            return False, "scan_stop_hz must be greater than scan_start_hz"
        if profile_duration_s < 3.0:
            return False, "Profile duration too short to build a useful time history"

        try:
            CSV_DIR.mkdir(parents=True, exist_ok=True)
            self.csv_path = CSV_DIR / f"ab_test_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            with open(self.csv_path, "w", newline="") as f:
                csv.writer(f).writerow(CSV_FIELDS)
        except OSError as e:
            self.csv_path = None
            return False, f"Could not create CSV file: {e}"

        self.running = True
        self.status = "running"
        self.error_message = ""
        self.results = []
        self.summary = None
        self.current_round = 0
        self.total_rounds = rounds
        self.current_antenna = None
        self.current_active_channels = 0
        self._stop_requested = False

        self._task = asyncio.create_task(self._run(
            antennas, rounds, scan_start_hz, scan_stop_hz, bandwidth_hz, profile_duration_s))
        await self._publish()
        return True, f"Test started — logging to {self.csv_path.name}"

    async def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "No test running"
        self._stop_requested = True
        return True, "Stop requested"

    async def _run(self, antennas, rounds, scan_start_hz, scan_stop_hz, bw_hz, duration_s):
        try:
            for rnd in range(1, rounds + 1):
                if self._stop_requested:
                    break
                self.current_round = rnd
                await self._publish()

                for ant in antennas:
                    if self._stop_requested:
                        break
                    ok, msg = await self._profile_antenna(
                        ant, rnd, scan_start_hz, scan_stop_hz, bw_hz, duration_s)
                    if not ok:
                        self.status = "error"
                        self.error_message = msg
                        logger.warning(f"AB test aborted: {msg}")
                        return
            self.status = "stopped" if self._stop_requested else "done"
            self.summary = self._compute_summary(antennas)
            self._write_summary_csv()
        except Exception as e:
            logger.exception("AB test crashed")
            self.status = "error"
            self.error_message = f"Unexpected error: {e}"
        finally:
            self.running = False
            self.current_antenna = None
            await self._publish()

    async def _profile_antenna(self, ant, rnd, scan_start_hz, scan_stop_hz,
                                bw_hz, duration_s) -> tuple[bool, str]:
        self.current_antenna = ant
        self.current_active_channels = 0
        await self._publish()

        ok, msg = await self.bridge.goto_antenna(ant)
        if not ok:
            return False, f"Antenna switch failed: {msg}"

        # Let the relay settle before trusting any reading off it.
        await asyncio.sleep(1.0)

        active, floor_db, err = await self._profile_band(
            scan_start_hz, scan_stop_hz, bw_hz, duration_s)
        if err:
            return False, err

        self.current_active_channels = len(active)
        ant_cfg = ANTENNAS.get(ant)
        ts = time.time()
        if not active:
            # A dead band during this antenna's window is real information
            # (not a failure) — log the floor alone so the round isn't lost.
            result = StepResult(
                round=rnd, antenna=ant,
                antenna_name=ant_cfg.name if ant_cfg else f"A{ant}",
                freq_hz=0.0, signal_db_p90=floor_db, floor_db=floor_db,
                snr_db=0.0, n_time_samples=0, ts=ts,
            )
            self.results.append(result)
            self._append_csv(result)
            await self._publish()
            return True, "ok"

        for freq_hz, p90_db, local_floor_db, n in active:
            result = StepResult(
                round=rnd, antenna=ant,
                antenna_name=ant_cfg.name if ant_cfg else f"A{ant}",
                freq_hz=freq_hz,
                signal_db_p90=round(p90_db, 2),
                floor_db=round(local_floor_db, 2),
                snr_db=round(p90_db - local_floor_db, 2),
                n_time_samples=n, ts=ts,
            )
            self.results.append(result)
            self._append_csv(result)
        await self._publish()
        return True, "ok"

    async def _profile_band(self, start_hz, stop_hz, bw_hz, duration_s):
        """
        Repeatedly sweeps every channel in [start_hz, stop_hz] for
        duration_s, reading the SDR's already-computed averaged spectrum
        each pass — building a [time, channel] history equivalent to a
        long-exposure waterfall over that span. Returns
        (active_channels, overall_floor_db, error_or_None), where
        active_channels is a list of (freq_hz, p90_db, local_floor_db, n).
        """
        span_hz = self.sdr.sample_rate_hz
        center_hz = self.sdr.rf_freq_hz
        if not (center_hz - span_hz / 2 + bw_hz <= start_hz and
                stop_hz <= center_hz + span_hz / 2 - bw_hz):
            self.sdr.set_center_freq_hz((start_hz + stop_hz) / 2)
            await asyncio.sleep(1.5)   # let the averaged frame refill after retune

        channel_freqs = np.arange(start_hz + bw_hz / 2, stop_hz, bw_hz)
        if len(channel_freqs) == 0:
            return [], -200.0, "Scan range too narrow for the given bandwidth"

        history = []
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            if self.bridge.rig.state.ptt:
                break   # TX started mid-profile — use whatever history we have so far
            row = np.array([
                (self.sdr.passband_strength_db(float(f), bw_hz) or np.nan)
                for f in channel_freqs
            ])
            history.append(row)
            await asyncio.sleep(PROFILE_SAMPLE_INTERVAL_S)

        if len(history) < 3:
            return [], -200.0, "No spectrum data across scan range (SDR not streaming, or TX active)"

        arr = np.vstack(history)   # [time, channel]
        p90 = np.nanpercentile(arr, SIGNAL_PERCENTILE, axis=0)
        med = np.nanmedian(arr, axis=0)
        overall_floor_db = float(np.nanmedian(med))

        active = []
        for f, p, m in zip(channel_freqs, p90, med):
            if p - m >= ACTIVE_CHANNEL_THRESHOLD_DB:
                active.append((float(f), float(p), float(m), len(history)))
        return active, overall_floor_db, None

    def _compute_summary(self, antennas: list[int]) -> list[dict]:
        """
        Matches channels round-by-round: within a single round, the same
        channel grid is used for every antenna, so a channel that showed up
        as active on two antennas in the *same* round is a genuine
        apples-to-apples comparison (whatever station was live on that
        channel during that ~10-30s window, heard by both antennas in
        immediate succession). Averaging that delta across every round —
        rather than trusting any single round — is what answers "which
        antenna is actually better" instead of "which antenna got lucky
        this round".
        """
        by_round: dict[int, dict[int, dict[float, StepResult]]] = {}
        for r in self.results:
            if r.freq_hz == 0.0:
                continue   # placeholder row for "no active channel this antenna/round"
            by_round.setdefault(r.round, {}).setdefault(r.antenna, {})[r.freq_hz] = r

        ref = antennas[0]
        summaries = []
        for other in antennas[1:]:
            sig_diffs, floor_diffs, snr_diffs = [], [], []
            for by_ant in by_round.values():
                ref_map = by_ant.get(ref, {})
                other_map = by_ant.get(other, {})
                for f in set(ref_map) & set(other_map):
                    a, b = ref_map[f], other_map[f]
                    sig_diffs.append(a.signal_db_p90 - b.signal_db_p90)
                    floor_diffs.append(a.floor_db - b.floor_db)
                    snr_diffs.append(a.snr_db - b.snr_db)

            if not sig_diffs:
                summaries.append({
                    "ref_antenna": ref, "compare_antenna": other, "n_matched": 0,
                    "note": "No channel was active on both antennas in the same round — "
                            "inconclusive, try more rounds or a longer profile duration",
                })
                continue
            summaries.append({
                "ref_antenna": ref, "compare_antenna": other,
                "n_matched": len(sig_diffs),
                "mean_signal_diff_db": round(float(np.mean(sig_diffs)), 2),
                "signal_diff_stdev_db": round(float(np.std(sig_diffs)), 2),
                "mean_floor_diff_db": round(float(np.mean(floor_diffs)), 2),
                "mean_snr_diff_db": round(float(np.mean(snr_diffs)), 2),
            })
        return summaries

    def _write_summary_csv(self):
        if self.csv_path is None or not self.summary:
            return
        summary_path = self.csv_path.with_name(self.csv_path.stem + "_summary.csv")
        try:
            with open(summary_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["ref_antenna", "compare_antenna", "n_matched",
                            "mean_signal_diff_db", "signal_diff_stdev_db",
                            "mean_floor_diff_db", "mean_snr_diff_db", "note"])
                for s in self.summary:
                    w.writerow([
                        s.get("ref_antenna"), s.get("compare_antenna"), s.get("n_matched"),
                        s.get("mean_signal_diff_db", ""), s.get("signal_diff_stdev_db", ""),
                        s.get("mean_floor_diff_db", ""), s.get("mean_snr_diff_db", ""),
                        s.get("note", ""),
                    ])
        except OSError as e:
            logger.error(f"Failed to write AB test summary CSV: {e}")

    def _append_csv(self, r: StepResult):
        if self.csv_path is None:
            return
        try:
            with open(self.csv_path, "a", newline="") as f:
                csv.writer(f).writerow([
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(r.ts)),
                    r.round, r.antenna, r.antenna_name, r.freq_hz,
                    r.signal_db_p90, r.floor_db, r.snr_db, r.n_time_samples,
                ])
        except OSError as e:
            logger.error(f"Failed to write AB test CSV row: {e}")
