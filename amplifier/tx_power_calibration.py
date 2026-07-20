"""
TX Power Calibration — dummy-load-only sweep of the FT-991A's RF power
ceiling, capturing the amp's actual (fwd_w, alc, drive_w) response at each
step to build a per-band lookup table for the TX-power-target closed loop's
pre-positioning step.

Operator-driven, not automated: this module sets each ceiling step and
waits for the operator's own transmissions (e.g. WSJT-X CQ cycling) — it
does not key the radio itself. rig.set_ptt() exists in RigctldClient but
has never actually been used anywhere in this codebase; keeping a human at
the key for every RF-producing burst matches how every transmission in
this project has worked so far, and was an explicit choice over
automating PTT.

Sampling happens near the END of each transmission, not early in it — the
actual output at a given ceiling also depends on where WSJT-X's own Pwr
slider is set, which the operator tunes live (watching ALC) during the
burst. Each ceiling step runs for several transmission cycles
(cycles_per_step) rather than just one, giving the operator room to dial
ALC in across multiple attempts rather than needing it right the first
time.

Safety: hard-refuses to start unless the currently selected antenna is a
dummy load (see amplifier.acom_bridge.ANTENNAS). This module doesn't
bypass or duplicate AcomBridge's own dummy-load power/duration curve
(DUMMY_LOAD_CURVE) or the antenna's own absolute ceiling (ANTENNAS[4]
.max_power_w) — those stay fully active throughout every step; this just
rides on top of them.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Coroutine, Optional

from amplifier.acom_bridge import AcomBridge, ANTENNAS

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "tx_calibration"

# Ignore telemetry for this long after TX starts before recording readings
# — same reasoning as the 3-frame mode-sync hysteresis in
# AcomBridge._on_telemetry, avoids the ramp transient.
SETTLE_S = 0.2
# The captured point for a cycle is the average of readings within this
# many seconds of TX ending — "however things looked by the end of the
# burst", after the operator's had the whole transmission to tune ALC.
TAIL_WINDOW_S = 1.5
# How many transmission cycles to sample at each ceiling step before
# advancing — gives the operator multiple attempts to tune WSJT-X's Pwr
# slider toward a good ALC rather than needing it right the first time.
CYCLES_PER_STEP_DEFAULT = 3
# How long to wait for the operator to key up before giving up on a cycle.
KEY_UP_TIMEOUT_S = 45.0

DEFAULT_CEILING_STEPS_W = [2, 4, 6, 8, 10, 15, 20, 25, 30, 35, 40]

StatusCallback = Callable[[dict], Coroutine]


@dataclass
class CalibrationPoint:
    ceiling_w: int
    cycle: int
    fwd_w: float
    alc: float
    drive_w: float
    ts: float


class TxPowerCalibration:
    """Sweeps rf_power_pct across a list of steps on the dummy load. Each
    step runs for cycles_per_step transmission cycles, sampling the tail
    end of each one, so the operator can tune ALC across a few attempts.
    Results persist per-band to data/tx_calibration/<band>.json — the last
    cycle sampled at each ceiling wins (presumed best-tuned), merged with
    any existing points for that band rather than overwriting the file."""

    def __init__(self, bridge: AcomBridge):
        self.bridge = bridge
        self.running = False
        self.status = "idle"   # idle | running | done | error | stopped
        self.error_message = ""
        self.waiting_for_tx = False
        self.band_name = ""
        self.steps: list[int] = []
        self.step_index = 0
        self.cycles_per_step = CYCLES_PER_STEP_DEFAULT
        self.cycle_index = 0
        self.points: list[CalibrationPoint] = []
        self.csv_path: Optional[Path] = None
        self._task: Optional[asyncio.Task] = None
        self._status_callbacks: list[StatusCallback] = []

    def on_status(self, cb: StatusCallback):
        self._status_callbacks.append(cb)

    async def _publish(self):
        status = self.get_status()
        for cb in self._status_callbacks:
            try:
                await cb(status)
            except Exception as e:
                logger.error(f"Calibration status callback error: {e}")

    def get_status(self) -> dict:
        return {
            "status": self.status,
            "error": self.error_message,
            "waiting_for_tx": self.waiting_for_tx,
            "band": self.band_name,
            "step_index": self.step_index,
            "total_steps": len(self.steps),
            "current_ceiling_w": (self.steps[self.step_index]
                                   if self.running and self.step_index < len(self.steps) else None),
            "cycle_index": self.cycle_index,
            "cycles_per_step": self.cycles_per_step,
            "points": [asdict(p) for p in self.points],
            "path": str(self.csv_path) if self.csv_path else None,
        }

    async def start(self, steps: Optional[list[int]] = None,
                     cycles_per_step: int = CYCLES_PER_STEP_DEFAULT) -> tuple[bool, str]:
        if self.running:
            return False, "Calibration already running"

        ant = ANTENNAS.get(self.bridge.station.selected_antenna)
        if not ant or not ant.dummy_load:
            return False, "Calibration requires the dummy load antenna (A4R) to be selected"

        band = self.bridge.rig.state.band
        if not band or band == "??":
            return False, "No band detected — tune the radio to a band first"

        requested = sorted(steps or DEFAULT_CEILING_STEPS_W)
        max_step = self.bridge.station.drive_limit_w
        self.steps = [s for s in requested if s <= max_step]
        if not self.steps:
            return False, f"No calibration steps fit under the current drive limit ({max_step}W)"

        self.step_index = 0
        self.cycle_index = 0
        self.cycles_per_step = max(1, cycles_per_step)
        self.points = []
        self.band_name = band
        self.status = "running"
        self.error_message = ""
        self.running = True
        await self._publish()
        self._task = asyncio.create_task(self._run())
        return True, (f"Calibration started: {len(self.steps)} steps x "
                       f"{self.cycles_per_step} cycles on {band}")

    async def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "Not running"
        self.running = False
        self.status = "stopped"
        self.waiting_for_tx = False
        if self._task:
            self._task.cancel()
        await self._publish()
        return True, "Calibration stopped"

    async def _run(self):
        try:
            for i, ceiling in enumerate(self.steps):
                if not self.running:
                    break
                self.step_index = i

                # Re-verify the safety gate every step, not just at start —
                # antenna or band could change mid-sweep.
                ant = ANTENNAS.get(self.bridge.station.selected_antenna)
                if not ant or not ant.dummy_load:
                    self.status = "error"
                    self.error_message = "Antenna changed away from dummy load — aborted"
                    await self._publish()
                    return
                if self.bridge.rig.state.band != self.band_name:
                    self.status = "error"
                    self.error_message = "Band changed mid-sweep — aborted"
                    await self._publish()
                    return

                await self.bridge.rig.set_rf_power(ceiling)

                for cycle in range(self.cycles_per_step):
                    if not self.running:
                        break
                    self.cycle_index = cycle
                    self.waiting_for_tx = True
                    await self._publish()

                    point = await self._capture_cycle_end(ceiling, cycle)
                    self.waiting_for_tx = False
                    if point is not None:
                        self.points.append(point)
                    await self._publish()

            if self.running:
                self._save_results()
                self.status = "done"
                await self._publish()
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            self.waiting_for_tx = False

    async def _capture_cycle_end(self, ceiling: int, cycle: int) -> Optional[CalibrationPoint]:
        """Waits for a fresh TX cycle (PTT false -> true, not just
        "currently true" — a transmission already underway when the
        ceiling changed must not get sampled), records readings throughout
        it, and returns the average of the last TAIL_WINDOW_S right before
        PTT drops. Returns None if the operator doesn't key up within
        KEY_UP_TIMEOUT_S — the cycle is simply skipped."""
        deadline = time.monotonic() + KEY_UP_TIMEOUT_S

        while self.bridge.rig.state.ptt:
            if not self.running or time.monotonic() > deadline:
                return None
            await asyncio.sleep(0.05)
        while not self.bridge.rig.state.ptt:
            if not self.running or time.monotonic() > deadline:
                return None
            await asyncio.sleep(0.05)

        tx_start = time.monotonic()
        readings: list[tuple[float, float, float, float]] = []   # (t, fwd_w, alc, drive_w)
        while self.bridge.rig.state.ptt:
            if not self.running:
                return None
            elapsed = time.monotonic() - tx_start
            if elapsed >= SETTLE_S:
                readings.append((
                    elapsed,
                    self.bridge.station.amp_fwd_w,
                    self.bridge.rig.state.alc,
                    self.bridge.station.amp_drive_w,
                ))
            await asyncio.sleep(0.1)

        if not readings:
            return None

        tail_start = readings[-1][0] - TAIL_WINDOW_S
        tail = [r for r in readings if r[0] >= tail_start] or [readings[-1]]
        return CalibrationPoint(
            ceiling_w=ceiling,
            cycle=cycle,
            fwd_w=sum(r[1] for r in tail) / len(tail),
            alc=sum(r[2] for r in tail) / len(tail),
            drive_w=sum(r[3] for r in tail) / len(tail),
            ts=time.time(),
        )

    def _save_results(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / f"{self.band_name}.json"
        existing: list[dict] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                existing = []
        by_ceiling = {p["ceiling_w"]: p for p in existing}
        for p in self.points:
            by_ceiling[p.ceiling_w] = asdict(p)   # last cycle at this ceiling wins
        merged = sorted(by_ceiling.values(), key=lambda p: p["ceiling_w"])
        path.write_text(json.dumps(merged, indent=2))
        self.csv_path = path
        logger.info(f"Calibration: saved {len(merged)} points to {path}")
