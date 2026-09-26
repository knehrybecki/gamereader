// Automatyczna aktualizacja: raz przy starcie sprawdza najnowsze wydanie na GitHubie,
// a gdy jest nowsze — pobiera, podmienia LiveDub.app i uruchamia aplikację ponownie.
const { app } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// publiczne wydania — działają bez tokena; prywatne repo z kodem tylko jako zapas (autor z tokenem)
const PUBLIC_REPO = "knehrybecki/livedub-releases";
const PRIVATE_REPO = "knehrybecki/gamereader";
const IS_WIN = process.platform === "win32";
const ASSET = IS_WIN ? "LiveDub-Setup-win-x64.exe" : "LiveDub-mac-arm64.zip";
const TOKEN_FILE = IS_WIN
  ? path.join(process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming"), "LiveDub", "github-token")
  : path.join(os.homedir(), "Library/Application Support/GameReader/github-token");

// token (opcjonalny): z pliku, zmiennej środowiskowej albo z zalogowanego `gh`
function githubToken() {
  const env = process.env.LIVEDUB_GITHUB_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
  if (env) return env.trim();
  try {
    const saved = fs.readFileSync(TOKEN_FILE, "utf8").trim();
    if (saved) return saved;
  } catch (_err) {
    /* brak pliku */
  }
  const ghs = IS_WIN ? ["gh"] : ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"];
  for (const gh of ghs) {
    if (!IS_WIN && !fs.existsSync(gh)) continue;
    const res = spawnSync(gh, ["auth", "token"], { encoding: "utf8", timeout: 5000, windowsHide: true });
    if (res.status === 0 && res.stdout.trim()) return res.stdout.trim();
  }
  return "";
}

const SIGN_NAME = "LiveDub Local";

function signingIdentity() {
  const res = spawnSync("/usr/bin/security", ["find-identity", "-v", "-p", "codesigning"], {
    encoding: "utf8",
    timeout: 5000,
  });
  return res.status === 0 && (res.stdout || "").includes(SIGN_NAME) ? SIGN_NAME : "-";
}

function newer(remote, local) {
  const parse = (v) => String(v || "").replace(/^v/i, "").split(/[.-]/).slice(0, 3).map((n) => parseInt(n, 10) || 0);
  const a = parse(remote);
  const b = parse(local);
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

function headers(token, accept) {
  const out = { Accept: accept, "User-Agent": "LiveDub-updater", "X-GitHub-Api-Version": "2022-11-28" };
  if (token) out.Authorization = `Bearer ${token}`;
  return out;
}

function run(cmd, args) {
  const res = spawnSync(cmd, args, { encoding: "utf8", timeout: 120000 });
  if (res.status !== 0) throw new Error(`${path.basename(cmd)}: ${(res.stderr || "").trim() || res.status}`);
}

// Najnowsze wydanie z obu repo (publiczne może chwilowo zostawać w tyle za prywatnym).
async function latestRelease(token, log) {
  const repos = token ? [PUBLIC_REPO, PRIVATE_REPO] : [PUBLIC_REPO];
  let best = null;
  let lastStatus = 0;
  for (const repo of repos) {
    try {
      const res = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, {
        headers: headers(token, "application/vnd.github+json"),
      });
      if (!res.ok) {
        lastStatus = res.status;
        log(`update-check ${repo} http ${res.status}`);
        continue;
      }
      const release = await res.json();
      if (!best || newer(release.tag_name, best.tag_name)) best = release;
    } catch (err) {
      log(`update-check ${repo} ${err && err.message ? err.message : err}`);
    }
  }
  return { release: best, status: lastStatus };
}

// Silnik autora: Engine.app = kopia Pythona z Homebrew tego Maca + venv ~/gamer/gr (setup.js: legacyEngine).
// Inni używają Pythona z ~/Library/Application Support/LiveDub/runtime, więc Engine.app ich nie dotyczy.
function usesLegacyEngine() {
  return fs.existsSync(path.join(os.homedir(), "gamer", "gr", "lib", "python3.14", "site-packages"));
}

function currentBundle() {
  // …/LiveDub.app/Contents/MacOS/LiveDub → …/LiveDub.app
  const bundle = path.resolve(process.execPath, "..", "..", "..");
  return bundle.endsWith(".app") ? bundle : null;
}

// Windows: nowy instalator (NSIS) po cichu instaluje wersję na miejsce starej i uruchamia LiveDub
async function installWindows({ asset, token, version, log, status }) {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "livedub-update-"));
  const setup = path.join(work, ASSET);
  const dl = await fetch(asset.url, { headers: headers(token, "application/octet-stream") });
  if (!dl.ok) throw new Error(`pobieranie: HTTP ${dl.status}`);
  fs.writeFileSync(setup, Buffer.from(await dl.arrayBuffer()));
  status(`Aktualizacja ${version} gotowa — instaluję i uruchamiam ponownie…`);
  log(`update-install ${version}`);
  // /S = bez okien, --updated --force-run = po instalacji uruchom LiveDub (instalator electron-builder)
  spawn(setup, ["/S", "--updated", "--force-run"], { detached: true, stdio: "ignore", windowsHide: true }).unref();
  setTimeout(() => app.quit(), 800);
  return { state: "installing", text: `Instaluję wersję ${version}…` };
}

