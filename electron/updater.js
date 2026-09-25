// Automatyczna aktualizacja: raz przy starcie sprawdza najnowsze wydanie na GitHubie,
// a gdy jest nowsze — pobiera, podmienia LiveDub.app i uruchamia aplikację ponownie.
const { app } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const REPO = "knehrybecki/gamereader";
const ASSET = "LiveDub-mac-arm64.zip";
const TOKEN_FILE = path.join(os.homedir(), "Library/Application Support/GameReader/github-token");

// repo jest prywatne: token z pliku, zmiennej środowiskowej albo z zalogowanego `gh`
function githubToken() {
  const env = process.env.LIVEDUB_GITHUB_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
  if (env) return env.trim();
  try {
    const saved = fs.readFileSync(TOKEN_FILE, "utf8").trim();
    if (saved) return saved;
  } catch (_err) {
    /* brak pliku */
  }
  for (const gh of ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"]) {
    if (!fs.existsSync(gh)) continue;
    const res = spawnSync(gh, ["auth", "token"], { encoding: "utf8", timeout: 5000 });
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

function currentBundle() {
  // …/LiveDub.app/Contents/MacOS/LiveDub → …/LiveDub.app
  const bundle = path.resolve(process.execPath, "..", "..", "..");
  return bundle.endsWith(".app") ? bundle : null;
}

// Wynik: { state: "none" | "installing" | "no-token" | "unavailable" | "dev", text }
async function checkForUpdates({ log, status }) {
  if (!app.isPackaged || process.platform !== "darwin" || process.env.LIVEDUB_NO_UPDATE) {
    return { state: "dev", text: "Aktualizacje działają tylko w zainstalowanej aplikacji." };
  }
  const bundle = currentBundle();
  if (!bundle) return { state: "dev", text: "Nie znalazłem LiveDub.app." };
  const token = githubToken();
  const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
    headers: headers(token, "application/vnd.github+json"),
  });
  if (!res.ok) {
    log(`update-check http ${res.status}${token ? "" : " (brak tokena GitHub)"}`);
    if (!token) return { state: "no-token", text: "Brak tokena GitHub — nie mogę sprawdzić aktualizacji." };
    if (res.status === 404) return { state: "unavailable", text: "Nie ma jeszcze żadnego wydania (albo token nie ma dostępu)." };
    return { state: "unavailable", text: `GitHub odpowiedział błędem ${res.status}.` };
  }
  const release = await res.json();
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

  // silnik: zostaw lokalny Python (dopasowany do Homebrew na tym Macu), a nie ten z maszyny budującej
  const engineRel = path.join("Contents", "Helpers", "Engine.app", "Contents", "MacOS", "Engine");
  const oldEngine = path.join(bundle, engineRel);
  if (fs.existsSync(oldEngine)) {
    fs.mkdirSync(path.dirname(path.join(fresh, engineRel)), { recursive: true });
    fs.copyFileSync(oldEngine, path.join(fresh, engineRel));
    fs.chmodSync(path.join(fresh, engineRel), 0o755);
  }
  spawnSync("/usr/bin/xattr", ["-cr", fresh]);
  spawnSync("/usr/bin/codesign", ["--force", "--sign", "-", path.join(fresh, "Contents", "Resources", "GameReaderHelper")]);
  spawnSync("/usr/bin/codesign", ["--force", "--sign", "-", path.join(fresh, "Contents", "Helpers", "Engine.app")]);
  // podmieniony silnik psuje podpis całości — bez tego macOS uzna aplikację za uszkodzoną.
  // Z lokalnym certyfikatem (macos/make_signing_cert.sh) podpis jest stały i macOS
  // zachowuje zgodę na nagrywanie ekranu; bez niego ad-hoc = zgodę trzeba dać ponownie.
  run("/usr/bin/codesign", ["--force", "--deep", "--sign", signingIdentity(), fresh]);

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
