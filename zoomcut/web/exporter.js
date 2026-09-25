// Export: pick a size and quality, watch it render, then open the result.
import { S, on, emit, changed, checkpoint, flush, trimRange, sourceName } from './store.js';
import { api, fileUrl } from './api.js';
import { SIZES, dims, sizeKey, aspectKey } from './ops.js';
import { $, h, icon, modal, toast, fmt, store, remember } from './ui.js';

const QUALITY = {
  fast: { label: 'Fast', tip: 'Quicker to make, slightly softer', crf: 20, preset: 'veryfast' },
  high: { label: 'High', tip: 'The default: crisp text, sensible size', crf: 17, preset: 'slow' },
  best: { label: 'Best', tip: 'Largest file, slowest to make', crf: 13, preset: 'slower' },
};

let job = null;            // {preview, started, name}
let poll = 0;
let sheet = null;          // the open modal, if any
let lastOutput = null;

export function initExporter() {
  $('#btnExport').addEventListener('click', openExport);
  on('project', () => { lastOutput = null; });
}

const qualityKey = () => {
  const o = S.doc.output;
  return Object.keys(QUALITY).find(k => QUALITY[k].crf === +o.crf && QUALITY[k].preset === o.preset) || 'high';
};

const baseName = () => sourceName().replace(/\.[^.]+$/, '') || 'zoomcut';

export function openExport() {
  if (!S.project) return;
  if (sheet?.open) return;
  const body = h('div', { class: 'xsheet' });
  sheet = modal(body, { cls: 'export', label: 'Export video', onClose: () => { sheet = null; } });
  if (job) renderProgress(body); else renderForm(body);
}

function renderForm(body) {
  const [a, b] = trimRange();
  const aspect = aspectKey() || '16:9';
  let name = store('exportName.' + baseName(), `${baseName()}-zoomcut`);

  const row = (label, ...kids) => h('div', { class: 'xrow' }, h('div', { class: 'xl' }, label), h('div', { class: 'xc' }, ...kids));
  const seg = (opts, get, set) => {
    const box = h('div', { class: 'chips' });
    const paint = () => [...box.children].forEach((c, i) => c.classList.toggle('on', opts[i].value === get()));
    opts.forEach(opt => box.append(h('button', { class: 'chip', type: 'button', 'data-tip': opt.tip,
      onclick: () => { set(opt.value); paint(); summary(); } },
      h('span', null, opt.label), opt.sub ? h('small', null, opt.sub) : null)));
    paint();
    return box;
  };

  const res = seg(Object.keys(SIZES).map(k => {
    const [w, hh] = dims(k, aspect);
    return { value: k, label: k === '4k' ? '4K' : k, sub: `${w}×${hh}` };
  }), () => sizeKey(), k => {
    const [w, hh] = dims(k, aspect);
    if (w !== S.doc.output.width || hh !== S.doc.output.height) { checkpoint('Output size'); Object.assign(S.doc.output, { width: w, height: hh }); changed('output'); }
  });
  const fps = seg([{ value: 60, label: '60 fps', tip: 'Smoothest camera moves' }, { value: 30, label: '30 fps', tip: 'Half the frames, smaller file' }],
    () => +S.doc.output.fps, v => { if (+S.doc.output.fps !== v) { checkpoint('Frame rate'); S.doc.output.fps = v; changed('output'); } });
  const qual = seg(Object.entries(QUALITY).map(([k, q]) => ({ value: k, label: q.label, tip: q.tip })),
    qualityKey, k => { checkpoint('Quality'); Object.assign(S.doc.output, { crf: QUALITY[k].crf, preset: QUALITY[k].preset }); changed('output'); });

  const nameIn = h('input', { type: 'text', class: 'xname', value: name, spellcheck: 'false', 'aria-label': 'File name' });
  nameIn.addEventListener('input', () => { name = nameIn.value; });
  const info = h('div', { class: 'xsum' });
  const summary = () => {
    const out = S.doc.output;
    const frames = Math.round((b - a) * out.fps);
    info.replaceChildren(
      h('span', null, icon('clock'), fmt(b - a, 1)),
      h('span', null, icon('frame'), `${out.width}×${out.height}`),
      h('span', null, icon('film'), `${frames.toLocaleString()} frames`),
      h('span', { class: 'xdir', 'data-tip': S.sys?.outDir || '' }, icon('folder'), shortDir(S.sys?.outDir)));
  };
  summary();

  const go = preview => async () => {
    const clean = (nameIn.value || '').trim().replace(/\.mp4$/i, '') || `${baseName()}-zoomcut`;
    remember('exportName.' + baseName(), clean);
    if (await start(preview, preview ? `${baseName()}-preview` : clean) && sheet?.open) renderProgress(body);
  };

  body.replaceChildren(
    h('header', { class: 'xhead' }, h('div', null, h('h2', null, 'Export video'), h('p', null, 'An h.264 MP4 that plays anywhere.')),
      h('button', { class: 'ib', type: 'button', 'aria-label': 'Close', onclick: () => sheet?.close() }, icon('x'))),
    row('Resolution', res),
    row('Frame rate', fps),
    row('Quality', qual),
    row('File name', h('div', { class: 'xnamebox' }, nameIn, h('span', null, '.mp4'))),
    info,
    S.project.sourceInfo?.has_audio
      ? h('p', { class: 'note dim xaudio' }, icon('info'), 'This recording has sound. Zoomcut exports the picture only — the video will be silent.')
      : null,
    h('footer', { class: 'xfoot' },
      h('button', { class: 'btn', type: 'button', 'data-tip': 'A fast 1280-wide draft to check the edit', onclick: go(true) }, icon('play'), 'Quick draft'),
      h('button', { class: 'btn primary', type: 'button', autofocus: true, onclick: go(false) }, icon('export'), 'Export')));
}

