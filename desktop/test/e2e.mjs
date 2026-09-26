// End-to-end test of the desktop app: the real window, the real server it
// starts, the real bundled ffmpeg.
//
//   node test/e2e.mjs                 # the packaged app in dist/, or
//   ZOOMCUT_APP=/path/to/app node test/e2e.mjs
//   node test/e2e.mjs --dev           # this checkout, run with `electron .`
//   ZOOMCUT_E2E_RECORD=1 ...          # also record the screen for real (CI only:
//                                     #  it captures whatever is on the display)
import { _electron as electron } from 'playwright-core';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const ROOT = path.resolve(DESKTOP, '..');
const DEV = process.argv.includes('--dev');
const RECORD = process.env.ZOOMCUT_E2E_RECORD === '1';
const SHOTS = process.env.ZOOMCUT_E2E_SHOTS || path.join(DESKTOP, 'dist', 'e2e');
const DEMO = path.join(ROOT, 'docs', 'demo-recording.mp4');

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok });
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${!ok && detail ? '   ' + detail : ''}`);
  if (!ok && process.env.GITHUB_ACTIONS === 'true') {
    console.log(`::error title=desktop (${process.platform})::${name}${detail ? ' -- ' + String(detail).replace(/\n/g, ' ').slice(0, 300) : ''}`);
  }
  return ok;
}

function packagedApp() {
  if (process.env.ZOOMCUT_APP) return process.env.ZOOMCUT_APP;
  const dist = path.join(DESKTOP, 'dist');
  const candidates = {
    darwin: ['mac-arm64/Zoomcut.app/Contents/MacOS/Zoomcut', 'mac/Zoomcut.app/Contents/MacOS/Zoomcut'],
    win32: ['win-unpacked/Zoomcut.exe'],
    linux: ['linux-unpacked/zoomcut-desktop'],
  }[process.platform] || [];
  const hit = candidates.map(c => path.join(dist, c)).find(p => fs.existsSync(p));
  if (!hit) throw new Error(`no packaged app in ${dist} - build it with packaging/build_desktop.py --dir`);
  return hit;
}

const listening = port => new Promise(resolve => {
  const s = net.connect({ port, host: '127.0.0.1' });
  s.once('connect', () => { s.destroy(); resolve(true); });
  s.once('error', () => resolve(false));
});
// EPERM is a process that is there but will not be touched - Chromium's
// browser process on Windows, for one - not a process that is gone
const alive = pid => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, ms, step = 250) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return false;
}

/** ffmpeg processes still running that belong to this app's bundle. */
function strayFfmpeg(appPath) {
  if (process.platform === 'win32') {
    try {
      const out = execFileSync('powershell', ['-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | Select-Object -ExpandProperty ExecutablePath"], { encoding: 'utf8' });
      return out.split(/\r?\n/).filter(l => l && l.toLowerCase().includes(path.dirname(appPath).toLowerCase()));
    } catch { return []; }
  }
  try {
    const out = execFileSync('ps', ['-axo', 'pid=,command='], { encoding: 'utf8' });
    return out.split('\n').filter(l => /ffmpeg|ffprobe/.test(l) && l.includes(DEV ? ROOT : path.dirname(path.dirname(appPath))));
  } catch { return []; }
}

/** Windows: these processes and their children, with their parents - to say
 *  who is still holding on when something outlives the app. */
function processTable(pids) {
  try {
    const list = pids.filter(Boolean).join(',');
    return execFileSync('powershell', ['-NoProfile', '-Command',
      `Get-CimInstance Win32_Process | Where-Object { @(${list}) -contains $_.ProcessId -or @(${list}) -contains $_.ParentProcessId } | ` +
      "ForEach-Object { '{0}<-{1} {2}' -f $_.ProcessId, $_.ParentProcessId, $_.Name }"], { encoding: 'utf8' })
      .split(/\r?\n/).filter(Boolean).join(', ');
  } catch (e) { return `no process table (${e.message})`; }
}

// the videos in a folder, hidden half-written ones included (not the project
// files, which the editor may save at any moment)
const videos = dir => fs.readdirSync(dir).filter(n => /\.(mp4|mov|mkv)$/i.test(n)).sort();

// A crash or a force-quit gets no chance to stop anything, so the server has
// to notice by itself - its stdin closes with the app - and take any export,
// and the ffmpeg doing it, down with it. Killed here in the middle of one.
async function crashMidExport(exe, env, out) {
  const app = await electron.launch(DEV ? { args: ['.'], cwd: DESKTOP, env } : { executablePath: exe, env });
  let pid = null, url = null, before = null;
  const appPid = app.process().pid;
  try {
    const win = await app.firstWindow();
    await win.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 90000 });
    pid = await app.evaluate(() => globalThis.__zoomcut.pid);
    url = await app.evaluate(() => globalThis.__zoomcut.url);
    await win.setInputFiles('.start.open input[type=file]', DEMO);
    await win.waitForSelector('#editor:not([hidden])', { timeout: 120000 });
    before = videos(out);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.send('menu', 'export'));
    await win.waitForSelector('.xsheet', { timeout: 10000 });
    await win.click('.xfoot .btn.primary');                 // the full export
    const running = await until(() => win.evaluate(async () => {
      const p = await (await fetch('/api/progress')).json();
      return p.state === 'running' && p.pct > 0;
    }).catch(() => false), 60000, 200);
    check('an export is under way when the app is killed', running);
  } catch (e) {
    check('the crash run got to an export', false, e.stack || e.message);
  }
  app.process().kill('SIGKILL');
  if (pid) {
    const gone = await until(() => !alive(pid), 20000);
    let why = `pid ${pid}`;
    if (!gone) {
      // which way it failed: never told the app had gone, or told and stuck
      const serving = url && await fetch(url + 'api/progress', { signal: AbortSignal.timeout(3000) })
        .then(r => r.ok, () => false);
      why += serving ? ', still serving - it never noticed the app go' : ', not serving - stuck stopping';
      if (alive(appPid)) why += `; the app itself (pid ${appPid}) is still running`;
      if (process.platform === 'win32') why += '; ' + processTable([appPid, pid]);
    }
    check('killing the app mid-export stops its server', gone, why);
  }
  const stray = await until(() => strayFfmpeg(exe || ROOT).length === 0, 10000) ? [] : strayFfmpeg(exe || ROOT);
  check('...and its ffmpeg', stray.length === 0, stray.join(' | '));
  // whatever happened, leave the machine as it was
  if (pid && alive(pid)) {
    if (process.platform === 'win32') {
      try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* gone */ }
    } else {
      try { process.kill(pid, 'SIGKILL'); } catch { /* gone */ }
    }
  }
  if (before) {
    const after = videos(out);
    check('...and leaves no half-written export', JSON.stringify(after) === JSON.stringify(before),
      after.filter(n => !before.includes(n)).join(', '));
  }
}

async function main() {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'zoomcut-e2e-'));
  fs.mkdirSync(SHOTS, { recursive: true });
  const exe = DEV ? null : packagedApp();
  console.log(`desktop e2e on ${process.platform}: ${DEV ? 'this checkout (electron .)' : exe}`);
  const env = { ...process.env, ZOOMCUT_DEMO: '1', ZOOMCUT_OUTPUT_DIR: out };
  // set by editors that run tools from inside themselves (VS Code does): it
  // turns Electron into plain Node. No real launch of the app has it.
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch(DEV
    ? { args: ['.'], cwd: DESKTOP, env }
    : { executablePath: exe, env });
  let pid = null, port = null;
  try {
    const win = await app.firstWindow();
    await win.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 90000 });
    const url = await app.evaluate(() => globalThis.__zoomcut.url);
    pid = await app.evaluate(() => globalThis.__zoomcut.pid);
    port = Number(new URL(url).port);
    check('the app starts its own Zoomcut server on a free port', port > 0 && alive(pid), url);

    await win.waitForSelector('#home .hero', { timeout: 30000 });
    await win.waitForFunction(() => document.body.classList.contains('booted'));
    check('the home screen opens', true);
    check('the page knows it is inside the desktop app',
      await win.evaluate(() => document.documentElement.dataset.desktop) === process.platform);
    const st = await win.evaluate(() => fetch('/api/state').then(r => r.json()));
    check('ffmpeg is there without installing anything', st.ffmpeg === true, JSON.stringify(st.ffmpeg));
    await win.screenshot({ path: path.join(SHOTS, 'home.png') });

    // the bundled ffmpeg, not one on PATH: the server was pointed at it
    if (!DEV) {
      const ff = path.join(await app.evaluate(() => process.resourcesPath), 'server', 'bin');
      check('the bundled ffmpeg is inside the app', fs.existsSync(path.join(ff, process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg')), ff);
    }

    // open a recording the way a person does: choose it (a real path via the bridge)
    await win.setInputFiles('.start.open input[type=file]', DEMO);
    await win.waitForSelector('#editor:not([hidden])', { timeout: 120000 });
    const pj = await win.evaluate(() => fetch('/api/project').then(r => r.json()).then(j => j.project));
    check('a chosen file is opened where it is, not copied', path.resolve(pj.source) === path.resolve(DEMO), pj.source);
    check('the auto-director placed zooms', pj.camera.shots.some(s => s.zoom > 1.01));
    const live = await win.waitForFunction(() => document.querySelector('#frame').classList.contains('live'), null, { timeout: 120000 })
      .then(() => true, () => false);
    check('the live preview comes up', live);

    await win.keyboard.press('Space');
    await sleep(1500);
    const t = await win.evaluate(() => document.querySelector('#vid').currentTime);
    await win.keyboard.press('Space');
    check('the preview plays (H.264 in this window)', t > 0.5, `currentTime ${t}`);
    await win.screenshot({ path: path.join(SHOTS, 'editor.png') });

    // the menu drives the editor
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.send('menu', 'export'));
    const sheet = await win.waitForSelector('.xsheet', { timeout: 10000 }).then(() => true, () => false);
    check('File > Export opens the export sheet', sheet);
    if (sheet) {
      await win.click('.xfoot .btn:not(.primary)');             // Quick draft
      const done = await win.waitForSelector('.xvid', { timeout: 240000 }).then(() => true, () => false);
      check('a draft renders with the bundled ffmpeg', done);
      const draft = fs.readdirSync(out).find(n => n.endsWith('-preview.mp4'));
      check('the draft is a real file in the output folder', !!draft && fs.statSync(path.join(out, draft)).size > 10000, draft);
      await win.keyboard.press('Escape');
    }

    if (RECORD) {
      await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.send('menu', 'new'));
      await win.waitForSelector('#home:not([hidden])');
      const modes = await win.$$eval('.modes .mode', b => b.map(x => x.textContent));
      await win.click(`.modes .mode:nth-child(${modes.findIndex(m => /Display/.test(m)) + 1})`);
      // the checkbox itself is hidden behind its switch: click what a person would
      const cd = win.locator('label.opt', { hasText: 'Countdown' });
      if (await cd.locator('input').isChecked()) await cd.locator('.sw').click();
      check('the countdown switches off', !(await cd.locator('input').isChecked()));
      await win.click('#btnRecord');
      const rec = await win.waitForSelector('#recOverlay', { timeout: 15000 }).then(() => true, () => false);
      check('recording starts from the app', rec);
      await sleep(3500);
      await win.click('#recOverlay .stop');
      // polled from here: waitForFunction takes a returned promise as truthy
      const edited = await until(() => win.evaluate(async () => !document.querySelector('#editor').hidden
        && /capture-/.test((await (await fetch('/api/project')).json()).project?.source || '')).catch(() => false), 180000, 1000);
      check('the recording opens in the editor', edited);
      await win.screenshot({ path: path.join(SHOTS, 'recorded.png') });
    }
  } catch (e) {
    check('the run completed', false, e.stack || e.message);
  } finally {
    const t = Date.now();
    await app.close().catch(() => {});
    // the shell kills a server that has not stopped in 20 s, so a quit that
    // long means the server never heard it and the app hung on the way out
    const ms = Date.now() - t;
    check('quitting takes a moment, not a timeout', ms < 10000, `${ms} ms`);
  }

  // nothing may outlive the window
  if (pid) check('quitting stops the server', await until(() => !alive(pid), 20000), `pid ${pid}`);
  if (port) check('...and frees its port', await until(async () => !(await listening(port)), 10000), `port ${port}`);
  const stray = await until(() => strayFfmpeg(exe || ROOT).length === 0, 10000) ? [] : strayFfmpeg(exe || ROOT);
  check('...and leaves no ffmpeg running', stray.length === 0, stray.join(' | '));

  await crashMidExport(exe, env, out);
  fs.rmSync(out, { recursive: true, force: true });

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
