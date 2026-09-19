# SCOPE_REVIEW_U3.md

**Date:** 2026-09-19  **Branch:** `u3-scope-review`  **Status:** planning only, no code

Purpose: "U3" has accumulated far more than the three visual items it was
written for. This document splits what is currently filed under U3 into
independently-schedulable packages, sizes each one honestly against the
code as it stands *after* U2, and recommends an order.

Sources read: `REFACTOR_PLAN.md` §U3 (including everything U2 folded in),
`IMPLEMENTATION_PLAN_U2.md` §6b and §7, `DESIGN.md` §9/§11,
`FUNCTIONS.md` §8 item 10, plus the actual code —
`sdr/audio_demod.py`, `sdr/combiner.py`, `sdr/sdr_client.py`,
`dashboard/server.py`, `dashboard/console.html`, `tests/`.

---

## 0. Summary

| # | Package | Size | Blocked on | Order |
|---|---|---|---|---|
| **R1** | **Rig reply-stream desync / reconnect loop — finding G** | **Unknown until measured** | — | **0 — ahead of all U3** |
| U3a | Context gating — findings B + F (+ AM/FM honesty title) | **Small** (half session) | one decision | 1 |
| ~~U3a-C~~ | ~~TX BW mode gate — finding C~~ | **On hold** | R1 | after R1 |
| U3b | Viewport-height console — finding E | **Small–medium** (one session) | variant choice | 2 |
| U3d | Gain-slider prominence + analog S-meter | **Medium** (one to two sessions) | — | 3 |
| U3c | Orphan regrouping (SSB Audio / Exciter Drive) | **Unscopeable** | Terry's design input | 4, when unblocked |
| — | Finding A diagnosis (no SDR audio on 2m/70cm) | Medium, unknown | — | 5, prerequisite for FM |
| U3e | **AM/FM demodulation** | **Large** (3–5 sessions) | A, squelch decision, §5.7 decisions | 6 |

Three structural conclusions:

1. **Finding C is withdrawn from U3a and superseded.** Log analysis
   (2026-09-19, recorded as finding G in `IMPLEMENTATION_PLAN_U2.md`
   §6b) shows the TX BW non-determinism is not a mode-validity problem
   at all — it is a rig reply-stream desync that force-reconnects the
   rigctld socket every ~15 s. **R1 below now precedes everything in
   this document.**

2. **U3e is not "the rest of U3." It is larger than U3a+U3b+U3c+U3d
   combined**, and it is the only package here that can ship looking and
   sounding fine while being wrong. It should not share a branch, a
   session, or a review with any of the UI work.

3. **"Give RX2 the full mode set" is not a UI task and should be deleted
   from the visual package.** Adding AM/FM/CW buttons to RX2 before U3e
   exists would reproduce on RX2 the exact defect §6b found on RX1: a
   button that looks functional and isn't. Those buttons belong inside
   U3e, as its last step, not its first.

---

## 1. U3a — Context gating (findings B, C, F)

### Finding B — antenna unavailability on 2m/70cm

**Verdict: still small. Genuinely layout-only. Recommended first** — it is
the one item here that U2 introduced.

Verified against the current tree:

- `#box-antenna` (`console.html:735`) and `#box-abtest` (`:752`) both exist as
  addressable boxes in the left column after U2a's move.
- `.rx-inert` exists (`:610`) and is already applied to two FT-991A RX
  control groups (`:819`, `:825`) with an explanatory `title`.
- `updateUI()` already reads the gate: `s.amp_in_path === false` at
  `:3971`, currently used only for `.panel-bandamp`'s `.amp-bypassed`
  class.

So the fix is ~3 lines plus two `title` strings. Nothing U2 did makes it
bigger; U2's own zoning comment (`:727-734`) already spells out the
intended shape.

**One thing that needs deciding, and it is not cosmetic.** `.rx-inert` is
`opacity: 0.4` and *nothing else* (`:610`). That was correct for the
FT-991A RX controls, which are inert but harmless to click. NEXT ANT is
not harmless in the same way: leaving it clickable means the operator
still gets `Error:` in SYSTEM MSGS on top of the dimming. Options:

