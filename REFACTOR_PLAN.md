# w7tlg-console — Refactor & Feature Plan (Phase 2, read-only)

Source material: `AUDIT.md` (2026-09-18) and the owner's known-issues/backlog
list reproduced in `w7tlg-console_audit_refactor_prompts.md`. No code was
modified to produce this document.

This plan sequences work into independent packages. Tier 0-1 packages are
safety/correctness fixes against **existing** behavior — they touch live
hardware-control paths and must land before any UI/feature work, per the
Phase 2 brief. Tier 2+ packages are the owner's backlog (UI reorg, new
features) and are sequenced so nothing is bolted onto the current
direct-I/O/no-test pattern once a better one exists.

Every package assumes the reusable **Phase 3 prompt template** already in
`w7tlg-console_audit_refactor_prompts.md`: read `AUDIT.md` and this file,
implement only the named package's scope, note anything else noticed under
"Observations" instead of fixing it, and flag what needs a real-hardware
smoke test before merge.

---

## Summary table

| ID | Name | Tier | Depends on | Own branch? |
|---|---|---|---|---|
| T0 | Fake-hardware test scaffolding | 0 — foundation | — | Yes (low hardware risk) |
| T1 | Frequency/mode validation — hardware-range (all paths) + band-plan guard (advisor only) | 1 — safety | T0 | Yes (CRITICAL path) |
| T2a | Amp HV/mode-divergence cross-check + telemetry error-code fix | 1 — safety | T0 | Yes |
| T2b | ACOM serial port rediscovery fix | 1 — safety | T0 | Yes |
| T3 | Atomic audio-target updates | 1 — safety | T0 | Optional (small) |
| T4 | WS/REST error-contract consistency | 1 — safety | T0 | Optional (small) |
| T5 | Cosmetic cleanup (model-version drift, enum alias) | 1 — safety | — | No — ride-along |
| U1 | RX1/RX2 startup alignment fix | 2 — UI bugs | T3 | Yes |
| U2 | Column reorg — operational / RX1 / RX2 / TX boxes | 2 — UI bugs | U1 | **SHIPPED 2026-09-19** (U2c deferred to U3) |
| U3 | Mode-parity, gain-slider prominence, analog S-meter | 2 — UI bugs | U2 | Yes |
| U4 | Knob-style frequency control (no text box) | 2 — UI bugs | U2 | Optional |
| U5 | Prominent main-display TX power readout | 2 — UI bugs | U2 | Optional |
| F1 | Voice-envelope TX tuning display | 3 — features | T0 | Yes |
| F2 | Richer WSJT-X/JS8Call parameter capture | 3 — features | T0 | Yes |
| F3 | Console-triggered log entry | 3 — features | F2 | Yes |
| F4 | Historical operating-comparison queries | 3 — features | F2, T1 | Yes |

---

## Tier 0 — Foundation

### T0 — Fake-hardware test scaffolding

**Goal.** Stand up `pytest`, a `tests/` package, and fake implementations of
`RigctldClient`, the ACOM serial/telemetry surface, and `SdrClient` that speak
the same interface the real classes expose, so every later package can add
tests without touching live hardware.

**Addresses.** AUDIT.md §6 (zero automated test coverage anywhere in the
repo, confirmed — no `pytest.ini`, no `conftest.py`, no fakes/mocks found).
This is also a direct prerequisite the Phase 3 template assumes ("write or
extend tests against the mocks") but that don't yet exist.

**Depends on.** Nothing.

**Files/modules touched.** New: `tests/conftest.py`, `tests/fakes/rig.py`,
`tests/fakes/amp.py`, `tests/fakes/sdr.py`, `requirements.txt` (add `pytest`).
Possibly thin `typing.Protocol` interface extraction in
`rig/rigctld_client.py`, `amplifier/acom_serial.py`, `sdr/sdr_client.py` if
their current public surface isn't already fake-able without one — read each
file's public API first; do not assume a Protocol is needed until confirmed.

**Non-goals.** No behavior changes to any hardware-I/O module. No new tests
for existing logic yet (that belongs to whichever later package owns that
logic) — this package's own tests are limited to proving the fakes work
(e.g. a fake rig reports a frequency, a fake amp reports telemetry, a fake
SDR reports samples) and that they satisfy the same call signatures the real
classes are actually invoked with elsewhere in the codebase.

**Validation.** Entirely mock-based by construction — that's the point of
the package. No real-hardware smoke test needed; `pytest` passing is the
bar. One check worth doing by hand: confirm the fakes' method signatures
actually match `dashboard/server.py`'s real call sites (grep, don't guess).

---

### T1 — Frequency/mode input validation (two-tier: hardware-range + advisor band-plan guard)

**Goal.** Close the CRITICAL path where the LLM advisor's `qsy_to_band` tool
can push an unvalidated frequency or mode straight to the rig, **without**
blocking the operator's own established need to manually tune outside
amateur-band segments — WWV/WWVH, CHU, and other reference/beacon
frequencies used in propagation and antenna-diversity work. A single
band-plan gate on every path would break that, so validation is two-tiered:

