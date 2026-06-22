# Architecture

Internal design reference for w7tlg-console. For setup, hardware, and operating instructions see [README.md](README.md).

## Overview

The console is a single FastAPI process that owns two persistent connections — Hamlib `rigctld` (TCP) and the ACOM 1200S (serial) — and fans state out to any number of browser tabs over WebSocket. There is no database; all state is in-memory for the life of the process, with one exception (per-antenna thermal inhibits) persisted to disk so they survive a restart.

```
                    ┌─────────────────┐       ┌──────────────────┐
                    │   rigctld       │       │   ACOM 1200S     │
                    │ (Hamlib daemon) │       │  (serial/RS232)  │
                    └────────┬────────┘       └─────────┬────────┘
                          TCP:4532                  binary frames
                             │                           │
                    ┌────────▼────────┐       ┌──────────▼────────┐
                    │ RigctldClient   │       │   AcomSerial      │
                    │ rig/            │       │   amplifier/      │
                    └────────┬────────┘       └──────────┬────────┘
                             │  state callbacks           │ telemetry/fault/
                             │                            │ antenna-change callbacks
                             └─────────────┬──────────────┘
                                           ▼
                                   ┌─────────────────┐
                                   │   AcomBridge     │  safety interlocks,
                                   │ amplifier/       │  trending, duty cycle
                                   │ acom_bridge.py   │
                                   └────────┬─────────┘
                                            │ StationState
                                            ▼
                                   ┌─────────────────┐
                                   │  FastAPI app     │  REST + WebSocket
                                   │ dashboard/       │
                                   │ server.py        │
                                   └────────┬─────────┘
                                            │ broadcast / cmd
                              ┌─────────────┴─────────────┐
                              ▼                           ▼
                      index.html (/)              monitor.html (/monitor)
                      operating console            trending strip charts
```

## Modules

### `rig/rigctld_client.py`
Async TCP client speaking the Hamlib rigctld text protocol to the FT-991A (model 1035). Polls on an interval (default 0.5s) and exposes `RigState` — frequency, mode, S-meter, PTT, DSP settings (NB, DNR level, DNF, AGC), mic gain, compression. `update_derived()` computes band and display-formatted frequency from raw Hz. Notifies subscribers via `on_state_change(cb)`.

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
- **`ThermalStateManager`**: per-antenna thermal inhibit state (reason, reflected-power baseline, watchdog), persisted to `config/thermal_state.json` (gitignored — runtime state, not source). Operators clear an inhibit explicitly via `operator_clear_thermal()`.
- **Trending**: a `deque(maxlen=6000)` ring buffer of `TrendSample` at ~10Hz (~10 min window) plus a separate 5-min duty-cycle sample buffer; `get_trend_data(since)` serves incremental updates to `/monitor`.
- **TX cycle counting** and **duty-cycle calculation** (`_calc_duty_cycle`).
- **`StationState`**: the single flat dataclass that represents everything a browser needs to render — rig sub-state plus amp telemetry (fwd/refl power, SWR, drive, temp, HV, current), fault lists (hard/soft/warnings + overall `fault_severity`), operating mode, selected antenna, drive limit, TX inhibit state, duty cycle, dummy-load timer, thermal inhibit. `to_dict()` is what actually goes over the wire.
- Antenna selection is **cycle-only, driven both ways**: `next_antenna()` sends the same forward-cycle command as the front-panel ANT button; `_on_antenna_change` updates `selected_antenna` from the amp's `0x27` feedback regardless of which side triggered the change, so the console stays in sync either way. Note the amp's built-in 4 relay antennas report 0-indexed in `0x27` (`ant_num + 1` in `_on_antenna_change`) — confirmed against real hardware, contrary to the protocol doc's generic `[1..10]` wording (which describes the ASEL 10-antenna accessory case).