const shortDir = d => {
  if (!d) return 'your Zoomcut folder';
  const parts = d.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join('/');
};

async function start(preview, name) {
  try {
    await flush();
    const r = await api('/api/render', { preview, name: name + '.mp4' });
    job = { preview, started: Date.now(), output: r.output, name };
    watch();
    emit('render', { state: 'running', pct: 0 });
    return true;
  } catch (e) {
    toast(e.message, { kind: 'err', ms: 8000 });
    return false;
  }
}

function watch() {
  clearTimeout(poll);
  const tick = async () => {
    let p;
    try { p = await api('/api/progress'); } catch { poll = setTimeout(tick, 1000); return; }
    emit('render', p);
    const btn = $('#btnExport');
    btn.style.setProperty('--p', `${p.pct || 0}%`);
    btn.classList.toggle('rendering', p.state === 'running');
    if (p.state === 'running') { poll = setTimeout(tick, 400); return; }
    const was = job;
    job = null;
    btn.classList.remove('rendering');
    if (p.state === 'done') {
      lastOutput = p.output;
      if (!sheet?.open) {
        toast(`${was?.preview ? 'Draft' : 'Export'} ready — ${p.output.split(/[\\/]/).pop()}`, {
          kind: 'ok', ms: 9000, action: { label: 'Open', run: () => { openExport(); } } });
      }
    } else if (p.state === 'error') {
      toast('Render failed: ' + p.message, { kind: 'err', ms: 12000 });
    }
    if (sheet?.open) {
      const body = sheet.card.querySelector('.xsheet');
      if (p.state === 'done') renderDone(body, p.output, was);
      else renderForm(body);
    }
  };
  tick();
}

