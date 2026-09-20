#!/usr/bin/env node
/* Waterfall palettes, trace colours and the spectrum-slider wheel gate,
 * all extracted from dashboard/console.html so there is no second copy.
 *     node ui-redesign/test_palettes.js
 */
'use strict';
const fs = require('fs'), path = require('path'), cp = require('child_process');
const FILE = path.join(__dirname, '..', 'dashboard', 'console.html');
const html = fs.readFileSync(FILE, 'utf8');

function objSource(name) {                 // `const NAME = { ... };`
  const i = html.indexOf('const ' + name + ' = {');
  if (i < 0) throw new Error('not found: ' + name);
  let d = 0;
  for (let j = html.indexOf('{', i); j < html.length; j++) {
    if (html[j] === '{') d++;
    else if (html[j] === '}') { d--; if (!d) return html.slice(i, j + 1) + ';'; }
  }
  throw new Error('unbalanced: ' + name);
}
function block(a, b) {
  const i = html.indexOf(a), j = html.indexOf(b, i);
  if (i < 0 || j < 0) throw new Error('block not found: ' + a);
  return html.slice(i, j);
}
const env = {};
new Function('exports', objSource('PALETTES') + objSource('PALETTES2')
  + block('/* ── V2TRACE:BEGIN', '/* V2TRACE:END */')
  + '\nexports.P = PALETTES; exports.P2 = PALETTES2;'
  + 'exports.v2TraceColour = v2TraceColour; exports.v2TraceFill = v2TraceFill;'
  + 'exports.v2PaletteGradient = v2PaletteGradient;'
  + 'exports.V2_TRACE_COLOURS = V2_TRACE_COLOURS;')(env);
const { P, P2, v2TraceColour, v2TraceFill, v2PaletteGradient, V2_TRACE_COLOURS } = env;

let pass = 0, fail = 0;
const ok = (n, c) => { if (c) { pass++; console.log('  ok   ' + n); }
                       else { fail++; console.log('  FAIL ' + n); } };
const eq = (n, g, w) => ok(n + ' = ' + JSON.stringify(g), JSON.stringify(g) === JSON.stringify(w));

console.log('the two lists are the same list');
eq('same names', Object.keys(P).sort(), Object.keys(P2).sort());
ok('every entry identical stop-for-stop',
   Object.keys(P).every(k => JSON.stringify(P[k]) === JSON.stringify(P2[k])));

console.log('the original five are untouched');
// Stops as they were at ui-v2-stage4b-final, read back out of git.
const before = cp.execSync('git show ui-v2-stage4b-final:dashboard/console.html',
  { cwd: path.join(__dirname, '..'), maxBuffer: 1 << 26 }).toString();
const bi = before.indexOf('const PALETTES = {');
let d = 0, bEnd = 0;
for (let j = before.indexOf('{', bi); j < before.length; j++) {
  if (before[j] === '{') d++;
  else if (before[j] === '}') { d--; if (!d) { bEnd = j + 1; break; } }
}
const oldEnv = {};
new Function('exports', before.slice(bi, bEnd) + ';exports.P = PALETTES;')(oldEnv);
for (const k of ['default', 'linrad', 'grayscale', 'hot', 'gqrx']) {
  eq('  ' + k, P[k], oldEnv.P[k]);
}

console.log('every palette is well formed');
const NEW = ['turbo', 'viridis', 'inferno', 'magma', 'plasma', 'amber', 'green'];
ok('all seven new palettes present', NEW.every(k => Array.isArray(P[k])));
for (const [name, stops] of Object.entries(P)) {
  let good = stops.length >= 2
    && stops[0][0] === 0 && Math.abs(stops[stops.length - 1][0] - 1) < 1e-9;
  for (let i = 0; i < stops.length; i++) {
    const [pos, r, g, b] = stops[i];
    if (!(pos >= 0 && pos <= 1)) good = false;
    if (i && pos <= stops[i - 1][0]) good = false;             // strictly ascending
    if (![r, g, b].every(v => Number.isInteger(v) && v >= 0 && v <= 255)) good = false;
    if (stops[i].length !== 4) good = false;
  }
  ok('  ' + name + ' (' + stops.length + ' stops, ascending 0..1, rgb in range)', good);
}

console.log('dbToColor over every palette');
// The real function, with currentPalette/wfContrast supplied.
const dbSrc = html.slice(html.indexOf('function dbToColor(db, floor, ceil)'));
const dbFn = dbSrc.slice(0, dbSrc.indexOf('\n}') + 2);
for (const name of Object.keys(P)) {
  const f = new Function('PALETTES', 'currentPalette', 'wfContrast',
    dbFn + '\nreturn dbToColor;')(P, name, 1.5);
  let good = true;
  for (const db of [-120, -90, -60]) {
    const c = f(db, -120, -60);
    if (!(Array.isArray(c) && c.length === 3
          && c.every(v => typeof v === 'number' && isFinite(v) && v >= 0 && v <= 255))) good = false;
  }
  ok('  ' + name + ' returns valid rgb at floor/mid/ceil', good);
}

console.log('trace colours');
eq('default is today’s exact colour', v2TraceColour('ice'), '#67D0F0');
eq('fill keeps today’s alpha', v2TraceFill('ice'), 'rgba(103, 208, 240, 0.25)');
ok('six choices', Object.keys(V2_TRACE_COLOURS).length === 6);
ok('unknown name falls back to ice', v2TraceColour('nope') === '#67D0F0');
ok('gradient uses every stop', v2PaletteGradient(P.viridis).split(',').length >= 10);

console.log('spectrum-slider wheel gate');
// Extract the handler body and run it against a fake event/element.
const wSrc = html.slice(html.indexOf("['span-slider', 'floor-slider', 'gain-slider', 'avg-slider'].forEach"));
const body = wSrc.slice(wSrc.indexOf("el.addEventListener('wheel', e => {") + "el.addEventListener('wheel', e => {".length,
                        wSrc.indexOf('}, { passive: false });'));
const handler = new Function('el', 'e', body);
function trial(evt) {
  const el = { disabled: false, step: '1', min: '0', max: '100', value: '50',
               dispatchEvent() { this._fired = true; }, _fired: false };
  let prevented = false;
  handler(el, Object.assign({ deltaY: 0, deltaX: 0, shiftKey: false,
                              preventDefault() { prevented = true; } }, evt));
  return { value: el.value, prevented, fired: el._fired };
}
let r = trial({ deltaY: -100 });
ok('no shift: value unchanged', r.value === '50');
ok('no shift: preventDefault NOT called', r.prevented === false);
r = trial({ deltaY: -100, shiftKey: true });
ok('shift+deltaY: value changed', Number(r.value) === 51);
ok('shift+deltaY: preventDefault called', r.prevented === true);
r = trial({ deltaX: -100, shiftKey: true });
ok('shift+deltaX (macOS): value changed', Number(r.value) === 51);
ok('shift+deltaX: preventDefault called', r.prevented === true);
r = trial({ deltaY: 100, shiftKey: true });
ok('shift down: value decreases', Number(r.value) === 49);
r = trial({ shiftKey: true });
ok('shift with no delta: no change', r.value === '50');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