### `dashboard/server.py`
FastAPI app. One `AcomBridge` instance lives in module-level `bridge`, constructed in the `lifespan` context manager (so serial/TCP connections open on startup and close cleanly on shutdown — important given the ACOM's DTR/RTS sensitivity, see README). A `ConnectionManager` tracks all open WebSockets and broadcasts every `StationState` change to all of them — there's no per-client filtering, every tab sees everything.

REST surface (`/api/state`, `/api/mode`, `/api/antenna/next`, `/api/tx`, `/api/thermal/clear/{ant}`) duplicates a subset of what's also reachable over the WebSocket command channel — REST for one-shot actions/polling, WebSocket for the live stream plus the same actions inline (`set_mode_op`, `set_frequency`, `set_rf_power`, `set_preamp`, `set_mic_gain`, `set_comp`, `set_nb`, `set_nr`/`set_nr_on`, `set_dnf`, `set_agc`, `next_antenna`, `inhibit_tx`/`allow_tx`, `clear_thermal`, `get_trend`). Every WS command gets a `cmd_response` echoed back to the sender; state changes are broadcast to everyone independent of who issued the command.

### `dashboard/index.html` / `dashboard/monitor.html`
No build step — plain HTML/CSS/JS served directly from disk by `server.py`. Both connect to the same `/ws` endpoint; `monitor.html` additionally polls `get_trend` to backfill its strip charts on load/reconnect.

## Data flow summary

1. `RigctldClient` and `AcomSerial` each poll/listen their hardware independently and call into `AcomBridge` via callbacks.
2. `AcomBridge` merges both sources into one `StationState`, applies safety logic (mode limits, thermal watchdog, TX inhibit), and invokes its own `on_state_change` callbacks.
3. `server.py`'s `on_station_state` handler broadcasts the new state as `{"type": "state", "data": ...}` to every connected browser.
4. Browser-originated commands (slider drags, button clicks) go out over the same WebSocket as `{"cmd": "..."}` messages, handled by `handle_ws_command`, which calls back into `AcomBridge`/`RigctldClient` and replies with a `cmd_response` — the resulting state change then arrives separately via the next broadcast.

## Safety interlocks (where they live in code)

| Interlock | Enforced in |
|---|---|
| AMP_OFF default on boot, antenna defaults to A4R (dummy load) | `AcomBridge.__init__` / `StationState` defaults |
| AMP_ON requires explicit confirmation | `AcomBridge.set_operating_mode(confirmed=...)`, surfaced as a confirm dialog in `index.html` |
| Drive power capped per mode (100W / 40W) | `StationState.drive_limit_w`, enforced both in `set_rf_power` (server clamps `pct`) and exposed to the UI |
| Per-antenna thermal inhibit + reflected-power watchdog | `ThermalStateManager` in `acom_bridge.py`, persisted in `config/thermal_state.json` |
| Antenna is cycle-only (no jump-to-antenna), console mirrors whichever side changed it | `next_antenna()` / `_on_antenna_change` in `acom_bridge.py` |
| Serial port opened exactly once, never re-asserted on reconnect | `acom_serial.py` connection handling — see README's "Critical Hardware Rules" for the DTR/RTS hazard this avoids |

## Known limitations

- **No direct antenna selection — cycle only.** The 1200S firmware has no "select antenna N" command at all (confirmed against an engineer-supplied v1.3 protocol doc, which superseded an earlier A600S-only v1.1 doc this codebase was originally built against, plus live hardware testing). The console drives antenna changes the same way the front-panel ANT button does — `cmd_next_antenna()`, forward cycling only, no "previous" — and the firmware itself skips antennas not assigned to the current band (e.g. on 40m it only toggles between two of the four). Full byte-level writeup of doc-vs-hardware discrepancies (the antenna number being ignored, the band number working despite being undocumented for this sub-command, the 0-indexed `0x27` antenna field) is in this project's Claude memory (`project_acom_1200s_protocol`), not duplicated here.
- **No automated test suite.** `tests/` contains only an empty `__init__.py`.
- **Single hardcoded serial port.** `ACOM_PORT` in `server.py` must be updated by hand when the FTDI adapter's device path changes (it has, at least twice, per git history); `find_acom_port()` exists but is unreliable with multiple FTDI devices attached.
