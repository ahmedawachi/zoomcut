// The start screen: record something, open something, or pick up a recent one.
import { S, on, emit } from './store.js';
import { api, posterUrl, upload } from './api.js';
import { $, h, icon, toast, fmt, fmtBytes, ago, store, remember, clamp } from './ui.js';

const MODE = {
  window: { label: 'Window', icon: 'window' },
  display: { label: 'Display', icon: 'monitor' },
  region: { label: 'Region', icon: 'crop' },
  interactive: { label: 'Ask me', icon: 'pointer' },
};

let mode = store('mode', 'window');
let windows = [], winId = null, query = '';
let opts = { cursor: store('cursor', true), clicks: store('clicks', false), countdown: store('countdown', true) };
let region = store('region', [0, 0, 1280, 800]);
let display = 1;
let handlers = {};          // {analyze(path), load(path), play(path)} from app.js

export function initHome(h_) {
  handlers = h_;
  on('sys', paintStatus);
  build();
}

export function showHome() {
  build();
  loadWindows();
  loadRecent();
}

// ---------------------------------------------------------------- layout
function build() {
  const root = $('#home');
  root.replaceChildren(h('div', { class: 'home-in' },
    S.project ? h('button', { class: 'resume', type: 'button', onclick: () => emit('view', 'editor') },
      icon('film'), h('span', null, 'Continue editing ', h('b', null, S.project.source.split(/[\\/]/).pop())), icon('chevron-right')) : null,
    h('div', { class: 'hero' },
      h('h1', null, 'Make a screen recording look ', h('em', null, 'edited'), '.'),
      h('p', null, 'Record, and Zoomcut moves the camera to what matters — then holds perfectly still. Every zoom stays editable.')),
    h('div', { class: 'starts' }, recordCard(), openCard()),
    h('section', { class: 'recent' },
      h('div', { class: 'recent-h' }, h('h2', null, 'Recent'), h('span', { class: 'recent-dir', id: 'recentDir' }),
        h('button', { class: 'ib', type: 'button', 'data-tip': 'Refresh', onclick: loadRecent }, icon('refresh'))),
      h('div', { class: 'recent-grid', id: 'recentGrid' }, skeletons()))));
  paintStatus();
}

const skeletons = () => Array.from({ length: 4 }, () => h('div', { class: 'rcard skel' }, h('div', { class: 'rposter' }), h('div', { class: 'rmeta' }, h('i'), h('i'))));

