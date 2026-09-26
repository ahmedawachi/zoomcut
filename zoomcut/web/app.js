// Zoomcut editor entry point: boot, views, the record -> analyse -> edit
// flow, drag and drop, and the keyboard.
import { S, on, emit, loadProject, undo, redo, canUndo, canRedo, undoLabel, redoLabel,
         checkpoint, changed, select, selected, flush, flushOnExit, sourceName } from './store.js';
import { api, upload } from './api.js';
import { clampCenter } from './camera.js';
import { initPreview, togglePlay, pause, seek, step, showExact, setLoop, fullscreen } from './preview.js';
import { initTimeline, zoomBy, fit, toggleSnap, revealPlayhead } from './timeline.js';
import { initInspector, showTab, refreshWallpapers } from './inspector.js';
import { initExporter, openExport, resume, playFile } from './exporter.js';
import { initHome, showHome, recordingOverlay, uploadProgress } from './home.js';
import { addZoomAt, splitAt, deleteSelected, selectRelative, setTrim, DEFAULT_ZOOM } from './ops.js';
import { $, h, icon, toast, tooltips, modal, isTyping, kbd, fmt, store, remember, debounce } from './ui.js';

const VIDEO = /\.(mov|mp4|m4v|mkv|webm|avi)$/i;
const IMAGE = /\.(png|jpe?g|webp|heic|bmp|tiff?)$/i;
const PROJECT = /\.json$/i;

// Inside the desktop app the same page gets a small native bridge: real file
// paths, native dialogs and a menu. In a browser this is null and nothing
// below changes.
const DESKTOP = window.zoomcutDesktop || null;
if (DESKTOP) document.documentElement.dataset.desktop = DESKTOP.platform;

// ---------------------------------------------------------------- views
function showView(v) {
  S.view = v;
  $('#home').hidden = v !== 'home';
  $('#editor').hidden = v !== 'editor';
  document.body.dataset.view = v;
  $('#crumb').hidden = !(v === 'editor' && S.project);
  for (const el of document.querySelectorAll('.editor-only')) el.hidden = v !== 'editor';
  if (v === 'home') showHome();
  title();
}
on('view', showView);

function title() {
  const rec = S.sys?.recording ? `● ${fmt(S.sys.recordSeconds || 0, 0)} · ` : '';
  document.title = rec + (S.project && S.view === 'editor' ? `${sourceName()} — Zoomcut` : 'Zoomcut');
}

function paintCrumb() {
  if (!S.project) return;
  const i = S.project.sourceInfo || {};
  $('#pName').textContent = sourceName();
  $('#pName').dataset.tip = S.project.source;
  $('#pMeta').textContent = `${i.width}×${i.height} · ${fmt(i.duration || 0, 1)}`;
}

// ---------------------------------------------------------------- open things
let busy = null;
function busyOverlay(titleText, detail) {
  busy?.();
  const el = h('div', { class: 'busy-overlay', role: 'status' },
    h('div', { class: 'bo-card' },
      h('div', { class: 'scanner' }, h('i'), h('i'), h('i')),
      h('h2', null, titleText), h('p', null, detail)));
  $('#overlays').append(el);
  const done = () => { el.classList.add('out'); setTimeout(() => el.remove(), 200); if (busy === done) busy = null; };
  busy = done;
  return done;
}

/** New recordings start from the look you used last. */
function lastLook() {
  const look = store('look', null);
  if (!look) return {};
  const style = look.style ? JSON.parse(JSON.stringify(look.style)) : undefined;
  // an uploaded background may be gone by now; never start a project on it
  if (style?.background?.type === 'image') style.background.type = 'wallpaper';
  return { style, output: look.output, spring: look.spring };
}
const saveLook = debounce(() => {
  if (!S.doc) return;
  remember('look', { style: S.doc.style, output: S.doc.output, spring: S.doc.spring });
}, 800);
on('doc', saveLook);

async function analyze(path) {
  pause();
  const name = path.split(/[\\/]/).pop();
  const done = busyOverlay('Planning the camera', `Reading ${name} and finding the moments worth zooming into…`);
  try {
    const look = lastLook();
    const r = await api('/api/analyze', { source: path, style: look.style, output: look.output });
    open(r.project);
    if (look.spring) { Object.assign(S.doc.spring, look.spring); changed('spring'); }
    const n = S.doc.segs.length, c = S.cuts.length;
    toast(n ? `Placed ${n} zoom${n === 1 ? '' : 's'} around ${c} view change${c === 1 ? '' : 's'}. Press Space to watch.`
            : 'Nothing moved enough to zoom into — add zooms by hand on the Zoom track.', { kind: 'ok', ms: 6000 });
  } catch (e) {
    toast(e.message, { kind: 'err', ms: 12000 });
  } finally { done(); }
}

