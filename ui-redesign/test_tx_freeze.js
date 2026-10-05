#!/usr/bin/env node
/* TX freeze of the RX1 display, on the REAL console.html in headless Chrome.
 *
 * The page is loaded as it ships, with a fake WebSocket in front of it. Frames keep
 * arriving on EVERY socket during a transmission (raw wideband, fine, processed,
 * ghost), which is the worst case: the server gates most of them, and this check
 * proves the page does not depend on that. Each frame carries one marker line whose
 * frequency says when it was made:
 *     dial + 1000 Hz   made before TX
 *     dial + 2000 Hz   made during TX (from 400 ms after PTT)
 *     dial +  500 Hz   made after TX
 * During TX the trace, the ghost and the waterfall must all still show the pre-TX
 * marker, and afterwards they must show the post-TX one. Four cases: COHERENT and
 * NOISE, in an SSB and in a digital session, GHOST on.
 *
 *     node ui-redesign/test_tx_freeze.js [console.html]
 *
 * This is the check that would have caught the NOISE-mode trace staying live in TX
 * (processed frames were stored with no rigPtt test; only the waterfall row was gated).
 */
'use strict';
const fs = require('fs'), os = require('os'), path = require('path');
const { spawnSync } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const target = process.argv[2] || path.join(__dirname, '..', 'dashboard', 'console.html');

const HEAD = `<script>
(function(){
  var NW = 65536, NF = 4096, RIG = 14074000;
  var C = window.__scn;
  window.__ptt = false; window.__pttAt = 0; window.__txDone = false;
  window.__cancel = {enabled: C.mode==='coherent', mode: C.mode, last_mode: C.mode, gain_db:0, phase_deg:0,
    noise_scale_db:-10, noise_n:2, noise_clamp:false, noise_status: C.mode==='noise'?'active':null, noise_lines:0,
    noise_floor1_db:-100, noise_false_pct:0.06, noise_beta_db:-20, noise_audio_on:false, ghost:true,
    follow_gain:true, rx2_locked:true, residual_span_db:null, residual_passband_db:null};
  function mark(){
    if (window.__ptt && Date.now() > window.__pttAt + 400) return RIG + 2000;
    return RIG + (window.__txDone ? 500 : 1000);
  }
  function wide(){ var b = new Float32Array(NW); for (var i=0;i<NW;i++) b[i] = -100 + Math.sin(i*12.9898)*0.5;
    var c = Math.round((mark() - (RIG - 1e6)) / (2e6/NW)); for (var k=-1;k<=1;k++) b[c+k] = -20; return b; }
  function fine(){ var b = new Float32Array(NF); for (var i=0;i<NF;i++) b[i] = -109 + Math.sin(i*7.1)*0.5;
    var c = Math.round((mark() - (RIG - 8000)) / (16000/NF)); for (var k=-3;k<=3;k++) b[c+k] = -20; return b; }
  function Fake(url){
    this.url = url; this.readyState = 1; var self = this;
    setTimeout(function(){ if (self.onopen) self.onopen(); }, 0);
    function send(kind, buf, span){ if (!self.onmessage) return;
      self.onmessage({data: JSON.stringify({type:'spectrum', kind:kind, ts:Date.now()/1000, center_freq_hz:RIG, span_hz:span,
        sample_rate_hz:span, bin_count:buf.length, groups:[], markers:[]})});
      self.onmessage({data: buf.buffer}); }
    if (/\\/ws$/.test(url)) this._t = setInterval(function(){ if (!self.onmessage) return;
        if (window.__ptt && !self._muted) { self._muted = true; self.onmessage({data: JSON.stringify({type:'tx_mute'})}); }
        if (!window.__ptt) self._muted = false;
        self.onmessage({data: JSON.stringify({type:'state', data:{
          rig:{freq_hz:RIG, mode: C.digital?'PKTUSB':'USB', is_digital:C.digital, ptt:window.__ptt, passband_hz:3000,
               sdr_rf_freq_hz_b:RIG, sdr_target_freq_hz_b:RIG, sdr_mode_b:'USB', sdr_bandwidth_hz_b:3000},
          cancel: window.__cancel}})}); }, 100);
    else if (/\\/ws\\/spectrum(_b)?$/.test(url)) this._t = setInterval(function(){ send('wide', wide(), 2e6); if (C.digital) send('fine', fine(), 16000); }, 60);
    else if (/\\/ws\\/spectrum_proc$/.test(url)) this._t = setInterval(function(){ if (C.mode==='noise') send('proc', wide(), 2e6); }, 60);
    else if (/\\/ws\\/spectrum_ghost$/.test(url)) this._t = setInterval(function(){ send('wide', wide(), 2e6); }, 60);
  }
  Fake.OPEN=1; Fake.CONNECTING=0; Fake.CLOSED=3; Fake.CLOSING=2;
  Fake.prototype.send = function(){}; Fake.prototype.close = function(){ clearInterval(this._t); if (this.onclose) this.onclose(); };
  window.WebSocket = Fake;
})();
</script>`;