function renderProgress(body) {
  const bar = h('i');
  const pctEl = h('b', null, '0%');
  const detail = h('span', null, 'Starting…');
  const cancel = h('button', { class: 'btn', type: 'button', onclick: async () => {
    cancel.disabled = true;
    try { await api('/api/render/cancel', {}); toast('Render cancelled.'); }
    catch (e) { toast(e.message, { kind: 'err' }); cancel.disabled = false; }
  } }, icon('x'), 'Cancel');
  body.replaceChildren(
    h('header', { class: 'xhead' }, h('div', null, h('h2', null, job?.preview ? 'Rendering a draft' : 'Exporting'),
      h('p', null, (job?.name || '') + '.mp4')),
    h('button', { class: 'ib', type: 'button', 'aria-label': 'Hide', 'data-tip': 'Keeps rendering in the background', onclick: () => sheet?.close() }, icon('chevron-down'))),
    h('div', { class: 'xprog' }, h('div', { class: 'xbar' }, bar), h('div', { class: 'xstat' }, pctEl, detail)),
    h('footer', { class: 'xfoot' }, h('span', { class: 'note dim' }, 'You can close this — it keeps going.'), cancel));
  const off = on('render', p => {
    if (!body.isConnected || p.state !== 'running') { off(); return; }
    bar.style.width = `${p.pct || 0}%`;
    pctEl.textContent = `${p.pct || 0}%`;
    const el = p.elapsed ?? (job ? (Date.now() - job.started) / 1000 : 0);
    const left = p.pct > 2 ? el * (100 - p.pct) / p.pct : null;
    detail.textContent = [p.message, left != null ? `about ${fmtDur(left)} left` : null].filter(Boolean).join(' · ');
  });
}

const fmtDur = s => (s < 60 ? `${Math.max(1, Math.round(s))} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);

function renderDone(body, output, was) {
  const file = output.split(/[\\/]/).pop();
  const vid = h('video', { class: 'xvid', src: fileUrl(output) + '&c=' + Date.now(), controls: true, autoplay: true, muted: true, loop: true, playsinline: true });
  body.replaceChildren(
    h('header', { class: 'xhead' }, h('div', null, h('h2', null, icon('check', 'okc'), was?.preview ? 'Draft ready' : 'Export ready'), h('p', null, file)),
      h('button', { class: 'ib', type: 'button', 'aria-label': 'Close', onclick: () => sheet?.close() }, icon('x'))),
    vid,
    h('footer', { class: 'xfoot' },
      h('button', { class: 'btn', type: 'button', onclick: () => api('/api/reveal', { path: output }).catch(e => toast(e.message, { kind: 'err' })) }, icon('folder'), 'Show in folder'),
      h('button', { class: 'btn', type: 'button', onclick: () => copy(output) }, icon('copy'), 'Copy path'),
      h('div', { class: 'grow' }),
      h('button', { class: 'btn', type: 'button', onclick: () => renderForm(body) }, 'Export again'),
      h('button', { class: 'btn primary', type: 'button', onclick: () => sheet?.close() }, 'Done')));
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast('Path copied.', { kind: 'ok', ms: 2000 }); }
  catch { toast(text, { ms: 8000 }); }
}

/** Pick up a render that was already running when the page loaded. */
export function resume(progress) {
  if (progress?.state === 'running' && !job) {
    job = { preview: !!progress.preview, started: Date.now() - (progress.elapsed || 0) * 1000, name: '' };
    watch();
  }
}

export function playFile(path) {
  const name = path.split(/[\\/]/).pop();
  const m = modal(h('div', { class: 'player' },
    h('header', { class: 'xhead' }, h('div', null, h('h2', null, name)),
      h('button', { class: 'ib', type: 'button', 'aria-label': 'Close', onclick: () => m.close() }, icon('x'))),
    h('video', { class: 'xvid', src: fileUrl(path), controls: true, autoplay: true, playsinline: true }),
    h('footer', { class: 'xfoot' },
      h('button', { class: 'btn', type: 'button', onclick: () => api('/api/reveal', { path }).catch(e => toast(e.message, { kind: 'err' })) }, icon('folder'), 'Show in folder'))),
  { cls: 'export wide', label: name });
}
