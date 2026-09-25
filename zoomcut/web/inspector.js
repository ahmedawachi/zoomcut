// The inspector: everything about the selected zoom, the look, and the camera.
import { S, on, emit, changed, checkpoint, dropCheckpoint, select, selected } from './store.js';
import { cropRect, clampCenter, SPRINGS, DIRECTORS, MAX_ZOOM, MIN_SEG, settlePoint, response } from './camera.js';
import { seek, pause, grabFrame } from './preview.js';
import { api, fileUrl, wallpaperUrl, upload } from './api.js';
import { bounds, deleteSelected, focusSeg, rerunAuto, clearZooms, sizeKey, aspectKey, setOutput, ASPECTS, dims } from './ops.js';
import { $, $$, h, icon, clamp, drag, fmt, parseTime, toast, store, remember } from './ui.js';

const DEFAULTS = {
  paddingRatio: 0.0702, cornerRadius: 0.012, edgeHighlight: 0.13,
  shadow: { distance: 0.0271, blur: 0.0215, alpha: 0.52 },
  background: { dim: 0.12, blur: 0 },
};

const GRADIENTS = [
  ['Violet', '#3F37C9', '#8C87DF'], ['Dusk', '#1F1C2C', '#928DAB'], ['Ocean', '#0F4C75', '#3DB2E0'],
  ['Aurora', '#0BA360', '#3CBA92'], ['Peach', '#FF9A8B', '#FF6A88'], ['Sunrise', '#F7971E', '#FFD200'],
  ['Berry', '#8E2DE2', '#4A00E0'], ['Mint', '#43E97B', '#38F9D7'], ['Rose', '#EE9CA7', '#FFDDE1'],
  ['Graphite', '#232526', '#414345'], ['Midnight', '#0F2027', '#2C5364'], ['Candy', '#FC466B', '#3F5EFB'],
];
const DIRS = [   // [label, rotation for the arrow icon, p0, p1]
  ['→', 0, [0, 0.5], [1, 0.5]], ['↘', 45, [0, 0], [1, 1]], ['↓', 90, [0.5, 0], [0.5, 1]],
  ['↙', 135, [1, 0], [0, 1]], ['←', 180, [1, 0.5], [0, 0.5]], ['↖', 225, [1, 1], [0, 0]],
  ['↑', 270, [0.5, 1], [0.5, 0]], ['↗', 315, [0, 1], [1, 0]],
];
const COLORS = ['#0B0C10', '#1C1D26', '#3A3D4E', '#F2F2F7', '#FFFFFF', '#3F37C9',
                '#0A84FF', '#30D158', '#FF9F0A', '#FF375F', '#BF5AF2', '#64D2FF'];

let tab = store('tab', 'zoom');
const syncers = new Set();
let zoomSync = [];
let zoomSig = '';
const segSig = () => [...S.doc.segs].sort((a, b) => a.start - b.start)
  .map(s => s.id + (s.reason === 'manual' ? 'm' : '')).join(',') + '|' + (S.sel || '');

export function initInspector() {
  for (const b of $$('.tabs [data-tab]')) b.addEventListener('click', () => showTab(b.dataset.tab));
  on('inspect', t => showTab(t));
  on('project', pj => { if (pj) buildAll(); });
  on('doc', f => {
    if (f.has('style') && bgKindChanged()) buildBackground();
    syncers.forEach(fn => fn());
    if (f.has('segs') && segSig() !== zoomSig) buildZoom();
    else zoomSync.forEach(fn => fn());
  });
  on('sel', () => buildZoom());
  on('frame', () => zoomSync.forEach(fn => fn.frame?.()));
  on('media', () => zoomSync.forEach(fn => fn.frame?.()));
  showTab(tab);
}

export function showTab(name) {
  tab = name;
  remember('tab', name);
  for (const b of $$('.tabs [data-tab]')) {
    const on_ = b.dataset.tab === name;
    b.classList.toggle('on', on_);
    b.setAttribute('aria-selected', String(on_));
  }
  for (const p of $$('.pane')) p.hidden = p.dataset.pane !== name;
}

function buildAll() {
  syncers.clear();
  buildZoom();
  buildBackground();
  buildFrame();
  buildMotion();
}

// ---------------------------------------------------------------- controls
const pct = v => `${Math.round(v * 100)}%`;

