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
- WSJT-X's own UDP multicast protocol (own QDataStream binary parser, `wsjtx/protocol.py`) — live rig/decode/QSO-logged events, no config changes to WSJT-X
- `sqlite3` (stdlib) — reads RUMLogNG's own logbook database directly, read-only
- `httpx` — PSKReporter, NOAA space weather, POTA, HamQTH/QRZ XML APIs
- `anthropic` — AI band advisor (Claude), streaming chat with optional auto-QSY tool-calling
- Raw asyncio telnet sockets — Reverse Beacon Network (CW) and a DXSpider cluster node (SSB + everything else), for live spotting outside digital modes
- Vanilla HTML/JS/CSS browser frontend (no build step)

## Views

### /console - Unified Operating Console (primary way to operate)
A single browser window: the panadapter in a persistent, resizable left pane (drag the handle to resize), and a tabbed right pane — Dashboard, Monitor, Propagation, Spot/Seek, AI Advisor. Built from iframes pointing at the standalone pages below rather than one merged page — each tab keeps running (WebSocket connections, trend logging, etc.) even while a different tab is visually active, since the iframe is hidden, not unmounted. See `dashboard/console.html`'s own docstring and [ARCHITECTURE.md](ARCHITECTURE.md) for why.

All of `/`, `/panadapter`, `/monitor`, `/propagation`, `/spotseek`, `/advisor` also still work standalone in their own browser tab/window if you'd rather not use the unified shell — `/console` is additive, nothing was removed.

#### Dashboard tab (/)
Primary operating interface with VFO display, band/mode selection, S-meter, TX meters, RF power slider, preamp selector, DSP controls (NB, DNR/NR, DNF, AGC, 3-band EQ), SSB audio controls (Mic Gain, Compression) which swap for a DT GAIN control in digital modes, ACOM telemetry panels, AMP ON/OFF with safety interlock, and antenna selection.

#### Panadapter (left pane, or /panadapter standalone)
Real-time spectrum and waterfall display from the RSPdx-R2, click-to-tune, and RX audio playback in-browser. This is the actual receive path for both voice and digital modes — see "Receive Audio Architecture" below.

