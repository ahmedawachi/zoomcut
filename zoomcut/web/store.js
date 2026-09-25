// Editor state, undo history, and the sync back to the server.
//
// The server holds the project; the browser holds an editable copy (`doc`)
// that every panel reads and writes. Edits apply locally at once - the
// preview never waits on a round trip - and are pushed to the server shortly
// after, so a render always sees what is on screen.
import { api } from './api.js';
import { keyframes, shotsFromSegs, segsFromShots, simulate, DEFAULT_SPRING } from './camera.js';

const subs = new Map();
export function on(evt, fn) {
  if (!subs.has(evt)) subs.set(evt, new Set());
  subs.get(evt).add(fn);
  return () => subs.get(evt).delete(fn);
}
export function emit(evt, data) {
  for (const fn of subs.get(evt) || []) {
    try { fn(data); } catch (e) { console.error(`[${evt}]`, e); }
  }
}

export const S = {
  view: 'home',
  sys: null,              // /api/state
  project: null,          // the server's project, as last seen
  dur: 0, sw: 0, sh: 0,   // source duration and size
  doc: null,              // { segs, trim, style, spring, output } - what undo tracks
  sel: null,              // selected zoom id
  t: 0,                   // playhead, seconds of source time
  playing: false,
  media: { state: 'idle' },
  activity: null, cuts: [],
  wallpapers: [], wallDefault: null,
  keys: [], sim: null,
  useProjectKeys: false,  // project keys that no shot list explains (imported zooms)
  directCam: null,        // [z, cx, cy] shown instead of the spring while reframing
};

const clone = o => JSON.parse(JSON.stringify(o));
const FIELDS = ['segs', 'trim', 'style', 'spring', 'output'];
const CAMERA = new Set(['segs', 'trim', 'spring', 'output']);

export function trimRange() {
  const [a, b] = S.doc?.trim || [0, null];
  return [a || 0, b == null ? S.dur : b];
}
export const selected = () => S.doc?.segs.find(s => s.id === S.sel) || null;
export const sourceName = () => (S.project?.source || '').split(/[\\/]/).pop();

function sameKeys(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  return a.every((k, i) => k.every((v, j) => Math.abs(v - b[i][j]) < 1e-3));
}

export function loadProject(pj) {
  S.project = pj;
  const info = pj.sourceInfo || {};
  S.dur = +info.duration || +pj.analysis?.duration || 0;
  S.sw = +info.width || 1; S.sh = +info.height || 1;
  const trim = pj.trim || [0, null];
  S.doc = {
    segs: segsFromShots(pj.camera?.shots),
    trim: [+trim[0] || 0, trim[1] == null ? null : +trim[1]],
    style: clone(pj.style || {}),
    spring: { ...DEFAULT_SPRING, ...(pj.camera?.spring || {}) },
    output: { ...(pj.output || {}) },
  };
  S.activity = pj.analysis?.activity || null;
  S.cuts = pj.analysis?.cuts || [];
  S.sel = null; S.t = 0; S.playing = false; S.directCam = null;
  // a project whose keys came from an imported editor preset has zooms the
  // shot list does not describe; honour them until the first zoom edit
  S.useProjectKeys = !!pj.camera?.keys?.length && !sameKeys(pj.camera.keys, keyframes(pj.camera.shots || []));
  undoStack.length = redoStack.length = 0; last = null;
  dirty.clear();
  recompute();
  emit('project', pj);
  emit('doc', new Set(FIELDS));
  emit('sel', null);
  emit('history');
}

export function unloadProject() {
  S.project = null; S.doc = null; S.sel = null; S.sim = null;
  emit('project', null);
}

function recompute() {
  if (!S.doc) return;
  S.keys = S.useProjectKeys ? S.project.camera.keys : keyframes(shotsFromSegs(S.doc.segs, S.dur));
  const [a, b] = trimRange();
  S.sim = simulate(S.keys, S.doc.spring, S.doc.output.fps || 60, a, b);
  emit('camera');
}

// ---------------------------------------------------------------- changes
const dirty = new Set();
let syncTimer = null, inflight = null;