function slider({ label, min, max, step, get, set, fmt: f = v => v, def, reg = syncers }) {
  const out = h('output');
  const inp = h('input', { type: 'range', min, max, step, 'aria-label': label });
  const top = h('div', { class: 'ctl-top', 'data-tip': def != null ? 'Double-click to reset' : null },
    h('span', { class: 'ctl-l' }, label), out);
  const row = h('div', { class: 'ctl' }, top, inp);
  const paint = v => {
    out.textContent = f(v);
    inp.style.setProperty('--p', `${clamp((v - min) / (max - min), 0, 1) * 100}%`);
  };
  inp.addEventListener('input', () => { checkpoint(label, true); set(+inp.value); paint(+inp.value); });
  if (def != null) top.addEventListener('dblclick', () => { checkpoint(label); set(def); sync(); });
  const sync = () => { const v = +get(); if (+inp.value !== v) inp.value = String(v); paint(v); };
  if (reg instanceof Set) reg.add(sync); else reg.push(sync);
  sync();
  return row;
}

function chips(options, { get, set, cls = '', reg = syncers, label }) {
  const box = h('div', { class: 'chips ' + cls, role: 'group', 'aria-label': label });
  const btns = options.map(o => {
    const b = h('button', { class: 'chip', type: 'button', 'data-tip': o.tip, onclick: () => set(o.value) },
      o.icon ? icon(o.icon) : null, o.swatch || null, o.label != null ? h('span', null, o.label) : null);
    box.append(b);
    return [o, b];
  });
  const sync = () => {
    const v = get();
    for (const [o, b] of btns) {
      const hit = JSON.stringify(o.value) === JSON.stringify(v);
      b.classList.toggle('on', hit);
      b.setAttribute('aria-pressed', String(hit));
    }
  };
  if (reg instanceof Set) reg.add(sync); else reg.push(sync);
  sync();
  return box;
}

const section = (title, ...kids) => h('section', { class: 'sec' },
  title ? h('h3', { class: 'sec-h' }, title) : null, ...kids);

const style = () => S.doc.style;
const bg = () => (S.doc.style.background ||= {});
const setStyle = fn => v => { fn(v); changed('style'); };

// ---------------------------------------------------------------- zoom
function buildZoom() {
  const pane = $('#paneZoom');
  if (!pane || !S.doc) return;
  zoomSync = [];
  zoomSig = segSig();
  const seg = selected();
  pane.replaceChildren(seg ? zoomEditor(seg) : zoomOverview());
}

function zoomOverview() {
  const list = [...S.doc.segs].sort((a, b) => a.start - b.start);
  const rows = h('div', { class: 'zlist' });
  const fill = () => {
    rows.replaceChildren();
    const segs = [...S.doc.segs].sort((a, b) => a.start - b.start);
    if (!segs.length) {
      rows.append(h('div', { class: 'empty-note' },
        icon('zoom-in'), h('b', null, 'No zooms yet'),
        h('span', null, 'Hover the Zoom track and click to add one, press Z at the playhead, or double-click the preview where you want the camera to go.')));
      return;
    }
    segs.forEach((s, i) => rows.append(h('button', {
      class: 'zrow', type: 'button', onclick: () => focusSeg(s),
    }, h('span', { class: 'zi' }, String(i + 1)),
      h('span', { class: 'zt' }, `${fmt(s.start, 1)} – ${fmt(s.end, 1)}`),
      h('span', { class: 'zz' }, `${+s.zoom.toFixed(2)}×`),
      h('span', { class: 'ztag' + (s.reason === 'manual' ? ' manual' : '') }, s.reason === 'manual' ? 'Manual' : 'Auto'))));
  };
  fill();
  zoomSync.push(fill);

  let preset = store('director', 'balanced');
  const btn = h('button', { class: 'btn wide', type: 'button' }, icon('sparkles'), h('span', null, 'Re-run auto zoom'));
  btn.onclick = async () => {
    btn.disabled = true;
    btn.classList.add('busy');
    try {
      const n = await rerunAuto(DIRECTORS[preset]);
      toast(`Auto zoom placed ${n} zoom${n === 1 ? '' : 's'}.`, {
        kind: 'ok', action: { label: 'Undo', run: () => emit('undo') } });
    } catch (e) { toast(e.message, { kind: 'err' }); }
    finally { btn.disabled = false; btn.classList.remove('busy'); }
  };
  const intensity = chips(Object.entries(DIRECTORS).map(([k, d]) => ({ value: k, label: d.label })), {
    get: () => preset, set: v => { preset = v; remember('director', v); intensity.querySelectorAll('.chip').forEach(b => b.classList.toggle('on', b.textContent === DIRECTORS[v].label)); },
    reg: zoomSync, label: 'Auto zoom intensity',
  });

  return h('div', null,
    section(`Zooms · ${list.length}`, rows,
      list.length ? h('button', { class: 'btn ghost small', type: 'button', onclick: clearZooms }, icon('trash'), 'Remove all zooms') : null),
    section('Auto zoom',
      h('p', { class: 'note' }, 'Zoomcut places zooms where things happen on screen and holds still everywhere else.'),
      h('div', { class: 'ctl-l sub' }, 'Intensity'), intensity, btn,
      h('p', { class: 'note dim' }, 'Replaces the zooms on the timeline. Undo brings them back.')),
    section('Tips', h('ul', { class: 'tips' },
      h('li', null, h('kbd', null, 'Z'), ' add a zoom at the playhead'),
      h('li', null, h('kbd', null, 'S'), ' split the zoom under the playhead'),
      h('li', null, 'Double-click the preview to zoom to that point'),
      h('li', null, h('kbd', null, '['), h('kbd', null, ']'), ' step between zooms'))));
}

