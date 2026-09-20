# DESIGN.md — W7TLG Console

Design contract for `w7tlg_console`. Every UI change must conform. When a
control is added or edited, audit it against this document first.

The rules here are enforceable, not aspirational. If a control violates one,
it is a bug — file it as such.

---

## 1. First principle

**The console is an instrument, not an app.**

An operator scans it, they do not read it. Every design decision is judged
against one question: *can this be understood at a glance, without parsing
text?* Color, position, and size carry state. Words are labels, not status.

Corollary: a control whose **label changes** to report its state is a bug.
`NB OFF` / `NB ON` forces a string comparison. `NB`, dim or amber, is read
instantly. The label is the function. The color is the state.

---

## 2. Color

Four roles. No color may serve two roles. No color appears outside its role.

### 2.1 Chrome — structure. Never on an interactive control.

| Token | Hex | Use |
|---|---|---|
| `--chrome-accent` | `#8A93D9` | Section headers, column rules |
| `--chrome-label` | `#97A1B0` | Control labels |
| `--chrome-muted` | `#5C6675` | Units, inactive text, axis ticks |
| `--border` | `#232B38` | Borders, dividers, slider tracks |
| `--bg-0` | `#0A0E14` | App ground |
| `--bg-1` | `#0F141C` | Panel |
| `--bg-2` | `#161C26` | Control at rest |
| `--fg-0` | `#E6EAF0` | Primary values, readouts |

The periwinkle `--chrome-accent` appears on **zero interactive elements**.
That is what makes it legible as furniture rather than signal. It is the
one color the eye never has to decode.

### 2.2 State — only on controls and indicators.

| Token | Hex | Means |
|---|---|---|
| `--state-selected` | `#E8A33D` | This option is the active choice in its group |
| `--state-ok` | `#4ADE80` | Connected, healthy, permitted |
| `--state-fault` | `#F0564A` | Fault, inhibited, transmitting, stop |

Amber means *selected* on every control in the app. Band, mode, antenna,
AGC, filter, preamp — one rule, learned once.

**Three exceptions, all outside the control surface** (added 2026-09-20,
after the v2 redesign). Each is a place where a colour appears on
something that is not a control, so it cannot be misread as a state:

1. **The S-meter face.** Its red arc, over-S9 ticks and `+10/+30/+60`
   labels are a *graphic scale colour* printed on a paper-white instrument
   face, not `--state-fault`. A red band above S9 is the universal
   convention on an analogue meter and the whole point of drawing one.
   Nothing on that face is interactive.
2. **TRACE colours.** The spectrum trace is operator-selectable — ice
   blue (the default and today's exact `#67D0F0`), cyan, green, amber,
   magenta, white. These are data colours the operator chose for a plot,
   including amber and green. They never appear on a control, and the
   plot is not a state readout.
3. **The SPLIT pill** is amber and carries a `⚠` glyph. Amber alone would
   be a second meaning; the glyph is what makes it a warning rather than
   a selection, and it is `content`, so it survives a stylesheet failing
   to load.

The rule that still holds without exception: **amber on a control always
means selected, and nothing else on a control is amber.**

### 2.3 Data — measured values only.

| Token | Hex | Use |
|---|---|---|
| `--data-trace` | `#67D0F0` | Spectrum trace, slider fill, meter fill |
| waterfall | viridis | Unchanged |

### 2.4 Grouping is structural, not chromatic

Group identity is carried by **proximity and borders**, never by tinting
the controls inside the group. If you find yourself wanting a third accent
color to distinguish a group, the group needs a border, not a color.

(Amended 2026-08-30: originally read "hairline + header only, never a box
around a group." Operator decision — this console runs alongside RUNlogNG,
two terminals, and WSJT-X's two windows across two monitors, and a function
group needs to be locatable by eye without reading labels. Every `<h2>`
section (or, per §9, small related group of them) is wrapped in a card —
the same uniform-bordered-module look a classic transceiver control panel
uses, which reads as uncluttered *because* every region is delineated the
same way, not because it has fewer controls. The two column rules this
section used to specify are superseded by the boxes themselves.

**Amended again 2026-09-20 — the card border is now a 1px `--v2-border`
hairline on a `--v2-bg-card` fill, 14px radius, 16px padding.** Terry's
explicit decision after reviewing the v2 style guide. What the old border
was for still matters: it was **2px solid white, 7px radius**, chosen so a
region could be found by eye across two monitors without reading it. The
v2 card earns that back with a *fill* rather than a bright edge — a card
now differs from the page in background, not just outline — but if the
across-monitors locatability turns out to be worse in practice, two
variables on `.v2-card` restore the old look in one place:

```css
.v2-card { --v2-card-border-w: 2px; --v2-card-border-c: var(--v2-border-strong); }
```

`--v2-border-strong` is `#FFFFFF` and exists only for that.

A `.section-box` may also be `.collapsible`: its `<h2>` becomes a
disclosure toggle (chevron, no label change — the header text is still the
function, per §1), collapse state persists per-browser in `localStorage`.
Used sparingly, for sections that are large and used occasionally (Antenna
A/B Test) or that are only worth a glance when something's wrong (Fault
Status, which collapses to just its severity badge — the badge itself
stays outside the collapsing body and force-expands the section the moment
severity leaves OK, so collapsing it can never hide an actual fault). Not a
general-purpose decluttering tool — most sections stay always-expanded.)