const PROBE = `<script>
(function(){
  var RIG = 14074000;
  function later(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function freqAt(id, x, w){
    var c = Panadapter.getViewCenterHz(), s = Panadapter.getViewSpanHz(); return c - s/2 + (x / w) * s - RIG; }
  function name(hz){ if (hz === null) return 'none'; if (Math.abs(hz - 1000) < 200) return 'pre'; if (Math.abs(hz - 2000) < 200) return 'tx';
    if (Math.abs(hz - 500) < 200) return 'post'; return 'other ' + Math.round(hz); }
  function colourPeak(id, match){ var c = document.getElementById(id), ctx = c.getContext('2d');
    var d = ctx.getImageData(0,0,c.width,c.height).data, best = c.height, bx = -1;
    for (var x = 0; x < c.width; x++) { for (var y = 0; y < c.height; y++) { var k = (y*c.width+x)*4;
      if (match(d[k],d[k+1],d[k+2],d[k+3])) { if (y < best) { best = y; bx = x; } break; } } }
    return (bx < 0 || best > c.height*0.7) ? null : freqAt(id, bx, c.width); }
  function trace(id){ return name(colourPeak(id || 'spectrum-canvas', function(r,g,b,a){ return a > 150 && r > 70 && r < 140 && g > 170 && g < 235 && b > 215; })); }
  function ghost(){ return name(colourPeak('spectrum-canvas', function(r,g,b,a){ return a > 60 && r > 195 && g > 195 && b > 195 && Math.abs(r-b) < 25; })); }
  function wfTop(id){ var c = document.getElementById(id), ctx = c.getContext('2d'); var d = ctx.getImageData(0,0,c.width,1).data, best = 0, bx = -1;
    for (var x = 0; x < c.width; x++) { var v = (d[x*4]+d[x*4+1]+d[x*4+2])/3; if (v > best) { best = v; bx = x; } }
    return name(best < 60 ? null : freqAt(id, bx, c.width)); }
  function wfHash(id){ var c = document.getElementById(id), ctx = c.getContext('2d'); var d = ctx.getImageData(0,0,c.width,Math.min(40,c.height)).data, h = 0;
    for (var i = 0; i < d.length; i += 97) h = (h*31 + d[i]) >>> 0; return h; }
  function snap(){ return {trace: trace(), trace2: trace('spectrum2-canvas'), ghost: ghost(), wf: wfTop('waterfall-canvas'), wf2: wfTop('waterfall2-canvas')}; }
  window.addEventListener('load', async function(){
    var out = {};
    await later(4000);  out.before = snap();
    window.__pttAt = Date.now(); window.__ptt = true;
    await later(1500);  out.tx1 = snap(); var h1 = wfHash('waterfall-canvas'), h2 = wfHash('waterfall2-canvas');
    await later(3000);  out.tx2 = snap();
    out.scrolling = (wfHash('waterfall-canvas') !== h1); out.scrolling2 = (wfHash('waterfall2-canvas') !== h2);
    window.__txDone = true; window.__ptt = false;
    await later(2500);  out.after = snap();
    var pre = document.createElement('pre'); pre.id = 'tx-freeze-out'; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
  });
})();
</script>`;

const CASES = [
  ['SSB, COHERENT', { digital: false, mode: 'coherent' }],
  ['SSB, NOISE', { digital: false, mode: 'noise' }],
  ['digital, COHERENT', { digital: true, mode: 'coherent' }],
  ['digital, NOISE', { digital: true, mode: 'noise' }],
];

if (!fs.existsSync(CHROME)) { console.error('Chrome not found at ' + CHROME); process.exit(2); }
const base = fs.readFileSync(target, 'utf8');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'txfreeze-'));
let pass = 0, fail = 0;
function check(label, ok, detail) {
  if (ok) pass++; else { fail++; console.log(`  FAIL ${label}: ${detail}`); }
}
for (const [label, scn] of CASES) {
  const page = path.join(tmp, 'page.html');
  fs.writeFileSync(page, base.replace('<head>', '<head><script>window.__scn=' + JSON.stringify(scn) + ';</script>' + HEAD)
    .replace('</body>', PROBE + '</body>'));
  const r = spawnSync(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1600,2000',
    '--virtual-time-budget=26000', '--dump-dom', 'file://' + page], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  const m = /<pre id="tx-freeze-out">(.*?)<\/pre>/s.exec(r.stdout || '');
  if (!m) { fail++; console.log(`  FAIL ${label}: the probe produced no output`); continue; }
  const o = JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
  const line = k => `trace ${o[k].trace}, ghost ${o[k].ghost}, waterfall ${o[k].wf}; RX2 trace ${o[k].trace2}, waterfall ${o[k].wf2}`;
  console.log(`${label}\n    before TX: ${line('before')}\n    TX 1.5 s:  ${line('tx1')}\n    TX 4.5 s:  ${line('tx2')}\n    after TX:  ${line('after')}`);
  check(`${label}: live before TX`, o.before.trace === 'pre' && o.before.trace2 === 'pre' && o.before.wf === 'pre' && o.before.wf2 === 'pre', line('before'));
  for (const k of ['tx1', 'tx2']) {
    check(`${label}: trace frozen (${k})`, o[k].trace === 'pre', o[k].trace);
    check(`${label}: RX2 trace frozen (${k})`, o[k].trace2 === 'pre', o[k].trace2);
    check(`${label}: no ghost from a TX-time frame (${k})`, o[k].ghost !== 'tx', o[k].ghost);
    check(`${label}: RX1 waterfall frozen (${k})`, o[k].wf === 'pre', o[k].wf);
    check(`${label}: RX2 waterfall frozen (${k})`, o[k].wf2 === 'pre', o[k].wf2);
  }
  check(`${label}: waterfalls not scrolling in TX`, !o.scrolling && !o.scrolling2, `${o.scrolling} ${o.scrolling2}`);
  check(`${label}: traces resume after TX`, o.after.trace === 'post' && o.after.trace2 === 'post', `${o.after.trace} ${o.after.trace2}`);
  check(`${label}: waterfalls resume after TX`, o.after.wf === 'post' && o.after.wf2 === 'post', `${o.after.wf} ${o.after.wf2}`);
}
fs.rmSync(tmp, { recursive: true, force: true });
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
