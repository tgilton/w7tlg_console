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

Amber means *selected*, everywhere in the app, with no exceptions. Band,
mode, antenna, AGC, filter, preamp — one rule, learned once.

### 2.3 Data — measured values only.

| Token | Hex | Use |
|---|---|---|
| `--data-trace` | `#67D0F0` | Spectrum trace, slider fill, meter fill |
| waterfall | viridis | Unchanged |

### 2.4 Grouping is structural, not chromatic

Group identity is carried by **proximity, dividers, and column rules** —
never by tinting the controls inside the group. Two column rules only:

- Left column (rig + DSP): 2px `--chrome-accent` left rule
- Right column (amplifier + antenna): 2px `--chrome-accent` left rule

Sections *within* a column separate with a `0.5px --border` hairline and a
`--chrome-accent` header. No further hue differentiation. If you find
yourself wanting a third accent color to distinguish a group, the group
needs a divider, not a color.

### 2.5 TX state

The VFO readout reports transmit state and is the primary TX indicator.

- **RX**: `--fg-0` (near-white). The frequency is a fact, not a status.
- **TX**: `--state-fault` (red).

TX is never signalled by color alone. During TX, additionally render a 2px
`--state-fault` border on the console shell. Color plus geometry — this is
the one state where being misread is expensive.

---

## 3. Typography

Monospace throughout, tabular figures on every numeric readout
(`font-variant-numeric: tabular-nums`) so digits do not jitter as values
update.

Three sizes, with real separation between them. Timid scales read as mush.

| Role | Size | Weight |
|---|---|---|
| Hero (VFO) | 56px | 500 |
| Section header | 12px, `letter-spacing: 0.1em` | 400 |
| Everything else | 12px | 400 |

Section headers and control/meter labels (VOL, DT GAIN, ALC, Fwd P, SWR,
etc.) are uppercase. Values, readouts, and descriptive text (antenna
aliases, system messages) are not — uppercase marks "what is this control,"
never "what is its value." (Amended 2026-07-14; originally read "Section
headers are the only uppercase text in the app," but labels had already
drifted uppercase in practice before this doc caught up.)

---

## 4. Spacing

Scale: **4 / 8 / 16 / 24 / 32**. No other values. Any margin or padding not
on this scale is a bug.

- Within a control group: 6px
- Between controls: 16px
- Between sections: 24px + hairline divider
- Panel padding: 16px

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
9. Does amber appear anywhere it does not mean "selected"?
10. Does green appear anywhere it does not mean "healthy"?
11. Is any quantity displayed more than once for the same question?
12. Is any spacing value off the 4/8/16/24/32 scale?
13. Is any dropdown unlabeled?
14. Is any numeric readout not tabular-figure mono?

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

Three zones, each with a `--chrome-accent` header and a hairline between.
They are currently three unrelated rows sharing a space.

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

## 10. Known defects (as of this writing)

- `VOL 400%` — likely a scaling bug, not a styling issue. Verify.
- Text clipped at the right window edge — layout overflow. Verify.
- Frequency entry field is inert — see §8.
- `ATT` has a value (`OFF`) but no control.
- `S9 Cal` is crammed into a corner with no label treatment.
- EQ bands are `B` / `M` / `T` — expand to Bass / Mid / Treble.
