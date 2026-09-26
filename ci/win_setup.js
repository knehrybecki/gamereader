// CI (Windows): instaluje silnik dokładnie tak, jak aplikacja przy pierwszym uruchomieniu
// (Python z python-build-standalone + requirements-win.txt), i zapisuje ścieżkę Pythona dla kolejnych kroków.
const fs = require("fs");
const path = require("path");
const { ensureEngine } = require("../electron/setup");

const root = path.resolve(__dirname, "..");
ensureEngine({
  engineDir: root,
  requirements: path.join(root, "requirements-win.txt"),
  legacyCandidates: [],
  status: (text) => console.log(`[status] ${text}`),
  log: (text) => console.log(`[log] ${text}`),
})
  .then((engine) => {
    console.log(`silnik: ${engine.py} (${engine.kind})`);
    if (process.env.GITHUB_ENV) fs.appendFileSync(process.env.GITHUB_ENV, `LIVEDUB_PY=${engine.py}\n`);
  })
  .catch((err) => {
    console.error(err && err.stack ? err.stack : err);
    process.exit(1);
  });
