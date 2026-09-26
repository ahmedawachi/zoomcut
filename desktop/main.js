// Zoomcut's desktop shell. It starts the local Zoomcut server - the same one
// `zoomcut` runs from a terminal - shows its editor in a native window, and
// makes sure the server and every ffmpeg it started go away with the window.
'use strict';

const { app, BrowserWindow, Menu, dialog, shell, ipcMain, session, screen } = require('electron');
const { spawn, execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

const isMac = process.platform === 'darwin';
const isWin = process.platform === 'win32';
const REPO = 'https://github.com/ahmedawachi/zoomcut';

// An AppImage cannot carry Chromium's setuid sandbox helper, and Ubuntu 24.04
// and later block the user-namespace sandbox for unconfined apps. There the
// window would never open; the page it shows is our own local server, so we
// run without that sandbox rather than not at all. Installed .deb builds keep it.
if (process.platform === 'linux' && process.env.APPIMAGE && userNamespacesBlocked()) {
  app.commandLine.appendSwitch('no-sandbox');
}

let server = null;          // { proc, url, origin }
let win = null;
let quitting = false;
let logStream = null;
const pendingPaths = [];    // files the OS asked us to open before the editor was up

// ------------------------------------------------------------------ server

function userNamespacesBlocked() {
  try {
    return fs.readFileSync('/proc/sys/kernel/apparmor_restrict_unprivileged_userns', 'utf8').trim() === '1';
  } catch {
    return false;
  }
}

function log(line) {
  try {
    if (!logStream) {
      fs.mkdirSync(app.getPath('logs'), { recursive: true });
      logStream = fs.createWriteStream(path.join(app.getPath('logs'), 'zoomcut.log'), { flags: 'w' });
    }
    logStream.write(line.endsWith('\n') ? line : line + '\n');
  } catch { /* logging must never take the app down */ }
}

/** Where the server lives: bundled inside the app, or - running from a
 *  checkout with `npm start` - the repository's own Python package. */
function serverCommand() {
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, 'server');
    const ext = isWin ? '.exe' : '';
    return {
      cmd: path.join(dir, 'zoomcut' + ext),
      args: [],
      cwd: dir,
      // the ffmpeg we ship, never whatever else happens to be on PATH
      env: {
        ZOOMCUT_FFMPEG: path.join(dir, 'bin', 'ffmpeg' + ext),
        ZOOMCUT_FFPROBE: path.join(dir, 'bin', 'ffprobe' + ext),
      },
    };
  }
  return {
    cmd: process.env.ZOOMCUT_PYTHON || (isWin ? 'python' : 'python3'),
    args: ['-m', 'zoomcut'],
    cwd: path.resolve(__dirname, '..'),
    env: {},
  };
}

