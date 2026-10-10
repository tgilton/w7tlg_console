#!/usr/bin/env node
/* Unit tests for the latency control in dashboard/audio-worklet.js.
 * Evaluates the real file against stubs of the AudioWorklet globals, so there
 * is no second copy.
 *     node ui-redesign/test_audio_latency.js
 */
'use strict';
const fs = require('fs'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'audio-worklet.js'), 'utf8');

const BLOCK = 128;   // render quantum

/* One processor at the given context rate, plus a driver that feeds it source audio in
 * real time (16 kHz in for every block out) and records what it does. */
function make(contextRate) {
  let Proc = null;
  const posted = [];
  class AudioWorkletProcessor {
    constructor() { this.port = { onmessage: null, postMessage: m => posted.push(m) }; }
  }
  const consts = new Function('AudioWorkletProcessor', 'registerProcessor', 'sampleRate',
    src + '\nreturn { TARGET_LATENCY_MS, MAX_LATENCY_MS, MAX_CATCHUP_RATE,'
        + ' CATCHUP_SLEW_PER_BLOCK, LATENCY_REPORT_MS };')(
    AudioWorkletProcessor, (_name, cls) => { Proc = cls; }, contextRate);
  const p = new Proc();
  const out = new Float32Array(BLOCK);
  let owed = 0;
  const h = {
    p, posted, consts, out,
    latencyMs: () => p.available / p.sourceRate * 1000,
    send: n => p.port.onmessage({ data: new Float32Array(n).fill(0.25) }),
    flush: () => p.port.onmessage({ data: { cmd: 'flush' } }),
    /* Run for `seconds`, feeding in real time unless feed is false. Returns the largest
     * catch-up rate seen and the largest change in it between two blocks. */
    run(seconds, feed) {
      let maxRate = 0, maxSlew = 0;
      const blocks = Math.round(seconds * contextRate / BLOCK);
      for (let b = 0; b < blocks; b++) {
        if (feed !== false) {
          owed += BLOCK * p.sourceRate / contextRate;
          const n = Math.floor(owed);
          owed -= n;
          h.send(n);
        }
        const before = p.catchupRate;
        p.process([], [[out]]);
        maxRate = Math.max(maxRate, p.catchupRate);
        maxSlew = Math.max(maxSlew, Math.abs(p.catchupRate - before));
      }
      return { maxRate, maxSlew };
    },
  };
  return h;
}

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log(`  ok   ${name}${detail ? ' — ' + detail : ''}`); }
  else { fail++; console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`); }
}

for (const rate of [48000, 44100]) {
  console.log(`context at ${rate} Hz`);
  let h = make(rate);
  const C = h.consts;
  ok('constants', C.TARGET_LATENCY_MS === 120 && C.MAX_LATENCY_MS === 250
    && C.MAX_CATCHUP_RATE === 0.05);

  console.log(' at the target: plays at real time');
  h.send(16 * C.TARGET_LATENCY_MS);
  let r = h.run(5);
  ok('no catch-up', r.maxRate === 0);
  ok('latency holds', Math.abs(h.latencyMs() - C.TARGET_LATENCY_MS) < 5, h.latencyMs().toFixed(1) + ' ms');

  console.log(' under the max (200 ms): left alone');
  h = make(rate);
  h.send(16 * 200);
  r = h.run(5);
  ok('no catch-up', r.maxRate === 0);
  ok('latency holds', Math.abs(h.latencyMs() - 200) < 5, h.latencyMs().toFixed(1) + ' ms');

  console.log(' over the max (400 ms): catches up to the target and stops there');
  h = make(rate);
  h.send(16 * 400);
  r = h.run(0.5);
  ok('running fast after 0.5 s', h.p.catchingUp && h.p.catchupRate > 0);
  const r2 = h.run(20);
  ok('rate never over +5%', Math.max(r.maxRate, r2.maxRate) <= C.MAX_CATCHUP_RATE + 1e-12,
    (Math.max(r.maxRate, r2.maxRate) * 100).toFixed(2) + '%');
  ok('rate glides, no step', Math.max(r.maxSlew, r2.maxSlew) <= C.CATCHUP_SLEW_PER_BLOCK + 1e-12);
  ok('back at real time', !h.p.catchingUp && h.p.catchupRate === 0);
  ok('settled at the target, not under it',
    h.latencyMs() <= C.TARGET_LATENCY_MS + 1 && h.latencyMs() > C.TARGET_LATENCY_MS - 20,
    h.latencyMs().toFixed(1) + ' ms');

  console.log(' a full 3 s ring: drains in about a minute at +5%');
  h = make(rate);
  h.send(16000 * 3);
  r = h.run(45);
  ok('still catching up at 45 s', h.p.catchingUp, h.latencyMs().toFixed(0) + ' ms');
  h.run(30);
  ok('at the target by 75 s', !h.p.catchingUp && h.latencyMs() <= C.TARGET_LATENCY_MS + 1,
    h.latencyMs().toFixed(1) + ' ms');

  console.log(' underrun: silence, as before');
  h = make(rate);
  h.out.fill(1);
  h.run(0.1, false);
  ok('output is zeros', h.out.every(v => v === 0));
  ok('no catch-up', h.p.catchupRate === 0);
  h.send(16 * 50);
  h.p.process([], [[h.out]]);
  ok('audio resumes', h.out[0] === 0.25);

  console.log(' flush');
  h = make(rate);
  h.send(16 * 400);
  h.run(0.5);
  h.flush();
  ok('buffer empty', h.p.available === 0);
  ok('catch-up cleared', !h.p.catchingUp && h.p.catchupRate === 0);

  console.log(' latency reports');
  h = make(rate);
  h.send(16 * 100);
  h.run(2.05);
  ok('one every 250 ms', h.posted.length === 8, h.posted.length + ' in 2 s');
  ok('integer ms near the buffered amount',
    h.posted.every(m => Number.isInteger(m.latencyMs)) && Math.abs(h.posted[7].latencyMs - 100) <= 5,
    'last = ' + h.posted[7].latencyMs);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
