// The timeline: ruler, filmstrip, zoom track and activity, over one shared
// time axis. Canvases are only as wide as the viewport and redraw on scroll,
// so a long recording zoomed all the way in never hits a canvas size limit.
import { S, on, emit, changed, checkpoint, dropCheckpoint, select, trimRange } from './store.js';
import { MIN_SEG, settlePoint, response } from './camera.js';
import { seek, pause } from './preview.js';
import { fileUrl } from './api.js';
import { plannedZoom, addZoomAt, bounds, focusSeg } from './ops.js';
import { $, h, icon, clamp, drag, fmt, store, remember } from './ui.js';

const G = 16;                          // gutter either side of the clip, px
const MAX_PPS = 600;
const E = {};
let pps = 40, fitPps = 40, atFit = true;
let snapOn = store('snap', true);
let strip = null, stripImg = null;
let raf = 0, dragging = false, suppressClick = false;
let C = {};                            // resolved theme colours for the canvases
const segEls = new Map();
let ramp = null;                       // seconds the camera takes to settle, for the current spring

const X = t => G + t * pps;
const tAt = clientX => clamp((clientX - E.scroll.getBoundingClientRect().left + E.scroll.scrollLeft - G) / pps, 0, S.dur);

export function initTimeline() {
  for (const id of ['tlScroll', 'tlContent', 'laneRuler', 'laneClip', 'laneZoom', 'laneAct', 'cvRuler',
                    'cvStrip', 'cvAct', 'ghost', 'cutsLayer', 'shadeL', 'shadeR', 'trimL', 'trimR',
                    'snapLine', 'playhead', 'tbZoom', 'tbSnap', 'tlInfo']) E[id] = $('#' + id);
  E.scroll = E.tlScroll;
  readColors();

  new ResizeObserver(() => requestAnimationFrame(refit)).observe(E.scroll);
  E.scroll.addEventListener('scroll', schedule, { passive: true });
  E.scroll.addEventListener('wheel', onWheel, { passive: false });

  on('project', pj => { strip = null; stripImg = null; if (pj) { atFit = true; refit(); } });
  on('doc', f => { if (f.has('spring')) ramp = null; layoutDom(); schedule(); });
  on('sel', () => { layoutDom(); });
  on('time', () => { placePlayhead(); follow(); });
  on('media', m => loadStrip(m?.strip));

  // scrubbing: the ruler, the filmstrip and the activity lane all move the playhead
  for (const lane of [E.laneRuler, E.laneClip, E.laneAct]) lane.addEventListener('pointerdown', scrub);
  E.playhead.querySelector('.ph-knob').addEventListener('pointerdown', scrub);

  zoomLane();
  trimHandles();
  hoverLine();

  E.tbZoom.addEventListener('input', () => setPps(ppsFromSlider(+E.tbZoom.value), E.scroll.clientWidth / 2));
  E.tbSnap.classList.toggle('on', snapOn);
  E.tbSnap.setAttribute('aria-pressed', String(snapOn));
  E.tbSnap.onclick = toggleSnap;
}

function readColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  C = { tick: v('--text-3'), label: v('--text-2'), line: v('--border-strong'), act: v('--act'),
        actFill: v('--act-soft'), clip: v('--surface-3'), cut: v('--cut'), accent: v('--accent') };
}

export function toggleSnap() {
  snapOn = !snapOn;
  remember('snap', snapOn);
  E.tbSnap.classList.toggle('on', snapOn);
  E.tbSnap.setAttribute('aria-pressed', String(snapOn));
  return snapOn;
}

// ---------------------------------------------------------------- scale
function refit() {
  if (!S.dur) return;
  fitPps = Math.max(1, (E.scroll.clientWidth - G * 2) / S.dur);
  if (atFit || pps < fitPps) pps = fitPps;
  layoutDom();
  syncSlider();
  schedule();
}
const maxPps = () => Math.max(fitPps * 1.001, MAX_PPS);
const ppsFromSlider = v => fitPps * Math.pow(maxPps() / fitPps, v / 100);
const sliderFromPps = p => 100 * Math.log(p / fitPps) / Math.log(maxPps() / fitPps);
function syncSlider() {
  E.tbZoom.value = String(clamp(sliderFromPps(pps), 0, 100));
  E.tbZoom.style.setProperty('--p', E.tbZoom.value + '%');
}

