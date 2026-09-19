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

## 6b. Findings from live testing — handed to U3 / backlog

Surfaced by the operator's hardware checkpoints during U2 (2026-09-19).
None are U2 regressions except B; all are out of U2's layout-only scope.

**A-F** came from U2's own checkpoints. **G and H** were added later the
same day, from a log capture taken while testing finding C — G turns out
to be C's actual root cause and is a priority item well ahead of any U3
UI work. Read C and G together.

**A. No SDR audio on 2m/70cm, in any mode. OPEN — cause unknown.**
Spectrum and waterfall are fine at 432 MHz, so the RSPduo capture is
working; only the audio demod stage is silent. Initially misdiagnosed as
"FM isn't implemented" — `sdr/audio_demod.py` really does implement SSB
only (its one mode branch is `mode == "USB" else -center_hz`, `:471`, a
sideband sign flip; there is no FM discriminator anywhere in `sdr/`), but
the operator then confirmed **USB on 2m is also silent**, so missing FM
support is not the cause. Note the RSPduo is an IQ receiver and does no
demodulation itself — FM here means writing a demodulator, not enabling a
device feature. Candidates not yet investigated: the much wider VHF
capture span (300 kHz vs 15 kHz) and its different decimation, and
`sdr/sdrplay_capi.py:70-79` recording that the RSPduo LNA/band tables are
applied "uniformly for now… pending live confirmation across bands" —
i.e. VHF/UHF was never verified after the RSPduo swap. Deserves its own
bounded session with the DSP code open.

**B. Antenna unavailability on 2m/70cm is announced 40 lines away.**
`acom_bridge.py:416` genuinely refuses `next_antenna` when `amp_in_path`
is false, so NEXT ANT is correctly rejected — but after U2a removed the
blanket ancestry dimming, the only feedback is a SYSTEM MSGS line at the
bottom of the window. The fix is a *local* inert state on `#box-antenna`
and `#box-abtest` using the existing `.rx-inert` pattern (`:554`, already
used for the FT-991A RX controls with an explanatory `title`), gated on
`amp_in_path`. **Not** a return to ancestry dimming, which hides controls
without saying why. This one is a gap U2 introduced.

**C. TX BW is offered in modes the rig rejects.** Clicking WIDE/RAG-CHEW/DX
outside SSB produces `Error: set_ssb_tx_bpf rejected by rig`. Confirmed by
the operator: switching the rig to SSB on 2m stops the error. The console
does not gate it — `set_ssb_tx_bpf` (`rig/rigctld_client.py:579-589`) just
sends `EX110n;` and reports what the radio says; menu 110 is SSB-only at
the radio. Same shape as B: gate the control on rig mode with a local
explanation. Pre-existing; U2a only made it reachable by un-dimming.

> **SUPERSEDED IN PART, 2026-09-19 — do not implement the mode gate yet.**
> Follow-up testing in DATA-U found the behaviour is *non-deterministic*:
> the same button in the same mode is sometimes accepted, sometimes
> rejected, sometimes silently ignored, and a retry after a rejection
> often succeeds. That is not a menu-validity pattern. **See finding G**
> — the cause is a rig reply-stream desync that force-reconnects the
> rigctld socket every ~15 s, plus the fact that `set_ssb_tx_bpf` is a
> fire-and-forget write whose value is never polled back in digital
> modes. A mode gate would not have fixed any of it. Whether menu 110
> genuinely accepts PKT-U is still unanswered and can only be tested once
> G is fixed.

**D. Waterfall blanks in digital mode when zoomed out.** In digital modes
the waterfall is fed *only* from `fine` frames (`:1701`) and the wide-frame
path is skipped (`:1712`). Fine frames span `DIGITAL_VIEW_SPAN_HZ = 3000`,
so at a whole-band 300 kHz view `cropToView()` spreads 3 kHz of data across
the full canvas and ~99% of every row renders as background. Visible in the
operator's own Step 0 baseline screenshot, before any U2 change. Arguably a
real bug: zooming out past ~3 kHz should fall back to the wide frame rather
than silently blanking. (Related, by design and not a bug: click-to-tune and
Whole Band are both disabled in digital modes, `:2338` and `:1586`.)

