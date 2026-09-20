#!/usr/bin/env node
/* Real-page wheel test (Amendment F16): loads the actual console.html in
 * headless Chrome and dispatches a genuine cancelable WheelEvent at every
 * slider after simulating a rested pointer, then reports what one notch
 * does in each slider's own units.
 *
 *   node ui-redesign/test_wheel_real.js
 */
'use strict';
const fs = require('fs'), os = require('os'), path = require('path');
const { execFileSync, spawnSync } = require('child_process');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const target = process.argv[2] || path.join(__dirname, '..', 'dashboard', 'console.html');

const IDS = [
  'span-slider','floor-slider','gain-slider','avg-slider',
  'span2-slider','floor2-slider','gain2-slider','avg2-slider',
  'width-slider','nr-slider','rx-vol-slider','rf-gain-slider',
  'eq-bass-slider','eq-mid-slider','eq-treble-slider',
  'width2-slider','nr2-slider','rx-vol2-slider','rf-gain2-slider',
  'eq2-bass-slider','eq2-mid-slider','eq2-treble-slider',
  'rf-power-slider','mic-gain-slider','comp-slider','dt-gain-slider',
];

const PROBE = `<script>
/* Timer chain, NOT a busy wait: under --virtual-time-budget the virtual
   clock does not advance inside a synchronous loop, so a busy wait on
   performance.now() never terminates. setTimeout does advance it. */
window.addEventListener('load', function () {
  var ids = ${JSON.stringify(IDS)};
  var out = [];
  var af = null;
  function step(i) {
    if (i >= ids.length) return finish();
    var id = ids[i], el = document.getElementById(id);
    if (!el) { out.push({ id: id, missing: true }); return step(i + 1); }
    var wasDisabled = el.disabled; if (wasDisabled) el.disabled = false;
    var before = parseFloat(el.value);
    var up = Number(el.value) >= Number(el.max);   // roll down at the ceiling
    function fire() {
      var ev = new WheelEvent('wheel', { deltaY: up ? 100 : -100, cancelable: true, bubbles: true });
      el.dispatchEvent(ev);
      return ev.defaultPrevented;
    }
    // 1. pointer arrives, wheel immediately -> must be blocked
    el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    var guardedPrevented = fire();
    var afterGuarded = parseFloat(el.value);
    // 2. let both guard windows expire, then wheel again -> must adjust
    setTimeout(function () {
      var restedPrevented = fire();
      var afterRested = parseFloat(el.value);
      out.push({ id: id, unit: (el.step || '1'), before: before,
                 guarded: afterGuarded, rested: afterRested,
                 perNotch: +(afterRested - afterGuarded).toFixed(4),
                 guardedBlocked: (afterGuarded === before) && !guardedPrevented,
                 restedPrevented: restedPrevented, wasDisabled: wasDisabled });
      el.value = String(before); if (wasDisabled) el.disabled = true;
      step(i + 1);
    }, 320);
  }
  function finish() {
    var el = document.getElementById('rx-vol-slider');
    if (el && typeof v2AfPosToGain === 'function') {
      var p0 = parseFloat(el.value);
      af = { atDefaultPos: p0, gainNow: +v2AfPosToGain(p0).toFixed(2),
             gainOneNotchUp: +v2AfPosToGain(p0 + 1).toFixed(2) };
    }
    var pre = document.createElement('pre'); pre.id = '__wheel';
    pre.textContent = JSON.stringify({ rows: out, af: af });
    document.body.appendChild(pre);
  }
  setTimeout(function () { step(0); }, 200);
});
</script>`;

let html = fs.readFileSync(target, 'utf8').replace('</body>', PROBE + '</body>');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wheel-'));
const page = path.join(dir, 'page.html');
fs.writeFileSync(page, html);
const profile = path.join(dir, 'prof');
const r = spawnSync(CHROME, ['--headless=new','--disable-gpu','--no-first-run',
  '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir=' + profile,
  '--window-size=1600,1400','--virtual-time-budget=30000','--dump-dom','file://' + page],
  { encoding: 'utf8', maxBuffer: 1 << 28, timeout: 120000 });
try { execFileSync('pkill', ['-f', 'user-data-dir=' + profile], { stdio: 'ignore' }); } catch (e) {}
const m = (r.stdout || '').match(/<pre id="__wheel">([\s\S]*?)<\/pre>/);
try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
if (!m) { console.error('probe did not run'); process.exit(1); }
const data = JSON.parse(m[1].replace(/&quot;/g, '"'));

let fail = 0;
console.log('id                  step     one notch   guarded blocked   preventDefault');
for (const row of data.rows) {
  if (row.missing) { console.log(`  ${row.id}  ** MISSING`); fail++; continue; }
  const okGuard = row.guardedBlocked, okPrev = row.restedPrevented, okMove = row.perNotch !== 0;
  if (!okGuard || !okPrev || !okMove) fail++;
  console.log(`  ${row.id.padEnd(18)} ${String(row.unit).padEnd(7)} `
    + `${String(row.perNotch).padStart(9)}   ${okGuard ? 'yes' : '** NO'}`.padEnd(24)
    + `${okPrev ? 'yes' : '** NO'}`);
}
if (data.af) {
  console.log(`\nAF GAIN at its default position ${data.af.atDefaultPos}: `
    + `${data.af.gainNow}% -> ${data.af.gainOneNotchUp}% per notch `
    + `(${(data.af.gainOneNotchUp - data.af.gainNow).toFixed(3)}% per notch)`);
}
console.log(fail ? `\n${fail} problems` : '\nall sliders adjust when rested and are blocked when not');
process.exit(fail ? 1 : 0);