/** Change the scale, keeping the time under `anchor` (px into the viewport) still. */
export function setPps(v, anchor = E.scroll.clientWidth / 2) {
  if (!S.dur) return;
  const t = (E.scroll.scrollLeft + anchor - G) / pps;
  pps = clamp(v, fitPps, maxPps());
  atFit = pps <= fitPps * 1.001;
  layoutDom();
  E.scroll.scrollLeft = G + t * pps - anchor;
  syncSlider();
  schedule();
}
export const zoomBy = f => {
  // zoom around the playhead when it is on screen, else the middle
  const x = X(S.t) - E.scroll.scrollLeft;
  setPps(pps * f, x >= 0 && x <= E.scroll.clientWidth ? x : E.scroll.clientWidth / 2);
};
export const fit = () => { atFit = true; setPps(fitPps, 0); E.scroll.scrollLeft = 0; };

function onWheel(e) {
  if (!S.dur) return;
  if (e.ctrlKey || e.metaKey) {
    // pinch on a trackpad arrives as ctrl+wheel
    e.preventDefault();
    const anchor = e.clientX - E.scroll.getBoundingClientRect().left;
    setPps(pps * Math.exp(-e.deltaY * (e.ctrlKey && !e.metaKey ? 0.012 : 0.004)), anchor);
  } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
    // there is nothing to scroll vertically, so a mouse wheel pans time
    e.preventDefault();
    E.scroll.scrollLeft += e.deltaY;
  }
}

/** Keep the playhead on screen while playing, a page at a time like an editor. */
function follow() {
  if (!S.playing || dragging) return;
  const x = X(S.t), l = E.scroll.scrollLeft, w = E.scroll.clientWidth;
  if (x > l + w - 40 || x < l) E.scroll.scrollLeft = x - 60;
}
export function revealPlayhead() {
  const x = X(S.t), l = E.scroll.scrollLeft, w = E.scroll.clientWidth;
  if (x < l + 20 || x > l + w - 20) E.scroll.scrollLeft = x - w / 2;
}

// ---------------------------------------------------------------- DOM layer
function layoutDom() {
  if (!S.doc || !S.dur) return;
  E.tlContent.style.width = (G * 2 + S.dur * pps) + 'px';
  renderSegs();
  renderCuts();
  placeTrim();
  placePlayhead();
  const n = S.doc.segs.length, c = S.cuts.length;
  const [a, b] = trimRange();
  const trimmed = a > 1e-3 || b < S.dur - 1e-3;
  E.tlInfo.textContent = `${n} zoom${n === 1 ? '' : 's'} · ${c} view change${c === 1 ? '' : 's'}` +
    (trimmed ? ` · trimmed to ${fmt(b - a, 1)}` : '');
}

function placePlayhead() {
  if (!S.dur) return;
  E.playhead.style.transform = `translateX(${X(S.t)}px)`;
}

function placeTrim() {
  const [a, b] = trimRange();
  E.shadeL.style.left = G + 'px';
  E.shadeL.style.width = Math.max(0, a * pps) + 'px';
  E.shadeR.style.left = X(b) + 'px';
  E.shadeR.style.width = Math.max(0, (S.dur - b) * pps) + 'px';
  E.trimL.style.left = X(a) + 'px';
  E.trimR.style.left = X(b) + 'px';
  E.trimL.classList.toggle('moved', a > 1e-3);
  E.trimR.classList.toggle('moved', b < S.dur - 1e-3);
}

