// The README's screenshots, taken from the real packaged app on macOS: the
// actual window, native title bar and all, captured by the window server.
//
//   node test/screenshots.mjs              # writes docs/screenshot-app.png, docs/screenshot-home.png
//
// Runs with ZOOMCUT_DEMO=1 (a fixed window list) on the synthetic demo
// recording, and captures only Zoomcut's own window - nothing else on the
// screen can end up in the images. Needs Screen Recording permission for
// whatever runs it, like any screen capture.
import { _electron as electron } from 'playwright-core';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, '..');
const ROOT = path.resolve(DESKTOP, '..');
const DOCS = path.join(ROOT, 'docs');
const APP = process.env.ZOOMCUT_APP || path.join(DESKTOP, 'dist/mac-arm64/Zoomcut.app/Contents/MacOS/Zoomcut');
// a neutral folder: its path shows on the home screen
const OUT = '/Users/Shared/Zoomcut';

if (process.platform !== 'darwin') throw new Error('the README screenshots are taken on macOS');
const sleep = ms => new Promise(r => setTimeout(r, ms));

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });
const env = { ...process.env, ZOOMCUT_DEMO: '1', ZOOMCUT_OUTPUT_DIR: OUT };
delete env.ELECTRON_RUN_AS_NODE;
const t0 = Date.now();
const step = what => console.log(`${((Date.now() - t0) / 1000).toFixed(1).padStart(6)}s  ${what}`);

// One launch of the app, sized like a laptop screen allows, and a way to
// capture just its window.
async function session(fn) {
  const app = await electron.launch({ executablePath: APP, env });
  try {
    const win = await app.firstWindow();
    await win.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 90000 });
    await app.evaluate(({ BrowserWindow }) => {
      const w = BrowserWindow.getAllWindows()[0];
      w.setSize(1480, 940);
      w.center();
      w.focus();
    });
    const windowId = (await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getMediaSourceId()))
      .split(':')[1];
    const shoot = async (name) => {
      // no toasts or tooltips in a README image
      await win.evaluate(() => { document.querySelectorAll('.toast, .tip').forEach(t => t.remove()); });
      await sleep(400);
      execFileSync('screencapture', ['-x', '-l', windowId, path.join(DOCS, name)]);
      step(`wrote docs/${name}`);
    };
    await fn(win, shoot);
  } finally {
    const closed = await Promise.race([app.close().then(() => true, () => true), sleep(60000).then(() => false)]);
    step(closed ? 'app closed' : 'the app did not quit within a minute');
    if (!closed) {
      app.process().kill('SIGKILL');
      throw new Error('the app did not quit');
    }
  }
}

try {
  // the editor, on the moment the result card is on screen - with a project
  // and a draft left in the folder for the home screen after
  await session(async (win, shoot) => {
    await win.waitForSelector('#home .hero');
    step('home');
    await win.setInputFiles('.start.open input[type=file]', path.join(DOCS, 'demo-recording.mp4'));
    await win.waitForSelector('#editor:not([hidden])', { timeout: 120000 });
    step('editor');
    await win.evaluate(() => fetch('/api/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }));
    await win.evaluate(() => fetch('/api/render', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preview: true }) }));
    // polled from here: waitForFunction takes a returned promise as truthy
    const end = Date.now() + 180000;
    let state;
    while ((state = await win.evaluate(async () => (await (await fetch('/api/progress')).json()).state)) !== 'done') {
      if (state === 'error' || Date.now() > end) throw new Error(`the draft did not render (${state})`);
      await sleep(1000);
    }
    step('draft rendered');
    await win.evaluate(() => { location.search = '?t=10.4'; });
    await win.waitForFunction(() => document.querySelector('#frame')?.classList.contains('live'), null, { timeout: 120000 });
    step('preview live');
    await sleep(1500);
    await shoot('screenshot-app.png');
  });

  // the home screen of a fresh launch, the folder already in use
  await session(async (win, shoot) => {
    await win.waitForSelector('#home .hero');
    await win.waitForFunction(() => document.querySelectorAll('.rcard:not(.skel)').length >= 2, null, { timeout: 30000 });
    await win.waitForFunction(() => [...document.querySelectorAll('.rposter img')].every(i => i.complete));
    step('home with recents');
    // as it opens on a laptop: the recents run on below the window
    await sleep(800);
    await shoot('screenshot-home.png');
  });
} finally {
  fs.rmSync(OUT, { recursive: true, force: true });
}
