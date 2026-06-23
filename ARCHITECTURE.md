# Architecture

Internal design reference for w7tlg-console. For setup, hardware, and operating instructions see [README.md](README.md).

## Overview

The console is a single FastAPI process that owns three persistent connections — Hamlib `rigctld` (TCP), the ACOM 1200S (serial), and the SDRplay RSPdx-R2 (native vendor API) — and fans state out to any number of browser tabs over WebSocket. There is no database; all state is in-memory for the life of the process.

**The SDR, not the radio's own receiver, is what's actually heard.** Under this station's SDR Switch wiring, the antenna is on the RSPdx-R2 for RX; the radio's own receive antenna port sees nothing except briefly during TX (when the switch hands the antenna to the TX chain). `rig.strength_db`/the radio's own AGC are therefore dead for anything audible — RX audio, S-meter, and noise reduction/EQ all come from the SDR's own demodulation (`sdr/audio_demod.py`), not the FT-991A. This same SDR audio also feeds digital-mode software (WSJT-X etc.) over a virtual audio cable (`sdr/virtual_audio_output.py`), so voice and digital modes share one RX path with no antenna/hardware switch between them — see "Digital Mode Integration" in [README.md](README.md).

```
        ┌──────────┐   ┌──────────────────┐   ┌──────────────┐
        │ rigctld  │   │   ACOM 1200S     │   │  RSPdx-R2    │
        │ (Hamlib) │   │  (serial/RS232)  │   │ (native API) │
        └────┬─────┘   └────────┬─────────┘   └──────┬───────┘
          TCP:4532          binary frames          IQ stream
             │                   │                     │
        ┌────▼─────┐    ┌────────▼────────┐    ┌───────▼────────┐
        │RigctldCl.│    │   AcomSerial    │    │   SdrClient     │
        │ rig/     │    │   amplifier/    │    │ sdr/            │
        └────┬─────┘    └────────┬────────┘    └───────┬─────────┘
             │ state callbacks    │ telemetry/fault/      │ spectrum frames
             │                    │ antenna-change          │ + AudioDemodulator
             └──────────┬─────────┘                         │ (EQ/NR/AGC, voice
                        ▼                                   │ <-> digital profile)
                ┌─────────────────┐                         │
                │   AcomBridge     │  safety interlocks,    │
                │ amplifier/       │  trending, duty cycle   │
                │ acom_bridge.py   │                         │
                └────────┬─────────┘                         │
                         │ StationState                      │
                         ▼                                   │
                ┌─────────────────────────────────────────────▼──┐
                │                FastAPI app                     │  REST + WebSocket
                │              dashboard/server.py               │
                └────────┬──────────────┬──────────────┬─────────┘
                         │ state/cmd     │ spectrum     │ audio
                         ▼               ▼              ▼
                index.html (/)   panadapter.html   monitor.html
                operating console  (/panadapter)    (/monitor)
                                                          │
                                                          ▼ (also)
                                              DigitalAudioOutput → BlackHole
                                              → WSJT-X/digital-mode software
```

## Modules

### `rig/rigctld_client.py`
Async TCP client speaking the Hamlib rigctld text protocol to the FT-991A (model 1035). Polls on an interval (default 0.5s) and exposes `RigState` — frequency, mode, S-meter, PTT, DSP settings (NB, DNR level, DNF, AGC), mic gain, compression, `is_digital`/`near_digital_freq`. `update_derived()` computes band and display-formatted frequency from raw Hz and derives `is_digital` from the rig's actual Hamlib mode strings (`PKTUSB`/`PKTLSB` — confirmed via `rigctl --dump-caps -m 1035`; plain `USB`/`LSB` are voice, not digital, despite the name similarity). Notifies subscribers via `on_state_change(cb)`.

`send_raw_cmd()` is a Hamlib raw-CAT passthrough (`w <cmd>`) for parameters not exposed as a standard Hamlib level on this rig — currently used for `get_dt_gain()`/`set_dt_gain()`, which read/write CAT menu **073 "DATA OUT LEVEL"** (operators call this "DT GAIN" — the digital-mode TX audio drive level from USB/DATA into the modulator; not menu 049 "AM DATA GAIN", which is unrelated/AM-only). Identified from the official Yaesu FT-991A CAT manual and live-verified against the real rig: `w EX073;` returns the radio's raw echo, e.g. `EX073030;`.

### `sdr/sdr_client.py`, `sdr/audio_demod.py`, `sdr/virtual_audio_output.py`
Owns the SDRplay RSPdx-R2 session (native vendor callback thread → bounded queue → dedicated consumer thread → asyncio, see inline comments for the thread-bridge rationale) and turns its IQ stream into both spectrum frames (for the panadapter) and demodulated audio.

