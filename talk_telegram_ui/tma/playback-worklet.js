class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.sourceStep = 24000 / sampleRate;
    this.playedSourceFrames = 0;
    this.reportFrames = 0;
    this.maxQueuedFrames = 24000 * 20;
    this.overrun = false;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "flush") {
        this.queue = [];
        this.offset = 0;
        this.playedSourceFrames = 0;
        this.reportFrames = 0;
        this.overrun = false;
        return;
      }
      if (event.data && event.data.type === "reset") {
        this.playedSourceFrames = 0;
        return;
      }
      const data = event.data instanceof ArrayBuffer ? new Int16Array(event.data) : null;
      if (data && data.length && !this.overrun) {
        if (this.bufferedFrames() + data.length > this.maxQueuedFrames) {
          this.overrun = true;
          this.port.postMessage({ type: "overrun" });
          return;
        }
        this.queue.push(data);
      }
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) return true;
    let written = 0;
    while (written < output.length && this.queue.length) {
      const chunk = this.queue[0];
      const index = Math.floor(this.offset);
      if (index >= chunk.length) {
        this.queue.shift();
        this.offset = 0;
        continue;
      }
      const fraction = this.offset - index;
      const a = chunk[index];
      const b = index + 1 < chunk.length ? chunk[index + 1] : this.queue[1]?.[0] ?? a;
      output[written++] = (a + (b - a) * fraction) / 32768;
      this.offset += this.sourceStep;
      this.playedSourceFrames += this.sourceStep;
      while (this.queue.length && this.offset >= this.queue[0].length) {
        this.offset -= this.queue[0].length;
        this.queue.shift();
      }
    }
    for (; written < output.length; written++) output[written] = 0;
    this.reportFrames += output.length;
    if (this.reportFrames >= sampleRate / 20) {
      this.reportFrames = 0;
      this.port.postMessage({ playedMs: Math.floor(this.playedSourceFrames * 1000 / 24000), bufferedMs: this.bufferedMs() });
    }
    return true;
  }

  bufferedFrames() {
    return this.queue.reduce((total, chunk) => total + chunk.length, 0) - this.offset;
  }

  bufferedMs() {
    let frames = this.bufferedFrames();
    return Math.max(0, Math.floor(frames * 1000 / 24000));
  }
}

registerProcessor("tma-playback", PlaybackProcessor);
