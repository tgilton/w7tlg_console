#!/usr/bin/env node
/* Unit tests for the AF GAIN audio taper in dashboard/console.html.
 * Extracts the V2AF block and evaluates it, so there is no second copy.
 *     node ui-redesign/test_af_taper.js
 */
'use strict';
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'console.html'), 'utf8');
const a = html.indexOf('/* ── V2AF:BEGIN');
const b = html.indexOf('/* V2AF:END */');
if (a < 0 || b < 0) { console.error('V2AF block not found'); process.exit(1); }
const sandbox = {};
new Function('exports', html.slice(a, b)
  + '\nexports.v2AfPosToGain = v2AfPosToGain;\nexports.v2AfGainToPos = v2AfGainToPos;')(sandbox);
const { v2AfPosToGain, v2AfGainToPos } = sandbox;

let pass = 0, fail = 0;
function near(name, got, want, eps) {
  eps = eps === undefined ? 1e-9 : eps;
  if (Math.abs(got - want) <= eps) { pass++; console.log(`  ok   ${name} = ${got}`); }
  else { fail++; console.log(`  FAIL ${name}: got ${got}, want ${want}`); }
}

console.log('endpoints');
near('pos 0 -> 0%', v2AfPosToGain(0), 0);
near('pos 1000 -> 1000%', v2AfPosToGain(1000), 1000);
near('0% -> pos 0', v2AfGainToPos(0), 0);
near('1000% -> pos 1000', v2AfGainToPos(1000), 1000);

console.log('the default, 100%');
near('100% -> pos 316 (rounded)', Math.round(v2AfGainToPos(100)), 316);
near('pos 316 -> 100% (rounded)', Math.round(v2AfPosToGain(316)), 100);
near('notch fraction is sqrt(0.1)', v2AfGainToPos(100) / 1000, Math.sqrt(0.1), 1e-12);

console.log('shape: 100% sits about a third along, not a tenth');
const third = v2AfGainToPos(100) / 1000;
if (third > 0.30 && third < 0.33) { pass++; console.log(`  ok   fraction = ${third.toFixed(4)}`); }
else { fail++; console.log(`  FAIL fraction = ${third}`); }
near('halfway is a quarter of full gain', v2AfPosToGain(500), 250);

console.log('clamping');
near('negative position clamps', v2AfPosToGain(-50), 0);
near('over-max position clamps', v2AfPosToGain(5000), 1000);
near('over-max gain clamps', v2AfGainToPos(99999), 1000);

console.log('round trip within 1 unit at every position');
let worst = 0, worstAt = -1;
for (let pos = 0; pos <= 1000; pos++) {
  const back = v2AfGainToPos(v2AfPosToGain(pos));
  const err = Math.abs(back - pos);
  if (err > worst) { worst = err; worstAt = pos; }
}
if (worst <= 1) { pass++; console.log(`  ok   worst error ${worst.toExponential(2)} at pos ${worstAt}`); }
else { fail++; console.log(`  FAIL worst error ${worst} at pos ${worstAt}`); }

console.log('round trip from gain, every 1%');
let worstG = 0;
for (let g = 0; g <= 1000; g++) {
  const back = v2AfPosToGain(v2AfGainToPos(g));
  worstG = Math.max(worstG, Math.abs(back - g));
}
if (worstG <= 1) { pass++; console.log(`  ok   worst error ${worstG.toExponential(2)}%`); }
else { fail++; console.log(`  FAIL worst error ${worstG}%`); }

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