// Wynik: { state: "none" | "installing" | "unavailable" | "dev", text }
async function checkForUpdates({ log, status }) {
  if (!app.isPackaged || !["darwin", "win32"].includes(process.platform) || process.env.LIVEDUB_NO_UPDATE) {
    return { state: "dev", text: "Aktualizacje działają tylko w zainstalowanej aplikacji." };
  }
  const bundle = IS_WIN ? null : currentBundle();
  if (!IS_WIN && !bundle) return { state: "dev", text: "Nie znalazłem LiveDub.app." };
  const token = githubToken();
  const { release, status: httpStatus } = await latestRelease(token, log);
  if (!release) {
    if (httpStatus === 404) return { state: "unavailable", text: "Nie ma jeszcze żadnego wydania." };
    if (httpStatus) return { state: "unavailable", text: `GitHub odpowiedział błędem ${httpStatus}.` };
    return { state: "unavailable", text: "Brak połączenia z GitHubem — nie mogę sprawdzić aktualizacji." };
  }
  const local = app.getVersion();
  if (!newer(release.tag_name, local)) {
    log(`update-none ${local} (najnowsza ${release.tag_name})`);
    return { state: "none", text: `Masz najnowszą wersję (${local}).` };
  }
  const asset = (release.assets || []).find((item) => item.name === ASSET);
  if (!asset) {
    log(`update-no-asset ${release.tag_name}`);
    return { state: "unavailable", text: `Wydanie ${release.tag_name} nie ma jeszcze paczki.` };
  }
  const version = String(release.tag_name).replace(/^v/i, "");
  status(`Pobieram aktualizację ${version}…`);
  log(`update-download ${local} -> ${version}`);
  if (IS_WIN) return installWindows({ asset, token, version, log, status });

  const work = fs.mkdtempSync(path.join(os.tmpdir(), "livedub-update-"));
  const zip = path.join(work, ASSET);
  const dl = await fetch(asset.url, { headers: headers(token, "application/octet-stream") });
  if (!dl.ok) throw new Error(`pobieranie: HTTP ${dl.status}`);
  fs.writeFileSync(zip, Buffer.from(await dl.arrayBuffer()));

  const unpacked = path.join(work, "app");
  fs.mkdirSync(unpacked);
  run("/usr/bin/ditto", ["-x", "-k", zip, unpacked]);
  const fresh = path.join(unpacked, "LiveDub.app");
  if (!fs.existsSync(path.join(fresh, "Contents", "MacOS"))) throw new Error("paczka bez LiveDub.app");

  // Paczka z CI ma stały podpis „LiveDub Release” — nietknięta zachowuje zgody macOS (nagrywanie ekranu).
  // Tylko silnik autora wymaga podmiany Engine (lokalny Python z Homebrew), a to psuje podpis całości.
  let resign = false;
  const engineRel = path.join("Contents", "Helpers", "Engine.app", "Contents", "MacOS", "Engine");
  const oldEngine = path.join(bundle, engineRel);
  if (usesLegacyEngine() && fs.existsSync(oldEngine)) {
    fs.mkdirSync(path.dirname(path.join(fresh, engineRel)), { recursive: true });
    fs.copyFileSync(oldEngine, path.join(fresh, engineRel));
    fs.chmodSync(path.join(fresh, engineRel), 0o755);
    resign = true;
  }
  spawnSync("/usr/bin/xattr", ["-cr", fresh]);
  if (!resign && spawnSync("/usr/bin/codesign", ["--verify", "--deep", "--strict", fresh]).status !== 0) {
    log("update-signature-invalid");
    resign = true;
  }
  if (resign) {
    spawnSync("/usr/bin/codesign", ["--force", "--sign", "-", path.join(fresh, "Contents", "Resources", "GameReaderHelper")]);
    spawnSync("/usr/bin/codesign", ["--force", "--sign", "-", path.join(fresh, "Contents", "Helpers", "Engine.app")]);
    // zepsuty podpis całości = macOS uzna aplikację za uszkodzoną.
    // Z lokalnym certyfikatem (macos/make_signing_cert.sh) podpis jest stały i macOS
    // zachowuje zgodę na nagrywanie ekranu; bez niego ad-hoc = zgodę trzeba dać ponownie.
    run("/usr/bin/codesign", ["--force", "--deep", "--sign", signingIdentity(), fresh]);
  }

  // podmiana po zamknięciu tej instancji; stara wersja zostaje jako kopia, gdyby coś poszło źle
  const script = path.join(work, "swap.sh");
  fs.writeFileSync(
    script,
    [
      "#!/bin/sh",
      `while kill -0 ${process.pid} 2>/dev/null; do sleep 0.3; done`,
      `OLD="${bundle}"`,
      `NEW="${fresh}"`,
      'BAK="$OLD.old"',
      'rm -rf "$BAK"',
      'if mv "$OLD" "$BAK" && mv "$NEW" "$OLD"; then',
      '  rm -rf "$BAK"',
      '  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$OLD" >/dev/null 2>&1',
      "else",
      '  [ -d "$OLD" ] || mv "$BAK" "$OLD"',
      "fi",
      'open "$OLD"',
      `rm -rf "${work}"`,
      "",
    ].join("\n"),
    { mode: 0o755 },
  );
  status(`Aktualizacja ${version} gotowa — uruchamiam ponownie…`);
  log(`update-install ${version}`);
  spawn("/bin/sh", [script], { detached: true, stdio: "ignore" }).unref();
  setTimeout(() => app.quit(), 1500);
  return { state: "installing", text: `Instaluję wersję ${version}…` };
}

module.exports = { checkForUpdates, newer };
