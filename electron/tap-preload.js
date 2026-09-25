const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tap", {
  ready: (rate, name) => ipcRenderer.send("tap-ready", { rate, name }),
  pcm: (buffer) => ipcRenderer.send("tap-pcm", buffer),
  fail: (text) => ipcRenderer.send("tap-fail", String(text || "tap")),
});
