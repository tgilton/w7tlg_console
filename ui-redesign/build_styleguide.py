#!/usr/bin/env python3
"""Generate ui-redesign/styleguide.html from the V2 block in console.html.

The styleguide must never drift from the real stylesheet, so it does not
own a copy of it: it extracts everything between the V2:BEGIN and V2:END
markers out of dashboard/console.html — both the <style> block and the
slider helper <script> — and inlines those, verbatim, into a standalone
page. Edit the CSS in console.html and re-run this; there is nowhere else
to change it.

Usage (from the repo root):

    ./venv/bin/python ui-redesign/build_styleguide.py

Then open ui-redesign/styleguide.html in a browser — it is a plain local
file, no server and no running console needed.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "dashboard" / "console.html"
OUT = ROOT / "ui-redesign" / "styleguide.html"

BEGIN = "/* V2:BEGIN"
END = "/* V2:END */"


def extract_blocks(src: str) -> tuple[str, str]:
    """Return (css, js) — the V2 region of the <style> block and of the
    helper <script>, in that order.

    Both regions are delimited by the same marker pair, so they are told
    apart by which parent tag they sit in rather than by guessing.
    """
    style = re.search(r"<style>\n(.*?)\n</style>", src, re.S)
    if not style:
        sys.exit("console.html: no <style> block found")

    scripts = re.findall(r"<script>\n(.*?)\n</script>", src, re.S)
    if not scripts:
        sys.exit("console.html: no <script> blocks found")

    def carve(text: str, what: str) -> str:
        start = text.find(BEGIN)
        stop = text.find(END, start + 1)
        if start == -1 or stop == -1:
            sys.exit(f"console.html: no V2:BEGIN/V2:END pair in the {what}")
        return text[start:stop + len(END)]

    css = carve(style.group(1), "<style> block")

    js_holders = [s for s in scripts if BEGIN in s]
    if len(js_holders) != 1:
        sys.exit(f"console.html: expected exactly one <script> carrying a V2 "
                 f"block, found {len(js_holders)}")
    js = carve(js_holders[0], "helper <script>")

    return css, js


# The demo markup. Every component in every state, on the console's own
# page background. Kept here rather than in a separate file so the whole
# generator is one readable thing.
BODY = """
<h1>W7TLG Console — UI v2 style guide</h1>
<p class="sg-note">
  Generated from the <code>V2:BEGIN … V2:END</code> block in
  <code>dashboard/console.html</code> by
  <code>ui-redesign/build_styleguide.py</code>. The CSS below is the real
  stylesheet, not a copy — edit it in console.html and regenerate.
  Nothing here is wired to the console; the sliders are live only in the
  sense that the shared fill helper is driving them.
</p>

<section class="sg-sec">
  <h2 class="sg-h">Card + block title + labels and values</h2>
  <div class="sg-row">
    <div class="v2-card" style="width:320px">
      <div class="v2-title">Session</div>
      <div class="sg-stack">
        <div class="sg-kv"><span class="v2-label">Field label</span><span class="v2-value">100<span class="v2-unit">%</span></span></div>
        <div class="sg-kv"><span class="v2-label v2-label--light">Light label</span><span class="v2-value v2-value--green">1200<span class="v2-unit">W</span></span></div>
        <div class="sg-kv"><span class="v2-label">Inactive</span><span class="v2-value v2-value--inactive">—</span></div>
        <div class="sg-kv"><span class="v2-label">Primary</span><span class="v2-value v2-value--fg">7.074.000</span></div>
      </div>
      <hr class="v2-divider">
      <div class="v2-label">After a divider</div>
    </div>
    <div class="v2-card v2-card--tight" style="width:320px">
      <div class="v2-title">Tight card</div>
      <p class="sg-note" style="margin:0">
        <code>.v2-card--tight</code> — 16px padding, for the spectrum and
        meter cards.
      </p>
    </div>
  </div>
</section>

