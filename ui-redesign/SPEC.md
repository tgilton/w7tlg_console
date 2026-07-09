# w7tlg_console — unified console UI redesign spec

## Purpose

Redesign the operating console into one cohesive block layout instead of the
current `console.html` iframe shell over three separate pages. Reference
mockup: `ui-redesign/mockup.html` (open it in a browser — it's built from the
same CSS tokens as the current dashboard, so it's a real visual target, not a
sketch).

## Do not regress: safety-critical isolation

`dashboard/console.html` currently uses iframes deliberately — see the
comment block at the top of that file. It isolates `index.html`,
`panadapter.html`, and `monitor.html` into separate JS scopes to protect:

- TX audio mute timing
- Feedback-loop prevention
- Digital-mode view locking

If this redesign moves to a single-scope page (recommended — see
Architecture below), every one of those behaviors must be re-verified
explicitly, not assumed to carry over. Treat this as the top acceptance
criterion, above any visual polish. When in doubt, keep the risky logic
(TX muting, PTT sequencing) in its own module with narrow, tested entry
points rather than inlining it into a shared render loop.

## Current state (for reference — do not treat as constraints, treat as
context)

- Backend: FastAPI, `dashboard/server.py`. Existing routes: `/`, `/monitor`,
  `/panadapter`, `/console`, `/propagation`, `/spotseek`, `/advisor`.
- Existing websockets: `/ws` (control/state), `/ws/spectrum` (panadapter FFT),
  `/ws/audio` (audio pipeline). This three-way split already matches the
  data-plane separation this spec assumes — keep it.
- REST: `/api/state`, `/api/mode` (has a `confirmed` flag — mode changes are
  gated), `/api/antenna/next`, `/api/amp/settings`, `/api/amp/relink-telemetry`,
  `/api/amp/atac`, `/api/tx` (has `inhibit`/`reason` — TX can be programmatically
  blocked with a stated cause), `/api/station-profile`, `/api/watchlist`,
  `/api/awards/*`, `/api/propagation`, `/api/advisor/*`.
- Existing color tokens (already dark, already good — reuse, don't
  reinvent): `--bg #0d1117`, `--surface #161b22`, `--border #21262d`,
  `--border2 #30363d`, `--cyan #00d4ff`, `--amber #ff8c00`,
  `--green #00e676`, `--red #ff3333`, `--yellow #ffd60a`, `--muted #58677a`,
  `--label #8b949e`, `--text #c9d1d9`. Fonts: JetBrains Mono (numeric
  readouts), Inter (UI text).
- SWR and other safety-relevant meters already use solid status colors
  (not just numeric) — preserve that convention in any new metering.

## Architecture: three data planes

Keep these separate in both the websocket layer and the frontend state
model — this is already true in the backend (`/ws`, `/ws/spectrum`,
`/ws/audio`); the frontend redesign should mirror it rather than flattening
everything into one state object.

1. **Control plane** (`/ws`) — rig freq/mode/split, amp telemetry, TX
   inhibit state, band, memory. Low frequency, small payloads.
2. **Spectrum plane** (`/ws/spectrum`) — SDR FFT bins for the panadapter
   and waterfall. High frequency (10–20 Hz), typed-array payloads, not JSON,
   if not already.
3. **Combiner plane** (new — see Diversity block below) — antenna A/B
   levels, phase offset, gain balance, null depth, auto/manual mode.
   Separate from both of the above because it updates on its own cadence
   and shouldn't block spectrum rendering or control-state updates.

## Block layout

Grid: `190px [mode/dsp] | 1fr [center] | 220px [band/amp]` top row, full-width
rows above and below. See `mockup.html` for exact proportions.

### 1. Status bar (full width, top)
Connection state for: rig (rigctld), amp (ACOM), SDR A, SDR B (once
diversity lands), WSJT-X UDP link. Plus current band/mode/grid summary.
Data source: derive from existing bridge connection states in `server.py`;
no new backend work needed for the single-SDR case.

### 2. Mode / DSP (left column)
Mode buttons (USB/CW/FT8 — extend from whatever `OperatingMode` enum
already defines in the bridge). NB/NR/Notch toggles, width/shift sliders.
Wire to CAT commands via the existing `/api/mode` pattern — reuse the
`confirmed` gating convention for anything that could interrupt an
in-progress QSO.

### 3. VFO / panadapter / waterfall / tune (center, dominant)
Large frequency readout (JetBrains Mono, matches existing style). Below it,
the panadapter with a **trace-overlay toggle**: Main / Loop / Combined —
this is new, needed for the diversity work, and should be built even before
the combiner backend exists (default to Main only, disable Loop/Combined
until SDR B is live). Waterfall below that. Tune/VOX/MOX row uses the
existing `/api/tx` inhibit/reason semantics — do not add a second, competing
TX-control path.

### 4. Band / amp status (right column)
Band buttons drive existing CAT band-set logic. Amp status block reads
ACOM telemetry already flowing through `acom_bridge.py` — power, SWR
(solid-color status, not just numeric, per existing convention), temp.

### 5. RX diversity combiner (new, full width row)
**Phase 2 — build the UI now, stub the backend.** No `sdr/` module for a
second tuner or combiner logic exists yet. Build this panel against a mock
state object (antenna A/B level, phase offset, null depth, auto/manual
toggle) so the layout and interaction pattern are validated before the real
RSPduo dual-tuner and combiner service exist. Leave a clear
`// TODO(diversity): replace mock state with /ws/combiner once
sdr/combiner.py exists` marker in the code. Don't block the rest of the
redesign on this backend not existing yet.

When the real backend does land: both SDR tuners must share one frequency
state (driven by the single VFO), not two independent frequency states —
this is diversity combining (same frequency, two antennas), not a second
independent receiver like a dual-VFO rig.

### 6. Digital modes + log (bottom, two columns)
Digital modes panel reads `/ws` or a WSJT-X-specific feed from
`wsjtx/udp_listener.py` / `wsjtx/protocol.py` — decode table (UTC, call/grid,
SNR, DT). Log panel reads from `wsjtx/qso_logger.py`. Both already have
backend support; this is primarily a frontend consolidation.

## Explicit non-goals for this pass

- Do not rebuild `monitor.html`, `propagation.html`, `spotseek.html`, or
  `advisor.html` — those stay as their own routes/pages for now.
- Do not implement the real diversity combiner backend (no second SDR
  tuner code, no phase/gain DSP) — UI scaffold only, per block 5.
- Do not change `/api/tx` or `/api/mode` semantics (the `inhibit`/`reason`
  and `confirmed` gates exist for a reason — extend, don't replace).

## Acceptance criteria

1. TX audio mute timing, feedback-loop prevention, and digital-mode view
   locking all still work — verified, not assumed.
2. Spectrum/waterfall render rate is unaffected by control-plane message
   frequency (no stutter when amp telemetry or band changes arrive).
3. Diversity combiner panel renders with mock data and is visually
   distinguishable as not-yet-live (e.g. disabled Loop/Combined toggle
   states) rather than looking broken or half-wired.
4. Existing routes (`/monitor`, `/panadapter`, `/propagation`, `/spotseek`,
   `/advisor`) remain reachable and unmodified.