function zoomEditor(seg) {
  const cur = () => S.doc.segs.find(s => s.id === seg.id) || seg;
  const list = [...S.doc.segs].sort((a, b) => a.start - b.start);
  const idx = list.findIndex(s => s.id === seg.id);
  const go = d => { const n = list[idx + d]; if (n) focusSeg(n); };

  const head = h('div', { class: 'zhead' },
    h('div', { class: 'zhead-t' },
      h('b', null, `Zoom ${idx + 1}`), h('span', null, ` of ${list.length}`),
      h('span', { class: 'ztag' + (seg.reason === 'manual' ? ' manual' : '') }, seg.reason === 'manual' ? 'Manual' : 'Auto')),
    h('div', { class: 'zhead-b' },
      h('button', { class: 'ib', type: 'button', 'data-tip': 'Previous zoom', 'data-kbd': '[', disabled: idx <= 0, onclick: () => go(-1) }, icon('chevron-left')),
      h('button', { class: 'ib', type: 'button', 'data-tip': 'Next zoom', 'data-kbd': ']', disabled: idx >= list.length - 1, onclick: () => go(1) }, icon('chevron-right')),
      h('button', { class: 'ib', type: 'button', 'data-tip': 'Done', 'data-kbd': 'Esc', onclick: () => select(null) }, icon('x'))));

  const level = slider({
    label: 'Zoom level', min: 1.05, max: MAX_ZOOM, step: 0.01, reg: zoomSync,
    get: () => cur().zoom, fmt: v => `${(+v).toFixed(2)}×`,
    set: v => { const s = cur(); s.zoom = v; [s.cx, s.cy] = clampCenter(v, s.cx, s.cy); intoView(s); changed('segs'); },
  });
  const presets = chips([1.25, 1.5, 2, 3].map(z => ({ value: z, label: `${z}×` })), {
    get: () => cur().zoom, reg: zoomSync, label: 'Zoom presets', cls: 'tight',
    set: v => { checkpoint('Zoom level'); const s = cur(); s.zoom = v; [s.cx, s.cy] = clampCenter(v, s.cx, s.cy); intoView(s); changed('segs'); },
  });

  const timeField = (label, get, set) => {
    const inp = h('input', { type: 'text', class: 'tfield', inputmode: 'decimal', 'aria-label': label, spellcheck: 'false' });
    const sync = () => { if (document.activeElement !== inp) inp.value = fmt(get(), 2); };
    const commit = () => {
      const v = parseTime(inp.value);
      if (Number.isNaN(v)) { toast('Use a time like 0:04.50 or 4.5', { kind: 'err' }); sync(); return; }
      checkpoint(label); set(v); changed('segs'); sync();
    };
    inp.addEventListener('change', commit);
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); inp.blur(); } if (e.key === 'Escape') { sync(); inp.blur(); } });
    zoomSync.push(sync);
    sync();
    return h('label', { class: 'tctl' }, h('span', { class: 'ctl-l' }, label), inp);
  };
  const timing = h('div', { class: 'grid2' },
    timeField('Starts at', () => cur().start, v => {
      const s = cur(), { lo } = bounds(s.id);
      const len = s.end - s.start;
      s.start = clamp(v, lo, s.end - MIN_SEG);
      if (s.start + len <= bounds(s.id).hi) s.end = Math.max(s.end, s.start + MIN_SEG);
    }),
    timeField('Lasts', () => cur().end - cur().start, v => {
      const s = cur(), { hi } = bounds(s.id);
      s.end = clamp(s.start + v, s.start + MIN_SEG, hi);
    }));

  const actions = h('div', { class: 'row-btns' },
    h('button', { class: 'btn small', type: 'button', 'data-tip': 'Put the zoom back in the middle', onclick: () => {
      checkpoint('Centre zoom'); const s = cur(); [s.cx, s.cy] = clampCenter(s.zoom, 0.5, 0.5); intoView(s); changed('segs');
    } }, icon('crosshair'), 'Centre'),
    h('button', { class: 'btn small', type: 'button', 'data-tip': 'Show this zoom in the preview', onclick: () => { pause(); seek(settlePoint(cur())); } }, icon('eye'), 'Preview'),
    h('button', { class: 'btn small danger', type: 'button', 'data-kbd': '⌫', 'data-tip': 'Delete this zoom', onclick: deleteSelected }, icon('trash'), 'Delete'));

  return h('div', null, head,
    section('Framing', picker(cur),
      h('p', { class: 'note dim' }, 'Drag the box, or drag the preview itself. Pull a corner — or scroll over the preview — to zoom.')),
    section(null, level, presets),
    section('Timing', timing),
    section(null, actions));
}

