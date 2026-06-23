"""
ACOM Bridge — Ties RigctldClient to AcomSerial

Responsibilities:
  - Watches rig frequency changes → sends band/antenna select to ACOM 1200S
  - Enforces operating mode power limits and safety interlocks
  - Surfaces a passive SWR warning (>2.5) from ACOM telemetry — never inhibits
  - Mirrors the amp's own hard/soft fault bits (message 0x21) into TX inhibit —
    real safety enforcement lives in the amp's firmware, not console heuristics
  - Enforces A4R (dummy load) 10-second TX hard cutoff
  - Publishes unified station state for WebSocket broadcast

Operating Modes:
  AMP_OFF — amp in standby (RF bypass); radio RF power capped at 100W
  AMP_ON  — amp in OPERATE; radio RF power capped at 40W, requires
            explicit operator confirmation before engaging

Antenna Configuration (w7tlg station):
  A1F — SS-25 / future DXF   1500W  all bands   unlimited
  A2F — unconnected           0W    disabled
  A3R — 40m EFHW multiband  300W   all HF
  A4R — dummy load          1500W   any         10s hard TX cutoff
"""

import asyncio
from collections import deque
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Coroutine

from amplifier.acom_protocol import (
    AmpTelemetry, FaultStatus,
    cmd_next_antenna, cmd_select_band, cmd_tx_prohibit, cmd_tx_allow,
    cmd_standby, cmd_operate, cmd_clear_soft_faults,
    freq_to_band as acom_freq_to_band, Band as AcomBand,
)
from amplifier.acom_serial import AcomSerial
from rig.rigctld_client import RigctldClient, RigState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trending
# ---------------------------------------------------------------------------

@dataclass
class TrendSample:
    """Single telemetry snapshot for time-series trending."""
    ts: float           # time.time()
    fwd_w: float
    refl_w: float
    swr: float
    temp_c: float
    drive_w: float
    current_a: float    # amps (not mA)
    is_tx: bool

    def to_list(self) -> list:
        """Compact list format for WebSocket transfer."""
        return [
            round(self.ts, 2),
            round(self.fwd_w, 1),
            round(self.refl_w, 1),
            round(self.swr, 2),
            round(self.temp_c, 1),
            round(self.drive_w, 1),
            round(self.current_a, 2),
            1 if self.is_tx else 0,
        ]

TREND_FIELDS = ["ts", "fwd_w", "refl_w", "swr", "temp_c",
                "drive_w", "current_a", "is_tx"]


# ---------------------------------------------------------------------------
# Operating modes
# ---------------------------------------------------------------------------

class OperatingMode(Enum):
    AMP_OFF = "AMP_OFF"   # Amp in standby, radio 0-100W
    AMP_ON  = "AMP_ON"    # Amp in OPR, radio 0-40W (requires confirmation)

# Radio drive limits per mode
MODE_DRIVE_LIMITS = {
    OperatingMode.AMP_OFF: 100,
    OperatingMode.AMP_ON:  40,
}

AMP_ACTIVE_MODES = {OperatingMode.AMP_ON}

# ---------------------------------------------------------------------------
# Antenna definitions
# ---------------------------------------------------------------------------

@dataclass
class AntennaConfig:
    port: str
    number: int
    name: str
    max_power_w: int
    bands: list
    enabled: bool
    dummy_load: bool

    def allows_band(self, band: AcomBand) -> bool:
        if not self.enabled:
            return False
        if not self.bands:
            return True
        return band in self.bands


ANTENNAS: dict[int, AntennaConfig] = {
    1: AntennaConfig(
        port="A1F", number=1,
        name="SS-25 / DXF Vertical",
        max_power_w=1500, bands=[], enabled=True,
        dummy_load=False,
    ),
    2: AntennaConfig(
        port="A2F", number=2,
        name="Unconnected",
        max_power_w=0, bands=[], enabled=False,
        dummy_load=False,
    ),
    3: AntennaConfig(
        port="A3R", number=3,
        name="40m EFHW Multiband",
        max_power_w=300, bands=[], enabled=True,
        dummy_load=False,
    ),
    4: AntennaConfig(
        port="A4R", number=4,
        name="Dummy Load",
        max_power_w=1500, bands=[], enabled=True,
        dummy_load=True,
    ),
}