**E. Viewport-height console redesign.** `html, body { min-height: 100% }`
with no viewport constraint means the page grows with content, so expanding
a tall section makes a scrollbar appear and its width reflows every column
narrower — a visible layout shift just from opening a disclosure. U2 takes
the one-line mitigation (`html { scrollbar-gutter: stable }`, `:83`), which
stops the shift but not the growth. The proper fix is `.console` at viewport
height with `overflow-y: auto` on the side columns' `.panel-body`, so the
columns scroll internally. **This also fixes a real problem: SYSTEM MSGS
currently scrolls off-screen, and per finding B that bar is the only place
errors surface at all.** Touches `.scope-group` sizing and therefore the
canvas `getBoundingClientRect()` paths, so it needs its own package.

**F. Waterfall canvases read back every frame without
`willReadFrequently`.** Chrome warns twice on every session (once per
tuner): "Multiple readback operations using getImageData are faster with
the willReadFrequently attribute set to true", pointing at
`pushWaterfallRow`/`pushWaterfallRow2`'s scroll readback
(`wfCtx.getImageData(0, 0, w, h - 1)`). Both contexts are created with a
bare `getContext('2d')` (`:2348`, `:3536`). Fix is one argument each:
`getContext('2d', { willReadFrequently: true })`. Pre-existing since the
waterfall was written; harmless but it is free browser-side CPU on a
station that already has a CPU ceiling problem elsewhere. Do not apply it
to the spectrum or overlay contexts — those are write-only, and the flag
would pessimize them.

**G. The rig reply stream desyncs and force-reconnects every ~15 s. OPEN —
mechanism confirmed from a log capture, upstream cause narrowed.
THIS IS FINDING C'S ACTUAL ROOT CAUSE.** Surfaced 2026-09-19 while testing
finding C; log captured by the operator over 07:35:12–07:46:06 (654 s).

*The signature is exact and repeats 16 times in 11 minutes:*

```
GET 't' timed out (no reply within 2.0s)
   ~1.9-2.0s later
Poll error: Implausible frequency reading (0 Hz) — rig reply stream likely desynced
Reconnecting to rigctld in 5.0s...
   5.0s later
Connected to rigctld at 127.0.0.1:4532
```

Measured gaps between the timeout and the implausible reading across all
occurrences: 1.940, 2.025, 1.941, 2.013, 1.947, 1.897, 1.947, 2.025 s.
That consistency is the tell — it is one further read-timeout's worth.

*The chain, now traceable line by line:*

1. `_send_get("t\n", 1)` hits its 2.0 s read timeout (`:945`) and calls
   `_drain_stale_reply()`.
2. `_drain_stale_reply()` (`:961`) waits **1.0 s** for a straggler and
   breaks on the first timeout. The real replies are arriving ~4 s after
   the request, so the drain gives up too early and mops up nothing.
3. `_poll_state` continues to `_get_float("f\n", 1)` (`:699`).
4. The late reply to **`t`** lands in **`f`**'s read. PTT's answer is the
   string `"0"`.
5. `freq = 0` fails `FREQ_SANITY_MIN_HZ` → `RuntimeError` (`:710`) →
   `_poll_loop` sets `connected = False` → `finally: _disconnect()` sets
   `self._writer = None` → `_run` sleeps `reconnect_interval = 5.0`.

The 0 Hz is not a garbage value — **it is PTT's reply read one slot
early.** The sanity check is working exactly as designed and is the only
reason this is visible at all; without it every subsequent read would
stay shifted silently. It is firing constantly.

*Why `t` specifically.* 11 of the 13 named timeouts in the capture are
`t`. `dashboard/server.py:492` — the fast-PTT watchdog — writes `b't\n'
` every **5 ms** (`_FAST_PTT_POLL_MS = 5`) on its own connection, i.e.
~200 requests/second of the same command. rigctld serializes all clients
onto one serial port, so the main poll loop's `t` queues behind that
flood. `RIGCTLD_DAEMON_CACHE_MS = 50` (lowered from the 1000 ms default
to make knob tuning feel smooth) caps how much of it the daemon cache can
absorb. Add WSJT-X in DATA-U and the two short-lived `get_dt_gain` /
`get_ssb_tx_bpf` connections (`:502`, `:559`) and there are four-plus
clients on one serial link. The watchdog also closes and reopens its
socket on every one of its own 0.5 s timeouts (`:553-559`), adding
connection churn under exactly the conditions that cause the timeouts.