function renderCuts() {
  const layer = E.cutsLayer;
  const want = S.cuts.length;
  while (layer.children.length > want) layer.lastChild.remove();
  while (layer.children.length < want) {
    const el = h('div', { class: 'cut' }, h('button', { class: 'cut-knob', type: 'button' }));
    layer.append(el);
  }
  S.cuts.forEach((c, i) => {
    const el = layer.children[i];
    el.style.left = X(c) + 'px';
    const k = el.firstChild;
    k.dataset.tip = `View change · ${fmt(c)}`;
    k.setAttribute('aria-label', `Go to the view change at ${fmt(c)}`);
    k.onclick = () => { pause(); seek(c); };
  });
}

const zoomLabel = z => `${+z.toFixed(2)}×`;

function renderSegs() {
  const seen = new Set();
  if (ramp == null) ramp = response(S.doc.spring).settleAt ?? 1;
  for (const s of S.doc.segs) {
    let el = segEls.get(s.id);
    if (!el) {
      el = makeSeg(s.id);
      segEls.set(s.id, el);
      E.laneZoom.append(el);
    }
    seen.add(s.id);
    const w = Math.max(4, (s.end - s.start) * pps);
    el.style.left = X(s.start) + 'px';
    el.style.width = w + 'px';
    // the camera is still travelling for the first part of every zoom
    el.style.setProperty('--ramp', Math.min(w, ramp * pps) + 'px');
    el.classList.toggle('sel', s.id === S.sel);
    el.classList.toggle('manual', s.reason === 'manual');
    el.classList.toggle('narrow', w < 100);
    el.classList.toggle('tiny', w < 34);
    el.querySelector('.seg-z').textContent = zoomLabel(s.zoom);
    el.querySelector('.seg-d').textContent = `${(s.end - s.start).toFixed(1)}s`;
    el.setAttribute('aria-label', `Zoom ${zoomLabel(s.zoom)} from ${fmt(s.start)} to ${fmt(s.end)}`);
    el.setAttribute('aria-pressed', String(s.id === S.sel));
  }
  for (const [id, el] of segEls) if (!seen.has(id)) { el.remove(); segEls.delete(id); }
}

function makeSeg(id) {
  const el = h('div', { class: 'seg', 'data-id': id, tabindex: '0', role: 'button' },
    h('div', { class: 'seg-h l', 'aria-hidden': 'true' }),
    h('div', { class: 'seg-in' }, icon('zoom-in'), h('b', { class: 'seg-z' }), h('span', { class: 'seg-d' })),
    h('div', { class: 'seg-h r', 'aria-hidden': 'true' }));
  el.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    const mode = e.target.closest('.seg-h.l') ? 'l' : e.target.closest('.seg-h.r') ? 'r' : 'move';
    segDrag(e, id, mode);
  });
  el.addEventListener('dblclick', e => { e.stopPropagation(); emit('inspect', 'zoom'); });
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const s = S.doc.segs.find(x => x.id === id);
      if (s) focusSeg(s);
    }
  });
  return el;
}

// ---------------------------------------------------------------- snapping
function snapPoints(exclude) {
  const [a, b] = trimRange();
  const pts = [0, S.dur, a, b, ...S.cuts];
  if (!S.playing) pts.push(S.t);
  for (const s of S.doc.segs) if (s.id !== exclude) pts.push(s.start, s.end);
  return pts;
}
function nearest(t, pts) {
  const th = 7 / pps;
  let best = null, bd = th;
  for (const p of pts) { const d = Math.abs(p - t); if (d < bd) { bd = d; best = p; } }
  return best;
}
function showSnap(t) {
  E.snapLine.hidden = t == null;
  if (t != null) E.snapLine.style.left = X(t) + 'px';
}

