# w7tlg-console — Codebase Audit (Phase 1, read-only)

Scope: Python/FastAPI/WebSocket console controlling a Yaesu FT-991A (CAT via
rigctld), an RSPduo dual-receiver SDR panadapter, WSJT-X digital-mode
integration, an ACOM 1200S/06AT amplifier, and an LLM-based band advisor.
Single-operator deployment on one fixed hardware combination — "doesn't
support other hardware" is intentional scope, not a finding.

No code was modified to produce this document.

---

## 1. Module & dependency map

**Layering is clean and consistently one-directional:**

```
dashboard/server.py  (FastAPI + WebSocket, orchestration only)
  ├─ amplifier/acom_bridge.py   (business logic: ties rig + amp together)
  │    ├─ rig/rigctld_client.py     (hardware I/O: rigctld TCP socket)
  │    └─ amplifier/acom_serial.py  (hardware I/O: ACOM serial port)
  │         └─ amplifier/acom_protocol.py (pure framing/parsing, no I/O)
  ├─ sdr/sdr_client.py           (hardware I/O: SDRplay ctypes API)
  │    ├─ sdr/audio_demod.py     (pure DSP, no I/O)
  │    ├─ sdr/combiner.py        (pure DSP, no I/O, independent copy of demod's filters)
  │    ├─ sdr/virtual_audio_output.py (hardware I/O: CoreAudio/BlackHole)
  │    └─ sdr/sdrplay_capi.py    (ctypes struct/function definitions only)
  ├─ session/session_manager.py  (business logic: orchestrates rig+sdr+external apps)
  ├─ wsjtx/udp_listener.py + protocol.py  (network I/O + pure parsing, read-only)
  ├─ advisor/claude_advisor.py, advisor/monitor.py  (LLM calls; claude_advisor.py
  │    is the only module with a write path back into rig/rigctld_client.py)
  └─ amplifier/antenna_ab_test.py, tx_power_calibration.py  (business logic,
       read sdr+bridge, write only through acom_bridge's own guarded methods)
```

The hardware I/O boundary is well respected: nothing outside
`rig/rigctld_client.py`, `amplifier/acom_serial.py`, and `sdr/sdrplay_capi.py`
touches a socket/serial port/ctypes call directly. `amplifier/acom_protocol.py`
is pure and independently testable (it even ships its own `__main__` self-test).

**LOW — Finding 1.** `sdr/combiner.py` duplicates three functions byte-for-byte
from `sdr/audio_demod.py` (`_choose_decim_stages`, `_design_decim_filter`,
`_design_ssb_filter` — `sdr/combiner.py:96-104,107-115` vs
`sdr/audio_demod.py:110-122,414-429`). This is called out as deliberate in
`sdr/combiner.py`'s own module docstring (independent auditability for an
experimental feature) — noted for completeness, not a defect, but a future
change to the shared math (e.g. a filter-design bug fix) must be applied in
both places by hand.

---

## 2. Concurrency audit

**No blocking I/O inside `async def` without an executor hop.** Every
blocking call is explicitly offloaded:
- `amplifier/acom_serial.py:158` (`_serial.write`) and `:244` (`_read_available`)
  — pyserial calls via `loop.run_in_executor`.
- `sdr/sdr_client.py:387,450,474,483,494,502,509,516,523,530` — every ctypes
  SDRplay API call (`_open_and_init`, `_apply_*`, teardown) via
  `run_in_executor`.
- `session/session_manager.py:325,342,352` — `asyncio.create_subprocess_exec`
  (never a blocking `subprocess.run`).
- WSJT-X UDP parsing (`wsjtx/protocol.py`) is pure/synchronous but O(µs) —
  not a blocking-I/O concern.

