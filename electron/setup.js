// Pierwsze uruchomienie: aplikacja sama przygotowuje silnik (Python + biblioteki).
// Najpierw sprawdza istniejące środowisko (Homebrew + ~/gamer/gr) — jeśli działa, zostaje przy nim.
// Jeśli go nie ma albo nie działa: pobiera samodzielnego Pythona (python-build-standalone)
// do ~/Library/Application Support/LiveDub/runtime i instaluje biblioteki z requirements.txt.
// Modele (głos, mowa, tłumacz) silnik pobiera sam przy pierwszym użyciu.
const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const HOME = os.homedir();
const IS_WIN = process.platform === "win32";
const RUNTIME = IS_WIN
  ? path.join(process.env.LOCALAPPDATA || path.join(HOME, "AppData", "Local"), "LiveDub", "runtime")
  : path.join(HOME, "Library/Application Support/LiveDub/runtime");
const RUNTIME_PY = IS_WIN ? path.join(RUNTIME, "python", "python.exe") : path.join(RUNTIME, "python", "bin", "python3");
const STAMP = path.join(RUNTIME, ".ready");
const PY_REPO = "astral-sh/python-build-standalone";
// macOS: ta sama wersja, na której silnik działa u autora; Windows: 3.12 — wszystkie biblioteki mają gotowe paczki
const PY_ASSET = IS_WIN
  ? new RegExp(
      `^cpython-3\\.12\\.\\d+\\+\\d+-${process.arch === "arm64" ? "aarch64" : "x86_64"}-pc-windows-msvc-install_only\\.tar\\.gz$`,
    )
  : /^cpython-3\.14\.\d+\+\d+-aarch64-apple-darwin-install_only\.tar\.gz$/;
const PY_LABEL = IS_WIN ? "Pythona 3.12 dla Windowsa" : "Pythona 3.14 dla Apple Silicon";
// dość, żeby silnik wstał; reszta bibliotek ładuje się leniwie
const PROBE = IS_WIN
  ? "import numpy, PIL, onnxruntime, supertonic, winrt.windows.media.ocr"
  : "import numpy, PIL, onnxruntime, supertonic";

const LEGACY_VENV = path.join(HOME, "gamer", "gr");
const BREW_CELLAR = "/opt/homebrew/Cellar/python@3.14";

function run(cmd, args, { env, onLine, timeout } = {}) {
  return new Promise((resolve) => {
    let out = "";
    let proc;
    try {
      proc = spawn(cmd, args, { env: env || process.env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    } catch (err) {
      resolve({ code: -1, out: String(err) });
      return;
    }
    const timer = timeout ? setTimeout(() => proc.kill(), timeout) : null;
    const feed = (chunk) => {
      const text = chunk.toString("utf8");
      out = (out + text).slice(-20000);
      if (onLine) text.split(/\r?\n/).forEach((line) => line.trim() && onLine(line.trim()));
    };
    proc.stdout.on("data", feed);
    proc.stderr.on("data", feed);
    proc.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({ code: -1, out: out + String(err) });
    });
    proc.on("close", (code) => {
      if (timer) clearTimeout(timer);
      resolve({ code, out });
    });
  });
}

// Homebrew Python: katalog najnowszej zainstalowanej 3.14 (zamiast wpisanej na sztywno 3.14.7)
function brewPythonHome() {
  try {
    const versions = fs
      .readdirSync(BREW_CELLAR)
      .filter((name) => /^3\.14\./.test(name))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
      .reverse();
    for (const ver of versions) {
      const home = path.join(BREW_CELLAR, ver, "Frameworks/Python.framework/Versions/3.14");
      if (fs.existsSync(home)) return home;
    }
  } catch (_err) {
    /* brak Homebrew */
  }
  return null;
}

// dotychczasowy zestaw: Engine.app (kopia Pythona z Homebrew) + venv ~/gamer/gr
function legacyEngine(engineDir, candidates) {
  if (IS_WIN) return null;
  const py = candidates.find((item) => fs.existsSync(item));
  const site = path.join(LEGACY_VENV, "lib/python3.14/site-packages");
  if (!py || !fs.existsSync(site)) return null;
  const env = {
    ...process.env,
    ELECTRON_RUN_AS_NODE: "",
    PYTHONUNBUFFERED: "1",
    VIRTUAL_ENV: LEGACY_VENV,
    PYTHONPATH: `${engineDir}${path.delimiter}${site}`,
    __PYVENV_LAUNCHER__: "",
  };
  const home = brewPythonHome();
  if (home) env.PYTHONHOME = home;
  return { py, env, kind: "legacy" };
}

function runtimeEngine(engineDir) {
  const env = { ...process.env, ELECTRON_RUN_AS_NODE: "", PYTHONUNBUFFERED: "1", PYTHONPATH: engineDir, PYTHONIOENCODING: "utf-8" };
  delete env.PYTHONHOME;
  delete env.VIRTUAL_ENV;
  return { py: RUNTIME_PY, env, kind: "runtime" };
}