// ---------------------------------------------------------------- zooms
function segDrag(e, id, mode) {
  const seg = S.doc.segs.find(s => s.id === id);
  if (!seg) return;
  e.stopPropagation();
  pause();
  const wasSelected = S.sel === id;
  select(id);
  emit('inspect', 'zoom');
  checkpoint(mode === 'move' ? 'Move zoom' : 'Resize zoom');
  const x0 = e.clientX, a0 = seg.start, b0 = seg.end, len = b0 - a0;
  const { lo, hi } = bounds(id);
  const pts = snapPoints(id);
  const el = e.currentTarget;
  let moved = false;
  dragging = true;
  el.classList.add('dragging');
  document.body.classList.add(mode === 'move' ? 'grabbing' : 'resizing');
  drag(e, {
    move: ev => {
      if (!moved && Math.abs(ev.clientX - x0) < 3) return;
      moved = true;
      const dt = (ev.clientX - x0) / pps;
      let snapped = null;
      if (mode === 'move') {
        let a = clamp(a0 + dt, lo, hi - len);
        if (snapOn && !ev.altKey) {
          const sa = nearest(a, pts), sb = nearest(a + len, pts);
          const da = sa == null ? Infinity : sa - a, db = sb == null ? Infinity : sb - (a + len);
          if (Math.abs(da) <= Math.abs(db) && sa != null) { a += da; snapped = sa; }
          else if (sb != null) { a += db; snapped = sb; }
          a = clamp(a, lo, hi - len);
        }
        seg.start = a; seg.end = a + len;
      } else if (mode === 'l') {
        let a = a0 + dt;
        if (snapOn && !ev.altKey) { const s = nearest(a, pts); if (s != null) { a = s; snapped = s; } }
        seg.start = clamp(a, lo, seg.end - MIN_SEG);
        seek(seg.start);
      } else {
        let b = b0 + dt;
        if (snapOn && !ev.altKey) { const s = nearest(b, pts); if (s != null) { b = s; snapped = s; } }
        seg.end = clamp(b, seg.start + MIN_SEG, hi);
        seek(seg.end);
      }
      showSnap(snapped);
      changed('segs');
    },
    up: () => {
      dragging = false;
      el.classList.remove('dragging');
      document.body.classList.remove('grabbing', 'resizing');
      showSnap(null);
      dropCheckpoint();
      if (!moved) {
        // a click: show the zoom once the camera has settled into it
        if (!(S.t >= seg.start && S.t <= seg.end) || !wasSelected) seek(settlePoint(seg));
      } else if (mode !== 'move') seek(settlePoint(seg));
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
    },
  }, el);
}

function zoomLane() {
  const lane = E.laneZoom, ghost = E.ghost;
  const hide = () => { ghost.hidden = true; lane.classList.remove('hovering'); };
  lane.addEventListener('pointermove', e => {
    if (dragging || !S.doc || e.target.closest('.seg')) return hide();
    const p = plannedZoom(tAt(e.clientX));
    if (!p) return hide();
    ghost.hidden = false;
    lane.classList.add('hovering');
    ghost.style.left = X(p.start) + 'px';
    ghost.style.width = (p.end - p.start) * pps + 'px';
    ghost.classList.toggle('narrow', (p.end - p.start) * pps < 90);
  });
  lane.addEventListener('pointerleave', hide);
  lane.addEventListener('click', async e => {
    if (suppressClick || !S.doc || e.target.closest('.seg')) return;
    hide();
    pause();
    await addZoomAt(tAt(e.clientX));
  });
}

/** A faint line and time readout under the pointer, like any editor. */
function hoverLine() {
  const line = $('#hoverLine'), label = line.querySelector('span');
  const hide = () => { line.hidden = true; };
  E.tlContent.addEventListener('pointermove', e => {
    if (dragging || !S.doc || e.target.closest('.seg, .trim-h, .ph-knob, .cut-knob')) return hide();
    const t = tAt(e.clientX);
    line.hidden = false;
    line.style.transform = `translateX(${X(t)}px)`;
    label.textContent = fmt(t);
  });
  E.tlContent.addEventListener('pointerleave', hide);
  E.tlContent.addEventListener('pointerdown', hide);
}