async function loadFile(path) {
  pause();
  const done = busyOverlay('Opening project', path.split(/[\\/]/).pop());
  try {
    const r = await api('/api/load', { path });
    open(r.project);
  } catch (e) { toast(e.message, { kind: 'err', ms: 10000 }); }
  finally { done(); }
}

function open(pj) {
  loadProject(pj);
  showView('editor');
  paintCrumb();
  // start on the first real camera move - a wide frame says nothing about the
  // edit - unless the address asks for a moment (/?t=10.4)
  const first = [...S.doc.segs].sort((a, b) => a.start - b.start)[0];
  const asked = parseFloat(new URLSearchParams(location.search).get('t'));
  seek(Number.isFinite(asked) ? asked
       : first ? Math.min(first.end, first.start + 0.9) : Math.min(S.dur * 0.15, 2));
  requestAnimationFrame(() => { fit(); revealPlayhead(); });
}

async function openFile(file) {
  // the desktop app knows where the file is, so it is opened in place
  const local = DESKTOP?.pathForFile(file);
  if (local) return openPath(local);
  if (IMAGE.test(file.name)) {
    if (!S.project) { toast('That is an image — open a recording first, then use it as the background.'); return; }
    try {
      const r = await upload(file).promise;
      setBackgroundImage(r.path);
    } catch (e) { toast(e.message, { kind: 'err' }); }
    return;
  }
  if (!VIDEO.test(file.name)) { toast(`${file.name} is not a video Zoomcut can open.`, { kind: 'err' }); return; }
  const up = upload(file, f => uploadProgress(f));
  const done = S.view === 'home' ? () => uploadProgress(null) : busyOverlay('Copying the recording', file.name);
  uploadProgress(0);
  try {
    const r = await up.promise;
    done();
    await analyze(r.path);
  } catch (e) {
    done();
    toast(e.message, { kind: 'err', ms: 10000 });
  }
}

function setBackgroundImage(path) {
  checkpoint('Background');
  Object.assign(S.doc.style.background, { type: 'image', path });
  changed('style');
  showTab('background');
  toast('Background set.', { kind: 'ok', action: { label: 'Undo', run: doUndo } });
}

/** A file by its path: a recording, a saved project, or a background image. */
async function openPath(p) {
  if (!p) return;
  const name = p.split(/[\\/]/).pop();
  if (PROJECT.test(name)) return loadFile(p);
  if (IMAGE.test(name)) {
    if (!S.project) { toast('That is an image — open a recording first, then use it as the background.'); return; }
    return setBackgroundImage(p);
  }
  if (!VIDEO.test(name)) { toast(`${name} is not a video Zoomcut can open.`, { kind: 'err' }); return; }
  await analyze(p);
}

// ---------------------------------------------------------------- recording
let closeRec = null;
on('recording', info => {
  closeRec?.();
  closeRec = recordingOverlay(info, stopRecording);
});

async function stopRecording() {
  try {
    const r = await api('/api/record/stop', {});
    closeRec?.(); closeRec = null;
    await analyze(r.path);
  } catch (e) {
    closeRec?.(); closeRec = null;
    toast(e.message, { kind: 'err', ms: 12000 });
  }
}

// ---------------------------------------------------------------- status
function paintStatus() {
  const s = S.sys;
  const btn = $('#statusBtn');
  if (offline || !s) {
    btn.classList.add('bad');
    $('#statusText').textContent = offline ? 'Not connected' : 'Connecting…';
    btn.dataset.tip = 'Zoomcut is not running. Start it again with `zoomcut`, then reload.';
    return;
  }
  const [ok] = s.permission || [true];
  const good = ok && s.ffmpeg;
  btn.classList.toggle('bad', !good);
  $('#statusText').textContent = good ? `${s.platform}` : !s.ffmpeg ? 'ffmpeg missing' : 'Permission needed';
  btn.dataset.tip = good ? 'Everything Zoomcut needs is here' : 'Something needs attention';
  title();
  if (s.recording && !closeRec) {
    closeRec = recordingOverlay({ started: Date.now() - (s.recordSeconds || 0) * 1000, what: 'Recording' }, stopRecording);
  }
}

