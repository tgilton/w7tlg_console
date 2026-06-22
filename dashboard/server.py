"""
W7TLG Console — FastAPI Server

Serves the dashboard UI and manages WebSocket connections.
All station state flows through here to connected browsers.

Endpoints:
  GET  /              — Dashboard HTML
  WS   /ws            — WebSocket: station state stream + command channel
  POST /api/mode      — Set operating mode
  POST /api/antenna   — Select antenna
  POST /api/tx        — TX inhibit / allow
  POST /api/thermal/clear/{ant} — Clear thermal inhibit
  GET  /api/state     — Current state snapshot (REST)
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from amplifier.acom_bridge import AcomBridge, OperatingMode, StationState
from amplifier.acom_serial import AcomSerial, find_acom_port
from rig.rigctld_client import RigctldClient
from sdr.sdr_client import SdrClient

logger = logging.getLogger(__name__)

RIGCTLD_HOST  = "127.0.0.1"
RIGCTLD_PORT  = 4532
# ACOM_PORT     = "/dev/cu.usbserial-A9V19CH7"
ACOM_PORT = "/dev/cu.usbserial-A92518IM"
ACOM_BAUD     = 9600
THERMAL_STATE = Path("config/thermal_state.json")

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
bridge: Optional[AcomBridge] = None
sdr: Optional[SdrClient] = None


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
        if not data["rig"].get("ptt", False):
            freq_hz = data["rig"].get("freq_hz")
            if freq_hz:
                bandwidth_hz = data["rig"].get("passband_hz") or 2400
                db_fs = sdr.passband_strength_db(float(freq_hz), float(bandwidth_hz))
                if db_fs is not None:
                    data["rig"]["sdr_strength_db"] = db_fs
    return data


async def on_station_state(state: StationState):
    if sdr is not None and sdr.available:
        sdr.audio.tx_active = bool(state.rig.get("ptt", False))
    await manager.broadcast({"type": "state", "data": build_state_payload(state)})


async def on_spectrum_frame(frame: dict):
    await spectrum_manager.broadcast_frame(frame)


async def on_audio_frame(audio_bytes: bytes):
    await audio_manager.broadcast_audio(audio_bytes)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, sdr
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
    bridge = AcomBridge(rig=rig, amp=amp, state_file=THERMAL_STATE)
    bridge.on_state_change(on_station_state)
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
    await sdr.start()
    if not sdr.available:
        logger.warning("SDR unavailable — panadapter features disabled.")

    logger.info("W7TLG Console running")

    yield

    logger.info("Shutting down...")
    await sdr.stop()
    await bridge.stop()


app = FastAPI(title="W7TLG Station Console", lifespan=lifespan)

# ---------------------------------------------------------------------------
# REST models
# ---------------------------------------------------------------------------

class ModeRequest(BaseModel):
    mode: str
    confirmed: bool = False

class AntennaRequest(BaseModel):
    antenna: int

class TxRequest(BaseModel):
    inhibit: bool
    reason: str = "Manual inhibit"

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


@app.post("/api/antenna")
async def select_antenna(req: AntennaRequest):
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    ok, msg = await bridge.select_antenna(req.antenna)
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


@app.post("/api/thermal/clear/{antenna_number}")
async def clear_thermal(antenna_number: int):
    if bridge is None:
        raise HTTPException(503, "Bridge not initialized")
    await bridge.operator_clear_thermal(antenna_number)
    return {"status": "ok",
            "message": f"Thermal inhibit cleared for antenna {antenna_number}"}


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

        elif cmd == "set_nr_on":
            ok = await bridge.rig.set_nr_on(bool(msg["on"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_nr":
            # level 0-15 from UI, scale to 0.0-1.0
            level = max(0, min(15, int(msg["level"])))
            ok = await bridge.rig.set_nr(level / 15.0)
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_dnf":
            ok = await bridge.rig.set_dnf_on(bool(msg["on"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_agc":
            value = int(msg["value"])
            ok = await bridge.rig.set_agc(value)
            # Also drives the SDR audio chain's auto-leveling speed — the
            # radio's own CAT-commanded AGC has no audible effect, since the
            # RSPdx-R2 (not the radio's receiver) is what's actually heard.
            if sdr is not None and sdr.available:
                sdr.audio.agc_mode = {0: "off", 2: "fast", 3: "slow"}.get(value, "slow")
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_rx_volume":
            ok = False
            if sdr is not None and sdr.available:
                sdr.audio.manual_gain = max(0.0, min(10.0, float(msg["gain"])))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "select_antenna":
            ok, reply = await bridge.select_antenna(int(msg["antenna"]))
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

        elif cmd == "clear_thermal":
            await bridge.operator_clear_thermal(int(msg["antenna"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": True}))

        elif cmd == "get_trend":
            since = float(msg.get("since", 0))
            trend = bridge.get_trend_data(since)
            await ws.send_text(json.dumps({
                "type": "trend_data", **trend}))

        elif cmd == "set_panadapter_freq":
            ok = False
            if sdr is not None and sdr.available:
                sdr.set_center_freq_hz(float(msg["freq_hz"]))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_audio_target":
            ok = False
            if sdr is not None and sdr.available:
                sdr.audio.target_freq_hz = float(msg["freq_hz"])
                sdr.audio.mode = msg.get("mode", "USB")
                sdr.audio.bandwidth_hz = float(msg.get("bandwidth_hz", 2400))
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_audio_enabled":
            ok = False
            if sdr is not None and sdr.available:
                sdr.audio.enabled = bool(msg["enabled"])
                ok = True
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        else:
            await ws.send_text(json.dumps({
                "type": "error", "message": f"Unknown command: {cmd}"}))

    except Exception as e:
        logger.error(f"WebSocket command error: {e}")
        await ws.send_text(json.dumps({"type": "error", "message": str(e)}))


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

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