In digital modes (DATA-U), the view locks to a 3kHz window pinned to the dial frequency at the left edge once the fine-resolution spectrum data is actually flowing (which requires audio to have been enabled at least once — the panadapter defaults to **Audio: Live** on page load for exactly this reason, accepting a possible unmuted-startup sound as the tradeoff; see `panadapter.html`'s `enableAudio()` comment) — not centered, and not user-adjustable while in this mode — fed by a separate, much finer-resolution FFT (~3.9Hz/bin vs ~30Hz/bin on the normal wideband view) so individual FT8 signals are actually resolved instead of blurring into a handful of bins. Outside digital modes the view is unaffected: scroll-to-zoom/drag-to-pan, no auto-centering. Click-to-tune is disabled while in digital mode — the narrow view means any click would otherwise nudge the actual rig VFO by a few hundred Hz, which is disruptive to FT8/digital timing.

#### Monitor tab (/monitor)
Six rolling 10-minute strip charts (Forward Power, Reflected Power, SWR, PA Temperature, Drive Power, Drain Current), TX duty cycle gauge, TX cycle counter, and ACOM fault/warning display.

Trend-CSV logging (`data/trend_logs/`) runs for as long as the `/console` shell (or a standalone `/monitor` tab) is open, gated by a heartbeat the page sends every 4s — see `amplifier/trend_csv_logger.py`.

#### Propagation tab (/propagation)
Live FT8 band activity (spot counts, avg SNR, DXCC entities heard per band) from PSKReporter, current solar indices (SFI/Kp) from NOAA, a feed of automatic band-opening/closing/Kp-spike alerts with a one-line Claude-generated explanation for each, and the **QTH toggle** (La Quinta CA / Boise ID) — switching it re-targets the PSKReporter queries at the new grid square automatically. See "QTH Profile" and "AI Advisor" below.

#### Spot/Seek tab (/spotseek)
- **Manual callsign lookup** (HamQTH, with QRZ as a fallback if configured) — for SSB/CW, where there's no automated decode feed the way WSJT-X provides for digital; type a callsign you just heard and get name/grid/state/country plus whether it's on your watch list or already worked.
- **Watch list** — callsigns you always want to know about the moment they're heard, across *all* spotting sources (WSJT-X decodes, RBN, DX cluster).
- **Live alerts feed** — watch-list matches, a grid-boundary heuristic for "possible Idaho station" (US callsign prefixes don't map to states, so this checks decoded/spotted grid squares against Idaho's approximate bounding box — real border is irregular, so treat it as "worth a look," not certain), and live POTA activations.
- **Award tracking** — Worked All States by band, and DXCC entities worked, both sourced from RUMLogNG's own database (see "RUMLogNG Integration" below).

#### AI Advisor tab (/advisor)
A streaming chat interface (Claude) with live rig state and propagation data as automatic context — ask a question or click "Recommend Now" for an immediate band/strategy assessment. **Auto-QSY** is an explicit, session-level, off-by-default toggle (visibly red/pulsing when on) — when enabled, Claude may actually retune the radio via tool-calling rather than just describing what to do. This is the one place in the console where an LLM can command the radio directly.

### Antenna A/B Test (dashboard panel)
A receive-only tool for comparing antennas rigorously instead of by ear. Manually switching antennas and eyeballing a single frequency doesn't hold up on a busy band — any one signal can go quiet between rounds, and there's no way to be sure a "noise" reference frequency isn't actually someone else's QSO. Voice/CW/FT8 also have no flat signal level to begin with (they all spend part of their time "off"), so a single instantaneous reading is unreliable regardless.

Instead, for each antenna in turn, the test repeatedly sweeps every channel across a configurable frequency range against the SDR's already-computed spectrum, building a time history per channel — numerically the same information a long-exposure waterfall shows visually. Any channel whose 90th-percentile level rises ≥6dB above its own time-median counts as a real signal (catches the "on" portion of an intermittent transmission rather than averaging it away). This repeats for several rounds, switching antennas each time; afterward, channels that were active on **both** antennas in the **same round** are matched and averaged into a final signal/floor/SNR comparison — an actual apples-to-apples answer instead of a guess.

Antenna switching goes through `AcomBridge.goto_antenna()`, which cycles forward (the only direction the amp supports) with telemetry-confirmed retries, and aborts the whole test cleanly on TX start, an amp fault, or a dropped serial connection. Results stream live to the dashboard panel and log incrementally to `data/ab_tests/` (one CSV per run, plus a `_summary.csv`) so a stopped or crashed run doesn't lose completed rounds. See `amplifier/antenna_ab_test.py`.

All views connect to the same FastAPI backend via WebSocket and can run simultaneously, whether inside the `/console` shell's tabs or as separate standalone browser tabs/windows.

## QTH Profile

Two saved station profiles — La Quinta, CA (winter) and Boise, ID (rest of year) — switchable from a toggle on the Propagation tab. This is a manual toggle, not auto-detected: a wrong auto-detection (e.g. from IP geolocation) would silently corrupt data that matters for QSO confirmations and awards, which is worse than requiring one click. Switching QTH re-targets propagation queries (PSKReporter's `senderGrid`) and the AI advisor's location context at the new grid square automatically. See `config/station_profile.py`.

**RUMLogNG's own QTH/grid setting is separate and external** — it does not sync from this toggle and must still be changed there by hand when your QTH changes.

## WSJT-X Integration

The console joins WSJT-X's own UDP multicast group (`224.0.0.1:2237` on `lo0`, per this station's `~/Library/Preferences/WSJT-X.ini`) as a passive third listener — the same group RUMLogNG and GridTracker2 already use. Zero WSJT-X configuration changes, zero effect on those other listeners; this only ever reads, never sends anything back to WSJT-X. See `wsjtx/protocol.py` (the binary QDataStream parser, built directly from WSJT-X's own `NetworkMessage.hpp` source) and `wsjtx/udp_listener.py`.

This feed drives: live rig/DX-call status broadcast to the console (`Status` messages), the QSO performance telemetry logger (`LoggedAdif` messages, below), and the Spot/Seek watch-list/Idaho alerts (`Decode` messages, for FT8/digital — see "DX Cluster / RBN Integration" for how SSB/CW are covered instead).

## QSO Performance Telemetry

A continuous ~1Hz rolling buffer of station telemetry (forward/reflected power, SWR, PA temp, drive, HV, drain current, frequency, band, mode, antenna, S-meter) — separate from `/monitor`'s trend CSVs, and running independently of whether any particular browser tab is open. When WSJT-X logs a QSO, the buffer is sliced to that QSO's exact time window and summarized (min/avg/max per field) into one JSON record appended to `data/qso_performance_log.jsonl` — a diagnostic record of station/rig/band performance per contact, not a logbook (RUMLogNG remains the authoritative log). JSONL rather than CSV specifically so the field list can keep growing without needing to migrate old records.

Convert to CSV for spreadsheet analysis with `python3 tools/qso_log_to_csv.py` — flattens the nested JSON into dotted column names and takes the union of every column seen across the file as the header, so older records just get blank cells for fields that didn't exist yet when they were logged. See `wsjtx/qso_logger.py`.

## RUMLogNG Integration (award tracking)

The Spot/Seek tab's Worked-All-States and DXCC-entities-worked views read directly from RUMLogNG's own SQLite database — not an export, the actual live logbook file, found in a normal Dropbox-synced folder (`~/Library/CloudStorage/Dropbox/.../TG Log 001.rlog`), opened strictly read-only since it's RUMLogNG's own actively-open database. Falls back to WSJT-X's local ADIF log if that path is ever unreachable (less complete data, but keeps the console working). See `wsjtx/award_tracker.py`'s module docstring for the full data-source story, including which RUMLogNG fields turned out not to mean what their names suggest (its `qsl`/`lotwqsl`/`eqsl` columns don't encode per-QSO confirmed status — worked-only tracking is used instead) and which table is deliberately never queried (`prefs`, which holds the operator's actual LoTW/eQSL account credentials).

## DX Cluster / RBN Integration

WSJT-X's Decode feed only covers digital modes. For SSB and CW, real-time spotting comes from two other networks, both plain read-only telnet connections (login with callsign, never post spots):

- **Reverse Beacon Network** (`telnet.reversebeacon.net:7000`) — automated CW/RTTY skimmers.
- **A DXSpider cluster node** (`dxspider.co.uk:7300`) — the traditional, decades-old human-operated DX cluster network; the only real source for SSB spots, since there's no automated way to "decode" a voice signal into a spot. Carries all modes humans choose to post, including digital ones already covered by WSJT-X — mode is inferred from the spot's frequency (phone sub-band ranges) since this network has no dedicated mode column.

Both feed into the same Spot/Seek watch-list and Idaho-grid-heuristic alert logic as the WSJT-X path. See `wsjtx/dx_cluster.py`.

## AI Advisor

Ported from an earlier, simpler project (`ft991a-panel`) and adapted to this console's actual data shapes and QTH-aware grid. Packages current rig state and live propagation data (PSKReporter band activity, NOAA SFI/Kp) into context for Claude, which responds as an HF propagation/DX strategy advisor — streamed token-by-token over SSE. A background poll (every 3 minutes, matching PSKReporter's own requested minimum interval) also runs change-detection for band openings/closings and Kp spikes, generating a one-line Claude explanation for each and broadcasting it to every connected view.

**Auto-QSY** is an explicit opt-in, off by default every page load, never persisted: only when enabled does Claude's `qsy_to_band` tool actually get offered, and only then can a response result in the radio's frequency/mode actually changing. See `advisor/claude_advisor.py`.

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

**Credentials (`.env`):** copy `.env.example` to `.env` and fill in real values — never commit `.env` itself (already gitignored). Loaded automatically on startup via `load_dotenv()` in `main.py`, no manual sourcing needed.

    ANTHROPIC_API_KEY=       # required for the AI Advisor tab
    HAMQTH_USERNAME=         # optional — manual callsign lookup (free account)
    HAMQTH_PASSWORD=
    QRZ_USERNAME=            # optional — supplements HamQTH; needs a paid Logbook Data
    QRZ_PASSWORD=            # subscription for full field coverage, otherwise login fails

The Spot/Seek and Propagation tabs still work without any of these except the manual lookup (needs at least one of HamQTH/QRZ) and the AI Advisor (needs the Anthropic key).

### Starting

    ~/start_w7tlg.sh

Starts rigctld if it isn't already running, starts the console, and opens the dashboard and panadapter tabs in Chrome. Load **http://localhost:8000/console** for the unified single-window shell (all 5 tabs), or the individual standalone routes (`/`, `/panadapter`, `/monitor`, `/propagation`, `/spotseek`, `/advisor`) if you'd rather run them separately.

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

These applications share the FT-991A via rigctld and communicate with each other via UDP — and this console is now also part of that picture, as a passive fourth listener on WSJT-X's UDP feed (see "WSJT-X Integration" above) and a direct read-only reader of RUMLogNG's own database (see "RUMLogNG Integration" above). It never sends anything to WSJT-X or writes anything to RUMLogNG's log — GridTracker2's own POTA-spotting role stays fully intact and independent; the console's own POTA feed (Spot/Seek tab) is a separate, additional source, not a replacement.

## Project Structure

    w7tlg_console/
    +-- amplifier/
    |   +-- acom_bridge.py       Ties rig + amp, safety interlocks, trending
    |   +-- acom_protocol.py     ACOM binary protocol: frames, commands, telemetry
    |   +-- acom_serial.py       Async serial port manager for ACOM 1200S
    |   +-- antenna_ab_test.py   Band-profiling antenna A/B test (see "Antenna A/B Test" above)
    |   +-- trend_csv_logger.py  Persists trend samples to CSV while /monitor is open
    +-- dashboard/
    |   +-- server.py            FastAPI app, WebSocket handlers, routes
    |   +-- console.html         Unified single-window shell (panadapter pane + tabs)
    |   +-- index.html           Operating console UI (Dashboard tab)
    |   +-- monitor.html         Trending/health monitoring UI (Monitor tab)
    |   +-- panadapter.html      SDR spectrum/waterfall + RX audio UI (left pane)
    |   +-- propagation.html     Solar/band-activity/alerts + QTH toggle (Propagation tab)
    |   +-- spotseek.html        Watch list, lookup, live alerts, WAS/DXCC (Spot/Seek tab)
    |   +-- advisor.html         AI advisor chat + auto-QSY toggle (AI Advisor tab)
    +-- rig/
    |   +-- rigctld_client.py    Async Hamlib rigctld TCP client
    +-- sdr/
    |   +-- sdr_client.py            RSPdx-R2 IQ capture + FFT pipeline
    |   +-- audio_demod.py           SSB demod, EQ, DeepFilterNet NR, AGC, voice/digital profiles
    |   +-- virtual_audio_output.py  BlackHole bridge for digital-mode software
    +-- wsjtx/
    |   +-- protocol.py          WSJT-X UDP binary (QDataStream) message parser
    |   +-- udp_listener.py      Joins WSJT-X's multicast group, dispatches parsed messages
    |   +-- adif.py              Minimal ADIF record parser
    |   +-- award_tracker.py     WAS/DXCC tracking — reads RUMLogNG's SQLite DB, falls back to WSJT-X ADIF
    |   +-- qso_logger.py        Per-QSO telemetry summary logger (JSONL)
    |   +-- spotter.py           Watch list, Idaho-grid heuristic, POTA polling, alert dispatch
    |   +-- callsign_lookup.py   Manual HamQTH/QRZ callsign lookup (SSB/CW)
    |   +-- dx_cluster.py        RBN (CW) + DXSpider cluster (SSB) telnet clients
    +-- advisor/
    |   +-- propagation.py       PSKReporter + NOAA solar data, QTH-aware
    |   +-- monitor.py           Band-opening/closing/Kp-spike change detection + Claude explanations
    |   +-- claude_advisor.py    Streaming chat advisor with opt-in auto-QSY tool-calling
    +-- config/
    |   +-- station_profile.py   QTH profile (La Quinta/Boise) config + persistence
    +-- tools/
    |   +-- qso_log_to_csv.py    Converts the QSO telemetry JSONL log to CSV
    +-- bridge/
    +-- tests/
    +-- data/                    Generated output (ab_tests/, trend_logs/, qso_performance_log.jsonl,
    |                            watchlist.json, station_profile.json) — gitignored
    +-- .env / .env.example      API credentials (ANTHROPIC_API_KEY, HAMQTH_*, QRZ_*) — .env gitignored
    +-- README.md
    +-- ARCHITECTURE.md

See [ARCHITECTURE.md](ARCHITECTURE.md) for internal design: data flow, module responsibilities, safety interlocks, and known limitations.

## License

MIT — see [LICENSE](LICENSE). This controls real RF/TX hardware; use at your own risk, no warranty.

## Author

Terry Gilton, W7TLG - Boise, Idaho (Grid DN13WN)
