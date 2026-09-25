const GAMES = [
  { id: "rdr2", label: "Red Dead Redemption 2" },
  { id: "tlou", label: "The Last of Us" },
  { id: "gow", label: "God of War" },
  { id: "gta", label: "GTA / Uncharted" },
  { id: "souls", label: "Souls / Elden Ring" },
  { id: "hogwarts", label: "Hogwarts Legacy" },
  { id: "generic", label: "Inna gra" },
];

const state = {
  running: false,
  mode: "auto",
  autoInterval: true,
  expectPick: false,
  regionGuide: false,
  collapsed: false,
};

function fmtNum(value, digits) {
  return Number(value).toFixed(digits).replace(".", ",");
}

function setScanLabel(sec, auto) {
  const el = document.getElementById("intervalVal");
  if (!el) return;
  const perSec = sec > 0 ? Math.round(1 / sec) : 0;
  el.textContent = auto ? `auto ${fmtNum(sec, 2)} s` : `${fmtNum(sec, 2)} s · ${perSec}/s`;
}

function setAutoScan(on) {
  state.autoInterval = !!on;
  const btn = document.getElementById("scanAuto");
  if (btn) btn.classList.toggle("primary", state.autoInterval);
}

function fillGames(items, current) {
  const sel = document.getElementById("game");
  if (!sel) return;
  const list = Array.isArray(items) && items.length ? items : GAMES;
  const chosen = current || sel.value || "rdr2";
  sel.innerHTML = list
    .map((item) => `<option value="${item.id}">${item.label}</option>`)
    .join("");
  sel.value = chosen;
}

function send(msg) {
  window.gr.send(msg);
}

function setLive(on) {
  const el = document.getElementById("live");
  el.className = on ? "pill on" : "pill off";
  el.textContent = on ? "CZYTA" : "WYŁ";
  const label = on ? "Stop" : "Czytaj";
  document.getElementById("start").textContent = label;
  const mini = document.getElementById("startMini");
  if (mini) mini.textContent = label;
}

function setCollapsedUi(on) {
  state.collapsed = !!on;
  document.body.classList.toggle("collapsed", state.collapsed);
  const btn = document.getElementById("collapse");
  if (btn) {
    btn.textContent = state.collapsed ? "+" : "−";
    btn.title = state.collapsed ? "Rozwiń" : "Zwiń";
  }
}

function applyState(msg) {
  if (msg.mode) {
    state.mode = msg.mode;
    document.getElementById("modeAuto").classList.toggle("primary", msg.mode === "auto");
    document.getElementById("modeOcr").classList.toggle("primary", msg.mode === "ocr");
    document.getElementById("modeAudio").classList.toggle("primary", msg.mode === "audio");
  }
  if (Array.isArray(msg.devices)) {
    const sel = document.getElementById("device");
    const current = msg.device || sel.value;
    sel.innerHTML = msg.devices.map((name) => `<option value="${name}">${name}</option>`).join("");
    if (current) sel.value = current;
  }
  if (typeof msg.overlay === "boolean") {
    const overlay = document.getElementById("overlay");
    if (overlay) overlay.checked = msg.overlay;
  }
  if (typeof msg.intensity === "number") {
    document.getElementById("intensity").value = String(msg.intensity);
    const intensityVal = document.getElementById("intensityVal");
    if (intensityVal) intensityVal.textContent = fmtNum(msg.intensity, 1);
  }
  if (typeof msg.autoInterval === "boolean") setAutoScan(msg.autoInterval);
  if (typeof msg.interval === "number") {
    document.getElementById("interval").value = String(msg.interval);
    setScanLabel(msg.interval, state.autoInterval);
  }
  if (Array.isArray(msg.games) || msg.game) {
    fillGames(msg.games, msg.game);
  }
  if (msg.gameHint) document.getElementById("gameHint").textContent = msg.gameHint;
  if ("psWindow" in msg) {
    const el = document.getElementById("psWin");
    if (el) {
      if (Array.isArray(msg.psWindow) && msg.psWindow.length === 4) {
        el.textContent = `PS Remote Play ${msg.psWindow[2]}×${msg.psWindow[3]}`;
      } else {
        el.textContent = "Brak okna PS Remote Play";
      }
    }
  }
  if (typeof msg.showRegion === "boolean" && msg.showRegion !== state.regionGuide) {
    state.regionGuide = msg.showRegion;
    const box = document.getElementById("showRegion");
    if (box) box.checked = msg.showRegion;
    window.gr.setRegionGuide(msg.showRegion);
  }
  if (typeof msg.collapsed === "boolean") setCollapsedUi(msg.collapsed);
  if (typeof msg.running === "boolean") {
    state.running = msg.running;
    setLive(msg.running);
  }
  if (Array.isArray(msg.region) && msg.region.length === 4 && state.expectPick) {
    setRegionGuide(true, true);
    state.expectPick = false;
  }
}

