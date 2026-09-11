"""
W7TLG Console — FastAPI Server

Serves the dashboard UI and manages WebSocket connections.
All station state flows through here to connected browsers.

Endpoints:
  GET  /              — Dashboard HTML
  WS   /ws            — WebSocket: station state stream + command channel
  POST /api/mode         — Set operating mode
  POST /api/antenna/next — Cycle to next antenna (front-panel ANT button)
  POST /api/tx           — TX inhibit / allow
  GET  /api/state     — Current state snapshot (REST)
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from amplifier.acom_bridge import AcomBridge, ANTENNAS, OperatingMode, StationState
from amplifier.acom_serial import AcomSerial, find_acom_port
from amplifier.antenna_ab_test import AntennaAbTest
from amplifier.tx_power_calibration import TxPowerCalibration
from amplifier.trend_csv_logger import TrendCsvLogger
from config.station_profile import station_profile
from rig.rigctld_client import RigctldClient
from sdr.sdr_client import SdrClient
from session.session_manager import SessionManager
from session.session_profiles import PROFILES
from wsjtx.udp_listener import wsjtx_listener
from wsjtx.protocol import Status as WsjtxStatus, LoggedAdif
from wsjtx.qso_logger import qso_telemetry_logger
from wsjtx.award_tracker import award_tracker
from wsjtx.spotter import spotter, SpotAlert
from wsjtx.dx_cluster import RbnClient, DxClusterClient
from wsjtx.callsign_lookup import callsign_lookup
from advisor.propagation import propagation_source
from advisor.monitor import propagation_monitor
from advisor.claude_advisor import ClaudeAdvisor

logger = logging.getLogger(__name__)

RIGCTLD_HOST  = "127.0.0.1"
RIGCTLD_PORT  = 4532
# ACOM_PORT     = "/dev/cu.usbserial-A9V19CH7"
ACOM_PORT = "/dev/cu.usbserial-A92518IM"
ACOM_BAUD     = 9600

# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"WebSocket connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.info(f"WebSocket disconnected. Total: {len(self.active)}")

    async def broadcast(self, data: dict):
        if not self.active:
            return
        message = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)


class SpectrumConnectionManager:
    """
    Separate from ConnectionManager: spectrum frames are far larger and
    more frequent than state broadcasts, and it's correct to drop a frame
    to a slow client rather than block — never appropriate for `state`.
    """
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"Spectrum WebSocket connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.info(f"Spectrum WebSocket disconnected. Total: {len(self.active)}")

    async def broadcast_frame(self, frame: dict):
        if not self.active:
            return
        header = json.dumps({
            "type": "spectrum",
            "kind": frame.get("kind", "wide"),
            "ts": frame["ts"],
            "center_freq_hz": frame["center_freq_hz"],
            "span_hz": frame["span_hz"],
            "sample_rate_hz": frame["sample_rate_hz"],
            "bin_count": len(frame["data"]),
        })
        payload = frame["data"].astype("float32").tobytes()
        dead = []
        for ws in self.active:
            try:
                await asyncio.wait_for(ws.send_text(header), timeout=0.05)
                await asyncio.wait_for(ws.send_bytes(payload), timeout=0.05)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)


class AudioConnectionManager:
    """Live PCM audio — drop-if-slow like spectrum, never buffer stale audio."""
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"Audio WebSocket connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.info(f"Audio WebSocket disconnected. Total: {len(self.active)}")

    async def broadcast_audio(self, audio_bytes: bytes):
        if not self.active:
            return
        dead = []
        for ws in self.active:
            try:
                await asyncio.wait_for(ws.send_bytes(audio_bytes), timeout=0.05)
            except asyncio.TimeoutError:
                pass   # one slow frame, not a dead connection — just skip it
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)



manager = ConnectionManager()
spectrum_manager = SpectrumConnectionManager()
audio_manager = AudioConnectionManager()
# Channel B (Tuner 2 / Antenna 2) gets its own connection managers — kept
# fully separate from Channel A's rather than multiplexed onto the same
# WS connections, matching how SdrClient itself keeps Channel B's
# publish path isolated (see project memory on the RSPduo migration).
spectrum_manager_b = SpectrumConnectionManager()
audio_manager_b = AudioConnectionManager()
# RX0 diversity combine (dashboard/diversity.html) — audio only, no
# spectrum of its own. See sdr/combiner.py.
audio_manager_0 = AudioConnectionManager()
spectrum_manager_0 = SpectrumConnectionManager()
# See subscribe_fine_spectrum/unsubscribe_fine_spectrum in handle_ws_command —
# ref-counts real demand for AudioDemodulator.fine_spectrum_enabled instead of
# it running unconditionally.
_fine_spectrum_subscribers: set[WebSocket] = set()
bridge: Optional[AcomBridge] = None
sdr: Optional[SdrClient] = None
ab_test: Optional[AntennaAbTest] = None
tx_cal: Optional[TxPowerCalibration] = None
session_manager: Optional[SessionManager] = None
claude_advisor: Optional["ClaudeAdvisor"] = None
trend_csv = TrendCsvLogger()

# Monitor-page liveness, used to gate trend CSV logging. Heartbeat-based
# (not tied to a specific WebSocket) because /monitor shares /ws with the
# dashboard — see trend_csv_logger.py docstring.
_MONITOR_HEARTBEAT_TIMEOUT_S = 10.0
_last_monitor_heartbeat: float = 0.0

# Which channel currently feeds the single BlackHole digital-audio cable —
# tracked here rather than inferred from AudioDemodulator's private
# callback list (see set_digital_source). Defaults to "A", matching the
# fixed subscription every session started with before source-select existed.
_digital_source_channel: str = "A"


async def _monitor_liveness_watcher():
    while True:
        await asyncio.sleep(2.0)
        alive = (time.time() - _last_monitor_heartbeat) < _MONITOR_HEARTBEAT_TIMEOUT_S
        if alive and not trend_csv.active:
            trend_csv.start()
        elif not alive and trend_csv.active:
            trend_csv.stop()


async def on_ab_test_status(status: dict):
    await manager.broadcast({"type": "ab_test_status", "data": status})


async def on_tx_cal_status(status: dict):
    await manager.broadcast({"type": "tx_cal_status", "data": status})


async def on_session_status(status: dict):
    await manager.broadcast({"type": "session_status", "data": status})


async def _propagation_poll_loop():
    """Refresh propagation data every 3 minutes (PSKReporter's own
    requested minimum interval), broadcast it, and check for band-opening/
    closing/Kp-spike alerts — ported from ft991a-panel's poll_propagation().
    A slow/erroring PSKReporter or NOAA fetch must not affect rig/amp
    polling at all; this loop is fully independent of those."""
    await asyncio.sleep(10.0)  # let the rest of startup settle first
    while True:
        try:
            state = await propagation_source.get_state()
            await manager.broadcast({"type": "propagation", "data": state})

            bands = state.get("bands", {})
            kp = state.get("solar", {}).get("kp")
            alerts = propagation_monitor.detect_changes(bands, kp)
            if alerts:
                priority = {"highband": 4, "opening": 3, "kp_spike": 2, "closing": 1}
                primary = sorted(alerts, key=lambda a: priority.get(a["type"], 0), reverse=True)[0]
                try:
                    explanation = await asyncio.to_thread(
                        propagation_monitor.explain_alert, primary, state
                    )
                except Exception as e:
                    logger.warning(f"Alert explanation failed: {e}")
                    explanation = primary["message"]
                if len(alerts) > 1:
                    others = [a["message"] for a in alerts if a is not primary]
                    explanation += " Also: " + "; ".join(others) + "."
                await manager.broadcast({
                    "type": "propagation_alert",
                    "data": {"alert": primary, "explanation": explanation},
                })
        except Exception as e:
            logger.warning(f"Propagation poll error: {e}")
        await asyncio.sleep(180.0)


# Port aliases for display — sourced from acom_bridge.ANTENNAS (the same
# config that drives the A4R dummy-load TX cutoff) so the console never
# carries a second, driftable copy of these names.
ANTENNA_NAMES = {a.number: a.name for a in ANTENNAS.values()}


def build_state_payload(state: StationState) -> dict:
    data = state.to_dict()
    data["rig"] = dict(data["rig"])   # copy — to_dict() hands back the live StationState.rig dict by reference
    # rig.strength_db (Hamlib STRENGTH) is dead under this station's SDR
    # Switch wiring — the radio's own receive antenna port sees nothing
    # during RX, the RSPdx-R2 is the actual receiver. Compute a real S-meter
    # from the SDR's own spectrum instead, at the rig's current passband.
    # None (omitted) during TX — the antenna's disconnected then too.
    if sdr is not None and sdr.available:
        data["rig"]["sdr_rx_volume"] = sdr.audio.manual_gain   # config values, not TX/RX-gated
        data["rig"]["sdr_agc_mode"] = sdr.audio.agc_mode
        data["rig"]["sdr_eq_enabled"] = sdr.audio.eq_enabled
        data["rig"]["sdr_eq_bass_db"] = sdr.audio.eq_bass_db
        data["rig"]["sdr_eq_mid_db"] = sdr.audio.eq_mid_db
        data["rig"]["sdr_eq_treble_db"] = sdr.audio.eq_treble_db
        data["rig"]["sdr_nr_enabled"] = sdr.audio.nr_enabled
        data["rig"]["sdr_nr_atten_limit_db"] = sdr.audio.nr_atten_limit_db
        data["rig"]["digital_audio_available"] = sdr.digital_audio.available
        data["rig"]["digital_audio_active"] = sdr.digital_audio.active
        data["rig"]["digital_source_channel"] = _digital_source_channel
        # Diagnostic (Terry 2026-09-12: "RX2 selected as source and no
        # sound getting to WSJT-X" — browser audio confirmed flowing fine,
        # narrowing down whether the BlackHole callback registration or
        # the actual device write is the gap).
        data["rig"]["digital_audio_registered_a"] = \
            sdr.digital_audio.on_audio_frame in sdr.audio._audio_callbacks
        data["rig"]["digital_audio_registered_b"] = \
            sdr.digital_audio.on_audio_frame in sdr.audio_b._audio_callbacks
        data["rig"]["digital_audio_qsize"] = sdr.digital_audio._q.qsize()
        data["rig"]["sdr_antenna"] = sdr.antenna_label
        data["rig"]["sdr_rf_gain_pct"] = sdr.rf_gain_pct
        # Phase 0 dual-tuner proof-of-life — Channel B (Tuner 2 / Antenna 2)
        # has no pipeline of its own yet, just a liveness check, so this is
        # the only way to confirm Dual Tuner mode actually worked without
        # grepping logs. See project memory on preferring live telemetry.
        data["rig"]["sdr_channel_b_alive"] = sdr.channel_b_alive
        # Channel B's own state — parallel to the Channel A fields above,
        # but sourced entirely from sdr.audio_b/sdr.*_b since Channel B has
        # no rig/CAT counterpart. sdr_target_freq_hz_b/mode_b/bandwidth_hz_b
        # are Channel B's own "dial frequency" — Channel A gets this from
        # rig.freq_hz/rig.mode (CAT ground truth), Channel B has none, so
        # its own AudioDemodulator's target is the ground truth instead.
        data["rig"]["sdr_rf_freq_hz_b"] = sdr.rf_freq_hz_b
        data["rig"]["sdr_rf_gain_pct_b"] = sdr.rf_gain_pct_b
        data["rig"]["sdr_antenna_b"] = sdr.antenna_label_b
        data["rig"]["sdr_target_freq_hz_b"] = sdr.audio_b.target_freq_hz
        data["rig"]["sdr_mode_b"] = sdr.audio_b.mode
        data["rig"]["sdr_bandwidth_hz_b"] = sdr.audio_b.bandwidth_hz
        data["rig"]["sdr_rx_volume_b"] = sdr.audio_b.manual_gain
        data["rig"]["sdr_agc_mode_b"] = sdr.audio_b.agc_mode
        data["rig"]["sdr_eq_enabled_b"] = sdr.audio_b.eq_enabled
        data["rig"]["sdr_eq_bass_db_b"] = sdr.audio_b.eq_bass_db
        data["rig"]["sdr_eq_mid_db_b"] = sdr.audio_b.eq_mid_db
        data["rig"]["sdr_eq_treble_db_b"] = sdr.audio_b.eq_treble_db
        data["rig"]["sdr_nr_enabled_b"] = sdr.audio_b.nr_enabled
        data["rig"]["sdr_nr_atten_limit_db_b"] = sdr.audio_b.nr_atten_limit_db
        # Diversity pane's phase-rotate experiment — see set_phase_offset.
        data["rig"]["sdr_phase_offset_deg"] = sdr.audio.phase_offset_deg
        data["rig"]["sdr_phase_offset_deg_b"] = sdr.audio_b.phase_offset_deg
        # Diversity pane's phase-coherence scope — see SdrClient.iq_phase_diff_deg.
        data["rig"]["iq_phase_diff_deg"] = sdr.iq_phase_diff_deg()
        # Diversity pane's RX0 combine — see sdr/combiner.py.
        data["rig"]["combine_enabled"] = sdr.combiner.enabled
        data["rig"]["combine_gain"] = sdr.combiner.gain
        data["rig"]["combine_phase_deg"] = sdr.combiner.phase_deg
        data["rig"]["combine_sample_delay"] = sdr.combiner.sample_delay
        data["rig"]["combine_level_dbfs"] = sdr.combiner.level_dbfs
        data["rig"]["combine_dropped_a"] = sdr.combiner.dropped_count_a
        data["rig"]["combine_dropped_b"] = sdr.combiner.dropped_count_b
        data["rig"]["combine_qsize_a"] = sdr.combiner._q_a.qsize()
        data["rig"]["combine_qsize_b"] = sdr.combiner._q_b.qsize()
        if not data["rig"].get("ptt", False):
            freq_hz = data["rig"].get("freq_hz")
            if freq_hz:
                bandwidth_hz = data["rig"].get("passband_hz") or 2400
                db_fs = sdr.passband_strength_db(float(freq_hz), float(bandwidth_hz))
                if db_fs is not None:
                    data["rig"]["sdr_strength_db"] = db_fs
            # target_freq_hz starts None until something sends
            # set_audio_target for channel B (no frontend does yet) — same
            # reason Channel A's block above guards on freq_hz being set.
            if sdr.audio_b.target_freq_hz is not None:
                db_fs_b = sdr.passband_strength_db_b(
                    sdr.audio_b.target_freq_hz, sdr.audio_b.bandwidth_hz)
                if db_fs_b is not None:
                    data["rig"]["sdr_strength_db_b"] = db_fs_b
    data["station_profile"] = station_profile.to_dict()
    data["antenna_names"] = ANTENNA_NAMES
    return data


async def on_station_state(state: StationState):
    if sdr is not None and sdr.available:
        ptt = bool(state.rig.get("ptt", False))
        if ptt and not sdr.audio.tx_active:
            # Rising edge on the slow path (100ms poll) — flush both queues
            # in case the fast PTT monitor missed it or hasn't connected yet.
            sdr.gate_tx()
        elif not ptt:
            sdr.audio.tx_active = False
            # gate_tx() sets both audio.tx_active and audio_b.tx_active as a
            # side effect on the rising edge (AudioDemodulator.gate_tx), but
            # nothing was clearing Channel B's back to False here — it would
            # go permanently silent after the first TX. Mirror the Channel A
            # line above.
            sdr.audio_b.tx_active = False
            sdr.combiner.tx_active = False
    await manager.broadcast({"type": "state", "data": build_state_payload(state)})


async def on_spectrum_frame(frame: dict):
    await spectrum_manager.broadcast_frame(frame)


async def on_spectrum_frame_b(frame: dict):
    await spectrum_manager_b.broadcast_frame(frame)


async def on_spot_alert(alert: SpotAlert):
    await manager.broadcast({
        "type": "spot_alert",
        "data": {"kind": alert.kind, "call": alert.call, "detail": alert.detail},
    })


async def on_wsjtx_status(status: WsjtxStatus):
    # Broadcast only — no dashboard panel consumes this yet (panels are
    # part of the deferred layout discussion). Making the data available
    # on /ws now means the eventual panel is just a UI change, not also a
    # backend one. Decode/LoggedAdif aren't broadcast here since they're
    # high-frequency/large — propagation (#9), QSO logging (#10), and
    # spot/seek (#11) each register their own callbacks directly on
    # wsjtx_listener rather than going through this broadcast path.
    await manager.broadcast({
        "type": "wsjtx_status",
        "data": {
            "dial_freq_hz": status.dial_freq_hz,
            "mode": status.mode,
            "dx_call": status.dx_call,
            "dx_grid": status.dx_grid,
            "de_call": status.de_call,
            "de_grid": status.de_grid,
            "transmitting": status.transmitting,
            "decoding": status.decoding,
            "tx_message": status.tx_message,
        },
    })



async def on_audio_frame(audio_bytes: bytes):
    # Gate: drop audio frames while TX is active so no pre-TX IQ that slipped
    # past the fast PTT monitor reaches the browser.
    if sdr is not None and sdr.audio.tx_active:
        return
    await audio_manager.broadcast_audio(audio_bytes)


async def on_audio_frame_b(audio_bytes: bytes):
    if sdr is not None and sdr.audio_b.tx_active:
        return
    await audio_manager_b.broadcast_audio(audio_bytes)


async def on_audio_frame_0(audio_bytes: bytes):
    # RX0 diversity combine — see sdr/combiner.py.
    if sdr is not None and sdr.combiner.tx_active:
        return
    await audio_manager_0.broadcast_audio(audio_bytes)


async def on_spectrum_frame_0(frame: dict):
    await spectrum_manager_0.broadcast_frame(frame)


# How often the fast PTT watchdog polls rigctld (ms).  5ms gives ~2.5ms
# average detection lag, comfortably ahead of the bridge's 100ms ACOM poll.
_FAST_PTT_POLL_MS  = 5
# How long to hold the TX gate after PTT drops — covers antenna relay
# bounce and IQ pipeline drain on TX→RX return.
_POST_TX_HOLD_S    = 0.15


async def _fast_ptt_monitor():
    """Dedicated low-latency PTT watchdog.

    Maintains its own persistent rigctld TCP connection and polls PTT every
    _FAST_PTT_POLL_MS.  On PTT rising edge it calls sdr.audio.gate_tx()
    immediately — flushing the IQ queue and resetting AGC gain — rather than
    waiting for the main 100ms bridge poll to propagate the change.  The main
    poll's on_station_state still sets tx_active too, acting as a safety net.
    """
    reader = writer = None
    last_ptt = False

    while True:
        try:
            if writer is None:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(RIGCTLD_HOST, RIGCTLD_PORT),
                    timeout=2.0)

            writer.write(b't\n')
            await writer.drain()
            val_line = await asyncio.wait_for(reader.readline(), timeout=0.5)
            decoded  = val_line.decode(errors='replace').strip()
            # Simple rigctld protocol returns just the value ("0" or "1") with
            # no RPRT terminator.  Reading a second line would block 0.5s on
            # every poll cycle, timing out and crashing the monitor back to its
            # 1.0s error-retry loop — making it effectively dead.
            if not decoded or decoded.startswith('RPRT'):
                last_ptt = last_ptt   # no valid reading; skip this cycle
                await asyncio.sleep(_FAST_PTT_POLL_MS / 1000)
                continue

            ptt = decoded == '1'

            if ptt and not last_ptt:
                # Rising edge ��� gate immediately
                if sdr is not None and sdr.available:
                    sdr.gate_tx()   # flushes IQ queue AND BlackHole queue
                # Signal the browser via the STATE channel (/ws), not the
                # audio channel (/ws/audio).  The audio WS carries a continuous
                # stream of binary frames; a text flush injected into that stream
                # sits behind the pending audio backlog and always loses the race
                # to the state message — confirmed by browser console showing the
                # state path firing txSilenceStart before the flush ever arrives.
                # Sending tx_mute on /ws reaches the browser immediately (no
                # backlog), which calls txSilenceStart() → closes audioWs →
                # dropping the pending audio frames before they can play.
                await manager.broadcast({"type": "tx_mute"})
                logger.info("Fast PTT: TX gate opened, tx_mute sent")
            elif not ptt and last_ptt:
                # Falling edge — keep polling through the hold so a rapid
                # re-TX isn't missed while sleeping.  A bare asyncio.sleep
                # here would black out polling for the full hold duration,
                # causing the bridge's 100ms state path to win the race on
                # any re-TX that starts before the sleep ends.
                hold_deadline = asyncio.get_event_loop().time() + _POST_TX_HOLD_S
                while asyncio.get_event_loop().time() < hold_deadline:
                    await asyncio.sleep(_FAST_PTT_POLL_MS / 1000)
                    writer.write(b't\n')
                    await writer.drain()
                    hline = await asyncio.wait_for(reader.readline(), timeout=0.5)
                    hdec  = hline.decode(errors='replace').strip()
                    if not hdec or hdec.startswith('RPRT'):
                        continue
                    new_ptt = hdec == '1'
                    if new_ptt and not ptt:
                        # Re-TX during hold — gate immediately
                        if sdr is not None and sdr.available:
                            sdr.gate_tx()
                        await manager.broadcast({"type": "tx_mute"})
                        logger.info("Fast PTT: re-TX during hold, tx_mute sent")
                    ptt = new_ptt
                if not ptt:
                    if sdr is not None and sdr.available:
                        sdr.audio.tx_active = False
                        sdr.audio_b.tx_active = False   # see on_station_state's matching fix
                        sdr.combiner.tx_active = False
                    logger.debug("Fast PTT: TX gate closed")

            last_ptt = ptt

        except Exception:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            reader = writer = None
            await asyncio.sleep(1.0)
            continue

        await asyncio.sleep(_FAST_PTT_POLL_MS / 1000)


_last_is_digital = False


async def on_session_status_for_audio_mode(status: dict):
    """Drives BOTH channels' audio-profile voice/digital toggle (AGC/NR/
    EQ/passband — AudioDemodulator.enter_digital_mode()/exit_digital_mode())
    off the operator's one selected SESSION (SSB vs FT8/JS8Call), not off
    RX1's live rig CAT mode.

    Used to be split: RX1 followed rig_state.is_digital directly (an
    on_rig_state_for_audio_mode callback on the rig itself) while RX2
    piggybacked on that same edge. That meant manually toggling RX1's raw
    MODE button (e.g. back to plain USB for a quick voice check) silently
    flipped RX2's profile too, even though RX2 has no CAT radio and no
    concept of "the rig's mode" at all (Terry 2026-09-11: RX2 audio went
    weak/noise-like purely from toggling RX1's mode, RX2 itself untouched).
    Terry: "there is no real use case for having them in different modes...
    ONE session mode selection for the whole console... determines the
    state of the RXs" — so both channels now follow the single session
    choice uniformly, and neither reacts to a bare CAT mode edit that
    didn't go through an actual session switch.

    This does NOT touch either channel's own USB/LSB sideband selection
    (AudioDemodulator.mode) — RX2 keeps that fully independent so it can
    listen to a different band/sideband than RX1 while sharing the same
    voice/digital profile (Terry: run SSB on 20m from RX1 while RX2
    listens to 40m LSB)."""
    global _last_is_digital
    if sdr is None or not sdr.available:
        return
    session_id = status.get("current_session_id")
    profile = PROFILES.get(session_id) if session_id else None
    is_digital = bool(profile and profile.rig_mode.startswith("PKT"))
    if is_digital and not _last_is_digital:
        sdr.audio.enter_digital_mode()
        sdr.audio_b.enter_digital_mode()
    elif not is_digital and _last_is_digital:
        sdr.audio.exit_digital_mode()
        sdr.audio_b.exit_digital_mode()
    _last_is_digital = is_digital


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, sdr, ab_test, tx_cal, session_manager, claude_advisor
    logger.info("Starting W7TLG Console...")

    # Tighter than the original 0.5s — the panadapter's rig-frequency marker
    # only moves as often as this polls, so turning the tuning knob looked
    # like a series of jumps rather than smooth motion.
    rig = RigctldClient(host=RIGCTLD_HOST, port=RIGCTLD_PORT, poll_interval=0.1)

    acom_port = ACOM_PORT or find_acom_port()
    if not acom_port:
        logger.warning("No ACOM serial port found — amp features disabled.")
        acom_port = "/dev/null"

    amp = AcomSerial(port=acom_port, baud=ACOM_BAUD)
    bridge = AcomBridge(rig=rig, amp=amp)
    bridge.on_state_change(on_station_state)
    bridge.on_trend_sample(trend_csv.log)
    bridge.on_state_change(qso_telemetry_logger.record_sample)
    await bridge.start()

    # Wait briefly for rigctld to report the radio's actual current
    # frequency so the panadapter starts there instead of an arbitrary
    # default — rig.start() only kicks off the poll loop, it doesn't block
    # until the first poll completes.
    initial_freq_hz = None
    for _ in range(20):
        if rig.state.connected and rig.state.freq_hz:
            initial_freq_hz = float(rig.state.freq_hz)
            break
        await asyncio.sleep(0.1)

    sdr = SdrClient(rf_freq_hz=initial_freq_hz) if initial_freq_hz else SdrClient()
    sdr.on_spectrum(on_spectrum_frame)
    sdr.audio.on_audio(on_audio_frame)
    # Channel B (Tuner 2 / Antenna 2) has no rig/CAT counterpart to follow
    # — it's an independent SDR-only VFO, so it just starts wherever
    # Channel A did (rf_freq_hz_b defaults to rf_freq_hz in SdrClient) and
    # the operator retunes it from its own panadapter.
    sdr.on_spectrum_b(on_spectrum_frame_b)
    sdr.audio_b.on_audio(on_audio_frame_b)
    sdr.combiner.on_audio(on_audio_frame_0)
    sdr.combiner.on_spectrum(on_spectrum_frame_0)
    await sdr.start()
    if not sdr.available:
        logger.warning("SDR unavailable — panadapter features disabled.")
    asyncio.create_task(_fast_ptt_monitor())
    asyncio.create_task(_monitor_liveness_watcher())

    ab_test = AntennaAbTest(bridge=bridge, sdr=sdr)
    ab_test.on_status(on_ab_test_status)

    tx_cal = TxPowerCalibration(bridge=bridge)
    tx_cal.on_status(on_tx_cal_status)

    session_manager = SessionManager(bridge=bridge, sdr=sdr, wsjtx_listener=wsjtx_listener)
    session_manager.on_status(on_session_status)
    session_manager.on_status(on_session_status_for_audio_mode)

    # WSJT-X UDP listener — passive third listener on the same multicast
    # group RUMLogNG/GridTracker2 already use (224.0.0.1:2237, lo0). See
    # wsjtx/udp_listener.py docstring. Failure here (e.g. port already
    # bound in a way that rejects SO_REUSEPORT) must not take down the
    # rest of the console — it's a nice-to-have data feed, not part of
    # rig/amp safety paths.
    wsjtx_listener.on_status(on_wsjtx_status)
    # Additive registration — on_status() supports multiple callbacks —
    # gives SessionManager its own liveness clock rather than reusing
    # wsjtx_listener.connected (sticky/naive, see session_manager.py).
    wsjtx_listener.on_status(session_manager.on_wsjtx_status)
    wsjtx_listener.on_logged_adif(qso_telemetry_logger.on_logged_adif)
    # Award tracker reads WSJT-X's local ADIF log — re-read it after every
    # newly logged QSO so "missing states" reflects what was just worked,
    # not last session's snapshot.
    wsjtx_listener.on_logged_adif(lambda _msg: asyncio.to_thread(award_tracker.reload))
    wsjtx_listener.on_decode(spotter.on_decode)
    try:
        await wsjtx_listener.start()
    except OSError as e:
        logger.warning(f"WSJT-X UDP listener unavailable: {e}")

    spotter.on_alert(on_spot_alert)
    await spotter.start_pota_polling()

    # RBN (CW) + DXSpider cluster (SSB and everything else humans post) —
    # covers real-time spotting for the modes WSJT-X's own Decode feed
    # can't (digital-only). Read-only: login with callsign, never post
    # spots. Same "must not affect the rest of the console" principle as
    # wsjtx_listener — failure here is a lost data feed, not a startup
    # failure.
    rbn_client = RbnClient(my_call=station_profile.current.call)
    rbn_client.on_spot(spotter.on_cluster_spot)
    dx_cluster_client = DxClusterClient(my_call=station_profile.current.call)
    dx_cluster_client.on_spot(spotter.on_cluster_spot)
    await rbn_client.start()
    await dx_cluster_client.start()

    claude_advisor = ClaudeAdvisor(rig=rig)
    asyncio.create_task(_propagation_poll_loop())

    logger.info("W7TLG Console running")

    yield

    logger.info("Shutting down...")
    trend_csv.stop()
    await spotter.stop_pota_polling()
    await rbn_client.stop()
    await dx_cluster_client.stop()
    await wsjtx_listener.stop()
    await sdr.stop()
    await bridge.stop()


app = FastAPI(title="W7TLG Station Console", lifespan=lifespan)

# ---------------------------------------------------------------------------
# REST models
# ---------------------------------------------------------------------------

class ModeRequest(BaseModel):
    mode: str
    confirmed: bool = False

class TxRequest(BaseModel):
    inhibit: bool
    reason: str = "Manual inhibit"

class StationProfileRequest(BaseModel):
    profile_id: str

class CallsignLookupRequest(BaseModel):
    callsign: str

class WatchlistRequest(BaseModel):
    callsign: str

class AdvisorRequest(BaseModel):
    question: str = ""
    clear_history: bool = False
    auto_qsy: bool = False

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def get_state():
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    return build_state_payload(bridge.station)


@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    try:
        mode = OperatingMode(req.mode)
    except ValueError:
        raise HTTPException(400, f"Unknown mode: {req.mode}")
    ok, msg = await bridge.set_operating_mode(mode, confirmed=req.confirmed)
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok", "message": msg}


@app.post("/api/antenna/next")
async def next_antenna():
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    ok, msg = await bridge.next_antenna()
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok", "message": msg}


@app.post("/api/amp/settings")
async def amp_request_settings():
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    from amplifier.acom_protocol import cmd_request_message, AmpMsg
    await bridge.amp.send(cmd_request_message(AmpMsg.SETTINGS))
    return {"status": "ok", "message": "SETTINGS request sent — watch log for 0x12 snapshot"}


@app.post("/api/amp/relink-telemetry")
async def amp_relink_telemetry():
    """Re-send ENABLE_TELEMETRY to recover a stalled telemetry stream."""
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    from amplifier.acom_protocol import cmd_enable_telemetry
    await bridge.amp.send(cmd_enable_telemetry())
    return {"status": "ok", "message": "ENABLE_TELEMETRY sent"}


@app.post("/api/amp/atac")
async def amp_atac():
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    ok, msg = await bridge.run_atac()
    if not ok:
        raise HTTPException(400, msg)
    return {"status": "ok", "message": msg}


@app.post("/api/tx")
async def tx_control(req: TxRequest):
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    if req.inhibit:
        await bridge.inhibit_tx(req.reason)
    else:
        await bridge.allow_tx()
    return {"status": "ok"}


@app.get("/api/station-profile")
async def get_station_profile():
    return station_profile.to_dict()


@app.post("/api/station-profile")
async def set_station_profile(req: StationProfileRequest):
    try:
        profile = station_profile.set_current(req.profile_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Broadcast immediately so every connected view (dashboard/monitor/
    # panadapter, and any future spot/seek panel) picks up the QTH change
    # without waiting for the next state poll.
    await manager.broadcast({"type": "station_profile", "data": station_profile.to_dict()})
    return {"status": "ok", "profile": profile.name}


@app.post("/api/lookup/callsign")
async def lookup_callsign(req: CallsignLookupRequest):
    """Manual callsign lookup for SSB/CW — no automated decode/grid feed
    exists for those modes, so the operator looks a heard callsign up by
    hand here instead of switching to a browser tab for QRZ."""
    if not callsign_lookup.any_available:
        raise HTTPException(503, "No callsign lookup service configured (HamQTH/QRZ credentials missing)")
    info = await callsign_lookup.lookup(req.callsign)
    return {
        "call": info.call, "found": info.found, "name": info.name,
        "grid": info.grid, "state": info.us_state, "country": info.country,
        "county": info.county, "source": info.source, "error": info.error,
        "on_watchlist": spotter.watch_list.contains(info.call),
        "worked_before": award_tracker.have_worked_call(info.call),
    }


@app.get("/api/watchlist")
async def get_watchlist():
    return {"callsigns": spotter.watch_list.list()}


@app.post("/api/watchlist")
async def add_watchlist(req: WatchlistRequest):
    spotter.watch_list.add(req.callsign)
    return {"status": "ok", "callsigns": spotter.watch_list.list()}


@app.delete("/api/watchlist/{callsign}")
async def remove_watchlist(callsign: str):
    spotter.watch_list.remove(callsign)
    return {"status": "ok", "callsigns": spotter.watch_list.list()}


@app.get("/api/awards/states")
async def get_award_states():
    """Per-band worked/missing US states (WAS award tracking), sourced
    from RUMLogNG's own database when reachable (falls back to WSJT-X's
    local ADIF otherwise — see award_tracker.py)."""
    award_tracker.reload()
    return {
        "source": award_tracker.source,
        "qso_count": award_tracker.qso_count(),
        "by_band": award_tracker.states_summary_by_band(),
    }


@app.get("/api/awards/dxcc")
async def get_award_dxcc():
    """DXCC entities worked, overall and per band. No "missing" list —
    that needs a complete current DXCC entity reference table this
    console doesn't have; see award_tracker.py's module docstring."""
    award_tracker.reload()
    entities = award_tracker.worked_dxcc_entities()
    return {
        "source": award_tracker.source,
        "qso_count": award_tracker.qso_count(),
        "total_entities_worked": len(entities),
        "entities": [
            {"prefix": e.prefix, "dxcc_adif": e.dxcc_adif,
             "worked_count": e.worked_count, "bands": sorted(e.bands)}
            for e in entities
        ],
        "by_band": award_tracker.dxcc_summary_by_band(),
    }


@app.get("/api/propagation")
async def get_propagation():
    """Current band activity + solar indices. Cached internally (~6min
    PSKReporter, ~15min NOAA) — safe to call often, won't hammer either
    upstream service."""
    return await propagation_source.get_state()


@app.post("/api/advisor/stream")
async def advisor_stream(req: AdvisorRequest):
    """Server-Sent Events stream of Claude's band-advisor response.
    auto_qsy defaults False — the operator must explicitly opt in per
    request before Claude's qsy_to_band tool is even offered, since this
    is the one path in the console where an LLM can command the radio."""
    if claude_advisor is None:
        raise HTTPException(503, "Advisor not initialized")
    if req.clear_history:
        claude_advisor.clear_history()

    rig_state = bridge.station.rig if bridge else {}
    prop_state = await propagation_source.get_state()
    question = req.question.strip() or None

    async def generate():
        async for event_type, event_data in claude_advisor.stream_advice_with_tools(
            rig_state, prop_state, question, req.auto_qsy
        ):
            if event_type == "text":
                yield "data: " + event_data + "\n\n"
            elif event_type == "qsy":
                yield "data: [QSY]" + json.dumps(event_data) + "\n\n"
            elif event_type == "error":
                yield "data: [ERROR]" + json.dumps({"error": event_data}) + "\n\n"
            elif event_type == "done":
                yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/advisor/clear")
async def advisor_clear():
    if claude_advisor is not None:
        claude_advisor.clear_history()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    if bridge:
        await websocket.send_text(json.dumps({
            "type": "state", "data": build_state_payload(bridge.station)}))
    try:
        while True:
            text = await websocket.receive_text()
            await handle_ws_command(text, websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        if websocket in _fine_spectrum_subscribers:
            _fine_spectrum_subscribers.discard(websocket)
            if not _fine_spectrum_subscribers and sdr is not None and sdr.available:
                sdr.audio.fine_spectrum_enabled = False
                sdr.audio_b.fine_spectrum_enabled = False


async def handle_ws_command(text: str, ws: WebSocket):
    if bridge is None:
        return
    try:
        msg = json.loads(text)
        cmd = msg.get("cmd")

        if cmd == "set_mode_op":
            mode = OperatingMode(msg["mode"])
            ok, reply = await bridge.set_operating_mode(
                mode, confirmed=msg.get("confirmed", False))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd,
                "ok": ok, "message": reply}))

        elif cmd == "set_frequency":
            ok = await bridge.rig.set_frequency(int(msg["freq_hz"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_mode":
            ok = await bridge.rig.set_mode(
                msg["mode"], int(msg.get("passband", 0)))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_rf_power":
            requested = int(msg["pct"])
            capped = min(requested, bridge.station.drive_limit_w)
            ok = await bridge.rig.set_rf_power(capped)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok,
                "capped": capped != requested}))

        elif cmd == "set_preamp":
            level = int(msg["level"])
            ok = await bridge.rig.set_preamp(level)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_att":
            db = int(msg["db"])
            ok = await bridge.rig.set_att(db)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_mic_gain":
            level = max(0.0, min(1.0, float(msg["level"])))
            ok = await bridge.rig.set_mic_gain(level)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_comp":
            level = max(0.0, min(1.0, float(msg["level"])))
            ok = await bridge.rig.set_comp(level)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_nb":
            ok = await bridge.rig.set_nb_on(bool(msg["on"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_audio_nr":
            # Noise reduction (DeepFilterNet3) on the SDR audio chain — this
            # is what's actually heard, unlike the radio's own NR (dead for
            # audio purposes since the SDR, not the radio, is the receiver).
            # Optional level (1-15, matching the UI slider) sets the
            # attenuation limit — higher = more aggressive suppression.
            # channel:"B" routes to Channel B's own AudioDemodulator instead
            # of Channel A's — same pattern used by every SDR-audio command
            # below, since these are all purely SDR-side (no rig involved)
            # and Channel B has its own independent AudioDemodulator.
            ok = False
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if msg.get("channel") == "B" else sdr.audio
                if "on" in msg:
                    audio.nr_enabled = bool(msg["on"])
                if "level" in msg:
                    level = max(1, min(15, int(msg["level"])))
                    audio.nr_atten_limit_db = 6.0 + (level - 1) / 14.0 * 34.0
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_phase_offset":
            # Diversity phase-rotate experiment (dashboard/diversity.html)
            # — a manual static phase shift on one channel's baseband, so
            # the operator can turn it while listening for a null/
            # reinforcement against the other channel in the stereo mix.
            # See AudioDemodulator.phase_offset_deg.
            ok = False
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if msg.get("channel") == "B" else sdr.audio
                audio.phase_offset_deg = float(msg["deg"]) % 360.0
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_combine_enabled":
            # RX0 diversity combine (dashboard/diversity.html) — costs
            # real CPU (a third full downmix/decimate/filter pipeline), so
            # off until the Diversity page actually asks for it. See
            # sdr/combiner.py.
            ok = False
            if sdr is not None and sdr.available:
                sdr.combiner.enabled = bool(msg["enabled"])
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_combine_weight":
            # z = gain * e^(j*phase_deg), applied to RX2 before RX0 = RX1 - z*RX2.
            ok = False
            if sdr is not None and sdr.available:
                if "gain" in msg:
                    sdr.combiner.gain = max(0.0, float(msg["gain"]))
                if "phase_deg" in msg:
                    sdr.combiner.phase_deg = float(msg["phase_deg"]) % 360.0
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "calibrate_combine":
            # One-shot (not adaptive — see sdr/combiner.py's own docstring):
            # cross-correlates recent RX1/RX2 baseband to find and store the
            # actual integer sample delay between the two channels'
            # independent processing threads, needed for a clean null.
            delay = None
            if sdr is not None and sdr.available:
                delay = sdr.combiner.calibrate()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": delay is not None,
                "sample_delay": delay}))

        elif cmd == "subscribe_fine_spectrum":
            # The diversity page's own RX1/RX2 spectrum panels — see
            # AudioDemodulator.fine_spectrum_enabled. Ref-counted across
            # possibly-multiple diversity tabs; _fine_spectrum_subscribers
            # cleaned up automatically on disconnect below.
            _fine_spectrum_subscribers.add(ws)
            if sdr is not None and sdr.available:
                sdr.audio.fine_spectrum_enabled = True
                sdr.audio_b.fine_spectrum_enabled = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": True}))

        elif cmd == "unsubscribe_fine_spectrum":
            _fine_spectrum_subscribers.discard(ws)
            if not _fine_spectrum_subscribers and sdr is not None and sdr.available:
                sdr.audio.fine_spectrum_enabled = False
                sdr.audio_b.fine_spectrum_enabled = False
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": True}))

        elif cmd == "set_eq":
            ok = False
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if msg.get("channel") == "B" else sdr.audio
                if "enabled" in msg:
                    audio.eq_enabled = bool(msg["enabled"])
                if "bass_db" in msg:
                    audio.eq_bass_db = max(-12.0, min(12.0, float(msg["bass_db"])))
                if "mid_db" in msg:
                    audio.eq_mid_db = max(-12.0, min(12.0, float(msg["mid_db"])))
                if "treble_db" in msg:
                    audio.eq_treble_db = max(-12.0, min(12.0, float(msg["treble_db"])))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_dt_gain":
            # CAT menu 073 "DATA OUT LEVEL" — digital-mode TX audio drive,
            # not exposed as a normal Hamlib level on this rig (raw CAT
            # passthrough, see RigctldClient.set_dt_gain).
            value = max(0, min(100, int(msg["value"])))
            ok = await bridge.rig.set_dt_gain(value)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_ssb_tx_bpf":
            # CAT menu 110 "SSB TX BPF" — one of 5 fixed radio presets (0-4),
            # not a Hamlib level (raw CAT passthrough, see
            # RigctldClient.set_ssb_tx_bpf / SSB_TX_BPF_PRESETS).
            value = int(msg["value"])
            ok = await bridge.rig.set_ssb_tx_bpf(value)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_dnf":
            ok = await bridge.rig.set_dnf_on(bool(msg["on"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_agc":
            value = int(msg["value"])
            channel = msg.get("channel", "A")
            # Channel B has no rig/CAT counterpart — the rig AGC call only
            # applies to Channel A. See set_audio_nr above for the general
            # channel-routing pattern used throughout this block.
            ok = True if channel == "B" else await bridge.rig.set_agc(value)
            # Also drives the SDR audio chain's auto-leveling speed — the
            # radio's own CAT-commanded AGC has no audible effect, since the
            # RSPdx-R2 (not the radio's receiver) is what's actually heard.
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if channel == "B" else sdr.audio
                audio.agc_mode = {0: "off", 2: "fast", 3: "slow"}.get(value, "slow")
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_rx_volume":
            ok = False
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if msg.get("channel") == "B" else sdr.audio
                audio.manual_gain = max(0.0, min(10.0, float(msg["gain"])))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_rf_gain":
            ok = False
            if sdr is not None and sdr.available:
                if msg.get("channel") == "B":
                    sdr.set_rf_gain_pct_b(float(msg["pct"]))
                else:
                    sdr.set_rf_gain_pct(float(msg["pct"]))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "next_antenna":
            ok, reply = await bridge.next_antenna()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd,
                "ok": ok, "message": reply}))

        elif cmd == "inhibit_tx":
            await bridge.inhibit_tx(msg.get("reason", "Browser inhibit"))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": True}))

        elif cmd == "allow_tx":
            await bridge.allow_tx()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": True}))

        elif cmd == "get_trend":
            since = float(msg.get("since", 0))
            trend = bridge.get_trend_data(since)
            await ws.send_text(json.dumps({
                "type": "trend_data", **trend}))

        elif cmd == "set_panadapter_freq":
            # Channel A's frequency is normally rig-CAT-driven (radio is
            # ground truth — see project memory on the panadapter tuning
            # model); this command is the manual-override path. Channel B
            # has no rig at all, so for it this IS the only way to tune —
            # not an override of anything.
            ok = False
            if sdr is not None and sdr.available:
                if msg.get("channel") == "B":
                    sdr.set_center_freq_hz_b(float(msg["freq_hz"]))
                else:
                    sdr.set_center_freq_hz(float(msg["freq_hz"]))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_audio_target":
            ok = False
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if msg.get("channel") == "B" else sdr.audio
                audio.target_freq_hz = float(msg["freq_hz"])
                audio.mode = msg.get("mode", "USB")
                audio.bandwidth_hz = float(msg.get("bandwidth_hz", 3000))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_audio_enabled":
            ok = False
            if sdr is not None and sdr.available:
                audio = sdr.audio_b if msg.get("channel") == "B" else sdr.audio
                audio.enabled = bool(msg["enabled"])
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_digital_source":
            # Which channel's demodulated audio feeds the single BlackHole
            # cable to WSJT-X etc. — a plain subscribe/unsubscribe swap on
            # AudioDemodulator.on_audio(), not two virtual cables (see
            # project memory on the RSPduo migration for why one cable is
            # the right design here).
            ok = False
            if sdr is not None and sdr.available:
                global _digital_source_channel
                channel = msg.get("channel", "A")
                target = sdr.audio_b if channel == "B" else sdr.audio
                other = sdr.audio if channel == "B" else sdr.audio_b
                other.off_audio(sdr.digital_audio.on_audio_frame)
                target.on_audio(sdr.digital_audio.on_audio_frame)
                # AudioDemodulator.enabled (the per-channel AUDIO button,
                # for local speaker monitoring) gates ALL processing —
                # feed() is a no-op while it's off. Selecting a channel as
                # the digital source has to imply "process this channel's
                # audio" regardless of whether the operator has separately
                # clicked its own AUDIO button, or nothing reaches WSJT-X
                # at all (Terry 2026-09-12: "the audio is not passing
                # through to WSJT-X" after selecting RX2 as source). Only
                # forces the target on — leaves `other` alone, since the
                # operator may still want to listen to it locally.
                target.enabled = True
                # Digital-mode audio profile (AGC/NR/EQ/passband, fine
                # spectrum) is NOT touched here — see
                # on_session_status_for_audio_mode's own comment. It's
                # driven by the operator's selected SESSION and applies to
                # both channels together, not per-source: an earlier
                # version called exit_digital_mode()
                # on `other` here, which froze the deselected channel's
                # fine spectrum while the radio was still genuinely in
                # digital mode (Terry 2026-09-12: RX1 looked dead/stale
                # right after selecting RX2 as source, even with real FT8
                # traffic on frequency).
                _digital_source_channel = channel
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "monitor_heartbeat":
            global _last_monitor_heartbeat
            _last_monitor_heartbeat = time.time()

        elif cmd == "ab_test_start":
            ok, reply = False, "AB test not initialized"
            if ab_test is not None:
                manual = bool(msg.get("manual", False))
                antennas = ([str(a) for a in msg["antennas"]] if manual
                            else [int(a) for a in msg["antennas"]])
                ok, reply = await ab_test.start(
                    antennas=antennas,
                    rounds=int(msg.get("rounds", 5)),
                    scan_start_hz=float(msg["scan_start_hz"]),
                    scan_stop_hz=float(msg["scan_stop_hz"]),
                    bandwidth_hz=float(msg.get("bandwidth_hz", 2400.0)),
                    profile_duration_s=float(msg.get("profile_duration_s", 30.0)),
                    manual=manual,
                )
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "ab_test_stop":
            ok, reply = False, "AB test not initialized"
            if ab_test is not None:
                ok, reply = await ab_test.stop()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "ab_test_confirm_switch":
            ok, reply = False, "AB test not initialized"
            if ab_test is not None:
                ok, reply = ab_test.confirm_switch()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "cal_start":
            ok, reply = False, "Calibration not initialized"
            if tx_cal is not None:
                steps = [int(s) for s in msg["steps"]] if "steps" in msg else None
                ok, reply = await tx_cal.start(steps=steps)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "cal_stop":
            ok, reply = False, "Calibration not initialized"
            if tx_cal is not None:
                ok, reply = await tx_cal.stop()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "cal_status":
            if tx_cal is not None:
                await ws.send_text(json.dumps({
                    "type": "tx_cal_status", "data": tx_cal.get_status()}))

        elif cmd == "ab_test_status":
            if ab_test is not None:
                await ws.send_text(json.dumps({
                    "type": "ab_test_status", "data": ab_test.get_status()}))

        elif cmd == "session_switch":
            ok, reply = False, "Session manager not initialized"
            if session_manager is not None:
                ok, reply = await session_manager.switch(msg["session_id"])
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "session_confirm_quit":
            ok, reply = False, "Session manager not initialized"
            if session_manager is not None:
                ok, reply = session_manager.confirm_quit()
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok, "message": reply}))

        elif cmd == "session_status":
            if session_manager is not None:
                await ws.send_text(json.dumps({
                    "type": "session_status", "data": session_manager.get_status()}))

        else:
            await ws.send_text(json.dumps({
                "type": "error", "message": f"Unknown command: {cmd}"}))

    except Exception as e:
        logger.error(f"WebSocket command error: {e}")
        await ws.send_text(json.dumps({"type": "error", "message": str(e)}))


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Dashboard HTML not found</h1>")

@app.get("/monitor", response_class=HTMLResponse)
async def monitor():
    html_path = Path(__file__).parent / "monitor.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Monitor HTML not found</h1>")

@app.get("/panadapter", response_class=HTMLResponse)
async def panadapter():
    html_path = Path(__file__).parent / "panadapter.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Panadapter HTML not found</h1>")

@app.get("/console", response_class=HTMLResponse)
async def console_view():
    """Unified single-window shell (Phase A of the layout consolidation) —
    additive only, doesn't replace / /panadapter /monitor, which stay
    fully functional standalone. See console.html's own docstring for why
    it's iframe-based rather than a JS merge."""
    html_path = Path(__file__).parent / "console.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Console HTML not found</h1>")