// ---------------------------------------------------------------- record
function recordCard() {
  const modes = (S.sys?.modes || ['window', 'display', 'region']).filter(m => MODE[m]);
  if (!modes.includes(mode)) mode = modes[0];
  const tabs = h('div', { class: 'modes', role: 'tablist' }, ...modes.map(m => h('button', {
    class: 'mode' + (m === mode ? ' on' : ''), type: 'button', role: 'tab', 'aria-selected': String(m === mode),
    onclick: () => {
      mode = m; remember('mode', m); build();
      if (m === 'window') loadWindows();
      $('#home .mode.on')?.focus();
    },
  }, icon(MODE[m].icon), h('span', null, MODE[m].label))));

  let body;
  if (mode === 'window') {
    const search = h('input', { type: 'text', placeholder: 'Search windows', value: query, 'aria-label': 'Search windows', spellcheck: 'false' });
    search.addEventListener('input', () => { query = search.value; paintWindows(); });
    body = h('div', { class: 'mode-body' },
      h('div', { class: 'search' }, icon('search'), search,
        h('button', { class: 'ib', type: 'button', 'data-tip': 'Refresh the list', onclick: loadWindows }, icon('refresh'))),
      h('div', { class: 'wcards', id: 'winGrid', role: 'listbox', 'aria-label': 'Windows' }, h('div', { class: 'wcards-empty' }, 'Looking for windows…')));
  } else if (mode === 'display') {
    const n = h('input', { type: 'number', min: 1, value: display, 'aria-label': 'Display number' });
    n.addEventListener('input', () => { display = Math.max(1, parseInt(n.value || '1', 10)); });
    body = h('div', { class: 'mode-body pad' }, h('div', { class: 'big-ico' }, icon('monitor')),
      h('label', { class: 'field' }, h('span', null, 'Display'), n, h('small', null, '1 is your main display')));
  } else if (mode === 'region') {
    const f = (label, i) => {
      const inp = h('input', { type: 'number', value: region[i], 'aria-label': label });
      inp.addEventListener('input', () => { region[i] = parseInt(inp.value || '0', 10); remember('region', region); });
      return h('label', { class: 'field' }, h('span', null, label), inp);
    };
    body = h('div', { class: 'mode-body pad' }, h('div', { class: 'grid4' }, f('X', 0), f('Y', 1), f('Width', 2), f('Height', 3)),
      h('small', { class: 'note dim' }, 'In screen points, from the top-left of the main display.'));
  } else {
    body = h('div', { class: 'mode-body pad' }, h('div', { class: 'big-ico' }, icon('pointer')),
      h('p', { class: 'note' }, 'macOS will let you drag out an area, or click a window, when you hit record.'));
  }

  const tog = (key, label, ico, tip) => {
    const inp = h('input', { type: 'checkbox', checked: opts[key] });
    inp.addEventListener('change', () => { opts[key] = inp.checked; remember(key, inp.checked); });
    return h('label', { class: 'opt', 'data-tip': tip }, inp, h('span', { class: 'sw' }), icon(ico), h('span', null, label));
  };
  const toggles = h('div', { class: 'toggles' },
    tog('cursor', 'Cursor', 'mouse', 'Show the pointer in the recording'),
    S.sys?.clicksSupported ? tog('clicks', 'Clicks', 'click', 'Highlight every click') : null,
    tog('countdown', 'Countdown', 'timer', 'Count down 3 seconds first, so you can switch windows'));

  return h('section', { class: 'card start rec' },
    h('div', { class: 'card-h' }, h('span', { class: 'card-ico red' }, icon('record')), h('div', null, h('h2', null, 'New recording'), h('p', null, 'Pick what to capture.'))),
    tabs, body, toggles,
    h('div', { class: 'perm', id: 'permBox', hidden: true }),
    h('button', { class: 'btn rec big', type: 'button', id: 'btnRecord', onclick: record }, h('i', { class: 'recdot' }), h('span', null, 'Start recording')));
}

async function loadWindows() {
  if (mode !== 'window') return;
  try {
    const r = await api('/api/windows');
    windows = r.windows || [];
    if (r.error) toast(r.error, { kind: 'err', ms: 9000 });
    if (!windows.some(w => w.id === winId)) winId = windows[0]?.id ?? null;
  } catch (e) {
    windows = [];
    toast('Could not list windows: ' + e.message, { kind: 'err' });
  }
  paintWindows();
}

const hue = s => [...String(s)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);

function paintWindows() {
  const grid = $('#winGrid');
  if (!grid) return;
  const q = query.trim().toLowerCase();
  const list = windows.filter(w => !q || `${w.app} ${w.title}`.toLowerCase().includes(q));
  grid.replaceChildren();
  if (!list.length) {
    grid.append(h('div', { class: 'wcards-empty' }, windows.length ? 'No window matches that.' :
      'No windows to record here. Try Display or Region.'));
    return;
  }
  for (const w of list) {
    const on_ = w.id === winId;
    const el = h('button', { class: 'wcard' + (on_ ? ' on' : ''), type: 'button', role: 'option', 'aria-selected': String(on_),
      onclick: () => { winId = w.id; paintWindows(); },
      ondblclick: () => { winId = w.id; record(); } },
      h('span', { class: 'wc-ico', style: { '--h': hue(w.app) } }, (w.app || '?').slice(0, 2)),
      h('span', { class: 'wc-m' }, h('b', null, w.app || 'Untitled'), h('span', null, w.title || 'untitled window')),
      h('span', { class: 'wc-sz' }, `${w.width}×${w.height}`));
    grid.append(el);
  }
}

function paintStatus() {
  const box = $('#permBox'), btn = $('#btnRecord');
  if (!box || !btn || !S.sys) return;
  const [ok, why] = S.sys.permission || [true, ''];
  const problems = [];
  if (!S.sys.ffmpeg) problems.push(['ffmpeg is not installed', 'Install it (macOS: brew install ffmpeg · Windows: winget install Gyan.FFmpeg · Linux: your package manager), then restart Zoomcut.']);
  const mac = handlers.desktop?.platform === 'darwin';
  if (!ok) {
    problems.push(['Screen recording is blocked', mac
      ? 'Turn Zoomcut on under Privacy & Security › Screen & System Audio Recording, then quit and reopen Zoomcut.'
      : why, mac]);
  }
  btn.disabled = !!problems.length || !!S.sys.recording;
  // repainted on every status poll: only rebuild when something changed,
  // or the button inside would lose the focus every second and a half
  const key = JSON.stringify(problems);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.hidden = !problems.length;
  box.replaceChildren(...problems.map(([t, d, settings]) => h('div', { class: 'perm-i' }, icon('alert'),
    h('div', null, h('b', null, t), h('span', null, d),
      settings ? h('button', { class: 'btn small', type: 'button', onclick: () => handlers.desktop.openScreenSettings() },
        'Open System Settings') : null))));
}