`AudioDemodulator` (`audio_demod.py`) demodulates one SSB channel and runs it through a small audio chain: decimate → SSB bandpass filter (passband `[low_cut_hz, low_cut_hz + bandwidth_hz]`) → 3-band EQ (RBJ cookbook shelf/peak biquads, `sosfilt` with state carried across blocks) → noise reduction (DeepFilterNet3, run on a rolling ~0.4s window rather than per-block — the model resets its own hidden state on every call, so feeding it this file's tiny ~8ms blocks directly would reset context constantly; see the docstring on `_apply_nr`) → AGC (time-constant-based, not a fixed per-call fraction, so behavior stays correct regardless of block size) → manual gain → hard limiter. `enter_digital_mode()`/`exit_digital_mode()` snapshot and force this chain to a different profile (AGC off, NR/EQ bypassed, passband widened to start at the dial frequency) — driven by `RigState.is_digital` transitions, wired in `dashboard/server.py`'s `on_rig_state_for_audio_mode`.

`DigitalAudioOutput` (`virtual_audio_output.py`) is a second, always-on subscriber on the same `AudioDemodulator.on_audio()` hook the browser uses — it resamples to 48kHz and writes to a virtual audio device ("BlackHole 2ch") so digital-mode software can use the SDR's RX audio as its soundcard input, instead of the antenna needing to be switched back to the radio's own receiver. TX is unaffected — digital-mode software still drives the radio's USB Audio CODEC for transmit audio.

**Digital-mode fine spectrum.** `AudioDemodulator` also runs a second, much finer-resolution FFT (4096-point, ~3.9Hz/bin) directly on its own decimated 16kHz baseband — the same signal already used for SSB demod, already centered exactly on the dial frequency, so no separate retuning concept is needed the way the wideband capture has one. Only computed while `in_digital_mode` (set by `enter_digital_mode()`/`exit_digital_mode()`); 4-frame exponential-power averaged (same technique as `SdrClient`'s wideband averaging, reset at the start of each RX cycle so it never blends stale pre-transmission content into a new one) to cut flicker, matching WSJT-X's own spectrum display. Published via `on_fine_spectrum(cb)`, wired in `SdrClient.__init__` straight into the same `_publish`/`on_spectrum` fan-out the wideband frames use, distinguished only by a `"kind": "fine"` tag on the frame dict — `panadapter.html` renders from whichever frame kind is active for the current mode, and locks the view to exactly 3000Hz wide with the dial pinned to the left edge while in digital mode (enforced every tick in `handleDigitalViewMode()`, frozen during TX so it doesn't chase WSJT-X's own small dial wobble around each transmission — see the project's Claude memory `project_wsjtx_rigctld_gotchas` for why that wobble exists and how to recognize it).

### `amplifier/acom_protocol.py`
Pure protocol library, no I/O. Encodes/decodes the ACOM binary frame format (`0x55` start byte, address, length, data, checksum). Defines the message address space (`AmpMsg` amp→computer, `CmdMsg`/`AmpCmd` computer→amp), amplifier mode codes (`STB`, `OPR_RX`, `OPR_TX`, `ATAC`, `TURN_OFF`, `TX_PROHIBIT`/`TX_ALLOW`), and the band table. `FULL_TELEMETRY` (`0x2F`) is the primary 72-byte-on-wire (68-byte payload) telemetry message sent ~10x/sec; the `0x23`–`0x26` legacy messages are superseded by it.

`ANT_BAND_SELECT` (`0x09`) drives two independent, non-obvious behaviors confirmed against an engineer-supplied v1.3 protocol doc plus live hardware (see Known Limitations below for the full story):
- `cmd_next_antenna()` — Byte5=`0x30`, cycles to the next antenna exactly like the amp's front-panel ANT button. There is no direct "select antenna N" (Byte4, the antenna number, is ignored by firmware) and no "previous antenna."
- `cmd_select_band(band)` — Byte5=raw band number (`0x01`–`0x0A`). Not in the documented Byte5 value list for this sub-command (which only enumerates the cycle codes), but required in practice: it's the only way the amp's band/LPF tracks the radio while in STANDBY, since its own RF frequency counter has nothing to detect without drive power.

### `amplifier/acom_serial.py`
Async serial port owner (`pyserial` under asyncio). Opens the FTDI device exclusively, frames/deframes bytes per `acom_protocol`, and fires `on_telemetry`, `on_fault`, `on_antenna_change`, `on_connection_change` callbacks. `find_acom_port()` exists for auto-detection but the README explicitly warns against relying on it when multiple FTDI devices are present — `ACOM_PORT` in `dashboard/server.py` is hardcoded instead.

### `amplifier/acom_bridge.py`
The coordinator — the only module that knows about both the rig and the amp at once. Responsibilities:
- **`OperatingMode`** state machine: `AMP_OFF` (RF bypass, 100W rig limit, safe default) ↔ `AMP_ON` (amp in circuit, 40W drive limit, requires `confirmed=True` from the caller).
- **TX inhibit**: driven only by the amp's own firmware-computed hard/soft fault bits (message `0x21`), the dummy-load 10s hard cutoff, serial connection loss, or manual operator action — see "Safety interlocks" below. Reflected power/SWR is *not* an inhibit source; it's a passive operator notification only (`swr_warning_active`/`swr_warning_peak` on `StationState`, computed from the amp's own reported SWR, threshold 2.5) — an earlier console-side "2x reflected-power baseline" auto-inhibit heuristic was removed after it falsely tripped on normal variation and had no reliable way to clear.
- **Trending**: a `deque(maxlen=6000)` ring buffer of `TrendSample` at ~10Hz (~10 min window) plus a separate 5-min duty-cycle sample buffer; `get_trend_data(since)` serves incremental updates to `/monitor`.
- **TX cycle counting** and **duty-cycle calculation** (`_calc_duty_cycle`).
- **`StationState`**: the single flat dataclass that represents everything a browser needs to render — rig sub-state plus amp telemetry (fwd/refl power, SWR, drive, temp, HV, current), fault lists (hard/soft/warnings + overall `fault_severity`), operating mode, selected antenna, drive limit, TX inhibit state, duty cycle, dummy-load timer, SWR warning. `to_dict()` is what actually goes over the wire.
- Antenna selection is **cycle-only, driven both ways**: `next_antenna()` sends the same forward-cycle command as the front-panel ANT button; `_on_antenna_change` updates `selected_antenna` from the amp's `0x27` feedback regardless of which side triggered the change, so the console stays in sync either way. Note the amp's built-in 4 relay antennas report 0-indexed in `0x27` (`ant_num + 1` in `_on_antenna_change`) — confirmed against real hardware, contrary to the protocol doc's generic `[1..10]` wording (which describes the ASEL 10-antenna accessory case).

### `dashboard/server.py`
FastAPI app. One `AcomBridge` instance and one `SdrClient` instance live in module-level `bridge`/`sdr`, constructed in the `lifespan` context manager (so serial/TCP connections open on startup and close cleanly on shutdown — important given the ACOM's DTR/RTS sensitivity, see README). A `ConnectionManager` tracks all open WebSockets and broadcasts every `StationState` change to all of them — there's no per-client filtering, every tab sees everything. Separate `SpectrumConnectionManager`/`AudioConnectionManager` handle the higher-rate panadapter spectrum/audio streams on their own WS endpoints (`/ws/spectrum`, `/ws/audio`), since those are fine to drop-if-slow where the main state stream is not. `SpectrumConnectionManager.broadcast_frame` passes through a `"kind"` tag (`"wide"` or `"fine"`) from whatever `SdrClient`/`AudioDemodulator` published, so the frontend can tell the two spectrum sources apart without a second endpoint.

`on_rig_state_for_audio_mode` is a second subscriber on `RigctldClient.on_state_change` (separate from `AcomBridge`'s own) that edge-triggers `sdr.audio.enter_digital_mode()`/`exit_digital_mode()` off `RigState.is_digital` — purely an SDR-audio concern, not a safety interlock, so it's kept out of `AcomBridge`.

REST surface (`/api/state`, `/api/mode`, `/api/antenna/next`, `/api/tx`) duplicates a subset of what's also reachable over the WebSocket command channel — REST for one-shot actions/polling, WebSocket for the live stream plus the same actions inline (`set_mode_op`, `set_frequency`, `set_mode`, `set_rf_power`, `set_preamp`, `set_mic_gain`, `set_comp`, `set_nb`, `set_audio_nr`, `set_eq`, `set_dt_gain`, `set_dnf`, `set_agc`, `set_rx_volume`, `next_antenna`, `inhibit_tx`/`allow_tx`, `get_trend`, plus the panadapter's `set_panadapter_freq`/`set_audio_target`/`set_audio_enabled`). Every WS command gets a `cmd_response` echoed back to the sender; state changes are broadcast to everyone independent of who issued the command.

### `dashboard/index.html` / `dashboard/monitor.html` / `dashboard/panadapter.html`
No build step — plain HTML/CSS/JS served directly from disk by `server.py`. All three connect to `/ws` for state/commands; `monitor.html` additionally polls `get_trend` to backfill its strip charts on load/reconnect, and `panadapter.html` additionally connects to `/ws/spectrum` and `/ws/audio` for the waterfall display and RX audio playback (via an `AudioWorklet` ring buffer, immune to per-message scheduling jitter).

## Data flow summary

1. `RigctldClient`, `AcomSerial`, and `SdrClient` each poll/listen their hardware independently and call into `AcomBridge` (rig+amp) or directly into `server.py` (SDR) via callbacks.
2. `AcomBridge` merges rig+amp into one `StationState`, applies safety logic (mode limits, TX inhibit, SWR warning), and invokes its own `on_state_change` callbacks.
3. `server.py`'s `on_station_state` handler broadcasts the new state as `{"type": "state", "data": ...}` to every connected browser; `build_state_payload` layers in SDR-derived fields (S-meter from the SDR spectrum, EQ/NR/AGC config, digital-audio feed status) since those don't live in `AcomBridge`'s `StationState`.
4. Browser-originated commands (slider drags, button clicks) go out over the same WebSocket as `{"cmd": "..."}` messages, handled by `handle_ws_command`, which calls back into `AcomBridge`/`RigctldClient`/`sdr.audio` and replies with a `cmd_response` — the resulting state change then arrives separately via the next broadcast.
5. Separately, `AudioDemodulator` fans its demodulated RX audio out to every registered subscriber — the browser (via `/ws/audio`) and `DigitalAudioOutput` (via a virtual audio cable to digital-mode software) both just subscribe to the same `on_audio()` hook; neither knows about the other.

## Safety interlocks (where they live in code)

| Interlock | Enforced in |
|---|---|
| AMP_OFF default on boot, antenna defaults to A4R (dummy load) | `AcomBridge.__init__` / `StationState` defaults |
| AMP_ON requires explicit confirmation | `AcomBridge.set_operating_mode(confirmed=...)`, surfaced as a confirm dialog in `index.html` |
| Drive power capped per mode (100W / 40W) | `StationState.drive_limit_w`, enforced both in `set_rf_power` (server clamps `pct`) and exposed to the UI |
| TX inhibit on amp hard/soft fault, dummy-load 10s cutoff, serial loss, or manual inhibit | `AcomBridge.inhibit_tx`/`allow_tx`, called from `_on_fault`/`_dummy_load_watchdog`/`_on_amp_connection` |
| Antenna is cycle-only (no jump-to-antenna), console mirrors whichever side changed it | `next_antenna()` / `_on_antenna_change` in `acom_bridge.py` |
| Serial port opened exactly once, never re-asserted on reconnect | `acom_serial.py` connection handling — see README's "Critical Hardware Rules" for the DTR/RTS hazard this avoids |

Reflected power/SWR is deliberately *not* in this table — see the `acom_bridge.py` module note above.

## Known limitations

- **No direct antenna selection — cycle only.** The 1200S firmware has no "select antenna N" command at all (confirmed against an engineer-supplied v1.3 protocol doc, which superseded an earlier A600S-only v1.1 doc this codebase was originally built against, plus live hardware testing). The console drives antenna changes the same way the front-panel ANT button does — `cmd_next_antenna()`, forward cycling only, no "previous" — and the firmware itself skips antennas not assigned to the current band (e.g. on 40m it only toggles between two of the four). Full byte-level writeup of doc-vs-hardware discrepancies (the antenna number being ignored, the band number working despite being undocumented for this sub-command, the 0-indexed `0x27` antenna field) is in this project's Claude memory (`project_acom_1200s_protocol`), not duplicated here.
- **No automated test suite.** `tests/` contains only an empty `__init__.py`.
- **Single hardcoded serial port.** `ACOM_PORT` in `server.py` must be updated by hand when the FTDI adapter's device path changes (it has, at least twice, per git history); `find_acom_port()` exists but is unreliable with multiple FTDI devices attached.
- **DeepFilterNet adds latency, not used at its full real-time potential.** `AudioDemodulator._apply_nr` runs DeepFilterNet3 on a rolling ~0.4s window rather than true frame-at-a-time streaming, because its public `enhance()` API resets the model's hidden state on every call — calling it per-~8ms-block (this file's normal cadence) would reset that context constantly and degrade quality. The safe fix costs latency (~0.4s when NR is on) instead of the ~10-20ms the model is capable of with proper frame-level streaming against its internal (undocumented) state-carrying API. Revisit if the added latency turns out to be perceptible/annoying in practice.
- **Raw CAT passthrough (`send_raw_cmd`/DT GAIN) shares a serial link with WSJT-X and can stall for seconds.** Confirmed live: a contended `w EX073;` read blocked the shared poll connection long enough to delay PTT/frequency broadcasts by several seconds, which showed up as real TX leakage briefly rendering in the panadapter before the freeze caught up. `get_dt_gain()` now uses its own short-lived connection and is polled as a detached background task (see `rigctld_client.py`'s `_poll_dt_gain`) so it can no longer block the main poll cycle — any *future* raw-passthrough addition should follow the same pattern rather than awaiting inline.