- **Tier A — hardware-range sanity check, applies to every path** (advisor
  and manual UI alike): reject a frequency outside the rig's actual
  tunable hardware range, and reject a mode string outside the rig's valid
  mode set. This is a pure capability check, not a band-plan restriction —
  it exists to reject garbage/out-of-range input, not to restrict where
  the operator can listen or transmit.
- **Tier B — amateur band-plan guard, advisor `qsy_to_band` path only**:
  additionally require the frequency/mode pair to resolve to a valid
  amateur band segment via `freq_to_band` (non-`UNKNOWN`). This is the
  actual fix for Finding 9 — the advisor's whole purpose is amateur-band
  operation, and it has no legitimate reason to command WWV/CHU/beacon
  frequencies, so it stays restricted. Manual UI-driven `set_frequency`/
  `set_mode`/`set_panadapter_freq` do **not** get Tier B — they only need
  to pass Tier A.

**Addresses.** AUDIT.md Finding 9 (CRITICAL — `advisor/claude_advisor.py:217-223`,
unvalidated `qsy_to_band` actuation → closed by Tier A + Tier B) and
Finding 6 (MEDIUM — `dashboard/server.py`'s `set_frequency`/`set_mode`/
`set_panadapter_freq` handlers and `rig/rigctld_client.py:307-308`'s
`set_frequency` writer path have no check at all, unlike the reader side's
existing `FREQ_SANITY_MIN_HZ/MAX_HZ` → closed by Tier A only, per the
operator's explicit out-of-band manual-tuning requirement).

**Depends on.** T0 (tests for both tiers use the fake rig to assert a bad
command is rejected before it reaches `RigctldClient`, and that a
legitimate out-of-band manual command is accepted).

**Files/modules touched.** `rig/rigctld_client.py` (add the Tier A
hardware-range check as its own constant/function, kept separate from the
existing `BAND_EDGES`/`freq_to_band` table — do not conflate a capability
bound with a band-plan table; confirm the actual tunable range from the
FT-991A documentation rather than assuming a value), `dashboard/server.py`
(`set_frequency`, `set_mode`, `set_panadapter_freq` handlers — Tier A
only), `advisor/claude_advisor.py` (the `qsy` tool-call handler — Tier A,
then Tier B via `freq_to_band`, before calling
`rig.set_frequency`/`set_mode`).

**Non-goals.** Do not apply Tier B (band-plan restriction) to any
UI-driven path — that's the whole point of the split. Do not build or
maintain an allowlist of specific known non-ham frequencies (WWV, CHU,
etc.) for the manual path; the correct bound there is "within the rig's
hardware range," not a curated list that would need upkeep as new
reference/beacon work comes up. Do not change the advisor's tool schema or
prompt beyond adding the two checks. Do not touch the amp or SDR write
paths — this package is rig-frequency/mode only. Do not add validation
UI/UX beyond a clear error to the caller — cosmetic, can ride on T4 or a
later pass.

**Validation.** Fully unit-testable against T0's fake rig — both tiers are
pure computation, no hardware needed. New test cases beyond the original
scope: (1) a manual UI command to a legitimate out-of-band frequency (e.g.
WWV at 10 MHz) is accepted; (2) the same frequency sent via the advisor's
`qsy_to_band` tool is rejected. Manual smoke test required only as a
regression check: confirm a legitimate in-band frequency/mode change from
the real console UI still reaches the real rig unchanged after the guard
is added.

---

### T2a — Amp HV/mode-divergence cross-check + telemetry error-code fix

**Goal.** Detect the case where the ACOM's telemetry `mode` byte still
reports an OPR-class (AMP_ON) state while `amp_hv_v` has actually collapsed
near zero during a TX cycle — today nothing cross-checks these, so the
console can confidently display AMP_ON while the amplifier contributes
nothing. Bundled in the same pass: fix the double-assignment bug in
`parse_full_telemetry` that silently corrupts the diagnostic `error_code`
field.

**Addresses.** AUDIT.md Finding 3 (HIGH — no cross-check between
`amp_hv_v`, populated at `amplifier/acom_bridge.py:659`, and
`operating_mode`; mitigation suggested there: while `_mode == AMP_ON` and
`flag_keyin` is true, warn/inhibit if `amp_hv_v` stays near zero) and
Finding 5 (LOW — `amplifier/acom_protocol.py:411-417` sets `t.error_code`
twice from disagreeing byte ranges, second one also colliding with
`fan_speed`'s use of `data[63]`).

**Depends on.** T0 (a fake amp/serial stream that can emit a synthetic
"OPR-class mode byte + near-zero HV during TX" telemetry sequence is the
only practical way to test this without ever forcing a real HV fault).

**Files/modules touched.** `amplifier/acom_bridge.py` (mode-sync logic
around `_mode`/`AMP_ON` handling, `:629-647`), `amplifier/acom_protocol.py`
(`parse_full_telemetry`, `:399-417`).

**Non-goals.** Do not change the existing fault-bit detection path
(`parse_fault_codes`/message 0x21) — this is a new, additional check
layered on top, not a replacement. Do not add a new UI element for this in
this package (surfacing it is a display concern, likely folds into the
existing Fault Status box in a later UI pass — note it, don't build it
here). Do not touch `antenna_ab_test.py` or `tx_power_calibration.py` even
though they're in the same module family.

**Validation.** The divergence-detection logic itself is fully testable
against T0's fake amp (feed it the synthetic bad sequence, assert the
console flags it). Genuinely cannot be validated against a real HV fault on
live hardware (that's an unsafe test to construct on purpose) — the
real-hardware smoke test here is narrower and different in kind: run several
normal operate/standby/TX cycles on real hardware and confirm **no false
positive** fires during ordinary operation.

---

### T2b — ACOM serial port rediscovery fix

**Goal.** Fix the dead-fallback bug where `find_acom_port()` can never
actually run because `ACOM_PORT or find_acom_port()` always short-circuits
on the hardcoded, possibly-stale device path — so a routine macOS
USB-serial re-enumeration silently and permanently disables amp control
until a human edits source.

**Addresses.** AUDIT.md Finding 4 (MEDIUM — `dashboard/server.py:54,606`).

**Depends on.** T0 (fake serial-port enumeration to test the fallback logic
without needing to physically unplug/replug the ACOM's USB-serial adapter).

**Files/modules touched.** `dashboard/server.py` (port-selection logic
around line 606), possibly `amplifier/acom_serial.py`'s reconnect loop if
it needs to be told to re-run discovery rather than retry the same stale
path forever.

**Non-goals.** Do not change `find_acom_port()`'s own FTDI-vs-SiLabs
discrimination logic — audit found it correct, just unreachable. Do not
change reconnect/backoff timing. Do not touch the rig or SDR reconnect
paths even though they're structurally similar — separate devices, separate
packages if they ever need the same treatment.

**Validation.** The `or`-fallback bug itself is trivially unit-testable
(assert `find_acom_port()` is actually called when the configured path is
stale/absent). The full loop — physically re-enumerating a real
USB-serial adapter and confirming the console recovers without a restart —
genuinely requires a manual smoke test with real hardware; this is exactly
the previously-hit-in-practice failure mode the finding describes.

---

### T3 — Atomic audio-target updates

**Goal.** Replace the three separate attribute writes in `set_audio_target`
(`freq_hz`, `mode`, `bandwidth_hz`) with a single immutable tuple/dataclass
assignment, so a demod cycle on the audio thread can never observe a
half-updated target.

**Addresses.** AUDIT.md Finding 2 (MEDIUM — `dashboard/server.py:1258-1267`).

**Depends on.** T0 (optional — the change is small enough to smoke-test
directly, but a fake demod consumer makes the "torn read" scenario
reproducible on demand rather than theoretical).

**Files/modules touched.** `dashboard/server.py` (`set_audio_target`),
`sdr/audio_demod.py` (whatever reads `target_freq_hz`/`mode`/`bandwidth_hz`
today — switch it to read the new bundled value).

**Non-goals.** Do not introduce a lock — audit explicitly notes this isn't
needed; the fix is a single atomic assignment, not synchronization. Do not
touch any other GIL-atomic single-attribute pattern documented elsewhere
(e.g. `sdr/sdr_client.py:770`'s `_last_sample_at`) — those were audited as
correct as-is.

**Validation.** Mockable: a fake consumer thread reading mid-update proves
the fix. Audit calls this cosmetic for audio (a torn read is one stale
~8ms block), but since it's live audio path, do one quick real-audio smoke
test after the change — tune across a few frequencies/modes and confirm no
new audible glitch was introduced by the refactor itself.

---

### T4 — WS/REST error-contract consistency

**Goal.** Make `handle_ws_command` behave consistently with the REST
endpoints when `bridge is None` (currently silently `return`s instead of
sending an error the client could display), and stop leaking raw Python
exception text to WebSocket clients.

**Addresses.** AUDIT.md Finding 7 (LOW — `dashboard/server.py:969-970` vs.
`:745-746`) and Finding 8 (LOW — `dashboard/server.py:1403-1405`).

**Depends on.** T0.

**Files/modules touched.** `dashboard/server.py` (`handle_ws_command`'s
guard clause and its trailing `except Exception` handler only).

**Non-goals.** Do not restructure the ~40-command dispatch table itself —
tempting since you're already in this function, but out of scope; a bigger
dispatch refactor is its own package if ever wanted. Do not change what
error information reaches the browser console/network tab vs. the on-screen
toast — audit flags this as low risk specifically because deployment is
LAN-only/single-operator (see AUDIT.md's noted deployment scope); don't
over-engineer a public-facing security posture for a single-operator app.

**Validation.** Fully mockable — call `handle_ws_command` with a fake
socket, `bridge=None`, and a fake that raises, assert both now produce a
client-visible `error` message. No hardware involved at all.

---

### T5 — Cosmetic cleanup (ride-along, no dedicated branch)

**Goal.** Two trivial fixes noted in the audit that don't warrant their own
branch: reconcile the two different Claude model-version strings
(`advisor/claude_advisor.py:31` vs `advisor/monitor.py:20`), and rename or
comment `AmpCmd.CLEAR_SOFT_FAULTS`/`CLEAR_FAULTS`'s intentional `0x08`
aliasing so a reader doesn't mistake it for a bug.

**Addresses.** AUDIT.md Finding 10 (LOW) and Finding 11 (LOW).

**Depends on.** Nothing — but land it inside whichever other package
already has a reason to touch `advisor/claude_advisor.py` (T1) or
`amplifier/acom_protocol.py` (T2a), rather than opening a PR for two
one-line changes.

**Files/modules touched.** `advisor/claude_advisor.py`,
`advisor/monitor.py`, `amplifier/acom_protocol.py`.

**Non-goals.** Don't use this as an excuse to touch anything else in those
files.

**Validation.** None needed beyond confirming the app still imports/runs —
these are string/comment changes with no behavioral surface.

---

### T6 — `drive_limit_w` is a percent wearing a watts label (backlog)

**Goal.** `MODE_DRIVE_LIMITS` / `station.drive_limit_w` / `AMP_ON_DRIVE_LIMIT`
hold the rig's **RF power setting as a percent** (0-100, derived from hamlib
`RFPOWER` 0.0-1.0 — `rig/rigctld_client.py:215`), not watts. They are
compared against `rig.state.rf_power_pct` and passed to
`set_rf_power(pct)`, which is correct; only the `_w` suffix and the
`"clamped to 15W"` log wording are wrong. On the FT-991A's HF ranges
percent and watts track closely enough that this has never produced a wrong
number, which is why it survived this long.

**Deferred deliberately** (Terry, 2026-09-19, during the Item 1 drive-clamp
change): renaming touches `dashboard/server.py`, both `console.html` and
`index.html`, and `amplifier/tx_power_calibration.py`, and would have
buried a safety fix in a rename diff.

**Files/modules touched.** `amplifier/acom_bridge.py`,
`amplifier/tx_power_calibration.py`, `dashboard/server.py`,
`dashboard/console.html`, `dashboard/index.html`.

**Non-goals.** Don't change any *value* while renaming — this is a pure
rename plus log-string fix, and it should be reviewable as such.

**Validation.** Full test suite, then confirm the RF power slider's max and
the calibration step filter still read the same numbers they did before.

---

## Tier 2 — UI reorganization (backlog: "little things" + the column reorg)

Everything in this tier is frontend (`dashboard/console.html`) plus, where
noted, the small amount of `dashboard/server.py`/`session_manager.py` state
needed to drive it. None of it should land until Tier 1's validation and
amp/rig safety fixes are in, per the brief's sequencing rule — new UI
shouldn't be built to interact with a control surface that's about to change
shape underneath it (e.g. U4's frequency knob calls `set_frequency`, which
T1 changes the validation behavior of).

### U1 — RX1/RX2 startup alignment fix

**Goal.** Fix the first-launch bug where RX2 has no mode set and produces
no audio to WSJT-X until the operator manually clicks USB. Per
`FUNCTIONS.md` §8, the intended behavior is already "Link to RX1" on by
default with RX2 inheriting RX1's frequency/mode/filter width the moment
the page connects — this package is about making that actually hold on the
very first cold load, not redesigning the Link/Copy behavior itself.

**Addresses.** Backlog item: "In opening the app for the first time, RX1
and RX2 are not aligned... I am required to click on USB in the mode to get
RX2 to send audio." Related prior fixes in this area (RX1/RX2 UI bugs,
2026-09-12) suggest this is a narrower remaining race in the cold-start
path specifically, not a regression of the already-fixed issues.

**Depends on.** T3 (same `set_audio_target`/mode-init code path — land the
atomicity fix first so this package isn't debugging a race against a
moving target).

**Files/modules touched.** `dashboard/server.py` (startup/session-init
state construction — what mode RX2's state carries before the first real
WS push), `dashboard/console.html` (client boot sequence / Link-to-RX1
default-init logic), `session/session_manager.py` if session-profile
selection is involved in seeding RX2's initial mode.

**Non-goals.** Do not change the Link/Copy mechanism's design or behavior
once already running — only the cold-start seeding. Do not touch layout
(that's U2).

**Validation.** The "does the first WS state push contain a real,
non-empty mode for RX2" invariant is testable against T0's fakes. The
actual symptom (audio reaching WSJT-X without a manual click) genuinely
needs a manual smoke test: fresh page load, confirm RX2 audio flows
immediately.

---

### U2 — Column reorg: operational / RX1 / RX2 / TX boxes

> **SHIPPED 2026-09-19** (`U2a` 6b7cb34, `U2b` b93fcf4), with a smaller
> scope than this entry anticipated, and one deferral. See
> `IMPLEMENTATION_PLAN_U2.md`.
>
> **This entry's description of the "current" layout was stale when
> written.** It predates the 2026-09-11 RSPduo reorg, which had already
> delivered two of the three things asked for below: RX1 and RX2 were
> already fully self-contained (each with its own Mode/Filter/AGC/NR/EQ,
> not a shared side column), and SSB Audio and the TX meters had already
> moved right. The genuine remaining work was the **antenna move** —
> Antenna and Antenna A/B Test out of `panel-bandamp` and into
> `panel-modedsp` — which is what actually made the right column TX-only.
>
> **Also shipped, not anticipated here:** narrowing the amp-bypass dimmer,
> which had been scoped by DOM ancestry and so silently disabled SSB
> Audio, TX Meters, Fault Status and Exciter Drive on 2m/70cm — four
> controls the backend still honours there. Plus a `.ant-grid` overflow
> fix, the column-width defaults rebalanced to the operator's own dragged
> widths, and `scrollbar-gutter: stable`.
>
> **Deferred: U2c, the single shared Mode control.** RX1's mode grid sends
> CAT to the rig; RX2's switches the SDR demodulator's sideband. Different
> commands to different hardware behind one label, so merging them is a
> behaviour change this package's own non-goals exclude — and `Link` already
> copies RX1's sideband to RX2. Goes to U3 with the RX2 mode-set work.
> Reasoning in `IMPLEMENTATION_PLAN_U2.md` §7.
>
> **Five findings from live testing (A-E) are recorded in
> `IMPLEMENTATION_PLAN_U2.md` §6b** and summarized in `DESIGN.md` §11.
> Finding B is a gap U2 introduced and should be picked up first.

**Goal.** Restructure `console.html`'s layout from its current
`panel-modedsp` (left: Session, Band, VHF/UHF, Mode/Filter/DSP, Digital
Audio) / `panel-center` (RX1) / `panel-center2` (RX2) / `panel-bandamp`
(right: Amp, TX Meters, Fault Status, Operating Mode, Exciter Drive,
**Antenna, Antenna A/B Test**) grid into the shape the owner asked for:
a left "general operational" column holding Session, Band, Antenna, and
Antenna A/B Test together (today Antenna and A/B Test live in the *right*
panel, mixed in with the amp — this is the actual move, not a resize); a
middle area with RX1 and RX2 each fully self-contained (frequency, S-meter,
spectrum/waterfall, and that receiver's own controls); one shared,
non-duplicated Mode control that drives both receivers; and a right column
that becomes purely transmitter-side (Amp, TX Meters, Fault Status,
Operating Mode, Exciter Drive) with Antenna removed from it.

**Addresses.** Backlog item 8 in full (the column-reorg description) and
partially items 2 ("control groups that are separate from the other
functions that they work with") and 6 (Mode buttons "too close to AF/RF
GAIN... do not match between the RXs" — the *placement* half of that item;
the *parity/redesign* half is U3).

**Depends on.** U1 (don't move DOM around while U1 is still chasing a
startup race in the same files).

**Files/modules touched.** `dashboard/console.html` only — grid-template
areas and the CSS/DOM currently under `.panel-modedsp`, `.panel-center`,
`.panel-center2`, `.panel-bandamp`. Likely `DESIGN.md`/`FUNCTIONS.md`
updates afterward to match (documentation, not app code).

**Non-goals.** This package moves controls; it does not change what any
control does, which WS command or state field it's bound to, or add the
"RX1/RX2 in different modes" capability that `FUNCTIONS.md` §4 explicitly
says not to preclude but also not to build yet — if the shared-Mode-control
relocation surfaces a real need for a new state field to support future
per-RX mode divergence, defer that to its own package rather than adding it
here. Do not implement U3/U4/U5's content changes in the same pass — this
is layout only.

**Validation.** Almost entirely a DOM/CSS concern with zero hardware I/O of
its own — no mock/fake needed for the move itself. Validate by: every
relocated control still fires the exact same JS handler / WS command it did
before the move (diff-checkable), then one live session open to confirm
nothing broke functionally end to end. No new hardware risk, but flag as
its own branch anyway given the size of the diff, for review clarity.

---

### U3 — Mode parity, gain-slider prominence, analog S-meter

> **Inherited from U2 (2026-09-19)** — added to this package's scope, full
> detail in `IMPLEMENTATION_PLAN_U2.md` §6b:
>
> - **U2c, the shared Mode control — now SUPERSEDED, not just deferred**
>   (operator direction, 2026-09-19). The open question ("should CW/AM/FM
>   get real SDR demodulation on RX2?") is answered: **yes**. But the same
>   answer removes the rationale for a *shared* control. RX2 is intended as
>   a genuinely independent second receiver on the FTDX 101D model — for
>   comparing signals and improving voice copy — which is precisely the
>   "two RXs in different modes" case the original backlog said not to
>   preclude. Forcing one Mode control across both would contradict that.
>   The existing design (per-RX mode controls + Link/Copy to match them on
>   demand) already serves both. **The real gap is mode *parity*, not mode
>   *unification*.**
>
> - **Mode parity is bigger than "RX2 is missing buttons."** The console
>   has **no AM or FM demodulator at all, for either receiver**.
>   `sdr/audio_demod.py` implements SSB only — its single mode branch is
>   `shift_hz = center_hz if mode == "USB" else -center_hz` (`:471`), a
>   sideband sign flip; there is no envelope detector and no quadrature
>   discriminator anywhere in `sdr/`. RX1's mode buttons send CAT to the
>   *radio*, but the console's own audio derives its demod from
>   `sidebandForRigMode()` (`console.html:2262`), which returns LSB if the
>   mode string contains an 'L' and USB otherwise. So pressing AM or FM on
>   RX1 changes the radio while the console keeps demodulating SSB. CW
>   survives by accident (it is normally copied as an offset tone inside an
>   SSB passband); AM and FM do not. **Scope this as DSP work in
>   `audio_demod.py`, not as adding buttons** — and note RX1's existing
>   AM/FM buttons are currently misleading, since they look functional.
> - **Finding B — context gating (do this first; U2 introduced it).**
>   Antenna and A/B Test must show their own unavailability on 2m/70cm via
>   `.rx-inert` gated on `amp_in_path`, instead of erroring into SYSTEM
>   MSGS. **Finding C is the same shape**: gate TX BW on the rig being in
>   SSB, since menu 110 is SSB-only.
> - **Finding E — viewport-height console.** Also fixes SYSTEM MSGS
>   scrolling off-screen, which is what makes finding B's error path
>   invisible in the first place. Touches `.scope-group` sizing and the
>   canvas `getBoundingClientRect()` paths; may deserve its own package.
> - **The orphan-regrouping question** the operator raised live: Exciter
>   Drive (possibly renamed RADIO POWER), Antenna A/B and Measure Noise
>   read as orphans, and SSB Audio arguably belongs with Digital Audio.
>   Deliberately **not** done in U2. Before attempting a bottom strip, read
>   `FUNCTIONS.md` §8 item 10: that was tried twice and reverted as worse
>   than the cramped column, with the recorded guidance to revisit via a
>   `FieldGrid` redesign (DESIGN.md §6.4) rather than a wider container.
>   Note also that much of the "orphan" feeling came from those controls
>   being *dimmed* on VHF/UHF, which U2 fixed — re-evaluate before moving
>   anything.
>
> **Not for U3:** finding A (no SDR audio on 2m/70cm) and finding D
> (digital-mode waterfall blanking) are receive-path bugs, not UI work.
> They need their own packages. **Finding A is additionally
> de-prioritized** (operator, 2026-09-19): VHF/UHF console audio is
> explicitly deferred, with spectrum/waterfall visibility plus the
> FT-991A's own front-panel audio accepted as the stopgap.
>
> **Out of scope permanently:** stereo panning and mixing. RX1-left /
> RX2-right is handled by an external mixer fed from the M2's output. The
> console's existing hard-pan (`pan.value = -1` / `= 1`) is what separates
> the two receivers into L/R for it and **must not be "fixed" to centre** —
> that was done once as a diagnostic (a8668c9 reverted it).

**Goal.** Three related visual-design items once U2's boxes exist: give
RX2 the same full mode set RX1 has (today RX2 only offers USB/LSB) and
settle the "should RX1 be the primary DATA-U receiver" question the backlog
raises against the existing "which RX feeds WSJT-X" toggle already
described in `FUNCTIONS.md` §8's "Digital Audio source" row; make AF GAIN
and RF GAIN visually more prominent given how often they're touched; build
a skeuomorphic analog S-meter widget for both receivers to replace whatever
numeric/bar readout exists today.

**Addresses.** Backlog items: "AF GAIN and RF GAIN... need to be more
prominent," "MODE selecting buttons need to be evaluated... RX2 has only
USB and LSB, but it needs all the options shown in RX1... evaluate using
RX1 as the main DATA-U receiver," "S meter would be better if it was a
skeuomorphic analog S-meter for both RXs."

**Depends on.** U2 (needs the new per-RX boxes to place these controls
inside of).

**Files/modules touched.** `dashboard/console.html` — mode-grid/
mode-mini-grid markup and CSS, AF/RF gain slider styling, a new S-meter
widget (canvas or inline SVG).

**Non-goals.** Do not add new demodulator modes — `FUNCTIONS.md` §1/§8
already flags CW/AM/FM as stubbed with no real demod behind them on either
receiver; giving RX2 the same *buttons* as RX1 is not the same as building
new demod paths, and this package must not silently expand into that. Do
not change which underlying telemetry value (SDR vs. rig `strength_db`)
feeds signal strength — confirm which is currently authoritative (SDR is
the actual receiver post-RSPduo swap) before wiring the new meter, but
don't change that wiring's source, only its presentation.

**Validation.** Frontend-only for the mode-button relocation and slider
styling — visual review in browser. The S-meter needs one live check: with
a known signal (or the existing S9-cal reference), confirm the analog
needle position and calibration match what the previous numeric readout
showed, so the redesign doesn't silently change what the number means.

---

### U4 — Knob-style frequency control (no text-entry box)

**Goal.** Add a non-text-box way to adjust frequency from within the
console — scroll-wheel-on-waterfall, a drag knob, or click-and-drag on the
spectrum, following the pattern common SDR packages use — as an addition
to, not replacement for, the existing frequency entry box.

**Addresses.** Backlog item: "We need a way to adjust the frequency within
the console that is not entering the frequency in a box. Evaluate how some
of the SDR software packages do this."

**Depends on.** U2 (needs a settled RX tuning-row location to live in).

**Files/modules touched.** `dashboard/console.html` (new interaction
handler on the waterfall/spectrum canvas or a new widget), calling the
existing `set_frequency` WS command — already hardened by T1's validation,
no new backend endpoint should be needed.

**Non-goals.** Do not rebuild any existing click-to-tune-on-waterfall
behavior if one is already present — audit that first within this
package's own scope rather than assuming greenfield. Do not change
`set_frequency`'s backend validation (T1 already owns that) — if this
control needs a different tuning-rate/step behavior, implement that
client-side only.

**Validation.** Frontend-only for the interaction mechanics. One
hardware-relevant smoke test matters here specifically: confirm the new
control debounces/rate-limits its `set_frequency` calls so a fast
scroll/drag gesture doesn't flood rigctld with more SET commands than it
can keep up with — this is the one place in U4 where hardware pacing, not
just UI feel, is at stake.

---

### U5 — Prominent main-display TX power readout

**Goal.** Surface live output power on the main frequency display, larger
and more visible than the existing small readout inside the amp box.

**Addresses.** Backlog item: "The Main display where the frequency is
shown needs to have a live output power display; the one in the AMP box is
useful but too small."

**Depends on.** U2 (needs the reorganized main-display area to exist).

**Files/modules touched.** `dashboard/console.html` only — this duplicates
an existing state field (`amp_fwd_w` / rig PO, already broadcast per
AUDIT.md §4's state-management trace) into a new, more prominent location;
no new backend field should be required.

**Non-goals.** Do not change how power is measured or which value is
authoritative (amp telemetry vs. rig PO) — confirm which one the existing
AMP-box readout uses and mirror that exact source, don't introduce a second
independent computation that could drift from it.

**Validation.** Frontend-only. Live check: confirm the new readout tracks
the existing AMP-box number in real time with no lag/mismatch during a
transmission.

---

## Tier 3 — New features

### F1 — Voice-envelope TX tuning display

**Goal.** A real-time amplitude-envelope display of the TX mic/SSB audio,
to help visually tune SSB operation (compression, ALC headroom) the way an
oscilloscope or envelope monitor would on a traditional rig.

**Addresses.** Backlog item: "Can we get a voice envelope display to tune
the SSB operations?" This is genuinely new capability — no envelope,
oscilloscope, or waveform-display code exists anywhere in the current
codebase (`dashboard/*.html`, `sdr/*.py` confirmed clean of it).

**Depends on.** T0 (this introduces new streaming state; needs test
scaffolding from the start rather than retrofitted later).

**Files/modules touched.** New module for envelope extraction (likely
under `sdr/` or a new `tx_audio/` package, reading from wherever TX mic
audio is already accessible before it reaches the rig), `dashboard/server.py`
for a new WS stream if server-side extraction is chosen, `dashboard/console.html`
for the display widget.

**Non-goals.** Do not modify the existing TX audio mute path — that
architecture already had five bugs fixed against it and is documented as
fragile; this package may *read* from it but must not alter its mute logic,
timing, or the ALC/compression chain it sits next to. This is an
observation-only feature with no new actuation path (same discipline as
keeping the advisor read-only, §7 of the audit).

**Validation.** This is the one package in the whole plan that's
essentially unvalidatable against a mock — a fake/synthetic sine wave
proves the rendering pipeline works, but the actual value of the feature
(does it help the operator tune real speech) can only be judged on real
audio. Plan for iterative on-air (or at minimum on-dummy-load, per the
dummy-load safety habit already established for TX experiments) testing as
part of this package, not as an afterthought.

---

### F2 — Richer WSJT-X/JS8Call parameter capture for RumLogNG

**Goal.** Extend what `wsjtx/qso_logger.py` already captures per QSO (it
currently joins WSJT-X's ADIF `LoggedAdif` event with a telemetry-window
summary into its own diagnostic JSONL — explicitly *not* RumLogNG, which
the operator has already told this module is the authoritative log) so
more parameters are available to correlate against RumLogNG's own
independently-populated log.

**Addresses.** Backlog item: "Better integration with WSJT-X and possibly
JS8CALL to allow more extensive parameters to be stored in the RumLogNG
logs that are automatically loaded."

**Depends on.** T0.

**Files/modules touched.** `wsjtx/qso_logger.py`, `wsjtx/adif.py`,
`wsjtx/udp_listener.py` if new fields must be captured from WSJT-X's UDP
stream rather than the final ADIF record.

**Non-goals.** This package's own docstring precedent
(`wsjtx/qso_logger.py`'s header comment) already establishes that this
console does not write into RumLogNG directly and RumLogNG is populated
from WSJT-X's own ADIF export independently. Do not build a direct
write-path into RumLogNG's database/file in this package — the backlog
item's exact intent ("stored in the RumLogNG logs") needs a clarifying
conversation with the operator before any code is written, specifically:
does "stored in RumLogNG" mean enrich what WSJT-X itself exports to ADIF
(so RumLogNG's existing auto-import picks it up), or does it mean a new,
separate write path into RumLogNG — these have very different scope and
risk, and only the first is assumed safe to build without further
discussion.

**Validation.** Testable against recorded/fake ADIF and UDP payloads (T0
fakes extend naturally to WSJT-X message fixtures, no live hardware
needed). One live-WSJT-X smoke test to confirm real traffic still parses
after the fields are extended.

---

### F3 — Console-triggered log entry

**Goal.** Let the operator manually trigger a log entry from within the
w7tlg console itself, rather than only ever having entries driven by
WSJT-X's own `LoggedAdif` event.

**Addresses.** Backlog item: "Need to be able to trigger a log entry from
within the w7tlg console. Need to discuss details." — the backlog item
itself flags this as undecided.

**Depends on.** F2 (shares the `qso_logger.py` plumbing; land after the
richer-capture package rather than before, so the manual-trigger path
writes the same enriched shape the automatic path does).

**Files/modules touched.** `dashboard/server.py` (new endpoint),
`dashboard/console.html` (new UI trigger), `wsjtx/qso_logger.py` (a
manual-entry function alongside the existing event-driven one).

**Non-goals.** The backlog item explicitly says this needs discussion —
this package's first deliverable is a short design note nailing down: does
a manual entry require an in-progress QSO/callsign context, or is it a
free-form operational note? What does it write to — the same
`qso_performance_log.jsonl`, or something new? Do not guess at this UX and
build ahead of that answer.

**Validation.** Mockable except for a final live check that a
console-triggered entry actually appears, correctly shaped, in whatever
store the design note settles on.

---

### F4 — Historical operating-comparison queries

**Goal.** A query feature letting the operator compare current operating
conditions/results against historical ones ("last time I used 17m," "was
SWR this high before") — the concrete first step of the already-noted
long-term "holistic operating analytics" goal, built on the operating data
that already exists (`data/qso_performance_log.jsonl`, `data/trend_logs/`).

**Addresses.** Backlog item: "Create a useful feature that allows querying
the previous monitoring results to compare previous operations to current
operations and discover problems."

**Depends on.** F2 (richer logged data makes comparisons more useful,
though not strictly blocking), T1 (if this exposes a new Claude-tool query
path through the advisor, it must inherit the same validation discipline
that closed Finding 9 — a new tool is a new opportunity for the same class
of bug if not built carefully).

**Files/modules touched.** New module (likely under `advisor/` if
LLM-driven natural-language query is wanted, or `tools/` for a simpler
structured-query approach — worth deciding which before starting), reads
`data/qso_performance_log.jsonl` and `data/trend_logs/`,
`dashboard/server.py` for a new endpoint, a new or existing page for the
query UI.

**Non-goals.** Strictly read-only against historical data — must gain **no**
actuation path back into rig/amp/SDR, full stop, regardless of how the
query is phrased or what an LLM-driven version might be tempted to offer as
a follow-up action. If built on top of `claude_advisor.py`'s existing
tool-enabled session, this must be explicitly re-reviewed against the same
CRITICAL-severity bar Finding 9 was held to — do not assume "read-only
intent" is sufficient without checking the actual tool surface offered to
the model.

**Validation.** Fully testable against recorded historical data with zero
live hardware involvement — genuinely one of the few packages in this plan
that can ship fully test-covered without any manual hardware smoke test at
all. Final validation is a UX check with the operator (does the answer to
a real query match what they'd expect), not a hardware check.

---

## Notes on sequencing

- Tiers 0/1 exist because the brief requires safety/correctness-critical
  work (hardware abstraction/mocks, input validation, event-loop/atomicity
  fixes) to land before feature work, and because T1 in particular (the
  CRITICAL advisor-actuation finding) is the single highest-severity item
  in `AUDIT.md` and should not wait behind any UI work.
- U2 (the column reorg) is the single largest diff in the plan and the one
  most likely to want its own extended session — it touches a 220KB file
  end to end. Consider running it in its own worktree/branch even relative
  to the other Tier 2 packages.
- F1 and F4 are the two packages most likely to reveal that they need a new
  backend capability not anticipated here once actually scoped in detail
  (F1: where exactly TX mic audio is accessible before muting; F4: whether
  the query UI should be LLM-driven or structured) — both should start
  with a short design/scoping pass before code, not go straight to
  implementation.
- Every package that touches `advisor/claude_advisor.py` or adds any new
  tool-call surface to it (T1, potentially F4) should be reviewed against
  AUDIT.md §7's standard: no path, direct or indirect, into actuation
  beyond what's explicitly intended and validated.
