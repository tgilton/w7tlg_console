#!/usr/bin/env node
/* Unit tests for the CANCEL gain and phase wheel steps in dashboard/console.html.
 * Extracts the CANCELWHEEL block and evaluates it, so there is no second copy.
 *     node ui-redesign/test_cancel_wheel.js
 */
'use strict';
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'console.html'), 'utf8');
const a = html.indexOf('/* CANCELWHEEL:BEGIN');
const b = html.indexOf('/* CANCELWHEEL:END */');
if (a < 0 || b < 0) { console.error('CANCELWHEEL block not found'); process.exit(1); }
const sandbox = {};
new Function('exports', html.slice(a, b)
  + '\nexports.cancelWheelAccumulate = cancelWheelAccumulate;'
  + '\nexports.cancelWheelNext = cancelWheelNext;'
  + '\nexports.TRIGGER = CANCEL_WHEEL_TRIGGER_PX;')(sandbox);
const { cancelWheelAccumulate, cancelWheelNext, TRIGGER } = sandbox;

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}: ${detail}`); }
}
const gain = (v, dy, mode, acc, shift) => cancelWheelNext(v, dy, mode || 0, acc || 0,
  { kind: 'gain', min: -40, max: 80, shift: !!shift });
const phase = (v, dy, mode, acc, shift) => cancelWheelNext(v, dy, mode || 0, acc || 0,
  { kind: 'phase', min: 0, max: 360, shift: !!shift });

console.log('one notch');
// A mouse notch is large (about 100 px), so it is one step, and the carry is capped.
let r = gain(10, -100);
check('wheel up is +0.1 dB', r.value === 10.1 && r.changed, JSON.stringify(r));
r = gain(10, 100);
check('wheel down is -0.1 dB', r.value === 9.9, JSON.stringify(r));
check('a big notch never banks more than one step', cancelWheelNext(10, -100, 0, 0,
  { kind: 'gain', min: -40, max: 80, shift: false }).acc === TRIGGER, 'carry should cap at the trigger');

console.log('shift is one dB');
r = gain(10, -100, 0, 0, true);
check('Shift + wheel up is +1.0 dB', r.value === 11.0, JSON.stringify(r));
r = gain(10, 100, 0, 0, true);
check('Shift + wheel down is -1.0 dB', r.value === 9.0, JSON.stringify(r));

console.log('trackpad accumulation');
let acc = 0, v = 0, steps = 0;
for (let i = 0; i < 10; i++) {               // ten small pixel events of 3 px each
  r = gain(v, -3, 0, acc); acc = r.acc; if (r.changed) { steps++; v = r.value; }
}
check('ten 3 px events make at most three steps', steps <= 3 && steps >= 2, `steps=${steps}`);
check('the value moved by 0.1 per step, no more', Math.abs(v - steps * 0.1) < 1e-9, `v=${v}`);
r = gain(0, -10000, 0, 0);
check('a huge single pixel delta is still one step', r.value === 0.1 && Math.abs(r.acc) <= TRIGGER, JSON.stringify(r));
r = gain(0, -10, 1, 0);
check('line-mode deltas are scaled to pixels', r.changed === true, JSON.stringify(r));

console.log('clamps and wrap');
check('gain clamps at +80', gain(80, -100).value === 80 && gain(80, -100).changed === false, JSON.stringify(gain(80, -100)));
check('gain clamps at -40', gain(-40, 100).value === -40 && gain(-40, 100).changed === false, JSON.stringify(gain(-40, 100)));
check('phase wraps up past 360 to 0.0', phase(359.9, -100).value === 0, JSON.stringify(phase(359.9, -100)));
check('phase wraps down past 0 to 359.9', phase(0, 100).value === 359.9, JSON.stringify(phase(0, 100)));
check('phase keeps 0.1 resolution', phase(12.3, -100).value === 12.4, JSON.stringify(phase(12.3, -100)));
check('phase Shift is 1 deg', phase(12.3, -100, 0, 0, true).value === 13.3, JSON.stringify(phase(12.3, -100, 0, 0, true)));

console.log('accumulator direction');
const up = cancelWheelAccumulate(0, -20, 0, TRIGGER);
check('negative deltaY is up (positive steps)', up.steps === 1, JSON.stringify(up));
const down = cancelWheelAccumulate(0, 20, 0, TRIGGER);
check('positive deltaY is down (negative steps)', down.steps === -1, JSON.stringify(down));
check('a sub-threshold move makes no step', cancelWheelAccumulate(0, -5, 0, TRIGGER).steps === 0, '');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