function statusSheet() {
  const s = S.sys || {};
  const [ok, why] = s.permission || [false, ''];
  const line = (good, t, d, path) => h('div', { class: 'st-row' + (good ? ' ok' : ' bad') }, icon(good ? 'check' : 'alert'),
    h('div', null, h('b', null, t), d ? h('span', { class: path ? 'path' : null }, d) : null));
  const m = modal(h('div', { class: 'status-sheet' },
    h('header', { class: 'xhead' }, h('div', null, h('h2', null, 'Zoomcut ', s.version || ''), h('p', null, `${s.platform || ''} · captures with ${s.backend || '?'}`)),
      h('button', { class: 'ib', type: 'button', 'aria-label': 'Close', onclick: () => m.close() }, icon('x'))),
    line(!!s.ffmpeg, s.ffmpeg ? (DESKTOP ? 'ffmpeg is built in' : 'ffmpeg is ready') : 'ffmpeg is missing',
      s.ffmpeg ? null : 'macOS: brew install ffmpeg · Windows: winget install Gyan.FFmpeg · Linux: apt/dnf/pacman install ffmpeg'),
    line(ok, ok ? 'Screen recording is allowed' : 'Screen recording is blocked', ok ? null : why),
    line(true, 'Files go to', s.outDir, true),
    DESKTOP && s.outDir ? h('button', { class: 'btn small', type: 'button', onclick: () => DESKTOP.openFolder(s.outDir) },
      icon('folder'), 'Open the folder') : null,
    h('p', { class: 'note dim' }, 'Everything runs on this machine. Nothing is uploaded.')), { cls: 'small', label: 'Status' });
}

let offline = false;
async function pollState() {
  try {
    S.sys = await api('/api/state');
    if (offline) toast('Connected to Zoomcut again.', { kind: 'ok', ms: 2500 });
    offline = false;
    emit('sys', S.sys);
  } catch {
    if (!offline) toast('Lost the connection to Zoomcut — is it still running?', { kind: 'err', ms: 6000 });
    offline = true;
  }
  paintStatus();
  setTimeout(pollState, S.sys?.recording && !offline ? 500 : 1500);
}

// ---------------------------------------------------------------- history
function doUndo() {
  const l = undo();
  if (l) toast(`Undid ${l.toLowerCase()}`, { ms: 1800, action: { label: 'Redo', run: doRedo } });
}
function doRedo() {
  const l = redo();
  if (l) toast(`Redid ${l.toLowerCase()}`, { ms: 1800 });
}
on('undo', doUndo);
on('history', () => {
  $('#tbUndo').disabled = !canUndo();
  $('#tbRedo').disabled = !canRedo();
  $('#tbUndo').dataset.tip = canUndo() ? `Undo ${undoLabel().toLowerCase()}` : 'Nothing to undo';
  $('#tbRedo').dataset.tip = canRedo() ? `Redo ${redoLabel().toLowerCase()}` : 'Nothing to redo';
});
on('error', e => toast(e.message, { kind: 'err', ms: 8000 }));

async function saveProject() {
  if (!S.project) return;
  try {
    await flush();
    const r = await api('/api/save', {});
    toast(`Project saved — ${r.path.split(/[\\/]/).pop()}`, {
      kind: 'ok', ms: 6000, action: { label: 'Show', run: () => api('/api/reveal', { path: r.path }).catch(e => toast(e.message, { kind: 'err' })) } });
  } catch (e) { toast(e.message, { kind: 'err' }); }
}

// ---------------------------------------------------------------- double-click to zoom
on('focus-point', async ({ cx, cy, seg }) => {
  // the zoom under the playhead takes the new focus, selected or not
  seg ||= S.doc?.segs.find(s => S.t >= s.start && S.t <= s.end);
  if (seg) {
    select(seg.id);
    checkpoint('Reframe zoom');
    [seg.cx, seg.cy] = clampCenter(seg.zoom, cx, cy);
    changed('segs');
    return;
  }
  await addZoomAt(S.t, { cx, cy, zoom: DEFAULT_ZOOM });
});