### 2.5 TX state

The VFO readout reports transmit state and is the primary TX indicator.

- **RX**: `--fg-0` (near-white). The frequency is a fact, not a status.
- **TX**: `--state-fault` (red).

TX is never signalled by color alone. During TX, additionally render a 2px
`--state-fault` border on the console shell. Color plus geometry — this is
the one state where being misread is expensive.

---

### 2.6 The v2 token table

Every `--v2-*` variable, from the `:root` block inside the `V2:BEGIN` /
`V2:END` markers in `dashboard/console.html`. The DESIGN.md tokens in
§2.1–2.3 are **not** replaced — both sets are live, the v1 ones still
drive everything outside the v2 blocks, and each v2 colour below names the
v1 role it plays. **The roles are the contract; the hexes are not.**

| Token | Value | Role / v1 counterpart |
|---|---|---|
| `--v2-scale` | `1` | one dial for the whole size system |
| **Surfaces** | | |
| `--v2-bg-page` | `#0b1017` | app ground — `--bg-0` |
| `--v2-bg-chrome` | `#0d131c` | top bar, system bar, rails — `--bg-1` |
| `--v2-bg-card` | `#121a25` | card fill (new; cards had no fill in v1) |
| `--v2-bg-inset` | `#0b1017` | text inputs, selects |
| `--v2-border` | `#1f2b3b` | card edge, divider, grid gaps — `--border` |
| `--v2-border-inset` | `#2a394d` | input edge |
| `--v2-border-strong` | `#FFFFFF` | the old 2px locator border; see §2.4 |
| **Text** | | |
| `--v2-fg` | `#e8edf5` | primary text — `--fg-0` |
| `--v2-label` | `#8794a7` | control labels — `--chrome-muted` |
| `--v2-label-light` | `#c3ccda` | secondary labels — `--chrome-label` |
| `--v2-title-fg` | `#9aa1e0` | block titles, wordmark — `--chrome-accent`; **furniture only, never on a control** |
| `--v2-readout` | `#8cc8ee` | measured values, slider fill — `--data-trace` |
| `--v2-readout-idle` | `#6f7b90` | inactive readout |
| **State** | | |
| `--v2-selected` / `-bg` | `#f0b04a` / `#3a2a10` | selected — `--state-selected` |
| `--v2-ok` / `-bg` / `-border` | `#7fd68f` / `#16301f` / `#3fae5a` | healthy — `--state-ok` |
| `--v2-go-bg` | `#10261a` | Start button fill |
| `--v2-fault` / `-bg` / `-border` | `#ff7a63` / `#2a1210` / `#c8442f` | fault, stop — `--state-fault` |
| `--v2-tx-vfo` | = `--v2-fault` | the VFO on transmit (§2.5) |
| `--v2-tx-badge` | = `--v2-selected` | **PTT-TX and amp opr-tx badges stay amber** (§2.5) |
| **Controls** | | |
| `--v2-btn-bg` / `-border` / `-fg` | `#182131` / `#26344a` / `#b3bfcf` | button at rest |
| `--v2-btn-off-bg` / `-border` / `-fg` | `#111823` / `#1c2738` / `#56627a` | disabled — a real dim palette, not `opacity` |
| `--v2-track` / `--v2-thumb-bg` | `#243044` / `#eef4fa` | slider track / thumb |
| `--v2-notch-fg` | `#5d6c82` | default-value notch, dim frequency separators |
| `--v2-focus` | `#f0b04a` | focus ring, 2px, 2px offset |

Sizes and spacing tokens are tabled in §3 and §4.

---

## 3. Typography

Monospace throughout, tabular figures on every numeric readout
(`font-variant-numeric: tabular-nums`) so digits do not jitter as values
update.

Real separation between sizes. Timid scales read as mush.

This table used to say "three sizes" and then list 56 / 12 / 12 — which is
exactly the mush it warns about. The v2 scale (2026-09-20) delivers the
separation the rule was asking for. **12px is the floor: nothing in the
console is smaller**, and the old 10px dense-mode buttons are gone.

| Role | Token | Size | Weight | Tracking |
|---|---|---|---|---|
| Hero (VFO) | `--v2-font-hero` | 76px | 500 | 0.03em |
| Receiver chip (RX1/RX2) | `--v2-font-chip` | 18px | 700 | `--v2-ls-chip` 0.08em |
| Value / readout | `--v2-font-value` | 16px | 700 | — |
| Block title | `--v2-font-title` | 14px | `--v2-weight-title` 500 | `--v2-ls-title` 0.2em |
| Button | `--v2-font-btn` | 13px | `--v2-weight-btn` 600 | `--v2-ls-btn` 0.04em |
| Pill / badge | `--v2-font-pill` | 13px | `--v2-weight-pill` 700 | — |
| Label | `--v2-font-label` | 12px | `--v2-weight-label` 500 | `--v2-ls-label` 0.16em |
| Body | `--v2-font-body` | 12px | 400 | — |