**Shared mutable state across threads** is real but deliberately handled via
GIL-atomic single-attribute writes/reads rather than locks, and this is
documented inline everywhere it happens (e.g. `sdr/sdr_client.py:770`
"`_last_sample_at` ... GIL-atomic, no lock needed"; `sdr/audio_demod.py`'s
`target_freq_hz`/`mode`/`bandwidth_hz` written from the asyncio event-loop
thread via WS command handlers, read from the vendor callback / consumer
threads). This is a legitimate design choice for soft-real-time audio (a
torn read just means one stale ~8ms audio block), but it is worth stating
explicitly: **MEDIUM — Finding 2.** Multi-field updates like `set_audio_target`
(`dashboard/server.py:1258-1267`, writing `audio.target_freq_hz`, `audio.mode`,
`audio.bandwidth_hz` as three separate statements) are not atomic as a group.
A demod cycle running concurrently on the audio thread could read the new
frequency with the old bandwidth for one block. Cosmetic for audio; would
matter more if this pattern were ever reused for something safety-relevant.
No lock is proposed here — the fix, if wanted, is to pass a single immutable
tuple/dataclass instead of three attributes.

**Rig/amp serial connections are correctly single-writer, single-lock:**
`rig/rigctld_client.py` uses one `asyncio.Lock` (`self._lock`) around every
socket read/write (`_send_get`/`_send_set`/`_send_raw_ex_set`), and
`amplifier/acom_serial.py` uses its own `_send_lock`. No path sends
concurrently on either shared connection.

**No coroutine holds a lock across an `await` that could stall unrelated
WebSocket clients.** The 100ms rig poll loop and the independent 5ms fast-PTT
monitor (`dashboard/server.py:456-547`) use their own separate rigctld TCP
connections specifically so a slow GET on one doesn't block PTT detection on
the other — documented and correct.

**Verified still in place:** `main.py:30-35`'s `torch.set_num_threads(2)` /
`set_num_interop_threads(1)` guard against the two concurrent
`AudioDemodulator` DeepFilterNet instances oversubscribing CPU threads.

---

## 3. Hardware failure-mode audit

| Device | Disconnect | Timeout | Malformed reply | Slow reply |
|---|---|---|---|---|
| Rig (rigctld) | Reconnect loop, `state.connected=False` broadcast (`rig/rigctld_client.py:545-570`) | Per-GET/SET timeout treated as "miss, not disconnect" (`:894-904`) with stale-reply draining | **Explicit desync guard**: implausible frequency reading raises and forces reconnect (`:651-661`) — this is the fix for the "stream desync forever" class of bug, applied here | Fast-PTT monitor on its own connection so a slow main-poll GET never delays PTT detection |
| Amp (ACOM serial) | Reconnect loop, `_on_amp_connection(False)` → `inhibit_tx()` (`amplifier/acom_bridge.py:780-786`) | `_telemetry_watchdog` re-enables telemetry after 30s silence (`amplifier/acom_serial.py:332-344`) | **Checksummed frames + resync-on-bad-length** (`amplifier/acom_protocol.py:561-612`) — same desync-guard class as the rig, correctly applied | N/A (frame-based, not line-based) |
| SDR (RSPduo) | `_on_event` (Device Removed/Failure) → `_schedule_recovery` → teardown + backoff re-init (`sdr/sdr_client.py:840-895`) | **Stall watchdog** (`:896-932`) — the one device class that goes silent with no vendor event at all, explicitly handled with a 3s data-liveness timeout | Frame integrity is the vendor driver's problem, not parsed here | Retry-with-backoff init (`:367-401`) for the "handle not settled yet" case |

**HIGH — Finding 3 (matches a previously-known open item).** The console
never cross-checks `amp_hv_v` (HV rail voltage, populated at
`amplifier/acom_bridge.py:659` from `AmpTelemetry.hv1_v`,
`amplifier/acom_protocol.py:399`) against the console's own `operating_mode`
(AMP_ON/AMP_OFF). Mode sync is driven entirely by the telemetry `mode` byte's
class bits (`acom_bridge.py:629-647`, requiring 3 consecutive OPR-class
frames to flip to AMP_ON). If the amp's firmware were to report an OPR-class
mode byte while the HV rail has actually collapsed — short of a hard
`PAM1 HV TOO LOW`/`PAM1 HV TOO HIGH` fault bit (`acom_protocol.py:494-495`)
actually firing on the 0x21 fault message — the console would keep showing
AMP_ON/OPERATE with the operator getting effectively zero amplification and
no indication why, which is exactly the incorrect-TX-state class this audit
was asked to flag regardless of severity ranking on `amp_mode`. There is no
code path anywhere in `amplifier/acom_bridge.py` that reads `amp_hv_v` for
anything other than display. A cheap mitigation: while `_mode == AMP_ON` and
`t.flag_keyin` (TX in progress), warn/inhibit if `amp_hv_v` stays near zero.

