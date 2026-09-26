const electron = require("electron");
const app = electron.app;
const BrowserWindow = electron.BrowserWindow;
const ipcMain = electron.ipcMain;
const shell = electron.shell;
if (!app) {
  console.error("LiveDub musi startować przez Electron, nie Node.", typeof electron, electron);
  process.exit(1);
}
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const { desktopCapturer, session, screen, Menu, dialog, Tray, nativeImage } = electron;
const crypto = require("crypto");
const { checkForUpdates, cleanupOldCopies } = require("./updater");
const { ensureEngine } = require("./setup");
const pkg = require("./package.json");

const WIDGET = { width: 340, height: 480 };
const WIDGET_COLLAPSED = { width: 260, height: 44 };
const CORNERS = ["tl", "tr", "bl", "br"];

let mainWindow = null;
let helperProc = null;
let workerProc = null;
let hubServer = null;
let dockTimer = null;
let dockCorner = "tr";
let lastPsWin = null;
let applyingDock = false;
let userDragging = false;
let dragTimer = null;
let tapWin = null;
let tapClient = null;
let tapWait = null;
let regionGuideWin = null;
let regionGuideOn = false;
let lastRegion = null;
let collapsed = false;

const HOME = os.homedir();
const IS_WIN = process.platform === "win32";
const IS_MAC = process.platform === "darwin";
// Windows: silnik łączy się z Electronem przez 127.0.0.1 (z losowym tokenem) zamiast gniazda Unix
const HUB_TOKEN = IS_WIN ? crypto.randomBytes(16).toString("hex") : "";
let hubPort = 0;
let tray = null;
// Windows: druga kopia (np. drugi klik w skrót) tylko pokazuje widżet pierwszej
if (IS_WIN && !app.requestSingleInstanceLock()) {
  app.exit(0);
}
app.on("second-instance", () => {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.showInactive();
});
const MAIN_LOG = IS_WIN ? path.join(os.tmpdir(), "gamereader-main.log") : "/tmp/gamereader-main.log";
const ENGINE_LOG = IS_WIN ? path.join(os.tmpdir(), "livedub-engine.log") : "/tmp/livedub-engine.log";
const VENV = path.join(HOME, "gamer", "gr");
const PYTHON = path.join(VENV, "bin", "python3.14");
const PYTHON_FALLBACK = "/opt/homebrew/opt/python@3.14/bin/python3.14";
const SOCK = path.join(HOME, "Library/Application Support/GameReader/helper.sock");

function resource(...parts) {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, ...parts);
  }
  return path.join(__dirname, "..", ...parts);
}

function helperPath() {
  const candidates = [
    path.join(process.resourcesPath, "GameReaderHelper"),
    path.join(__dirname, "build", "GameReaderHelper"),
    path.join(HOME, "Applications/LiveDub.app/Contents/Resources/GameReaderHelper"),
  ];
  return candidates.find((item) => fs.existsSync(item)) || candidates[0];
}

function pythonBin() {
  const bundled = [
    path.join(process.resourcesPath, "..", "Helpers", "Engine.app", "Contents", "MacOS", "Engine"),
    path.join(__dirname, "build", "Engine.app", "Contents", "MacOS", "Engine"),
    path.join(HOME, "Applications/LiveDub.app/Contents/Helpers/Engine.app/Contents/MacOS/Engine"),
  ];
  const found = bundled.find((item) => fs.existsSync(item));
  if (found) return found;
  const brew = "/opt/homebrew/opt/python@3.14/bin/python3.14";
  if (fs.existsSync(brew)) return brew;
  if (fs.existsSync(PYTHON)) return PYTHON;
  return PYTHON_FALLBACK;
}

function sendToWindow(payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("engine-event", payload);
  }
  if ((payload.event === "ready" || payload.event === "state") && Array.isArray(payload.region) && payload.region.length === 4) {
    lastRegion = toDip(payload.region.map((n) => Number(n)));
    syncRegionGuide();
  }
  if (IS_WIN && (payload.event === "ready" || payload.event === "state") && "psWindow" in payload) {
    // Windows: okno gry zna silnik (lista okien systemu) — widżet przykleja się do niego
    const win = Array.isArray(payload.psWindow) && payload.psWindow.length === 4 ? toDip(payload.psWindow.map(Number)) : null;
    if (win) dockWidget({ x: win[0], y: win[1], width: win[2], height: win[3] });
    else lastPsWin = null;
  }
  if ((payload.event === "ready" || payload.event === "state") && payload.dockCorner) {
    if (CORNERS.includes(payload.dockCorner)) dockCorner = payload.dockCorner;
  }
  if ((payload.event === "ready" || payload.event === "state") && typeof payload.showRegion === "boolean") {
    if (regionGuideOn !== payload.showRegion) {
      regionGuideOn = payload.showRegion;
      syncRegionGuide();
    }
  }
  if ((payload.event === "ready" || payload.event === "state") && typeof payload.collapsed === "boolean") {
    if (collapsed !== payload.collapsed) setCollapsed(payload.collapsed, false);
  }
}

