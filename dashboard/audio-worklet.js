// Runs on the dedicated audio rendering thread, immune to main-thread
// jank (canvas redraws, GC pauses, etc.) — the previous approach scheduled
// a new AudioBufferSourceNode per incoming WebSocket message, so any delay
// in handling that exact message produced an audible gap. Here, incoming
// PCM just gets written into a ring buffer; process() reads it out
// continuously regardless of when messages actually arrive.
class PcmStreamProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.sourceRate = 16000;            // rate of the incoming demodulated audio
    this.ringSize = this.sourceRate * 3; // 3s of headroom — generous, cheap
    this.ring = new Float32Array(this.ringSize);
    this.writeIdx = 0;
    this.available = 0;   // unread source samples currently buffered
    this.readPos = 0;     // fractional read index into ring, source-sample units

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

    // Source is 16kHz; the context runs at its own native rate (usually
    // 44.1/48kHz). Nearest-neighbor resampling on read — plenty for
    // monitoring SSB/CW, and keeps this dependency-free.
    const step = this.sourceRate / sampleRate;
    for (let i = 0; i < output.length; i++) {
      if (this.available >= 1) {
        output[i] = this.ring[Math.floor(this.readPos) % this.ringSize];
        this.readPos += step;
        this.available -= step;
      } else {
        output[i] = 0;   // underrun: brief silence, not a click
      }
    }
    this.readPos = this.readPos % this.ringSize;
    return true;
  }
}

registerProcessor('pcm-stream-processor', PcmStreamProcessor);
