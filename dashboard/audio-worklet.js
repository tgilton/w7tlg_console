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

    this.port.onmessage = (evt) => {
      // Control commands arrive as plain objects; PCM arrives as Float32Array.
      if (evt.data && evt.data.cmd === 'flush') {
        // TX started — drop all buffered audio instantly.
        // Also align readPos to writeIdx: without this, the reader's position
        // is frozen mid-ring while writeIdx keeps moving. When RX audio arrives
        // after TX the reader starts from the OLD readPos, replaying stale
        // pre-TX audio in the ring as a series of echoes before finally
        // catching up to where fresh data is being written.
        this.available = 0;
        this.readPos = this.writeIdx;
        this.catchingUp = false;
        this.catchupRate = 0;
        return;
      }
      const samples = evt.data;
      for (let i = 0; i < samples.length; i++) {
        this.ring[this.writeIdx] = samples[i];
        this.writeIdx = (this.writeIdx + 1) % this.ringSize;
      }
      // Ring overwrite already drops the oldest data on overflow; just
      // keep the bookkeeping in bounds to match.
      this.available = Math.min(this.ringSize, this.available + samples.length);
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0][0];
    if (!output) return true;

    // Source is 16kHz; context runs at the hardware rate (48kHz on the M2,
    // 44.1kHz elsewhere).  Linear interpolation handles the non-integer ratio
    // cleanly — nearest-neighbor produced audible warbly artifacts at 44.1kHz
    // (step ≈ 0.363) but linear is inaudible and costs almost nothing.
    const latencyMs = this.available / this.sourceRate * 1000;
    if (!this.catchingUp && latencyMs > MAX_LATENCY_MS) this.catchingUp = true;
    else if (this.catchingUp && latencyMs <= TARGET_LATENCY_MS) this.catchingUp = false;
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
      if (this.available >= 1) {
        const idx  = Math.floor(this.readPos) % this.ringSize;
        const next = (idx + 1) % this.ringSize;
        const frac = this.readPos - Math.floor(this.readPos);
        output[i] = this.ring[idx] * (1 - frac) + this.ring[next] * frac;
        this.readPos += step;
        this.available -= step;
      } else {
        output[i] = 0;   // underrun: brief silence, not a click
      }
    }
    this.readPos = this.readPos % this.ringSize;

    this.framesSinceReport += output.length;
    if (this.framesSinceReport >= sampleRate * LATENCY_REPORT_MS / 1000) {
      this.framesSinceReport = 0;
      this.port.postMessage({
        latencyMs: Math.round(Math.max(0, this.available) / this.sourceRate * 1000) });
    }
    return true;
  }
}

registerProcessor('pcm-stream-processor', PcmStreamProcessor);