// ---------------------------------------------------------------- keyboard
const SHORTCUTS = [
  ['Playback', [[['Space'], 'Play / pause'], [['←', '→'], 'Step one frame'], [['Shift+←', 'Shift+→'], 'Jump one second'],
                [['Home', 'End'], 'Go to start / end'], [['L'], 'Loop playback'], [['F'], 'Render the exact frame']]],
  ['Zooms', [[['Z'], 'Add a zoom at the playhead'], [['S'], 'Split the zoom under the playhead'], [['⌫'], 'Delete the selected zoom'],
             [['[', ']'], 'Previous / next zoom'], [['Esc'], 'Deselect'], [['Double-click'], 'Zoom to a point on the preview']]],
  ['Timeline', [[['I', 'O'], 'Trim start / end at the playhead'], [['=', '−'], 'Zoom the timeline in / out'], [['0'], 'Fit the whole clip'],
                [['N'], 'Snapping on / off'], [['Mod+Scroll'], 'Zoom the timeline'], [['Alt+Drag'], 'Move without snapping']]],
  ['Project', [[['Mod+Z'], 'Undo'], [['Shift+Mod+Z'], 'Redo'], [['Mod+S'], 'Save the project'], [['Mod+E'], 'Export'], [['?'], 'This list']]],
];

function shortcutSheet() {
  const m = modal(h('div', { class: 'keys' },
    h('header', { class: 'xhead' }, h('div', null, h('h2', null, 'Keyboard shortcuts')),
      h('button', { class: 'ib', type: 'button', 'aria-label': 'Close', onclick: () => m.close() }, icon('x'))),
    h('div', { class: 'keys-grid' }, ...SHORTCUTS.map(([group, list]) => h('section', null, h('h3', null, group),
      ...list.map(([k, d]) => h('div', { class: 'krow' }, h('span', null, d),
        h('span', { class: 'kk' }, ...k.map(x => h('kbd', null, kbd(x)))))))))), { cls: 'wide', label: 'Keyboard shortcuts' });
}

function onKey(e) {
  if (S.view !== 'editor' || !S.doc || isTyping(e) || $('#modals').children.length || $('#overlays .countdown')) return;
  if (e.target?.type === 'range' && /^(Arrow|Page|Home|End)/.test(e.key)) return;
  const mod = e.metaKey || e.ctrlKey;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const run = fn => { e.preventDefault(); fn(); };
  if (mod) {
    if (k === 'z') return run(e.shiftKey ? doRedo : doUndo);
    if (k === 'y') return run(doRedo);
    if (k === 's') return run(saveProject);
    if (k === 'e') return run(openExport);
    return;
  }
  if (e.altKey) return;
  switch (k) {
    case ' ': return run(togglePlay);
    case 'ArrowLeft': return run(() => (e.shiftKey ? (pause(), seek(S.t - 1)) : step(-1)));
    case 'ArrowRight': return run(() => (e.shiftKey ? (pause(), seek(S.t + 1)) : step(1)));
    case 'Home': return run(() => { pause(); seek(S.doc.trim[0] || 0); revealPlayhead(); });
    case 'End': return run(() => { pause(); seek(S.doc.trim[1] ?? S.dur); revealPlayhead(); });
    case 'z': return run(() => { pause(); addZoomAt(S.t); });
    case 's': return run(() => splitAt(S.t));
    case 'Delete': case 'Backspace': return run(() => { if (deleteSelected()) toast('Zoom deleted.', { ms: 2600, action: { label: 'Undo', run: doUndo } }); });
    case 'Escape': return run(() => select(null));
    case '[': return run(() => selectRelative(-1));
    case ']': return run(() => selectRelative(1));
    case 'i': return run(() => setTrim('in', S.t));
    case 'o': return run(() => setTrim('out', S.t));
    case '=': case '+': return run(() => zoomBy(1.5));
    case '-': case '_': return run(() => zoomBy(1 / 1.5));
    case '0': return run(fit);
    case 'n': return run(() => toast(toggleSnap() ? 'Snapping on' : 'Snapping off', { ms: 1400 }));
    case 'l': return run(() => { const b = $('#tpLoop'); setLoop(!b.classList.contains('on')); });
    case 'f': return run(() => { pause(); showExact(true); });
    case '?': return run(shortcutSheet);
  }
}