Every size is `calc(Npx * var(--v2-scale))`, so the whole system can be
dialled from one variable. `--v2-scale` is 1. Radii, border widths and the
slider thumb are deliberately **not** scaled — a 1.5px border scaled to
1.8px only blurs.

The meter face is the one place another family appears: **Barlow Semi
Condensed** 700/800 for its scale numerals, loaded alongside JetBrains Mono
and used nowhere else.

Section headers and control/meter labels (VOL, DT GAIN, ALC, Fwd P, SWR,
etc.) are uppercase. Values, readouts, and descriptive text (antenna
aliases, system messages) are not — uppercase marks "what is this control,"
never "what is its value." (Amended 2026-07-14; originally read "Section
headers are the only uppercase text in the app," but labels had already
drifted uppercase in practice before this doc caught up.)

---

## 4. Spacing

Scale: **4 / 6 / 8 / 12 / 16 / 24 / 32**. No other values. Any margin or
padding not on this scale is a bug. (6 was always sanctioned for
within-group gaps; 12 joined it in v2 for the title-to-content gap.)

| What | Token | Value |
|---|---|---|
| Card padding | `--v2-card-pad` | 16px |
| Card padding, tight (scope/meter) | `--v2-card-pad-tight` | 16px |
| Gap between cards | `--v2-card-gap` | 16px |
| Gap within a control group | `--v2-grid-gap` | 6px |
| Block title to content | `--v2-title-mb` | 12px |

Control sizes, from the measured "recipe G" pass (see `ui-redesign/reports/stage3-plan.md`):

| What | Token | Value |
|---|---|---|
| Button height | `--v2-btn-h` | **44px** |
| Band-grid button height | `--v2-btn-h-band` | **36px** |
| Dense touch row (toggles, chips) | `--v2-touch-row` | 32px |
| Input height | `--v2-input-h` | 32px |
| Slider track / thumb | `--v2-track-h` / `--v2-thumb` | 8px / 22px |
| Card radius | `--v2-radius-card` | 14px |
| Button radius | `--v2-radius-btn` | 8px |
| Pill / inset radius | `--v2-radius-pill` / `--v2-radius-inset` | 6px |
| Control border width | `--v2-border-w` | 1.5px |

The spec asked for 48px buttons. 48 put the left column 36% taller and the
page 16% taller, because BAND alone carries five rows; 44 general with 36
for the band grid lands the page at +6%. Band labels are two or three
characters, which is where the air was. **`--v2-btn-h` is settled at 44 —
Amendment F17, do not revisit.**

---

## 5. Data display

**A quantity appears once per distinct question it answers.**

The frequency currently appears six times in four fonts and four colors. It
should appear four times, because there are exactly four questions:

| Question | Where | Treatment |
|---|---|---|
| Where is the radio tuned? | Hero readout | 56px, `--fg-0` / `--state-fault` |
| What is under my cursor? | Plot cursor line | 12px, `--chrome-muted` |
| What is the frequency scale? | Plot axis ticks | 12px, `--chrome-muted` |
| Let me enter a frequency | TUNING box field, separate from the hero | — |

(Amended 2026-07-14: operator decision to keep frequency entry as its own
field in the TUNING box rather than merging it into the hero VFO — see §8.
Day-to-day band tuning happens on the radio's own knob; the console field
is only for occasional jump-to-frequency, so the SDR#/SDRuno-style
prominence the click-to-edit hero pattern buys doesn't actually matter
here.)

Delete the span-start overlays in the corners of the spectrum and waterfall.
The leftmost axis tick already answers that question.

Frequency is always rendered in tabular mono with consistent digit grouping.
Size encodes importance. Color follows the state rules. It never encodes
which widget the number happens to live in.

---

## 6. Components

Four primitives. **Every control in the app is built from one of them.**
Bespoke markup for individual controls is how the current UI drifted; it is
now prohibited.

### 6.1 `Slider`

```
[ LABEL      ][ ————————●———— ][  VALUE UNIT ]
  72px fixed     flex: 1          64px fixed
  --chrome-label                  --fg-0, right-aligned, tabular
```

- Label is mandatory. A slider with no label is a bug (see: DNR level).
- Value is mandatory, right-aligned, includes its unit.
- Track is `--border`; fill and thumb are `--data-trace`.
- All sliders in a column share one track length. Ragged track lengths in a
  row are a bug (see: SPAN / FLOOR / GAIN / AVG).

### 6.2 `ButtonGroup`

```
[   NAR   ][   WID   ]        <- flex: 1 each, 6px gap, fills container
```

- The group fills the width of its container. Always.
- Members are equal width. Always.
- Selected member: `--state-selected` text and border, `#2A2113` background.
- Unselected: `--chrome-label` text, `--border` border, `--bg-2` background.
- Label text is the **function**, never the state. `NB`, not `NB OFF`.

