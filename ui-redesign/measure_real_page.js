#!/usr/bin/env node
/* Measure the REAL console.html in headless Chrome (Amendment F16).
 *
 * No rebuilt harness: the page is loaded as it ships, with its modules
 * and their init() running, so anything that only breaks at runtime shows
 * up here too. WebSocket failures are expected without the server.
 *
 *   node ui-redesign/measure_real_page.js <file> <width> [--tray=open|closed]
 */
'use strict';
const fs = require('fs'), os = require('os'), path = require('path');
const { execFileSync, spawnSync } = require('child_process');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const file = process.argv[2] || path.join(__dirname, '..', 'dashboard', 'console.html');
const width = parseInt(process.argv[3] || '1600', 10);
const trayArg = (process.argv.find(a => a.startsWith('--tray=')) || '').split('=')[1];
const shot = (process.argv.find(a => a.startsWith('--shot=')) || '').split('=')[1];
const scroll = parseInt((process.argv.find(a => a.startsWith('--scroll=')) || '').split('=')[1] || '0', 10);

const PROBE = `<script>
window.addEventListener('load', function () { setTimeout(function () {
  try {
    ${trayArg === 'open' ? "selectTrayTab('abtest',{silent:true}); setTrayCollapsed(false);" : ''}
    ${trayArg === 'closed' ? 'setTrayCollapsed(true);' : ''}
  } catch (e) {}
  setTimeout(function () {
    function box(sel) { var e = document.querySelector(sel); if (!e) return null;
      var r = e.getBoundingClientRect(); return { w: +r.width.toFixed(2), h: +r.height.toFixed(2) }; }
    function content(sel) { var e = document.querySelector(sel); if (!e) return 0;
      var k = e.children; if (!k.length) return 0;
      return Math.round(k[k.length - 1].getBoundingClientRect().bottom
        - e.getBoundingClientRect().top + parseFloat(getComputedStyle(e).paddingBottom)); }
    var out = {
      viewport: window.innerWidth,
      canvases: {
        spectrum: box('#spectrum-canvas'), waterfall: box('#waterfall-canvas'),
        spectrum2: box('#spectrum2-canvas'), waterfall2: box('#waterfall2-canvas') },
      shell: {
        topBar: box('.panel-status'), sysBar: box('.panel-sysmsgs'),
        tray: box('#panel-tray'),
        handleLeft: box('#resize-modedsp'), tabLeft: box('#tab-modedsp') },
      columns: { left: content('#panel-modedsp > .panel-body'),
                 centre: content('.panel-center'),
                 right: content('#panel-bandamp > .panel-body') },
      auto: (function () {
        var r = [];
        document.querySelectorAll('.v2-title-row').forEach(function (row) {
          var h = row.querySelector('h2'), c = row.querySelector('.v2-auto-chip');
          if (!h || !c) return;
          var hb = h.getBoundingClientRect(), cb = c.getBoundingClientRect();
          var overlap = !(cb.left >= hb.right - 0.5 || cb.right <= hb.left + 0.5
                       || cb.top >= hb.bottom - 0.5 || cb.bottom <= hb.top + 0.5);
          r.push({ title: h.textContent.trim().slice(0, 12),
                   chipTop: Math.round(cb.top + window.scrollY),
                   chipH: +cb.height.toFixed(1), overlapsTitle: overlap,
                   chipRightOfTitle: cb.left >= hb.right - 0.5 });
        });
        return r;
      })(),
      selects: ['palette-select','palette2-select','trace-select','trace2-select']
        .map(function (id) { var e = document.getElementById(id);
          if (!e) return { id: id, missing: true };
          var r = e.getBoundingClientRect(); var cs = getComputedStyle(e);
          return { id: id, h: +r.height.toFixed(1), font: cs.fontSize,
                   pad: cs.paddingLeft + '/' + cs.paddingRight,
                   arrow: cs.backgroundImage !== 'none' }; }),
      page: { w: +document.querySelector('.console').getBoundingClientRect().width.toFixed(2),
              h: Math.round(document.querySelector('.console').getBoundingClientRect().height),
              scrollW: document.documentElement.scrollWidth,
              hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth }
    };
    var sc = ' + scroll + ';
    if (sc) window.scrollTo(0, sc);
    var pre = document.createElement('pre'); pre.id = '__measure';
    pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
  }, 120);
}); });
</script>`;

let html = fs.readFileSync(file, 'utf8').replace('</body>', PROBE + '</body>');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'measure-'));
const page = path.join(dir, 'page.html');
fs.writeFileSync(page, html);
const profile = path.join(dir, 'prof');
const args = ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
  '--host-resolver-rules=MAP * ~NOTFOUND', '--user-data-dir=' + profile,
  '--window-size=' + width + ',' + (process.env.SMOKE_H || 1400), '--virtual-time-budget=4000'];
if (shot) args.push('--screenshot=' + shot);
args.push('--dump-dom', 'file://' + page);
const r = spawnSync(CHROME, args, { encoding: 'utf8', maxBuffer: 1 << 28, timeout: 90000 });
try { execFileSync('pkill', ['-f', 'user-data-dir=' + profile], { stdio: 'ignore' }); } catch (e) {}
const m = (r.stdout || '').match(/<pre id="__measure">([\s\S]*?)<\/pre>/);
try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
if (!m) { console.error('probe did not run'); process.exit(1); }
console.log(m[1].replace(/&quot;/g, '"'));
