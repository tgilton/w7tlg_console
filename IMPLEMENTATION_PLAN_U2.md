# IMPLEMENTATION_PLAN_U2.md

Implementation plan for work package **U2 — Column reorg: operational /
RX1 / RX2 / TX boxes** (`REFACTOR_PLAN.md` §Tier 2, backlog item 8).

Status: **plan only — no code written.** Branch `u2-column-reorg`,
currently identical to `c563a74` (U1b).

---

## 0. Headline finding, read this first

**Most of U2 already landed on 2026-09-11.** `REFACTOR_PLAN.md`'s
description of the *current* layout is stale — it was written against the
pre-RSPduo-reorg file and describes `panel-modedsp` as holding
"Mode/Filter/DSP" and `panel-center`/`panel-center2` as bare spectrum
columns. That is no longer true. In today's `dashboard/console.html`:

- The 4-zone grid U2 asks for **already exists** (`console.html:104-108`)
  and its own CSS comment already states the intended zoning in U2's own
  words ("the right side of the entire console should be Transmit stuff").
- RX1 and RX2 are **already fully self-contained** — each carries its own
  frequency readout, S-meter, AF/RF GAIN, Mode, spectrum+waterfall,
  display controls, tuning row, Filter, AGC/NR, notches and Audio EQ.
- SSB Audio and TX meters **already moved** to the right column.

The **actual remaining delta** for U2 is therefore much smaller than the
plan's paragraph implies, and splits into three clearly separable parts:

| Part | What it is | Size | Recommendation |
|---|---|---|---|
| **U2a** | Move the `Antenna` and `Antenna A/B Test` section-boxes from `panel-bandamp` into `panel-modedsp` | ~50 lines of markup, cut-and-paste | **Do now** — this is the real, uncontested move |
| **U2b** | Rebalance the two side columns' default widths + relocate the now-misfiled CSS rules and stale comments | ~15 lines CSS/JS | **Do now**, after U2a |
| **U2c** | "One shared, non-duplicated Mode control that drives both receivers" | Behavioral, not layout | **Defer — needs an operator decision, see §7** |

U2c is the only part that carries real risk, and §7 argues it should not
ship inside U2 at all.

---

## 1. Current layout structure (as actually built today)

### 1.1 Grid skeleton — `console.html:85-120`

```
.console  display:grid
  grid-template-columns: var(--col-left, 240px) 1fr 1fr var(--col-right, 268px)
  grid-template-areas:
    "status   status   status   status"
    "modedsp  center   center2  bandamp"
    "sysmsgs  sysmsgs  sysmsgs  sysmsgs"
```

Five top-level `.panel` children of `.console`, in source order:
`panel-status`, `panel-modedsp`, `panel-center`, `panel-center2`,
`panel-bandamp`, `panel-sysmsgs`. Placement is entirely by `grid-area`
(`:271`, `:309`, `:310`, `:461`), **not** by source order — so a panel's
position in the markup is already decoupled from its position on screen.

Only the two side columns (`modedsp`, `bandamp`) have the flex
`.panel-body` + collapse-tab + resize-handle wrapper (`:123-142`). The
two center panels are plain `.panel`s with no body wrapper — an
asymmetry to be aware of when moving markup between them.

### 1.2 Panel contents today, in source order

**`panel-modedsp`** (left, `:626-726`) — `.panel-body` holding four
`.section-box`es:
1. Session (`#session-grid`, `#session-step`)
2. Band + VHF/UHF (two `.band-grid`s separated by a `.divider`) — closes at `:657`
3. Digital Audio (`#digital-tx-controls`: DT GAIN, SOURCE RX1/RX2) + FT-991A RX NB/DNF + FT-991A RX Preamp/ATT, all in **one** box separated by `.divider`s
4. Measure Noise (RX1-only by explicit operator decision — comment at `:716`)

then `#resize-modedsp` and `#tab-modedsp` as siblings of `.panel-body`.

**`panel-center`** (RX1, `:728-948`) — no `.panel-body`; `#vfo`,
`.vfo-meta` badges, S-meter, `.vol-alc-group` (AF GAIN / RF GAIN / MODE),
`.scope-group`, then section-boxes for Spectrum&Waterfall + Tuning, and
Filter/AGC-NR/EQ.