function setRegionGuide(on, persist = true) {
  const next = !!on;
  state.regionGuide = next;
  const box = document.getElementById("showRegion");
  if (box) {
    box.checked = next;
    box.blur();
  }
  window.gr.setRegionGuide(next);
  if (persist) send({ cmd: "config", showRegion: next });
  window.gr.releaseFocus();
}

function toggleStart() {
  send({ cmd: state.running ? "stop" : "start" });
  window.gr.releaseFocus();
}

document.getElementById("modeAuto").onclick = () => send({ cmd: "config", mode: "auto" });
document.getElementById("modeOcr").onclick = () => send({ cmd: "config", mode: "ocr" });
document.getElementById("modeAudio").onclick = () => send({ cmd: "config", mode: "audio" });
document.getElementById("pick").onclick = () => {
  state.expectPick = true;
  send({ cmd: "pick" });
};
document.getElementById("test").onclick = () => send({ cmd: "test" });
document.getElementById("start").onclick = toggleStart;
document.getElementById("startMini").onclick = toggleStart;
document.getElementById("showRegion").onchange = (e) => setRegionGuide(e.target.checked, true);
document.getElementById("save").onclick = () => {
  send({ cmd: "save" });
  window.gr.releaseFocus();
};
document.getElementById("collapse").onclick = (e) => {
  e.stopPropagation();
  const next = !state.collapsed;
  setCollapsedUi(next);
  window.gr.setCollapsed(next);
};
const device = document.getElementById("device");
if (device) device.onchange = (e) => send({ cmd: "config", device: e.target.value });
const overlay = document.getElementById("overlay");
if (overlay) overlay.onchange = (e) => send({ cmd: "config", overlay: e.target.checked });
document.getElementById("intensity").oninput = (e) => {
  const intensityVal = document.getElementById("intensityVal");
  if (intensityVal) intensityVal.textContent = fmtNum(e.target.value, 1);
};
document.getElementById("intensity").onchange = (e) => {
  send({ cmd: "config", intensity: Number(e.target.value) });
  window.gr.releaseFocus();
};
document.getElementById("interval").oninput = (e) => {
  setAutoScan(false);
  setScanLabel(Number(e.target.value), false);
};
document.getElementById("interval").onchange = (e) => {
  send({ cmd: "config", interval: Number(e.target.value) });
  window.gr.releaseFocus();
};
document.getElementById("scanAuto").onclick = () => send({ cmd: "config", autoInterval: true });
document.getElementById("game").onchange = (e) => send({ cmd: "config", game: e.target.value });
fillGames(GAMES, "rdr2");
setScanLabel(0.16, true);

window.gr.onEvent((msg) => {
  if (msg.event === "ready" || msg.event === "state") applyState(msg);
  if (msg.event === "status") document.getElementById("status").textContent = msg.text || "";
  if (msg.event === "heard") document.getElementById("heard").textContent = msg.text || "";
  if (msg.event === "line") {
    document.getElementById("line").textContent = msg.text || "";
    document.getElementById("mood").textContent = `emocja: ${msg.label || msg.mood || "—"}`;
  }
  if (msg.event === "preview") return;
  if (msg.event === "running") {
    state.running = !!msg.on;
    setLive(state.running);
  }
  if (msg.event === "regionGuide") {
    state.regionGuide = !!msg.on;
    const box = document.getElementById("showRegion");
    if (box) box.checked = state.regionGuide;
  }
  if (msg.event === "collapsed") setCollapsedUi(!!msg.on);
});