// ---------------------------------------------------------------- trim + scrub
function trimHandles() {
  const handle = (el, side) => {
    el.addEventListener('pointerdown', e => {
      if (e.button !== 0 || !S.doc) return;
      e.stopPropagation();
      pause();
      checkpoint(side === 'in' ? 'Trim start' : 'Trim end');
      const pts = snapPoints(null).filter(p => p !== trimRange()[side === 'in' ? 0 : 1]);
      dragging = true;
      el.classList.add('dragging');
      drag(e, {
        move: ev => {
          let t = tAt(ev.clientX);
          let snapped = null;
          if (snapOn && !ev.altKey) { const s = nearest(t, pts); if (s != null) { t = s; snapped = s; } }
          const [a, b] = trimRange();
          if (side === 'in') S.doc.trim[0] = clamp(t, 0, b - 0.5);
          else { const v = clamp(t, a + 0.5, S.dur); S.doc.trim[1] = v >= S.dur - 1e-3 ? null : v; }
          showSnap(snapped);
          changed('trim');
          seek(side === 'in' ? trimRange()[0] : trimRange()[1]);
        },
        up: () => {
          dragging = false;
          el.classList.remove('dragging');
          showSnap(null);
          dropCheckpoint();
        },
      }, el);
    });
    el.addEventListener('dblclick', () => {
      checkpoint('Reset trim');
      if (side === 'in') S.doc.trim[0] = 0; else S.doc.trim[1] = null;
      changed('trim');
    });
  };
  handle(E.trimL, 'in');
  handle(E.trimR, 'out');
}

function scrub(e) {
  if (e.button !== 0 || !S.doc) return;
  if (e.target.closest('.trim-h, .cut-knob')) return;
  pause();
  const el = e.currentTarget;
  const pts = snapPoints(null).filter(p => p !== S.t);
  const to = ev => {
    let t = tAt(ev.clientX);
    if (snapOn && !ev.altKey && ev.shiftKey) { const s = nearest(t, pts); if (s != null) t = s; }
    seek(t);
  };
  dragging = true;
  document.body.classList.add('scrubbing');
  to(e);
  drag(e, {
    move: to,
    up: () => { dragging = false; document.body.classList.remove('scrubbing'); },
  }, el);
}

// ---------------------------------------------------------------- canvases
function schedule() {
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; drawRuler(); drawStrip(); drawAct(); });
}

