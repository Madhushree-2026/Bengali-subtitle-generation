// frontend/preload.js
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Open a native file picker and return the chosen file path
  openFile: () => ipcRenderer.invoke('open-file-dialog'),

  /**
   * FIX: Read a local file as Base64 via the main process (Node.js fs).
   * The renderer cannot use fetch('file:///...') due to Electron CSP.
   * This bridge sends the file bytes safely over IPC instead.
   *
   * @param {string} filePath  Absolute path returned by openFile()
   * @returns {Promise<{base64: string, mimeType: string}|{error: string}>}
   */
  readFileAsBase64: (filePath) => ipcRenderer.invoke('read-file-as-base64', filePath),
});