### 6.3 `MeterRow`

```
[ LABEL           VALUE UNIT ]
[ ——————————————————————————— ]   <- scale, always present
```

- Every meter has a visible scale. An empty track with no scale communicates
  nothing (see: Fwd P, Rev P, SWR, Drive P).
- Fill is `--data-trace`, except when the value is in an alarm band, where it
  is `--state-fault`.
- A meter with no data shows an empty track and an em-dash, not a missing
  element. Absence must be visible.

### 6.4 `FieldGrid`

For forms (Antenna A/B test).

- One aligned label column.
- Uniform input widths within a grid.
- Checkbox and its label are vertically centered and share a type size.

---

## 7. Rules of thumb (the audit checklist)

Run this against every screen. Each line is a violation to be fixed.

1. Does any control lack a visible label?
2. Does any button group fail to fill its container?
3. Are any group members unequal width?
4. Does any button label change to report state?
5. Does any slider lack a right-aligned value with units?
6. Do sliders in the same row have different track lengths?
7. Does any meter lack a scale?
8. Does `--chrome-accent` appear on an interactive control?
9. Does amber appear on a *control* where it does not mean "selected"?
   (The three §2.2 exceptions are not controls.)
10. Does green appear on a *control* where it does not mean "healthy"?
11. Is any quantity displayed more than once for the same question?
12. Is any spacing value off the 4/6/8/12/16/24/32 scale?
13. Is any dropdown unlabeled?
14. Is any numeric readout not tabular-figure mono?
15. Is any text smaller than 12px?
16. Does a readout change width with its value, moving what sits beside it?
    (Reserve width in `ch` for the longest string it can hold.)
17. Does a control's own size depend on its content in a way that can
    change at runtime? A reading growing from `S9` to `S9+10` must not
    resize its card.

---

## 8. Interaction

**Click-to-edit hero — decided against (2026-07-14).** Considered folding
the TUNING box's frequency field into the hero VFO (the SDR#/SDRuno/SmartSDR
pattern), but the operator tunes primarily with the radio's own knob — the
console field is only for occasional jump-to-frequency, so the hero pattern's
main benefit (discoverability/prominence) doesn't apply here. Frequency
entry stays where it is, in the TUNING box, permanently.

**Polled fields must yield to the editor.** The rig-state WebSocket handler
must not write to a field that currently has focus. Unconditional writes
from the poll loop overwrite keystrokes and make the field appear dead —
confirmed live 2026-07-14 as a real bug, not hypothetical: the TUNING box's
Tune button appeared to silently fail because the field's live-telemetry
refresh (a `requestAnimationFrame` loop, not just a poll handler) raced the
button's focus-stealing click and clobbered the typed value first. A bare
`document.activeElement === input` check is not sufficient against a
continuously-scheduled refresh — the actual fix used was
`onmousedown="event.preventDefault()"` on the tune button (stops it from
ever taking focus off the field, so the guard holds for the whole
interaction), not the focus/blur-flag pattern below, though that pattern is
still correct guidance for a poll/websocket-driven refresh specifically:

```javascript
let editing = false;
freqInput.addEventListener('focus', () => { editing = true;  });
freqInput.addEventListener('blur',  () => { editing = false; });

function onRigState(state) {
  if (!editing) freqInput.value = formatHz(state.freq_hz);
}
```

**Invalid input flashes `--state-fault` and is not sent.** Never forward an
unparsed value to `rigctld`.

---

## 9. Bottom strip

Three zones, each its own `.section-box` per §2.4 (amended 2026-08-30 —
was header + hairline).

| Zone | Contents |
|---|---|
| TUNING | Tune, Whole Band, Center, Audio |
| DISPLAY | Span, Auto, Floor, Gain, Avg, Palette |
| AUDIO EQ | Bass, Mid, Treble |

Controls within a zone distribute across the available width. Left-packing
with dead space to the right is a bug.

Every dropdown carries a label. `Default` alone tells the operator nothing —
it is a Palette selector and must say so.

---

## 10. Dual-receiver layout (RX1/RX2)

Added 2026-09-11 for the RSPduo swap: a genuine dual-tuner SDR gives two
independent, simultaneous receive antennas on one device (antenna 1 is
fixed as the only TX-capable chain — wiring, not software). The console
runs both receivers side by side, always visible, not as a mode you
switch into.

