#!/usr/bin/env node
/* Unit tests for the pure S-meter math in dashboard/console.html.
 *
 * Extracts the V2METER:BEGIN..END block and evaluates it in isolation, so
 * the functions are tested exactly as they ship — no second copy to drift.
 *
 *     node ui-redesign/test_meter_math.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'dashboard', 'console.html'), 'utf8');
const a = html.indexOf('/* ── V2METER:BEGIN');
const b = html.indexOf('/* V2METER:END */');
if (a < 0 || b < 0) { console.error('V2METER block not found'); process.exit(1); }
const src = html.slice(a, b);

const sandbox = {};
new Function('exports', src + '\nexports.v2MeterFraction = v2MeterFraction;'
  + '\nexports.v2MeterAngle = v2MeterAngle;\nexports.v2PeakStep = v2PeakStep;')(sandbox);
const { v2MeterFraction, v2MeterAngle, v2PeakStep } = sandbox;

let pass = 0, fail = 0;
function near(name, got, want, eps) {
  eps = eps === undefined ? 1e-9 : eps;
  if (Math.abs(got - want) <= eps) { pass++; console.log(`  ok   ${name} = ${got}`); }
  else { fail++; console.log(`  FAIL ${name}: got ${got}, want ${want}`); }
}
function eq(name, got, want) {
  if (got === want) { pass++; console.log(`  ok   ${name} = ${got}`); }
  else { fail++; console.log(`  FAIL ${name}: got ${got}, want ${want}`); }
}

const CAL = -75;                      // dBFS at S9

console.log('fraction');
// S1 is 8 S-units (48 dB) below S9.
near('S1 -> 0', v2MeterFraction(CAL - 48, CAL), 0);
near('S9 -> 0.58', v2MeterFraction(CAL, CAL), 0.58);
near('S9+60 -> 1', v2MeterFraction(CAL + 60, CAL), 1);
near('below S1 clamps to 0', v2MeterFraction(CAL - 200, CAL), 0);
near('above +60 clamps to 1', v2MeterFraction(CAL + 500, CAL), 1);
near('S5 -> half of 0.58', v2MeterFraction(CAL - 24, CAL), 0.29);
near('S9+30 -> 0.79', v2MeterFraction(CAL + 30, CAL), 0.79, 1e-12);
near('calibration shifts the scale', v2MeterFraction(-60, -60), 0.58);
eq('NaN input -> 0', v2MeterFraction(NaN, CAL), 0);

console.log('angle');
near('f 0 -> -38', v2MeterAngle(0), -38);
near('f 0.5 -> 0', v2MeterAngle(0.5), 0);
near('f 1 -> +38', v2MeterAngle(1), 38);
near('angle at S9', v2MeterAngle(0.58), -38 + 76 * 0.58, 1e-12);

console.log('peak hold (fake clock)');
const HOLD = 6000, DECAY = 3;
let p = null;
p = v2PeakStep(p, -80, 0, HOLD, DECAY);
near('first reading seeds the peak', p.db, -80);

// Live level drops; peak must hold flat for the full 6 s.
p = v2PeakStep(p, -100, 1000, HOLD, DECAY);
near('holds at 1s', p.db, -80);
p = v2PeakStep(p, -100, 5999, HOLD, DECAY);
near('holds at 5.999s', p.db, -80);

// After the hold, 3 dB per second.
p = v2PeakStep(p, -100, 7000, HOLD, DECAY);
near('decays 3dB after 1s past hold', p.db, -83);
p = v2PeakStep(p, -100, 9000, HOLD, DECAY);
near('decays 9dB after 3s past hold', p.db, -89);

// Never below the live level.
p = v2PeakStep(p, -100, 60000, HOLD, DECAY);
near('never below live', p.db, -100);
p = v2PeakStep(p, -90, 60100, HOLD, DECAY);
near('rises to a higher live reading', p.db, -90);

// A higher reading resets the hold.
let q = v2PeakStep(null, -80, 0, HOLD, DECAY);
q = v2PeakStep(q, -70, 3000, HOLD, DECAY);
near('higher reading takes the peak', q.db, -70);
eq('and restarts the hold clock', q.at, 3000);
q = v2PeakStep(q, -100, 8000, HOLD, DECAY);
near('still holding 5s after the reset', q.db, -70);
q = v2PeakStep(q, -100, 10000, HOLD, DECAY);
near('decays 3dB 1s after the new hold ends', q.db, -73);

// Equal reading also counts as a new peak (spec: "at or above").
let r = v2PeakStep(null, -80, 0, HOLD, DECAY);
r = v2PeakStep(r, -80, 4000, HOLD, DECAY);
eq('equal reading restarts the hold', r.at, 4000);

// Rate must not depend on how often it is called.
let coarse = v2PeakStep(null, -80, 0, HOLD, DECAY);
coarse = v2PeakStep(coarse, -120, 8000, HOLD, DECAY);
let fine = v2PeakStep(null, -80, 0, HOLD, DECAY);
for (let t = 100; t <= 8000; t += 100) fine = v2PeakStep(fine, -120, t, HOLD, DECAY);
near('frame-rate independent', fine.db, coarse.db, 1e-9);
near('  ...and both are -86 (2s past the hold)', coarse.db, -86, 1e-9);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
