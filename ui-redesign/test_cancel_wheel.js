#!/usr/bin/env node
/* Unit tests for CANCEL's wheel, step and grid logic in dashboard/console.html.
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
  + '\nexports.cancelWheelIsMouse = cancelWheelIsMouse;'
  + '\nexports.cancelWheelAccumulate = cancelWheelAccumulate;'
  + '\nexports.cancelWheelNext = cancelWheelNext;'
  + '\nexports.cancelWheelAllowed = cancelWheelAllowed;'
  + '\nexports.cancelGridRound = cancelGridRound;'
  + '\nexports.CHOICES = CANCEL_STEP_CHOICES;'
  + '\nexports.cancelBetaKey = cancelBetaKey;'
  + '\nexports.cancelBetaWheel = cancelBetaWheel;'
  + '\nexports.cancelAudioToggle = cancelAudioToggle;'
  + '\nexports.cancelModeLabel = cancelModeLabel;'
  + '\nexports.TRIGGER = CANCEL_WHEEL_TRIGGER_PX;'
  + '\nexports.SCROLL_MS = CANCEL_WHEEL_SCROLL_MS;')(sandbox);
const { cancelWheelIsMouse, cancelWheelAccumulate, cancelWheelNext, cancelWheelAllowed,
        cancelGridRound, CHOICES, TRIGGER, SCROLL_MS } = sandbox;

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}: ${detail}`); }
}
// Helpers: a wheel event, and the options the handler passes.
const ev = (deltaY, o = {}) => Object.assign({ deltaY, deltaMode: 0, wheelDeltaY: undefined, gapMs: Infinity }, o);
const opt = (kind, step, shift) => ({ kind, min: -40, max: 80, step, shift: !!shift });
const gain = (v, e, step = 0.5, shift = false, acc = 0) => cancelWheelNext(v, e, acc, opt('gain', step, shift));
const phase = (v, e, step = 0.5, shift = false, acc = 0) =>
  cancelWheelNext(v, e, acc, Object.assign(opt('phase', step, shift), { min: 0, max: 360 }));
const near = (x, y) => Math.abs(x - y) < 1e-9;

console.log('mouse notches: one physical notch is one step, whatever its deltaY');
for (const dy of [-3, -4, -12, -100, -120, -240, -400]) {
  const r = gain(10, ev(dy), 0.1);
  check(`deltaY ${dy} px (deltaMode 0) -> +0.1`, r.value === 10.1 && r.changed, JSON.stringify(r));
}
for (const dy of [3, 4, 12, 100, 120]) {
  const r = gain(10, ev(dy), 0.1);
  check(`deltaY +${dy} px -> -0.1`, r.value === 9.9, JSON.stringify(r));
}
check('line deltas (deltaMode 1) are a mouse notch', gain(10, ev(-3, { deltaMode: 1 }), 0.1).value === 10.1, '');
check('page deltas (deltaMode 2) are a mouse notch', gain(10, ev(-1, { deltaMode: 2 }), 0.1).value === 10.1, '');
check('legacy wheelDeltaY 120 is a mouse notch', gain(10, ev(-3, { wheelDeltaY: 120 }), 0.1).value === 10.1, '');
check('a notch after a quiet gap is a mouse notch', gain(10, ev(-3, { gapMs: 100 }), 0.1).value === 10.1, '');
check('three notches in a row are three steps, not one', (() => {
  let v = 0, acc = 0;
  for (let i = 0; i < 3; i++) { const r = cancelWheelNext(v, ev(-3, { gapMs: 60 }), acc, opt('gain', 0.1, false)); v = r.value; acc = r.acc; }
  return near(v, 0.3);
})(), '');

console.log('the selected step, and Shift');
for (const [k, step] of Object.entries(CHOICES)) {
  const r = gain(0, ev(-100), step);
  check(`${k} (${step}) notch -> +${step}`, near(r.value, step), JSON.stringify(r));
}
check('Shift multiplies the step by 5 (MED: +2.5)', near(gain(0, ev(-100), 0.5, true).value, 2.5), '');
check('Shift + COARSE -> +10', near(gain(0, ev(-100), 2.0, true).value, 10), '');
check('Shift down on FINE -> -0.5', near(gain(0, ev(100), 0.1, true).value, -0.5), '');
check('phase Shift + FINE -> +0.5 deg', near(phase(12.0, ev(-100), 0.1, true).value, 12.5), '');

console.log('trackpad: small pixel deltas accumulate, one step per event at most');
let acc = 0, v = 0, steps = 0, events = 0;
for (let i = 0; i < 40; i++) {                      // forty 3 px events, 8 ms apart
  const r = cancelWheelNext(v, ev(-3, { gapMs: 8 }), acc, opt('gain', 0.1, false));
  acc = r.acc; events++;
  if (r.changed) { steps++; v = r.value; }
}
check('40 x 3 px make 10 steps (one per 12 px)', steps === 10, `steps=${steps}`);
check('the value moved by exactly 0.1 per step', near(v, 1.0), `v=${v}`);
const big = cancelWheelNext(0, ev(-10000, { gapMs: 4 }), 0, opt('gain', 0.1, false));
check('one huge trackpad event is at most one step', near(big.value, 0.1) && Math.abs(big.acc) <= TRIGGER, JSON.stringify(big));
const carry = cancelWheelAccumulate(0, -10000, 0, TRIGGER);
check('the carry never banks more than one threshold', Math.abs(carry.acc) <= TRIGGER, JSON.stringify(carry));
check('a sub-threshold trackpad move is no step', cancelWheelAccumulate(0, -5, 0, TRIGGER).steps === 0, '');

console.log('rest gate: only a real page scroll passes the wheel to the page');
check('no scroll ever: allowed', cancelWheelAllowed(1000, -Infinity) === true, '');
check('scrolled 100 ms ago: blocked', cancelWheelAllowed(1000, 900) === false, '');
check(`scrolled ${SCROLL_MS} ms ago: allowed again`, cancelWheelAllowed(1000, 1000 - SCROLL_MS) === true, '');
let allowedWhileWheeling = true;
for (let t = 0; t < 2000; t += 16) if (!cancelWheelAllowed(t, -Infinity)) allowedWhileWheeling = false;
check('continuous wheeling over a control, no scroll: never dropped', allowedWhileWheeling, '');

console.log('wrap and clamp');
check('phase wraps up past 360 to 0', near(phase(359.5, ev(-100), 0.5).value, 0), JSON.stringify(phase(359.5, ev(-100), 0.5)));
check('phase wraps down past 0 to 359.5', near(phase(0, ev(100), 0.5).value, 359.5), JSON.stringify(phase(0, ev(100), 0.5)));
check('gain clamps at +80, and reports no change', gain(80, ev(-100), 0.5).value === 80 && gain(80, ev(-100), 0.5).changed === false, '');
check('gain clamps at -40', gain(-40, ev(100), 0.5).value === -40, '');
check('gain near the top clamps to 80', gain(79.8, ev(-100), 0.5).value === 80, JSON.stringify(gain(79.8, ev(-100), 0.5)));

console.log('grid: every value sits on the step grid, with no float drift');
let fv = 0; let fa = 0;
for (let i = 0; i < 10; i++) { const r = cancelWheelNext(fv, ev(-100, { gapMs: 60 }), fa, opt('gain', 0.1, false)); fv = r.value; fa = r.acc; }
check('ten FINE notches from 0 give exactly 1.0', fv === 1.0, `fv=${fv}`);
check('an off-grid value snaps to the MED grid, then steps', near(gain(10.3, ev(-100), 0.5).value, 10.5), JSON.stringify(gain(10.3, ev(-100), 0.5)));
check('an off-grid value snaps down on a down notch', near(gain(10.3, ev(100), 0.5).value, 10.0), JSON.stringify(gain(10.3, ev(100), 0.5)));
check('an off-grid value with no step is left alone', gain(10.3, ev(0), 0.5).changed === false, '');
check('COARSE snaps 0.1 up to 2.0', near(gain(0.1, ev(-100), 2.0).value, 2.0), JSON.stringify(gain(0.1, ev(-100), 2.0)));
check('cancelGridRound removes drift', cancelGridRound(0.30000000000000004, 0.1) === 0.3, '');
check('the step-choice table is FINE 0.1 / MED 0.5 / COARSE 2.0',
  CHOICES.fine === 0.1 && CHOICES.med === 0.5 && CHOICES.coarse === 2.0, JSON.stringify(CHOICES));

console.log('direction');
check('mouse wheel up (deltaY < 0) increases', gain(0, ev(-3)).value > 0, '');
check('mouse wheel down (deltaY > 0) decreases', gain(0, ev(3)).value < 0, '');
check('isMouse: a trackpad stream is not a mouse', cancelWheelIsMouse(ev(-3, { gapMs: 8 })) === false, '');

const { cancelBetaKey, cancelBetaWheel, cancelAudioToggle, cancelModeLabel } = sandbox;

console.log('AUDIO toggle: one switch, NOISE mode only');
let cmd = cancelAudioToggle({ mode: 'noise', noise_audio_on: false });
check('off -> on sends set_noise_audio on', cmd && cmd.cmd === 'set_noise_audio' && cmd.on === true, JSON.stringify(cmd));
cmd = cancelAudioToggle({ mode: 'noise', noise_audio_on: true });
check('on -> off sends set_noise_audio off', cmd && cmd.on === false, JSON.stringify(cmd));
check('no command in COHERENT mode', cancelAudioToggle({ mode: 'coherent', noise_audio_on: false }) === null, '');
check('no command when CANCEL is off', cancelAudioToggle({ mode: 'off', noise_audio_on: true }) === null, '');
check('there is no A/B field in the command', cmd && Object.keys(cmd).sort().join() === 'cmd,on', JSON.stringify(cmd));
check('short mode labels', cancelModeLabel('noise') === 'NOISE' && cancelModeLabel('coherent') === 'COH', '');

console.log('beta (max attenuation): arrow keys');
check('ArrowUp on MED: -20 -> -19.5', cancelBetaKey(-20, 'ArrowUp', 0.5, false) === -19.5, cancelBetaKey(-20, 'ArrowUp', 0.5, false));
check('ArrowDown on MED: -20 -> -20.5', cancelBetaKey(-20, 'ArrowDown', 0.5, false) === -20.5, '');
check('Shift is 5x: -20 -> -17.5', cancelBetaKey(-20, 'ArrowUp', 0.5, true) === -17.5, '');
check('FINE: -20 -> -19.9', cancelBetaKey(-20, 'ArrowUp', 0.1, false) === -19.9, '');
check('COARSE: -20 -> -22', cancelBetaKey(-20, 'ArrowDown', 2.0, false) === -22, '');
check('clamps at -6', cancelBetaKey(-6, 'ArrowUp', 2.0, true) === -6, '');
check('clamps at -40', cancelBetaKey(-39, 'ArrowDown', 2.0, true) === -40, '');
check('other keys are ignored', cancelBetaKey(-20, 'Enter', 0.5, false) === null, '');

console.log('beta: wheel');
const notch = d => ({ deltaY: d, deltaMode: 0, wheelDeltaY: undefined, gapMs: 60 });
check('one mouse notch up on MED: -20 -> -19.5', cancelBetaWheel(-20, notch(-3), 0, 0.5, false).value === -19.5, '');
check('one mouse notch down: -20 -> -20.5', cancelBetaWheel(-20, notch(3), 0, 0.5, false).value === -20.5, '');
check('Shift notch: -20 -> -22.5', cancelBetaWheel(-20, notch(100), 0, 0.5, true).value === -22.5, '');
check('wheel clamps at -6 and reports no change', cancelBetaWheel(-6, notch(-3), 0, 0.5, false).changed === false, '');
check('wheel clamps at -40', cancelBetaWheel(-40, notch(3), 0, 2.0, false).value === -40, '');
let bv = -20, ba = 0, bsteps = 0;
for (let i = 0; i < 40; i++) {
  const br = cancelBetaWheel(bv, { deltaY: -3, deltaMode: 0, gapMs: 8 }, ba, 0.5, false);
  ba = br.acc; if (br.changed) { bsteps++; bv = br.value; }
}
check('trackpad: 120 px is 10 steps of 0.5 (-20 -> -15)', bsteps === 10 && bv === -15, `steps=${bsteps} v=${bv}`);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