**Four columns, not three**: `modedsp` (general operating, left) |
`center` (RX1) | `center2` (RX2) | `bandamp` (TX, right). Only the two
outer columns collapse/resize (`COLUMN_CLASS_NAME`/`COLUMN_VAR_NAME`,
generalized from the original 2-column version's ternary logic).

Amended by U2 (2026-09-19): Antenna and Antenna A/B Test moved from
`bandamp` to `modedsp`, which is what makes the right column genuinely
TX-only and the left column "general operational functions" rather than
"whatever wasn't a receiver". Two structural rules came out of that move
and are worth keeping:

- **Don't gate controls by ancestry.** The amp-bypass dimmer targeted
  `.panel-bandamp > .panel-body`, so a box's availability depended on
  which column it happened to sit in. Moving two boxes silently changed
  the behaviour of four others. Gate on what a control actually depends
  on, named explicitly (`#box-amp`, `#box-opmode`), not on where it lives.
- **A control that is unavailable must say so at the control.** Dimming
  without explanation and erroring into a log 40 lines away are both
  failures of the same rule; see `.rx-inert` for the pattern that works.

The side columns' default widths live in **four** places — three
`grid-template-columns` rules and `COLUMN_WIDTH_DEFAULT` in JS. Nothing
enforces agreement; change them together or double-click-to-reset snaps
to a width the CSS disagrees with.

**RX1 and RX2 are each fully self-contained** — own VFO, S-meter, AF/RF
gain, Mode, a merged Spectrum+Waterfall+Tuning box, and a merged
Filter+AGC/NR+Audio EQ box (Filter and AGC/NR side by side within it).
Hiding RX2's column must leave RX1 completely operable — nothing RX-
specific may leak into the other RX's column or into the global/TX
columns. This was corrected mid-build: an earlier 5-column version gave
RX2 its own small DSP column that sat visually between RX1's controls and
RX1's own spectrum, reading as if it belonged to RX1.

**Alignment is pixel-exact, not "close"**: RX1's and RX2's spectrum/
waterfall/filter/AGC rows must land on the same pixel row, verified with
`getBoundingClientRect()`, not eyeballed. Two techniques, chosen by
what's actually asymmetric:
- A row whose height differs because of a content difference that never
  changes at runtime (RX1's `.smeter-readout` has an S9 Cal input RX2
  doesn't; RX1's Mode grid briefly had more rows than RX2's) — pin an
  explicit `min-height` on both sides, sized to the taller one.
- A row whose height differs because it can *wrap* differently at
  different widths (RX2's Tuning row has 2 more buttons than RX1's, for
  Link/Copy) — a min-height guess breaks again the moment either row
  wraps to a different line count than assumed (confirmed live: RX1
  itself wraps at some widths too). Use `flex-wrap: nowrap; overflow-x:
  auto` instead — constant one-line height always, scrolling instead of
  wrapping in the rare case content doesn't fit.

**Link vs. Copy** (RX2 Tuning box): "Link" is continuous — RX2 follows
RX1's frequency/mode/filter width live, using the same margin-gated
recenter RX1 already uses to follow its own CAT frequency (don't retune
hardware or pan the view on every tick, only once the tuned point
actually drifts out of the current view). "Copy" is the one-shot version
— Terry's own framing: "exactly what the radio does with the A=B knob."
Copy (and Link's own first application) also mirrors RX1's *current view*
(span and center), not just what it's tuned to — deliberately not the
same thing, since RX1's own view can be centered somewhere other than its
own dial frequency (e.g. right after Whole Band, which centers on the
band midpoint).

**RX2 has no CAT** — its "ground truth" is whichever of (a) the operator's
own tuning or (b) Link makes it, tracked entirely client-side. `Panadapter2`
is deliberately fresh code, not a refactor of `Panadapter` — RX1's module
turned out to be too deeply woven around following the rig's own CAT
frequency/mode to safely share. Startup default: Link to RX1 is ON: RX2
also independently reaches Whole Band on startup the same way RX1 does
(once its own first frame arrives) rather than trying to copy RX1's span
at a fixed moment, which raced against RX1's own async startup snap and
wasn't reliable.

---

## 11. Known defects

**Closed by the v2 redesign (stages 0–6b, 2026-09-20):**

- ~~`VOL 400%` — likely a scaling bug~~ — it was real. The default was
  `manual_gain = 4.0` and it rode the limiter; now 1.0, on an audio taper.
  See §12.4, and note what else that gain feeds.
- ~~Frequency entry field is inert~~ — fixed; `onmousedown` preventDefault
  on the Tune button, see §8.
- ~~`ATT` has a value but no control~~ — IPO/A1/A2 and OFF/12 are controls
  in the merged FT-991A RX group.
- ~~`S9 Cal` is crammed into a corner with no label treatment~~ — labelled,
  72px, on **both** receivers, and persisted. §12.2.
- ~~EQ bands are `B` / `M` / `T`~~ — Bass / Mid / Treble.
- ~~Antenna unavailability on 2m/70cm is only reported in SYSTEM MSGS~~ —
  `.rx-inert` with an explanatory title, gated on `amp_in_path`.
- ~~Waterfall canvases lack `willReadFrequently`~~ — both have it.
- ~~Text clipped at the right window edge~~ — no overflow at 480/700/960
  panel widths on either receiver, measured. The page still grows past the
  viewport, which is the separate item below.

**Still open:**

- `.rx-inert` on the FT-991A RX group is **static**, not conditional.
  Preamp/ATT/NB/DNF are permanently dimmed because the radio's receiver is
  never in the RF path under the SDR Switch wiring. Correct today, but it
  is a hard-coded assumption about the station's cabling, not a computed
  state.
- The antenna tiles are indicators with no keyboard path at all. Correct
  for cycle-only hardware, but NEXT ANT is then the only focusable control
  in that card.
- **The centre column is ~2500px tall.** The receiver panels do not fit on
  one screen at most window heights. See the viewport-height item below.

Found during U2 live testing (2026-09-19), deferred to U3 — full detail
and file/line evidence in `IMPLEMENTATION_PLAN_U2.md` §6b. Two of that
list were closed by the v2 work and have moved up:

- **No SDR audio on 2m/70cm in any mode**, though the capture is fine
  (spectrum and waterfall work). Cause unknown. Note `sdr/audio_demod.py`
  implements SSB only — there is no FM demodulator — but USB on 2m is
  silent too, so that is not the explanation.
- **TX BW is offered in modes the rig rejects** — menu 110 is SSB-only, so
  clicking it in FM/DATA errors. Gate on rig mode.
- **The waterfall blanks in digital modes when zoomed out** past the
  3 kHz fine frame, instead of falling back to the wide frame.
- **The page grows instead of fitting the viewport.** `scrollbar-gutter:
  stable` stops the resulting layout shift, but SYSTEM MSGS still scrolls
  off-screen — and per finding B that bar is currently the only place
  errors surface at all. A viewport-height console with per-column
  scrolling fixes both; it touches `.scope-group` sizing and therefore the
  canvas `getBoundingClientRect()` paths, so it needs its own package.

Added later the same day, from a log capture taken while testing the TX BW
finding — these two are not UI defects and are a priority ahead of U3:

- **The rig reply stream desyncs and force-reconnects every ~15 s.** A
  `t` (PTT) read times out, the 1 s stale-reply drain is too short for
  the ~4 s reply latency actually present, the late `t` reply is read as
  the frequency (`"0"`), the sanity check fires and forces a reconnect —
  16 times in an 11-minute capture, leaving the console unable to send
  anything for ~12 % of the session. **This is the real cause of the TX
  BW non-determinism**, and `<cmd> rejected by rig` is a false
  attribution: the rig never rejected anything. Prime suspect for the
  latency is the fast-PTT watchdog polling `t` every 5 ms on its own
  connection into a serialized rigctld.
- **A desynced reply read as PTT fakes a whole transmit state**
  (**partly closed 2026-09-19**: the `t` reply is now validated against
  Hamlib's `[0-3]` PTT enum and an unparseable reply holds the previous
  state instead of faking TX. A bare single-digit straggler from
  `u NB`/`u NR`/`u ANF`/`s` is still indistinguishable from real PTT —
  closing that needs the fast-PTT watchdog cross-check, see
  IMPLEMENTATION_PLAN_U2.md §6b finding I) —
  audio gated, spectrum frozen, TX meters showing the radio's power
  *setting* as 100 W output, a TX start pushed to the amp bridge, and
  nothing in SYSTEM MSGS. Proven against the ACOM's own KEY-IN flag and
  forward power, both flat at zero. Worse than the desync itself, because
  `f` is only polled while PTT is false, so a phantom PTT switches off the
  frequency sanity check — the only other guard there is.
- **The 2.0 s read timeout is shorter than the ~4.0 s actual reply
  latency**, so during a latency episode every command fails before it is
  sent. Measured across 12 stragglers: median 4.01 s, 12/12 unanswerable.
  The tight clustering (4.00-4.04) points at a fixed retry below us, not
  congestion. This is the real next diagnostic target — everything else
  here is downstream of it.
- **Audio queue drops are bursty, not sustained**, and anti-correlate
  with the rig desyncs — so the two are separate problems, and DSP load
  starving the event loop is ruled out as a unifying cause. This is the
  already-open 2026-09-13 tail-latency finding with better data; the
  cumulative counter makes it look worse than the deltas show.

Reported by the operator 2026-09-19 while checkpointing U3a. Neither is a
U3a regression; both are pre-existing. Detail in
`IMPLEMENTATION_PLAN_U2.md` §6b, findings K and L:

- **The SSB session sets USB on every band.** On 40m it should be LSB
  (as on 160m and 80m). Not a bug in the switch path — the path has no
  band input: `SessionProfile.rig_mode` is one fixed string and
  `SessionManager` applies it verbatim. `ssb` is the only profile whose
  correct mode depends on frequency, which is why the data model never
  had to express it. The fix is a decision, not a patch: derive the
  sideband from frequency, or stop asserting one and leave it to RX1's
  mode grid.
- **After a server restart the Session box shows no session**, while the
  radio is still in whatever mode it was left in. A browser reload is
  fine — the client re-requests `session_status` on connect. Not
  persisting the session to disk is deliberate and correct, but "don't
  remember it" and "show nothing" are separate decisions and only the
  first was argued: rig mode plus the existing WSJT-X/JS8Call liveness
  probes can *derive* it live. Display only — deriving it must never
  drive the radio (§ the no-auto-override rule).

---

## 12. The v2 layout (2026-09-20)

What the console is now, block by block. Built in stages 0–6b; the
per-stage reports are in `ui-redesign/reports/`, the decisions that
overrode the original spec in `ui-redesign/SPEC_AMENDMENTS.md`.

### 12.1 Column order

| Column | Contents, top to bottom |
|---|---|
| **Left** (`modedsp`, 288px) | SESSION · BAND · DIGITAL AUDIO · SSB AUDIO · ANTENNA |
| **Centre** (`center`, `center2`) | RX1 and RX2, each self-contained (§10) |
| **Right** (`bandamp`, 274px) | AMP · TX METERS · EXCITER DRIVE · OPERATING MODE · FAULT STATUS |
| **Tray** (full width) | ANTENNA A/B TEST · MEASURE NOISE |

SSB Audio moved left in stage 3b: mic, comp and TX BW are operating
controls, and the right column is now genuinely TX *telemetry* plus the
two TX-safety controls. The centre column is by a wide margin the tallest
and is what sets page height — which is why `--v2-btn-h` in the side
columns costs nothing (§4).

### 12.2 Receiver panel — top row

Frequency card left, analog meter card right, **side by side only while
the digits fit at their full 76px**; below about 646px of panel content
they stack rather than the readout shrinking. Measured flip: between a
692px and a 700px panel.

- **Frequency card**: "FREQUENCY" label, receiver chip (`RX1`/`RX2`,
  identical styling — only the label differs, so a screenshot of one side
  is identifiable), the hero readout, then the existing state pills.
  Group separators are dim `--v2-notch-fg` and 0.3em wide.
- **Meter card**: the S-meter (§12.3), then the live dBFS and S reading,
  a peak line, and S9 CAL.

**The readout must never reflow.** Width is reserved in `ch` for the
longest string each field can hold (`-160.0 dBFS`, `S9+60`), both lines
have a fixed height derived from one font variable, and figures are
tabular. A reading growing from `S9` to `S9+10` once made the meter card
taller and pushed the spectrum down the page; that is the bug rule 17 in
§7 exists for.

### 12.3 The analog S-meter

Inline SVG, `viewBox 0 0 460 240`, generated by
`ui-redesign/build_meter_svg.py`. Arc centre (230, 330), radius 270,
sweeping −38° to +38° from vertical. Paper face only — `#f2efe6` with
`#15181d` ink, `#d0402b` red and a `#1f80c4` peak marker. The backlit
theme in the spec was not built.

- **Scale**: S1→S9 is the first **58%** of the arc, linear in S units;
  S9→+60 is the last **42%**, linear in dB. Major ticks at S1 S3 S5 S7 S9
  and +10 +30 +60; minor at the rest. The over-S9 labels sit *outside* the
  arc, which is both what the mockup shows and what keeps them clear of
  the peak marker.
- **S-unit maths**, with calibration `cal` (dBFS at S9):
  `s = 9 + (dBFS − cal) / 6`. Below S9: `f = (max(s,1) − 1) / 8 × 0.58`.
  At or above: `f = 0.58 + min(dBFS − cal, 60) / 60 × 0.42`.
  Needle angle `= −38 + 76f`. Pure functions `v2MeterFraction` /
  `v2MeterAngle`, unit-tested.
- **Peak hold**: a reading at or above the current peak sets it and
  restarts a **6 s** hold; after that it decays at **3 dB/s** toward the
  live level and never falls below it. Timestamps, not frame counts, and
  the decay is incremental from the later of the previous step and the end
  of the hold — decaying the already-decayed value by total elapsed time
  compounds, and at a 10 Hz push rate that is 30 dB/s, not 3.
- **The text label keeps 10 dB rounding above S9** (`S9+10`, `S9+20`) while
  the needle moves continuously. Deliberate: that is how a real meter is
  read. Amendment D1.
- **S9 CAL** exists on both receivers and persists —
  `localStorage['s9Cal:rx1'|'s9Cal:rx2']`. Before v2 it reset to −75 every
  reload and RX2 had no control at all.

The old linear bar still exists and is still updated; it is only hidden.

### 12.4 Receiver panel — gains and mode row

Gains card left (AF GAIN over RF GAIN), mode card right, stacking below
660px of panel.

- **AF GAIN** is an **audio taper**: the slider is a *position* 0–1000 and
  `gain% = 1000 × (pos/1000)²`, inverse `pos = 1000 × √(gain/1000)`. 100%
  therefore sits about a third along instead of a tenth, and the default
  notch marks it (`data-v2-notch="0.3162"`). Pure functions
  `v2AfPosToGain` / `v2AfGainToPos`, unit-tested including a round trip
  within 1 unit at all 1001 positions.
- **The AF GAIN default is 100%** (`manual_gain = 1.0`,
  `sdr/audio_demod.py`). It was 400%, which rode the `np.clip(±0.95)`
  limiter constantly. **This gain is not only the operator's headphones:**
  `_publish` hands the same bytes to the browser *and* to
  `DigitalAudioOutput → BlackHole → WSJT-X`, so it sets the decoders'
  input level one for one. Change it deliberately or not at all.
- **RF GAIN** is a 7-position discrete LNA control, labelled "N dB atten",
  running *backwards* (`HF_LNA_GR_DB = [0,6,12,18,37,42,61]`). Not a
  continuous 0–100 knob; presenting it as one produced dead zones and a
  sudden 10 dB jump.
- **MODE** is a 3×2 grid on both receivers. RX2 demodulates SSB only, so
  its CW/AM/FM/DATA-U are rendered **disabled rather than hidden**, so the
  two grids align. Those four carry **no `data-mode`**, because `updateUI`
  sweeps `querySelectorAll('[data-mode]')` page-wide and reassigns
  `className` — a `data-mode` there would light amber whenever the *rig's*
  mode matched, which has nothing to do with RX2's sideband.

### 12.5 Spectrum, waterfall and their controls

The scope card keeps **today's canvas sizes to the pixel**. Its inset is
border + padding together: the v1 card was 2px + 8px, the v2 hairline is
1px, so the padding is **9px** horizontally to keep the same 10px. Do not
"tidy" that back to 8.

- **Twelve palettes**: the original five (`default`, `linrad`,
  `grayscale`, `hot`, `gqrx`) are preserved **stop for stop** — `default`
  must keep reproducing the waterfall exactly as it was — plus Turbo,
  Viridis, Inferno, Magma, Plasma, Amber phosphor, Green phosphor.
  `[pos,r,g,b]` stops, `dbToColor` and the `wfContrast` 1.5 gamma are
  unchanged. Applies to new rows; history is not redrawn.
- **Six trace colours**: ice blue (the default, today's exact `#67D0F0`
  with a 0.25 fill alpha), cyan, green, amber, magenta, white.
- Both persist per receiver: `wfPalette:rx1|rx2`,
  `spectrumTrace:rx1|rx2`, validated against the known names at load.
- Each selector carries a live preview — the gradient strip is built from
  the palette's own stops, so it cannot drift from what is painted.
- **Controls block**: a two-column grid of SPAN, FLOOR, GAIN, AVG, then a
  divider, then the two pickers. **AUTO lives in the block's title row**,
  not in GAIN's row: it gates GAIN *and* FLOOR, and sitting between GAIN's
  label and its readout it read as part of that one row.
- **AVG defaults to 30%** on both receivers, from one constant behind the
  markup value and both derived floats.

### 12.6 Tuning, filter / AGC-NR, audio EQ

Tuning wraps rather than overflowing — RX2's LNK and CPY used to be
clipped off the end of the row. Filter and AGC/NR are one card in two
columns with a real divider; all three cells are placed **explicitly**,
because a `::before` divider is the first grid item in source order and
auto-placement puts the columns in the wrong cells. The NR slider is
labelled **"NR STRENGTH"** (1–15 → 6–40 dB DeepFilterNet attenuation
limit) — §6.1 has called an unlabelled slider a bug since before this
redesign. EQ bands are v2 ranges with a centre notch.

### 12.7 The tools tray

A full-width grid row between the main row and the system bar, **in normal
flow — never fixed, sticky or overlaid**. Collapsed height is 88px: a
44px tab row with 22px padding. Tabs are real `role="tab"` buttons with
`aria-selected` and `aria-controls`; HIDE/SHOW carries `aria-expanded`;
clicking the active tab collapses. State persists in `trayCollapsed` and
`traySelectedTab`, defaulting to **collapsed on the A/B tab**.

**Opening or closing the tray must not change the canvas sizes.** It does
not, because a full-width row changes row heights only and the column
tracks never move — and because `html { scrollbar-gutter: stable }`
reserves the scrollbar gutter, so a scrollbar appearing cannot steal 15px
and reflow every column. **That rule is load-bearing; do not remove it.**

Each tab carries a status dot that goes `--v2-ok` while its tool runs, so
a running test is visible with the tray shut. For the A/B test "running"
includes `awaiting_antenna` — a manual-switch pause is still a run.

### 12.8 The page shell

Top bar 56px on `--v2-bg-chrome` with a hairline bottom edge: wordmark,
the four status items as dot **plus label** (colour is never the only
signal — the label changes colour with the dot), and the five tool links
as 40px bordered buttons. System bar 44px, same chrome. The rails take the
chrome colour with a visible grip; the resize handle is **8px** so it can
be caught with a mouse.

### 12.9 The wheel

**The wheel adjusts every slider, with no modifier** (Amendment F18). The
guard is *rest*, not a key: a wheel event is ignored within **250 ms of a
page scroll** or **120 ms of the pointer arriving** on the control, and in
that case the handler returns **without** `preventDefault`, so the page
keeps scrolling. Point at a slider, pause, roll: always works. Scroll past
one: never fires.

This replaced a Shift-only rule that was briefly shipped and was wrong —
the operator uses the wheel for precision. The accident being guarded
against is real: AVG once drifted to 39% because the pointer sat over a
slider during a page scroll.

`v2WheelShouldAdjust` is pure and unit-tested at both window boundaries.
The spectrum's own wheel-zoom is a separate handler and is not gated.