// ---------------------------------------------------------------- recording
let countdown = null;

async function record() {
  if (S.sys?.recording) return;
  const body = { mode, cursor: opts.cursor, clicks: opts.clicks };
  if (mode === 'window') {
    if (!winId) { toast('Pick a window first.', { kind: 'err' }); return; }
    body.windowId = winId;
  }
  if (mode === 'display') body.display = display;
  if (mode === 'region') {
    if (region.some(v => !Number.isFinite(v)) || region[2] <= 0 || region[3] <= 0) {
      toast('A region needs a width and height above zero.', { kind: 'err' });
      return;
    }
    body.region = region;
  }
  const w = windows.find(x => x.id === winId);
  const what = mode === 'window' && w ? `${w.app} — ${w.title || 'window'}` : MODE[mode].label;
  if (opts.countdown && mode !== 'interactive') {
    const go = await runCountdown(what);
    if (!go) return;
  }
  try {
    const r = await api('/api/record/start', body);
    emit('recording', { started: Date.now(), what, path: r.path });
  } catch (e) {
    toast(e.message, { kind: 'err', ms: 12000 });
  }
}

function runCountdown(what) {
  return new Promise(resolve => {
    const num = h('div', { class: 'cd-n' }, '3');
    const el = h('div', { class: 'countdown', role: 'dialog', 'aria-label': 'Countdown' },
      h('div', { class: 'cd-ring' }, num),
      h('p', null, 'Recording ', h('b', null, what), ' in a moment — switch to it now.'),
      h('button', { class: 'btn', type: 'button', onclick: () => finish(false) }, 'Cancel'),
      h('small', null, 'Esc to cancel · Enter to start now'));
    $('#overlays').append(el);
    const cancel = el.querySelector('button');
    requestAnimationFrame(() => cancel.focus());
    let n = 3;
    const timer = setInterval(() => {
      n--;
      if (n <= 0) return finish(true);
      num.textContent = String(n);
      num.classList.remove('pop'); void num.offsetWidth; num.classList.add('pop');
    }, 1000);
    const key = e => {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Tab') { e.preventDefault(); cancel.focus(); }   // nothing behind it is live
    };
    document.addEventListener('keydown', key, true);
    function finish(go) {
      clearInterval(timer);
      document.removeEventListener('keydown', key, true);
      el.classList.add('out');
      setTimeout(() => el.remove(), 180);
      countdown = null;
      resolve(go);
    }
    countdown = finish;
  });
}

/** The full-screen "you are recording" state, with a big Stop. */
export function recordingOverlay(info, onStop) {
  $('#recOverlay')?.remove();
  const time = h('span', { class: 'ro-t' }, '0:00');
  const stop = h('button', { class: 'btn stop big', type: 'button', onclick: async () => { stop.disabled = true; stop.classList.add('busy'); await onStop(); } },
    h('i', { class: 'stopsq' }), h('span', null, 'Stop recording'));
  const el = h('div', { class: 'rec-overlay', id: 'recOverlay', role: 'dialog', 'aria-label': 'Recording' },
    h('div', { class: 'ro-card' },
      h('div', { class: 'ro-live' }, h('i', { class: 'dot live' }), 'REC', time),
      h('h2', null, info.what || 'Recording'),
      h('p', null, 'Do the thing you want to show. Come back here and stop when you are done — Zoomcut takes it from there.'),
      stop,
      h('small', null, 'Esc stops too, while this tab is in front')));
  $('#overlays').append(el);
  requestAnimationFrame(() => stop.focus());
  const key = e => {
    if (e.key === 'Escape' && !stop.disabled) { e.preventDefault(); stop.click(); }
    if (e.key === 'Tab') { e.preventDefault(); stop.focus(); }
  };
  document.addEventListener('keydown', key, true);
  const started = info.started || Date.now();
  const iv = setInterval(() => {
    if (!el.isConnected) return clearInterval(iv);
    time.textContent = fmt((Date.now() - started) / 1000, 0);
  }, 250);
  return () => {
    clearInterval(iv);
    document.removeEventListener('keydown', key, true);
    el.classList.add('out');
    setTimeout(() => el.remove(), 200);
  };
}

