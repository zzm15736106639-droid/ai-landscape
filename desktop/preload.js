const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('aiLandscape', {
  pickVideoFiles: () => ipcRenderer.invoke('dialog:videos'),
  pickSubtitleFiles: () => ipcRenderer.invoke('dialog:subtitles'),
  pickEffectFiles: () => ipcRenderer.invoke('dialog:effects'),
  pickOutputDirectory: () => ipcRenderer.invoke('dialog:output-directory'),
  showItemInFolder: path => ipcRenderer.invoke('shell:show-item', path),
  openPath: path => ipcRenderer.invoke('shell:open-path', path),
  platform: process.platform,
})