let strip = { path: null, img: null };
function stripImage(path, use) {
  if (strip.path === path && strip.img?.complete) return use(strip.img);
  if (strip.path !== path) {
    strip = { path, img: new Image() };
    strip.img.src = fileUrl(path);
  }
  strip.img.addEventListener('load', () => use(strip.img), { once: true });
}

/** Park the playhead where the zoom has settled, so edits are visible. */
function intoView(s) {
  if (!(S.t >= s.start && S.t <= s.end)) seek(settlePoint(s));
}

/** The whole frame with the zoom's crop drawn on it - the Screen Studio way
 *  of framing a zoom, with the full picture in view. */
function picker(cur) {
  const w = 320, hgt = Math.max(40, Math.round(320 * S.sh / S.sw));
  const cv = h('canvas', { class: 'pick-cv', width: w, height: hgt });
  const rect = h('div', { class: 'pick-rect' },
    ...['nw', 'ne', 'sw', 'se'].map(c => h('i', { class: `pk pk-${c}` })),
    h('span', { class: 'pick-z' }));
  const box = h('div', { class: 'picker', style: { aspectRatio: `${S.sw} / ${S.sh}` } }, cv, rect);

  const place = () => {
    const s = cur();
    const r = cropRect(s.zoom, s.cx, s.cy);
    Object.assign(rect.style, { left: r.x0 * 100 + '%', top: r.y0 * 100 + '%', width: r.w * 100 + '%', height: r.w * 100 + '%' });
    rect.querySelector('.pick-z').textContent = `${s.zoom.toFixed(2)}×`;
  };
  const paint = () => {
    const s = cur();
    const ctx = cv.getContext('2d');
    if (S.t >= s.start && S.t <= s.end && grabFrame(cv)) return;
    const m = S.media?.strip;
    if (m?.path) {
      stripImage(m.path, img => {
        const i = clamp(Math.floor(settlePoint(s) / m.interval), 0, m.count - 1);
        ctx.drawImage(img, (i % m.cols) * m.tw, Math.floor(i / m.cols) * m.th, m.tw, m.th, 0, 0, w, hgt);
      });
    } else {
      ctx.fillStyle = '#1b1c24';
      ctx.fillRect(0, 0, w, hgt);
    }
  };
  place.frame = paint;
  zoomSync.push(place);
  requestAnimationFrame(() => { place(); paint(); });

  box.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    const s = cur();
    pause();
    intoView(s);
    const br = box.getBoundingClientRect();
    const corner = e.target.closest('.pk');
    const onRect = e.target.closest('.pick-rect');
    checkpoint(corner ? 'Zoom level' : 'Reframe zoom');
    if (!corner && !onRect) {
      // clicking outside the box jumps the centre there, then drags from it
      [s.cx, s.cy] = clampCenter(s.zoom, (e.clientX - br.left) / br.width, (e.clientY - br.top) / br.height);
      changed('segs');
    }
    const sx = e.clientX, sy = e.clientY, cx0 = s.cx, cy0 = s.cy;
    rect.classList.add('active');
    drag(e, {
      move: ev => {
        if (corner) {
          const nx = (ev.clientX - br.left) / br.width, ny = (ev.clientY - br.top) / br.height;
          const half = Math.max(Math.abs(nx - s.cx), Math.abs(ny - s.cy));
          s.zoom = Math.round(clamp(1 / Math.max(2 * half, 1e-3), 1.05, MAX_ZOOM) * 100) / 100;
        } else {
          s.cx = cx0 + (ev.clientX - sx) / br.width;
          s.cy = cy0 + (ev.clientY - sy) / br.height;
        }
        [s.cx, s.cy] = clampCenter(s.zoom, s.cx, s.cy);
        S.directCam = [s.zoom, s.cx, s.cy];
        changed('segs');
      },
      up: () => { rect.classList.remove('active'); S.directCam = null; dropCheckpoint(); changed('segs'); },
    }, box);
  });
  return box;
}