**`panel-center2`** (RX2, `:950-1145`) — structurally identical to
`panel-center`, all IDs suffixed `2`, plus the `LNK`/`CPY` buttons in its
tuning row.

**`panel-bandamp`** (right, `:1147-1336`) — `.panel-body` holding eight
`.section-box`es:
1. Amp (ACOM 1200S) + DIAG HV/I
2. TX Meters (`#tx-meters`)
3. SSB Audio (MIC/COMP/TX BW)
4. Fault Status (`#box-fault-status`, collapsible)
5. Operating Mode (AMP OFF / AMP ON)
6. Exciter Drive
7. **Antenna** (`:1283-1296`) — `.ant-grid` A1F/A2F/A3R/A4R indicators,
   NEXT ANT button, `#dummy-timer-wrap`
8. **Antenna A/B Test** (`#box-abtest`, `:1298-1333`, collapsible)

Note the collapse-tab and resize-handle appear **before** `.panel-body`
here (`:1148-1149`) and **after** it in `panel-modedsp` (`:723-724`) —
mirror-image edge placement.

### 1.3 Non-obvious couplings that the visual layout does not reveal

These are the things that make this a reorg rather than a cut-and-paste.

**(a) The amp-bypass dimmer swallows the antenna controls.**
`console.html:3888` does:

```js
document.querySelector('.panel-bandamp')
  .classList.toggle('amp-bypassed', s.amp_in_path === false);
```

and `:464` applies `opacity: 0.35; pointer-events: none` to
`.panel-bandamp.amp-bypassed > .panel-body`. Because Antenna and A/B Test
live inside that `.panel-body`, **selecting 2m/70cm today silently
disables NEXT ANT and the whole A/B test panel.** Nothing in the markup
of those two boxes says so; it is purely ancestry. Moving them out of
`panel-bandamp` changes that behavior *by omission*, with no error and no
diff line that mentions it. This is the single highest-value thing in
this document — see Step 3 and §7.

**(b) The "Band / amp column" CSS block is already misfiled.**
`.band-grid` (`:473`) is defined under the `/* ── Band / amp column ── */`
banner but is used exclusively by `panel-modedsp`. Likewise `.ant-grid`,
`.ant-indicator`, `.ant-alias` (`:554-556`), `.dummy-timer-*`
(`:572-575`), `.abt-row`, `#abt-results`, `.scroll-table` (`:576-580`)
sit in that block and will follow the antenna boxes left. **All of these
are single-class or ID selectors with no panel ancestry**, so they
continue to match after the move — the file just becomes misleading to
read. Cosmetic, but worth one housekeeping commit.

**(c) `.section-box:last-child { margin-bottom: 0 }` (`:487`) is
positional.** After the move, the last box in `panel-modedsp` and in
`panel-bandamp` both change identity. Effect is a 16px bottom margin
appearing/disappearing. Cosmetic only.

**(d) The two side columns' default widths are asymmetric and
duplicated in two places.** 240px left / 268px right, expressed as CSS
custom-property fallbacks at `:104` and `:118-120`, **and again** as
`COLUMN_WIDTH_DEFAULT` in JS at `:4595`. The move shifts two boxes' worth
of content from the 268px column into the 240px one, so both numbers want
revisiting — and they must be changed in both places or double-click-reset
will snap back to the old width.

**(e) Panel placement is `grid-area`-driven, so markup order is free.**
Moving a `.section-box` between panels is a pure parent change; nothing
reorders on screen as a side effect. Conversely, nothing protects against
dropping a box into the wrong panel's `.panel-body` vs. alongside it —
`panel-bandamp` has the resize handle and tab as siblings of
`.panel-body`, and a box landing there instead of inside would be
invisible to the collapse rule at `:135`.

---

## 2. Target layout

Grid skeleton: **unchanged.** Same four columns, same `grid-template-areas`,
same `grid-area` assignments. No change to `.console`'s structure at all.

**Left column `panel-modedsp` — "general operational"**, section-boxes in
this order:

1. Session
2. Band + VHF/UHF
3. **Antenna** *(moved in)*
4. **Antenna A/B Test** *(moved in)*
5. Digital Audio + FT-991A RX (NB/DNF, Preamp/ATT)
6. Measure Noise

