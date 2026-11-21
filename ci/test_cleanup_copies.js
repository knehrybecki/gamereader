// 20.11 20:30: uruchomiony build deweloperski (gamer-wt/…/electron/dist/mac-arm64/LiveDub.app) po starcie
// „posprzątał stare kopie” i przeniósł zainstalowaną /Applications/LiveDub.app do Kosza — po opróżnieniu
// Kosza LiveDub zniknął z Aplikacji. Uruchom: node --test ci/test_cleanup_copies.js
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const trashed = [];
const realLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === "electron") {
    return { app: { isPackaged: true, getVersion: () => "1.3.105" }, shell: { trashItem: async (p) => trashed.push(p) } };
  }
  return realLoad.call(this, request, parent, isMain);
};

const root = fs.mkdtempSync(path.join(os.tmpdir(), "livedub-cleanup-"));
process.env.HOME = root; // os.homedir() → ~/Applications w katalogu testu
const { cleanupOldCopies } = require("../electron/updater");

function fakeBundle(dir) {
  fs.mkdirSync(path.join(dir, "Contents", "MacOS"), { recursive: true });
  fs.writeFileSync(path.join(dir, "Contents", "Info.plist"), "<string>pl.kamil.gamereader.mac</string>");
  return dir;
}

function runningFrom(bundle) {
  process.execPath = path.join(bundle, "Contents", "MacOS", "LiveDub");
}

const ours = () => trashed.filter((p) => p.startsWith(root));
const log = () => {};

test("build deweloperski nie wyrzuca zainstalowanej LiveDub do Kosza", { skip: process.platform !== "darwin" }, async () => {
  trashed.length = 0;
  const installed = fakeBundle(path.join(root, "Applications", "LiveDub.app"));
  runningFrom(fakeBundle(path.join(root, "gamer-wt", "electron", "dist", "mac-arm64", "LiveDub.app")));
  const moved = await cleanupOldCopies({ log });
  assert.strictEqual(moved, 0);
  assert.deepStrictEqual(trashed, [], "nic nie idzie do Kosza");
  assert.ok(fs.existsSync(installed));
});

test("zainstalowana LiveDub dalej sprząta kopie po aktualizacjach", { skip: process.platform !== "darwin" }, async () => {
  trashed.length = 0;
  const apps = path.join(root, "Applications");
  const running = fakeBundle(path.join(apps, "LiveDub.app"));
  const leftover = fakeBundle(path.join(apps, "LiveDub 2.app"));
  const old = fakeBundle(path.join(apps, "LiveDub.app.old"));
  runningFrom(running);
  await cleanupOldCopies({ log });
  assert.deepStrictEqual(ours().sort(), [leftover, old].sort());
});