*Impact on finding C, which is the reason this was found.* Three
observed outcomes, none of them a rig mode rejection:

- **"rejected by rig"** — `set_ssb_tx_bpf` returns False only when
  `self._writer` is None or closing (`_send_raw_ex_set`, `:1021`).
  That is precisely the 5 s reconnect window. 16 cycles × 5.02 s = **80 s
  of the 654 s capture, ~12%, with the console unable to send anything at
  all** — plus ~2 s per cycle of connected-but-desynced time before it.
  `console.html:3815` renders any `ok:false` as `<cmd> rejected by rig`,
  a generic client-side fallback. **The rig never rejected anything and
  cannot** — `_send_raw_ex_set` is fire-and-forget and never reads a
  reply. The message is a false attribution.
- **Silently ignored, no error** — the write succeeds, `ok:true` is
  returned, and `state.ssb_tx_bpf = value` is set optimistically
  (`:587`). In DATA-U nothing ever contradicts it: `_poll_ssb_bpf` is
  gated on `not self.state.is_digital` (`:875`), and `PKTUSB` is digital
  (`:280`). **In DATA-U the console's TX BW readout is unverified from
  the moment it is clicked.** Applied and lost look identical from the
  console side.
- **Accepted** — the write landed between desync cycles.

So gating TX BW on rig mode would have fixed none of the three. Finding C
is correctly on hold.

*Not yet established:* whether the ~4 s reply latency is contention alone
or something slower in rigctld/the serial link. Finding H rules out our
own event loop (see below). The decisive measurement is the one used on
2026-08-30 for the cache question — time `t` and `f` replies from a
separate raw socket against rigctld while the console runs, first with
the fast-PTT watchdog at 5 ms and then with it disabled or slowed.

---

**H. Audio queue drops are bursty, not a sustained deficit — and are
NOT the same problem as G.** Same capture. This is new data on the
already-open dropout finding (2026-09-13), not a new defect.

Two interleaved counters, one per `AudioDemodulator`
(`sdr/audio_demod.py:639`). Channel A reached 1170 and channel B 552 over
the capture, which reads alarming because **the counter is cumulative and
monotonic — it cannot recover by construction.** The diagnostic quantity
is the `+N in last 5s` delta, and the logger only emits a line when the
count changed.

Channel A deltas: +7, +125, +155, +82, +244, +102, +90, +20, +23, +65,
+5, +134, +46, +57, +15. Channel B: +7, +68, +38, +7, +103, +37, +26,
+15, +28, +97, +2, +100. And there are windows with **no line at all**,
meaning zero drops: 07:39:53→07:41:18 (85 s) and 07:44:13→07:45:09
(56 s).

So the rate goes to zero for a minute at a time and then bursts. That is
the **tail-latency-spike** signature already identified on 2026-09-13,
not a throughput ceiling. A and B spike together, within ~100-200 ms,
which points at a process- or system-wide stall rather than anything
per-channel.

*G and H are separate problems.* The timestamps anti-correlate as often
as they correlate:

- **07:39:03–07:39:53** — four drop bursts (+82, +244, +102, +37 on A)
  with **zero rig events**; the rig link is quiet 07:38:53→07:40:33.
- **07:41:33–07:42:08** — three complete reconnect cycles with **zero
  drop lines** (nearest are 07:41:18 and 07:42:33).

This also **rules out the most attractive unifying theory**: that DSP
load starves the asyncio event loop and makes `wait_for` fire on replies
that actually arrived on time. If that were happening, the rig timeouts
would track the drop bursts. They do not. The ~4 s reply latency in G is
coming from rigctld or the serial link, not from our own event loop.

---

**Do G or H connect to findings A or D?**

- **D — no.** The waterfall blanking is `cropToView()` spreading a 3 kHz
  fine frame across a 300 kHz view (`:1701`, `:1712`). Pure display
  geometry, no timing component. Unrelated.