DUMMY_LOAD_MAX_TX_S = 10.0
SWR_WARNING_THRESHOLD = 2.5
SWR_WARNING_CLEAR_THRESHOLD = 2.3

# ---------------------------------------------------------------------------
# Unified station state
# ---------------------------------------------------------------------------

@dataclass
class StationState:
    rig: dict = field(default_factory=dict)
    amp_connected: bool = False
    amp_mode: str = ""
    amp_fwd_w: float = 0.0
    amp_refl_w: float = 0.0
    amp_swr: float = 0.0
    amp_drive_w: float = 0.0
    amp_temp_c: float = 0.0
    amp_hv_v: float = 0.0
    amp_current_ma: float = 0.0
    amp_ptt_active: bool = False
    amp_atu_tuned: bool = False
    fault_severity: str = "OK"
    fault_hard: list = field(default_factory=list)
    fault_soft: list = field(default_factory=list)
    fault_warnings: list = field(default_factory=list)
    operating_mode: str = OperatingMode.AMP_OFF.value
    selected_antenna: int = 4
    drive_limit_w: int = 100
    tx_inhibited: bool = False
    tx_inhibit_reason: str = ""
    duty_cycle_pct: float = 0.0
    tx_cycle_count: int = 0
    dummy_load_active: bool = False
    dummy_load_remaining_s: float = 0.0
    swr_warning_active: bool = False
    swr_warning_peak: float = 0.0

    def to_dict(self) -> dict:
        return {
            "rig":                    self.rig,
            "amp_connected":          self.amp_connected,
            "amp_mode":               self.amp_mode,
            "amp_fwd_w":              self.amp_fwd_w,
            "amp_refl_w":             self.amp_refl_w,
            "amp_swr":                self.amp_swr,
            "amp_drive_w":            self.amp_drive_w,
            "amp_temp_c":             self.amp_temp_c,
            "amp_hv_v":               self.amp_hv_v,
            "amp_current_ma":         self.amp_current_ma,
            "amp_ptt_active":         self.amp_ptt_active,
            "amp_atu_tuned":          self.amp_atu_tuned,
            "fault_severity":         self.fault_severity,
            "fault_hard":             self.fault_hard,
            "fault_soft":             self.fault_soft,
            "fault_warnings":         self.fault_warnings,
            "operating_mode":         self.operating_mode,
            "selected_antenna":       self.selected_antenna,
            "drive_limit_w":          self.drive_limit_w,
            "tx_inhibited":           self.tx_inhibited,
            "duty_cycle_pct":         self.duty_cycle_pct,
            "tx_cycle_count":         self.tx_cycle_count,
            "tx_inhibit_reason":      self.tx_inhibit_reason,
            "dummy_load_active":      self.dummy_load_active,
            "dummy_load_remaining_s": self.dummy_load_remaining_s,
            "swr_warning_active":     self.swr_warning_active,
            "swr_warning_peak":       self.swr_warning_peak,
        }


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

StationStateCallback = Callable[[StationState], Coroutine]


