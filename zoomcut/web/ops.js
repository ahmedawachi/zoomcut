// Editing operations shared by the timeline, the inspector and the keyboard.
import { S, emit, changed, checkpoint, select, selected, trimRange, flush } from './store.js';
import { MIN_SEG, uid, settlePoint, clampCenter, segsFromShots } from './camera.js';
import { seek, pause } from './preview.js';
import { api } from './api.js';
import { toast, clamp } from './ui.js';

export const DEFAULT_LEN = 2.0;
export const DEFAULT_ZOOM = 1.6;

const sorted = () => [...S.doc.segs].sort((a, b) => a.start - b.start);

/** The free stretch around t, or null when t sits inside a zoom. */
export function gapAt(t) {
  let prev = 0;
  for (const s of sorted()) {
    if (t < s.start) return { start: prev, end: s.start };
    if (t <= s.end) return null;
    prev = s.end;
  }
  return { start: prev, end: S.dur };
}

/** Neighbours' edges - how far a zoom may move or grow. */
export function bounds(id) {
  const list = sorted();
  const i = list.findIndex(s => s.id === id);
  return { lo: i > 0 ? list[i - 1].end : 0, hi: i < list.length - 1 ? list[i + 1].start : S.dur };
}

/** Where a zoom added at t would go: from t onwards, slid back if the next
 *  zoom is in the way. */
export function plannedZoom(t, len = DEFAULT_LEN) {
  if (!S.doc) return null;
  const g = gapAt(t);
  if (!g || g.end - g.start < MIN_SEG) return null;
  let a = clamp(t, g.start, g.end), b = a + len;
  if (b > g.end) { b = g.end; a = Math.max(g.start, b - len); }
  return b - a >= MIN_SEG ? { start: a, end: b } : null;
}

/** Ask the server where the action is; fall back to the middle. */
async function suggest(a, b) {
  try {
    const r = await api('/api/suggest', { t0: a, t1: b });
    if (r.zoom > 1.05) return { zoom: r.zoom, cx: r.cx, cy: r.cy };
  } catch { /* older server, or nothing moved */ }
  return { zoom: DEFAULT_ZOOM, cx: 0.5, cy: 0.5 };
}

export async function addZoomAt(t, focus = null) {
  if (!S.doc) return;
  const p = plannedZoom(t);
  if (!p) {
    const under = S.doc.segs.find(s => t >= s.start && t <= s.end);
    if (under && !focus) { focusSeg(under); return under; }
    toast(under ? 'There is already a zoom here — it is selected now.' : 'Not enough room for a zoom here.');
    if (under) focusSeg(under);
    return;
  }
  const f = focus ? { zoom: focus.zoom || DEFAULT_ZOOM, cx: focus.cx, cy: focus.cy } : await suggest(p.start, p.end);
  const [cx, cy] = clampCenter(f.zoom, f.cx, f.cy);
  checkpoint('Add zoom');
  const seg = { id: uid(), start: p.start, end: p.end, zoom: Math.round(f.zoom * 100) / 100, cx, cy, reason: 'manual' };
  S.doc.segs.push(seg);
  changed('segs');
  select(seg.id);
  emit('inspect', 'zoom');
  seek(settlePoint(seg));
  return seg;
}

export function splitAt(t) {
  const seg = S.doc?.segs.find(s => t > s.start + MIN_SEG - 1e-6 && t < s.end - MIN_SEG + 1e-6);
  if (!seg) {
    toast('Put the playhead inside a zoom, at least a moment from either end, to split it.');
    return;
  }
  checkpoint('Split zoom');
  const b = { ...seg, id: uid(), start: t };
  seg.end = t;
  S.doc.segs.push(b);
  changed('segs');
  select(b.id);
}

export function deleteSelected() {
  const seg = selected();
  if (!seg) return false;
  checkpoint('Delete zoom');
  S.doc.segs = S.doc.segs.filter(s => s.id !== seg.id);
  select(null);
  changed('segs');
  return true;
}

