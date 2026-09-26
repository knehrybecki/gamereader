const { contextBridge, ipcRenderer } = require("electron");

// Windows: okno zaznaczania paska napisów oddaje prostokąt (albo null = anulowano)
contextBridge.exposeInMainWorld("pick", {
  done: (rect) => ipcRenderer.send("pick-done", rect),
});