- **B1 (recommended)** — `.rx-inert` for the look, plus `pointer-events:
  none` on the two boxes only, plus the `title`. The control is visibly
  unavailable and says why on hover, and the error path is never reached.
- **B2** — dim + title only, keep it clickable, accept the duplicate
  error. Cheaper, and preserves "the operator can always try."

Either way this is **not** a return to ancestry dimming, per the standing
note at `:730-734`.

### Finding C — TX BW offered in rig-rejected modes

**Verdict: WITHDRAWN from U3a. The premise was wrong.** Full analysis is
finding G in `IMPLEMENTATION_PLAN_U2.md` §6b; summary here.

Live testing in DATA-U showed the same button in the same mode being
accepted, rejected, and silently ignored on different clicks, with
retries often succeeding. A log capture explains all three, and none of
them is the rig rejecting menu 110:

- **"rejected by rig"** happens when `self._writer` is None — i.e.
  during the 5 s reconnect window that finding G's desync loop opens 16
  times in 11 minutes (~12 % of the session). The message is a generic
  client-side fallback at `console.html:3815`; `set_ssb_tx_bpf` is
  fire-and-forget and **cannot** learn a rejection from the radio.
- **"Silently ignored"** is unobservable by design in DATA-U:
  `_poll_ssb_bpf` is gated on `not is_digital`
  (`rigctld_client.py:875`) and `PKTUSB` is digital, so nothing ever
  contradicts the optimistic `state.ssb_tx_bpf = value` at `:587`.
- **"Accepted"** is a write that landed between desync cycles.

A mode gate fixes none of this. Worse, shipping one would have made the
console *look* correct while the underlying link kept dropping. The
original question — does menu 110 accept PKT-U? — is still open and
**cannot be answered reliably until R1 is fixed**, because today a
"rejection" carries no information about the radio at all.

### Finding F — `willReadFrequently` (rides along)

Two arguments, one per tuner: `getContext('2d')` at `:2348` and `:3536`
become `getContext('2d', { willReadFrequently: true })`. Both are the
waterfall scroll-readback contexts that `pushWaterfallRow` /
`pushWaterfallRow2` call `getImageData` on every frame (`:2232`). Do not
touch the spectrum or overlay contexts. Free CPU on a station with a
known CPU ceiling. Fold into U3a.

### An honesty fix worth doing in U3a

RX1's mode grid (`:911-918`) offers AM and FM, and they change the radio
while console audio silently stays SSB. That is the same "looks
functional, isn't" defect as finding B, and it will remain true until
U3e ships. A `title` on those two buttons saying so costs minutes and
decouples the misleading-UI problem from the DSP work. Recommended.

---

## 1b. R1 — Rig reply-stream desync loop (finding G)

**Priority 0. Ahead of every U3 package, including the ones already
called small.** Not UI work; listed here because it displaced finding C
out of U3a.

**What is proven.** The failure chain is traced end to end and the
mechanism is not in doubt: a `t` read times out at 2.0 s;
`_drain_stale_reply()`'s 1.0 s window (`rigctld_client.py:961`) is far
too short for the ~4 s reply latency actually present, so it mops up
nothing; the late `t` reply — the string `"0"` — is then consumed by the
next command, `f`; the frequency sanity check (`:710`) correctly rejects
0 Hz and forces a reconnect. 16 cycles in 654 s, each costing a 5 s
window in which the console cannot send anything.

**What is not yet established:** why rigctld is taking ~4 s to answer.
Finding H's anti-correlation rules out our own event loop being starved
by DSP load. The prime suspect is contention: the fast-PTT watchdog
(`server.py:492`) writes `t` **every 5 ms** on its own connection, which
is the same command that accounts for 11 of the 13 observed timeouts,
and `RIGCTLD_DAEMON_CACHE_MS = 50` limits how much of that the daemon
cache absorbs.

**Why this cannot be sized yet.** The fix depends entirely on that
answer. If it is watchdog contention, the change may be as small as
backing off the 5 ms poll or letting the cache absorb it — but that
trades against the PTT-detection latency the watchdog exists to provide,
which is a real operating requirement, not a tuning knob. If rigctld or
the serial link is slow for another reason, it is a different fix.
**Measure before proposing.**

