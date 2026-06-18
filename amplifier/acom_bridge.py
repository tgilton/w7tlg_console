"""
ACOM Bridge — Ties RigctldClient to AcomSerial

Responsibilities:
  - Watches rig frequency changes → sends band/antenna select to ACOM 1200S
  - Enforces operating mode power limits and safety interlocks
  - Monitors ACOM telemetry → reflected power watchdog for A3R (EFHW)
  - Enforces A4R (dummy load) 10-second TX hard cutoff
  - Publishes unified station state for WebSocket broadcast

Operating Modes:
  QRP       — amp bypassed, drive ≤ 5W
  BAREFOOT  — amp bypassed, drive ≤ 100W
  NORMAL    — amp active, drive ≤ 40W, no confirmation
  HIGH_POWER— amp active, drive ≤ 40W, requires explicit confirmation

Antenna Configuration (w7tlg station):
  A1F — SS-25 / future DXF   1500W  all bands   unlimited
  A2F — unconnected           0W    disabled
  A3R — 40m EFHW multiband  300W   all HF      reflected power watchdog
  A4R — dummy load          1500W   any         10s hard TX cutoff
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Coroutine

from amplifier.acom_protocol import (
    AmpTelemetry, FaultStatus,
    cmd_select_antenna_band, cmd_tx_prohibit, cmd_tx_allow,
    cmd_standby, cmd_operate,
    freq_to_band as acom_freq_to_band, Band as AcomBand,
)
from amplifier.acom_serial import AcomSerial
from rig.rigctld_client import RigctldClient, RigState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Operating modes
# ---------------------------------------------------------------------------

class OperatingMode(Enum):
    QRP        = "QRP"
    BAREFOOT   = "BAREFOOT"
    NORMAL     = "NORMAL"
    HIGH_POWER = "HIGH_POWER"

MODE_DRIVE_LIMITS = {
    OperatingMode.QRP:        5,
    OperatingMode.BAREFOOT:   100,
    OperatingMode.NORMAL:     40,
    OperatingMode.HIGH_POWER: 40,
}

AMP_ACTIVE_MODES = {OperatingMode.NORMAL, OperatingMode.HIGH_POWER}

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
    refl_watchdog: bool

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
        dummy_load=False, refl_watchdog=False,
    ),
    2: AntennaConfig(
        port="A2F", number=2,
        name="Unconnected",
        max_power_w=0, bands=[], enabled=False,
        dummy_load=False, refl_watchdog=False,
    ),
    3: AntennaConfig(
        port="A3R", number=3,
        name="40m EFHW Multiband",
        max_power_w=300, bands=[], enabled=True,
        dummy_load=False, refl_watchdog=True,
    ),
    4: AntennaConfig(
        port="A4R", number=4,
        name="Dummy Load",
        max_power_w=1500, bands=[], enabled=True,
        dummy_load=True, refl_watchdog=False,
    ),
}

DUMMY_LOAD_MAX_TX_S = 10.0
REFL_WATCHDOG_RATIO = 2.0

# ---------------------------------------------------------------------------
# Thermal state
# ---------------------------------------------------------------------------

@dataclass
class AntennaThermState:
    antenna_number: int
    inhibited: bool = False
    inhibited_at: Optional[str] = None
    inhibited_reason: str = ""
    ambient_temp_c: Optional[float] = None
    refl_baseline: dict = field(default_factory=dict)
    operator_cleared: bool = False

    def to_dict(self) -> dict:
        return {
            "antenna_number":   self.antenna_number,
            "inhibited":        self.inhibited,
            "inhibited_at":     self.inhibited_at,
            "inhibited_reason": self.inhibited_reason,
            "ambient_temp_c":   self.ambient_temp_c,
            "refl_baseline":    self.refl_baseline,
            "operator_cleared": self.operator_cleared,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AntennaThermState":
        obj = cls(antenna_number=d.get("antenna_number", 0))
        obj.inhibited        = d.get("inhibited", False)
        obj.inhibited_at     = d.get("inhibited_at")
        obj.inhibited_reason = d.get("inhibited_reason", "")
        obj.ambient_temp_c   = d.get("ambient_temp_c")
        obj.refl_baseline    = d.get("refl_baseline", {})
        obj.operator_cleared = d.get("operator_cleared", False)
        return obj


class ThermalStateManager:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.states: dict[int, AntennaThermState] = {}
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                for ant_num, d in data.items():
                    self.states[int(ant_num)] = AntennaThermState.from_dict(d)
                logger.info(f"Loaded thermal state from {self.state_file}")
            except Exception as e:
                logger.warning(f"Could not load thermal state: {e}")

    def save(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {str(k): v.to_dict() for k, v in self.states.items()}
            self.state_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Could not save thermal state: {e}")

    def get(self, antenna_number: int) -> AntennaThermState:
        if antenna_number not in self.states:
            self.states[antenna_number] = AntennaThermState(
                antenna_number=antenna_number)
        return self.states[antenna_number]

    def inhibit(self, antenna_number: int, reason: str,
                ambient_temp_c: Optional[float] = None):
        state = self.get(antenna_number)
        state.inhibited        = True
        state.inhibited_at     = datetime.now(timezone.utc).isoformat()
        state.inhibited_reason = reason
        state.ambient_temp_c   = ambient_temp_c
        state.operator_cleared = False
        self.save()
        logger.warning(f"Antenna {antenna_number} thermally inhibited: {reason}")

    def clear_inhibit(self, antenna_number: int):
        state = self.get(antenna_number)
        state.inhibited        = False
        state.inhibited_reason = ""
        state.refl_baseline    = {}
        state.operator_cleared = True
        self.save()
        logger.info(f"Antenna {antenna_number} thermal inhibit cleared by operator")

    def set_refl_baseline(self, antenna_number: int,
                          band_name: str, refl_w: float):
        state = self.get(antenna_number)
        if band_name not in state.refl_baseline:
            state.refl_baseline[band_name] = refl_w
            self.save()
            logger.info(
                f"Ant {antenna_number} {band_name} refl baseline: {refl_w:.1f}W")

    def check_refl_watchdog(self, antenna_number: int,
                            band_name: str, refl_w: float) -> bool:
        state = self.get(antenna_number)
        baseline = state.refl_baseline.get(band_name)
        if baseline is None or baseline < 1.0:
            return False
        return refl_w >= baseline * REFL_WATCHDOG_RATIO


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
    operating_mode: str = OperatingMode.BAREFOOT.value
    selected_antenna: int = 1
    drive_limit_w: int = 100
    tx_inhibited: bool = False
    tx_inhibit_reason: str = ""
    dummy_load_active: bool = False
    dummy_load_remaining_s: float = 0.0
    thermal_inhibit: dict = field(default_factory=dict)

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
            "tx_inhibit_reason":      self.tx_inhibit_reason,
            "dummy_load_active":      self.dummy_load_active,
            "dummy_load_remaining_s": self.dummy_load_remaining_s,
            "thermal_inhibit":        self.thermal_inhibit,
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
        state_file: Path = Path("config/thermal_state.json"),
    ):
        self.rig = rig
        self.amp = amp
        self.thermal = ThermalStateManager(state_file)
        self.station = StationState()

        self._mode = OperatingMode.BAREFOOT
        self._selected_antenna = 1
        self._high_power_confirmed = False
        self._tx_inhibited = False
        self._tx_inhibit_reason = ""
        self._current_acom_band: Optional[AcomBand] = None
        self._last_freq_hz: int = 0
        self._dummy_tx_start: Optional[float] = None
        self._tx_was_active: bool = False
        self._state_callbacks: list[StationStateCallback] = []

        self.rig.on_state_change(self._on_rig_state)
        self.amp.on_telemetry(self._on_telemetry)
        self.amp.on_fault(self._on_fault)
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
        if mode == OperatingMode.HIGH_POWER and not confirmed:
            return False, "HIGH_POWER requires explicit operator confirmation"

        self._mode = mode
        self._high_power_confirmed = confirmed

        if mode in AMP_ACTIVE_MODES:
            await self.amp.send(cmd_operate())
        else:
            await self.amp.send(cmd_standby())

        logger.info(f"Operating mode → {mode.value}")
        await self._publish()
        return True, f"Mode set to {mode.value}"

    async def select_antenna(self, antenna_number: int) -> tuple[bool, str]:
        config = ANTENNAS.get(antenna_number)
        if not config:
            return False, f"Unknown antenna {antenna_number}"
        if not config.enabled:
            return False, f"{config.port} ({config.name}) is disabled"

        therm = self.thermal.get(antenna_number)
        if therm.inhibited:
            return False, (
                f"{config.name} is thermally inhibited since "
                f"{therm.inhibited_at}. "
                f"Reason: {therm.inhibited_reason}. "
                f"Operator must clear before use.")

        self._selected_antenna = antenna_number

        if self._current_acom_band:
            cmd = cmd_select_antenna_band(antenna_number, self._current_acom_band)
            await self.amp.send(cmd)

        logger.info(f"Antenna → {config.port} ({config.name})")
        await self._publish()
        return True, f"Antenna {config.port} selected"

    async def operator_clear_thermal(self, antenna_number: int):
        self.thermal.clear_inhibit(antenna_number)
        await self._publish()

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
        ant_config = ANTENNAS.get(self._selected_antenna)
        if ant_config and ant_config.enabled:
            cmd = cmd_select_antenna_band(self._selected_antenna, new_band)
            await self.amp.send(cmd)
            logger.info(
                f"Band → {band_name}: sent ANT{self._selected_antenna} "
                f"({ant_config.port}) band {new_band.name}")

    async def _on_tx_start(self):
        logger.info("TX start detected")
        ant_config = ANTENNAS.get(self._selected_antenna)
        if not ant_config:
            return

        therm = self.thermal.get(self._selected_antenna)
        if therm.inhibited:
            await self.inhibit_tx(f"{ant_config.name} is thermally inhibited")
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

        ant_config = ANTENNAS.get(self._selected_antenna)
        if (ant_config and ant_config.refl_watchdog
                and self._tx_was_active
                and self._current_acom_band):
            band_name = self._current_acom_band.name
            refl = t.refl_power_w
            self.thermal.set_refl_baseline(
                self._selected_antenna, band_name, refl)
            if self.thermal.check_refl_watchdog(
                    self._selected_antenna, band_name, refl):
                reason = (
                    f"Reflected power {refl:.0f}W exceeded 2x baseline "
                    f"on {band_name} — antenna may be overheating")
                await self.inhibit_tx(reason)
                self.thermal.inhibit(self._selected_antenna, reason)

        self.station.thermal_inhibit = {
            str(k): v.to_dict() for k, v in self.thermal.states.items()
        }
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

    async def _on_amp_connection(self, connected: bool):
        self.station.amp_connected = connected
        if not connected:
            await self.inhibit_tx("ACOM serial connection lost")
        else:
            logger.info("ACOM connected — waiting for amp to stabilize")
            await asyncio.sleep(3.0)
            await self.set_operating_mode(self._mode, self._high_power_confirmed)
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
