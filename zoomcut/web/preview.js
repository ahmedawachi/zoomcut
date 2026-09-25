// The live preview: the recording composited the way render.py composites
// it, with the camera spring run in the browser so playback shows the real
// moves. Until the server has a browser-friendly proxy of the recording, it
// falls back to frames rendered by the server itself.
import { S, on, emit, trimRange, changed, checkpoint, dropCheckpoint, selected, flush } from './store.js';
import { cameraAt, cropRect, clampCenter, MAX_ZOOM } from './camera.js';
import { api, fileUrl, wallpaperUrl } from './api.js';
import { $, clamp, drag, toast, store, remember } from './ui.js';

const E = {};
let L = null;               // the frame's layout, in CSS px
let ready = false;          // the proxy is loaded and seekable
let loop = store('loop', true);
let rate = 1;
let exactToken = 0, exactShown = false, stillTimer = 0;
let docVersion = 0, stillKey = '', stillBusy = false, missing = false;
let mediaKnown = false;      // no still until the server has said whether the recording is there
let mediaTimer = 0, mediaGen = 0;

export const isReady = () => ready;
export const layoutInfo = () => L;

export function initPreview() {
  for (const id of ['stageWrap', 'frame', 'bgFill', 'bgImg', 'bgGrad', 'bgDim', 'win', 'vid',
                    'winEdge', 'exact', 'badgeExact', 'badgeTrim', 'reframeHint', 'stageStatus',
                    'tCur', 'tDur', 'tpPlay', 'tpLoop', 'tpSpeed']) E[id] = $('#' + id);

  // next frame, not inside the callback: laying out resizes what is observed
  new ResizeObserver(() => requestAnimationFrame(layout)).observe(E.stageWrap);
  on('project', pj => { resetVideo(); if (pj) { layout(); paintBackground(); watchMedia(); } });
  on('doc', f => {
    docVersion++;
    if (f.has('style') || f.has('output')) { layout(); paintBackground(); }
    if (!ready) wantStill(); else if (exactShown) hideExact();
    drawCam();
  });
  on('camera', drawCam);
  on('time', () => { drawCam(); drawTime(); });
  on('sel', drawCam);
  on('play', drawTime);

  E.vid.addEventListener('waiting', () => E.frame.classList.add('buffering'));
  E.vid.addEventListener('playing', () => E.frame.classList.remove('buffering'));
  E.vid.addEventListener('seeked', () => { E.frame.classList.remove('buffering'); emit('frame'); });
  E.vid.addEventListener('pause', () => { if (S.playing && !E.vid.ended) { S.playing = false; emit('play'); } });

  E.tpLoop.classList.toggle('on', loop);
  E.tpLoop.onclick = () => setLoop(!loop);
  E.tpSpeed.onclick = () => {
    const rates = [0.5, 1, 1.5, 2];
    rate = rates[(rates.indexOf(rate) + 1) % rates.length];
    E.vid.playbackRate = rate;
    E.tpSpeed.textContent = rate + '×';
  };
  reframing();
}

export function setLoop(v) {
  loop = v; remember('loop', v);
  E.tpLoop.classList.toggle('on', v);
  E.tpLoop.setAttribute('aria-pressed', String(v));
}