**The measurement, first.** Same technique used on 2026-08-30 for the
cache question: time `t` and `f` replies from a separate raw socket
against rigctld while the console runs — once with the watchdog at 5 ms,
once with it slowed or disabled. That single comparison decides the
whole package.

**Three defects worth fixing regardless of the outcome**, each small and
independently useful:

1. `_drain_stale_reply()`'s 1.0 s window is demonstrably too short.
   Widening it, or draining until the socket is genuinely idle, stops
   the shift from propagating even when a reply is late.
2. `console.html:3815` renders every `ok:false` as `<cmd> rejected by
   rig`. For fire-and-forget commands the rig said nothing, so the
   message is false. It should distinguish "not sent — rig link down"
   from an actual refusal. This is console-wide, not TX-BW-specific.
3. `set_ssb_tx_bpf` writes `state.ssb_tx_bpf` optimistically and, in
   digital modes, nothing ever reads it back (`:875`). Either poll it in
   all modes or do not claim the value. The same shape applies to
   `set_dt_gain`.

**Explicitly not part of this package:** finding H. See §1c.

---

## 1c. Finding H — audio queue drops (not a new defect)

Recorded here only to close it out of R1's scope. The 1170/552 drop
totals look alarming because the counter is cumulative and monotonic;
the `+N in last 5s` deltas show a bursty pattern with **zero drops for
85 s and 56 s stretches**. That is the tail-latency-spike signature
already identified on 2026-09-13, not a throughput ceiling and not a
regression.

It is **a separate problem from R1** — the timestamps anti-correlate
(heavy drops with a healthy rig link 07:39:03–07:39:53; three reconnect
cycles with zero drops 07:41:33–07:42:08), which also rules out the
tempting unifying theory that DSP load starves the event loop.

No action proposed. It stays where it was, on the existing open dropout
finding, with this capture as better evidence.

---

## 2. U3b — Viewport-height console (finding E)

**Verdict: smaller than §6b feared, *if* the right variant is chosen.**

§6b warns this "touches `.scope-group` sizing and therefore the canvas
`getBoundingClientRect()` paths." That is true of one variant and not the
other, and the difference is most of the cost.

The grid is already the right shape for this. `.console`
(`console.html:96-140`) is a three-row template — `status` / four columns
/ `sysmsgs` — with no height constraint, and `html, body { min-height:
100% }` (`:85`) is what lets the page grow. `.panel-modedsp` and
`.panel-bandamp` already have a `.panel-body` child to scroll (`:152`);
`.panel-center` / `.panel-center2` are plain `.panel` and would take
`overflow-y: auto` directly.

**Variant 1 (recommended, small).** `.console { height: 100vh;
grid-template-rows: auto 1fr auto; }` plus `overflow-y: auto` on the four
column bodies. Canvas heights stay at their fixed `260px` / `195px`
(`:442`, `:444`); the columns scroll internally around them. The status
bar and SYSTEM MSGS become permanently pinned, which is the real point.
**No `getBoundingClientRect()` path changes behaviour** — the canvases'
rects are unchanged in both dimensions — so the sizing code at `:2074`,
`:2206`, `:2219`, `:3159`, `:3265`, `:3272` is untouched. One session,
mostly spent checking the four columns and both collapsed states
(`.collapsed-left` / `.collapsed-right`, `:147-149`).

**Variant 2 (larger, not recommended now).** Let the scope boxes grow to
fill the viewport. This *does* hit the canvas paths, and it introduces a
new bug the fixed heights currently prevent: `pushWaterfallRow`
(`:2229`) scroll-blits the canvas with `getImageData`/`putImageData`, and
assigning `canvas.width`/`canvas.height` clears the backing store. With
fixed px heights that happens only on a DPR change. With
viewport-derived heights, **every window resize wipes the waterfall
history on both receivers.** Fixing that means preserving and rescaling
the history across a resize — real work, and a separate package.

