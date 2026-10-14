(() => {
  const startButton = document.getElementById("start");
  const endButton = document.getElementById("end");
  const hint = document.getElementById("hint");
  const status = document.getElementById("status");
  let socket = null;
  let context = null;
  let stream = null;
  let capture = null;
  let playback = null;
  let ending = false;
  let starting = false;
  let startGeneration = 0;
  let activeAttempt = null;
  const warmKey = "hermes.tma.microphone-warmed";

  const setStatus = (text) => { status.textContent = text || ""; };
  const wsUrl = () => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/tma/ws`;

  function setWarmPermission() {
    try { localStorage.setItem(warmKey, "1"); } catch (_) {}
  }

  function warmPermissionKnown() {
    try { return localStorage.getItem(warmKey) === "1"; } catch (_) { return false; }
  }

  async function microphonePermission() {
    if (!navigator.permissions || !navigator.permissions.query) return null;
    try { return await navigator.permissions.query({ name: "microphone" }); } catch (_) { return null; }
  }

  async function resumeContext(audioContext, pending) {
    const resume = pending || audioContext.resume();
    let timer;
    try {
      await Promise.race([
        resume,
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("audio resume timed out")), 3000); }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  function disconnect(node) {
    if (!node) return;
    try { node.disconnect(); } catch (_) {}
  }

  function stopTracks(mediaStream) {
    if (!mediaStream) return;
    for (const track of mediaStream.getTracks()) {
      try { track.stop(); } catch (_) {}
    }
  }

  function closeContext(audioContext) {
    if (!audioContext) return;
    try {
      const pending = audioContext.close();
      if (pending && typeof pending.catch === "function") pending.catch(() => {});
    } catch (_) {}
  }

  function cleanupAttempt(attempt) {
    if (!attempt) return;
    if (attempt.socket && attempt.socket.readyState < WebSocket.CLOSING) attempt.socket.close();
    disconnect(attempt.capture);
    disconnect(attempt.playback);
    stopTracks(attempt.stream);
    closeContext(attempt.context);
    if (socket === attempt.socket) socket = null;
    if (context === attempt.context) context = null;
    if (stream === attempt.stream) stream = null;
    if (capture === attempt.capture) capture = null;
    if (playback === attempt.playback) playback = null;
    if (activeAttempt === attempt) activeAttempt = null;
  }

  async function start(automatic = false) {
    if (socket || starting) return;
    if (automatic && !warmPermissionKnown()) return;
    const generation = ++startGeneration;
    const attempt = { generation, context: null, stream: null, capture: null, playback: null, socket: null };
    activeAttempt = attempt;
    starting = true;
    ending = false;
    startButton.disabled = true;
    setStatus("Requesting microphone…");
    try {
      attempt.context = new AudioContext();
      let resumePending;
      try { resumePending = Promise.resolve(attempt.context.resume()); }
      catch (error) { resumePending = Promise.reject(error); }
      resumePending.catch(() => {});
      attempt.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      if (generation !== startGeneration || ending) throw new Error("start canceled");
      setWarmPermission();
      await attempt.context.audioWorklet.addModule("/tma/capture-worklet.js");
      await attempt.context.audioWorklet.addModule("/tma/playback-worklet.js");
      await resumeContext(attempt.context, resumePending);
      if (generation !== startGeneration || ending) throw new Error("start canceled");
      if (attempt.context.state !== "running") throw new Error("audio context is suspended");
      const telegram = window.Telegram && window.Telegram.WebApp;
      telegram && telegram.ready();
      const currentSocket = new WebSocket(wsUrl());
      if (generation !== startGeneration || ending) {
        currentSocket.close();
        throw new Error("start canceled");
      }
      attempt.socket = currentSocket;
      context = attempt.context;
      stream = attempt.stream;
      socket = currentSocket;
      socket.binaryType = "arraybuffer";
      const current = () => socket === currentSocket && generation === startGeneration && !ending;
      currentSocket.onopen = () => {
        if (!current()) return;
        currentSocket.send(JSON.stringify({ type: "auth", initData: telegram ? telegram.initData : "" }));
      };
      currentSocket.onmessage = (event) => {
        if (!current()) return;
        if (typeof event.data === "string") {
          let message;
          try { message = JSON.parse(event.data); } catch (_) { return; }
          if (message.type === "ready") {
            try {
              setupAudioNodes(attempt, currentSocket, generation);
            } catch (_) {
              setStatus("Voice start failed. Tap Start to try again.");
              stop(false);
              return;
            }
            hint.textContent = "Listening. Speak naturally, then tap End when finished.";
            endButton.disabled = false;
            setStatus("");
          } else if (message.type === "flush_audio") {
            if (playback) playback.port.postMessage({ type: "flush", generation: message.generation });
          } else if (message.type === "reset_played") {
            if (playback) playback.port.postMessage({ type: "reset" });
          } else if (message.type === "overrun") {
            setStatus(message.text || "Playback fell behind; ending the call.");
            stop(true);
          } else if (message.type === "error" || message.type === "message") {
            setStatus(message.text || "The call ended.");
          }
          return;
        }
        if (playback && event.data instanceof ArrayBuffer) playback.port.postMessage(event.data, [event.data]);
      };
      currentSocket.onerror = () => { if (current()) setStatus("The voice connection failed."); };
      currentSocket.onclose = () => { if (current()) stop(false); };
    } catch (error) {
      cleanupAttempt(attempt);
      if (generation === startGeneration) {
        ending = false;
        const permissionError = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
        setStatus(permissionError ? "Microphone access is needed to start voice." : "Voice start failed. Tap Start to try again.");
        startButton.disabled = false;
        endButton.disabled = true;
      }
    } finally {
      starting = false;
    }
  }

  function setupAudioNodes(attempt, currentSocket, generation) {
    if (socket !== currentSocket || generation !== startGeneration || ending) return;
    const input = context.createMediaStreamSource(stream);
    capture = new AudioWorkletNode(context, "tma-capture");
    attempt.capture = capture;
    capture.port.onmessage = (event) => {
      if (socket !== currentSocket || generation !== startGeneration || ending
        || currentSocket.readyState !== WebSocket.OPEN || currentSocket.bufferedAmount > 512000) return;
      currentSocket.send(event.data);
    };
    input.connect(capture);
    const silent = context.createGain();
    silent.gain.value = 0;
    capture.connect(silent).connect(context.destination);
    playback = new AudioWorkletNode(context, "tma-playback");
    attempt.playback = playback;
    playback.connect(context.destination);
    playback.port.onmessage = (event) => {
      if (socket !== currentSocket || generation !== startGeneration || ending) return;
      if (event.data && event.data.type === "overrun") {
        setStatus("Playback fell behind; ending the call.");
        stop(true);
        return;
      }
      if (currentSocket.readyState === WebSocket.OPEN) {
        currentSocket.send(JSON.stringify({ type: "played_ms", played_ms: event.data.playedMs, buffered_ms: event.data.bufferedMs }));
      }
    };
  }

  function stop(sendEnd = true) {
    if (ending) return;
    ending = true;
    ++startGeneration;
    const currentSocket = socket;
    if (sendEnd && currentSocket && currentSocket.readyState === WebSocket.OPEN) {
      currentSocket.send(JSON.stringify({ type: "end" }));
    }
    const attempt = activeAttempt;
    if (attempt) cleanupAttempt(attempt);
    else {
      if (currentSocket && currentSocket.readyState < WebSocket.CLOSING) currentSocket.close();
      disconnect(capture);
      disconnect(playback);
      stopTracks(stream);
      closeContext(context);
    }
    socket = context = stream = capture = playback = null;
    activeAttempt = null;
    startButton.disabled = false;
    endButton.disabled = true;
    hint.textContent = "Tap Start and allow microphone access.";
  }

  startButton.addEventListener("click", () => start(false));
  endButton.addEventListener("click", () => stop(true));
  window.addEventListener("pagehide", () => stop(true));
  const telegram = window.Telegram && window.Telegram.WebApp;
  telegram && telegram.ready();

  async function tryWarmStart() {
    if (document.visibilityState !== "visible" || !warmPermissionKnown()) return;
    const permission = await microphonePermission();
    if (!permission || permission.state === "granted") start(true);
  }

  tryWarmStart();
  document.addEventListener("visibilitychange", tryWarmStart);
})();
