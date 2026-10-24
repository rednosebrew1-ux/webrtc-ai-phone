// AI Phone client: login, ringing, WebRTC call, live transcript.
const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  mode: "idle", // idle | outgoing | incoming | connecting | in-call
  pc: null,
  dc: null,
  stream: null,
  ring: null, // { id, reason } for an incoming call
  muted: false,
  timer: null,
  startedAt: 0,
  lastAiLine: null,
};

// ---- screens & UI ----

function show(id) {
  for (const s of document.querySelectorAll(".screen")) s.hidden = s.id !== id;
}

function setStatus(text) {
  $("status").textContent = text;
}

function setMode(mode) {
  state.mode = mode;
  const visible = {
    idle: ["call-btn"],
    outgoing: ["hangup-btn"],
    connecting: ["hangup-btn"],
    incoming: ["decline-btn", "accept-btn"],
    "in-call": ["mute-btn", "hangup-btn"],
  }[mode];
  for (const id of ["call-btn", "decline-btn", "accept-btn", "mute-btn", "hangup-btn"]) {
    $(id).hidden = !visible.includes(id);
  }
  $("avatar").classList.toggle("ringing", mode === "incoming" || mode === "outgoing");
  $("reason").hidden = mode !== "incoming" || !state.ring;
}

function addLine(role, text, append) {
  const box = $("transcript");
  if (append && role === "assistant" && state.lastAiLine) {
    state.lastAiLine.textContent += " " + text;
  } else {
    const p = document.createElement("p");
    p.className = `line ${role}`;
    p.textContent = text;
    box.appendChild(p);
    if (role === "assistant") state.lastAiLine = p;
  }
  box.scrollTop = box.scrollHeight;
}

function startTimer() {
  state.startedAt = Date.now();
  $("timer").hidden = false;
  const tick = () => {
    const s = Math.floor((Date.now() - state.startedAt) / 1000);
    $("timer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  tick();
  state.timer = setInterval(tick, 1000);
}

function stopTimer() {
  clearInterval(state.timer);
  state.timer = null;
  $("timer").hidden = true;
}

// ---- sound unlock & notifications ----

async function enableSound() {
  if (await Sounds.unlock()) $("sound-banner").hidden = true;
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}

function notifyIncoming(reason) {
  if (navigator.vibrate) navigator.vibrate([400, 200, 400, 1000, 400, 200, 400]);
  if (!document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
  const n = new Notification(`${state.config.aiName} is calling`, { body: reason, tag: "ai-phone-ring", requireInteraction: true });
  n.onclick = () => { window.focus(); n.close(); };
}

// ---- incoming rings (server-sent events) ----

function listenForRings() {
  const es = new EventSource("/api/events");
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "ring" && state.mode === "idle") {
      state.ring = { id: ev.id, reason: ev.reason };
      $("reason").textContent = ev.reason;
      $("transcript").textContent = "";
      setStatus("Incoming call…");
      setMode("incoming");
      Sounds.ringtone();
      notifyIncoming(ev.reason);
    } else if (ev.type === "ring_end" && state.ring && state.ring.id === ev.id && state.mode === "incoming") {
      Sounds.stop();
      state.ring = null;
      setStatus(ev.status === "missed" ? "Missed call" : ev.status === "answered" ? "Answered elsewhere" : "Declined");
      setMode("idle");
    }
  };
  // EventSource reconnects on its own; nothing else to do on error.
}

// ---- the call itself ----

async function startCall(ringId) {
  const outgoing = !ringId;
  setMode(outgoing ? "outgoing" : "connecting");
  setStatus(outgoing ? "Calling…" : "Connecting…");
  $("transcript").textContent = "";
  state.lastAiLine = null;
  if (outgoing) Sounds.ringback(); else Sounds.stop();

  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    Sounds.stop();
    setStatus("Microphone blocked. Allow mic access and try again.");
    setMode("idle");
    if (ringId) declineRing(ringId);
    return;
  }

  const pc = new RTCPeerConnection({ iceServers: state.config.iceServers });
  state.pc = pc;
  state.dc = pc.createDataChannel("events");
  state.dc.onmessage = (msg) => onCallEvent(JSON.parse(msg.data));
  for (const track of state.stream.getTracks()) pc.addTrack(track, state.stream);
  pc.ontrack = (ev) => {
    $("remote-audio").srcObject = ev.streams[0] || new MediaStream([ev.track]);
    $("remote-audio").play().catch(() => {});
  };
  pc.onconnectionstatechange = () => {
    if (["failed", "disconnected"].includes(pc.connectionState) && state.pc === pc) {
      endCall("Connection lost");
    }
  };

  await pc.setLocalDescription(await pc.createOffer());
  await waitForIce(pc);
  try {
    const r = await fetch("/api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type, ringId }),
    });
    if (!r.ok) throw new Error(await r.text());
    await pc.setRemoteDescription(await r.json());
  } catch (e) {
    endCall(`Call failed: ${e.message}`);
  }
}