<section class="sg-sec">
  <h2 class="sg-h">Buttons</h2>
  <div class="sg-row">
    <button class="v2-btn">Off</button>
    <button class="v2-btn v2-is-selected">Selected</button>
    <button class="v2-btn" disabled>Disabled</button>
    <button class="v2-btn v2-btn--go">&#9654; Start</button>
    <button class="v2-btn v2-btn--stop">&#9632; Stop</button>
    <button class="v2-btn v2-btn--dense">Dense 44</button>
    <button class="v2-btn v2-btn--dense v2-is-selected">Dense selected</button>
  </div>
  <div class="sg-row sg-row--tight">
    <span class="sg-note">Tab through these to see the focus ring
      (2px <code>#f0b04a</code>, 2px offset).</span>
  </div>
  <div class="sg-row">
    <div style="width:320px">
      <div class="v2-title">Mode</div>
      <div class="sg-grid3">
        <button class="v2-btn">USB</button>
        <button class="v2-btn">LSB</button>
        <button class="v2-btn" disabled>CW</button>
        <button class="v2-btn" disabled>AM</button>
        <button class="v2-btn" disabled>FM</button>
        <button class="v2-btn v2-is-selected">DATA-U</button>
      </div>
    </div>
  </div>
</section>

<section class="sg-sec">
  <h2 class="sg-h">Toggle chips</h2>
  <div class="sg-row">
    <button class="v2-toggle">DNR</button>
    <button class="v2-toggle v2-is-on">DNR on</button>
    <button class="v2-toggle" disabled>RF NOTCH</button>
    <button class="v2-toggle">DAB NOTCH</button>
  </div>
</section>

<section class="sg-sec">
  <h2 class="sg-h">Pills and the receiver chip</h2>
  <div class="sg-row">
    <span class="v2-pill v2-pill--amber">40m</span>
    <span class="v2-pill v2-pill--amber">PKTUSB</span>
    <span class="v2-pill v2-pill--green">RX</span>
    <span class="v2-pill v2-pill--green">FT8</span>
    <span class="v2-pill v2-pill--green">DIGI AUDIO</span>
    <span class="v2-pill v2-pill--warn">SPLIT</span>
    <span class="v2-pill v2-pill--green-outline">OPR/RX</span>
  </div>
  <div class="sg-row">
    <span class="v2-rx-chip">RX1</span>
    <span class="v2-rx-chip">RX2</span>
  </div>
</section>

<section class="sg-sec">
  <h2 class="sg-h">Inputs, selects, checkbox chips</h2>
  <div class="sg-row">
    <input class="v2-input" type="text" value="7.074000" style="width:190px">
    <input class="v2-input" type="number" value="-75" style="width:64px">
    <input class="v2-input" type="text" value="disabled" disabled style="width:120px">
    <select class="v2-select">
      <option>Default</option><option>Linrad</option><option>Grayscale</option>
      <option>Hot</option><option>GQRX</option>
    </select>
    <label class="v2-check"><input type="checkbox"> Manual switching</label>
    <label class="v2-check"><input type="checkbox" checked> A1F</label>
    <label class="v2-check"><input type="checkbox" checked> A3R</label>
    <label class="v2-check"><input type="checkbox"> A4R</label>
  </div>
</section>

<section class="sg-sec">
  <h2 class="sg-h">Sliders</h2>
  <p class="sg-note">
    Plain native <code>&lt;input type=range&gt;</code>. The fill is drawn
    from <code>--v2-fill</code>, which the shared helper recomputes from
    the live <code>value/min/max</code> every frame — so a slider moved by
    code, not by hand, fills correctly too. Press the button below to prove
    it.
  </p>
  <div class="sg-row">
    <div class="v2-card" style="width:420px">
      <div class="v2-title">Gains</div>

      <div class="v2-slider-row" style="margin-bottom:18px">
        <div class="v2-slider-head">
          <span class="v2-label">AF Gain</span>
          <span class="v2-value" id="sg-af-val">100<span class="v2-unit">%</span></span>
        </div>
        <input class="v2-range" type="range" id="sg-af"
               min="0" max="1000" step="10" value="100" data-v2-notch="0.3162">
      </div>

      <div class="v2-slider-row">
        <div class="v2-slider-head">
          <span class="v2-label">RF Gain</span>
          <span class="v2-value">6<span class="v2-unit">dB atten</span></span>
        </div>
        <input class="v2-range" type="range" min="0" max="6" step="1" value="5">
      </div>
    </div>

    <div class="v2-card" style="width:420px">
      <div class="v2-title">States</div>
      <div class="v2-slider-row" style="margin-bottom:18px">
        <div class="v2-slider-head">
          <span class="v2-label">At zero</span><span class="v2-value">0</span>
        </div>
        <input class="v2-range" type="range" min="0" max="100" value="0">
      </div>
      <div class="v2-slider-row" style="margin-bottom:18px">
        <div class="v2-slider-head">
          <span class="v2-label">At full</span><span class="v2-value">100</span>
        </div>
        <input class="v2-range" type="range" min="0" max="100" value="100">
      </div>
      <div class="v2-slider-row">
        <div class="v2-slider-head">
          <span class="v2-label">Disabled</span><span class="v2-value">50</span>
        </div>
        <input class="v2-range" type="range" min="0" max="100" value="50" disabled>
      </div>
    </div>
  </div>
  <div class="sg-row">
    <button class="v2-btn" id="sg-drive">Move AF Gain from code</button>
    <span class="sg-note" id="sg-drive-note">
      sets <code>.value</code> directly, the way a state push does — no
      input event fires.
    </span>
  </div>
</section>

<section class="sg-sec">
  <h2 class="sg-h">Frequency card (assembled)</h2>
  <div class="sg-row">
    <div class="v2-card" style="width:438px">
      <div class="sg-kv" style="margin-bottom:14px">
        <span class="v2-label">Frequency</span>
        <span class="v2-rx-chip">RX1</span>
      </div>
      <div class="sg-freq">7<i>.</i>074<i>.</i>000</div>
      <div class="sg-row sg-row--tight" style="margin-top:16px">
        <span class="v2-pill v2-pill--amber">40m</span>
        <span class="v2-pill v2-pill--amber">PKTUSB</span>
        <span class="v2-pill v2-pill--green">RX</span>
        <span class="v2-pill v2-pill--warn">SPLIT</span>
        <span class="v2-pill v2-pill--green">FT8</span>
        <span class="v2-pill v2-pill--green">DIGI AUDIO</span>
      </div>
    </div>
  </div>
</section>
"""

# Styleguide chrome only — never component CSS. Anything that a later
# stage might actually reuse belongs in console.html's V2 block instead.
PAGE_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: var(--v2-bg-page);
  color: var(--v2-fg);
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 12px;
  line-height: 1.4;
  font-variant-numeric: tabular-nums;
}
body { padding: 32px 40px 80px; }
h1 { font-size: 20px; font-weight: 500; letter-spacing: 0.12em;
     text-transform: uppercase; color: var(--v2-title-fg); margin-bottom: 10px; }
.sg-note { color: var(--v2-label); max-width: 70ch; margin-bottom: 8px; }
.sg-note code, code { color: var(--v2-readout); }
.sg-sec { margin-top: 40px; border-top: 1px solid var(--v2-border); padding-top: 18px; }
.sg-h { font-size: 12px; font-weight: 500; letter-spacing: 0.18em;
        text-transform: uppercase; color: var(--v2-label-light); margin-bottom: 16px; }
.sg-row { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; margin-bottom: 8px; }
.sg-row--tight { gap: 8px; align-items: center; }
.sg-stack { display: flex; flex-direction: column; gap: 10px; }
.sg-kv { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.sg-grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.sg-freq { font-size: 76px; font-weight: 500; color: var(--v2-fg); line-height: 1; }
.sg-freq i { font-style: normal; color: var(--v2-notch-fg); display: inline-block;
             width: 0.3em; text-align: center; }
"""

DEMO_JS = """
// Styleguide-only. Proves the fill helper catches a programmatic write:
// this assigns .value the way updateUI() does on a state push, fires no
// input event, and the fill still follows.
(function () {
  var btn = document.getElementById('sg-drive');
  var el = document.getElementById('sg-af');
  var out = document.getElementById('sg-af-val');
  var steps = [0, 100, 250, 500, 1000, 100];
  var i = 0;
  btn.addEventListener('click', function () {
    var v = steps[i++ % steps.length];
    el.value = v;                       // no dispatchEvent, deliberately
    out.innerHTML = v + '<span class="v2-unit">%</span>';
  });
})();
"""


def main() -> None:
    src = CONSOLE.read_text()
    css, js = extract_blocks(src)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>W7TLG Console — UI v2 style guide</title>
<!-- GENERATED FILE — do not edit.
     Source of truth: the V2:BEGIN/V2:END block in dashboard/console.html.
     Regenerate: ./venv/bin/python ui-redesign/build_styleguide.py -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
{css}

/* ── Style-guide page chrome (NOT part of the v2 system) ───────────── */
{PAGE_CSS.strip()}
</style>
</head>
<body>
{BODY.strip()}

<script>
{js}
</script>
<script>
{DEMO_JS.strip()}
</script>
</body>
</html>
"""
    OUT.write_text(page)
    print(f"wrote {OUT.relative_to(ROOT)}  "
          f"({len(css.splitlines())} lines of v2 CSS, "
          f"{len(js.splitlines())} lines of helper JS)")


if __name__ == "__main__":
    main()