So: E is small at variant 1 and medium-plus at variant 2. **Needs Terry's
choice.** Variant 1 delivers the stated benefit (SYSTEM MSGS stays
visible, no reflow-on-disclosure) at a fraction of the cost.

Note U2 already took the one-line mitigation `html { scrollbar-gutter:
stable }` (`:83`); that stays either way.

---

## 3. U3c — The orphan-regrouping question

**Verdict: cannot be scoped. Blocked on Terry, by design.**

This is a layout-design question, not an implementation question, and the
record says so twice:

- `FUNCTIONS.md` §8 item 10 — the A/B test was pulled into a full-width
  bottom row **twice** (giant buttons, then space-between fields) and
  both were reverted as worse than the cramped column. Recorded guidance:
  revisit via an actual `FieldGrid` redesign (`DESIGN.md` §6.4), not by
  widening the container.
- `DESIGN.md` §9 already specifies a bottom strip with three fixed zones
  (TUNING / DISPLAY / AUDIO EQ). Any new bottom-strip proposal has to say
  how it relates to that, not just add a fourth zone.

Two further reasons not to start:

1. Much of the "orphan" feeling came from those controls being *dimmed*
   on VHF/UHF — which U2 fixed by narrowing the amp-bypass dimmer. The
   premise may have partly dissolved. **Re-evaluate on the live console
   before moving anything.**
2. The candidates don't share one answer. "SSB Audio belongs with Digital
   Audio" is a grouping claim that crosses the TX/RX boundary U2 just
   established (`.panel-bandamp` is now TX-only, `:115-120`). "Exciter
   Drive → RADIO POWER" is a rename. "Measure Noise is an orphan" is a
   placement claim. Bundling them guarantees the same outcome as the two
   reverted attempts.

**What is actually needed before any code:** a sketch or a written target
from Terry — which controls end up where, and what the bottom strip is
*for* if it gains a fourth zone. Without that, this package cannot be
given a size and should not be given a branch.

---

## 4. U3d — Gain-slider prominence and the analog S-meter

Kept as a package because it is what U3 was originally written for, and
it is genuinely independent of everything above.

- **AF/RF gain prominence** — styling. `.eq-bmt-group input[type=range] {
  transform: scaleY(1.5) }` (`:515`) is the existing precedent for
  "make this slider louder without inventing a new control language."
  Small.
- **Skeuomorphic analog S-meter, both receivers** — a new canvas or
  inline-SVG widget, drawn twice, plus needle ballistics. Medium, and the
  only genuinely new component. Existing non-goal stands: mirror
  whatever telemetry the current readout uses, do not change the source.
  (Per project memory the SDR is the actual receiver post-RSPduo, so
  confirm that is what feeds it before wiring, and change presentation
  only.) Needs one live check against the existing numeric readout and
  the S9 cal reference so the redesign doesn't silently redefine the
  number.

**Removed from this package:** "give RX2 the full mode set." See §0 and
§5.6 — it is part of U3e.

**Also worth retiring from the U3 text:** the backlog's "evaluate using
RX1 as the main DATA-U receiver" question. That capability already
exists as `set_digital_source` (`server.py:1347-1375`), which
subscribe/unsubscribe-swaps whichever channel feeds the single BlackHole
cable. The remaining question is only whether the default and its
discoverability are right — a much smaller item than the backlog wording
implies.

---

## 5. U3e — AM and FM demodulation

Treated here as its own major package. It is **not** a continuation of
"mode parity," and framing it that way is what made it look small.

### 5.1 What exists today

`sdr/audio_demod.py` is one linear pipeline in `_process()` (`:707-833`),
per block of raw IQ:

1. Mix to baseband at `target.freq_hz` (`:718`)
2. Optional static phase rotation (`:721`)
3. Two-stage stateful decimation, overlap-save, to
   `INTERMEDIATE_RATE_HZ = 16_000` complex (`:730-742`)
4. Optional fine FFT for the digital-mode zoom (`:747`)
5. Complex asymmetric channel filter, 161 taps, overlap-save (`:750-767`)
6. `np.real(filtered) / 32768` (`:772`)
7. EQ (stateful `sosfilt`) → NR (DeepFilterNet3) → AGC → hard clip → int16

