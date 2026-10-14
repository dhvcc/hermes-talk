class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.pending = [];
    this.sourceRate = sampleRate;
    this.targetRate = 24000;
    this.step = this.sourceRate / this.targetRate;
    this.position = 0;
    this.batch = [];
    this.batchFrames = 0;
    this.batchLimit = 960;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || !channel.length) return true;
    for (const sample of channel) this.pending.push(sample);
    const count = Math.floor((this.pending.length - 1 - this.position) / this.step);
    if (count <= 0) return true;
    const pcm = new Int16Array(count);
    for (let i = 0; i < count; i++) {
      const at = this.position + i * this.step;
      const left = Math.floor(at);
      const fraction = at - left;
      const a = this.pending[left] || 0;
      const b = this.pending[left + 1] || a;
      const value = Math.max(-1, Math.min(1, a + (b - a) * fraction));
      pcm[i] = value < 0 ? value * 32768 : value * 32767;
    }
    this.position += count * this.step;
    const consumed = Math.floor(this.position);
    if (consumed) {
      this.pending.splice(0, consumed);
      this.position -= consumed;
    }
    this.batch.push(pcm);
    this.batchFrames += pcm.length;
    if (this.batchFrames >= this.batchLimit) this.flush();
    return true;
  }

  flush() {
    if (!this.batchFrames) return;
    const pcm = new Int16Array(this.batchFrames);
    let offset = 0;
    for (const part of this.batch) {
      pcm.set(part, offset);
      offset += part.length;
    }
    this.batch = [];
    this.batchFrames = 0;
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
  }
}

registerProcessor("tma-capture", CaptureProcessor);
