# W7TLG Console — UI Redesign Handoff (Claude Code)

Status: design approved by Terry on 2026-09-20. This document is the spec. The interactive mockup was only a visual reference; rebuild it in the console's own front-end, do not port mockup code.

## 0. Ground rules

1. **Presentational change only.** Keep every existing element ID, event handler, WebSocket message and backend call. Change markup, CSS and layout. If a control needs a new behavior (listed in section 4), add it as a small, isolated change and call it out in the commit message.
2. **Do not touch TX-safety logic.** The TX-gating audit work (PTT validation, AMP ON drive cap, multi-source TX check, hang time) stays exactly as it is. The amp and exciter blocks are restyled only.
3. **Branch from `ui-redesign`.** Its tip was `cf0f4fe` on 2026-09-19; verify before branching. Commit per stage (section 6) so each stage can be viewed in the browser and reverted alone.
4. **Fix the stale FT8 session bug first**, before restyling SESSION: after WSJT-X exits (Cmd-Q), the FT8 button stays lit and clicking it reports "Already in FT8 (WSJT-X) session" without relaunching. Launching WSJT-X by hand works.
5. **Design px are a proportion spec.** The mockup page is 2720 × 2088 px, roughly 1.2× the size of the current UI. Implement with CSS variables or rem so the whole page can scale uniformly (see open question Q1).
6. Claude Code runs the tests; do not ask Terry to.

Reference images: Terry should export a PNG of each mockup board into `docs/ui-redesign/` (Full page, Receiver panel, Receiver column, Left column, Right column, Tray). Treat those as the visual truth where this text is ambiguous.

## 1. Page layout (left to right)

| Element | Width | Notes |
|---|---|---|
| Left column | 388 | Cards 340 wide, 24 px page padding, 16 px gap between cards |
| Left rail | 32 | Collapse chevron, 1 px borders |
| RX1 column | 940 | Content width 892 (24 px padding) |
| RX2 column | 940 | 1 px left divider |
| Right rail | 32 | Collapse chevron |
| Right column | 388 | Same card rules as left |

Vertical: top bar 56, main area, collapsed tools tray strip 88 (open tray is taller, see 2.7), system messages bar 44.

Left/right columns collapse independently from the rails; persist the state (reuse the existing persistence mechanism if there is one).

Top bar: "W7TLG CONSOLE" wordmark (letter-spaced, `#8d93d6`), status dots with text labels (Rig, Amp, SDR green `#4fd07a`; WSJT-X red `#e8583d` when not running; color must never be the only signal, keep the text), and on the right the five existing tool links (Monitor, Propagation, Spot/Seek, Advisor, Diversity) as bordered buttons with an up-right arrow icon. Keep existing link targets.

System messages bar: label "SYSTEM MSGS" plus the existing message text ("No messages" when empty).

## 2. Blocks

Every block is a card: background `#121a25`, 1 px border `#1f2b3b`, radius 14, padding 20 (16 for the spectrum card and the meter card). Titles are centered, 14 px, weight 500, letter-spacing 0.2em, color `#9aa1e0`. Small field labels are 12 px, weight 500, letter-spacing 0.16em, color `#8794a7`.

### 2.1 Left column, top to bottom

