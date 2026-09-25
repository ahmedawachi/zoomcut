// Small DOM helpers shared by every part of the editor.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
export const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** h('div', {class: 'x', onclick}, child, 'text', [more]) */
export function h(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(n.style, v);
      else if (k === 'html') n.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const k of kids.flat(Infinity)) {
    if (k != null && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return n;
}

const SVGNS = 'http://www.w3.org/2000/svg';
export function icon(name, cls) {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('class', 'ic' + (cls ? ' ' + cls : ''));
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', '#i-' + name);
  s.append(u);
  return s;
}

export const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** 64.25 -> "1:04.25" */
export function fmt(t, digits = 2) {
  const p = 10 ** digits;
  t = Math.round(Math.max(0, +t || 0) * p) / p;
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(digits).padStart(digits ? digits + 3 : 2, '0')}`;
}

/** "1:04.5", "64.5", "64.5s" -> seconds, or NaN. */
export function parseTime(str) {
  const m = String(str).trim().replace(/s$/i, '').match(/^(?:(\d+):)?(\d+(?:\.\d*)?|\.\d+)$/);
  if (!m) return NaN;
  return (m[1] ? +m[1] * 60 : 0) + +m[2];
}

export function fmtBytes(n) {
  if (!(n >= 0)) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

export function ago(epoch) {
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return new Date(epoch * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** "Mod+S" -> "⌘S" on a Mac, "Ctrl+S" elsewhere. */
export const kbd = s => String(s)
  .replace(/Mod\+/g, MAC ? '⌘' : 'Ctrl+')
  .replace(/Shift\+/g, MAC ? '⇧' : 'Shift+')
  .replace(/Alt\+/g, MAC ? '⌥' : 'Alt+');

/** Follow a pointer until it lets go, with capture so the drag survives
 *  leaving the element. */
export function drag(e, { move, up }, el = e.currentTarget) {
  try { el.setPointerCapture(e.pointerId); } catch { /* element gone */ }
  const mv = ev => move?.(ev);
  const done = ev => {
    el.removeEventListener('pointermove', mv);
    el.removeEventListener('pointerup', done);
    el.removeEventListener('pointercancel', done);
    up?.(ev);
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', done);
  el.addEventListener('pointercancel', done);
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function store(key, fallback) {
  try { const v = localStorage.getItem('zoomcut.' + key); return v == null ? fallback : JSON.parse(v); }
  catch { return fallback; }
}
export function remember(key, value) {
  try { localStorage.setItem('zoomcut.' + key, JSON.stringify(value)); } catch { /* private mode */ }
}

// ---------------------------------------------------------------- toasts
export function toast(msg, { kind = 'info', ms = 3800, action = null } = {}) {
  const box = $('#toasts');
  const ico = { ok: 'check', err: 'alert', info: 'info' }[kind] || 'info';
  const t = h('div', { class: `toast ${kind}`, role: kind === 'err' ? 'alert' : 'status' },
    icon(ico), h('span', { class: 'toast-msg' }, msg));
  let gone = false;
  const close = () => {
    if (gone) return;
    gone = true;
    t.classList.add('out');
    setTimeout(() => t.remove(), 200);
  };
  if (action) {
    t.append(h('button', { class: 'toast-act', onclick: () => { close(); action.run(); } }, action.label));
  }
  t.append(h('button', { class: 'toast-x', 'aria-label': 'Dismiss', onclick: close }, icon('x')));
  box.append(t);
  while (box.children.length > 4) box.firstChild.remove();
  if (ms) setTimeout(close, ms);
  return close;
}

// ---------------------------------------------------------------- tooltips
/** A button that is only an icon is named by its tooltip, so screen readers
 *  hear what everyone else reads on hover - one source, never out of step. */
function autoLabel(root = document) {
  for (const b of root.querySelectorAll('button[data-tip]')) {
    const named = b.hasAttribute('aria-label') && !b.dataset.autoLabel;
    if (named || b.textContent.trim()) continue;
    if (b.getAttribute('aria-label') !== b.dataset.tip) b.setAttribute('aria-label', b.dataset.tip);
    b.dataset.autoLabel = '1';
  }
}

export function tooltips() {
  autoLabel();
  let pending = 0;
  new MutationObserver(() => {
    if (pending) return;
    pending = requestAnimationFrame(() => { pending = 0; autoLabel(); });
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-tip'] });
  const tip = h('div', { class: 'tip', role: 'tooltip' });
  document.body.append(tip);
  let timer = 0, cur = null;
  const hide = () => { clearTimeout(timer); tip.classList.remove('on'); cur = null; };
  document.addEventListener('pointerover', e => {
    const t = e.target.closest?.('[data-tip]');
    if (t === cur) return;
    hide();
    if (!t || e.pointerType === 'touch') return;
    cur = t;
    timer = setTimeout(() => {
      if (!document.contains(t) || !t.dataset.tip) return;
      tip.replaceChildren(h('span', null, t.dataset.tip),
        t.dataset.kbd ? h('kbd', null, kbd(t.dataset.kbd)) : '');
      const r = t.getBoundingClientRect();
      tip.style.left = '0px'; tip.style.top = '0px';
      tip.classList.add('on');
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      const below = r.bottom + th + 10 < innerHeight;
      tip.style.left = clamp(r.left + r.width / 2 - tw / 2, 6, innerWidth - tw - 6) + 'px';
      tip.style.top = (below ? r.bottom + 8 : r.top - th - 8) + 'px';
    }, 450);
  });
  document.addEventListener('pointerdown', hide, true);
  document.addEventListener('keydown', hide, true);
  addEventListener('blur', hide);
}

// ---------------------------------------------------------------- modals
export function modal(content, { cls = '', onClose, label = 'Dialog' } = {}) {
  const back = h('div', { class: 'modal-back' });
  const card = h('div', { class: 'modal ' + cls, role: 'dialog', 'aria-modal': 'true', 'aria-label': label }, content);
  back.append(card);
  $('#modals').append(back);
  const prev = document.activeElement;
  let open = true;
  const close = () => {
    if (!open) return;
    open = false;
    back.classList.add('out');
    document.removeEventListener('keydown', key, true);
    setTimeout(() => back.remove(), 160);
    prev?.focus?.();
    onClose?.();
  };
  const key = e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } };
  back.addEventListener('pointerdown', e => { if (e.target === back) close(); });
  document.addEventListener('keydown', key, true);
  requestAnimationFrame(() => card.querySelector('[autofocus], button.primary, button, input')?.focus());
  return { close, card, get open() { return open; } };
}

export const isTyping = e => {
  const t = e.target;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) &&
    !(t.tagName === 'INPUT' && /^(range|checkbox|radio|button)$/.test(t.type));
};