async function works(engine) {
  if (!engine || !fs.existsSync(engine.py)) return false;
  const res = await run(engine.py, ["-c", PROBE], { env: engine.env, timeout: 60000 });
  return res.code === 0;
}

function hashFile(file) {
  return crypto.createHash("sha1").update(fs.readFileSync(file)).digest("hex");
}

async function findPythonAsset() {
  const headers = { Accept: "application/vnd.github+json", "User-Agent": "LiveDub-setup" };
  // CI (test na Windowsie): z tokenem, żeby nie trafić na limit zapytań bez logowania
  if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const rel = await fetch(`https://api.github.com/repos/${PY_REPO}/releases/latest`, { headers });
  if (!rel.ok) throw new Error(`GitHub (Python): HTTP ${rel.status}`);
  const release = await rel.json();
  // wydanie ma setki plików — lista stronami
  for (let page = 1; page <= 30; page += 1) {
    const res = await fetch(
      `https://api.github.com/repos/${PY_REPO}/releases/${release.id}/assets?per_page=100&page=${page}`,
      { headers },
    );
    if (!res.ok) throw new Error(`GitHub (Python): HTTP ${res.status}`);
    const assets = await res.json();
    const hit = assets.find((item) => PY_ASSET.test(item.name));
    if (hit) return hit;
    if (assets.length < 100) break;
  }
  throw new Error(`nie znalazłem ${PY_LABEL}`);
}

async function installPython(status, log) {
  status("Pierwsze uruchomienie: pobieram Pythona…");
  const asset = await findPythonAsset();
  log(`setup-python ${asset.name}`);
  const res = await fetch(asset.browser_download_url, { headers: { "User-Agent": "LiveDub-setup" } });
  if (!res.ok) throw new Error(`pobieranie Pythona: HTTP ${res.status}`);
  fs.mkdirSync(RUNTIME, { recursive: true });
  const tarball = path.join(RUNTIME, "python.tar.gz");
  fs.writeFileSync(tarball, Buffer.from(await res.arrayBuffer()));
  fs.rmSync(path.join(RUNTIME, "python"), { recursive: true, force: true });
  // Windows 10/11 ma własny tar (bsdtar) w System32
  const tar = IS_WIN ? path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "/usr/bin/tar";
  const untar = await run(tar, ["-xzf", tarball, "-C", RUNTIME]);
  fs.rmSync(tarball, { force: true });
  if (untar.code !== 0 || !fs.existsSync(RUNTIME_PY)) throw new Error(`rozpakowanie Pythona: ${untar.out.slice(-300)}`);
}

async function installPackages(requirements, env, status, log) {
  status("Pierwsze uruchomienie: instaluję składniki silnika (kilka minut)…");
  await run(RUNTIME_PY, ["-m", "pip", "install", "--upgrade", "pip"], { env });
  let count = 0;
  const res = await run(
    RUNTIME_PY,
    ["-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off", "-r", requirements],
    {
      env,
      onLine: (line) => {
        const match = line.match(/^(?:Collecting|Downloading) ([A-Za-z0-9_.\-]+)/);
        if (match && line.startsWith("Collecting")) {
          count += 1;
          status(`Instaluję składniki silnika… ${count}: ${match[1]}`);
        }
      },
    },
  );
  log(`setup-pip code=${res.code}`);
  if (res.code !== 0) throw new Error(`instalacja bibliotek: ${res.out.slice(-400)}`);
}

// Zwraca { py, env, kind } do uruchomienia silnika; w razie potrzeby najpierw instaluje.
async function ensureEngine({ engineDir, requirements, legacyCandidates, status, log }) {
  const legacy = legacyEngine(engineDir, legacyCandidates);
  if (legacy && (await works(legacy))) {
    log("engine legacy");
    return legacy;
  }
  const runtime = runtimeEngine(engineDir);
  const wanted = fs.existsSync(requirements) ? hashFile(requirements) : "";
  const stamp = fs.existsSync(STAMP) ? fs.readFileSync(STAMP, "utf8").trim() : "";
  if (fs.existsSync(RUNTIME_PY) && stamp === wanted && (await works(runtime))) {
    log("engine runtime");
    return runtime;
  }
  if (!fs.existsSync(RUNTIME_PY)) await installPython(status, log);
  // nowa wersja aplikacji z innym requirements.txt — pip doinstaluje tylko różnicę
  await installPackages(requirements, runtime.env, status, log);
  if (!(await works(runtime))) throw new Error("silnik nie startuje po instalacji");
  fs.writeFileSync(STAMP, wanted);
  status("Składniki gotowe — ładuję lektora (pierwszy raz pobiera modele głosu)…");
  log("engine runtime installed");
  return runtime;
}

module.exports = { ensureEngine, brewPythonHome };