- **A — probably not, but there is one cheap decisive test.** A is
  *continuous silence* on VHF/UHF with a working spectrum; H is
  *intermittent* loss on HF that goes to zero for a minute at a time.
  Intermittent drops produce choppiness, not silence. But if the 300 kHz
  VHF span costs enough per callback, A could be a *sustained* 100 %
  drop, which is a different regime of the same counter. **Test:** tune
  to 2m and watch whether `Audio queue drops` climbs at a high, steady
  rate. Climbing steadily → A is a throughput problem and is related to
  H. Flat while the audio is silent → A is downstream of the queue
  entirely and is unrelated.


---

**I. PHANTOM TX — a desynced reply read as PTT puts the console into a
fake transmit state with no error anywhere. OPEN, pre-existing, and more
serious than G itself.** Proven 2026-09-19 from a second log capture plus
the amp's own telemetry.

*Symptoms the operator saw:* the console froze completely — no audio, no
spectrum or waterfall — for ~7 s after a burst of TX BW clicks, then TX
METERS showed **100 W with no transmission** for about a second. Nothing
in SYSTEM MSGS either time.

*Proof that no RF existed.* `data/trend_logs/trend_20260919_073442.csv`
across 08:13:05-08:13:23 shows `fwd_w = 0.0` and `is_tx = 0` on every
sample. `is_tx` is `t.flag_keyin` (`acom_bridge.py:733`) — the ACOM's own
KEY-IN flag from its telemetry frame, entirely independent of rigctld.
The amp was never keyed and never saw drive. Note also that
`acom_bridge.py:520` derives "TX start detected" from `rig.ptt`, so that
log line is **not** independent corroboration of a transmission — it is
the same false PTT propagating.

*The mechanism.* `_poll_state` parses PTT as `bool(int(val))` over
`float(lines[0])`. **Any numeric straggler except exactly `0` becomes
TX** — a passband (`2400`), a frequency (`14074000`), a signal strength
(`-73`), an SWR of `1.0`. The func reads that end `_poll_controls`
(`u NB` / `u NR` / `u ANF`, each returning exactly `"0"` or `"1"`) sit
immediately before the next cycle's `t`, which makes them the highest-risk
adjacency in the loop; in DATA-U the operator's preset leaves several of
them on.

*The captured sequence, with the ordering proof:*

```
08:13:09,713  GET 'f' timed out          <- ptt still False (f is only polled when !ptt)
              ...f's late reply straggles; next cycle's `t` reads it
08:13:13,873  GET 'l ALC' timed out      <- ALC is ONLY polled when ptt is True. PROOF.
08:13:14,875  TX start detected          <- from rig.ptt, not the amp
08:13:14,903  Audio WebSocket disconnected (x2)   <- audio dies
08:13:19,940  Implausible frequency (0 Hz) -> reconnect
08:13:24,952  Connected to rigctld
08:13:24,956  TX end detected            <- 4 ms after the reconnect
08:13:25,464  Audio WebSocket connected  <- outage 10.5 s
```

The 4 ms between the reconnect and "TX end" is the clincher: the
"transmission" ended the instant the reply stream was reset, not from any
change in RF.

*Why each symptom follows.* `gate_tx()` flushes the IQ queue and
`_publish` returns early on `tx_active` (no audio); the frontend freezes
its display on `rig.ptt` (`console.html:1603`) and `sdr_client.py:1014`
stops averaging (no spectrum/waterfall); the meter poll switches to the TX
set (`:781`) where **`l RFPOWER` returns the radio's power *setting*,
1.0 = 100%, not a measurement** (the 100 W). Nothing failed, so nothing
reached SYSTEM MSGS.

*Why this is worse than G.* The frequency sanity check is the only desync
guard in the system, and `f` is only polled when `not ptt`. **A phantom
PTT switches off the one detector that would have caught it**, which is
why this episode ran ~10 s instead of being killed in ~2 s like every
episode in the first capture. It also fakes a TX state to the amp bridge.

*Also visible in the same capture:* `alc` latched to `0.05` from 08:13:19
onward while `fwd_w = 0.0` and `is_tx = 0` — a misrouted `l ALC` straggler
stuck in state. Same root cause, and the same shape as the known
stale-telemetry display problem.