**There is no detector stage.** Step 6 *is* the SSB detector: taking the
real part of a signal whose passband has been pushed entirely to one side
of zero recovers the audio. That is elegant for SSB and it is precisely
why AM/FM is not "add a branch" — there is no seam to branch at. One has
to be created.

The whole file's mode-dependence is one line, `:471`:

```
shift_hz = center_hz if mode == "USB" else -center_hz
```

A sideband sign flip, inside `_design_ssb_filter`. Everything else in the
file is mode-agnostic.

Three further facts that matter for scoping:

- **Two independent instances, not one shared demodulator.**
  `sdr_client.py:279` (`self.audio`, channel A / RX1) and `:287`
  (`self.audio_b`, channel B / RX2). The class is channel-agnostic and
  all mutable state — overlap buffers, AGC gain, cached filters — is
  per-instance.
- **A third copy exists.** `sdr/combiner.py:118` is a deliberate
  independent copy of `_design_ssb_filter`, with the same sign flip at
  `:130`, for the RX0 coherent combiner.
- **No mode validation anywhere on the path.** `server.py:1332` passes
  `msg.get("mode", "USB")` straight into `set_target()` with no
  vocabulary check, and the sign flip treats *anything that is not the
  string `"USB"`* as LSB. Sending `mode: "AM"` today does not error — it
  demodulates LSB. Any new mode vocabulary has to be validated at that
  boundary or the failure mode is silent.

### 5.2 What an AM envelope detector actually requires

1. **A symmetric channel filter centred on the carrier**, not an offset
   sideband. Cheap: `_design_ssb_filter(bw, mode, low_cut_hz)` computes
   `center_hz = low_cut + bw/2`, so passing `low_cut_hz = -bw/2` yields
   `center_hz = 0`, `shift_hz = 0`, and a real symmetric lowpass. The
   filter design is reusable with a mode-dependent `low_cut_hz`.
2. **Envelope detection** — `np.abs(filtered)` instead of `np.real()`.
3. **DC / carrier removal.** `np.abs()` output rides on a DC pedestal
   equal to the carrier amplitude. It needs a DC blocker (one-pole
   highpass around 20 Hz) **with state carried across blocks**, the same
   discipline as every other stateful stage in this file. Omitting it
   does not produce silence or an obvious artifact: the AGC divides the
   whole thing down to hit `target_rms = 0.15`, and the result is audio
   that is quiet and thin but entirely intelligible. **This is the
   archetype of the silent-wrong-output problem in this package.**
4. **Bandwidth.** Ham AM wants ~6 kHz, broadcast/aviation up to ~10 kHz.
   Both width sliders are hard-capped at 3000 Hz (`console.html:989` and
   `:1196`). Per-mode slider ranges are required, on both receivers.
5. **Not in scope:** a synchronous/carrier-locked detector. It is better
   under selective fading but it is a PLL. Envelope first.
6. AGC and NR are fine as-is for AM voice — a constant carrier makes RMS
   AGC behave, and DeepFilterNet3 is speech-trained.

### 5.3 What an FM discriminator actually requires

1. **Polar discriminator** — `d = x[1:] * np.conj(x[:-1])`, then
   `np.angle(d)`. Cheap to write, and that is the trap.
2. **State across block boundaries.** The `[1:]`/`[:-1]` pairing consumes
   one sample and leaves a phase discontinuity at *every* block edge.
   Blocks here are ~131 samples at 16 kHz, i.e. roughly every 8 ms — a
   click train at ~122 Hz riding under the audio. This is the identical
   failure class the file's own comment at `:757-763` describes as "the
   original, most severe source of distortion" for the SSB filter. Fix is
   the same: carry the last complex sample forward, like `_ssb_overlap`.
   By ear this reads as "a bit of hiss," not as a bug.
3. **Scale factor** — `angle_diff * INTERMEDIATE_RATE_HZ / (2π ·
   deviation_hz)`. Get it wrong and the audio is simply quiet or loud,
   which is indistinguishable from a volume problem.
