# W7TLG Station Console

A web-based ham radio station control console for the W7TLG operating position. Provides unified monitoring and control of a Yaesu FT-991A transceiver and ACOM 1200S linear amplifier from a browser, replacing the need to constantly glance at multiple hardware displays.

## Station Hardware

| Equipment | Connection | Role |
|-----------|-----------|------|
| Yaesu FT-991A | USB to Mac Studio | Transceiver, all modes HF/VHF/UHF |
| SDRplay RSPdx-R2 | USB to Mac Studio | Actual RX receiver — see "Receive Audio Architecture" below |
| ACOM 1200S | FTDI USB-to-RS232 | 1200W linear amplifier |
| ACOM 06AT | RF coax from 1200S | Automatic antenna tuner (powered via 1200S) |
| SS-25/DXF (A1F) | Coax | Vertical antenna |
| 40m EFHW (A3R) | Coax | Multiband end-fed half-wave, primary operating antenna |
| Dummy Load (A4R) | Coax | 50 ohm dummy load |

An SDR Switch keeps the antenna on the RSPdx-R2 for RX, handing it to the TX chain only for the duration of a transmission (to protect the SDR front end from the amplifier's output).

## Software Stack

- Python 3.12 / FastAPI / WebSockets (backend server)
- Hamlib rigctld (transceiver control, FT-991A model 1035)
- Custom ACOM 1200S serial protocol (binary telemetry and command framing)
- SDRplay API (RSPdx-R2 IQ capture), numpy/scipy (demodulation, EQ, AGC)
- DeepFilterNet3 (real-time noise reduction), `sounddevice`/BlackHole (digital-mode virtual audio cable)
- Vanilla HTML/JS/CSS browser frontend (no build step)

## Views

### /dashboard - Operating Console
Primary operating interface with VFO display, band/mode selection, S-meter, TX meters, RF power slider, preamp selector, DSP controls (NB, DNR/NR, DNF, AGC, 3-band EQ), SSB audio controls (Mic Gain, Compression) which swap for a DT GAIN control in digital modes, ACOM telemetry panels, AMP ON/OFF with safety interlock, and antenna selection.

### /panadapter - SDR Spectrum/Waterfall
Real-time spectrum and waterfall display from the RSPdx-R2, click-to-tune, and RX audio playback in-browser. This is the actual receive path for both voice and digital modes — see "Receive Audio Architecture" below.

In digital modes (DATA-U), the view locks to a 3kHz window pinned to the dial frequency at the left edge — not centered, and not user-adjustable while in this mode — fed by a separate, much finer-resolution FFT (~3.9Hz/bin vs ~30Hz/bin on the normal wideband view) so individual FT8 signals are actually resolved instead of blurring into a handful of bins. Outside digital modes the view is unaffected: scroll-to-zoom/drag-to-pan, no auto-centering.

### /monitor - Trending and Health
Six rolling 10-minute strip charts (Forward Power, Reflected Power, SWR, PA Temperature, Drive Power, Drain Current), TX duty cycle gauge, TX cycle counter, and ACOM fault/warning display. Designed to run in a second browser window during long operating sessions, especially FT8.

All three views connect to the same backend via WebSocket and can run simultaneously in separate browser tabs or windows.

## Setup

### Prerequisites
- macOS (developed on Mac Studio M1)
- Python 3.12+
- Hamlib (brew install hamlib)
- Chrome browser (Safari HTTPS-only mode blocks localhost HTTP)
- SDRplay API + RSPdx-R2 for the panadapter/SDR-receive features
- BlackHole 2ch (`brew install blackhole-2ch`, then reboot — the installer needs admin password and a restart to take effect) if you want digital-mode software to receive over the virtual audio cable instead of the radio's own receiver

### Installation

    git clone https://github.com/tgilton/w7tlg_console.git
    cd w7tlg_console
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

### Configuration
The ACOM serial port is hardcoded in dashboard/server.py. Update ACOM_PORT to match your FTDI adapter device path. Do not rely on find_acom_port() if multiple FTDI devices are present.

### Starting

    ~/start_w7tlg.sh

Starts rigctld if it isn't already running, starts the console, and opens the dashboard and panadapter tabs in Chrome. Load http://localhost:8000/monitor manually if you want the trending view too.

To start the pieces individually instead:

    ~/start_rigctld.sh && sleep 2 && ~/start_console.sh

### Stopping
Ctrl+C in the console terminal.

## Operating Modes

| Mode | Amp State | RF Power Limit | Notes |
|------|-----------|---------------|-------|
| AMP OFF | Standby (RF bypass) | 100W | Safe default |
| AMP ON | Operate | 40W drive | Requires confirmation dialog |

The console starts with AMP OFF and antenna defaulting to A4R (dummy load) for safety. Antenna changes (from either the console's NEXT ANT button or the amp's front panel) are mirrored on both sides — see "Antenna Switching" below.

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
The 1200S has no remote "select antenna N" command — confirmed against ACOM's own engineer-supplied v1.3 protocol doc and live hardware. The console's NEXT ANT button sends the same forward-only cycle command as the amp's front-panel ANT button (`cmd_next_antenna()`); the firmware itself skips antennas not assigned to the current band. The console's antenna indicator follows the amp's `0x27` telemetry regardless of which side triggered the change, so it stays in sync either way.

Band-following (so the amp's display/LPF tracks the radio even while in STANDBY, when the amp has no drive RF for its own frequency counter to sense band from) is sent automatically on every band change via the same `0x09` command's band-number field — this isn't in ACOM's documented value list for that field, but is required and confirmed working in practice.

## Receive Audio Architecture

Under this station's SDR Switch wiring, the FT-991A's own receive antenna port sees nothing during RX — the RSPdx-R2 is the actual receiver, for both voice and digital modes. RX audio, the S-meter, noise reduction, and EQ all come from the SDR's demodulation (`sdr/audio_demod.py`), not the radio. The radio's own NB/DNF and AGC settings have no audible effect for this reason; the console's DNR/EQ/AGC controls act on the SDR audio chain instead.

That demodulated audio has two simultaneous consumers: the panadapter browser tab (for listening), and a virtual audio cable (`sdr/virtual_audio_output.py`) for digital-mode software — meaning **no antenna or hardware switch is needed to move between voice and digital operation.** TX still goes through the real radio either way (the SDR can't transmit) — only the RX audio source is unified.

### Noise Reduction & EQ
DeepFilterNet3 (best-in-class real-time speech enhancement) and a 3-band EQ (bass/mid/treble) run on the SDR audio chain, controlled from the DNR/EQ controls on `/dashboard`. NR runs on a rolling ~0.4s buffer rather than true frame-at-a-time streaming (see `ARCHITECTURE.md`'s Known Limitations for why), so there's a noticeable but bounded added delay when NR is on — toggle it off if that's bothersome for a given session.

### Digital Mode Setup (one-time)
1. Install BlackHole 2ch (`brew install blackhole-2ch`), reboot.
2. In WSJT-X (or other digital-mode software): set the **Rx** soundcard device to "BlackHole 2ch". Leave the **Tx** soundcard device on the FT-991A's "USB Audio CODEC" (unchanged) — transmit audio still has to go through the real radio.
3. Leave CAT/rig control in WSJT-X pointed at rigctld as before — unaffected by any of this.

### Operating
Tune to the digital sub-band (e.g. via the panadapter, same as tuning to a voice frequency) and press the console's **DATA-U** button. That does two things:
- Sets the radio's CAT mode to DATA-U (so the radio knows to key from USB/DATA audio, not the mic, when WSJT-X triggers PTT).
- Reconfigures the SDR audio chain for digital use: AGC off, NR/EQ bypassed, and the passband widened to start right at the dial frequency (0-3000Hz) instead of excluding voice rumble/hum — switching back to a voice mode restores whatever AGC/NR/EQ settings were active before.

The console's Mic Gain/Comp sliders swap for a **DT GAIN** slider in digital modes — this is CAT menu 073 "DATA OUT LEVEL", the USB/DATA audio drive level into the radio's modulator (keep this low enough that ALC doesn't move; see Yaesu's guidance on avoiding over-drive/distortion on DATA modes). A small badge near the VFO shows whether the BlackHole feed is actually active, so you can tell at a glance if WSJT-X should be receiving audio.

In WSJT-X, set **Split Operation** to **Rig**, not **Fake It** — on this rig, Fake It causes a real, visible dial-frequency jump at the start/end of every transmission (confirmed via live CAT logging, not a console bug) that the panadapter's digital-mode view would otherwise have to chase. `Rig` behaves more cleanly here and reverts reliably.

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
    |   +-- panadapter.html      SDR spectrum/waterfall + RX audio UI
    +-- rig/
    |   +-- rigctld_client.py    Async Hamlib rigctld TCP client
    +-- sdr/
    |   +-- sdr_client.py            RSPdx-R2 IQ capture + FFT pipeline
    |   +-- audio_demod.py           SSB demod, EQ, DeepFilterNet NR, AGC, voice/digital profiles
    |   +-- virtual_audio_output.py  BlackHole bridge for digital-mode software
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