Rationale for the order: backlog item 8 names exactly four boxes as
"general operational functions — SESSION, BAND, ANTENNA, ANTENNA A/B
TEST", so those four go first, in the order the operator listed them.
Digital Audio and the inert FT-991A RX controls stay but drop below them;
FUNCTIONS.md §8 already describes Digital Audio source as a left-column
control, and Measure Noise is explicitly RX1-only and rarely used.

**Middle `panel-center` / `panel-center2`: unchanged.** Already
self-contained. U2 touches neither. (This matters for §5.)

**Right column `panel-bandamp` — purely transmitter-side:**
Amp, TX Meters, SSB Audio, Fault Status, Operating Mode, Exciter Drive.
Antenna and A/B Test removed.

**Not in this package:** the shared Mode control (§7), any RX2 mode-set
expansion (that is U3), the analog S-meter (U3), the frequency knob (U4),
the main-display power readout (U5).

---

## 3. Ordered DOM/CSS changes

Each step is intended to be one small, self-contained, reviewable commit.

### Step 0 — Baseline capture (no code)
Before touching anything, with the console open:
- Screenshot the full window at the current column widths.
- Note the current values of `localStorage` keys `columnWidth:modedsp`,
  `columnWidth:bandamp`, `columnCollapsed:*`, `sectionCollapsed:box-abtest`,
  `sectionCollapsed:box-fault-status` (devtools → Application → Local
  Storage). Steps 2 and 4 interact with these and a stale value is an easy
  false alarm.
- Select 2m or 70cm once and observe the amp-bypassed dimming with the
  Antenna box still inside it — this is the "before" for Step 3.

### Step 1 — Move the Antenna section-box
Cut `console.html:1283-1296` (the `<div class="section-box">` through its
matching `</div>`, inclusive of the Antenna `<h2>`, `.ant-grid`, NEXT ANT
button and `#dummy-timer-wrap`) and paste it into `panel-modedsp`'s
`.panel-body` immediately after the Band box's closing `</div>` at
`:657`, before the Digital Audio box at `:659`.

Zero attribute changes. No IDs renamed. No CSS touched. Indentation only.

### Step 2 — Move the Antenna A/B Test section-box
Same operation for `#box-abtest`, `console.html:1298-1333`, pasted
directly after the Antenna box from Step 1.

Keep `id="box-abtest"` **exactly** — `initSectionBoxes()` (`:4537`) keys
its persisted collapsed state on `sectionCollapsed:box-abtest`, so a
rename would silently reset Terry's saved collapsed/expanded preference.

### Step 3 — Resolve the amp-bypass coupling *(decision needed)*
After Steps 1-2, Antenna and A/B Test are no longer dimmed/disabled when
`amp_in_path === false`. Two options:

- **3a (recommended default): keep them live.** The ACOM 1200S *is* the
  antenna switch — `RigControl.nextAntenna()` sends `next_antenna` to the
  amp, which still cycles its relays whether or not it is in the RF path
  for the current band. Dimming them on 2m/70cm was an ancestry
  side-effect, not a decision. Add `id="box-antenna"` and a short comment
  on both boxes recording that they are deliberately *outside* the
  bypass dimmer. (`id` is safe: `initSectionBoxes()` only matches
  `.section-box.collapsible[id]`, and the Antenna box is not collapsible.)
- **3b: preserve today's dimming.** Add an explicit hook rather than
  relying on ancestry: in `updateUI` also toggle `amp-bypassed` on
  `.console`, and add
  `.console.amp-bypassed #box-antenna, .console.amp-bypassed #box-abtest { opacity:.35; pointer-events:none; }`.

Either way this is an *explicit* one-line-of-CSS decision instead of an
invisible consequence. Do not skip this step — skipping it silently picks
3a.

### Step 4 — Rebalance the default column widths
Left column gains two boxes; right loses two.

**Measured baseline (operator devtools, 2026-09-19):** `columnWidth:modedsp = 286px`, `columnWidth:bandamp = 274px`, `columnCollapsed:* = 0`, `sectionCollapsed:box-abtest = 1`, `sectionCollapsed:box-fault-status = null`.