/** Call after mutating S.doc. Recomputes the camera if needed, tells every
 *  panel, and schedules the server sync. */
export function changed(...fields) {
  if (!S.doc) return;
  const set = new Set(fields.includes('all') ? FIELDS : fields);
  set.forEach(f => dirty.add(f));
  if (set.has('segs')) S.useProjectKeys = false;
  if ([...set].some(f => CAMERA.has(f))) recompute();
  emit('doc', set);
  clearTimeout(syncTimer);
  syncTimer = setTimeout(flush, 260);
}

function pendingBody() {
  const body = {};
  if (dirty.has('segs')) body.shots = shotsFromSegs(S.doc.segs, S.dur);
  if (dirty.has('trim')) body.trim = [...S.doc.trim];
  if (dirty.has('style')) body.style = S.doc.style;
  if (dirty.has('spring')) body.spring = S.doc.spring;
  if (dirty.has('output')) body.output = S.doc.output;
  return body;
}

/** The page is going away (reload, tab closed) with an edit not yet sent:
 *  a keepalive request outlives the page, so the edit is not lost. */
export function flushOnExit() {
  if (!dirty.size || !S.project) return;
  const body = JSON.stringify(pendingBody());
  dirty.clear();
  try {
    fetch('/api/project', { method: 'POST', keepalive: true, body,
                            headers: { 'Content-Type': 'application/json' } });
  } catch { /* nothing more can be done from here */ }
}

/** Push pending edits now; resolves once the server has them. */
export async function flush() {
  clearTimeout(syncTimer);
  while (inflight) await inflight;
  if (!dirty.size || !S.project) return;
  const body = pendingBody();
  dirty.clear();
  emit('sync', 'saving');
  inflight = api('/api/project', body)
    .then(r => { if (S.project && r.project) S.project = r.project; emit('sync', 'saved'); })
    .catch(e => { emit('sync', 'error'); emit('error', e); })
    .finally(() => { inflight = null; });
  await inflight;
}

// ---------------------------------------------------------------- history
const undoStack = [], redoStack = [];
let last = null;          // {label, at} of the newest checkpoint, for coalescing
const snapshot = () => JSON.stringify(Object.fromEntries(FIELDS.map(f => [f, S.doc[f]])));

/** Remember the document before a change. Repeated checkpoints with the same
 *  label inside a short window collapse, so dragging a slider is one undo. */
export function checkpoint(label, coalesce = false) {
  if (!S.doc) return;
  const now = performance.now();
  if (coalesce && last && last.label === label && now - last.at < 1200) { last.at = now; return; }
  undoStack.push({ label, snap: snapshot() });
  if (undoStack.length > 200) undoStack.shift();
  redoStack.length = 0;
  last = { label, at: now };
  emit('history');
}

/** For drags that turned out to be clicks: forget a checkpoint that changed nothing. */
export function dropCheckpoint() {
  const top = undoStack[undoStack.length - 1];
  if (top && top.snap === snapshot()) { undoStack.pop(); last = null; emit('history'); }
}

function restore(snap) {
  const d = JSON.parse(snap);
  FIELDS.forEach(f => { S.doc[f] = d[f]; });
  if (S.sel && !S.doc.segs.some(s => s.id === S.sel)) { S.sel = null; emit('sel', null); }
  last = null;
  changed('all');
  emit('history');
}
export function undo() {
  const e = undoStack.pop();
  if (!e) return null;
  redoStack.push({ label: e.label, snap: snapshot() });
  restore(e.snap);
  return e.label;
}
export function redo() {
  const e = redoStack.pop();
  if (!e) return null;
  undoStack.push({ label: e.label, snap: snapshot() });
  restore(e.snap);
  return e.label;
}
export const canUndo = () => undoStack.length > 0;
export const canRedo = () => redoStack.length > 0;
export const undoLabel = () => undoStack[undoStack.length - 1]?.label;
export const redoLabel = () => redoStack[redoStack.length - 1]?.label;

export function select(id) {
  if (S.sel === id) return;
  S.sel = id;
  emit('sel', id);
}
