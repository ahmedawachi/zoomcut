// The only bridge between the editor page and the desktop shell. The page is
// the same one a browser shows; this adds what only a native app can do, and
// nothing else - no Node, no filesystem, no shell.
'use strict';

const { contextBridge, ipcRenderer, webUtils } = require('electron');

const arg = name => (process.argv.find(a => a.startsWith(`--${name}=`)) || '').split('=').slice(1).join('=');

contextBridge.exposeInMainWorld('zoomcutDesktop', {
  platform: process.platform,
  version: arg('zoomcut-version'),
  /** A dropped or chosen file's real path, so it is opened where it is
   *  instead of being copied through an upload. */
  pathForFile(file) {
    try { return webUtils.getPathForFile(file) || null; } catch { return null; }
  },
  openDialog: () => ipcRenderer.invoke('open-dialog'),
  openFolder: p => ipcRenderer.invoke('open-folder', p),
  openScreenSettings: () => ipcRenderer.invoke('open-screen-settings'),
});

// menu items arrive as a DOM event the page listens for
ipcRenderer.on('menu', (_e, action, arg_) => {
  window.dispatchEvent(new CustomEvent('zoomcut:menu', { detail: { action, arg: arg_ } }));
});