**Revised proposal:** `--col-left` 240px → **288px**, `--col-right` 268px → **274px** — i.e. adopt the operator's own dragged-in widths as the new defaults. The plan's original 248px for the right column is **withdrawn**: it assumed losing two boxes made that column narrower, but those boxes never set its width, and the operator had already dragged it *wider* than default. Evidence it is if anything still too narrow: SSB Audio's TX BW segmented row wraps to three lines at 274px. That wrap is a content fix, not a layout move — hand it to U3 (backlog item 2), do not fix it here.

Must be changed in **all four** places or behavior diverges:
- `:104` `grid-template-columns` fallback
- `:118` `.console.collapsed-left`
- `:119` `.console.collapsed-right`
- `:4595` `COLUMN_WIDTH_DEFAULT` in JS (used by double-click reset)

`COLUMN_WIDTH_LIMITS` (`:4594`, [200, 560]) needs no change. **Terry's
saved `columnWidth:*` values will override the new defaults** — to see
them, double-click each resize handle once, or clear those two keys.

### Step 4b — `.ant-grid` overflow at narrow column widths *(found during Step 1)*

**Symptom.** Dragging the left column narrow makes the A1F/A2F/A3R/A4R
indicators overflow past the panel's right edge instead of shrinking or
wrapping (operator screenshot, 2026-09-19, immediately after Step 1).

**Cause.** `.ant-grid` (`console.html:554`) uses
`grid-template-columns: repeat(2, 1fr)`. A `1fr` track's automatic minimum
is `min-content`, so the tracks cannot shrink below the longest unbreakable
word in the aliases ("Unconnected", "Multiband", "Vertical"). `.panel`
already sets `min-width: 0` (`:122`), so the panel is not the constraint —
the grid track floor is.

**Not a U2 regression.** `COLUMN_WIDTH_LIMITS` (`:4594`) allows a 200px
minimum on *both* columns, so the same overflow was always reachable in
`panel-bandamp`; the move only relocated it to a column the operator
actually resizes.