4. **De-emphasis** — one-pole IIR, state carried. Narrowband FM
   (amateur / land-mobile) uses 750 µs; broadcast FM uses 75 µs. Wrong
   constant = audio that is bright and harsh but fully intelligible.
   Another silent-wrong case.
5. **The bandwidth ceiling — the real architectural finding here.**
   `INTERMEDIATE_RATE_HZ` is 16 kHz, and stage-2's anti-alias cutoff is
   `output_rate / 2.4 = 6.67 kHz` (`:443`). The complex baseband
   therefore carries about **±6.67 kHz, i.e. ~13.3 kHz usable.** Amateur
   NBFM at ±5 kHz deviation with 3 kHz audio has a Carson bandwidth of
   2(5+3) = **16 kHz**. AM fits comfortably. NBFM does not — the outer
   sidebands get clipped, and a discriminator fed a truncated signal
   distorts in a way that varies with modulation level. Three options:
   - **(a) Accept it and measure.** Build FM at the existing rate, then
     characterise the distortion on a real 2m signal. Cheap, honest, and
     it may simply be acceptable for voice. **Recommended first.**
   - **(b) Raise `INTERMEDIATE_RATE_HZ` globally.** 24 kHz keeps
     `NR_RESAMPLE_RATIO = 48000/rate` an exact integer (2 instead of 3),
     but it perturbs `FINE_FFT_SIZE` bin maths, `_choose_decim_stages`'s
     exact-divisor requirement, and the `batch_samples` phase-lock snap —
     and it raises CPU on a station that already has a documented CPU
     ceiling. Not a side effect of an FM package.
   - **(c) A separate wider decimation tail used only in FM mode.** Most
     correct, most code, most new state.
   Do not silently pick (b) or (c) inside U3e.
6. **Squelch is not optional for FM.** Grepped the whole tree: there is
   no squelch, anywhere, in any form. An unsquelched discriminator emits
   full-scale white noise with no carrier present, and the AGC will
   faithfully level that noise up to `target_rms` — the speaker blasts.
   The existing squelch backlog item (gate on SDR signal strength) is
   therefore a **hard prerequisite or an in-scope sub-task**, not a
   nice-to-have. This needs a decision.

### 5.4 RX1 and RX2: one implementation or two?

**One implementation, two instances. No new architecture is needed on the
Python side.**

`AudioDemodulator` is already channel-agnostic and already instantiated
twice with fully per-instance state. New per-mode state — the FM
last-sample carry, the AM DC blocker, the de-emphasis filter — added as
instance attributes inherits that isolation for free. The fake SDR
(`tests/fakes/sdr.py:67-68`) constructs both real instances, so both are
testable without hardware.

**The coupling risk is not in Python. It is in the two client-side mode
mappers.**

- `sidebandForRigMode()` — `console.html:2262`, inside the RX1 closure.
- `sidebandForRigMode2()` — `console.html:2909`, a deliberate duplicate
  inside the RX2 closure (the comment at `:2899-2908` records why: the
  first one is closure-private, and calling it cross-closure threw and
  silently aborted Link/Copy).

Both collapse every rig mode to `'LSB'` if the string contains an `L`,
else `'USB'`. **Both must learn AM/FM, in lockstep.** And
`applyRx1ToRx2()` (`:2929-2934`) copies RX1's mode onto RX2 on every Link
tick — so after U3e, putting RX1 in FM would drag RX2 into FM too. That
directly contradicts the recorded intent that RX2 is an independent
second receiver on the FTDX 101D model. **Needs a decision** (§5.7).

This is exactly the pattern that has already caused two regressions in
one session — a fix scoped to one RX channel breaking the other through
shared mode/flag handling. Any change here is verified on both channels
or it is not verified.

**Two paths that are explicitly out of scope:**

- **RX0 / the combiner stays SSB-only.** `sdr/combiner.py` is a third
  demod implementation, and its coherent-combine step cross-correlates
  the two *SSB-filtered* channels to find their alignment (`:403-407`).
  An AM/FM path there changes the correlation's assumptions and is a
  harder problem than either detector. RX0 is already deferred on CPU
  grounds; leave it.
