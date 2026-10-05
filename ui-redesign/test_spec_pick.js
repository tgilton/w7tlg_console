#!/usr/bin/env node
/* Unit tests for the frame pick and the smoothing step in dashboard/console.html.
 * Extracts the V2SPEC block, so there is no second copy. Also checks, against the page
 * source, that the ghost and the main trace use separate buffers.
 *     node ui-redesign/test_spec_pick.js
 */
'use strict';
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'console.html'), 'utf8');
const a = html.indexOf('/* V2SPEC:BEGIN');
const b = html.indexOf('/* V2SPEC:END */');
if (a < 0 || b < 0) { console.error('V2SPEC block not found'); process.exit(1); }
const sandbox = {};
new Function('exports', html.slice(a, b)
  + '\nexports.v2PickFrame = v2PickFrame;\nexports.v2SmoothInto = v2SmoothInto;\nexports.v2ProcFresh = v2ProcFresh;')(sandbox);
const { v2PickFrame, v2SmoothInto, v2ProcFresh } = sandbox;

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}: ${detail}`); }
}

console.log('which frame is drawn');
const pick = (procActive, digital, fineAvail) => v2PickFrame({ procActive, digital, fineAvail });
check('NOISE active, SSB session -> processed', pick(true, false, false) === 'proc', '');
check('NOISE active, digital session -> processed, not fine', pick(true, true, true) === 'proc', pick(true, true, true));
check('NOISE off, digital session -> fine (unchanged)', pick(false, true, true) === 'fine', '');
check('NOISE off, digital, no fine frame yet -> wide', pick(false, true, false) === 'wide', '');
check('NOISE off, SSB session -> wide (unchanged)', pick(false, false, true) === 'wide', '');
check('bypassed in a digital session -> fine (what the session shows)', pick(false, true, true) === 'fine', '');
check('bypassed in an SSB session -> wide', pick(false, false, false) === 'wide', '');

console.log('TX: the processed frame is held, not aged');
const fresh = (ageMs, ptt, wanted = true, have = true) => v2ProcFresh({ wanted, have, ageMs, ptt });
check('RX, 200 ms old -> fresh', fresh(200, false) === true, '');
check('RX, 1.2 s old -> stale (fall back to the session display)', fresh(1200, false) === false, '');
check('TX, 1.2 s old -> still held', fresh(1200, true) === true, '');
check('TX, 60 s old -> still held', fresh(60000, true) === true, '');
check('TX but NOISE mode off -> not used', fresh(100, true, false) === false, '');
check('TX but no processed frame yet -> not used', fresh(100, true, true, false) === false, '');
check('held frame in a digital session during TX -> processed is drawn, not fine',
  v2PickFrame({ procActive: fresh(30000, true), digital: true, fineAvail: true }) === 'proc', '');
check('long RX gap in a digital session -> fine again',
  v2PickFrame({ procActive: fresh(30000, false), digital: true, fineAvail: true }) === 'fine', '');

console.log('ghost and trace under AVG > 0');
// Trace at -100 dB, ghost at -60 dB, alternating calls as the draw loop makes them.
const N = 16, alpha = 0.5;
const trace = new Float32Array(N).fill(-100), ghost = new Float32Array(N).fill(-60);
let bufT = null, bufG = null, outT, outG;
for (let k = 0; k < 50; k++) {
  outT = v2SmoothInto(bufT, trace, alpha); bufT = outT;
  outG = v2SmoothInto(bufG, ghost, alpha); bufG = outG;
}
check('separate buffers: trace stays at -100', Math.abs(outT[0] + 100) < 1e-4, outT[0]);
check('separate buffers: ghost stays at -60', Math.abs(outG[0] + 60) < 1e-4, outG[0]);
check('the two buffers are different objects', bufT !== bufG, '');
// The old fault, for contrast: one shared buffer.
let shared = null, sT, sG;
for (let k = 0; k < 50; k++) {
  sT = v2SmoothInto(shared, trace, alpha); shared = sT;
  const tDrawn = sT[0];
  sG = v2SmoothInto(shared, ghost, alpha); shared = sG;
  if (k === 49) {
    check('one shared buffer would pull the trace toward the ghost (the fault)', tDrawn > -95, tDrawn);
    console.log(`       shared buffer: trace drawn at ${tDrawn.toFixed(1)} dB, ghost at ${sG[0].toFixed(1)} dB`);
  }
}
check('AVG at zero returns the input untouched', v2SmoothInto(bufT, trace, 0) === trace, '');
check('a length change starts a fresh buffer', v2SmoothInto(bufT, new Float32Array(8).fill(-50), alpha)[0] === -50, '');

console.log('page wiring');
check('the ghost is smoothed with its own function', /applyGhostSmoothing\(cropToView\(latestGhostFrame/.test(html), '');
check('the ghost never goes through the trace buffer',
  !/applySpectrumSmoothing\(cropToView\(latestGhostFrame/.test(html), '');
check('the ghost buffer is its own variable', /let smoothedGhost = null;/.test(html)
  && /v2SmoothInto\(smoothedGhost, points, specAvgAlpha\)/.test(html), '');
check('RX1 draws through v2PickFrame', /const pick = v2PickFrame\(\{ procActive: !!procFresh/.test(html), '');
check('RX2 draws through v2PickFrame', /const pick2 = v2PickFrame\(\{ procActive: Panadapter\.procActive\(\)/.test(html), '');

console.log('TX freeze wiring');
check('processed handler drops frames while rigPtt, before storing',
  /if \(rigPtt\) \{ pendingProcHeader = null; return; \}\s*latestProcFrame = \{/.test(html), '');
check('ghost handler drops frames while rigPtt, before storing',
  /if \(rigPtt\) \{ pendingGhostHeader = null; return; \}[^\n]*\n\s*latestGhostFrame = \{/.test(html), '');
check('RX1 fine frame is stored only when not in TX', /if \(!rigPtt\) \{\s*latestFineFrame = frame;/.test(html), '');
check('RX2 fine frame is stored only when not in TX', /if \(!rigPtt\) \{[^\n]*\n\s*latestFineFrame2 = frame;/.test(html), '');
check('no unguarded fine-frame store is left', (html.match(/latestFineFrame2? = frame;/g) || []).length === 2, '');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