**Fix (2 lines, folded into Step 4's commit):**

```css
.ant-grid  { grid-template-columns: repeat(2, minmax(0, 1fr)); }  /* was 1fr */
.ant-alias { overflow-wrap: anywhere; }
```

**Checkpoint.** Drag the left column to its 200px minimum: indicators stay
inside the panel border, aliases wrap, nothing clips. Then drag back out
and confirm the normal-width rendering is unchanged.

---

### Step 5 — Relocate the now-misfiled CSS rules
Move `.band-grid` (`:473`), `.ant-grid` / `.ant-indicator` / `.ant-alias`
(`:554-556`), `.dummy-timer-wrap` / `.dummy-timer` (`:572-575`),
`.abt-row` / `#abt-results` / `.scroll-table` (`:576-580`) from the
`/* ── Band / amp column ── */` block into the `/* ── Mode / DSP
column ── */` block at `:270`.

**Verify while doing this:** every rule moved is a single-class or single-ID
selector with no `.panel-bandamp` ancestry, so relocation changes neither
what it matches nor its specificity. If any rule turns out to be
panel-scoped, stop and treat it as a real change, not housekeeping.
`.amp-bypassed-note` (`:465-472`) stays where it is — it is genuinely
amp-column-only.

### Step 6 — Update the in-file zoning comment
Rewrite the `.console` comment block at `:88-103` so the four-zone
description names Antenna/A-B Test as left-column "general operational"
and the right column as purely TX. The existing comment is the file's own
explanation of why the layout is shaped this way; leaving it describing
the pre-U2 arrangement is how the next session gets misled the same way
`REFACTOR_PLAN.md` misled this one.

### Step 7 — Documentation
- `FUNCTIONS.md` §7 heading "Right column — antenna" → "Left column —
  antenna"; §4 "Left column — rig and DSP" reworded to cover the
  operational grouping.
- `DESIGN.md` §10 layout description updated to match.
- `REFACTOR_PLAN.md` U2 entry annotated: note that the 2026-09-11 reorg
  already delivered the self-contained-RX and TX-column halves, and that
  U2 as shipped is the antenna move + width rebalance; U2c deferred.

Docs only — no app code. Keep it as its own commit so the code diff stays
reviewable.

### Step 8 — U2c: shared Mode control — **deferred, see §7**

---

## 4. JS that could break — full inventory

This is the category where a DOM move breaks code that was never edited.
Every site below was found by grepping `console.html` for
`getElementById`, `querySelector(All)`, `parentElement`, `parentNode`,
`closest(`, `children[`, `*Sibling`, `*ElementChild`, `appendChild`,
`insertBefore`, `innerHTML =`, `getBoundingClientRect`, `offsetWidth`,
`clientWidth`, `ResizeObserver` and `addEventListener('resize'`.

**First, the good news — a whole class of risk is absent.** Every element
lookup in both `Panadapter` and `Panadapter2` happens inside `init()` or
inside an event handler (e.g. `spectrumWrap = document.getElementById(...)`
at `:2269` and `:3457`), never at script-parse time. So no reference is
captured before the DOM settles, and moving a node between parents does
not invalidate any held reference — it is the same node object. There is
also no `ResizeObserver` and no `window.addEventListener('resize', …)`
anywhere in the file, so nothing depends on being *notified* of a layout
change.

| # | Site | Mechanism | Risk under U2 | Mitigation |
|---|---|---|---|---|
| **R1** | `:3888` `document.querySelector('.panel-bandamp').classList.toggle('amp-bypassed', …)` + `:464` `.panel-bandamp.amp-bypassed > .panel-body` | **Ancestry** | **HIGH — silent behavior change.** Selector still resolves; the *set of controls it dims and disables* changes. Antenna/A-B stop being disabled on 2m/70cm. No error, no failing selector, nothing in the diff to flag it. | Step 3 forces the explicit decision |
| **R2** | `:4546-4640` `toggleColumn` / `initColumnCollapse` / `updateColumnTab` / `startColumnResize` / `resetColumnWidth` / `initColumnWidths` | IDs `panel-<which>`, `tab-<which>`, `resize-<which>`; maps `COLUMN_TAB_LABEL` / `COLUMN_CLASS_NAME` / `COLUMN_VAR_NAME` / `COLUMN_WIDTH_LIMITS` / `COLUMN_WIDTH_DEFAULT` / `ALL_COLUMNS` | **LOW as planned** (panel IDs and column count unchanged). Would break the moment anyone adds, renames or splits a column — all six maps must stay in lockstep, and the CSS fallbacks are a seventh copy | Don't rename panels in U2. Step 4 changes exactly one of these maps and its CSS twins |
| **R3** | `:3993` `querySelectorAll('[data-band]')`, `:3996` `querySelectorAll('[data-mode]')`, `:3860` `querySelectorAll('[data-session]')` | **Document-wide attribute selector**, each doing wholesale `btn.className = 'btn' + …` | **HIGH if U2c ships.** These reset `className` outright. A shared Mode control that reuses `data-mode` but carries any other class (`dsp-agc-btn`, `eq-square-btn`, a layout class) loses it on the next state tick — the button renders unstyled and no one knows why. Equally, duplicating any `data-band` button makes both instances state-driven | Another reason to defer U2c (§7). If a shared control is ever built, give it distinct ids and update these loops deliberately |
| **R4** | `:4031-4041` antenna indicator update — `btn.className.replace(' active', '').replace(' active-amber', '')` on `#ant-1..4` | **String surgery on the exact class string** `btn ant-indicator[ disabled]` | **LOW-MEDIUM.** Survives the move as-is. But Step 3a adds an `id` (safe) — do **not** also add a class whose name contains the substring `active`, and do not reorder classes such that the naive `.replace()` chain drops the wrong token | Keep class lists on `#ant-1..4` byte-identical through Steps 1-3 |
| **R5** | `:4537` `initSectionBoxes()` → `querySelectorAll('.section-box.collapsible[id]')`, `localStorage['sectionCollapsed:' + box.id]` | Class + id, position-independent | **LOW.** Move is safe; a rename of `box-abtest` would silently reset the operator's saved collapsed state | Preserve `id="box-abtest"` exactly (Step 2) |
| **R6** | `:487` `.section-box:last-child { margin-bottom: 0 }` | **Positional CSS** | **LOW, cosmetic.** Last box in each column changes identity; a 16px margin appears/disappears at the bottom of each side column | Visual check in Step 1/2 verification |
| **R7** | Canvas sizing: `:1992`, `:2124`, `:2137`, `:3077`, `:3183`, `:3190` — all read `getBoundingClientRect()` and reallocate `canvas.width/height` on mismatch, inside the `requestAnimationFrame` loop (`:2039`, `:2324`, `:3134`, `:3531`) | Re-measured **every frame** | **LOW.** A column-width change is self-correcting within one frame; no resize handler is needed and none exists. **Expected side effect:** assigning `canvas.width` clears the canvas, so Step 4's width change blanks the waterfall history once on first load. That is pre-existing resize behavior, not a U2 bug | Note it in the Step 4 checkpoint so it isn't mistaken for a regression |
| **R8** | Hit-testing / pan / hover: `:1848` `attachHoverCrosshair`, `:2292` + `:2305` click-to-tune and drag-to-pan, `:2952` `attachHoverCrosshair2`, `:3504` + `:3522` RX2 equivalents | `getBoundingClientRect()` read **per event** | **LOW.** Always current. Mouse listeners at `:2295`/`:2303` are on `window`, so they are position-independent by construction | Frequency-accuracy check after Step 4 (click-to-tune must land where clicked) |
| **R9** | `:3852-3858` `updateSessionUI` — `grid.innerHTML = …` when `grid.children.length !== profiles.length` | Rebuilds `#session-grid`'s children | **LOW.** Container-scoped, not position-dependent. Only a hazard if the Session box were ever nested inside another rebuilt container | None needed; do not nest the Session box |
| **R10** | `:1366-1378` sysmsg log `appendChild` / `.sysmsg-empty` lookup | Scoped to `#sysmsg-log` | **NONE.** `panel-sysmsgs` is untouched | — |
| **R11** | Cross-script-tag globals: `notifyRx1ViewChange` → `Panadapter2.onRx1ViewChange` (`:1729-1737`), `Panadapter.getViewCenterHz()` (`:2527`), `RigControl.setDragging(…)` from inline handlers throughout | **`<script>` tag order**, not DOM position | **NONE as planned, HIGH if script tags move.** The four `<script>` blocks (`:1347`, `:1434` Panadapter, `:2392` Panadapter2, `:3617` RigControl) must keep their relative order and must stay after all markup | Move markup only. Do not relocate any `<script>` tag in U2 |
| **R12** | Every inline `onclick=` / `oninput=` / `onmousedown=` handler on the moved boxes (NEXT ANT, `#abt-*`, `toggleSectionBox('box-abtest')`) | Attribute on the element | **NONE.** Handlers travel with the node; the module globals they call are page-level | — |
| **R13** | `dashboard/index.html` also contains `.ant-grid` / `#abt-rounds` markup (`:543`, `:1247`, `:1275`, `:1427`) | Separate page served at `/` | **NONE.** `console.html` is served at `/console` and is a standalone document — no shared CSS/JS file | Out of scope; do not "keep them in sync" |

---

## 5. Does U1b (today's work) depend on anything U2 moves?

Checked directly against `c563a74`, `ea08821` and `e0ad917`. Summary:
**no, provided U2 stays out of `panel-center2` — which the plan above
does.** Detail:

**The view-sync relay (`notifyRx1ViewChange` / `onRx1ViewChange`) — SAFE.**
`notifyRx1ViewChange` (`:1729`) takes three numbers and makes one
cross-module call. Its nine call sites (`:1755`, `:1765`, `:1785`,
`:1805`, `:1838`, `:2276`, `:2318`, plus the drift-recenter and span
paths) all live inside `Panadapter`'s closure and touch the DOM only via
`#span-slider` / `#span-val` by ID. `onRx1ViewChange` (`:2697`) writes
`viewCenterHz2` / `viewSpanHz2` and `#span2-slider` / `#span2-val` by ID.
**Zero DOM traversal, zero ancestry, zero position dependence.** The
relay is a plain function call across script tags — dependent on R11
(script order), which U2 does not touch.

One nuance: the drag-to-pan relay added in U1b is attached to
`spectrumWrap` (`#spectrum-wrap`) inside `panel-center`, via `window`-level
`mousemove`/`mouseup` listeners at `:2303`/`:2295`. Those compute
`hzPerPx` from a live `getBoundingClientRect()` each move (R8), so even a
width change is handled correctly. `panel-center` is not moved anyway.

**The `linkToRx1` gates — SAFE.** All of them (`:2801-2814`,
`:3470-3475`, `:2488`, `:2527`, `:2771`, `:2900`, `:2907`, `:3482`,
`:3503`, `:3521`) resolve `#link2-btn`, `#freq2-input`, `#tune2-btn`,
`#span2-slider` by ID, and every one of those elements is inside
`panel-center2`'s tuning row, which U2 does not touch. The
`disabled`-property writes are element-local.

**The RX2 mode normalization fix (`sidebandForRigMode2`) — SAFE AS
PLANNED, BUT THIS IS THE ONE U2c WOULD BREAK. Flagging explicitly.**
`applyRx1ToRx2` (`:2846-2853`) does:

```js
mode2 = sidebandForRigMode2(lastRigMode);
document.getElementById('mode2-USB').classList.toggle('active', mode2 === 'USB');
document.getElementById('mode2-LSB').classList.toggle('active', mode2 === 'LSB');
document.getElementById('badge2-mode').textContent = mode2;
```

with **no null guards**, near the top of the function, *before* the
`syncFilterRange2()` / `set_panadapter_freq` / `setAudioTarget` work
beneath it. If U2c's "one shared Mode control" removes or renames
`#mode2-USB` / `#mode2-LSB`, this throws `TypeError: … classList of null`
on **every Link and every Copy**, aborting `applyRx1ToRx2` before RX2's
frequency, filter width and audio target are ever applied.

That is *exactly* the failure `ea08821` fixed yesterday — the
`ReferenceError: sidebandForRigMode is not defined` that made Copy/Link
"silently stop moving RX2's frequency" in Terry's 2026-09-19 smoke test.
Shipping U2c in the same pass would reintroduce the identical symptom
through a different cause, one week later, in a diff that reads as
"layout only." Independent reason to defer U2c.

**The Channel-B audio-target seed (`e0ad917`) — SAFE.** Server-side
(`dashboard/server.py` / SDR start path); U2 is `console.html`-only.

---

## 6. Sequencing and per-step manual verification

Frontend-only, no automated coverage, so every checkpoint is a human
looking at the running console. Per `feedback_dont_stack_live_symptom_fixes`:
**complete each step and its checkpoint before starting the next**; do not
batch Steps 1-5 and then smoke-test once.

Per `feedback_confirm_before_console_restart`: **do not start or stop the
console.** These are static-file edits — a browser reload picks them up.
If a restart is genuinely needed, ask first. Also close duplicate console
tabs before testing (duplicate tabs caused RX2 clipping on 2026-09-12).

| Step | Change | Checkpoint — what to verify |
|---|---|---|
| 0 | Baseline capture | Screenshot + localStorage values + amp-bypassed "before" recorded |
| 1 | Antenna box → left column | Reload. Antenna box renders in the left column below Band, with borders/spacing intact at 240px. A1F/A3R/A4R highlight correctly for the current antenna; aliases still populate. **NEXT ANT actually cycles the amp's antenna** (this is the one control here with real hardware effect — watch the amp's front panel). Dummy-load timer still appears when A4R is selected. Right column re-flows with no gap where the box was |
| 2 | A/B Test box → left column | Reload. Box renders collapsed (or matching its saved state — compare against the Step 0 note). Expand: manual-switching checkbox, antenna checkboxes, rounds/dwell/MHz inputs, Start/Stop all present and correctly sized at the narrower width. **Collapse it, reload, confirm it comes back collapsed** — that proves `sectionCollapsed:box-abtest` survived the move |
| 3 | amp-bypass decision | Select 2m (or 70cm). Under 3a: amp boxes dim, Antenna + A/B Test stay bright and clickable. Under 3b: both dim together. Either way, the observed result must match the option chosen — not "whatever happened." Return to 20m and confirm everything un-dims |
| 4 | Column width rebalance | Reload — expect **no visible change yet**, because saved `columnWidth:*` override the defaults. Double-click each resize handle to reset. Left widens, right narrows, spectra re-flow. **Waterfall history clears once — expected (R7), not a bug.** Then: click-to-tune on RX1's spectrum lands on the frequency clicked; hover crosshair tracks correctly; drag-to-pan moves the right amount; **with LNK on, RX2's view still follows RX1's** (this is the U1b relay under a new width). Drag each handle to both limits and back; collapse and re-expand each column |
| 5 | CSS rule relocation | Reload. Pixel-identical to post-Step-4. Any visible difference means a rule's specificity or match set actually changed — revert and investigate rather than adjusting |
| 6 | Zoning comment | No runtime effect. Reload once to confirm nothing was damaged mid-comment |
| 7 | Docs | None (no app code) |

