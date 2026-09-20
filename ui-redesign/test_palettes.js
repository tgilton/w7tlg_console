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

console.log('wheel guard (pure)');
const guardSrc = block('/* ── V2WHEEL:BEGIN', '/* V2WHEEL:END */')
  .replace(/document\.addEventListener[\s\S]*?\}\);\n/g, '')   // listeners need a DOM
  .replace(/let _v2[\s\S]*?\n/g, '');
const genv = {};
new Function('exports', guardSrc
  + '\nexports.v2WheelShouldAdjust = v2WheelShouldAdjust;')(genv);
const guard = genv.v2WheelShouldAdjust;
ok('rested pointer adjusts', guard(10000, 0, 0) === true);
ok('100ms after a page scroll does NOT adjust', guard(10000, 9900, 0) === false);
ok('249ms after a page scroll does NOT adjust', guard(10000, 9751, 0) === false);
ok('251ms after a page scroll adjusts', guard(10000, 9749, 0) === true);
ok('50ms after entering does NOT adjust', guard(10000, 0, 9950) === false);
ok('119ms after entering does NOT adjust', guard(10000, 0, 9881) === false);
ok('121ms after entering adjusts', guard(10000, 0, 9879) === true);
ok('both windows expired adjusts', guard(10000, 9000, 9000) === true);
ok('never entered (-Infinity) adjusts', guard(10000, -Infinity, -Infinity) === true);
ok('both windows active does NOT adjust', guard(10000, 9950, 9950) === false);

console.log('the guard is defined before Panadapter.init');
ok('  ordering', html.indexOf('function v2WheelShouldAdjust') < html.indexOf('Panadapter.init();'));

console.log('every slider wheel handler, all 26 ids');
function handlerBodyAfter(marker) {
  const a2 = html.indexOf(marker);
  if (a2 < 0) throw new Error('marker not found: ' + marker);
  const s2 = html.slice(a2);
  const open = s2.indexOf("addEventListener('wheel', e => {");
  const body = s2.slice(open + "addEventListener('wheel', e => {".length);
  return body.slice(0, body.indexOf('}, { passive: false });'));
}
const BODIES = {
  'spectrum sliders': handlerBodyAfter("['span-slider', 'floor-slider', 'gain-slider', 'avg-slider']"),
  'RX2 sliders':      handlerBodyAfter("['width2-slider',    v => setWidth(v)"),
  'addWheelToSlider': handlerBodyAfter('function addWheelToSlider(id, sendFn, key)'),
};
// step and starting value per slider, as the page actually declares them
const SL = {
  'span-slider':[1,'0','1000','500'], 'floor-slider':[1,'-160','0','-90'],
  'gain-slider':[1,'0','100','50'],   'avg-slider':[1,'0','100','30'],
  'span2-slider':[1,'0','1000','500'],'floor2-slider':[1,'-160','0','-90'],
  'gain2-slider':[1,'0','100','50'],  'avg2-slider':[1,'0','100','30'],
  'width2-slider':[50,'200','3000','3000'], 'nr2-slider':[1,'1','15','1'],
  'rx-vol2-slider':[1,'0','1000','316'], 'rf-gain2-slider':[1,'0','6','5'],
  'eq2-bass-slider':[1,'-12','12','0'],'eq2-mid-slider':[1,'-12','12','0'],
  'eq2-treble-slider':[1,'-12','12','0'],
  'width-slider':[50,'200','3000','3000'], 'nr-slider':[1,'1','15','1'],
  'rx-vol-slider':[1,'0','1000','316'], 'rf-gain-slider':[1,'0','6','5'],
  'eq-bass-slider':[1,'-12','12','0'], 'eq-mid-slider':[1,'-12','12','0'],
  'eq-treble-slider':[1,'-12','12','0'], 'rf-power-slider':[1,'5','100','50'],
  'mic-gain-slider':[1,'0','100','50'], 'comp-slider':[1,'0','100','0'],
  'dt-gain-slider':[1,'0','100','15'],
};
const GROUP = {
  'spectrum sliders': ['span-slider','floor-slider','gain-slider','avg-slider',
                       'span2-slider','floor2-slider','gain2-slider','avg2-slider'],
  'RX2 sliders':      ['width2-slider','nr2-slider','rx-vol2-slider','rf-gain2-slider',
                       'eq2-bass-slider','eq2-mid-slider','eq2-treble-slider'],
  'addWheelToSlider': ['width-slider','nr-slider','rx-vol-slider','rf-gain-slider',
                       'eq-bass-slider','eq-mid-slider','eq-treble-slider',
                       'rf-power-slider','mic-gain-slider','comp-slider','dt-gain-slider'],
};
let nIds = 0;
for (const [group, body] of Object.entries(BODIES)) {
  const fn = new Function('el','e','key','sendFn','RigControl','setDragging',
                          'clearTimeout','setTimeout','v2WheelAllowed', body);
  for (const id of GROUP[group]) {
    nIds++;
    const [step, min, max, val] = SL[id];
    const mk = () => ({ id, disabled:false, step:String(step), min, max, value:val,
                        dispatchEvent(){ this._fired = true; }, _fired:false });
    const run = (el, allowed, evt) => { let p = false;
      fn(el, Object.assign({ deltaY:-100, preventDefault(){ p = true; } }, evt),
         'k', () => {}, { setDragging(){} }, () => {}, () => {}, () => 0, () => allowed);
      return p; };
    // width-slider and width2-slider ship at their max (3000), where a
    // wheel UP correctly clamps and moves nothing — so roll DOWN on any
    // slider that starts at its ceiling.
    const atMax = Number(val) >= Number(max);
    let el = mk();
    let prevented = run(el, true, { deltaY: atMax ? 100 : -100 });
    const moved = Math.abs(Number(el.value) - Number(val));
    ok(`  ${id}: rested wheel moves by its step (${step}${atMax ? ', rolled down' : ''})`,
       moved === step && prevented);
    el = mk(); prevented = run(el, false, {});
    ok(`  ${id}: guarded wheel changes nothing, no preventDefault`,
       el.value === val && prevented === false);
  }
}
ok(`all 26 slider ids covered (saw ${nIds})`, nIds === 26);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
