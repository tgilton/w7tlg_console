# W7TLG Station Console

A web-based ham radio station control console for the W7TLG operating position. Provides unified monitoring and control of a Yaesu FT-991A transceiver and ACOM 1200S linear amplifier from a browser, replacing the need to constantly glance at multiple hardware displays.

## Station Hardware

| Equipment | Connection | Role |
|-----------|-----------|------|
| Yaesu FT-991A | USB to Mac Studio | Transceiver, all modes HF/VHF/UHF |
| ACOM 1200S | FTDI USB-to-RS232 | 1200W linear amplifier |
| ACOM 06AT | RF coax from 1200S | Automatic antenna tuner (powered via 1200S) |
| SS-25/DXF (A1F) | Coax | Vertical antenna |
| 40m EFHW (A3R) | Coax | Multiband end-fed half-wave, primary operating antenna |
| Dummy Load (A4R) | Coax | 50 ohm dummy load |

## Software Stack

- Python 3.12 / FastAPI / WebSockets (backend server)
- Hamlib rigctld (transceiver control, FT-991A model 1035)
- Custom ACOM 1200S serial protocol (binary telemetry and command framing)
- Vanilla HTML/JS/CSS browser frontend (no build step)

## Views

### /dashboard - Operating Console
Primary operating interface with VFO display, band/mode selection, S-meter, TX meters, RF power slider, preamp selector, DSP controls (NB, DNR, DNF, AGC), SSB audio controls (Mic Gain, Compression), ACOM telemetry panels, AMP ON/OFF with safety interlock, and antenna selection.

### /monitor - Trending and Health
Six rolling 10-minute strip charts (Forward Power, Reflected Power, SWR, PA Temperature, Drive Power, Drain Current), TX duty cycle gauge, TX cycle counter, and ACOM fault/warning display. Designed to run in a second browser window during long operating sessions, especially FT8.

Both views connect to the same backend via WebSocket and can run simultaneously in separate browser tabs or windows.

## Setup

### Prerequisites
- macOS (developed on Mac Studio M1)
- Python 3.12+
- Hamlib (brew install hamlib)
- Chrome browser (Safari HTTPS-only mode blocks localhost HTTP)

### Installation

    git clone https://github.com/tgilton/w7tlg_console.git
    cd w7tlg_console
    python3 -m venv venv
    source venv/bin/activate
    pip install fastapi uvicorn pyserial websockets

### Configuration
The ACOM serial port is hardcoded in dashboard/server.py. Update ACOM_PORT to match your FTDI adapter device path. Do not rely on find_acom_port() if multiple FTDI devices are present.

### Starting

    ~/start_rigctld.sh && sleep 2 && ~/start_console.sh

Open Chrome to http://localhost:8000 (dashboard) and optionally http://localhost:8000/monitor (trending).

### Stopping
Ctrl+C in the console terminal.

## Operating Modes

| Mode | Amp State | RF Power Limit | Notes |
|------|-----------|---------------|-------|
| AMP OFF | Standby (RF bypass) | 100W | Safe default |
| AMP ON | Operate | 40W drive | Requires confirmation dialog |

The console starts with AMP OFF and antenna defaulting to A4R (dummy load) for safety. The amp front panel antenna selection is authoritative and the console follows it.

## ACOM 1200S Integration Notes

### Critical Hardware Rules
- Never open the ACOM serial port while the console is running (the console owns it exclusively)
- Never send cmd_standby() on serial reconnect
- Opening the serial port asserts DTR/RTS which disturbs the amp firmware
- The 06AT tuner has no independent power (powered via RF coax from the 1200S)
- After a rear power switch cycle, wait 30+ seconds before powering back on

### ATU Recovery Procedure
If ATU/ASEL communication is lost:
1. Rear power switch OFF
2. Wait 30 seconds
3. Rear power switch ON, then front panel power button
4. Wait for TEST, S, R boot sequence on amp display
5. Set ANTENNA TUNER INSTALLED to YES in Preferences menu
6. Key FT-991A briefly in CW to trigger ATU initialization handshake

### Telemetry
The ACOM 1200S streams 68-byte telemetry frames (72 bytes on the wire including header) at approximately 10Hz via RS232. The console parses these for forward power, reflected power, SWR, PA temperature, drain current, HV voltage, and fault status.

### Antenna Switching
The 0x09 antenna-select command is implemented but the 1200S currently ignores it. ACOM support has been contacted. The reference implementation (bjornekelund/ACOM-Controller on GitHub) also does not implement remote antenna selection. Antenna changes must be made on the amp front panel; the console reads and follows them via 0x27 telemetry messages.

## Digital Mode Integration

The console coexists with the standard FT8 software chain:
- WSJT-X (digital mode encode/decode)
- GridTracker2 (grid square tracking and POTA spotting)
- RUMLogNG (QSO logging)

These applications share the FT-991A via rigctld and communicate with each other via UDP.

## Project Structure

    w7tlg_console/
    +-- amplifier/
    |   +-- acom_bridge.py       Ties rig + amp, safety interlocks, trending
    |   +-- acom_protocol.py     ACOM binary protocol: frames, commands, telemetry
    |   +-- acom_serial.py       Async serial port manager for ACOM 1200S
    +-- dashboard/
    |   +-- server.py            FastAPI app, WebSocket handlers, routes
    |   +-- index.html           Operating console UI
    |   +-- monitor.html         Trending/health monitoring UI
    +-- rig/
    |   +-- rigctld_client.py    Async Hamlib rigctld TCP client
    +-- bridge/
    +-- config/
    +-- tests/
    +-- README.md
    +-- ARCHITECTURE.md

See [ARCHITECTURE.md](ARCHITECTURE.md) for internal design: data flow, module responsibilities, safety interlocks, and known limitations.

## License

Personal project, not currently licensed for redistribution.

## Author

Terry Gilton, W7TLG - Boise, Idaho (Grid DN13WN)