**End-to-end pass after Step 6**, one live session, deliberately covering
the cross-column paths a layout diff would not obviously touch — and per
`feedback_dual_channel_coupling_regressions`, **exercising both RX
channels, not just RX1**:

1. Fresh reload. RX1 and RX2 both produce audio; LNK is on by default and
   RX2 is aligned to RX1 (the U1 cold-start invariant — confirm it still
   holds after the reorg).
2. Turn the VFO knob. RX1 follows the radio; RX2 follows RX1. Zoom RX1
   (wheel + span slider + BAND + CTR) — RX2 mirrors each. Drag-pan RX1 —
   RX2 mirrors.
3. `CPY`, then toggle `LNK` off and on. RX2's mode badge, filter width and
   frequency all update — **no console errors** (this is the
   `applyRx1ToRx2` path from §5; a silent abort there looks like "Copy
   just doesn't do anything").
4. Un-link RX2, tune it independently, confirm RX1 is unaffected, re-link.
5. Switch bands including 2m/70cm and back; confirm the Step 3 dimming
   behavior and that Antenna state stays correct across the transition.
6. Change session profile (SSB ↔ FT8); confirm the Session box still
   rebuilds and digital-audio source RX1/RX2 still switches.
7. Right column: TX meters, Fault Status expand/collapse, Operating Mode,
   Exciter Drive all still respond. Amp telemetry still updates.
8. Devtools console clean throughout. Then pull `/api/state` and confirm
   the values there match what the UI is showing (per
   `feedback_verify_with_live_telemetry` — do not conclude "nothing broke"
   from the absence of console errors alone).

---

## 7. U2c — the shared Mode control: recommend deferring, with reasons

U2's goal paragraph asks for "one shared, non-duplicated Mode control that
drives both receivers." Recommendation: **do not build it in U2.** Four
reasons, in order of weight:

1. **It is not layout — it is behavior, which U2's own non-goals forbid.**
   U2 states it "moves controls; it does not change what any control does,
   which WS command or state field it's bound to." Today RX1's mode grid
   calls `RigControl.setMode()` (a **CAT command to the FT-991A**) and
   RX2's calls `Panadapter2.setMode()` (a **client-side SDR demodulator
   sideband switch**). These are different commands to different hardware
   that happen to share a label. Merging them is precisely the kind of
   change the non-goal excludes.
