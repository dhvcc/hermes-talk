"""Exercise the Mini App lifecycle without a browser."""

from __future__ import annotations

from pathlib import Path
from subprocess import run

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "talk_telegram_ui" / "tma" / "app.js"

HARNESS = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const listeners = {}, contexts = [], sockets = [], sent = [];
const elements = Object.fromEntries(["start", "end", "hint", "status"].map((id) => [id, {
  disabled: false, textContent: "",
  addEventListener(type, callback) { listeners[id + ":" + type] = callback; },
}]));
const document = {
  visibilityState: "hidden",
  getElementById(id) { return elements[id]; },
  addEventListener(type, callback) { listeners["document:" + type] = callback; },
};
const storage = new Map();
const localStorage = {
  getItem(key) { return storage.get(key) || null; },
  setItem(key, value) { storage.set(key, value); },
};
const tracks = [];
const stream = () => {
  const track = { stopped: 0, stop() { this.stopped++; } };
  tracks.push(track);
  return { getTracks() { return [track]; }, track };
};
let getUserMedia = () => Promise.resolve(stream());
const navigator = {
  mediaDevices: { getUserMedia(...args) { return getUserMedia(...args); } },
  permissions: { async query() { return { state: "granted" }; } },
};
class AudioContext {
  constructor() {
    this.state = "suspended"; this.resumeCalls = 0; this.closeCalls = 0;
    this.destination = {};
    this.audioWorklet = { async addModule() {} };
    contexts.push(this);
  }
  resume() { this.resumeCalls++; this.state = "running"; return Promise.resolve(); }
  createMediaStreamSource() { return { connect(node) { return node; } }; }
  createGain() { return { gain: { value: 0 }, connect(node) { return node; } }; }
  close() { this.closeCalls++; this.state = "closed"; return Promise.resolve(); }
}
class AudioWorkletNode {
  constructor() { this.port = { onmessage: null, postMessage() {} }; }
  connect(node) { return node; }
  disconnect() {}
}
class WebSocket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {
    this.url = url; this.readyState = WebSocket.CONNECTING;
    this.bufferedAmount = 0; this.closeCalls = 0; sockets.push(this);
  }
  send(value) { sent.push({ socket: this, value }); }
  close() { this.closeCalls++; this.readyState = WebSocket.CLOSING; }
  open() { this.readyState = WebSocket.OPEN; if (this.onopen) this.onopen(); }
  message(value) {
    if (this.onmessage) this.onmessage({
      data: typeof value === "string" ? value : JSON.stringify(value),
    });
  }
  closeEvent() { this.readyState = WebSocket.CLOSED; if (this.onclose) this.onclose(); }
}
const window = {
  Telegram: null, location: { protocol: "http:", host: "127.0.0.1" },
  addEventListener(type, callback) { listeners["window:" + type] = callback; },
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const waitFor = async (predicate) => {
  const deadline = Date.now() + 1000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out");
    await tick();
  }
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  window, location: window.location, document, navigator, localStorage,
  AudioContext, AudioWorkletNode, WebSocket,
  setTimeout, clearTimeout, Promise, console,
});
'''


def execute(scenario: str) -> None:
    result = run(
        [
            "node", "-e",
            HARNESS + "\n(async () => {\n" + scenario
            + "\n})().catch((error) => { console.error(error); process.exitCode = 1; });",
            str(APP),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stop_during_pending_microphone_request_releases_late_stream():
    execute(r'''
let release;
getUserMedia = () => new Promise((resolve) => { release = resolve; });
listeners["start:click"]();
await tick();
assert.equal(contexts.length, 1);
assert.equal(contexts[0].resumeCalls, 1);
listeners["end:click"]();
const late = stream(); release(late);
await tick(); await tick();
assert.equal(late.track.stopped, 1);
assert(contexts[0].closeCalls >= 1);
''')


def test_stale_socket_close_cannot_end_newer_call():
    execute(r'''
listeners["start:click"]();
await waitFor(() => sockets.length === 1);
await new Promise((resolve) => setTimeout(resolve, 10));
const first = sockets[0];
listeners["end:click"]();
await tick();
listeners["start:click"]();
await waitFor(() => sockets.length === 2);
const second = sockets[1];
second.open();
second.message({ type: "ready" });
await tick();
assert.equal(elements.end.disabled, false);
first.closeEvent();
assert.equal(second.readyState, WebSocket.OPEN);
assert.equal(elements.end.disabled, false);
''')