- **The digital-audio / WSJT-X feed needs a guard.** `set_digital_source`
  (`server.py:1347`) subscribes whichever channel feeds the single
  BlackHole cable. Today it is impossible to put that channel in a
  non-SSB demod, because every mode maps to USB or LSB. After U3e it
  becomes possible, and WSJT-X would receive envelope-detected or
  discriminated audio and decode nothing. Either block it or surface it
  loudly.

### 5.5 Test and validation strategy

This is DSP correctness work that can produce confidently wrong audio, so
the strategy is: **do not validate by listening. Validate by measuring,
against analytically known ground truth, before any hardware is
involved.**

The codebase already supports this. `_process()` is effectively pure
given `(block_i, block_q)` plus instance state, and the fake SDR reuses
the *real* `AudioDemodulator` rather than a stub — so synthetic-signal
unit tests are available today with no radio attached. There is a pytest
harness in place (`pytest.ini`, `tests/`), and
`tests/test_audio_target_atomicity.py` already drives
`AudioDemodulator` directly.

Proposed `tests/test_audio_demod_modes.py`:

1. **Round-trip tone tests, per mode, with analytic IQ.**
   - *AM:* carrier at a known offset, modulated by a 1 kHz tone at depth
     *m*. FFT the recovered audio and assert: a peak at 1 kHz within one
     bin; **no DC component above −60 dB** (catches the missing DC
     blocker); no 2 kHz component above −40 dBc (catches squaring /
     detector distortion); recovered amplitude proportional to *m*.
   - *FM:* carrier modulated by a 1 kHz tone at a known deviation.
     Assert the 1 kHz peak, a THD bound, and — most importantly —
     **recovered amplitude scaling linearly with deviation**, which is
     the only cheap way to catch a wrong scale constant that otherwise
     just sounds like a volume difference.
2. **Block-boundary continuity test — the single highest-value test
   here.** Feed one long synthetic signal two ways: as a single large
   block, and as N small blocks matching the real ~131-sample cadence.
   Assert the concatenated outputs match within a tight tolerance. This
   catches *every* missing-state bug at once — the FM last-sample carry,
   the AM DC blocker, the de-emphasis history — and it catches them as a
   numeric failure rather than as "sounds slightly hissy." This is the
   test that would have caught the original SSB overlap-save distortion.
3. **SSB regression guard.** Run the same harness against USB and LSB
   before and after introducing the detector seam, and assert the output
   is bit-for-bit (or within float tolerance) unchanged. Cheap insurance
   given how the refactor has to cut through the existing hot path.
4. **Both instances, and their independence.** Run every mode test
   against `sdr.audio` and `sdr.audio_b` via the fake SDR, and assert
   that setting one channel's mode leaves the other's `target` untouched.
   The dual-channel coupling regression has already happened twice.
5. **Level / clip assertion.** For each mode, on a nominal-level input,
   assert the output never rides the ±0.95 limiter (`audio_demod.py:829`). AM's DC
   pedestal and FM's unsquelched noise both surface here.
6. **Mode-vocabulary test at the WS boundary.** Assert that an unknown
   mode string is rejected rather than silently demodulated as LSB
   (§5.1). Follows the existing pattern in
   `tests/test_ws_frequency_mode_validation.py`.

**Only then, live** — and in this order:

- **AM first, on WWV at 10.000 MHz.** It is AM, it is receivable on the
  Greyline antenna, and its content is known in advance (tone, ticks,
  voice announcements), so "is this right?" has an actual answer. Far
  better than a random broadcast station. Cross-check against the
  FT-991A's own speaker on the same signal.
- **FM second, on a local 2m repeater, and only after finding A is
  resolved.** Today there is no SDR audio at all on 2m/70cm in any mode,
  USB included. Live-testing a brand-new FM demodulator into that is
  guaranteed to produce a symptom with two candidate causes. Diagnose A
  first — this is the "don't stack live-symptom fixes" case exactly.

### 5.6 Where the RX2 mode buttons fit