// ---------------------------------------------------------------- background
let bgBuiltFor = null;
const bgKindChanged = () => bg().type !== bgBuiltFor;

function buildBackground() {
  const pane = $('#paneBg');
  if (!pane || !S.doc) return;
  for (const fn of [...syncers]) if (fn.bg) syncers.delete(fn);
  const b = bg();
  bgBuiltFor = b.type || 'wallpaper';
  const reg = new Set();
  const kind = chips([
    { value: 'wallpaper', label: 'Wallpaper', icon: 'image' },
    { value: 'gradient', label: 'Gradient', icon: 'gradient' },
    { value: 'color', label: 'Colour', icon: 'drop' },
    { value: 'image', label: 'Image', icon: 'upload' },
  ], {
    get: () => bg().type || 'wallpaper', reg, label: 'Background type', cls: 'kinds',
    set: v => {
      if (v === 'image' && !bg().path) return pickImage();
      checkpoint('Background');
      bg().type = v;
      changed('style');
    },
  });

  let body;
  if (bgBuiltFor === 'wallpaper') body = wallpapers(reg);
  else if (bgBuiltFor === 'gradient') body = gradients(reg);
  else if (bgBuiltFor === 'color') body = colours(reg);
  else body = imageBg();

  const common = [];
  if (bgBuiltFor === 'wallpaper' || bgBuiltFor === 'image') {
    common.push(slider({ label: 'Blur', min: 0, max: 60, step: 1, def: 0, reg,
      get: () => bg().blur || 0, fmt: v => `${Math.round(v)} px`, set: setStyle(v => { bg().blur = v; }) }));
  }
  common.push(slider({ label: 'Dim', min: 0, max: 0.6, step: 0.01, def: DEFAULTS.background.dim, reg,
    get: () => bg().dim ?? 0, fmt: pct, set: setStyle(v => { bg().dim = v; }) }));

  for (const fn of reg) { fn.bg = true; syncers.add(fn); }
  pane.replaceChildren(section(null, kind), body, section('Adjust', ...common));
}