export function clearZooms() {
  if (!S.doc?.segs.length) return;
  checkpoint('Remove all zooms');
  S.doc.segs = [];
  select(null);
  changed('segs');
}

export function selectRelative(dir) {
  const list = sorted();
  if (!list.length) return;
  let i = list.findIndex(s => s.id === S.sel);
  if (i < 0) {
    // nothing selected: take the nearest zoom in that direction from the playhead
    i = dir > 0 ? list.findIndex(s => s.end > S.t) : list.map(s => s.start < S.t).lastIndexOf(true);
    if (i < 0) i = dir > 0 ? 0 : list.length - 1;
  } else i = clamp(i + dir, 0, list.length - 1);
  focusSeg(list[i]);
}

export function focusSeg(seg) {
  pause();
  select(seg.id);
  if (!(S.t >= seg.start && S.t <= seg.end)) seek(settlePoint(seg));
  emit('inspect', 'zoom');
}

export function setTrim(side, t) {
  if (!S.doc) return;
  const [a, b] = trimRange();
  checkpoint(side === 'in' ? 'Trim start' : 'Trim end');
  if (side === 'in') S.doc.trim[0] = clamp(t, 0, b - 0.5);
  else {
    const v = clamp(t, a + 0.5, S.dur);
    S.doc.trim[1] = v >= S.dur - 1e-3 ? null : v;
  }
  changed('trim');
}

export function resetTrim() {
  if (!S.doc || (S.doc.trim[0] === 0 && S.doc.trim[1] == null)) return;
  checkpoint('Reset trim');
  S.doc.trim = [0, null];
  changed('trim');
}

/** Run the auto-director again with another temperament. Keeps the look,
 *  trim and motion; replaces the zooms (undoable like anything else). */
export async function rerunAuto(director) {
  if (!S.project) return;
  await flush();
  const keep = JSON.parse(JSON.stringify({ trim: S.doc.trim, style: S.doc.style, spring: S.doc.spring, output: S.doc.output }));
  const r = await api('/api/analyze', { source: S.project.source, style: keep.style, output: keep.output, director });
  checkpoint('Auto zoom');
  const pj = r.project;
  S.project = pj;
  S.activity = pj.analysis?.activity || S.activity;
  S.cuts = pj.analysis?.cuts || S.cuts;
  S.doc.segs = segsFromShots(pj.camera.shots);
  Object.assign(S.doc, keep);
  select(null);
  // analysing makes a fresh project on the server, so every field is resent
  changed('all');
  return S.doc.segs.length;
}

// ---------------------------------------------------------------- output
export const SIZES = { '1080p': 1080, '1440p': 1440, '4k': 2160 };
export const ASPECTS = { '16:9': 16 / 9, '16:10': 1.6, '4:3': 4 / 3, '1:1': 1, '9:16': 9 / 16 };

export function dims(size, aspect) {
  const short = SIZES[size] || 1440, r = ASPECTS[aspect] || 16 / 9;
  return r >= 1 ? [Math.round(short * r / 2) * 2, short] : [short, Math.round(short / r / 2) * 2];
}
export function sizeKey() {
  const short = Math.min(S.doc.output.width, S.doc.output.height);
  return Object.entries(SIZES).sort((a, b) => Math.abs(a[1] - short) - Math.abs(b[1] - short))[0][0];
}
export function aspectKey() {
  const r = S.doc.output.width / S.doc.output.height;
  const hit = Object.entries(ASPECTS).find(([, v]) => Math.abs(v - r) < 0.01);
  return hit ? hit[0] : null;
}
export function setOutput(size, aspect) {
  const [w, h] = dims(size, aspect);
  if (w === S.doc.output.width && h === S.doc.output.height) return;
  checkpoint('Output size');
  S.doc.output.width = w;
  S.doc.output.height = h;
  changed('output');
}