function prep(cv, w, hgt) {
  const d = Math.min(2, devicePixelRatio || 1);
  const W = Math.max(1, Math.round(w * d)), H = Math.max(1, Math.round(hgt * d));
  if (cv.width !== W || cv.height !== H) {
    cv.width = W; cv.height = H;
    cv.style.width = w + 'px'; cv.style.height = hgt + 'px';
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(d, 0, 0, d, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  return ctx;
}

const STEPS = [[0.1, 2], [0.2, 4], [0.5, 5], [1, 5], [2, 4], [5, 5], [10, 5], [15, 3], [30, 6],
               [60, 6], [120, 4], [300, 5], [600, 6]];

function drawRuler() {
  const w = E.scroll.clientWidth, hh = E.laneRuler.clientHeight;
  const ctx = prep(E.cvRuler, w, hh);
  if (!S.dur) return;
  const [major, div] = STEPS.find(([s]) => s * pps >= 78) || STEPS[STEPS.length - 1];
  const minor = major / div;
  const l = E.scroll.scrollLeft;
  const i0 = Math.max(0, Math.floor((l - G) / pps / minor)), i1 = Math.ceil((l + w - G) / pps / minor);
  ctx.font = '500 10px ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';
  for (let i = i0; i <= i1; i++) {
    const t = i * minor;
    if (t > S.dur + 1e-6) break;
    const x = Math.round(G + t * pps - l) + 0.5;
    const isMajor = i % div === 0;
    ctx.fillStyle = isMajor ? C.label : C.tick;
    ctx.globalAlpha = isMajor ? 0.9 : 0.55;
    ctx.fillRect(x - 0.5, isMajor ? hh - 9 : hh - 5, 1, isMajor ? 9 : 5);
    if (isMajor) {
      ctx.globalAlpha = 1;
      ctx.fillText(major < 1 ? fmt(t, 1) : fmt(t, 0), x + 4, hh / 2 - 3);
    }
  }
  ctx.globalAlpha = 1;
}

function loadStrip(meta) {
  if (!meta || !meta.path) return;
  if (strip && strip.path === meta.path) return;
  strip = meta;
  const img = new Image();
  img.onload = () => { if (strip === meta) { stripImg = img; schedule(); } };
  img.src = fileUrl(meta.path);
}

function roundRect(ctx, x, y, w, hh, r) {
  ctx.beginPath();
  ctx.roundRect ? ctx.roundRect(x, y, w, hh, r) : ctx.rect(x, y, w, hh);
}

function drawStrip() {
  const w = E.scroll.clientWidth, hh = E.laneClip.clientHeight;
  const ctx = prep(E.cvStrip, w, hh);
  if (!S.dur) return;
  const l = E.scroll.scrollLeft;
  const top = 5, bh = hh - 10;
  const cx0 = G - l, cw = S.dur * pps;
  ctx.save();
  roundRect(ctx, cx0, top, cw, bh, 7);
  ctx.clip();
  ctx.fillStyle = C.clip;
  ctx.fillRect(cx0, top, cw, bh);
  if (stripImg && strip) {
    const slot = bh * strip.tw / strip.th;
    const first = Math.max(0, Math.floor((l - G) / slot));
    for (let x = G + first * slot; x < l + w && x < G + cw; x += slot) {
      const t = (x - G + slot / 2) / pps;
      const idx = clamp(Math.floor(t / strip.interval), 0, strip.count - 1);
      ctx.drawImage(stripImg, (idx % strip.cols) * strip.tw, Math.floor(idx / strip.cols) * strip.th,
                    strip.tw, strip.th, x - l, top, slot, bh);
    }
  } else {
    // shimmer placeholder while the filmstrip is being made
    const g = ctx.createLinearGradient(cx0, 0, cx0 + cw, 0);
    g.addColorStop(0, 'rgba(255,255,255,0.02)');
    g.addColorStop(0.5, 'rgba(255,255,255,0.06)');
    g.addColorStop(1, 'rgba(255,255,255,0.02)');
    ctx.fillStyle = g;
    ctx.fillRect(cx0, top, cw, bh);
  }
  ctx.restore();
  ctx.strokeStyle = C.line;
  ctx.lineWidth = 1;
  roundRect(ctx, cx0 + 0.5, top + 0.5, cw - 1, bh - 1, 7);
  ctx.stroke();
}

function drawAct() {
  const w = E.scroll.clientWidth, hh = E.laneAct.clientHeight;
  const ctx = prep(E.cvAct, w, hh);
  if (!S.dur) return;
  const l = E.scroll.scrollLeft;
  const base = hh - 5, amp = hh - 11;
  ctx.fillStyle = C.line;
  ctx.fillRect(Math.max(0, G - l), base + 0.5, Math.min(w, S.dur * pps), 1);
  const a = S.activity;
  if (!a || !a.v?.length) return;
  const step = 2;
  const pts = [];
  const xEnd = Math.min(w, G + S.dur * pps - l);
  for (let px = Math.max(0, G - l); px <= xEnd; px += step) {
    const ta = (px + l - G) / pps, tb = (px + step + l - G) / pps;
    const ia = Math.max(0, Math.floor((ta - a.t0) / a.dt));
    const ib = Math.min(a.v.length - 1, Math.floor((tb - a.t0) / a.dt));
    let v = 0;
    for (let i = ia; i <= ib; i++) if (a.v[i] > v) v = a.v[i];
    pts.push([px, base - Math.sqrt(v) * amp]);   // sqrt lifts the quiet parts into view
  }
  if (pts.length < 2) return;
  const grad = ctx.createLinearGradient(0, base - amp, 0, base);
  grad.addColorStop(0, C.actFill);
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath();
  ctx.moveTo(pts[0][0], base);
  for (const [x, y] of pts) ctx.lineTo(x, y);
  ctx.lineTo(pts[pts.length - 1][0], base);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = C.act;
  ctx.lineWidth = 1.25;
  ctx.lineJoin = 'round';
  ctx.stroke();
}