1. **SESSION**: SSB, FT8 (WSJT-X), JS8Call. 2-column grid, 48 px buttons.
2. **BAND**: 3-column grid, 160 80 60 / 40 30 20 / 17 15 12 / 10 6. Divider, small centered label "VHF / UHF — DIRECT TO RIG", then 2m and 70cm in a 2-column grid. One shared selection across all of them.
3. **DIGITAL AUDIO**: DT GAIN slider (0–100, shows the number), SOURCE (RX1 / RX2). Divider. One group labeled "FT-991A RX" containing NB, DNF (2-column toggles), PREAMP (IPO / A1 / A2) and ATT (OFF / 12). The whole group is dimmed and disabled under the same condition as today (the current UI shows it dimmed when the SDR is the digital audio source); keep the existing enable logic, do not invent new rules. The old UI showed the "FT-991A RX" chip twice; the redesign shows it once.
4. **SSB AUDIO**: MIC slider (0–100%), COMP slider (0–100%), divider, TX BW as one horizontal row of three buttons (WIDE, RAG-CHEW, DX). This block moved here from the right column.
5. **ANTENNA**: 2×2 tiles (A1F "SS-25 / DXF Vertical", A2F "Unconnected" disabled, A3R "40m EFHW Multiband", A4R "Dummy Load"), then a full-width NEXT ANT button with a circular-arrow icon. NEXT ANT skips unavailable antennas. Tile text comes from the existing antenna config, not hard-coded.

ANTENNA A/B TEST and MEASURE NOISE no longer live in this column; they move to the tools tray (2.7).

### 2.2 Receiver panel (one per receiver, top of each center column)

Panel is 940 × 560 with 24 px padding. Top row 320 px tall, two cards 438 wide with a 16 px gap; bottom row 176 px tall, two cards 438 wide.

**Frequency card (top left)**
- Header row: small label "FREQUENCY" on the left, receiver chip on the right. The chip reads "RX1" or "RX2" (label only, identical styling for both): background `#8cc8ee`, text `#0b1017`, 18 px bold, letter-spacing 0.08em, padding 4 × 12, radius 8. Purpose: a screenshot of one side must be identifiable.
- Digits: JetBrains Mono, 76 px, weight 500, `#eef2f8`, format `7.074.000`. The dot separators are dim (`#5d6c82`) and narrower (`0.3em` wide, centered).
- Status pills under the digits, wrapping, 13 px bold, padding 6 × 10, radius 6. Amber pills (`#3a2a10` bg, `#f0b04a` text): band, mode. Green pills (`#16301f` bg, `#7fd68f` text): RX, FT8, DIGI AUDIO. Warning pill (SPLIT) is amber with a triangle icon. RX1 shows: 40m, PKTUSB, RX, SPLIT (only when split is active), FT8, DIGI AUDIO. RX2 shows: ANT B, USB. Drive these from the existing state flags; the set of pills is whatever the current UI shows.

**Meter card (top right)**
- Analog S-meter face, 406 × 212 px, radius 10, inset shadow. SVG viewBox 460 × 240. Arc center (230, 330), radius 270, sweeping −38° to +38° from vertical (total 76°).
- Scale: S1 → S9 occupies the first 58% of the arc, linear in S-units (ticks every S unit, major at S1 S3 S5 S7 S9; S1 is labeled "S"). S9 → +60 dB occupies the last 42%, linear in dB (major ticks at +10, +30, +60; minor at +20, +40, +50). The S9→+60 segment, its ticks and the arc band are red. Labels: S, 3, 5, 7, 9, +10, +30, +60 (Barlow Semi Condensed 700, 30 units in viewBox). "SIGNAL" text centered low on the face (weight 800, letter-spacing 3).
- Face themes: `paper` (face `#f2efe6`, ink `#15181d`, red `#d0402b`, needle `#15181d`, peak marker `#1f80c4`) is the default. `backlit` (face `#171d27`, ink `#f1e3b8`, red `#ff6a4d`, needle `#ff9a3d`, peak `#8cc8ee`) is an optional theme for a dark shack; expose it as a setting but ship `paper` as default.
- Needle: a 3 px line rotating about (230, 330); transition 0.15 s ease-out. Update rate 10 Hz.
- **S-unit math.** With calibration `cal` (dBFS at S9; default −75, user-editable in the "S9 CAL" field): `s = 9 + (dBFS − cal) / 6`. If `s < 9`: fraction `f = (max(s,1) − 1) / 8 × 0.58`, label `S` + floor(s) (S0 below S1). If `s ≥ 9`: `over = dBFS − cal`, `f = 0.58 + min(over, 60) / 60 × 0.42`, label `S9` or `S9+N` (N = round(over)). Needle angle = −38° + 76° × f.
- **Peak marker** (new behavior): a small filled triangle just inside the arc, pointing outward, behind the needle, using the same fraction mapping as the needle. Rules: any sample at or above the current peak sets the peak and restarts the hold timer; the peak holds for 6 s (configurable 1–20 s); after the hold it decays at 3 dB/s (0.3 dB per 100 ms tick) toward the live level and never goes below it. Marker transition 0.12 s linear.
- Readout row under the face: live dBFS in 32 px `#8cc8ee` with a small "dBFS" unit, then the S reading (26 px bold, same blue). Second line: triangle icon, "PEAK", peak dBFS, "dBFS", peak S reading. On the right: label "S9 CAL" and a 64 px input (keep the existing input and its persistence).