// ---------------------------------------------------------------- open
function openCard() {
  const file = h('input', { type: 'file', accept: 'video/*,.mov,.mkv,.mp4,.m4v,.webm', hidden: true });
  file.addEventListener('change', () => { if (file.files[0]) handlers.openFile(file.files[0]); file.value = ''; });
  const path = h('input', { type: 'text', placeholder: 'or paste a path: ~/Movies/demo.mov', 'aria-label': 'Path to a recording', spellcheck: 'false' });
  const go = () => { const p = path.value.trim().replace(/^["']|["']$/g, ''); if (p) handlers.analyze(p); };
  path.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  const drop = h('button', { class: 'dropzone', type: 'button', id: 'dropzone', onclick: () => file.click() },
    h('span', { class: 'dz-ico' }, icon('upload')),
    h('b', null, 'Drop a recording here'),
    h('span', null, 'or click to choose a file — MOV, MP4, MKV, WebM'),
    h('span', { class: 'dz-prog', hidden: true }, h('i')));
  return h('section', { class: 'card start open' },
    h('div', { class: 'card-h' }, h('span', { class: 'card-ico' }, icon('film')), h('div', null, h('h2', null, 'Open a recording'), h('p', null, 'Any screen recording you already have.'))),
    drop, file,
    h('div', { class: 'pathrow' }, path, h('button', { class: 'btn', type: 'button', onclick: go }, 'Open')));
}

export function uploadProgress(frac) {
  const dz = $('#dropzone');
  if (!dz) return;
  const bar = dz.querySelector('.dz-prog');
  bar.hidden = frac == null;
  dz.classList.toggle('busy', frac != null);
  if (frac != null) bar.firstChild.style.width = `${clamp(frac, 0, 1) * 100}%`;
}

// ---------------------------------------------------------------- recent
async function loadRecent() {
  const grid = $('#recentGrid');
  if (!grid) return;
  let r;
  try { r = await api('/api/recent'); }
  catch (e) {
    grid.replaceChildren(h('div', { class: 'recent-empty' }, e.status === 404 ? 'This version of the server does not list recent files.' : e.message));
    return;
  }
  const dir = $('#recentDir');
  if (dir) { dir.textContent = r.outDir || ''; dir.dataset.tip = 'Where Zoomcut keeps recordings and exports'; }
  const items = r.items || [];
  grid.replaceChildren();
  if (!items.length) {
    grid.append(h('div', { class: 'recent-empty' }, icon('film'), h('b', null, 'Nothing here yet'), h('span', null, 'Recordings you make, files you open and projects you save show up here.')));
    return;
  }
  const KIND = { recording: 'Recording', import: 'Opened', project: 'Project', export: 'Export' };
  for (const it of items) {
    const posterFor = it.kind === 'project' ? it.source : it.path;
    const card = h('button', { class: `rcard k-${it.kind}`, type: 'button', 'data-tip': it.path,
      onclick: () => {
        if (it.kind === 'project') handlers.load(it.path);
        else if (it.kind === 'export') handlers.play(it.path);
        else handlers.analyze(it.path);
      } },
      h('div', { class: 'rposter' },
        posterFor ? h('img', { loading: 'lazy', alt: '', src: posterUrl(posterFor), onerror: e => e.target.remove() }) : null,
        h('span', { class: 'rkind' }, KIND[it.kind] || it.kind),
        it.duration ? h('span', { class: 'rdur' }, fmt(it.duration, 0)) : null,
        h('span', { class: 'rplay' }, icon(it.kind === 'export' ? 'play' : it.kind === 'project' ? 'folder' : 'sparkles'))),
      h('div', { class: 'rmeta' }, h('b', null, it.name),
        h('span', null, [ago(it.mtime), it.width ? `${it.width}×${it.height}` : null, fmtBytes(it.size)].filter(Boolean).join(' · '))));
    grid.append(card);
  }
}
