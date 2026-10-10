#!/usr/bin/env node
/* Unit tests for the latency control (fast catch-up, standing-latency trim, re-prime) in dashboard/audio-worklet.js.
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
        + ' CATCHUP_SLEW_PER_BLOCK, LATENCY_REPORT_MS, STREAM_IDLE_MS, MIN_HEADROOM_MS,'
        + ' MAX_HEADROOM_MS, TRIM_RATE, TRIM_ENTER_OVER_MS, TRIM_EXIT_OVER_MS };')(
    AudioWorkletProcessor, (_name, cls) => { Proc = cls; }, contextRate);
  const p = new Proc();
  const out = new Float32Array(BLOCK);
  let owed = 0;
  const h = {
    p, posted, consts, out,
    lat: () => posted.filter(m => m.diag === undefined),
    diag: kind => posted.filter(m => m.diag === kind),
    latencyMs: () => p.available / p.sourceRate * 1000,
    send: n => p.port.onmessage({ data: new Float32Array(n).fill(0.25) }),
    flush: () => p.port.onmessage({ data: { cmd: 'flush' } }),
    /* Run for `seconds`, feeding in real time unless feed is false. A number for feed
     * delivers the same audio in bursts of that many ms (delivery jitter: the buffered
     * latency then swings by that much peak to peak). Returns the largest catch-up rate
     * seen, the largest change in it between two blocks, and the lowest latency. */
    run(seconds, feed) {
      let maxRate = 0, maxSlew = 0, minLat = Infinity;
      const burst = typeof feed === 'number' ? feed * p.sourceRate / 1000 : 1;
      const blocks = Math.round(seconds * contextRate / BLOCK);
      for (let b = 0; b < blocks; b++) {
        if (feed !== false) {
          owed += BLOCK * p.sourceRate / contextRate;
          if (owed >= burst) {
            const n = Math.floor(owed);
            owed -= n;
            h.send(n);
          }
        }
        const before = p.catchupRate;
        p.process([], [[out]]);
        maxRate = Math.max(maxRate, p.catchupRate);
        maxSlew = Math.max(maxSlew, Math.abs(p.catchupRate - before));
        minLat = Math.min(minLat, h.latencyMs());
      }
      return { maxRate, maxSlew, minLat };
    },
    /* Run (real-time feed) until a diag of this kind is posted, at most maxSeconds.
     * Returns what run() does, plus the seconds it took (-1 if it never came). */
    runUntil(kind, maxSeconds) {
      const had = h.diag(kind).length;
      const r = { maxRate: 0, maxSlew: 0, minLat: Infinity, seconds: -1 };
      for (let t = 0.05; t <= maxSeconds && r.seconds < 0; t += 0.05) {
        const q = h.run(0.05);
        r.maxRate = Math.max(r.maxRate, q.maxRate);
        r.maxSlew = Math.max(r.maxSlew, q.maxSlew);
        r.minLat = Math.min(r.minLat, q.minLat);
        if (h.diag(kind).length > had) r.seconds = t;
      }
      return r;
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
    && C.MAX_CATCHUP_RATE === 0.05 && C.MIN_HEADROOM_MS === 40 && C.MAX_HEADROOM_MS === 100
    && C.TRIM_RATE === 0.01);

  console.log(' at the target: real time (the trim needs 10 s of history before it acts)');
  h.send(16 * C.TARGET_LATENCY_MS);
  let r = h.run(5);
  ok('no catch-up', r.maxRate === 0);
  ok('latency holds', Math.abs(h.latencyMs() - C.TARGET_LATENCY_MS) < 5, h.latencyMs().toFixed(1) + ' ms');

  console.log(' under the max (200 ms): no fast catch-up; left alone for the first 10 s');
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
  const r2 = h.runUntil('catchup_exit', 30);
  const atExit = h.latencyMs();
  ok('rate never over +5%', Math.max(r.maxRate, r2.maxRate) <= C.MAX_CATCHUP_RATE + 1e-12,
    (Math.max(r.maxRate, r2.maxRate) * 100).toFixed(2) + '%');
  ok('rate glides, no step', Math.max(r.maxSlew, r2.maxSlew) <= C.CATCHUP_SLEW_PER_BLOCK + 1e-12);
  ok('catch-up over', !h.p.catchingUp);
  ok('catch-up entered and exited, each reported once with its latency',
    h.diag('catchup_enter').length === 1 && h.diag('catchup_exit').length === 1
    && h.diag('catchup_enter')[0].latencyMs > C.MAX_LATENCY_MS
    && h.diag('catchup_exit')[0].latencyMs <= C.TARGET_LATENCY_MS,
    `enter ${h.diag('catchup_enter')[0].latencyMs} ms, exit ${h.diag('catchup_exit')[0].latencyMs} ms`);
  ok('catch-up stopped at the target, not under it',
    atExit <= C.TARGET_LATENCY_MS + 1 && atExit > C.TARGET_LATENCY_MS - 20,
    atExit.toFixed(1) + ' ms');
  const r3 = h.run(20);
  ok('the trim takes it the rest of the way, then real time',
    !h.p.trimming && h.p.catchupRate === 0
    && Math.abs(h.latencyMs() - (C.MIN_HEADROOM_MS + C.TRIM_EXIT_OVER_MS)) < 5,
    h.latencyMs().toFixed(1) + ' ms');
  ok('still a glide through the hand-over', r3.maxSlew <= C.CATCHUP_SLEW_PER_BLOCK + 1e-12);

  console.log(' a full 3 s ring: drains in about a minute at +5%');
  h = make(rate);
  h.send(16000 * 3);
  r = h.run(45);
  ok('still catching up at 45 s', h.p.catchingUp, h.latencyMs().toFixed(0) + ' ms');
  h.run(30);
  ok('at the target by 75 s', !h.p.catchingUp && h.latencyMs() <= C.TARGET_LATENCY_MS + 1,
    h.latencyMs().toFixed(1) + ' ms');

  console.log(' start: silent until the ring holds the target');
  h = make(rate);
  h.send(16 * 50);
  h.p.process([], [[h.out]]);
  ok('50 ms buffered: still silent', h.out.every(v => v === 0) && h.p.priming);
  h.send(16 * 80);
  h.p.process([], [[h.out]]);
  ok('130 ms buffered: playing', h.out[0] === 0.25 && !h.p.priming);
  h.run(1.2);
  ok('start is not an underrun', h.diag('underrun').length === 0);

  console.log(' underrun: silence, then re-prime to the target before resuming');
  h = make(rate);
  h.send(16 * C.TARGET_LATENCY_MS);
  h.run(1);
  h.run(0.2, false);                          // a 200 ms delivery stall: the ring runs dry
  ok('ran dry', h.p.priming && h.p.starving && h.out.every(v => v === 0));
  h.send(16 * 50);
  h.p.process([], [[h.out]]);
  ok('50 ms back: still silent (the old code resumed here)', h.out.every(v => v === 0));
  h.send(16 * 80);
  h.p.process([], [[h.out]]);
  ok('at the target: audio resumes', h.out[0] === 0.25 && !h.p.priming);
  h.run(2);
  ok('latency back near the target', Math.abs(h.latencyMs() - C.TARGET_LATENCY_MS) < 15,
    h.latencyMs().toFixed(1) + ' ms');
  let u = h.diag('underrun');
  const starved = u.reduce((a, m) => a + m.starvedSamples, 0);
  ok('one underrun reported', u.reduce((a, m) => a + m.count, 0) === 1);
  ok('starved samples counted (~80 ms of silence + the two blocks above)',
    starved > rate * 0.07 && starved < rate * 0.1, starved + ' samples');
  ok('at most one underrun line per second', u.length <= 2);

  console.log(' a stream that stops is not starvation');
  h = make(rate);
  h.send(16 * C.TARGET_LATENCY_MS);
  h.run(1);
  h.run(10, false);                           // muted / TX: nothing arrives for 10 s
  u = h.diag('underrun');
  const idleStarved = u.reduce((a, m) => a + m.starvedSamples, 0);
  ok('counted only up to the idle cutoff', idleStarved <= rate * (C.STREAM_IDLE_MS + 10) / 1000,
    idleStarved + ' samples');
  ok('then no more underrun lines', u.length <= 2, u.length + ' lines in 10 s');

  console.log(' flush');
  h = make(rate);
  h.send(16 * 400);
  h.run(0.5);
  h.flush();
  ok('buffer empty', h.p.available === 0);
  ok('catch-up cleared', !h.p.catchingUp && h.p.catchupRate === 0);
  ok('reported with the latency it discarded',
    h.diag('flush').length === 1 && h.diag('flush')[0].latencyMs > 300);
  ok('re-primes without counting an underrun', h.p.priming && !h.p.starving);

  console.log(' trim: 150 ms of standing excess');
  const level = C.MIN_HEADROOM_MS + C.TRIM_EXIT_OVER_MS;   // where the trim stops
  h = make(rate);
  h.send(16 * (level + 150));
  r = h.run(9.5);
  ok('untouched until there are 10 s of history', r.maxRate === 0 && !h.p.trimming,
    h.latencyMs().toFixed(1) + ' ms');
  const tEnter = 9.5 + h.runUntil('trim_enter', 2).seconds;
  const tExit = tEnter + h.runUntil('trim_exit', 30).seconds;
  ok('trim entered at 10 s', Math.abs(tEnter - 10) < 0.2, tEnter.toFixed(2) + ' s');
  ok('converged ~15 s later (150 ms at +1%)', tExit - tEnter > 14 && tExit - tEnter < 17,
    `${(tExit - tEnter).toFixed(1)} s of trim, ${tExit.toFixed(1)} s from the start`);
  ok('at headroom + 5', Math.abs(h.latencyMs() - level) < 3, h.latencyMs().toFixed(1) + ' ms');
  r = h.run(30);
  ok('then real time, and it stays there', r.maxRate <= C.CATCHUP_SLEW_PER_BLOCK * 5
    && h.p.catchupRate === 0 && Math.abs(h.latencyMs() - level) < 3
    && h.diag('trim_enter').length === 1, h.latencyMs().toFixed(1) + ' ms');
  const te = h.diag('trim_enter')[0], tx = h.diag('trim_exit')[0];
  ok('enter and exit reported with M10 and the headroom',
    Math.abs(te.m10Ms - (level + 150)) <= 4 && te.headroomMs === C.MIN_HEADROOM_MS
    && tx.m10Ms <= level && tx.m10Ms >= level - 2 && tx.headroomMs === C.MIN_HEADROOM_MS,
    `enter m10=${te.m10Ms}, exit m10=${tx.m10Ms}, headroom=${tx.headroomMs}`);
  ok('no underrun, no fast catch-up', h.diag('underrun').length === 0
    && h.diag('catchup_enter').length === 0);

  console.log(' trim: never over +1%, eased in and out');
  h = make(rate);
  h.send(16 * 200);
  r = h.run(40);
  ok('rate never over +1%', r.maxRate <= C.TRIM_RATE + 1e-12, (r.maxRate * 100).toFixed(2) + '%');
  ok('rate glides, no step', r.maxSlew <= C.CATCHUP_SLEW_PER_BLOCK + 1e-12);

  console.log(' trim: 80 ms of jitter, peak to peak');
  h = make(rate);
  h.send(16 * 130);                // swings 130 -> 50 ms: the dips are inside headroom + 20
  r = h.run(60, 80);
  ok('dips at 50 ms: not trimmed', r.maxRate === 0 && h.diag('trim_enter').length === 0
    && Math.abs(r.minLat - 50) < 5, `lowest ${r.minLat.toFixed(1)} ms in 60 s`);
  ok('no underrun', h.diag('underrun').length === 0 && !h.p.priming);
  h = make(rate);
  h.send(16 * 280);                // same jitter riding 150 ms higher: 280 -> 200 ms
  r = h.run(60, 80);
  const dips = h.run(5, 80).minLat;
  ok('dips at 200 ms: trimmed', h.diag('trim_enter').length === 1
    && h.diag('trim_exit').length === 1 && !h.p.trimming);
  ok('until the dips are at headroom + 5, not the average', Math.abs(dips - level) < 5,
    `dips to ${dips.toFixed(1)} ms`);
  ok('no underrun', h.diag('underrun').length === 0 && !h.p.priming);

  console.log(' trim: an underrun raises the headroom');
  h = make(rate);
  h.send(16 * C.TARGET_LATENCY_MS);
  h.run(1);
  h.run(0.2, false);               // delivery stall: runs dry
  ok('not raised while still dry', h.p.headroomMs === C.MIN_HEADROOM_MS);
  h.send(16 * 130);
  h.p.process([], [[h.out]]);
  ok('raised by 20 ms when the stream resumes', h.p.headroomMs === C.MIN_HEADROOM_MS + 20);
  ok('history forgotten', h.p.bucketMins.length === 0 && !h.p.trimming);
  h.run(30);
  ok('trims to the raised level', !h.p.trimming
    && Math.abs(h.latencyMs() - (C.MIN_HEADROOM_MS + 20 + C.TRIM_EXIT_OVER_MS)) < 4,
    h.latencyMs().toFixed(1) + ' ms');
  ok('exit reports the raised headroom',
    h.diag('trim_exit')[0].headroomMs === C.MIN_HEADROOM_MS + 20);
  h.run(31);
  ok('decays 10 ms after 60 s without one', h.p.headroomMs === C.MIN_HEADROOM_MS + 10);
  h.run(60);
  ok('and back to the minimum, no lower', h.p.headroomMs === C.MIN_HEADROOM_MS);
  h.run(120);
  ok('stays at the minimum', h.p.headroomMs === C.MIN_HEADROOM_MS);
  for (let i = 0; i < 6; i++) {    // repeated stalls
    h.run(0.4, false);
    h.send(16 * 130);
    h.run(0.5);
  }
  ok('capped at 100 ms', h.p.headroomMs === C.MAX_HEADROOM_MS, h.p.headroomMs + ' ms');

  console.log(' trim: a stream that stops does not raise the headroom');
  h = make(rate);
  h.send(16 * C.TARGET_LATENCY_MS);
  h.run(1);
  h.run(3, false);                 // mute / TX
  h.send(16 * 130);
  h.run(1);
  ok('headroom unchanged', h.p.headroomMs === C.MIN_HEADROOM_MS && !h.p.priming);
  h.flush();
  h.send(16 * 130);
  h.run(1);
  ok('nor does a flush', h.p.headroomMs === C.MIN_HEADROOM_MS);
  h.send(16 * 100);
  h.run(15);
  h.flush();
  ok('a flush stops a trim and forgets the history', !h.p.trimming && h.p.bucketMins.length === 0);

  console.log(' latency reports');
  h = make(rate);
  h.send(16 * C.TARGET_LATENCY_MS);
  h.run(2.05);
  ok('one every 250 ms', h.lat().length === 8, h.lat().length + ' in 2 s');
  ok('integer ms, the instantaneous buffered amount',
    h.lat().every(m => Number.isInteger(m.latencyMs))
    && Math.abs(h.lat()[7].latencyMs - C.TARGET_LATENCY_MS) <= 5,
    'last = ' + h.lat()[7].latencyMs);

  console.log(' 5 s stats');
  h = make(rate);
  h.send(16 * C.TARGET_LATENCY_MS);
  h.run(10.1);
  let st = h.diag('stats');
  ok('one every 5 s', st.length === 2, st.length + ' in 10 s');
  ok('min <= mean <= max, around the target',
    st[1].minMs <= st[1].meanMs && st[1].meanMs <= st[1].maxMs
    && Math.abs(st[1].meanMs - C.TARGET_LATENCY_MS) <= 10,
    `${st[1].minMs}/${st[1].meanMs}/${st[1].maxMs} ms`);
  const blocksIn5s = Math.ceil(5 * rate / BLOCK);
  ok('frames and samples received in the window, and the headroom',
    Math.abs(st[1].frames - blocksIn5s) <= 2 && Math.abs(st[1].samples - 80000) <= 100
    && st[1].headroomMs === C.MIN_HEADROOM_MS,
    `${st[1].frames} frames, ${st[1].samples} samples`);
  h = make(rate);
  h.run(10.1, false);
  ok('a silent receiver posts no stats', h.diag('stats').length === 0);
  ok('and nothing else', h.posted.every(m => m.diag === undefined));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
