#!/usr/bin/env node
/* Load dashboard/console.html in a real browser and assert it actually works.
 *
 * Every other check in this repo is static: node --check sees syntax, the
 * inventory sees markup, the renders see layout. None of them can see a
 * ReferenceError thrown inside Panadapter.init() — which is exactly what
 * shipped in stage 5 and left the operator with no audio, no spectrum, no
 * waterfall and dead controls. This is the check that catches that class
 * of fault, and it must pass before any commit that touches console.html.
 *
 * No npm. It drives the Chrome that is already on the machine, with a
 * throwaway profile, and reads results back out of the dumped DOM.
 *
 *     node ui-redesign/smoke_console.js [path/to/console.html]
 */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync, spawnSync } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const target = process.argv[2] || path.join(__dirname, '..', 'dashboard', 'console.html');

// Ids the page cannot work without. Kept deliberately short: this is a
// smoke test, not a schema.
const REQUIRED_IDS = [
  'vfo', 'vfo2', 'spectrum-canvas', 'waterfall-canvas',
  'spectrum2-canvas', 'waterfall2-canvas',
  'smeter-db', 'smeter-label', 'smeter2-db', 'smeter2-label',
  'span-slider', 'floor-slider', 'gain-slider', 'avg-slider',
  'span2-slider', 'floor2-slider', 'gain2-slider', 'avg2-slider',
  'rx-vol-slider', 'rx-vol2-slider', 'rf-gain-slider', 'rf-gain2-slider',
  'width-slider', 'width2-slider', 'nr-slider', 'nr2-slider',
  'freq-input', 'freq2-input', 'session-grid', 'box-antenna',
  'panel-tray', 'box-abtest', 'box-noise', 'rf-power-slider',
];

const PROLOGUE = `<script>
window.__smokeErrors = [];
window.addEventListener('error', function (e) {
  window.__smokeErrors.push((e.message || 'error') + ' @' + (e.lineno || '?'));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__smokeErrors.push('unhandledrejection: ' + (e.reason && e.reason.message || e.reason));
});
</script>`;

const EPILOGUE = `<script>
(function () {
  var out = { errors: window.__smokeErrors.slice(), modules: {}, handlers: [], missingIds: [] };
  // Indirect eval, NOT window[name]: console.html declares its modules
  // with a top-level const, which lives in the global LEXICAL environment
  // and is never a property of window. Inline on* handlers resolve it
  // through the scope chain, so window[name] would report every module
  // missing even on a perfectly healthy page.
  function g(expr) { try { return (0, eval)(expr); } catch (e) { return undefined; } }
  ['Panadapter', 'Panadapter2', 'RigControl'].forEach(function (m) {
    out.modules[m] = g('typeof ' + m);
  });
  var REQ = ${JSON.stringify(REQUIRED_IDS)};
  REQ.forEach(function (id) { if (!document.getElementById(id)) out.missingIds.push(id); });

  // Every on* attribute that calls into a module must resolve to a real
  // function. This is what catches a handler renamed or never exported.
  var seen = {};
  var all = document.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    for (var j = 0; j < el.attributes.length; j++) {
      var a = el.attributes[j];
      if (a.name.slice(0, 2) !== 'on') continue;
      var re = /\\b(RigControl|Panadapter2|Panadapter)\\.([A-Za-z_$][\\w$]*)/g, m;
      while ((m = re.exec(a.value))) {
        var key = m[1] + '.' + m[2];
        if (seen[key]) continue;
        seen[key] = true;
        var ok = g('typeof ' + m[1] + ' === "object" && typeof ' + m[1] + '["' + m[2] + '"] === "function"') === true;
        out.handlers.push({ call: key, ok: ok, where: (el.id || el.tagName) + '@' + a.name });
      }
    }
  }
  // Bare global handlers used from markup (tray, column, section helpers).
  ['toggleTray', 'selectTrayTab', 'toggleColumn', 'toggleSectionBox',
   'startColumnResize', 'resetColumnWidth'].forEach(function (fn) {
    out.handlers.push({ call: fn, ok: g('typeof ' + fn) === 'function', where: 'global' });
  });

  var pre = document.createElement('pre');
  pre.id = '__smoke';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>`;