*Fix ATTEMPTED then REVERTED, 2026-09-19.* A strict `^[0-3]$` PTT guard,
a lock-held resync, a wider stale-reply drain and read-back-instead-of-
optimistic-write for the raw-CAT setters were all implemented and passed
98 tests, then **reverted in full** after a live run
(`rig/rigctld_client.py` is back at HEAD). They were not wrong in
principle, but two of them were actively harmful on real hardware:

- The wider drain runs while holding `_lock`, which took a timeout cycle
  from 3.0s to a measured 4.5s and pushed poll-loop lock occupancy from
  ~70% to ~100% during a timeout storm. TX BW and DT GAIN went to ~4s,
  and `session_switch` failed outright.
- Dropping the optimistic state write meant that when the read-back also
  failed, DT GAIN showed *nothing* — visibly dead rather than merely
  unverified, with no feedback either way.

The PTT guard itself was never shown to misbehave and is still the right
idea. It should be retried **after** finding J, not before: while every
command is failing by construction there is no way to tell a guard's
effect from the noise, which is exactly how this attempt went wrong.

*Original proposal, for the record.* Validate the PTT reply strictly: require*Original proposal, for the record.* Validate the PTT reply strictly: require
the raw string to match `^[0-3]$` before touching PTT, treating anything
else as a miss. That rejects `1.0`, `2400`, `14074000` and `-73` while
still accepting hamlib's `RIG_PTT_ON_MIC`/`ON_DATA` (2/3). It is the PTT
analogue of the frequency sanity check, and it closes the widest and most
dangerous hole. Worth raising separately: the fast-PTT watchdog
(`server.py:492`, own connection, every reply on it is a `t`, so it cannot
meaningfully desync) is the real TX detector — it is arguable the main
poll loop should not set PTT at all.

**Important: this was captured on PRE-FIX code.** The server process
started 07:34:34 and never restarted, so the G fixes were not loaded. The
log confirms it independently: no `Drained N stale replies` line appears
anywhere, and the 08:13:17,922 -> 08:13:19,940 gap is 2.018 s, shorter
than the new drain's 2.5 s minimum when nothing arrives. The resemblance
to `DRAIN_MAX_TOTAL_S`'s worst case was a coincidence — finding I is
pre-existing and independent of that change.


---

**J. The read timeout is shorter than the reply latency, so during a
latency episode EVERY command fails by construction. OPEN — this is the
real root cause under G and I.** Measured 2026-09-19 from the 08:34-08:39
capture, which is the first run with the drain instrumented well enough to
time replies.

Each `Drained N stale replies` line dates the straggler's arrival: drain
elapsed, minus the closing quiet window, plus the 2.0s read timeout
already spent. Across 12 stragglers:

```
2.98  3.44  3.56  3.93  3.94  4.00  4.01  4.02  4.02  4.03  4.03  4.04
min 2.98s   median 4.01s   max 4.04s      read timeout = 2.0s
```

**12 of 12 could not have arrived in time.** Not "often slow" — every one
was a guaranteed failure before it was sent. Draining, resyncing,
reconnecting and the PTT/frequency guards are all downstream of this; they
manage the wreckage of a request that was never going to be answered.

*The clustering is the clue.* 4.00, 4.01, 4.02, 4.02, 4.03, 4.03, 4.04 is
not congestion — congestion gives a spread. It is quantised, which means a
fixed timeout-and-retry somewhere below us. Hamlib's serial backends carry
their own `timeout` and `retry` settings, and 2 retries at a 2000ms serial
timeout lands exactly here. If that is what it is, the serial read is
*failing* and being retried, not merely queueing — which is a different
problem from raw contention and would explain why the fast-PTT watchdog's
5ms poll hurts so much more than its request count alone suggests.

*Two ways out, and they are not equivalent:*

- **Raise the read timeout above ~4.5s.** Cheap, and better on both axes:
  at a 2.0s timeout a command costs 2.0s + drain and *fails*, leaving a
  straggler; at 5.0s the same command costs ~4.0s and *succeeds*, leaving
  nothing to clean up. Strictly better whenever the reply eventually
  arrives, which was 12/12 here. Worse only when a reply never comes at
  all. **Not yet tested.**
- **Fix the ~4.0s itself.** Check hamlib's `timeout`/`retry` for this
  backend and run the fast-PTT watchdog experiment. This is the actual
  cure; the timeout change only stops the console from guaranteeing its
  own failure while the latency exists.


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