// Windows: silnik liczy w fizycznych pikselach, okna Electrona — w punktach (skalowanie 125 %, 150 %…)
function toDip(rect) {
  if (!IS_WIN || !rect || rect.length !== 4) return rect;
  const r = screen.screenToDipRect(null, { x: rect[0], y: rect[1], width: rect[2], height: rect[3] });
  return [r.x, r.y, r.width, r.height];
}

function widgetSize() {
  return collapsed ? WIDGET_COLLAPSED : WIDGET;
}

function ensureRegionGuide() {
  if (regionGuideWin && !regionGuideWin.isDestroyed()) return regionGuideWin;
  regionGuideWin = new BrowserWindow({
    width: 120,
    height: 40,
    x: 0,
    y: 0,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    movable: false,
    focusable: false,
    hasShadow: false,
    show: false,
    ...(IS_MAC ? { type: "panel" } : {}),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  regionGuideWin.setAlwaysOnTop(true, IS_WIN ? "screen-saver" : "floating");
  if (IS_MAC) regionGuideWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  // Windows: ramka nie może trafić do zrzutu, z którego OCR czyta napisy
  if (IS_WIN) regionGuideWin.setContentProtection(true);
  regionGuideWin.setIgnoreMouseEvents(true);
  regionGuideWin.loadFile(path.join(__dirname, "renderer", "region.html"));
  regionGuideWin.on("closed", () => {
    regionGuideWin = null;
  });
  return regionGuideWin;
}

function syncRegionGuide() {
  if (!regionGuideOn || !lastRegion || lastRegion.length !== 4) {
    if (regionGuideWin && !regionGuideWin.isDestroyed()) regionGuideWin.hide();
    return;
  }
  const [x, y, width, height] = lastRegion;
  if (!(width >= 8 && height >= 8)) return;
  const win = ensureRegionGuide();
  win.setIgnoreMouseEvents(true);
  win.setBounds({
    x: Math.round(x),
    y: Math.round(y),
    width: Math.round(width),
    height: Math.round(height),
  });
  if (!win.isVisible()) win.showInactive();
  win.setIgnoreMouseEvents(true);
}

function setRegionGuide(on) {
  regionGuideOn = !!on;
  syncRegionGuide();
  sendToWindow({ event: "regionGuide", on: regionGuideOn });
  releaseGameFocus();
}

function setCollapsed(on, persist = true) {
  collapsed = !!on;
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const size = widgetSize();
  applyingDock = true;
  const cur = mainWindow.getBounds();
  mainWindow.setMinimumSize(size.width, size.height);
  mainWindow.setMaximumSize(size.width, collapsed ? size.height : 640);
  mainWindow.setBounds({ x: cur.x, y: cur.y, width: size.width, height: size.height });
  mainWindow.webContents.send("engine-event", { event: "collapsed", on: collapsed });
  setTimeout(() => {
    applyingDock = false;
    if (lastPsWin) dockWidget(lastPsWin);
  }, 60);
  if (persist) engineSend({ cmd: "config", collapsed });
  releaseGameFocus();
}

function releaseGameFocus() {
  try {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.blur();
  } catch (_err) {
    /* ignore */
  }
}

function stopTap() {
  if (tapWin && !tapWin.isDestroyed()) tapWin.close();
  tapWin = null;
  tapWait = null;
}

function startTapWindow() {
  return new Promise((resolve, reject) => {
    stopTap();
    const ses = session.defaultSession;
    ses.setPermissionRequestHandler((_wc, perm, cb) => {
      cb(perm === "media" || perm === "display-capture" || perm === "fullscreen");
    });
    ses.setDisplayMediaRequestHandler(async (_req, callback) => {
      const sources = await desktopCapturer.getSources({
        types: ["window", "screen"],
        thumbnailSize: { width: 16, height: 16 },
      });
      const named = sources.find((item) => /remote\s*play|playstation/i.test(item.name));
      const fallback = sources.find((item) => String(item.id).startsWith("screen:")) || sources[0];
      const video = named || fallback;
      log(`tap-source ${video ? video.name : "none"}`);
      callback({ video, audio: "loopback" });
    });
    tapWin = new BrowserWindow({
      show: false,
      width: 8,
      height: 8,
      skipTaskbar: true,
      webPreferences: {
        preload: path.join(__dirname, "tap-preload.js"),
        contextIsolation: true,
        backgroundThrottling: false,
      },
    });
    tapWait = { resolve, reject };
    tapWin.loadFile(path.join(__dirname, "renderer", "tap.html"));
    tapWin.on("closed", () => {
      tapWin = null;
      if (tapWait) {
        tapWait.reject(new Error("tap window closed"));
        tapWait = null;
      }
    });
    setTimeout(() => {
      if (tapWait) {
        tapWait.reject(new Error("tap timeout"));
        tapWait = null;
      }
    }, 3500);
  });
}

function shotRegion(x, y, w, h, dest) {
  return new Promise((resolve, reject) => {
    const bin = helperPath();
    if (!fs.existsSync(bin)) {
      reject(new Error("Brak helpera zrzutu."));
      return;
    }
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const proc = spawn(bin, ["--shot", String(x), String(y), String(w), String(h), dest], { env });
    let err = "";
    proc.stderr.on("data", (chunk) => {
      err += chunk.toString("utf8");
    });
    proc.on("close", (code) => {
      if (code === 0 && fs.existsSync(dest)) resolve();
      else reject(new Error(err.trim() || "zrzut nie wszedł"));
    });
  });
}

function startSwiftTap(socket) {
  return new Promise((resolve, reject) => {
    const bin = helperPath();
    if (!fs.existsSync(bin)) {
      reject(new Error("Brak helpera dźwięku."));
      return;
    }
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const proc = spawn(bin, ["--tap"], { env, stdio: ["ignore", "pipe", "pipe"] });
    helperProc = proc;
    let message = "";
    let ok = false;
    proc.stderr.on("data", (chunk) => {
      const text = chunk.toString("utf8");
      message += text;
      if (!ok && /LISTENING|AUDIO_OK/i.test(message)) {
        ok = true;
        resolve(message.replace(/\s+/g, " ").trim() || "PS Remote Play");
      }
    });
    proc.stdout.on("data", (chunk) => {
      if (socket && !socket.destroyed) socket.write(chunk);
    });
    proc.on("close", () => {
      if (helperProc === proc) helperProc = null;
      if (!ok) reject(new Error(message.trim() || "PS Remote Play nie oddaje dźwięku."));
    });
    setTimeout(() => {
      if (!ok) {
        try {
          proc.kill();
        } catch (_err) {
          /* ignore */
        }
        reject(new Error(message.trim() || "PS Remote Play nie oddaje dźwięku."));
      }
    }, 9000);
  });
}

function pickRegion() {
  return new Promise((resolve) => {
    const bin = helperPath();
    if (!fs.existsSync(bin)) {
      resolve("ERR noexe");
      return;
    }
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const proc = spawn(bin, ["--pick"], { env });
    helperProc = proc;
    let out = "";
    proc.stdout.on("data", (chunk) => {
      out += chunk.toString("utf8");
    });
    proc.on("close", (code) => {
      if (helperProc === proc) helperProc = null;
      const parts = out
        .trim()
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      resolve(code === 0 && parts.length === 4 ? `OK ${parts.join(",")}` : "ERR cancel");
    });
  });
}

function writeLine(socket, text) {
  try {
    socket.write(`${text}\n`);
  } catch (_err) {
    /* ignore */
  }
}

function handleHub(socket, line) {
  const parts = line.trim().split(/\s+/);
  if (HUB_TOKEN) {
    if (parts.shift() !== HUB_TOKEN) {
      socket.destroy();
      return;
    }
  }
  const cmd = parts[0] || "";
  if (cmd === "PERM") {
    writeLine(socket, "OK");
    socket.end();
    return;
  }
  if (cmd === "SHOT" && parts.length >= 6) {
    const dest = parts[5];
    const x = Number(parts[1]);
    const y = Number(parts[2]);
    const w = Number(parts[3]);
    const h = Number(parts[4]);
    shotRegion(x, y, w, h, dest)
      .then(() => writeLine(socket, "OK"))
      .catch((err) => writeLine(socket, `ERR ${err.message || err}`))
      .finally(() => socket.end());
    return;
  }
  if (cmd === "WIN") {
    findRemotePlayWindow()
      .then((text) => writeLine(socket, `OK ${text}`))
      .catch((err) => writeLine(socket, `ERR ${err.message || err}`))
      .finally(() => socket.end());
    return;
  }
  if (cmd === "PICK") {
    (IS_WIN ? pickRegionWin() : pickRegion())
      .then((text) => writeLine(socket, text))
      .finally(() => socket.end());
    return;
  }
  if (cmd === "TAP") {
    if (tapClient && tapClient !== socket) {
      try {
        tapClient.destroy();
      } catch (_err) {
        /* ignore */
      }
    }
    tapClient = socket;
    const started = startSwiftTap(socket);
    started
      .then((name) => {
        writeLine(socket, `OK LISTENING ${name}`);
        socket.on("close", () => {
          stopTap();
          if (helperProc) helperProc.kill();
        });
        socket.on("error", () => {
          stopTap();
          if (helperProc) helperProc.kill();
        });
      })
      .catch((err) => {
        writeLine(socket, `ERR ${err.message || err}`);
        socket.end();
        tapClient = null;
        stopTap();
      });
    return;
  }
  writeLine(socket, "ERR unknown");
  socket.end();
}

function hubConnection(socket) {
  let buf = Buffer.alloc(0);
  let taken = false;
  socket.on("error", () => {});
  socket.on("data", (chunk) => {
    if (taken) return;
    buf = Buffer.concat([buf, chunk]);
    const idx = buf.indexOf(10);
    if (idx < 0) return;
    taken = true;
    handleHub(socket, buf.slice(0, idx).toString("utf8"));
  });
}

// Windows: zaznaczanie paska napisów — przezroczyste okno na cały ekran, przeciągnij prostokąt
function pickRegionWin() {
  return new Promise((resolve) => {
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const b = display.bounds;
    const win = new BrowserWindow({
      x: b.x,
      y: b.y,
      width: b.width,
      height: b.height,
      frame: false,
      transparent: true,
      backgroundColor: "#00000000",
      alwaysOnTop: true,
      skipTaskbar: true,
      resizable: false,
      movable: false,
      fullscreenable: false,
      hasShadow: false,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, "pick-preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    win.setAlwaysOnTop(true, "screen-saver");
    let done = false;
    const finish = (rect) => {
      if (done) return;
      done = true;
      ipcMain.removeListener("pick-done", onDone);
      if (!win.isDestroyed()) win.close();
      if (!rect || !(rect.width >= 8 && rect.height >= 8)) {
        resolve("ERR cancel");
        return;
      }
      const phys = screen.dipToScreenRect(null, {
        x: Math.round(b.x + rect.x),
        y: Math.round(b.y + rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      });
      resolve(`OK ${phys.x},${phys.y},${phys.width},${phys.height}`);
    };
    const onDone = (event, rect) => {
      if (event.sender === win.webContents) finish(rect);
    };
    ipcMain.on("pick-done", onDone);
    win.on("closed", () => finish(null));
    win.once("ready-to-show", () => {
      win.show();
      win.focus();
    });
    win.loadFile(path.join(__dirname, "renderer", "pick.html"));
  });
}

function startHub() {
  if (IS_WIN) {
    hubServer = net.createServer((socket) => hubConnection(socket));
    hubServer.listen(0, "127.0.0.1", () => {
      hubPort = hubServer.address().port;
      log(`hub-listen ${hubPort}`);
    });
    hubServer.on("error", (err) => log(`hub-err ${err}`));
    return;
  }
  try {
    fs.mkdirSync(path.dirname(SOCK), { recursive: true });
    fs.unlinkSync(SOCK);
  } catch (_err) {
    /* ignore */
  }
  hubServer = net.createServer((socket) => hubConnection(socket));
  hubServer.listen(SOCK, () => {
    try {
      fs.chmodSync(SOCK, 0o600);
    } catch (_err) {
      /* ignore */
    }
    log("hub-listen");
  });
  hubServer.on("error", (err) => log(`hub-err ${err}`));
}

async function startWorker() {
  const script = app.isPackaged
    ? path.join(process.resourcesPath, "engine", "gamereader_worker.py")
    : path.join(__dirname, "..", "gamereader_worker.py");
  const engineDir = path.dirname(script);
  const reqName = IS_WIN ? "requirements-win.txt" : "requirements.txt";
  const requirements = app.isPackaged
    ? path.join(process.resourcesPath, "engine", reqName)
    : path.join(__dirname, "..", reqName);
  if (IS_WIN) {
    // silnik dostaje port centrali w zmiennej środowiskowej — poczekaj, aż centrala słucha
    for (let i = 0; i < 50 && !hubPort; i += 1) await new Promise((r) => setTimeout(r, 100));
  }
  let engine;
  try {
    // dotychczasowe środowisko, a gdy go nie ma albo nie działa — instalacja przy pierwszym starcie
    engine = await ensureEngine({
      engineDir,
      requirements,
      legacyCandidates: IS_WIN ? [] : [pythonBin(), PYTHON],
      status: (text) => sendToWindow({ event: "status", text }),
      log,
    });
  } catch (err) {
    log(`setup-error ${err && err.stack ? err.stack : err}`);
    sendToWindow({
      event: "status",
      text: `Nie udało się przygotować silnika: ${err && err.message ? err.message : err}. Sprawdź internet i uruchom LiveDub ponownie.`,
    });
    return;
  }
  const hubEnv = IS_WIN
    ? { GAMEREADER_HUB: `127.0.0.1:${hubPort}:${HUB_TOKEN}`, PYTHONIOENCODING: "utf-8" }
    : { GAMEREADER_HELPER: helperPath(), GAMEREADER_SOCK: SOCK };
  workerProc = spawn(engine.py, [script], {
    cwd: engineDir,
    env: { ...engine.env, ...hubEnv },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  let buf = "";
  workerProc.stdout.on("data", (chunk) => {
    buf += chunk.toString("utf8");
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.event === "preview") {
          continue;
        }
        sendToWindow(msg);
      } catch (_err) {
        sendToWindow({ event: "status", text: line.slice(0, 160) });
      }
    }
  });
  workerProc.stderr.on("data", (chunk) => {
    const text = chunk.toString("utf8").trim();
    const last = text.split("\n").pop() || "";
    if (last && !/warning:|userwarning|futurewarning|expects mwt|deprecation|context leak|coreanalytics|onnxruntime|fetching|%\||\[W:/i.test(last)) {
      sendToWindow({ event: "status", text: last });
    }
  });
  workerProc.on("exit", (code) => {
    workerProc = null;
    sendToWindow({ event: "status", text: `Silnik się zatrzymał (${code ?? "?"}).` });
    sendToWindow({ event: "running", on: false });
  });
}

function engineSend(msg) {
  if (workerProc && workerProc.stdin.writable) {
    workerProc.stdin.write(JSON.stringify(msg) + "\n");
  }
}

function findRemotePlayWindow() {
  return new Promise((resolve, reject) => {
    const bin = helperPath();
    if (!fs.existsSync(bin)) {
      reject(new Error("Brak helpera."));
      return;
    }
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const proc = spawn(bin, ["--win"], { env });
    let out = "";
    let err = "";
    proc.stdout.on("data", (chunk) => {
      out += chunk.toString("utf8");
    });
    proc.stderr.on("data", (chunk) => {
      err += chunk.toString("utf8");
    });
    proc.on("close", (code) => {
      const parts = out
        .trim()
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      if (code === 0 && parts.length === 4) resolve(parts.join(","));
      else reject(new Error(err.trim() || "NO_REMOTE_PLAY"));
    });
  });
}

function parseWin(text) {
  const parts = String(text)
    .split(",")
    .map((item) => Number(item.trim()));
  if (parts.length !== 4 || parts.some((item) => !Number.isFinite(item))) return null;
  return { x: parts[0], y: parts[1], width: parts[2], height: parts[3] };
}

function cornerPoint(win, corner) {
  const display = screen.getDisplayMatching(win);
  const work = display.workArea;
  const inset = 8;
  const size = widgetSize();
  const top = win.y + 28;
  const spots = {
    tl: { x: win.x + inset, y: top },
    tr: { x: win.x + win.width - size.width - inset, y: top },
    bl: { x: win.x + inset, y: win.y + win.height - size.height - inset },
    br: { x: win.x + win.width - size.width - inset, y: win.y + win.height - size.height - inset },
  };
  let { x, y } = spots[corner] || spots.tr;
  x = Math.max(work.x + 4, Math.min(x, work.x + work.width - size.width - 4));
  y = Math.max(work.y + 4, Math.min(y, work.y + work.height - size.height - 4));
  return { x, y };
}

function nearestCorner(win, bounds) {
  let best = "tr";
  let bestDist = Infinity;
  for (const corner of CORNERS) {
    const pos = cornerPoint(win, corner);
    const dist = (bounds.x - pos.x) ** 2 + (bounds.y - pos.y) ** 2;
    if (dist < bestDist) {
      bestDist = dist;
      best = corner;
    }
  }
  return best;
}

function dockWidget(win) {
  if (!mainWindow || mainWindow.isDestroyed() || userDragging) return;
  lastPsWin = win;
  const size = widgetSize();
  const pos = cornerPoint(win, dockCorner);
  const cur = mainWindow.getBounds();
  if (Math.abs(cur.x - pos.x) <= 4 && Math.abs(cur.y - pos.y) <= 4 && cur.width === size.width && cur.height === size.height) {
    return;
  }
  applyingDock = true;
  mainWindow.setBounds({ x: pos.x, y: pos.y, width: size.width, height: size.height });
  setTimeout(() => {
    applyingDock = false;
  }, 80);
}

function onUserMoved() {
  if (applyingDock || !mainWindow || mainWindow.isDestroyed()) return;
  userDragging = true;
  clearTimeout(dragTimer);
  dragTimer = setTimeout(() => {
    userDragging = false;
    if (!lastPsWin || !mainWindow || mainWindow.isDestroyed()) return;
    dockCorner = nearestCorner(lastPsWin, mainWindow.getBounds());
    dockWidget(lastPsWin);
    engineSend({ cmd: "config", dockCorner });
  }, 160);
}

function startDockLoop() {
  if (dockTimer) clearInterval(dockTimer);
  const tick = () => {
    findRemotePlayWindow()
      .then((text) => {
        const win = parseWin(text);
        if (win) dockWidget(win);
      })
      .catch(() => {});
  };
  tick();
  dockTimer = setInterval(tick, 1600);
}

function createMain() {
  const display = screen.getPrimaryDisplay();
  const work = display.workArea;
  const size = widgetSize();
  mainWindow = new BrowserWindow({
    width: size.width,
    height: size.height,
    x: work.x + work.width - size.width - 18,
    y: work.y + 48,
    minWidth: WIDGET_COLLAPSED.width,
    minHeight: WIDGET_COLLAPSED.height,
    maxWidth: WIDGET.width,
    frame: false,
    title: "LiveDub",
    backgroundColor: "#0B0D12",
    alwaysOnTop: true,
    fullscreenable: false,
    resizable: false,
    // panel = widżet unosi się nad grą/filmem na pełnym ekranie, a ikona LiveDub zostaje w Docku
    // (samo visibleOnFullScreen chowa ikonę z Docka — stąd skipTransformProcessType niżej)
    ...(IS_MAC ? { type: "panel" } : {}),
    ...(IS_WIN ? { icon: path.join(__dirname, "icon.png") } : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.setAlwaysOnTop(true, IS_WIN ? "screen-saver" : "floating");
  if (IS_MAC) {
    mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    mainWindow.setWindowButtonVisibility(false);
  }
  // Windows: OCR zrzuca ekran — widżet nie może zasłonić napisów w zrzucie
  if (IS_WIN) mainWindow.setContentProtection(true);
  mainWindow.setMovable(true);
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.on("will-move", () => {
    userDragging = true;
  });
  mainWindow.on("moved", onUserMoved);
  mainWindow.on("closed", () => {
    mainWindow = null;
    if (dockTimer) clearInterval(dockTimer);
    dockTimer = null;
  });
}

function log(line) {
  try {
    fs.appendFileSync(MAIN_LOG, `${new Date().toISOString()} ${line}\n`);
  } catch (_err) {
    /* ignore */
  }
}


// Licencje składników — pokazywane w „O LiveDub” i w menu Pomoc
const LICENSES = [
  ["Electron", "MIT"],
  ["Supertonic 3 — kod", "MIT"],
  ["Supertonic 3 — model głosu", "OpenRAIL-M"],
  ["ONNX Runtime", "MIT"],
  ["NVIDIA Parakeet TDT v3 — model mowy", "CC BY 4.0"],
  ["parakeet-mlx", "Apache 2.0"],
  ["MLX", "MIT"],
  ["Argos Translate", "MIT"],
  ["NumPy", "BSD-3-Clause"],
  ["Pillow", "MIT-CMU"],
  ["python-mss", "MIT"],
  ["python-sounddevice", "MIT"],
  ["ocrmac", "MIT"],
  ["PyObjC", "MIT"],
  ["imageio-ffmpeg / FFmpeg", "BSD-2 / LGPL 2.1+"],
];

const HELP_TEXT = [
  "1. Włącz PS Remote Play (albo Netflixa/YouTube w Chrome) — okno musi być widoczne, nie zminimalizowane.",
  "2. LiveDub sam wykryje grę i zacznie czytać napisy (albo kliknij „Uruchom”).",
  "3. Przy polskich napisach wybierz tryb „Napisy” (Więcej → Napisy).",
  "4. Jeśli nic nie czyta: Ustawienia → Prywatność → Nagrywanie ekranu — włącz LiveDub.",
  "5. „Ścisz grę” ścisza dźwięk gry, gdy mówi lektor (działa dla dźwięku z Remote Play / Chrome).",
  "",
  "Gdy lektor się spóźnia albo coś nie gra: Pomoc → Otwórz log lektora i wyślij jego końcówkę.",
].join("\n");

const HELP_TEXT_WIN = [
  "1. Włącz PS Remote Play albo Netflixa/YouTube w Chrome — okno musi być widoczne, nie zminimalizowane.",
  "2. LiveDub sam wykryje grę i zacznie czytać napisy (albo kliknij „Uruchom”).",
  "3. Polskie znaki w napisach: Windows musi mieć język polski (Ustawienia → Czas i język → Język i region).",
  "4. Na Windowsie LiveDub czyta na razie tylko napisy — tłumaczenie dźwięku będzie w kolejnej wersji.",
  "",
  "Gdy lektor się spóźnia albo coś nie gra: ikona LiveDub w zasobniku → Otwórz log lektora i wyślij jego końcówkę.",
].join("\n");

function licensesText() {
  return LICENSES.map(([name, lic]) => `${name} — ${lic}`).join("\n");
}

function showAbout() {
  const year = new Date().getFullYear();
  dialog.showMessageBox({
    type: "info",
    message: `LiveDub ${app.getVersion()}`,
    detail: `Polski lektor na żywo do gier i filmów.\n© ${year} ${pkg.author || "Kamil"}. Wszelkie prawa zastrzeżone.\n\nLicencje składników:\n${licensesText()}`,
    buttons: ["OK"],
  });
}

// Windows: zamiast menu aplikacji (okno bez ramki go nie pokazuje) — ikona w zasobniku obok zegara
function setupTray() {
  const image = nativeImage.createFromPath(path.join(__dirname, "icon.png")).resize({ width: 16, height: 16 });
  tray = new Tray(image);
  tray.setToolTip("LiveDub");
  const toggleWidget = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isVisible()) mainWindow.hide();
    else mainWindow.showInactive();
  };
  const checkNow = async () => {
    const res = await runUpdateCheck();
    if (res && res.state === "installing") return;
    dialog.showMessageBox({ type: "info", message: "Aktualizacje", detail: (res && res.text) || "Gotowe.", buttons: ["OK"] });
  };
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Pokaż / ukryj widżet", click: toggleWidget },
      { label: "Zwiń / rozwiń widżet", click: () => setCollapsed(!collapsed, true) },
      { type: "separator" },
      {
        label: "Jak używać LiveDub",
        click: () => dialog.showMessageBox({ type: "info", message: "Jak używać LiveDub", detail: HELP_TEXT_WIN, buttons: ["OK"] }),
      },
      {
        label: "Otwórz log lektora",
        click: () => {
          if (!fs.existsSync(ENGINE_LOG)) fs.writeFileSync(ENGINE_LOG, "");
          shell.openPath(ENGINE_LOG);
        },
      },
      { label: "Ustawienia języka (polski OCR)", click: () => shell.openExternal("ms-settings:regionlanguage") },
      { label: "Sprawdź aktualizacje…", click: () => checkNow() },
      { label: "O LiveDub", click: () => showAbout() },
      { type: "separator" },
      { label: "Zakończ LiveDub", click: () => app.quit() },
    ]),
  );
  tray.on("click", toggleWidget);
}

function setupAppMenu() {
  if (IS_WIN) {
    Menu.setApplicationMenu(null);
    setupTray();
    return;
  }
  const year = new Date().getFullYear();
  app.setAboutPanelOptions({
    applicationName: "LiveDub",
    applicationVersion: app.getVersion(),
    version: "",
    copyright: `© ${year} ${pkg.author || "Kamil"}. Wszelkie prawa zastrzeżone.`,
    credits: `Polski lektor na żywo do gier i filmów.\n\nLicencje składników:\n${licensesText()}`,
  });
  const checkUpdatesNow = async () => {
    const res = await runUpdateCheck();
    if (res && res.state === "installing") return; // aplikacja zaraz sama się uruchomi ponownie
    dialog.showMessageBox({
      type: "info",
      message: "Aktualizacje",
      detail: (res && res.text) || "Gotowe.",
      buttons: ["OK"],
    });
  };
  const template = [
    {
      label: "LiveDub",
      submenu: [
        { role: "about", label: "O LiveDub" },
        { label: "Sprawdź aktualizacje…", click: () => checkUpdatesNow() },
        { type: "separator" },
        { role: "services", label: "Usługi" },
        { type: "separator" },
        { role: "hide", label: "Ukryj LiveDub" },
        { role: "hideOthers", label: "Ukryj pozostałe" },
        { role: "unhide", label: "Pokaż wszystkie" },
        { type: "separator" },
        { role: "quit", label: "Zakończ LiveDub" },
      ],
    },
    {
      label: "Edycja",
      submenu: [
        { role: "undo", label: "Cofnij" },
        { role: "redo", label: "Przywróć" },
        { type: "separator" },
        { role: "cut", label: "Wytnij" },
        { role: "copy", label: "Kopiuj" },
        { role: "paste", label: "Wklej" },
        { role: "selectAll", label: "Zaznacz wszystko" },
      ],
    },
    {
      label: "Okno",
      submenu: [
        { role: "minimize", label: "Minimalizuj" },
        { label: "Zwiń / rozwiń widżet", click: () => setCollapsed(!collapsed, true) },
        { type: "separator" },
        { role: "front", label: "Wszystko na wierzch" },
      ],
    },
    {
      role: "help",
      label: "Pomoc",
      submenu: [
        {
          label: "Jak używać LiveDub",
          click: () => dialog.showMessageBox({ type: "info", message: "Jak używać LiveDub", detail: HELP_TEXT, buttons: ["OK"] }),
        },
        {
          label: "Licencje składników",
          click: () => dialog.showMessageBox({ type: "info", message: "Licencje składników", detail: licensesText(), buttons: ["OK"] }),
        },
        { type: "separator" },
        {
          label: "Otwórz log lektora",
          click: () => {
            const file = ENGINE_LOG;
            if (!fs.existsSync(file)) fs.writeFileSync(file, "");
            shell.openPath(file);
          },
        },
        {
          label: "Ustawienia: nagrywanie ekranu",
          click: () => shell.openExternal("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(() => {
  log("ready");
  app.setName("LiveDub");
  setupAppMenu();
  try {
    startHub();
    createMain();
    if (!IS_WIN) startDockLoop();
    setTimeout(() => {
      startWorker().catch((err) => log(`worker-error ${err && err.stack ? err.stack : err}`));
    }, 400);
    // raz przy starcie: nowa wersja na GitHubie → pobierz, podmień i uruchom ponownie
    setTimeout(() => runUpdateCheck(), 4000);
    // macOS: stare kopie LiveDub w Aplikacjach (po wcześniejszych aktualizacjach) — do Kosza
    setTimeout(() => {
      cleanupOldCopies({ log })
        .then((n) => {
          if (n > 0) sendToWindow({ event: "status", text: `Przeniosłem do Kosza stare kopie LiveDub (${n}).` });
        })
        .catch((err) => log(`cleanup-error ${err && err.message ? err.message : err}`));
    }, 2500);
    log("windows-up");
  } catch (err) {
    log(`boot-error ${err && err.stack ? err.stack : err}`);
  }
});

app.on("window-all-closed", () => {
  log("all-closed");
  engineSend({ cmd: "quit" });
  stopTap();
  if (helperProc) helperProc.kill();
  if (hubServer) hubServer.close();
  app.quit();
});

process.on("uncaughtException", (err) => {
  log(`uncaught ${err && err.stack ? err.stack : err}`);
});

app.on("before-quit", () => {
  engineSend({ cmd: "quit" });
  stopTap();
  if (regionGuideWin && !regionGuideWin.isDestroyed()) regionGuideWin.close();
  if (helperProc) helperProc.kill();
  if (workerProc) workerProc.kill();
  if (hubServer) hubServer.close();
});

ipcMain.on("tap-ready", (_e, info) => {
  if (tapWait) {
    tapWait.resolve((info && info.name) || "system-audio");
    tapWait = null;
  }
});
ipcMain.on("tap-fail", (_e, text) => {
  if (tapWait) {
    tapWait.reject(new Error(text || "tap"));
    tapWait = null;
  }
});
ipcMain.on("tap-pcm", (_e, buffer) => {
  if (tapClient && !tapClient.destroyed) {
    try {
      tapClient.write(Buffer.from(buffer));
    } catch (_err) {
      /* ignore */
    }
  }
});
let updateCheck = null;
function runUpdateCheck() {
  // jedno sprawdzanie naraz (start aplikacji i przycisk w „O aplikacji”)
  if (!updateCheck) {
    updateCheck = checkForUpdates({ log, status: (text) => sendToWindow({ event: "status", text }) })
      .catch((err) => {
        log(`update-error ${err && err.message ? err.message : err}`);
        return { state: "error", text: `Aktualizacja nie wyszła: ${err && err.message ? err.message : err}` };
      })
      .finally(() => {
        updateCheck = null;
      });
  }
  return updateCheck;
}

ipcMain.handle("app-info", () => ({
  name: pkg.productName || "LiveDub",
  version: app.getVersion(),
  author: pkg.author,
  electron: process.versions.electron,
}));
ipcMain.handle("check-updates", () => runUpdateCheck());
ipcMain.on("open-url", (_e, url) => {
  if (typeof url === "string" && url.startsWith("https://")) shell.openExternal(url);
});
ipcMain.on("engine", (_e, msg) => engineSend(msg));
ipcMain.on("quit-app", () => app.quit());
ipcMain.on("region-guide", (_e, on) => setRegionGuide(!!on));
ipcMain.on("set-collapsed", (_e, on) => setCollapsed(!!on, true));
ipcMain.on("release-focus", () => releaseGameFocus());
ipcMain.on("open-screen", () => {
  if (IS_WIN) return; // Windows nie pyta o zgodę na zrzut ekranu
  shell.openExternal("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture");
});
ipcMain.on("open-mic", () => {
  shell.openExternal(
    IS_WIN ? "ms-settings:privacy-microphone" : "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
  );
});
