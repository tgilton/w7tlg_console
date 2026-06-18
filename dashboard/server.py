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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from amplifier.acom_bridge import AcomBridge, OperatingMode, StationState
from amplifier.acom_serial import AcomSerial, find_acom_port
from rig.rigctld_client import RigctldClient

logger = logging.getLogger(__name__)

RIGCTLD_HOST  = "127.0.0.1"
RIGCTLD_PORT  = 4532
ACOM_PORT     = "/dev/cu.usbserial-A9V19CH7"
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


manager = ConnectionManager()
bridge: Optional[AcomBridge] = None


async def on_station_state(state: StationState):
    await manager.broadcast({"type": "state", "data": state.to_dict()})


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge
    logger.info("Starting W7TLG Console...")

    rig = RigctldClient(host=RIGCTLD_HOST, port=RIGCTLD_PORT, poll_interval=0.5)

    acom_port = ACOM_PORT or find_acom_port()
    if not acom_port:
        logger.warning("No ACOM serial port found — amp features disabled.")
        acom_port = "/dev/null"

    amp = AcomSerial(port=acom_port, baud=ACOM_BAUD)
    bridge = AcomBridge(rig=rig, amp=amp, state_file=THERMAL_STATE)
    bridge.on_state_change(on_station_state)
    await bridge.start()
    logger.info("W7TLG Console running")

    yield

    logger.info("Shutting down...")
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
    return bridge.station.to_dict()


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
            "type": "state", "data": bridge.station.to_dict()}))
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
            ok = await bridge.rig.set_rf_power(int(msg["pct"]))
            await ws.send_text(json.dumps({
                "type": "cmd_response", "cmd": cmd, "ok": ok}))

        elif cmd == "set_preamp":
            level = int(msg["level"])
            ok = await bridge.rig.set_preamp(level)
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
            ok = await bridge.rig.set_agc(int(msg["value"]))
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
