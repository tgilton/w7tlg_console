#!/usr/bin/env node
/* End-to-end checks of the AF GAIN send and state-sync paths, for BOTH
 * receivers, against the real source in dashboard/console.html.
 *
 * The two send functions and the two sync paths are extracted by name and
 * run against a fake send() and a fake DOM, so a taper applied in one
 * receiver and forgotten in the other cannot pass — which is exactly the
 * bug this file was written after (RX2's setRxVolume still sent
 * position/100 while its label said 100%).
 *
 *     node ui-redesign/test_af_paths.js
 */
'use strict';
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'console.html'), 'utf8');

function block(startMark, endMark) {
  const a = html.indexOf(startMark), b = html.indexOf(endMark, a);
  if (a < 0 || b < 0) throw new Error('block not found: ' + startMark);
  return html.slice(a, b);
}
const taper = block('/* ── V2AF:BEGIN', '/* V2AF:END */');

// Pull a named function's source out of the file, brace-matched.
function fnSource(name, from) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(', 'g');
  re.lastIndex = from || 0;
  const m = re.exec(html);
  if (!m) throw new Error('function not found: ' + name);
  let i = html.indexOf('{', m.index), depth = 0;
  for (let j = i; j < html.length; j++) {
    if (html[j] === '{') depth++;
    else if (html[j] === '}') { depth--; if (!depth) return html.slice(m.index, j + 1); }
  }
  throw new Error('unbalanced: ' + name);
}

// RigControl's are the later definitions; Panadapter2's come first.
const rx2Send = fnSource('setRxVolume');
const rx1Send = fnSource('setRxVolume', html.indexOf(rx2Send) + rx2Send.length);
const rx2Sync = fnSource('updateAfGain2');

let pass = 0, fail = 0;
function near(n, got, want, eps) {
  eps = eps === undefined ? 1e-9 : eps;
  if (Math.abs(got - want) <= eps) { pass++; console.log(`  ok   ${n} = ${got}`); }
  else { fail++; console.log(`  FAIL ${n}: got ${got}, want ${want}`); }
}
function eq(n, got, want) {
  if (got === want) { pass++; console.log(`  ok   ${n} = ${JSON.stringify(got)}`); }
  else { fail++; console.log(`  FAIL ${n}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
}

// ── send paths ────────────────────────────────────────────────────────
function makeSender(src, sendName) {
  const sent = [];
  const fn = new Function(sendName, 'v2AfPosToGain', 'v2AfGainToPos',
    taper + '\n' + src + '\nreturn setRxVolume;')(
      m => sent.push(m), undefined, undefined);
  return { fn, sent };
}
for (const [label, src, sendName] of [['RX1', rx1Send, 'send'], ['RX2', rx2Send, 'send2']]) {
  console.log(label + ' send path');
  const { fn, sent } = makeSender(src, sendName);
  fn(316); near('  pos 316 -> gain ~1.0', sent.at(-1).gain, 1.0, 0.01);
  fn(1000); near('  pos 1000 -> gain 10.0', sent.at(-1).gain, 10.0);
  fn(0); near('  pos 0 -> gain 0', sent.at(-1).gain, 0);
  eq('  cmd is set_rx_volume', sent.at(-1).cmd, 'set_rx_volume');
  fn(632); near('  pos 632 -> gain ~4.0 (the old default)', sent.at(-1).gain, 4.0, 0.02);
}

// ── sync paths ────────────────────────────────────────────────────────
function fakeDom(ids) {
  const els = {};
  ids.forEach(id => { els[id] = { value: null, textContent: null }; });
  return { els, getElementById: id => els[id] || null };
}
function runSync(label, src, sliderId, valId, volKey, dragKey) {
  console.log(label + ' state sync');
  const dom = fakeDom([sliderId, valId]);
  const RigControl = { isDragging: k => RigControl._drag === k, _drag: null };
  const fn = new Function('document', 'RigControl', 'v2AfPosToGain', 'v2AfGainToPos',
    taper + '\n' + src + '\nreturn ' + (src.match(/function\s+(\w+)/)[1]) + ';')(
      dom, RigControl, undefined, undefined);

  fn({ [volKey]: 1.0 });
  near('  server 1.0 -> position 316', dom.els[sliderId].value, 316);
  eq('  ...and label "100%"', dom.els[valId].textContent, '100%');

  fn({ [volKey]: 4.0 });
  near('  server 4.0 -> position 632', dom.els[sliderId].value, 632);
  eq('  ...and label "400%"', dom.els[valId].textContent, '400%');

  fn({ [volKey]: 10.0 });
  near('  server 10.0 -> position 1000', dom.els[sliderId].value, 1000);
  eq('  ...and label "1000%"', dom.els[valId].textContent, '1000%');

  // dragging must not stomp the operator's hand
  dom.els[sliderId].value = 999;
  RigControl._drag = dragKey;
  fn({ [volKey]: 1.0 });
  near('  ignored while dragging', dom.els[sliderId].value, 999);
  RigControl._drag = null;

  // absent value must not blank it
  dom.els[sliderId].value = 42;
  fn({});
  near('  absent value leaves the slider alone', dom.els[sliderId].value, 42);
}

// RX1's sync is inline in updateUI, so assert on its source text instead.
console.log('RX1 state sync (inline in updateUI)');
const rx1SyncSrc = html.slice(html.indexOf("if (!isDragging('rxVol') && rig.sdr_rx_volume"));
const rx1Chunk = rx1SyncSrc.slice(0, rx1SyncSrc.indexOf('\n  }') + 4);
for (const [name, needle] of [
  ['  uses the inverse taper', 'v2AfGainToPos(volPct)'],
  ['  writes the slider position', "getElementById('rx-vol-slider').value"],
  ['  writes the percent label', "rx-vol-val').textContent = volPct + '%'"],
  ['  guarded by isDragging', "isDragging('rxVol')"]]) {
  if (rx1Chunk.includes(needle)) { pass++; console.log(`  ok  ${name}`); }
  else { fail++; console.log(`  FAIL ${name}: ${needle} not found`); }
}

runSync('RX2', rx2Sync, 'rx-vol2-slider', 'rx-vol2-val', 'sdr_rx_volume_b', 'rxVol2');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