// ---------------------------------------------------------------- layout
/** render.Stage's geometry, in output pixels, then scaled to the screen. */
function layout() {
  if (!S.doc) return;
  const ow = +S.doc.output.width || 2560, oh = +S.doc.output.height || 1440;
  const box = E.stageWrap.getBoundingClientRect();
  const gut = box.width < 640 ? 14 : 30;
  const s = Math.max(0.01, Math.min((box.width - gut * 2) / ow, (box.height - gut * 2) / oh));
  const W = Math.round(ow * s), H = Math.round(oh * s);
  E.frame.style.width = W + 'px';
  E.frame.style.height = H + 'px';

  const st = S.doc.style || {};
  const margin = (+st.paddingRatio || 0) * Math.min(ow, oh);
  const sc = Math.min((ow - 2 * margin) / S.sw, (oh - 2 * margin) / S.sh);
  let cw = Math.round(S.sw * sc), ch = Math.round(S.sh * sc);
  cw = Math.max(2, cw - (cw % 2));
  ch = Math.max(2, ch - (ch % 2));
  const x0 = Math.floor((ow - cw) / 2), y0 = Math.floor((oh - ch) / 2);
  const radius = Math.max(0, (+st.cornerRadius || 0) * cw) * s;

  Object.assign(E.win.style, {
    left: x0 * s + 'px', top: y0 * s + 'px', width: cw * s + 'px', height: ch * s + 'px',
    borderRadius: radius + 'px',
  });
  E.winEdge.style.borderRadius = radius + 'px';
  E.vid.style.width = cw * s + 'px';
  E.vid.style.height = ch * s + 'px';

  // the shadow is a blurred copy of the window's shape, offset downwards;
  // CSS blur radii are two standard deviations, Pillow's are one
  const sh = st.shadow || {};
  const alpha = +(sh.alpha ?? 0.52);
  const off = Math.round((+(sh.distance ?? 0.0271)) * oh);
  const sig = Math.max(0.1, (+(sh.blur ?? 0.0215)) * oh);
  const col = (sh.color || [12, 10, 34]).join(',');
  E.win.style.boxShadow = alpha > 0 ? `0 ${off * s}px ${2 * sig * s}px rgba(${col},${alpha})` : 'none';
  const hi = +(st.edgeHighlight ?? 0.13);
  E.winEdge.style.boxShadow = hi > 0 && radius >= 0.5 ? `inset 0 0 0 1px rgba(255,255,255,${hi})` : 'none';

  L = { s, W, H, ow, oh, cw: cw * s, ch: ch * s, x0: x0 * s, y0: y0 * s };
  const b = st.background || {};
  applyBlur(b);
  drawCam();
}

function applyBlur(b) {
  const px = (+b.blur || 0) * (L?.s || 1);
  E.bgImg.style.filter = px > 0 ? `blur(${px}px)` : '';
  E.bgImg.style.inset = px > 0 ? `${-3 * px}px` : '0';
}

// ---------------------------------------------------------------- background
let bgKey = '';
function paintBackground() {
  if (!S.doc || !L) return;
  const b = S.doc.style.background || {};
  let kind = b.type || 'wallpaper';
  let url = null;
  if (kind === 'wallpaper') {
    const name = b.name || S.wallDefault;
    if (name) {
      // ask for the wallpaper at the output's own aspect ratio, so the
      // server's centre crop matches the one render.py will make
      const long = 1600, r = L.ow / L.oh;
      const w = r >= 1 ? long : Math.round(long * r / 8) * 8;
      const hh = r >= 1 ? Math.round(long / r / 8) * 8 : long;
      url = wallpaperUrl(name, w, hh);
      E.bgImg.dataset.small = wallpaperUrl(name);
    } else kind = 'gradient';
  } else if (kind === 'image') {
    if (b.path) url = fileUrl(b.path); else kind = 'gradient';
  }
  E.bgImg.hidden = !url;
  E.bgGrad.hidden = kind !== 'gradient';
  E.bgFill.style.background = kind === 'color' ? (b.color || '#3F37C9') : '#000';
  if (url && bgKey !== url) {
    bgKey = url;
    const small = kind === 'wallpaper' ? E.bgImg.dataset.small : null;
    if (small) E.bgImg.style.backgroundImage = `url("${small}")`;
    const img = new Image();
    img.onload = () => { if (bgKey === url) E.bgImg.style.backgroundImage = `url("${url}")`; };
    img.src = url;
  }
  if (kind === 'gradient') drawGradient(b.gradient || {});
  E.bgDim.style.opacity = String(clamp(+b.dim || 0, 0, 1));
  applyBlur(kind === 'wallpaper' || kind === 'image' ? b : {});
}

