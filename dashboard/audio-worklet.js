// Runs on the dedicated audio rendering thread, immune to main-thread
// jank (canvas redraws, GC pauses, etc.) — the previous approach scheduled
// a new AudioBufferSourceNode per incoming WebSocket message, so any delay
// in handling that exact message produced an audible gap. Here, incoming
// PCM just gets written into a ring buffer; process() reads it out
// continuously regardless of when messages actually arrive.

// Latency control. Playback at exactly real time never works off a backlog,
// so anything that piles up in the ring stays as permanent lag behind the
// spectrum. Once the buffered audio passes MAX_LATENCY_MS the reader runs
// slightly fast (at most MAX_CATCHUP_RATE over real time, eased in and out)
// until it is back at TARGET_LATENCY_MS, then returns to real time.
const TARGET_LATENCY_MS = 120;
const MAX_LATENCY_MS = 250;
const MAX_CATCHUP_RATE = 0.05;       // +5% read step at most
const CATCHUP_SLEW_PER_BLOCK = 0.002; // rate change per render quantum: a glide, not a step
const LATENCY_REPORT_MS = 250;       // how often the buffered latency is posted to the page

// Re-prime. After an underrun (and at start, and after a flush) playback waits
// until the ring holds TARGET_LATENCY_MS again before resuming. Resuming on the
// first samples to arrive left the buffer near empty, so every later delivery
// stall became another gap.
//
// Diagnostics, posted to the page as { diag: ... } (it forwards them to the
// server log, see console.html). Events only, never per render quantum:
//   catchup_enter / catchup_exit   with the latency at that moment
//   flush                          with the latency that was discarded
//   underrun                       once a second, only if there were any: how many,
//                                  and how many output samples were silence
//   stats                          every STATS_INTERVAL_MS: min / mean / max latency
//                                  and the PCM frames and samples received
const UNDERRUN_REPORT_MS = 1000;
const STATS_INTERVAL_MS = 5000;
// No PCM for this long means the stream has stopped (mute, TX, NR off->on), not
// stalled: silence after that is not counted as starvation.
const STREAM_IDLE_MS = 500;

class PcmStreamProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.sourceRate = 16000;            // rate of the incoming demodulated audio
    this.ringSize = this.sourceRate * 3; // 3s of headroom — generous, cheap
    this.ring = new Float32Array(this.ringSize);
    this.writeIdx = 0;
    this.available = 0;   // unread source samples currently buffered
    this.readPos = 0;     // fractional read index into ring, source-sample units
    this.catchingUp = false;
    this.catchupRate = 0;       // current extra read rate, 0..MAX_CATCHUP_RATE
    this.framesSinceReport = 0;
    this.primeSamples = this.sourceRate * TARGET_LATENCY_MS / 1000;
    this.priming = true;        // waiting for the ring to refill before playing
    this.starving = false;      // that wait was caused by an underrun (not start/flush)
    this.framesSincePcm = 0;    // output frames since PCM last arrived
    // Diagnostics accumulators.
    this.underruns = 0;
    this.starvedFrames = 0;
    this.framesSinceUnderrunReport = 0;
    this.framesSinceStats = 0;
    this.statBlocks = 0;
    this.statSum = 0;
    this.statMin = Infinity;
    this.statMax = 0;
    this.pcmFrames = 0;
    this.pcmSamples = 0;

    this.port.onmessage = (evt) => {
      // Control commands arrive as plain objects; PCM arrives as Float32Array.
      if (evt.data && evt.data.cmd === 'flush') {
        // TX started — drop all buffered audio instantly.
        // Also align readPos to writeIdx: without this, the reader's position
        // is frozen mid-ring while writeIdx keeps moving. When RX audio arrives
        // after TX the reader starts from the OLD readPos, replaying stale
        // pre-TX audio in the ring as a series of echoes before finally
        // catching up to where fresh data is being written.
        this.port.postMessage({ diag: 'flush', latencyMs: this.latencyMs() });
        this.available = 0;
        this.readPos = this.writeIdx;
        this.catchingUp = false;
        this.catchupRate = 0;
        this.priming = true;
        this.starving = false;
        return;
      }
      const samples = evt.data;
      this.pcmFrames++;
      this.pcmSamples += samples.length;
      this.framesSincePcm = 0;
      for (let i = 0; i < samples.length; i++) {
        this.ring[this.writeIdx] = samples[i];
        this.writeIdx = (this.writeIdx + 1) % this.ringSize;
      }
      // Ring overwrite already drops the oldest data on overflow; just
      // keep the bookkeeping in bounds to match.
      this.available = Math.min(this.ringSize, this.available + samples.length);
    };
  }

  latencyMs() {
    return Math.round(Math.max(0, this.available) / this.sourceRate * 1000);
  }

  process(_inputs, outputs) {
    const output = outputs[0][0];
    if (!output) return true;

    this.framesSincePcm += output.length;
    if (this.starving && this.framesSincePcm > sampleRate * STREAM_IDLE_MS / 1000) {
      this.starving = false;   // the stream stopped; this silence is not a stall
    }
    if (this.priming && this.available >= this.primeSamples) {
      this.priming = false;
      this.starving = false;
    }

    // Source is 16kHz; context runs at the hardware rate (48kHz on the M2,
    // 44.1kHz elsewhere).  Linear interpolation handles the non-integer ratio
    // cleanly — nearest-neighbor produced audible warbly artifacts at 44.1kHz
    // (step ≈ 0.363) but linear is inaudible and costs almost nothing.
    const latencyMs = this.available / this.sourceRate * 1000;
    this.statBlocks++;
    this.statSum += latencyMs;
    if (latencyMs < this.statMin) this.statMin = latencyMs;
    if (latencyMs > this.statMax) this.statMax = latencyMs;
    if (!this.catchingUp && latencyMs > MAX_LATENCY_MS) {
      this.catchingUp = true;
      this.port.postMessage({ diag: 'catchup_enter', latencyMs: Math.round(latencyMs) });
    } else if (this.catchingUp && latencyMs <= TARGET_LATENCY_MS) {
      this.catchingUp = false;
      this.port.postMessage({ diag: 'catchup_exit', latencyMs: Math.round(latencyMs) });
    }
    // Full rate while far over the target, tapering as it closes in (with a
    // floor, so it arrives rather than approaching forever).
    let wantRate = 0;
    if (this.catchingUp) {
      const excess = (latencyMs - TARGET_LATENCY_MS) / (MAX_LATENCY_MS - TARGET_LATENCY_MS);
      wantRate = MAX_CATCHUP_RATE * Math.min(1, Math.max(0.2, excess));
    }
    const delta = wantRate - this.catchupRate;
    this.catchupRate += Math.max(-CATCHUP_SLEW_PER_BLOCK, Math.min(CATCHUP_SLEW_PER_BLOCK, delta));

    const step = this.sourceRate / sampleRate * (1 + this.catchupRate);
    for (let i = 0; i < output.length; i++) {
      if (!this.priming && this.available >= 1) {
        const idx  = Math.floor(this.readPos) % this.ringSize;
        const next = (idx + 1) % this.ringSize;
        const frac = this.readPos - Math.floor(this.readPos);
        output[i] = this.ring[idx] * (1 - frac) + this.ring[next] * frac;
        this.readPos += step;
        this.available -= step;
      } else {
        if (!this.priming) {
          // Underrun: silence (not a click) until the ring is back at the target.
          this.priming = true;
          this.starving = true;
          this.underruns++;
          this.catchingUp = false;
          this.catchupRate = 0;
        }
        output[i] = 0;
        if (this.starving) this.starvedFrames++;
      }
    }
    this.readPos = this.readPos % this.ringSize;

    this.framesSinceReport += output.length;
    if (this.framesSinceReport >= sampleRate * LATENCY_REPORT_MS / 1000) {
      this.framesSinceReport = 0;
      this.port.postMessage({ latencyMs: this.latencyMs() });
    }
    this.framesSinceUnderrunReport += output.length;
    if (this.framesSinceUnderrunReport >= sampleRate * UNDERRUN_REPORT_MS / 1000) {
      this.framesSinceUnderrunReport = 0;
      if (this.underruns > 0 || this.starvedFrames > 0) {
        this.port.postMessage({
          diag: 'underrun', count: this.underruns, starvedSamples: this.starvedFrames,
          starvedMs: Math.round(this.starvedFrames / sampleRate * 1000) });
        this.underruns = 0;
        this.starvedFrames = 0;
      }
    }
    this.framesSinceStats += output.length;
    if (this.framesSinceStats >= sampleRate * STATS_INTERVAL_MS / 1000) {
      this.framesSinceStats = 0;
      if (this.pcmFrames > 0 || this.statMax > 0) {   // nothing to say about a silent receiver
        this.port.postMessage({
          diag: 'stats', minMs: Math.round(this.statMin), maxMs: Math.round(this.statMax),
          meanMs: Math.round(this.statSum / this.statBlocks),
          frames: this.pcmFrames, samples: this.pcmSamples });
      }
      this.statBlocks = 0;
      this.statSum = 0;
      this.statMin = Infinity;
      this.statMax = 0;
      this.pcmFrames = 0;
      this.pcmSamples = 0;
    }
    return true;
  }
}

registerProcessor('pcm-stream-processor', PcmStreamProcessor);