function startServer() {
  return new Promise((resolve, reject) => {
    const { cmd, args, cwd, env } = serverCommand();
    log(`starting: ${cmd} ${args.join(' ')}`);
    const proc = spawn(cmd, [...args, 'ui', '--port', '0', '--no-open', '--exit-with-stdin'], {
      cwd,
      env: { ...process.env, ...env, PYTHONUNBUFFERED: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      // its own process group on macOS and Linux, so a stuck server can be
      // stopped together with any ffmpeg it started
      detached: !isWin,
    });
    let started = false;
    let tail = '';
    const keep = s => { tail = (tail + s).slice(-6000); log(s); };
    const timer = setTimeout(() => fail(new Error('Zoomcut took more than a minute to start.')), 60000);
    function fail(err) {
      clearTimeout(timer);
      if (started) return;
      started = true;
      try { proc.kill(); } catch { /* already gone */ }
      err.log = tail;
      reject(err);
    }
    readline.createInterface({ input: proc.stdout }).on('line', line => {
      keep(line + '\n');
      const m = line.match(/zoomcut ui\s+->\s+(http:\/\/\S+)/);
      if (m && !started) {
        started = true;
        clearTimeout(timer);
        const url = m[1].endsWith('/') ? m[1] : m[1] + '/';
        server = { proc, url, origin: new URL(url).origin };
        resolve(server);
      }
    });
    proc.stderr.on('data', d => keep(String(d)));
    proc.on('error', e => fail(new Error(`Zoomcut could not start: ${e.message}`)));
    proc.on('exit', (code, signal) => {
      log(`server exited: code=${code} signal=${signal}`);
      if (!started) return fail(new Error(`Zoomcut stopped while starting (exit ${code ?? signal}).`));
      if (server && server.proc === proc) server = null;
      if (!quitting) serverLost(code ?? signal, tail);
    });
  });
}

/** Ask the server to finish - it stops when its stdin closes, finishing a
 *  recording and stopping renders first - and only then insist. */
function stopServer(timeoutMs = 20000) {
  const s = server;
  if (!s) return Promise.resolve();
  server = null;
  return new Promise(resolve => {
    const proc = s.proc;
    if (proc.exitCode !== null || proc.signalCode !== null) return resolve();
    const done = () => { clearTimeout(timer); resolve(); };
    const timer = setTimeout(() => { killTree(proc); setTimeout(resolve, 500); }, timeoutMs);
    proc.once('exit', done);
    try { proc.stdin.end(); } catch { killTree(proc); }
  });
}

function killTree(proc) {
  if (!proc.pid) return;
  if (isWin) {
    execFile('taskkill', ['/PID', String(proc.pid), '/T', '/F'], { windowsHide: true }, () => {});
  } else {
    try { process.kill(-proc.pid, 'SIGKILL'); } catch { try { proc.kill('SIGKILL'); } catch { /* gone */ } }
  }
}

async function state() {
  if (!server) return null;
  try {
    // quitting asks this first: a server that stopped answering must not
    // turn Quit into a hang
    const r = await fetch(server.url + 'api/state', { signal: AbortSignal.timeout(3000) });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ window

function createWindow() {
  const area = screen.getPrimaryDisplay().workAreaSize;
  win = new BrowserWindow({
    width: Math.min(1480, area.width),
    height: Math.min(940, area.height),
    minWidth: 960,
    minHeight: 620,
    title: 'Zoomcut',
    backgroundColor: '#09090d',
    show: false,
    // the editor's own top bar becomes the title bar
    titleBarStyle: isMac ? 'hiddenInset' : isWin ? 'hidden' : 'default',
    trafficLightPosition: isMac ? { x: 18, y: 19 } : undefined,
    titleBarOverlay: isWin ? { color: '#0e0f14', symbolColor: '#a3a6b8', height: 51 } : undefined,
    icon: process.platform !== 'linux' ? undefined
      : app.isPackaged ? path.join(process.resourcesPath, 'icon.png')
      : path.join(__dirname, '..', 'docs', 'zoomcut.png'),
    autoHideMenuBar: !isMac,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
      additionalArguments: [`--zoomcut-version=${app.getVersion()}`],
    },
  });
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => { win = null; });
  win.on('close', e => {
    if (quitting) return;
    // closing the last window quits, so it asks the same questions
    e.preventDefault();
    app.quit();
  });

  const wc = win.webContents;
  // nothing but our own server's pages in this window
  wc.on('will-navigate', (e, url) => {
    if (!server || !url.startsWith(server.origin + '/')) {
      e.preventDefault();
      if (/^https?:/.test(url)) shell.openExternal(url);
    }
  });
  wc.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  wc.on('render-process-gone', (_e, d) => {
    log(`renderer gone: ${d.reason}`);
    if (!quitting && d.reason !== 'clean-exit' && server) wc.loadURL(server.url);
  });
  win.loadFile(path.join(__dirname, 'loading.html'));
}

function send(action, arg) {
  if (win && !win.isDestroyed()) win.webContents.send('menu', action, arg);
}

function openPath(p) {
  if (!p) return;
  if (!server || !win || win.webContents.isLoading()) { pendingPaths.push(p); return; }
  send('open-path', p);
  if (win.isMinimized()) win.restore();
  win.focus();
}

async function openDialog() {
  if (!win) return null;
  const r = await dialog.showOpenDialog(win, {
    title: 'Open a recording or project',
    properties: ['openFile'],
    filters: [
      { name: 'Recordings and projects', extensions: ['mov', 'mp4', 'm4v', 'mkv', 'webm', 'avi', 'json'] },
      { name: 'All files', extensions: ['*'] },
    ],
  });
  return r.canceled ? null : r.filePaths[0];
}

async function showServerFailure(err) {
  const r = await dialog.showMessageBox({
    type: 'error',
    title: 'Zoomcut could not start',
    message: 'Zoomcut could not start.',
    detail: `${err.message}\n\n${(err.log || '').trim().split('\n').slice(-12).join('\n')}`,
    buttons: ['Quit', 'Show log'],
    defaultId: 0,
  });
  if (r.response === 1) shell.showItemInFolder(path.join(app.getPath('logs'), 'zoomcut.log'));
}

async function serverLost(code, tail) {
  const r = await dialog.showMessageBox(win || undefined, {
    type: 'error',
    title: 'Zoomcut stopped',
    message: 'Zoomcut\'s engine stopped unexpectedly.',
    detail: `Exit ${code}. Your recordings and exports are safe in the Zoomcut folder.\n\n` +
            tail.trim().split('\n').slice(-8).join('\n'),
    buttons: ['Restart', 'Quit', 'Show log'],
    defaultId: 0,
  });
  if (r.response === 2) shell.showItemInFolder(path.join(app.getPath('logs'), 'zoomcut.log'));
  if (r.response !== 0) { app.quit(); return; }
  try {
    await startServer();
    if (win) win.loadURL(server.url);
  } catch (err) {
    await showServerFailure(err);
    app.quit();
  }
}

// ------------------------------------------------------------------ menus

function buildMenu() {
  const item = (label, accelerator, action, extra = {}) => ({ label, accelerator, click: () => send(action), ...extra });
  const template = [
    ...(isMac ? [{
      label: 'Zoomcut',
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    }] : []),
    {
      label: 'File',
      submenu: [
        item('New Recording…', 'CmdOrCtrl+N', 'new'),
        { label: 'Open…', accelerator: 'CmdOrCtrl+O', click: async () => openPath(await openDialog()) },
        { type: 'separator' },
        item('Save Project', 'CmdOrCtrl+S', 'save'),
        item('Export…', 'CmdOrCtrl+E', 'export'),
        { type: 'separator' },
        { label: 'Show the Zoomcut Folder', click: async () => { const s = await state(); if (s?.outDir) { fs.mkdirSync(s.outDir, { recursive: true }); shell.openPath(s.outDir); } } },
        ...(isMac ? [] : [{ type: 'separator' }, { role: 'quit' }]),
      ],
    },
    {
      label: 'Edit',
      submenu: [
        // undo is the editor's own; the page hands it to a text field when one has focus
        item('Undo', 'CmdOrCtrl+Z', 'undo'),
        item('Redo', isMac ? 'Shift+Cmd+Z' : 'Ctrl+Y', 'redo'),
        { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        ...(app.isPackaged ? [] : [{ type: 'separator' }, { role: 'reload' }, { role: 'toggleDevTools' }]),
      ],
    },
    ...(isMac ? [{ role: 'windowMenu' }] : []),
    {
      role: 'help',
      submenu: [
        item('Keyboard Shortcuts', isMac ? 'Cmd+/' : 'Ctrl+/', 'shortcuts'),
        { type: 'separator' },
        { label: 'Zoomcut on GitHub', click: () => shell.openExternal(REPO) },
        { label: 'Report a Problem…', click: () => shell.openExternal(REPO + '/issues/new') },
        { label: 'Show the Log', click: () => shell.showItemInFolder(path.join(app.getPath('logs'), 'zoomcut.log')) },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ------------------------------------------------------------------ lifecycle

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_e, argv) => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
    const file = argv.slice(1).find(a => !a.startsWith('-') && fs.existsSync(a) && fs.statSync(a).isFile());
    if (file) openPath(file);
  });
  // macOS: a file dropped on the Dock icon, or opened with Zoomcut from Finder
  app.on('open-file', (e, p) => { e.preventDefault(); openPath(p); });

  app.whenReady().then(async () => {
    app.setAboutPanelOptions({
      applicationName: 'Zoomcut',
      applicationVersion: app.getVersion(),
      copyright: 'MIT licensed. Includes ffmpeg (GPL) — see the licenses folder.',
      website: REPO,
    });
    session.defaultSession.setPermissionRequestHandler((_wc, permission, cb) => {
      cb(['clipboard-sanitized-write', 'fullscreen'].includes(permission));
    });
    ipcMain.handle('open-dialog', () => openDialog());
    ipcMain.handle('open-folder', async (_e, p) => {
      if (typeof p !== 'string' || !path.isAbsolute(p)) return false;
      fs.mkdirSync(p, { recursive: true });
      return (await shell.openPath(p)) === '';
    });
    ipcMain.handle('open-screen-settings', () => shell.openExternal(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'));
    buildMenu();
    createWindow();
    try {
      await startServer();
    } catch (err) {
      log(`failed to start: ${err.message}`);
      await showServerFailure(err);
      quitting = true;
      app.quit();
      return;
    }
    const file = process.argv.slice(1).find(a => !a.startsWith('-') && a !== '.' && fs.existsSync(a) && fs.statSync(a).isFile());
    if (file) pendingPaths.push(path.resolve(file));
    // files asked for while starting are handed over once the editor is up
    win.webContents.on('did-finish-load', () => {
      if (!server || !win.webContents.getURL().startsWith(server.origin + '/')) return;
      while (pendingPaths.length) send('open-path', pendingPaths.shift());
    });
    win.loadURL(server.url);
  });

  app.on('window-all-closed', () => app.quit());

  app.on('before-quit', async e => {
    if (quitting) return;
    e.preventDefault();
    const s = await state();
    if (s?.recording || s?.progress?.state === 'running') {
      const what = s.recording ? 'Zoomcut is recording.' : `An export is ${s.progress.pct || 0}% done.`;
      const r = await dialog.showMessageBox(win || undefined, {
        type: 'warning',
        message: what,
        detail: s.recording
          ? 'Quitting stops the recording and keeps what has been recorded so far.'
          : 'Quitting cancels the export.',
        buttons: [s.recording ? 'Stop and Quit' : 'Cancel Export and Quit', 'Keep Going'],
        defaultId: 1,
        cancelId: 1,
      });
      if (r.response !== 0) return;
    }
    quitting = true;
    await stopServer();
    app.quit();
  });
}

// for the end-to-end tests: which server this window is talking to
globalThis.__zoomcut = { get pid() { return server?.proc.pid ?? null; }, get url() { return server?.url ?? null; } };