/** render._gradient: t = ((p - p0) . (p1 - p0)) / |p1 - p0|^2 in normalised
 *  coordinates. That is linear in pixels too, so a canvas gradient can
 *  reproduce it exactly once the direction is mapped into pixel space. */
function drawGradient(g) {
  const cw = 640, ch = Math.max(2, Math.round(640 * L.oh / L.ow));
  const cv = E.bgGrad;
  if (cv.width !== cw || cv.height !== ch) { cv.width = cw; cv.height = ch; }
  const ctx = cv.getContext('2d');
  const p0 = g.p0 || [0, 0], p1 = g.p1 || [1, 1];
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  const den = dx * dx + dy * dy || 1;
  const gx = dx / (den * cw), gy = dy / (den * ch);
  const g2 = gx * gx + gy * gy || 1e-12;
  const ax = p0[0] * cw, ay = p0[1] * ch;
  const lg = ctx.createLinearGradient(ax, ay, ax + gx / g2, ay + gy / g2);
  lg.addColorStop(0, g.from || '#3F37C9');
  lg.addColorStop(1, g.to || '#8C87DF');
  ctx.fillStyle = lg;
  ctx.fillRect(0, 0, cw, ch);
}

// ---------------------------------------------------------------- camera
export function cameraNow() {
  if (S.directCam) return S.directCam;
  return S.sim ? cameraAt(S.sim, S.t) : [1, 0.5, 0.5];
}

function drawCam() {
  if (!L || !S.sim) return;
  const [z, cx, cy] = cameraNow();
  const r = cropRect(z, cx, cy);
  E.vid.style.transform = `translate(${-r.x0 * L.cw * r.z}px, ${-r.y0 * L.ch * r.z}px) scale(${r.z})`;
  const [a, b] = trimRange();
  E.badgeTrim.hidden = !(S.t < a - 1e-3 || S.t > b + 1e-3);
  const seg = selected();
  const inside = seg && S.t >= seg.start - 1e-3 && S.t <= seg.end + 1e-3;
  E.frame.classList.toggle('reframe', !!(inside && !S.playing));
  E.reframeHint.hidden = !(inside && !S.playing);
}