function wallpapers(reg) {
  const list = S.wallpapers || [];
  if (!list.length) {
    return section('Wallpapers', h('div', { class: 'empty-note' }, icon('image'), h('b', null, 'No desktop pictures here'),
      h('span', null, 'This machine has no wallpapers Zoomcut can read. Try a gradient or your own image.')));
  }
  const grid = h('div', { class: 'wgrid' });
  const current = () => bg().name || S.wallDefault;
  for (const name of list) {
    const t = h('button', { class: 'wtile', type: 'button', 'data-tip': name, 'aria-label': name,
      onclick: () => { checkpoint('Background'); Object.assign(bg(), { type: 'wallpaper', name }); changed('style'); } },
      h('img', { loading: 'lazy', src: wallpaperUrl(name), alt: '' }),
      name === S.wallDefault ? h('span', { class: 'wbadge' }, 'Desktop') : null,
      h('span', { class: 'wname' }, name));
    grid.append(t);
  }
  const sync = () => {
    const c = current();
    for (const t of grid.children) t.classList.toggle('on', t.getAttribute('aria-label') === c);
  };
  reg.add(sync);
  sync();
  requestAnimationFrame(() => grid.querySelector('.on')?.scrollIntoView({ block: 'nearest' }));
  return section('Wallpapers', grid);
}

function gradients(reg) {
  const g = () => (bg().gradient ||= { from: '#3F37C9', to: '#8C87DF', p0: [0, 0], p1: [1, 1] });
  const swatch = (a, b) => h('i', { class: 'gsw', style: { background: `linear-gradient(135deg, ${a}, ${b})` } });
  const presets = chips(GRADIENTS.map(([n, a, b]) => ({ value: [a, b], swatch: swatch(a, b), tip: n })), {
    get: () => [g().from, g().to], reg, label: 'Gradient presets', cls: 'swatches',
    set: ([a, b]) => { checkpoint('Background'); Object.assign(g(), { from: a, to: b }); bg().type = 'gradient'; changed('style'); },
  });
  const pick = (label, key) => {
    const inp = h('input', { type: 'color', 'aria-label': label });
    const code = h('span', { class: 'hex' });
    const sync = () => { inp.value = normHex(g()[key]); code.textContent = inp.value.toUpperCase(); };
    inp.addEventListener('input', () => { checkpoint(label, true); g()[key] = inp.value; code.textContent = inp.value.toUpperCase(); changed('style'); });
    reg.add(sync);
    sync();
    return h('label', { class: 'cpick' }, inp, h('span', { class: 'cpick-l' }, label), code);
  };
  const dirs = chips(DIRS.map(([n, rot, p0, p1]) => ({ value: [p0, p1], icon: 'arrow', tip: `Direction ${n}`, rot })), {
    get: () => [g().p0 || [0, 0], g().p1 || [1, 1]], reg, label: 'Gradient direction', cls: 'dirs',
    set: ([p0, p1]) => { checkpoint('Background'); Object.assign(g(), { p0, p1 }); changed('style'); },
  });
  [...dirs.children].forEach((b, i) => { b.querySelector('svg').style.transform = `rotate(${DIRS[i][1]}deg)`; });
  return h('div', null,
    section('Presets', presets),
    section('Colours', h('div', { class: 'grid2' }, pick('From', 'from'), pick('To', 'to'))),
    section('Direction', dirs));
}

