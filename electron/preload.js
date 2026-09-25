const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("gr", {
  send: (msg) => ipcRenderer.send("engine", msg),
  onEvent: (fn) => {
    ipcRenderer.on("engine-event", (_e, data) => fn(data));
  },
  setRegionGuide: (on) => ipcRenderer.send("region-guide", !!on),
  setCollapsed: (on) => ipcRenderer.send("set-collapsed", !!on),
  releaseFocus: () => ipcRenderer.send("release-focus"),
  openScreen: () => ipcRenderer.send("open-screen"),
  openMic: () => ipcRenderer.send("open-mic"),
  quit: () => ipcRenderer.send("quit-app"),
  appInfo: () => ipcRenderer.invoke("app-info"),
  checkUpdates: () => ipcRenderer.invoke("check-updates"),
  openUrl: (url) => ipcRenderer.send("open-url", url),
});
