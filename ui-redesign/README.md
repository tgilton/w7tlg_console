# `ui-redesign/` — the v2 console redesign

Everything used to rebuild `dashboard/console.html` to the v2 design, in
stages 0–7 on 2026-09-20, on the branch `ui-v2`.

**If you are about to edit `dashboard/console.html`, two things first:**

1. Read **ARCHITECTURE.md → "Runtime rules for editing
   `dashboard/console.html`"**. Short list, each rule bought with an
   outage or a silent regression.
2. Run **`./ui-redesign/run_checks.sh`** before you commit. Amendment F15
   requires it.

---

## Start here

| File | What it is |
|---|---|
| `SPEC_AMENDMENTS.md` | **The authority.** Overrides `UI_REDESIGN_HANDOFF.md` and `SPEC.md` wherever they conflict. Sections A–E are the standing constraints; F1–F20 is the decisions log, in the order the operator made them. |
| `../DESIGN.md` | The design system. §2.6 is the v2 token table, §12 is what the console is now, block by block. |
| `../ARCHITECTURE.md` | The whole system. The runtime-rules section is the one that matters for this file. |
| `reports/` | One report and one diff per stage — what changed, what was measured, what to check live. Not tracked by git (see `.git/info/exclude`). |
| `styleguide.html` | Every v2 component, rendered from the real stylesheet. Open it in a browser. |

`SPEC.md` and `UI_REDESIGN_HANDOFF.md` are the **original** brief and are
kept for provenance only. Both are out of date in places — SPEC.md still
describes a diversity-combiner panel and a WSJT-X decode table that were
cut, and both were written before the amendments. Where they disagree
with `SPEC_AMENDMENTS.md`, the amendments win; where they disagree with
the file, the file wins.

The PNGs (`full-page.png`, `left-column.png`, `right-column.png`,
`receiver-column.png`, `receiver-panel.png`, `tray.png`, `mockup.html`)
are the original mockups. They are **proportions and styling only** —
Amendment A rejected their fixed pixel widths outright.

---

## Checks

### `run_checks.sh` — run this before every commit

```sh
./ui-redesign/run_checks.sh            # everything (~2 min)
./ui-redesign/run_checks.sh --quick    # skip the two browser checks (~5 s)
```

Seven checks, each printing PASS or FAIL, non-zero exit if any fails:
pytest, the four node unit tests, `node --check` on each `<script>` block,
the five protected literal lines, CSS brace balance, `smoke_console.js`,
and `test_wheel_real.js`. The header comment in the script says why each
one exists.

It starts nothing and stops nothing. The browser checks load
`console.html` from disk as a `file://` URL in a headless Chrome with a
throwaway profile directory, so they cannot see the operator's Chrome
window or tabs, and cannot touch the running console, rigctld or WSJT-X.

### `smoke_console.js` — the one that catches an outage

```sh
node ui-redesign/smoke_console.js
```

Loads the real page in headless Chrome and asserts: no uncaught or
unexpected console errors; `Panadapter`, `Panadapter2` and `RigControl`
all exist (checked by indirect eval — they are top-level `const`s, so
they are **not** on `window`); all 85 inline `on*` handler references
resolve; every required id is present. WebSocket failures and
audio-worklet `file://` artefacts are expected without a server and are
tolerated by name.

This exists because stage 5 shipped a `ReferenceError` in
`Panadapter.init()` and every static check passed. `tests/test_console_smoke.py`
runs it from pytest under the `browser` marker; it takes ~90 s, so the
fast suite is `pytest -m "not browser"`.

### Node unit tests — no second copy of anything

Each extracts the real functions out of `console.html` by name and runs
them, so the test cannot drift from the source.

| | |
|---|---|
| `test_meter_math.js` | 27 — S-meter fraction/angle, and the peak-hold decay, asserted frame-rate independent (it was compounding) |
| `test_af_taper.js` | 14 — the AF GAIN quadratic taper and its inverse |
| `test_af_paths.js` | 22 — the AF send and state-sync paths on **both** receivers, against a fake `send()` and DOM |
| `test_palettes.js` | 101 — all twelve waterfall palettes, the six trace colours, the wheel gate |
| `test_wheel_real.js` | real `WheelEvent`s at all 26 sliders in a real page: adjust when rested, blocked when not |

### Inventory scripts — what `node --check` cannot see

`node --check` parses JavaScript, not HTML. A truncated
`ontouchend="…"` is invisible to it. These list every id, `on*` handler,
`data-*` attribute and JS-referenced class in a region, so a restyle can
be proved additive by diffing before against after. They caught
`#resize-modedsp` silently disappearing and four slider tags being
truncated mid-attribute — neither visible in a screenshot.