**MEDIUM — Finding 4.** `ACOM_PORT` is a hardcoded device path
(`dashboard/server.py:54`, with a previous value left commented out directly
above it at line 53 rather than removed) rather than validated/discovered at
startup. The fallback logic is `acom_port = ACOM_PORT or find_acom_port()`
(`dashboard/server.py:606`) — since `ACOM_PORT` is always a non-empty string,
`find_acom_port()` is **never actually called** as a fallback if the hardcoded
path is stale (e.g. after a macOS USB-serial re-enumeration, which commonly
changes `/dev/cu.usbserial-*` paths). `amplifier/acom_serial.py`'s reconnect
loop (`_run`/`_connect`) will then retry forever against a path that will
never come back, with the amp silently staying in "amp features disabled"
mode. This is a real, previously-hit-in-practice class of failure for exactly
this station's USB-serial hardware (see `find_acom_port`'s own docstring
about distinguishing FTDI-vs-SiLabs ports).

**LOW — Finding 5.** `parse_full_telemetry` in `amplifier/acom_protocol.py`
sets `t.error_code`/`t.error_param` twice from two different, non-agreeing
byte ranges — once at lines 411-413 (bytes 60-62) and again immediately after
at lines 415-417 (bytes 63-65), with the second silently overwriting the
first. The second assignment's use of `data[63]` for `error_code` also
collides with `data[63]` already being consumed for `fan_speed`/`active_lpf`
two lines above it (line 408: `t.fan_speed = (data[63] >> 4) & 0x0F`). This
field is diagnostic-only (not read by any fault-detection logic — real fault
detection uses the separate `parse_fault_codes`/message 0x21 path), so this
doesn't affect safety behavior, but the displayed/logged "error code" value
is not trustworthy as written.

---

## 4. State management

There is one authoritative source of truth per device, each broadcast
outward — clients do not reconstruct or diverge independently:

- **Rig**: `RigctldClient.state` (a single `RigState` dataclass) is mutated
  only inside `rig/rigctld_client.py`'s own poll loop, then pushed via
  `_fire_callbacks()` → `AcomBridge._on_rig_state` → `StationState.rig` →
  `ConnectionManager.broadcast()` → every connected `/ws` client. No
  client-side reconciliation logic exists in `console.html`/`panadapter.html`
  beyond rendering what it's sent.
- **Amp**: same pattern — `AcomBridge.station` (`StationState`) is the one
  object mutated by `_on_telemetry`/`_on_fault`/`_on_antenna_change`, then
  `_publish()` fans it out.
- **SDR**: `SdrClient` instance attributes (`rf_freq_hz`, `rf_gain_pct`, etc.)
  are the source; `build_state_payload()` (`dashboard/server.py:269-361`)
  reads them fresh on every broadcast/poll rather than caching a copy.

Example trace (frequency change end-to-end): operator turns VFO knob on the
FT-991A → `rig/rigctld_client.py:_poll_state` reads new `f` value (0.1s poll)
→ sanity-checked against `FREQ_SANITY_MIN_HZ/MAX_HZ` → `RigState.freq_hz`
updated → `_fire_callbacks()` → `AcomBridge._on_rig_state` (also triggers
`_handle_freq_change` → amp band-select if band changed) → `_publish()` →
`on_station_state` (`dashboard/server.py:364`) → `manager.broadcast()` → every
open `/ws` socket receives the new `freq_hz` in the same `state` message.
Single path, no per-client state.