@app.get("/propagation", response_class=HTMLResponse)
async def propagation_view():
    html_path = Path(__file__).parent / "propagation.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Propagation HTML not found</h1>")

@app.get("/spotseek", response_class=HTMLResponse)
async def spotseek_view():
    html_path = Path(__file__).parent / "spotseek.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Spot/Seek HTML not found</h1>")

@app.get("/advisor", response_class=HTMLResponse)
async def advisor_view():
    html_path = Path(__file__).parent / "advisor.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Advisor HTML not found</h1>")

@app.get("/diversity", response_class=HTMLResponse)
async def diversity_view():
    html_path = Path(__file__).parent / "diversity.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Diversity HTML not found</h1>")


@app.get("/audio-worklet.js")
async def audio_worklet():
    js_path = Path(__file__).parent / "audio-worklet.js"
    return Response(content=js_path.read_text(), media_type="application/javascript")


@app.websocket("/ws/spectrum")
async def spectrum_websocket(websocket: WebSocket):
    await spectrum_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        spectrum_manager.disconnect(websocket)


@app.websocket("/ws/audio")
async def audio_websocket(websocket: WebSocket):
    await audio_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        audio_manager.disconnect(websocket)


@app.websocket("/ws/spectrum_b")
async def spectrum_websocket_b(websocket: WebSocket):
    await spectrum_manager_b.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        spectrum_manager_b.disconnect(websocket)


@app.websocket("/ws/spectrum_0")
async def spectrum_websocket_0(websocket: WebSocket):
    await spectrum_manager_0.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        spectrum_manager_0.disconnect(websocket)


@app.websocket("/ws/audio_b")
async def audio_websocket_b(websocket: WebSocket):
    await audio_manager_b.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        audio_manager_b.disconnect(websocket)


@app.websocket("/ws/audio_0")
async def audio_websocket_0(websocket: WebSocket):
    await audio_manager_0.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        audio_manager_0.disconnect(websocket)