function normHex(c) {
  let s = String(c || '#000000').trim();
  if (/^#[0-9a-f]{3}$/i.test(s)) s = '#' + [...s.slice(1)].map(x => x + x).join('');
  return /^#[0-9a-f]{6}$/i.test(s) ? s.toLowerCase() : '#000000';
}

function colours(reg) {
  const sw = chips(COLORS.map(c => ({ value: c, swatch: h('i', { class: 'csw', style: { background: c } }), tip: c })), {
    get: () => (bg().color || '').toUpperCase(), reg, label: 'Colours', cls: 'swatches',
    set: c => { checkpoint('Background'); Object.assign(bg(), { type: 'color', color: c }); changed('style'); },
  });
  const inp = h('input', { type: 'color', 'aria-label': 'Custom colour' });
  const code = h('span', { class: 'hex' });
  const sync = () => { inp.value = normHex(bg().color); code.textContent = inp.value.toUpperCase(); };
  inp.addEventListener('input', () => { checkpoint('Colour', true); bg().color = inp.value.toUpperCase(); code.textContent = bg().color; changed('style'); });
  reg.add(sync);
  sync();
  return h('div', null, section('Colours', sw),
    section('Custom', h('label', { class: 'cpick' }, inp, h('span', { class: 'cpick-l' }, 'Pick any colour'), code)));
}

function imageBg() {
  const b = bg();
  const name = (b.path || '').split(/[\\/]/).pop();
  return section('Your image',
    b.path ? h('div', { class: 'imgprev', style: { backgroundImage: `url("${fileUrl(b.path)}")` } }, h('span', null, name)) : null,
    h('button', { class: 'btn wide', type: 'button', onclick: pickImage }, icon('upload'), b.path ? 'Choose another image…' : 'Choose an image…'),
    h('p', { class: 'note dim' }, 'PNG, JPEG, WebP or HEIC. It is copied into your Zoomcut folder.'));
}

function pickImage() {
  const inp = h('input', { type: 'file', accept: 'image/*,.heic' });
  inp.onchange = async () => {
    const f = inp.files[0];
    if (!f) return;
    const done = toast(`Adding ${f.name}…`, { ms: 0 });
    try {
      const r = await upload(f).promise;
      if (r.kind !== 'image') throw new Error('That file is not an image.');
      checkpoint('Background');
      Object.assign(bg(), { type: 'image', path: r.path });
      changed('style');
    } catch (e) { toast(e.message, { kind: 'err' }); }
    finally { done(); }
  };
  inp.click();
}

// ---------------------------------------------------------------- frame
function buildFrame() {
  const pane = $('#paneFrame');
  if (!pane || !S.doc) return;
  const sh = () => (style().shadow ||= {});
  const aspect = chips(Object.keys(ASPECTS).map(k => ({ value: k, label: k, swatch: ratioIcon(ASPECTS[k]) })), {
    get: () => aspectKey(), label: 'Aspect ratio', cls: 'aspects',
    set: k => setOutput(sizeKey(), k),
  });
  const dimsNote = h('p', { class: 'note dim' });
  syncers.add(() => { const { width: w, height: hh } = S.doc.output; dimsNote.textContent = `Exports at ${w} × ${hh}. Change the resolution when you export.`; });

  pane.replaceChildren(
    section('Canvas', aspect, dimsNote),
    section('Window',
      slider({ label: 'Padding', min: 0, max: 0.2, step: 0.002, def: DEFAULTS.paddingRatio,
        get: () => style().paddingRatio ?? DEFAULTS.paddingRatio, fmt: v => pct(v * 5), set: setStyle(v => { style().paddingRatio = v; }) }),
      slider({ label: 'Roundness', min: 0, max: 0.04, step: 0.0005, def: DEFAULTS.cornerRadius,
        get: () => style().cornerRadius ?? DEFAULTS.cornerRadius, fmt: v => pct(v * 25), set: setStyle(v => { style().cornerRadius = v; }) }),
      slider({ label: 'Edge highlight', min: 0, max: 0.4, step: 0.01, def: DEFAULTS.edgeHighlight,
        get: () => style().edgeHighlight ?? DEFAULTS.edgeHighlight, fmt: v => pct(v / 0.4), set: setStyle(v => { style().edgeHighlight = v; }) })),
    section('Shadow',
      slider({ label: 'Strength', min: 0, max: 1, step: 0.01, def: DEFAULTS.shadow.alpha,
        get: () => sh().alpha ?? DEFAULTS.shadow.alpha, fmt: pct, set: setStyle(v => { sh().alpha = v; }) }),
      slider({ label: 'Distance', min: 0, max: 0.08, step: 0.001, def: DEFAULTS.shadow.distance,
        get: () => sh().distance ?? DEFAULTS.shadow.distance, fmt: v => pct(v / 0.08), set: setStyle(v => { sh().distance = v; }) }),
      slider({ label: 'Softness', min: 0, max: 0.08, step: 0.001, def: DEFAULTS.shadow.blur,
        get: () => sh().blur ?? DEFAULTS.shadow.blur, fmt: v => pct(v / 0.08), set: setStyle(v => { sh().blur = v; }) })),
    section(null, h('button', { class: 'btn ghost small', type: 'button', onclick: () => {
      checkpoint('Reset frame');
      Object.assign(style(), { paddingRatio: DEFAULTS.paddingRatio, cornerRadius: DEFAULTS.cornerRadius, edgeHighlight: DEFAULTS.edgeHighlight });
      Object.assign(sh(), DEFAULTS.shadow);
      changed('style');
    } }, icon('refresh'), 'Reset window and shadow')));
}

function ratioIcon(r) {
  const w = r >= 1 ? 18 : Math.round(18 * r), hh = r >= 1 ? Math.round(18 / r) : 18;
  return h('i', { class: 'ratio', style: { width: w + 'px', height: hh + 'px' } });
}

// ---------------------------------------------------------------- motion
function buildMotion() {
  const pane = $('#paneMotion');
  if (!pane || !S.doc) return;
  const sp = () => S.doc.spring;
  const presetOf = () => Object.keys(SPRINGS).find(k => ['mass', 'stiffness', 'damping'].every(p => Math.abs(SPRINGS[k][p] - sp()[p]) < 1e-6)) || null;

  const curve = h('canvas', { class: 'curve', width: 600, height: 180, 'aria-label': 'How the camera eases into a new zoom' });
  const settle = h('span', { class: 'settle' });
  const drawCurve = () => {
    const { t, x, settleAt } = response(sp());
    const ctx = curve.getContext('2d');
    const W = curve.width, H = curve.height, pad = 16;
    ctx.clearRect(0, 0, W, H);
    const cs = getComputedStyle(document.documentElement);
    const X_ = v => pad + v / t[t.length - 1] * (W - pad * 2);
    const Y_ = v => H - pad - v * (H - pad * 2.6);
    ctx.strokeStyle = cs.getPropertyValue('--border-strong');
    ctx.setLineDash([6, 6]); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(pad, Y_(1)); ctx.lineTo(W - pad, Y_(1)); ctx.stroke();
    ctx.setLineDash([]);
    if (settleAt != null) {
      ctx.strokeStyle = cs.getPropertyValue('--text-3');
      ctx.beginPath(); ctx.moveTo(X_(settleAt), pad); ctx.lineTo(X_(settleAt), H - pad); ctx.stroke();
    }
    const g = ctx.createLinearGradient(0, 0, W, 0);
    g.addColorStop(0, cs.getPropertyValue('--accent'));
    g.addColorStop(1, cs.getPropertyValue('--accent-2'));
    ctx.strokeStyle = g; ctx.lineWidth = 4; ctx.lineJoin = 'round';
    ctx.beginPath();
    t.forEach((v, i) => (i ? ctx.lineTo(X_(v), Y_(x[i])) : ctx.moveTo(X_(v), Y_(x[i]))));
    ctx.stroke();
    settle.textContent = settleAt == null ? 'Still moving after 2 s' : `Settles in ${settleAt.toFixed(2)} s`;
  };
  syncers.add(drawCurve);

  const presets = h('div', { class: 'springs' }, ...Object.entries(SPRINGS).map(([k, p]) => {
    const b = h('button', { class: 'spring', type: 'button', onclick: () => {
      checkpoint('Motion');
      Object.assign(sp(), { mass: p.mass, stiffness: p.stiffness, damping: p.damping });
      changed('spring');
    } }, h('b', null, p.label), h('span', null, `${response(p).settleAt?.toFixed(2) ?? '–'} s`));
    syncers.add(() => { const on_ = presetOf() === k; b.classList.toggle('on', on_); b.setAttribute('aria-pressed', String(on_)); });
    return b;
  }));

  const adv = h('details', { class: 'adv' }, h('summary', null, 'Fine-tune the spring'),
    slider({ label: 'Stiffness', min: 20, max: 600, step: 1, def: SPRINGS.balanced.stiffness,
      get: () => sp().stiffness, fmt: v => Math.round(v), set: v => { sp().stiffness = v; changed('spring'); } }),
    slider({ label: 'Damping', min: 5, max: 80, step: 0.5, def: SPRINGS.balanced.damping,
      get: () => sp().damping, fmt: v => (+v).toFixed(1), set: v => { sp().damping = v; changed('spring'); } }),
    slider({ label: 'Mass', min: 0.5, max: 6, step: 0.05, def: SPRINGS.balanced.mass,
      get: () => sp().mass, fmt: v => (+v).toFixed(2), set: v => { sp().mass = v; changed('spring'); } }));

  pane.replaceChildren(
    section('Camera motion',
      h('p', { class: 'note' }, 'Every move is driven by a spring, so the camera eases in and settles like something with weight — not like a tween.'),
      presets, h('div', { class: 'curve-box' }, curve, settle)),
    section(null, adv));
  drawCurve();
}

export function refreshWallpapers() { if (S.doc && bgBuiltFor === 'wallpaper') buildBackground(); }