**Gains card (bottom left)**
- AF GAIN and RF GAIN stacked vertically, each a label row (label left, value right in `#8cc8ee`) above a custom slider.
- **AF GAIN: range 0–1000%, default 100%** (the old default of 400% was too loud; Terry almost always lowered it to about 100%). Slider uses an audio taper so 100% sits about a third of the way along: `gain% = 1000 × x²` where `x` is slider position 0–1; inverse `x = sqrt(gain / 1000)`. Draw a small notch on the track at the default position. A "linear" taper option exists in the mockup (100% at 10% of travel); ship the quadratic taper and keep the option as a setting only if it is cheap. **Check where the 400% default currently lives (front end, backend, or a saved user setting) and migrate it, including any persisted value, so the new default actually takes effect.**
- **RF GAIN**: shown as "N dB atten", range 0–36 (assumed, see Q2), default 18.
- Slider look: track 8 px `#243044`, fill `#8cc8ee`, thumb 22 px `#eef4fa` with a 3 px `#8cc8ee` border. Fill width is `calc(11px + (100% − 22px) × fraction)` so it ends under the thumb center.

**Mode card (bottom right)**: title "MODE", 3 × 2 grid: USB, LSB, CW / AM, FM, DATA-U. 48 px buttons. Active button amber. On RX2 only USB and LSB are enabled today; render CW, AM, FM and DATA-U disabled (dim) rather than hidden so the two receivers align. When RX2 gains more modes, enable them there. RX1 defaults to DATA-U in the mockup only because that is the current state.

### 2.3 Spectrum and waterfall (per receiver, under the receiver panel)

Card padding 16. Inside, top to bottom: spectrum canvas (about 858 × 198), waterfall canvas (858 × 250), then a frequency axis row with 7 evenly spaced labels (7.0740, 7.0745, … 7.0770 for a 3 kHz span; derive from actual span and center). Keep the existing rendering, cursor line (dashed, label with the cursor frequency at top) and interaction. Only the following change:

- **AVG default is 30%** on both receivers (RX1 was 39%). Keep the existing averaging algorithm and meaning; only the default changes. Check whether a saved value overrides it.
- **New: WF PALETTE selector** with these palettes (color stops evenly spaced, interpolated linearly): 
  - Default: `#02030c #050a3a #0a1f9a #1568d8 #12b8c8 #22d47a #a6e63a #f5d21f #f58a1a #e02a12 #fff0e0` (this should reproduce today's look; if today's waterfall differs, keep today's colors as "Default" and match the stops to it)
  - Turbo: `#30123b #4145ab #4675ed #39a2fc #1bcfd4 #24eca6 #61fc6c #a4fc3b #d1e834 #f3c63a #fe9b2d #f36315 #d93806 #b11901 #7a0402`
  - Viridis: `#440154 #482878 #3e4a89 #31688e #26828e #1f9e89 #35b779 #6ece58 #b5de2b #fde725`
  - Inferno: `#000004 #1b0c41 #4a0c6b #781c6d #a52c60 #cf4446 #ed6925 #fb9b06 #f7d13d #fcffa4`
  - Magma: `#000004 #180f3d #440f76 #721f81 #9e2f7f #cd4071 #f1605d #fd9668 #feca8d #fcfdbf`
  - Plasma: `#0d0887 #47039f #7301a8 #9c179e #bd3786 #d8576b #ed7953 #fb9f3a #fdca26 #f0f921`
  - Amber phosphor: `#050300 #3a2500 #8a5a00 #e0a020 #fff0b0`
  - Green phosphor: `#000a02 #04440f #0f9a2c #33ff66 #e6ffee`
  - Grayscale: `#000000 #ffffff`
  Implement as a 256-entry lookup table applied when the waterfall row is painted (the mockup fakes this with an SVG color filter over a grayscale image; do not copy that). Redrawing history on palette change is nice to have; at minimum apply to new rows. Show a small gradient strip next to the dropdown as a preview.
- **New: TRACE color selector** for the spectrum line and its fill: Ice blue `#7fb6d1` (today's look, default), Cyan `#4fd8e8`, Green `#6fe08a`, Amber `#f0b04a`, Magenta `#e06fd0`, White `#e8edf5`. Line 1.6 px, fill the same color at 22% opacity. Swatch next to the dropdown.
- Persist palette and trace choices per receiver.

### 2.4 Spectrum and waterfall controls (per receiver)

Title "RX1 SPECTRUM & WATERFALL" (RX2 likewise). Two-column grid of slider fields: SPAN (shown as kHz), FLOOR (dB), GAIN (%, with an AUTO checkbox in its header), AVG (%). A divider, then WF PALETTE and TRACE dropdowns side by side. AUTO disables GAIN and FLOOR (as the current UI dims them). In the current UI RX2's SPAN is disabled; preserve whatever rule drives that today. Slider ranges in the mockup are placeholders (Q2); use the real ranges from the existing controls.

### 2.5 Tuning, filter, AGC/NR, audio EQ (per receiver)

Layout changes only, behavior unchanged.
- **RX_ TUNING**: frequency text input (190 px) followed by TUN, BAND, CTR, AUD (RX2 also LNK, CPY). Toggle buttons (AUD, LNK) go amber when on. Keep today's enable and disable rules (RX1's BAND and RX2's TUN, and the frequency input when LNK is on, are disabled in the current UI).
- **RX_ FILTER | RX_ AGC / NR**: one card, two columns split by a vertical divider. Left: width slider (shown as Hz, e.g. "3000Hz") and NARROW / WIDE buttons. Right: an unlabeled slider in the current UI (the mockup labels it "NR LEVEL"; confirm, Q3), DNR / RF NOTCH / DAB NOTCH toggles in a 3-column grid, and an AGC row with OFF / FAST / SLOW.
- **RX_ AUDIO EQ**: EQ on/off button, then BASS, MID, TREBLE sliders with a center notch and numeric readout (the mockup uses ±12; use the real range). When EQ is off the sliders are dimmed.

### 2.6 Right column (transmit), top to bottom

1. **AMP (ACOM 1200S)**: OPR/RX status pill (green outline), then **FWD P** as the hero readout: label "FWD P", the number centered at 60 px (`#7fd68f`) with a 24 px "W", above a **14 px thick** bar (the other bars are 6 px). Below it stacked rows (label left, value right, 6 px bar): REV P, SWR, DRIVE P, PAM1 T. Divider, then a DIAG chip and "HV 51.6V  I 8.8A" (live values). Bar scales in the mockup are guesses matched to the old screenshot: FWD 0–1000 W, REV 0–200 W, SWR 1–3, DRIVE 0–100 W, PAM1 T 0–80 °C. Use the scales the existing bars use.
2. **TX METERS**: ALC, RADIO PO, SWR stacked like the amp rows (label left, value right, 6 px bar). In RX they show a dimmed "—" with empty bars, as today. Keep the title text "TX METERS".
3. **EXCITER DRIVE**: slider (mockup 5–100 W) with the value in green to the right. Keep the existing range and the AMP ON drive-cap behavior untouched.
4. **OPERATING MODE**: AMP OFF and AMP ON in a 2-column grid; AMP ON active in amber.
5. **FAULT STATUS**: title with a small caret icon, then the OK chip (green). Keep the existing expand behavior if the caret is a disclosure today.

### 2.7 Bottom tools tray (full width, hideable)

Purpose: occasional and experimental tools, out of the main workflow. A slim strip stays visible when collapsed (88 px including padding); an opened tray pushes the layout up so the waterfalls stay visible (it must not overlay them).

- Strip: label "TOOLS", one tab per tool (44 px, status dot on the left), and a SHOW/HIDE button with a chevron on the right. Clicking the active tab, or HIDE, collapses. Selected tab is amber.
- Status dot: gray when idle, green (`#4fd07a`) while that tool is running, so a running test is visible even when the tray is closed.
- **ANTENNA A/B TEST** tab: one row of controls (moved from the left column, same fields and behavior): "Manual switching" checkbox; ANTENNAS checkboxes A1F, A3R, A4R (checked chips go amber); ROUNDS (default 10); DWELL S (default 12); MHz range from/to (defaults 14.200 and 14.325); STATUS text (idle / running; the mockup's "running" text is a guess, show whatever the current UI shows during a run); Start button (green `#10261a` bg, `#3fae5a` border, `#7fd68f` text, play icon) and Stop button (red `#2a1210` bg, `#c8442f` border, `#ff7a63` text, square icon).
- **MEASURE NOISE** tab: the existing Measure Noise button plus a RESULT readout (shows "—" until a result exists).
- New tools become new tabs; the layout does not change when one is added (candidates: LLM band advisor panel, the planned QRM Eliminator controls).

## 3. Design tokens

Fonts: JetBrains Mono (400, 500, 700) for everything; Barlow Semi Condensed (600, 700, 800) for the meter face only.

| Token | Value |
|---|---|
| Page background | `#0b1017` |
| Top bar, rails, system bar | `#0d131c` |
| Card | `#121a25`, border `#1f2b3b`, radius 14 |
| Divider | `#1f2b3b` |
| Inset (text inputs, selects) | bg `#0b1017`, border `#2a394d`, radius 6–8 |
| Text primary / muted label / light label | `#e8edf5` / `#8794a7` / `#c3ccda` |
| Block title | `#9aa1e0` |
| Blue readout and slider fill | `#8cc8ee` |
| Slider track / thumb | `#243044` / `#eef4fa` with 3 px `#8cc8ee` border |
| Button (off) | bg `#182131`, border 1.5 px `#26344a`, text `#b3bfcf` |
| Button (selected, amber) | bg `#3a2a10`, border `#f0b04a`, text `#f0b04a` |
| Button (disabled) | bg `#111823`, border `#1c2738`, text `#56627a` |
| Green pill / status | bg `#16301f` (or `#10261a`), text `#7fd68f`, border `#3fae5a` |
| Stop (red) | bg `#2a1210`, border `#c8442f`, text `#ff7a63` |
| Inactive readout | `#6f7b90` |

Sizes: buttons 48 px tall (44 px in dense groups), radius 8; slider track 8 px, thumb 22 px, touch row 32 px; block titles 14 px, small labels 12 px, values 15–17 px. Every interactive element is a real `button`, `input`, `select` or `a` with a visible focus ring (2 px `#f0b04a`, 2 px offset) and an accessible name.

## 4. New behavior summary (everything else is layout)

1. Meter peak hold and decay (2.2).
2. AF GAIN scale 0–1000%, default 100%, audio taper, default notch (2.2).
3. AVG default 30% (2.3).
4. WF PALETTE and TRACE selectors with persistence (2.3).
5. Collapsible side columns and collapsible tools tray, with persisted state (1, 2.7).
6. Tray status dot for running tools (2.7).
7. Analog meter face replaces the linear dBFS bar; the numeric dBFS and S reading remain.

## 5. What the mockup fakes (do not port)

Simulated dBFS level and peak; the static spectrum trace and its AVG smoothing; the grayscale waterfall images and SVG palette filter; the A/B test "running" state; all button states (they are local component state). Wire everything to the existing real data sources.

## 6. Staged plan

Each stage is one commit that runs and can be viewed in the browser. Run the existing tests after every stage.

0. Fix the stale FT8 session bug (rule 4). Branch from `ui-redesign`.
1. **Tokens and shared pieces**: CSS variables, fonts, card, title, button (off/selected/disabled), slider (with fill and optional notch), input, select, pill, chip.
2. **Right column** (least logic; validates the approach). Acceptance: identical behavior to before, FWD P hero readout in place.
3. **Left column and tools tray**: new order, the ANTENNA A/B TEST and MEASURE NOISE move into the tray, session restyle. Acceptance: FT8/WSJT-X session buttons work, including relaunch after Cmd-Q.
4. **Receiver panel**: frequency card with RX chip and pills, S-meter with peak hold, gains card (AF default 100%), mode card. Acceptance: needle and peak track the same dBFS feed the old bar used; S9 CAL still works.
5. **Spectrum, waterfall, controls, tuning, filter/AGC-NR, EQ**: layout, AVG 30%, palettes, trace colors, persistence.
6. **Page shell**: top bar, system messages bar, rails and collapse persistence, scale behavior.
7. Cleanup: remove dead CSS, update ARCHITECTURE.md and the UI section of any docs.

Verification per stage: run the tests; load the page at 2560 × 1440 and at a reduced width; take screenshots into `docs/ui-redesign/after/` for comparison with the mockup PNGs; confirm no console errors and that every control still sends its original message.

## 7. Open questions and assumptions (confirm with Terry before or during the stage that needs them)

- **Q1 Window size and scale.** The mockup page is 2720 px wide. Terry's actual browser width is unknown; the current UI's proportions match, but the redesign is about 1.2× larger. Decide whether to scale the whole page uniformly (CSS `zoom`, rem base or a `clamp()`ed root font size) or shrink specific sizes.
- **Q2 Slider and bar ranges** are guesses from an old screenshot: SPAN 0.5–24 kHz, FLOOR −120 to −60 dB, GAIN 0–100%, DT GAIN 0–100, RF atten 0–36 dB, exciter 5–100 W, filter width 300–3000 Hz, EQ ±12, meter bars as listed in 2.6. Use the real ranges from the existing code.
- **Q3 The unlabeled slider in AGC / NR** is called "NR LEVEL" in the mockup. Confirm what it controls.
- **Q4 FT-991A RX group** in DIGITAL AUDIO: confirm the exact condition that enables it (the mockup only shows the dimmed state from the old screenshot).
- **Q5 Tray behavior**: should the tray auto-open when a test starts, or only when Terry opens it? Default is manual; the green status dot is the cue.
- **Q6 A/B test run display**: only the idle state was seen; match the current UI during a run.
- **Q7 Meter face**: ship `paper` by default; decide whether the `backlit` theme is exposed as a setting.
- **Q8 Peak hold**: 6 s hold and 3 dB/s decay are approved; the hold time being adjustable (1–20 s) is optional.

Known unresolved item from earlier work, not part of this redesign but adjacent: DT GAIN and SSB TX BPF writes reporting "no reason reported", rig-side changes not appearing in the console, RX1 waterfall missing at startup until a session is selected. Restyling must not mask these; note any change in behavior you observe.
