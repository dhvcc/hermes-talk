"""Run the Telegram TMA audio worklets in a small Node VM harness."""

from __future__ import annotations

import shutil
from pathlib import Path
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_WORKLET = ROOT / "talk_telegram_ui" / "tma" / "capture-worklet.js"
PLAYBACK_WORKLET = ROOT / "talk_telegram_ui" / "tma" / "playback-worklet.js"
NODE = shutil.which("node")

WORKLET_CHECK = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const capturePath = process.argv[1], playbackPath = process.argv[2];

function load(path, rate, processorName) {
  const processors = {};
  const AudioWorkletProcessor = class {
    constructor() {
      this.port = {
        messages: [],
        onmessage: null,
        postMessage(value) { this.messages.push(value); },
      };
    }
  };
  const context = vm.createContext({
    ArrayBuffer, AudioWorkletProcessor, Float32Array, Int16Array, Math, sampleRate: rate,
    registerProcessor(name, processor) { processors[name] = processor; },
  });
  vm.runInContext(fs.readFileSync(path, "utf8"), context, { filename: path });
  assert(processors[processorName], `missing ${processorName} in ${path}`);
  return {
    ArrayBuffer: context.ArrayBuffer,
    Float32Array: context.Float32Array,
    Int16Array: context.Int16Array,
    Processor: processors[processorName],
  };
}

function enqueue(processor, worklet, frames, value = 12000) {
  const pcm = new worklet.Int16Array(frames);
  pcm.fill(value);
  processor.port.onmessage({ data: pcm.buffer });
}

function drain(processor, worklet, rate, seconds) {
  const quantum = 128;
  const expected = rate * seconds;
  const limit = Math.ceil(expected / quantum) + 100;
  let nonzero = 0;
  let started = false;
  let trailingSilence = false;
  for (let i = 0; i < limit; i++) {
    const output = new worklet.Float32Array(quantum);
    processor.process([], [[output]]);
    for (const sample of output) {
      if (Math.abs(sample) > 1e-6) {
        assert(!trailingSilence, "playback inserted a silent gap");
        started = true;
        nonzero++;
      } else if (started) {
        trailingSilence = true;
      }
    }
    if (processor.queue.length === 0 && processor.bufferedFrames() <= 0) {
      return nonzero;
    }
  }
  throw new Error(`playback did not drain at ${rate}Hz`);
}

function reportsAfterDrain(processor, worklet, rate) {
  const extraQuanta = Math.ceil(rate / 20 / 128) + 1;
  for (let i = 0; i < extraQuanta; i++) {
    processor.process([], [[new worklet.Float32Array(128)]]);
  }
  const reports = processor.port.messages.filter(
    (message) => message && typeof message.playedMs === "number",
  );
  assert(reports.length, `no playback reports at ${rate}Hz`);
  return reports.at(-1);
}

function captureOneSecond(rate) {
  const worklet = load(capturePath, rate, "tma-capture");
  const processor = new worklet.Processor();
  const total = rate;
  const quantum = 128;
  for (let offset = 0; offset < total; offset += quantum) {
    const length = Math.min(quantum, total - offset);
    const input = new worklet.Float32Array(length);
    input.fill(0.25);
    processor.process([[input]]);
  }
  processor.flush();
  const frames = processor.port.messages.reduce(
    (totalFrames, message) => totalFrames + new worklet.Int16Array(message).length,
    0,
  );
  assert(Math.abs(frames - 23999) <= 1, `${rate}Hz capture produced ${frames} frames`);
}

function playbackOneSecond(rate) {
  const worklet = load(playbackPath, rate, "tma-playback");
  const processor = new worklet.Processor();
  enqueue(processor, worklet, 24000);
  const nonzero = drain(processor, worklet, rate, 1);
  assert(nonzero >= rate - 128, `${rate}Hz playback dropped output frames`);
  const report = reportsAfterDrain(processor, worklet, rate);
  const quantumMs = 1000 * 128 / rate + 1;
  assert(Math.abs(report.playedMs - 1000) <= quantumMs,
    `${rate}Hz playback reported ${report.playedMs}ms`);
  assert.equal(report.bufferedMs, 0);
}

function playbackTenSeconds(rate) {
  const worklet = load(playbackPath, rate, "tma-playback");
  const processor = new worklet.Processor();
  for (let i = 0; i < 10; i++) enqueue(processor, worklet, 24000);
  const nonzero = drain(processor, worklet, rate, 10);
  assert(nonzero >= rate * 10 - 128, `${rate}Hz ten-second playback lost samples`);
}

function playbackOverrun(rate) {
  const worklet = load(playbackPath, rate, "tma-playback");
  const processor = new worklet.Processor();
  for (let i = 0; i < 20; i++) enqueue(processor, worklet, 24000);
  assert.equal(processor.bufferedFrames(), processor.maxQueuedFrames);
  enqueue(processor, worklet, 24000);
  assert(processor.overrun, `${rate}Hz playback did not report overrun`);
  assert(processor.bufferedFrames() <= processor.maxQueuedFrames);
  assert(processor.port.messages.some((message) => message.type === "overrun"));
}

function playbackFlushesToSilence() {
  const worklet = load(playbackPath, 48000, "tma-playback");
  const processor = new worklet.Processor();
  enqueue(processor, worklet, 24000);
  const before = new worklet.Float32Array(128);
  processor.process([], [[before]]);
  assert(Array.from(before).some((sample) => Math.abs(sample) > 0.1));
  processor.port.onmessage({ data: { type: "flush" } });
  assert.equal(processor.bufferedFrames(), 0);
  assert.equal(processor.playedSourceFrames, 0);
  const after = new worklet.Float32Array(128);
  processor.process([], [[after]]);
  assert(Array.from(after).every((sample) => sample === 0), "flush left audible audio");
}

for (const rate of [44100, 48000]) {
  captureOneSecond(rate);
  playbackOneSecond(rate);
  playbackTenSeconds(rate);
  playbackOverrun(rate);
}
playbackFlushesToSilence();
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is required for TMA worklet checks")
def test_telegram_audio_worklets_preserve_duration_and_bounds():
    result = run(
        [NODE, "-e", WORKLET_CHECK, str(CAPTURE_WORKLET), str(PLAYBACK_WORKLET)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