class AcomBridge:
    """
    Coordinates RigctldClient and AcomSerial.
    Manages operating modes, antenna selection, and all safety interlocks.
    """

    def __init__(
        self,
        rig: RigctldClient,
        amp: AcomSerial,
    ):
        self.rig = rig
        self.amp = amp
        self.station = StationState()

        self._mode = OperatingMode.AMP_OFF
        self._selected_antenna = 4
        self._high_power_confirmed = False
        self._tx_inhibited = False
        self._swr_warning_active = False
        # Trending buffers
        self._trend_buffer = deque(maxlen=6000)   # ~10min at 10Hz
        self._duty_samples = deque(maxlen=3000)   # ~5min at 10Hz
        self._tx_cycle_count = 0
        self._tx_was_trending = False
        self._tx_inhibit_reason = ""
        self._current_acom_band: Optional[AcomBand] = None
        self._last_freq_hz: int = 0
        self._dummy_tx_start: Optional[float] = None
        self._tx_was_active: bool = False
        self._state_callbacks: list[StationStateCallback] = []

        self.rig.on_state_change(self._on_rig_state)
        self.amp.on_telemetry(self._on_telemetry)
        self.amp.on_fault(self._on_fault)
        self.amp.on_antenna_change(self._on_antenna_change)
        self.amp.on_connection_change(self._on_amp_connection)

    def on_state_change(self, cb: StationStateCallback):
        self._state_callbacks.append(cb)

    async def start(self):
        await self.rig.start()
        await self.amp.start()
        logger.info("AcomBridge started")

    async def stop(self):
        await self.rig.stop()
        await self.amp.stop()
        logger.info("AcomBridge stopped")

    async def set_operating_mode(self, mode: OperatingMode,
                                  confirmed: bool = False) -> tuple[bool, str]:
        if mode == OperatingMode.AMP_ON and not confirmed:
            return False, "AMP_ON requires explicit operator confirmation"

        self._mode = mode
        self._high_power_confirmed = confirmed

        if mode in AMP_ACTIVE_MODES:
            await self.amp.send(cmd_operate())
            logger.info("Amp → OPERATE")
        else:
            await self.amp.send(cmd_standby())
            logger.info("Amp → STANDBY")

        # Enforce the new drive limit immediately: if the radio is currently
        # set above the new cap (e.g. was at 80W with amp off, now switching
        # to AMP_ON's 40W cap), bring it down rather than letting an
        # out-of-range setting persist into the new mode.
        new_limit = MODE_DRIVE_LIMITS[mode]
        if self.rig.state.rf_power_pct > new_limit:
            await self.rig.set_rf_power(new_limit)
            logger.info(f"RF power clamped to {new_limit}W for {mode.value}")

        logger.info(f"Operating mode → {mode.value}")
        await self._publish()
        return True, f"Mode set to {mode.value}"

    async def next_antenna(self) -> tuple[bool, str]:
        """
        Cycle to the next antenna — same action as the amp's front-panel
        ANT button. The A1200S has no command to jump directly to a given
        antenna, so the console can only nudge forward and wait for the
        amp's own ANT_BAND_INFO (0x27) feedback to learn which antenna it
        landed on (see _on_antenna_change).
        """
        await self.amp.send(cmd_next_antenna())
        logger.info("Sent NEXT ANTENNA (front-panel ANT button equivalent)")
        return True, "Antenna cycle requested"

    async def inhibit_tx(self, reason: str):
        if not self._tx_inhibited:
            self._tx_inhibited = True
            self._tx_inhibit_reason = reason
            await self.amp.send(cmd_tx_prohibit())
            logger.warning(f"TX inhibited: {reason}")
            await self._publish()

    async def allow_tx(self):
        if self._tx_inhibited:
            self._tx_inhibited = False
            self._tx_inhibit_reason = ""
            if self.station.fault_soft:
                # Soft faults can latch on the amp's own side (e.g. "PA LOAD
                # SWR TOO HIGH") — clearing only the console's flag would let
                # the next fault-status read immediately re-trip _on_fault.
                # Ask the amp to clear its own soft faults at the source.
                await self.amp.send(cmd_clear_soft_faults())
                logger.info("Sent CLEAR_SOFT_FAULTS to amp")
            await self.amp.send(cmd_tx_allow())
            logger.info("TX inhibit cleared")
            await self._publish()

    async def _on_rig_state(self, rig: RigState):
        self.station.rig = rig.to_dict()

        if rig.freq_hz != self._last_freq_hz and rig.freq_hz > 0:
            self._last_freq_hz = rig.freq_hz
            await self._handle_freq_change(rig.freq_hz, rig.band)

        if rig.ptt and not self._tx_was_active:
            await self._on_tx_start()
        elif not rig.ptt and self._tx_was_active:
            await self._on_tx_end()
        self._tx_was_active = rig.ptt

        await self._publish()

    async def _handle_freq_change(self, freq_hz: int, band_name: str):
        new_band = acom_freq_to_band(freq_hz)
        if new_band == self._current_acom_band:
            return
        self._current_acom_band = new_band
        if new_band is None:
            logger.warning(f"Frequency {freq_hz} Hz out of ACOM band range")
            return
        # Keeps the amp's band/LPF tracking the radio even while in
        # STANDBY (no drive RF for the amp's own F-counter to detect band
        # from) — confirmed on real hardware, despite not being in the
        # documented v1.3 cycle-code list for this sub-command.
        await self.amp.send(cmd_select_band(new_band))
        logger.info(f"Band → {band_name}: sent amp band select {new_band.name}")

    async def _on_tx_start(self):
        logger.info("TX start detected")
        ant_config = ANTENNAS.get(self._selected_antenna)
        if not ant_config:
            return

        if ant_config.dummy_load:
            self._dummy_tx_start = time.monotonic()
            asyncio.create_task(self._dummy_load_watchdog())

    async def _on_tx_end(self):
        logger.info("TX end detected")
        self._dummy_tx_start = None
        self.station.dummy_load_active = False
        self.station.dummy_load_remaining_s = 0.0

    async def _dummy_load_watchdog(self):
        start = self._dummy_tx_start
        self.station.dummy_load_active = True
        while self._dummy_tx_start == start and self._tx_was_active:
            elapsed = time.monotonic() - start
            remaining = DUMMY_LOAD_MAX_TX_S - elapsed
            self.station.dummy_load_remaining_s = max(0.0, remaining)
            await self._publish()
            if elapsed >= DUMMY_LOAD_MAX_TX_S:
                logger.warning("Dummy load 10s limit — inhibiting TX")
                await self.inhibit_tx("Dummy load 10s limit reached")
                break
            await asyncio.sleep(0.25)
        self.station.dummy_load_active = False
        self.station.dummy_load_remaining_s = 0.0


    def _calc_duty_cycle(self) -> float:
        """Rolling 5-minute TX duty cycle percentage."""
        if not self._duty_samples:
            return 0.0
        now = time.time()
        cutoff = now - 300  # 5 minute window
        while self._duty_samples and self._duty_samples[0][0] < cutoff:
            self._duty_samples.popleft()
        if not self._duty_samples:
            return 0.0
        tx_count = sum(1 for _, is_tx in self._duty_samples if is_tx)
        return round(100.0 * tx_count / len(self._duty_samples), 1)

    def get_trend_data(self, since: float = 0) -> dict:
        """Return trend samples since a given timestamp."""
        samples = [s.to_list() for s in self._trend_buffer if s.ts > since]
        return {
            "fields": TREND_FIELDS,
            "samples": samples,
            "duty_cycle_pct": self.station.duty_cycle_pct,
            "tx_cycle_count": self._tx_cycle_count,
        }

    async def _on_telemetry(self, t: AmpTelemetry):
        self.station.amp_mode       = t.mode_name
        self.station.amp_fwd_w      = t.fwd_power_w
        self.station.amp_refl_w     = t.refl_power_w
        self.station.amp_swr        = t.swr
        self.station.amp_drive_w    = t.input_power_w
        self.station.amp_temp_c     = t.pam1_temp_c
        self.station.amp_hv_v       = t.hv1_v
        self.station.amp_current_ma = t.id1_ma
        self.station.amp_ptt_active = t.flag_keyin
        self.station.amp_atu_tuned  = t.flag_atu_tuned

        # SWR is purely a passive notification to the operator — never an
        # auto-inhibit. The amp's own firmware-computed fault bits (handled
        # in _on_fault) are the real, robust protection; this is just a
        # heads-up. Edge-triggered with hysteresis so it doesn't chatter
        # right at the threshold.
        if self._tx_was_active and t.swr >= SWR_WARNING_THRESHOLD:
            if not self._swr_warning_active:
                self._swr_warning_active = True
                logger.warning(
                    f"SWR {t.swr:.2f} exceeded {SWR_WARNING_THRESHOLD} warning threshold")
            self.station.swr_warning_peak = max(self.station.swr_warning_peak, t.swr)
        elif not self._tx_was_active or t.swr <= SWR_WARNING_CLEAR_THRESHOLD:
            self._swr_warning_active = False
            self.station.swr_warning_peak = 0.0
        self.station.swr_warning_active = self._swr_warning_active

        # ---- Trending sample collection ----
        now = time.time()
        is_tx = t.flag_keyin
        sample = TrendSample(
            ts=now, fwd_w=t.fwd_power_w, refl_w=t.refl_power_w,
            swr=t.swr, temp_c=t.pam1_temp_c, drive_w=t.input_power_w,
            current_a=t.id1_ma / 1000.0, is_tx=is_tx,
        )
        self._trend_buffer.append(sample)
        self._duty_samples.append((now, is_tx))
        # TX cycle counting
        if is_tx and not self._tx_was_trending:
            self._tx_cycle_count += 1
        self._tx_was_trending = is_tx
        self.station.duty_cycle_pct = self._calc_duty_cycle()
        self.station.tx_cycle_count = self._tx_cycle_count

        await self._publish()

    async def _on_fault(self, faults: FaultStatus):
        self.station.fault_severity = faults.severity
        self.station.fault_hard     = faults.hard_faults
        self.station.fault_soft     = faults.soft_faults
        self.station.fault_warnings = faults.warnings

        if faults.has_hard_fault:
            await self.inhibit_tx(
                f"ACOM HARD FAULT: {', '.join(faults.hard_faults)}")
        elif faults.has_soft_fault:
            await self.inhibit_tx(
                f"ACOM SOFT FAULT: {', '.join(faults.soft_faults)}")

        await self._publish()

    async def _on_antenna_change(self, ant_num: int, ant_type_byte: int):
        """Called when amp sends ANT_BAND_INFO (0x27) — sync console indicator.
        For this amp's 4 built-in relay antennas, the live 0x27 message
        reports ant_num 0-indexed (Ant0..Ant3) — confirmed against real
        hardware (front-panel toggling lit the wrong indicator without this
        +1). The protocol doc's "[1..10]" wording describes the ASEL
        accessory case; doesn't hold for the built-in relays."""
        logger.debug(f"Amp antenna sync: A{ant_num}, type_byte=0x{ant_type_byte:02X}")
        self._selected_antenna = ant_num + 1
        await self._publish()

    async def _on_amp_connection(self, connected: bool):
        self.station.amp_connected = connected
        if not connected:
            await self.inhibit_tx("ACOM serial connection lost")
        else:
            await self.set_operating_mode(
                self._mode, self._high_power_confirmed)
        await self._publish()

    async def _publish(self):
        self.station.tx_inhibited      = self._tx_inhibited
        self.station.tx_inhibit_reason = self._tx_inhibit_reason
        self.station.operating_mode    = self._mode.value
        self.station.drive_limit_w     = MODE_DRIVE_LIMITS[self._mode]
        self.station.selected_antenna  = self._selected_antenna
        for cb in self._state_callbacks:
            try:
                await cb(self.station)
            except Exception as e:
                logger.error(f"Station state callback error: {e}")