2. **It would break `applyRx1ToRx2` in the exact way `ea08821` just
   fixed.** See §5 — unguarded `getElementById('mode2-USB').classList`
   throws and silently aborts Link/Copy before frequency and audio target
   are applied. Same symptom Terry reported on 2026-09-19, one week later,
   in a diff labelled "layout only."
3. **R3 makes it a styling landmine.** `querySelectorAll('[data-mode]')`
   at `:3996` reassigns `className` wholesale on every state tick.
4. **It is largely already true in practice.** With LNK on (the default),
   `applyRx1ToRx2` already copies RX1's sideband to RX2 on every rig mode
   change. The operator-visible gap is the *other* half of backlog item 6
   — RX2 offering only USB/LSB where RX1 has six modes — and
   `REFACTOR_PLAN.md` explicitly assigns that to **U3**.

**Open question for Terry, needed before U3 can settle this:** the RX2
mode buttons carry a comment from 2026-09-11 (`:983-988`) recording an
unanswered question — should CW/AM/FM be given **real SDR demodulation**
on RX2, or stay absent? Until that is answered, "one shared Mode control"
has no well-defined behavior for four of its six buttons. That answer
belongs in U3, not here.

If U2c is wanted inside U2 anyway, it should be its own branch off the
finished U2a/U2b, with §5's null-guard hazard fixed *first* (add guards to
`applyRx1ToRx2`) as a separate commit.

---

## 8. Summary of what to approve

- **Steps 0-2, 4-7**: mechanical, low-risk, recommended as-is.
- **Step 3**: needs Terry's call between 3a (recommended — antenna
  controls stay live when the amp is bypassed) and 3b (preserve today's
  dimming).
- **Step 8 / U2c**: recommended deferred to U3, with the open CW/AM/FM
  demod question answered first.

Awaiting approval before any code is written.