One copy-on-read is done correctly and deliberately:
`build_state_payload:271` copies `state.to_dict()["rig"]` before mutating it
with SDR-derived fields, specifically to avoid corrupting the live
`StationState.rig` dict that other code still holds a reference to.

No findings in this section beyond what's captured in the concurrency section
(§2, Finding 2) about sub-field write atomicity.

---

## 5. API & WebSocket contract review

**Two parallel command surfaces exist** (REST `/api/*` and WS `cmd` messages
in `handle_ws_command`) with some functional overlap (e.g. `/api/mode` and
`set_mode_op`; `/api/antenna/next` and `next_antenna`) but no schema
divergence found between them — both ultimately call the same
`AcomBridge`/`RigctldClient`/`SdrClient` methods.

**MEDIUM — Finding 6.** Input validation is inconsistent across WS commands
in `dashboard/server.py`'s `handle_ws_command` (lines 968-1401):
- Clamped/validated: `set_rf_power` (capped to `drive_limit_w`, line 996),
  `set_mic_gain`/`set_comp` (0.0-1.0, lines 1015/1021),
  `set_audio_nr` level (1-15, line 1047), `set_eq` bands (±12dB, lines
  1130-1134), `set_dt_gain` (0-100, line 1143), `set_rx_volume` (0-10,
  line 1182).
- **Not validated at all**: `set_frequency` (`int(msg["freq_hz"])` straight
  through to `rig.set_frequency`, line 984; `RigctldClient.set_frequency`
  itself, `rig/rigctld_client.py:307-308`, has no range check either — only
  the *reader* side has `FREQ_SANITY_MIN_HZ/MAX_HZ`, the *writer* side has
  none), `set_mode` (arbitrary string forwarded to rigctld's `M` command,
  line 988-990), `set_panadapter_freq` (arbitrary float, line 1242-1256).
  A malformed or wildly out-of-range value from any WS client reaches the
  radio directly; rigctld itself may reject it, but the console does not
  guard this boundary. This is the same gap exploited by the LLM
  auto-QSY path in §7 below — it is a general input-validation gap, not
  something specific to the advisor.

**LOW — Finding 7.** Guard inconsistency: REST endpoints uniformly return
`HTTPException(503, ...)` when `bridge is None` (e.g.
`dashboard/server.py:745-746`), while `handle_ws_command` silently `return`s
with no message sent to the client at all (`dashboard/server.py:969-970`) in
the same situation — a WS client sees nothing rather than an error it could
surface to the operator.

**LOW — Finding 8.** The blanket `except Exception as e: ... await
ws.send_text(json.dumps({"type": "error", "message": str(e)}))` at the end
of `handle_ws_command` (`dashboard/server.py:1403-1405`) returns raw Python
exception text to any connected WebSocket client. Low risk given the
single-operator/LAN-only deployment scope (§ Deployment scope), but worth
noting if this console is ever exposed more broadly.

---

## 6. Test coverage map

**Zero automated tests exist in this repository.** Confirmed:
- `tests/__init__.py` is a 0-byte file; no `test_*.py` or `*_test.py` files
  exist anywhere (`amplifier/antenna_ab_test.py` is a live operational
  feature, not a pytest file, despite the name).
- No `pytest.ini`, `conftest.py`, or CI configuration (`.github/workflows`)
  found anywhere in the repo.

The only "tests" that exist are manual/live self-checks intended to be run
by hand against real hardware: `amplifier/acom_protocol.py`'s `__main__`
block (frame builder/parser assertions — this one is at least a real,
hardware-independent unit test, just not wired into an automated test
runner), and `if __name__ == '__main__'` smoke-test entry points in
`rig/rigctld_client.py`, `amplifier/acom_serial.py` that require live
hardware and a human watching the output.