function run(file) {
  let html = fs.readFileSync(file, 'utf8');
  if (!html.includes('<body>')) throw new Error('no <body> in ' + file);
  html = html.replace('<body>', '<body>' + PROLOGUE);
  html = html.replace('</body>', EPILOGUE + '</body>');

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'smoke-'));
  const page = path.join(dir, 'page.html');
  fs.writeFileSync(page, html);
  const profile = path.join(dir, 'prof');

  const r = spawnSync(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--host-resolver-rules=MAP * ~NOTFOUND',
    '--user-data-dir=' + profile,
    '--enable-logging=stderr', '--virtual-time-budget=3000',
    '--dump-dom', 'file://' + page,
  ], { encoding: 'utf8', maxBuffer: 1 << 28, timeout: 90000 });

  try { execFileSync('pkill', ['-f', 'user-data-dir=' + profile], { stdio: 'ignore' }); } catch (e) {}

  const dom = r.stdout || '';
  const stderr = r.stderr || '';
  const m = dom.match(/<pre id="__smoke">([\s\S]*?)<\/pre>/);
  let probe = null;
  if (m) {
    try {
      probe = JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&')
                              .replace(/&lt;/g, '<').replace(/&gt;/g, '>'));
    } catch (e) { probe = null; }
  }

  // Console messages Chrome logged. Two families are tolerated, because
  // both are artefacts of loading the page from file:// rather than from
  // the server, and both happen on a known-good build:
  //   - WebSocket connects: there is no server here by design.
  //   - audio-worklet.js: a worklet module cannot be fetched cross-origin
  //     from file://, so the audio path always reports this. The page
  //     catches it and retries on the next click, which is why it is a
  //     logged message rather than a crash.
  // Everything else is a real fault. Keep this list short — every entry
  // is a class of bug this test can no longer see.
  const TOLERATED =
    /websocket|ERR_NAME_NOT_RESOLVED|net::ERR|Failed to load resource|audio-worklet\.js|worklet module script|worklet setup failed|CORS policy/i;
  const consoleErrs = stderr.split('\n')
    .filter(l => /INFO:CONSOLE|ERROR:CONSOLE/.test(l))
    .filter(l => !TOLERATED.test(l))
    .map(l => l.replace(/^.*?CONSOLE[:(]?/, '').trim());

  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
  return { probe, consoleErrs, gotDom: !!dom };
}

const TOLERATED_IN_PAGE = /websocket|worklet|AbortError|CORS/i;

function report(label, res) {
  const lines = [];
  let ok = true;
  if (!res.probe) {
    ok = false;
    lines.push('  ** probe did not run — the page failed to reach the end of <body>');
  } else {
    const p = res.probe;
    const uncaught = p.errors.filter(e => !TOLERATED_IN_PAGE.test(e));
    if (uncaught.length) { ok = false; uncaught.forEach(e => lines.push('  ** uncaught: ' + e)); }
    for (const [name, t] of Object.entries(p.modules)) {
      if (t !== 'object') { ok = false; lines.push(`  ** typeof ${name} === '${t}', expected 'object'`); }
    }
    const bad = p.handlers.filter(h => !h.ok);
    if (bad.length) {
      ok = false;
      bad.forEach(h => lines.push(`  ** handler missing: ${h.call}  (${h.where})`));
    }
    if (p.missingIds.length) {
      ok = false;
      lines.push('  ** missing ids: ' + p.missingIds.join(', '));
    }
    lines.push(`  checked ${p.handlers.length} handler references, ${Object.keys(p.modules).length} modules`);
  }
  if (res.consoleErrs.length) {
    ok = false;
    res.consoleErrs.slice(0, 10).forEach(e => lines.push('  ** console: ' + e));
  }
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label);
  lines.forEach(l => console.log(l));
  return ok;
}

const res = run(target);
process.exit(report(target, res) ? 0 : 1);