// Wait until ICE candidates are gathered (the server expects them in the offer).
function waitForIce(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { pc.removeEventListener("icegatheringstatechange", check); resolve(); };
    const check = () => pc.iceGatheringState === "complete" && done();
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, 3000);
  });
}

function onCallEvent(ev) {
  switch (ev.type) {
    case "ringing":
      setStatus("Ringing…");
      break;
    case "answered":
      Sounds.connected();
      setMode("in-call");
      setStatus("Connected");
      startTimer();
      break;
    case "state":
      if (state.mode === "in-call") {
        setStatus({ listening: "Listening", hearing: "Listening…", thinking: "Thinking…", speaking: "Speaking" }[ev.state] || "Connected");
      }
      break;
    case "transcript":
      addLine(ev.role, ev.text, ev.append);
      break;
    case "interrupted":
      if (state.lastAiLine) state.lastAiLine.textContent += " —";
      break;
    case "hangup":
      endCall(ev.by === "ai" ? `${state.config.aiName} hung up` : "Call ended", true);
      break;
  }
}

function endCall(message = "Call ended", remote = false) {
  if (!remote && state.dc && state.dc.readyState === "open") {
    state.dc.send(JSON.stringify({ type: "hangup" }));
  }
  const wasActive = state.mode !== "idle";
  const pc = state.pc;
  state.pc = null;
  state.dc = null;
  if (pc) setTimeout(() => pc.close(), 300); // give the hangup message time to send
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  state.stream = null;
  state.ring = null;
  state.muted = false;
  $("mute-btn").classList.remove("active");
  stopTimer();
  if (wasActive) Sounds.hangup();
  setStatus(message);
  setMode("idle");
}

async function declineRing(id) {
  await fetch(`/api/ring/${id}/decline`, { method: "POST" }).catch(() => {});
}

// ---- wiring ----

$("call-btn").onclick = () => startCall();
$("hangup-btn").onclick = () => endCall();
$("accept-btn").onclick = () => {
  if (state.ring) startCall(state.ring.id);
};
$("decline-btn").onclick = () => {
  Sounds.stop();
  if (state.ring) declineRing(state.ring.id);
  state.ring = null;
  setStatus("Declined");
  setMode("idle");
};
$("mute-btn").onclick = () => {
  state.muted = !state.muted;
  if (state.stream) state.stream.getAudioTracks().forEach((t) => (t.enabled = !state.muted));
  $("mute-btn").classList.toggle("active", state.muted);
};
document.addEventListener("pointerdown", enableSound);
document.addEventListener("keydown", enableSound);

$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: $("password").value }),
  });
  if (r.ok) boot();
  else $("login-error").hidden = false;
};

async function boot() {
  state.config = await (await fetch("/api/config")).json();
  if (!state.config.loggedIn) {
    show("login");
    return;
  }
  $("ai-name").textContent = state.config.aiName;
  $("avatar-letter").textContent = state.config.aiName.slice(0, 1).toUpperCase();
  document.title = `${state.config.aiName} · AI Phone`;
  $("sound-banner").hidden = Sounds.unlocked();
  show("phone");
  setMode("idle");
  listenForRings();
}

boot();