**Hardware-control paths with zero coverage of any kind** (not even a manual
smoke test): every WS command handler in `dashboard/server.py`
(~40 commands), all of `AcomBridge`'s safety logic (mode-drive-limit
enforcement, dummy-load power/duration curve, SWR warning hysteresis,
antenna-switch TX/fault guards in `goto_antenna`), `SessionManager`'s full
choreography (app quit/launch/liveness/port-check sequence), and all of
`sdr/audio_demod.py`'s DSP chain (decimation, SSB filtering, AGC, EQ, NR).
Given `amplifier/acom_protocol.py`'s frame parser already has a working
self-test pattern, the dummy-load curve math
(`_dummy_load_curve_watts`/`_dummy_load_curve_duration_s`,
`amplifier/acom_bridge.py:179-207`) and the frequency-sanity/band-mapping
logic (`rig/rigctld_client.py`'s `freq_to_band`, `FREQ_SANITY_MIN/MAX`) are
the highest-value, purely-computational candidates for a first real pytest
suite — no hardware required to test either.

---

## 7. LLM band-advisor coupling

Two independent Claude integrations exist:

1. **`advisor/monitor.py`** (`PropagationMonitor.explain_alert`) — text-only.
   Calls `self._client.messages.create(...)` with no `tools` argument at all
   and returns `message.content[0].text` as a plain string consumed only for
   display (`dashboard/server.py:244-257`). **No actuation path — confirmed
   clean.**

2. **`advisor/claude_advisor.py`** (`ClaudeAdvisor.stream_advice_with_tools`)
   — **has a real, direct actuation path.**

**CRITICAL — Finding 9.** When the operator opts in per-request
(`auto_qsy=True`, gated only at the API layer by defaulting False —
`dashboard/server.py:737`, `AdvisorRequest.auto_qsy`), the `qsy_to_band` tool
is offered to Claude (`advisor/claude_advisor.py:181-183`), and if Claude
calls it, the handler executes it **immediately and without any validation**:

```python
# advisor/claude_advisor.py:217-223
elif kind == "qsy":
    freq = data.get("frequency_hz")
    mode = data.get("mode", "PKTUSB")
    if freq:
        await self._rig.set_frequency(int(freq))
        await self._rig.set_mode(mode)
    yield ("qsy", data)
```

- `frequency_hz` is truthiness-checked (`if freq:`) but never range- or
  band-checked. `rig.set_frequency` (`rig/rigctld_client.py:307-308`) itself
  performs no validation either (§5, Finding 6) — the value goes straight
  to rigctld's `F` command. `rig/rigctld_client.py` already has a
  `BAND_EDGES` table (`:49-63`) used for *display* (`freq_to_band`), but it
  is never consulted here to reject an out-of-band frequency.
- `mode` is passed straight through to `rig.set_mode` with no check against
  the tool schema's own declared `enum` (`advisor/claude_advisor.py:47`) —
  the schema only constrains what a well-behaved model *should* send, not
  what the code actually accepts if it sends something else.
- This means a single hallucinated tool call (or an adversarial prompt
  injection reaching the advisor's question field, `req.question` at
  `dashboard/server.py:735`, which is included verbatim in the context sent
  to Claude at `format_context`/line 140) can move the radio's VFO to an
  arbitrary frequency **and mode**, off any amateur band, with no
  band-plan check anywhere in the call chain. Because this only sets
  frequency/mode (not PTT — `rig.set_ptt` is confirmed unused anywhere in
  this codebase, `amplifier/tx_power_calibration.py:9-11`), it does not
  itself cause a transmission. But per this audit's own instruction, an
  indirect actuation path is flagged as high severity regardless of
  likelihood: **the realistic failure mode is the operator, trusting
  auto-QSY, transmitting on the new frequency without re-checking it lands
  in an authorized allocation** — a real regulatory/safety consequence for
  a licensed service. Mitigation would be to validate `frequency_hz`
  against `rig.rigctld_client.freq_to_band()` returning non-`UNKNOWN`
  (or an explicit ham-band table) before calling `set_frequency`, and to
  validate `mode` against the tool's own declared enum, before this method
  is allowed to touch the rig at all.

No other indirect path was found: no UI element pre-fills a control from
advisor output for one-click apply, and the advisor has no visibility into
or write path to the amp (`AcomBridge`) or SDR (`SdrClient`) at all — only
`RigctldClient.set_frequency`/`set_mode` are reachable from
`ClaudeAdvisor`, confirmed by `advisor/claude_advisor.py:29`'s single import.

---

## 8. General code-quality notes

- **LOW — Finding 10.** `amplifier/acom_protocol.py:70,74`: `AmpCmd.CLEAR_SOFT_FAULTS`
  and `AmpCmd.CLEAR_FAULTS` are both defined as `0x08` on the same `IntEnum`
  — legal (aliasing) but confusing; a reader has no way to tell from the
  name alone that these are the same wire value, and only
  `cmd_clear_soft_faults()` (`:236-239`) is actually used anywhere.
- **LOW — Finding 11.** Model version strings have drifted between the two
  Claude integrations: `advisor/claude_advisor.py:31` uses
  `MODEL = "claude-sonnet-5"` while `advisor/monitor.py:20` uses
  `MODEL = "claude-sonnet-4-5"`. Likely just two separate ports from
  ft991a-panel that were never reconciled — worth confirming which is
  actually intended going forward.
- **Config/secrets handling is clean**: `main.py:22`'s `load_dotenv()` is the
  only environment-loading path; no hardcoded API keys, passwords, or
  secrets found anywhere in the repository (`ANTHROPIC_API_KEY` is read by
  the `anthropic` SDK from the environment, never touched directly by this
  codebase's own code).
- **No dead/orphaned modules found** — every file under `amplifier/`, `sdr/`,
  `rig/`, `session/`, `wsjtx/`, `advisor/` is imported and reachable from
  `dashboard/server.py`'s lifespan startup, with the sole exception of
  `tools/*.py`, which are clearly-labeled standalone offline analysis
  scripts (QSO CSV export, propagation correlation) not part of the running
  console.
- Error handling style is consistent throughout (`logger.error`/`.warning`
  plus either a safe default return or a state broadcast) — no inconsistent
  swallow-vs-crash patterns found outside the two items already noted in §7
  and §5.

---

## Summary — top 5 findings by severity

1. **CRITICAL (Finding 9)** — The LLM band-advisor's `qsy_to_band` tool
   (`advisor/claude_advisor.py:217-223`) sets the radio's live frequency and
   mode with no band-plan or value validation whatsoever, whenever the
   operator has auto-QSY enabled for that request; combined with the general
   lack of input validation in `RigctldClient.set_frequency`/`set_mode`
   (Finding 6), this is a real, if narrow, path from an LLM hallucination or
   prompt-injected question straight to an out-of-band VFO change with no
   safety net.
2. **HIGH (Finding 3)** — The amp's HV rail telemetry (`amp_hv_v`) is
   collected but never cross-checked against the console's own
   AMP_ON/AMP_OFF state, so a firmware-side HV collapse that doesn't trip an
   explicit fault bit would leave the console confidently showing AMP_ON
   with the amplifier actually contributing nothing — the incorrect-TX-state
   class of bug this audit was asked to weight heavily, and a previously
   known but still-unfixed issue.
3. **MEDIUM (Finding 4)** — The ACOM serial port is a hardcoded device path
   with `find_acom_port()` as dead fallback code (it can never actually run
   given the current `or` logic), so a routine macOS USB-serial
   re-enumeration silently and permanently disables amp control until a
   human edits the source.
4. **MEDIUM (Finding 6)** — Frequency and mode setters accept unvalidated
   values from any WebSocket client (not just the advisor), which is both a
   standalone gap and the root enabler of Finding 9.
5. **HIGH (Finding 6, test coverage)** — Zero automated test coverage across
   every hardware-control and safety-interlock path in the codebase; the
   purely-computational, hardware-independent logic most worth covering
   first (dummy-load curve math, frequency/band-sanity checks) is identified
   in §6 and would require no mocking of live hardware to start testing today.