function drawTime() {
  E.tCur.textContent = fmtT(S.t);
  E.tDur.textContent = fmtT(S.dur);
  E.tpPlay.classList.toggle('on', S.playing);
  E.tpPlay.querySelector('use').setAttribute('href', S.playing ? '#i-pause' : '#i-play');
  E.tpPlay.dataset.tip = S.playing ? 'Pause' : 'Play';
}
const fmtT = t => {
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(2).padStart(5, '0')}`;
};

// ---------------------------------------------------------------- media
function resetVideo() {
  ready = false;
  missing = false;
  mediaKnown = false;
  stillKey = '';
  clearTimeout(mediaTimer);
  mediaGen++;
  E.vid.onerror = null;
  E.vid.pause();
  E.vid.removeAttribute('src');
  delete E.vid.dataset.src;
  E.vid.load();
  E.frame.classList.remove('live');
  hideExact(true);
  bgKey = '';
}

/** Poll the server's media job until the proxy and filmstrip exist. */
export function watchMedia() {
  clearTimeout(mediaTimer);
  const gen = ++mediaGen;
  const tick = async () => {
    if (gen !== mediaGen || !S.project) return;
    let m;
    try { m = await api('/api/media'); }
    catch (e) { m = { state: 'error', message: e.status === 404 ? 'this server has no live preview' : e.message }; }
    if (gen !== mediaGen || !S.project) return;
    if (m.source && m.source !== S.project.source) { mediaTimer = setTimeout(tick, 500); return; }
    S.media = m;
    emit('media', m);
    missing = m.state === 'error' && /not there/.test(m.message || '');
    mediaKnown = true;
    status(m);
    if (m.state === 'ready' && m.proxy) setProxy(m.proxy);
    else if (m.state === 'error') wantStill();
    else { wantStill(); mediaTimer = setTimeout(tick, 600); }
  };
  tick();
}

function status(m) {
  const el = E.stageStatus;
  if (ready || !m || m.state === 'ready') { el.hidden = true; return; }
  el.hidden = false;
  if (missing) {
    el.className = 'stage-status warn';
    el.textContent = 'The recording has been moved or deleted — nothing to show';
    el.title = S.project?.source || '';
  } else if (m.state === 'error') {
    el.className = 'stage-status warn';
    el.textContent = 'Live preview unavailable — showing rendered frames';
    el.title = m.message || '';
  } else {
    el.className = 'stage-status';
    el.replaceChildren(Object.assign(document.createElement('i'), { className: 'spin' }),
      `Preparing live preview${m.pct ? ` · ${m.pct}%` : '…'}`);
  }
}

function setProxy(path) {
  const url = fileUrl(path);
  if (E.vid.dataset.src === url) return;
  E.vid.dataset.src = url;
  E.vid.src = url;
  E.vid.load();
  E.vid.addEventListener('loadeddata', () => {
    ready = true;
    try { E.vid.currentTime = S.t; } catch { /* not seekable yet */ }
    E.frame.classList.add('live');
    hideExact(true);
    status(null);
    emit('ready');
  }, { once: true });
  E.vid.onerror = () => {
    ready = false;
    E.frame.classList.remove('live');
    status({ state: 'error', message: 'the browser could not play the preview file' });
    wantStill();
  };
}

// ---------------------------------------------------------------- stills
/** The fallback while there is no live preview: a server-rendered frame,
 *  asked for only when the time or the edit changed, and one at a time -
 *  a 4K frame can take seconds, and a queue of them starves the proxy. */
function wantStill() {
  if (ready || !S.project || missing || !mediaKnown) return;
  clearTimeout(stillTimer);
  stillTimer = setTimeout(async () => {
    const key = `${S.t.toFixed(3)}|${docVersion}`;
    if (key === stillKey || stillBusy) return;
    stillKey = key;
    stillBusy = true;
    try { await showExact(false); } finally { stillBusy = false; }
    if (`${S.t.toFixed(3)}|${docVersion}` !== stillKey) wantStill();
  }, 180);
}

/** A frame rendered by the server - pixel for pixel what the export holds. */
export async function showExact(badge = true) {
  if (!S.project || !L) return;
  const token = ++exactToken;
  const t = S.t;
  try {
    await flush();
    const width = Math.min(1920, Math.round(L.W * Math.min(2, devicePixelRatio || 1) / 2) * 2);
    const r = await api('/api/still', { t, width });
    if (token !== exactToken) return;
    await new Promise(done => {
      const img = new Image();
      img.onload = () => {
        if (token === exactToken) {
          E.exact.src = img.src;
          E.exact.hidden = false;
          E.badgeExact.hidden = !badge;
          exactShown = true;
        }
        done();
      };
      img.onerror = done;
      img.src = fileUrl(r.path) + '&c=' + Date.now();
    });
  } catch (e) {
    if (token === exactToken) toast(e.message, { kind: 'err' });
  }
}

function hideExact(force = false) {
  if (!force && !ready) return;
  exactToken++;
  E.exact.hidden = true;
  E.badgeExact.hidden = true;
  exactShown = false;
}

// ---------------------------------------------------------------- playback
export function seek(t) {
  S.t = clamp(+t || 0, 0, S.dur);
  if (ready) {
    try { E.vid.currentTime = S.t; } catch { /* not seekable yet */ }
    if (exactShown) hideExact();
  } else wantStill();
  emit('time');
}

export function play() {
  if (!S.project) return;
  if (!ready) { toast('The live preview is still being prepared — one moment.'); return; }
  const [a, b] = trimRange();
  if (S.t < a - 1e-3 || S.t >= b - 0.03) seek(a);
  hideExact();
  S.directCam = null;
  E.vid.playbackRate = rate;
  E.vid.play()?.catch(() => {});
  S.playing = true;
  emit('play');
  requestAnimationFrame(tick);
}

export function pause() {
  if (!S.playing) return;
  S.playing = false;
  E.vid.pause();
  S.t = clamp(E.vid.currentTime, 0, S.dur);
  emit('play');
  emit('time');
}
export const togglePlay = () => (S.playing ? pause() : play());

function tick() {
  if (!S.playing) return;
  const [a, b] = trimRange();
  let t = E.vid.currentTime;
  if (t >= b - 1e-3 || E.vid.ended) {
    if (loop) {
      E.vid.currentTime = t = a;
      if (E.vid.paused) E.vid.play()?.catch(() => {});
    } else {
      S.playing = false;
      E.vid.pause();
      S.t = b;
      try { E.vid.currentTime = b; } catch { /* ignore */ }
      emit('play'); emit('time');
      return;
    }
  }
  S.t = t;
  emit('time');
  requestAnimationFrame(tick);
}

export function step(frames) {
  pause();
  const fps = +S.doc?.output.fps || 60;
  seek(Math.round((S.t + frames / fps) * fps) / fps);
}

export function fullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else E.stageWrap.requestFullscreen?.().catch(() => {});
}

/** Draw the current video frame, for the zoom inspector's framing picker. */
export function grabFrame(canvas) {
  if (!ready || E.vid.readyState < 2) return false;
  const ctx = canvas.getContext('2d');
  try { ctx.drawImage(E.vid, 0, 0, canvas.width, canvas.height); return true; }
  catch { return false; }
}

// ---------------------------------------------------------------- reframing
/** With a zoom selected and the playhead inside it: drag the picture to move
 *  the camera, scroll to change how far in it goes. Double-click anywhere
 *  to zoom to that point. */
function reframing() {
  const inside = () => {
    const seg = selected();
    return seg && !S.playing && S.t >= seg.start - 1e-3 && S.t <= seg.end + 1e-3 ? seg : null;
  };

  E.frame.addEventListener('pointerdown', e => {
    const seg = inside();
    if (!seg || e.button !== 0) return;
    e.preventDefault();
    checkpoint('Reframe zoom');
    const sx = e.clientX, sy = e.clientY, cx0 = seg.cx, cy0 = seg.cy;
    let [cx, cy] = clampCenter(seg.zoom, cx0, cy0);
    E.frame.classList.add('grabbing');
    drag(e, {
      move: ev => {
        const z = seg.zoom;
        [cx, cy] = clampCenter(z, cx0 - (ev.clientX - sx) / (L.cw * z), cy0 - (ev.clientY - sy) / (L.ch * z));
        seg.cx = cx; seg.cy = cy;
        S.directCam = [z, cx, cy];
        changed('segs');
      },
      up: () => {
        E.frame.classList.remove('grabbing');
        S.directCam = null;
        dropCheckpoint();
        drawCam();
      },
    }, E.frame);
  });

  let wheelEnd = 0;
  E.frame.addEventListener('wheel', e => {
    const seg = inside();
    if (!seg) return;
    e.preventDefault();
    checkpoint('Zoom level', true);
    const z = clamp(seg.zoom * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)), 1.05, MAX_ZOOM);
    seg.zoom = Math.round(z * 100) / 100;
    [seg.cx, seg.cy] = clampCenter(seg.zoom, seg.cx, seg.cy);
    S.directCam = [seg.zoom, seg.cx, seg.cy];
    changed('segs');
    clearTimeout(wheelEnd);
    wheelEnd = setTimeout(() => { S.directCam = null; drawCam(); }, 350);
  }, { passive: false });

  E.frame.addEventListener('dblclick', e => {
    if (!L || !S.project) return;
    const rect = E.win.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width, py = (e.clientY - rect.top) / rect.height;
    if (px < 0 || px > 1 || py < 0 || py > 1) return;
    const [z, ccx, ccy] = cameraNow();
    const r = cropRect(z, ccx, ccy);
    emit('focus-point', { cx: r.x0 + px * r.w, cy: r.y0 + py * r.w, seg: inside() });
  });
}
