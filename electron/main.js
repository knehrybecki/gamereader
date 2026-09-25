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
const { desktopCapturer, session, screen } = electron;

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
    lastRegion = payload.region.map((n) => Number(n));
    syncRegionGuide();
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
    type: "panel",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  regionGuideWin.setAlwaysOnTop(true, "floating");
  regionGuideWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
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
    pickRegion()
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

function startHub() {
  try {
    fs.mkdirSync(path.dirname(SOCK), { recursive: true });
    fs.unlinkSync(SOCK);
  } catch (_err) {
    /* ignore */
  }
  hubServer = net.createServer((socket) => {
    let buf = Buffer.alloc(0);
    let taken = false;
    socket.on("data", (chunk) => {
      if (taken) return;
      buf = Buffer.concat([buf, chunk]);
      const idx = buf.indexOf(10);
      if (idx < 0) return;
      taken = true;
      handleHub(socket, buf.slice(0, idx).toString("utf8"));
    });
  });
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

function startWorker() {
  const py = pythonBin();
  const script = app.isPackaged
    ? path.join(process.resourcesPath, "engine", "gamereader_worker.py")
    : path.join(__dirname, "..", "gamereader_worker.py");
  const engineDir = path.dirname(script);
  const site = path.join(VENV, "lib/python3.14/site-packages");
  workerProc = spawn(py, [script], {
    cwd: engineDir,
    env: {
      ...process.env,
      ELECTRON_RUN_AS_NODE: "",
      PYTHONUNBUFFERED: "1",
      VIRTUAL_ENV: VENV,
      PYTHONPATH: `${engineDir}${path.delimiter}${site}`,
      GAMEREADER_HELPER: helperPath(),
      GAMEREADER_SOCK: SOCK,
      PYTHONHOME: "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14",
      __PYVENV_LAUNCHER__: "",
    },
    stdio: ["pipe", "pipe", "pipe"],
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
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.setAlwaysOnTop(true, "floating");
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  mainWindow.setWindowButtonVisibility(false);
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
    fs.appendFileSync("/tmp/gamereader-main.log", `${new Date().toISOString()} ${line}\n`);
  } catch (_err) {
    /* ignore */
  }
}

app.whenReady().then(() => {
  log("ready");
  app.setName("LiveDub");
  try {
    startHub();
    createMain();
    startDockLoop();
    setTimeout(startWorker, 400);
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
ipcMain.on("engine", (_e, msg) => engineSend(msg));
ipcMain.on("quit-app", () => app.quit());
ipcMain.on("region-guide", (_e, on) => setRegionGuide(!!on));
ipcMain.on("set-collapsed", (_e, on) => setCollapsed(!!on, true));
ipcMain.on("release-focus", () => releaseGameFocus());
ipcMain.on("open-screen", () => {
  shell.openExternal("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture");
});
ipcMain.on("open-mic", () => {
  shell.openExternal("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone");
});