```sh
./venv/bin/python ui-redesign/<name>.py > before.txt   # at the previous tag
./venv/bin/python ui-redesign/<name>.py > after.txt
diff before.txt after.txt
```

`column_inventory.py` (any named region) · `right_column_inventory.py` ·
`moved_region_inventory.py` (A/B + Measure Noise, located by content so
it works either side of the move) · `rx_toprow_inventory.py` ·
`rx_bottomrow_inventory.py` · `rx_lower_inventory.py` ·
`shell_inventory.py`

### `measure_real_page.js` — geometry, from the real page

```sh
node ui-redesign/measure_real_page.js --shot=out.png --scroll=800
SMOKE_H=700 node ui-redesign/measure_real_page.js
```

Amendment F16: measure and screenshot the page **as it ships**, with its
modules and their `init()` running. Rebuilt harnesses that ran only
selected script blocks are what let the stage 5 outage through.

---

## Generated files

```sh
./venv/bin/python ui-redesign/build_styleguide.py   # → styleguide.html
./venv/bin/python ui-redesign/build_meter_svg.py    # → the S-meter face
```

`styleguide.html` is **generated, never hand-edited.** It extracts
everything between the `V2:BEGIN` / `V2:END` markers in
`dashboard/console.html`, so it cannot drift from the real stylesheet.
Regenerate it after any change inside those markers.

`build_meter_svg.py` emits the analog meter face (viewBox 460×240, arc
centre (230, 330), radius 270, ±38° from vertical; S1→S9 is the first 58%
of the arc). Its output is pasted into the page once per receiver, so
re-run it only if the geometry or the labels change.

---

## Stage → tag map

Each tag is the last commit of that stage. Diffs in `reports/` are from
the previous stage's tag.

| Stage | Tag | Commit | What |
|---|---|---|---|
| 0 | `ui-v2-stage0` | `2006be7` | branch, spec copied in, stale-FT8-session bug fixed |
| 1 | `ui-v2-stage1` | `7e1118d` | v2 tokens, `.v2-*` components, style guide |
| 2 | `ui-v2-stage2` | `53dd93c` | right column (AMP, TX METERS, EXCITER DRIVE, OPERATING MODE, FAULT STATUS) |
| 2p | `ui-v2-stage2-polish` | `210667d` | FWD P hero label, centred badges, Operating Mode fit |
| 3a | `ui-v2-stage3a` | `e8f30e5` | tools tray; A/B test and Measure Noise moved in |
| 3b | `ui-v2-stage3b` | `f2e4d35` | left column; SSB Audio moved in from the right |
| 4a | `ui-v2-stage4a` | `cc43064` | frequency card, analog S-meter, RX chip, S9 CAL on both |
| 4a‑fix | `ui-v2-stage4a-fix` | `28a364d` | the meter readout can no longer reflow |
| 4b | `ui-v2-stage4b` | `ec98ca5` | gains and mode cards (AF GAIN default gated pending the operator's call) |
| 4b‑final | `ui-v2-stage4b-final` | `bb1f896` | AF taper on RX2's send path, RX2 synced from the server |
| 5 | `ui-v2-stage5` | `0c6b87f` | AVG 30%, twelve palettes, TRACE, wheel |
| 5‑fix | `ui-v2-stage5-fix` | `4d5625d` | **the outage** — v2 helpers moved above the modules |
| 5b | `ui-v2-stage5b` | `47bfa70` | AUTO to the title row, selects, controls polish |
| 6 | `ui-v2-stage6` | `1ce32b9` | the page shell |
| 6b | `ui-v2-stage6b` | `5890cb9` | the wheel adjusts every slider again, guarded by rest |
| 7 | `ui-v2-stage7` | — | docs, `run_checks.sh`, smoke test, dead-CSS candidates. No CSS or markup deleted (F19), `console.html` not modified (F20). |

```sh
git diff ui-v2-stage3b..ui-v2-stage4a -- . ':(exclude)ui-redesign/'
```

---

## Two things that are deliberate and look like bugs

- **`Panadapter2` is a near-copy of `Panadapter`, not a second instance**
  (Amendment E7). Do not "fix" it. Do verify both channels after any
  RX-channel change — a fix scoped to one has broken the other twice.
- **The antenna tiles are indicators, not buttons**: no `onclick`, no
  hover, no focus (Amendment E1). NEXT ANT is the only actuator, because
  the ACOM firmware has no select-antenna-N command at all.