// ---------------------------------------------------------------- drag and drop
function dropping() {
  let depth = 0;
  const veil = $('#dropVeil');
  const files = e => [...(e.dataTransfer?.items || [])].some(i => i.kind === 'file');
  addEventListener('dragenter', e => { if (!files(e)) return; e.preventDefault(); if (++depth === 1) veil.classList.add('on'); });
  addEventListener('dragleave', e => { if (!files(e)) return; e.preventDefault(); if (--depth <= 0) { depth = 0; veil.classList.remove('on'); } });
  addEventListener('dragover', e => { if (files(e)) e.preventDefault(); });
  addEventListener('drop', e => {
    if (!files(e)) return;
    e.preventDefault();
    depth = 0;
    veil.classList.remove('on');
    const f = e.dataTransfer.files[0];
    if (f) openFile(f);
  });
}

// ---------------------------------------------------------------- transport + topbar
function wire() {
  $('#goHome').onclick = () => { pause(); showView('home'); };
  $('#statusBtn').onclick = statusSheet;
  $('#btnSave').onclick = saveProject;
  $('#btnKeys').onclick = shortcutSheet;
  $('#tpPlay').onclick = togglePlay;
  $('#tpBack').onclick = () => step(-1);
  $('#tpFwd').onclick = () => step(1);
  $('#tpStart').onclick = () => { pause(); seek(S.doc?.trim[0] || 0); revealPlayhead(); };
  $('#tpEnd').onclick = () => { pause(); seek(S.doc?.trim[1] ?? S.dur); revealPlayhead(); };
  $('#tpExact').onclick = () => { pause(); showExact(true); };
  $('#tpFull').onclick = fullscreen;
  $('#tbUndo').onclick = doUndo;
  $('#tbRedo').onclick = doRedo;
  $('#tbAdd').onclick = () => { pause(); addZoomAt(S.t); };
  $('#tbSplit').onclick = () => splitAt(S.t);
  $('#tbDel').onclick = () => deleteSelected();
  $('#tbZoomIn').onclick = () => zoomBy(1.5);
  $('#tbZoomOut').onclick = () => zoomBy(1 / 1.5);
  $('#tbFit').onclick = fit;
  on('sel', () => { $('#tbDel').disabled = !selected(); });
  on('doc', () => { $('#tbDel').disabled = !selected(); });
  on('render', p => { $('#btnExport').classList.toggle('rendering', p.state === 'running'); });
  addEventListener('keydown', onKey);
  addEventListener('pagehide', flushOnExit);
  document.addEventListener('visibilitychange', () => { if (document.hidden) flushOnExit(); });
}

// ---------------------------------------------------------------- desktop menu
let bootDone;
const booted = new Promise(r => { bootDone = r; });

/** Edit > Undo is the editor's undo - unless a text field has the focus,
 *  where it means what it means everywhere else. */
function textEdit(cmd) {
  const t = document.activeElement;
  if (!t || !(t.isContentEditable || /^(INPUT|TEXTAREA)$/.test(t.tagName)) || /^(range|checkbox|radio|button|color)$/.test(t.type)) return false;
  document.execCommand(cmd);
  return true;
}

addEventListener('zoomcut:menu', async e => {
  await booted;
  const { action, arg } = e.detail || {};
  const needsProject = () => { if (!S.project) { toast('Open or record something first.'); return false; } return true; };
  switch (action) {
    case 'new': pause(); showView('home'); break;
    case 'open-path': openPath(arg); break;
    case 'save': if (needsProject()) saveProject(); break;
    case 'export': if (needsProject()) openExport(); break;
    case 'undo': if (!textEdit('undo')) doUndo(); break;
    case 'redo': if (!textEdit('redo')) doRedo(); break;
    case 'shortcuts': if (!$('#modals').children.length) shortcutSheet(); break;
  }
});

// ---------------------------------------------------------------- boot
(async function boot() {
  tooltips();
  initPreview();
  initTimeline();
  initInspector();
  initExporter();
  initHome({ analyze, load: loadFile, play: playFile, openFile, desktop: DESKTOP });
  wire();
  dropping();
  emit('history');

  try { S.sys = await api('/api/state'); emit('sys', S.sys); paintStatus(); resume(S.sys.progress); }
  catch (e) { toast(e.message, { kind: 'err', ms: 0 }); }
  try {
    const w = await api('/api/wallpapers');
    S.wallpapers = w.wallpapers || [];
    S.wallDefault = w.default || null;
  } catch { S.wallpapers = []; }

  // a reload, or a second tab, picks up exactly where the server is
  let pj = null;
  try { pj = (await api('/api/project')).project; } catch { /* nothing yet */ }
  if (pj) { open(pj); refreshWallpapers(); }
  else showView('home');
  document.body.classList.add('booted');
  bootDone();
  setTimeout(pollState, 1200);
})();