Last step of U3e, not part of the visual package: once the demodulators
exist and pass §5.5, add the modes RX2 can actually perform to
`:1122-1124`, update `setMode` (`:3021`) and `applyRx1ToRx2` (`:2929`)
together, and widen RX2's width slider per mode. Note `updateUI`
reassigns `className` wholesale on every `[data-mode]` button each state
tick (`:4077-4079`), so any new per-mode styling has to survive that.

### 5.7 Size

- Python demod work: moderate in line count (~120–180 lines across the
  detector seam, DC blocker, de-emphasis, discriminator state, per-mode
  filter and bandwidth selection). **This is the smallest part.**
- Mode vocabulary + WS validation, per-mode bandwidth ranges in two
  client closures, RX2's buttons, Link/Copy semantics: comparable size
  again in `console.html` and `server.py`.
- The test harness of §5.5 is likely the largest single piece — and it is
  what makes the package trustworthy rather than plausible.
- Squelch, if folded in rather than taken as a prerequisite.
- Live validation across two receivers, two bands, three modes.

**Honest estimate: 3–5 focused sessions once the prerequisites are
resolved**, and the only package in this document with a real chance of
shipping silently broken. It should not be started until finding A is
diagnosed and the squelch and FM-bandwidth decisions are made.

---

## 6. Recommended order

0. **R1** — measure the rig reply latency, then fix the desync loop.
   Ahead of everything else: while it is unfixed, the console is
   unreachable for ~12 % of a session, every other live test is
   contaminated by it, and any error toast it produces may be a false
   attribution. Finding C's testing has already been wasted once by it.
1. **U3a** — findings B + F, plus the RX1 AM/FM honesty `title`.
   Finding C is withdrawn (§1). B is a regression U2 introduced.
2. **U3b** — viewport-height console, variant 1. Pins SYSTEM MSGS, which
   is the console's only error surface.
3. **U3d** — gain prominence, then the analog S-meter.
4. **U3c** — orphan regrouping, as soon as Terry provides a target
   layout; re-evaluate the premise on the live console first, since U2's
   dimmer fix may have dissolved part of it.
5. **Finding A** — diagnose "no SDR audio on 2m/70cm." Its own bounded
   session with the DSP code open. Prerequisite for FM live-testing even
   though VHF/UHF console audio is otherwise de-prioritized.
6. **U3e** — AM/FM. Own branch, own sessions, tests before hardware.

---

## 7. Decisions needed from Terry

Blocking the package named:

| # | Decision | Blocks |
|---|---|---|
| 1 | Finding B: dim + block clicks (B1, recommended), or dim + title only and keep it clickable (B2)? `.rx-inert` is opacity-only today. | U3a |
| 2 | ~~Finding C mode gate~~ — withdrawn; superseded by finding G. Nothing to decide until R1 is fixed, at which point "does menu 110 accept PKT-U" becomes answerable for the first time. | — |
| 2b | R1: confirm the measurement is worth a session before any fix is proposed, and confirm PTT-detection latency is genuinely worth the 5 ms watchdog if that turns out to be the cause. | R1 |
| 3 | Finding E: variant 1 (columns scroll, canvases keep fixed heights — recommended) or variant 2 (canvases grow, which needs waterfall-history preservation across resize)? | U3b |
| 4 | Orphan regrouping: a sketch or written target — which control goes where, and what the bottom strip is for. Two prior attempts were reverted for want of this. | U3c |
| 5 | FM bandwidth: accept the ~13.3 kHz ceiling and measure the distortion on a real signal (recommended), or commit up front to raising the intermediate rate? | U3e |
| 6 | Squelch: hard prerequisite package, or folded into U3e? FM is unusable without it. | U3e |
| 7 | Should Link/Copy propagate an AM or FM mode from RX1 to RX2, or should RX2 hold its own mode independently once it has more than two? | U3e |
| 8 | Confirm the ordering: finding A diagnosed before any FM live test. | U3e |

Non-blocking, but worth confirming:

- Is AM wanted on both receivers, or is RX1-only enough for a first cut?
  RX1-only is meaningfully cheaper on the client side and would still get
  WWV and aviation AM.
- The U3 text's "evaluate using RX1 as the main DATA-U receiver" —
  `set_digital_source` already implements the switch. Is the remaining
  question just the default and its discoverability?
