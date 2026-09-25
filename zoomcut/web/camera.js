// The camera, ported from director.py and render.py so the browser preview
// moves exactly the way the export will. Keep these in step with the Python:
// if a render and the preview ever disagree, the preview is the one that lies.

export const DEFAULT_SPRING = { mass: 2.25, stiffness: 200, damping: 36 };
export const MIN_SEG = 0.3;          // seconds; a zoom shorter than this never settles
export const MAX_ZOOM = 4;

// Motion presets. zeta stays between 0.8 and 0.95 on all of them, so each
// one settles without a visible wobble - they differ only in how fast.
export const SPRINGS = {
  snappy:   { label: 'Snappy',   mass: 1.8,  stiffness: 380, damping: 44 },
  balanced: { label: 'Balanced', mass: 2.25, stiffness: 200, damping: 36 },
  smooth:   { label: 'Smooth',   mass: 2.25, stiffness: 110, damping: 28 },
  lazy:     { label: 'Lazy',     mass: 2.5,  stiffness: 55,  damping: 22 },
};

// Auto-director presets, as DirectorConfig field names. Every preset sets
// every field it touches, so switching back to Balanced really resets.
export const DIRECTORS = {
  subtle:   { label: 'Subtle',   max_zoom: 1.4, min_zoom: 1.18, context: 1.35, min_shot: 1.2 },
  balanced: { label: 'Balanced', max_zoom: 1.6, min_zoom: 1.22, context: 1.2,  min_shot: 1.0 },
  punchy:   { label: 'Punchy',   max_zoom: 2.2, min_zoom: 1.25, context: 1.08, min_shot: 0.8 },
};

export class Spring {
  constructor(x0, stiffness, mass, damping, sub = 8) {
    this.x = x0; this.v = 0;
    this.k = stiffness; this.m = mass; this.c = damping; this.sub = sub;
  }
  step(target, dt) {
    const h = dt / this.sub;
    for (let i = 0; i < this.sub; i++) {
      this.v += ((this.k * (target - this.x) - this.c * this.v) / this.m) * h;
      this.x += this.v * h;
    }
    return this.x;
  }
}

/** The spring's step response, and when it stays within 2% of its target. */
export function response(p, seconds = 2, hz = 120) {
  const s = new Spring(0, +p.stiffness, +p.mass, +p.damping);
  const t = [], x = [];
  for (let i = 0; i <= seconds * hz; i++) {
    t.push(i / hz);
    x.push(i ? s.step(1, 1 / hz) : 0);
  }
  let settleAt = 0;
  for (let i = x.length - 1; i >= 0; i--) {
    if (Math.abs(x[i] - 1) > 0.02) { settleAt = i + 1 < x.length ? t[i + 1] : null; break; }
  }
  return { t, x, settleAt };
}

export function clampCenter(z, cx, cy) {
  const half = 0.5 / Math.max(z, 1e-6);
  return [Math.min(Math.max(cx, half), 1 - half), Math.min(Math.max(cy, half), 1 - half)];
}

const round = (v, d) => { const p = 10 ** d; return Math.round(v * p) / p; };

/** Shots -> (t, zoom, cx, cy) targets; two keys per shot hold it perfectly still. */
export function keyframes(shots) {
  const keys = [];
  for (const sh of shots) {
    const z = round(Math.max(sh.zoom, 1), 4);
    // round first, clamp second - clamping before rounding would be undone
    const [cx, cy] = clampCenter(z, round(sh.cx, 6), round(sh.cy, 6));
    keys.push([round(sh.start, 3), z, cx, cy], [round(sh.end, 3), z, cx, cy]);
  }
  return keys;
}

/** render.target_at, as a closure that remembers where it is: the
 *  simulation asks in increasing time, so this stays linear overall. */
function targetFn(keys) {
  let i = 0;
  return t => {
    if (!keys.length) return [1, 0.5, 0.5];
    if (t <= keys[0][0]) return [keys[0][1], keys[0][2], keys[0][3]];
    if (i > 0 && keys[i][0] >= t) i = 0;        // time went backwards
    // the first pair with a <= t <= b wins, exactly like the Python loop
    while (i < keys.length - 1 && keys[i + 1][0] < t) i++;
    if (i >= keys.length - 1) { const k = keys[keys.length - 1]; return [k[1], k[2], k[3]]; }
    const a = keys[i], b = keys[i + 1];
    const span = b[0] - a[0];
    const u = span <= 0 ? 0 : (t - a[0]) / span;
    return [a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u, a[3] + (b[3] - a[3]) * u];
  };
}

export const targetAt = (keys, t) => targetFn(keys)(t);

/** Run the camera spring over [t0, t1] at the output frame rate. Frame n is
 *  the state after n+1 steps, which is what render() composites at t0+n/fps. */
export function simulate(keys, spring, fps, t0, t1) {
  fps = Math.max(1, +fps || 60);
  const k = +spring?.stiffness || DEFAULT_SPRING.stiffness;
  const m = +spring?.mass || DEFAULT_SPRING.mass;
  const c = +spring?.damping || DEFAULT_SPRING.damping;
  const n = Math.max(2, Math.ceil(Math.max(0, t1 - t0) * fps) + 2);
  const Z = new Float64Array(n), X = new Float64Array(n), Y = new Float64Array(n);
  const tgt = targetFn(keys);
  const [z0, x0, y0] = tgt(t0);
  const sz = new Spring(Math.log(Math.max(z0, 1e-6)), k, m, c);
  const sx = new Spring(x0, k, m, c), sy = new Spring(y0, k, m, c);
  const dt = 1 / fps;
  for (let i = 0; i < n; i++) {
    const [tz, tx, ty] = tgt(t0 + i / fps);
    Z[i] = Math.exp(sz.step(Math.log(Math.max(tz, 1e-6)), dt));
    X[i] = sx.step(tx, dt);
    Y[i] = sy.step(ty, dt);
  }
  return { t0, fps, n, Z, X, Y };
}

/** Camera state at any time, interpolated between output frames so the
 *  preview stays smooth on a 120 Hz display. */
export function cameraAt(sim, t) {
  const f = (t - sim.t0) * sim.fps;
  if (!(f > 0)) return [sim.Z[0], sim.X[0], sim.Y[0]];
  const i = Math.floor(f);
  if (i >= sim.n - 1) return [sim.Z[sim.n - 1], sim.X[sim.n - 1], sim.Y[sim.n - 1]];
  const u = f - i;
  return [sim.Z[i] + (sim.Z[i + 1] - sim.Z[i]) * u,
          sim.X[i] + (sim.X[i + 1] - sim.X[i]) * u,
          sim.Y[i] + (sim.Y[i + 1] - sim.Y[i]) * u];
}

/** render.crop_box in normalised source coordinates. */
export function cropRect(z, cx, cy) {
  z = Math.max(1, z);
  const w = 1 / z;
  [cx, cy] = clampCenter(z, cx, cy);
  return {
    x0: Math.min(Math.max(cx - w / 2, 0), 1 - w),
    y0: Math.min(Math.max(cy - w / 2, 0), 1 - w),
    w, z,
  };
}

// ---------------------------------------------------------------- segments
// The project stores a tiled shot list (wide shots included) because the
// keys between two shots must never interpolate - a gap would become a slow
// tween. The editor works on the zooms alone and fills the gaps back in.

let seq = 0;
export const uid = () => 'z' + (++seq).toString(36) + Math.random().toString(36).slice(2, 6);

export function segsFromShots(shots) {
  return (shots || [])
    .filter(s => s.zoom > 1.001 && s.end - s.start > 1e-3)
    .map(s => ({ id: uid(), start: +s.start, end: +s.end, zoom: +s.zoom,
                 cx: +s.cx, cy: +s.cy, reason: s.reason || 'focus' }))
    .sort((a, b) => a.start - b.start);
}

export function shotsFromSegs(segs, dur) {
  const out = [];
  const wide = (start, end, reason) => out.push({ start, end, zoom: 1, cx: 0.5, cy: 0.5, reason });
  let at = 0;
  for (const s of [...segs].sort((a, b) => a.start - b.start)) {
    if (s.start > at + 1e-4) wide(at, s.start, at === 0 ? 'open' : 'wide');
    out.push({ start: s.start, end: s.end, zoom: s.zoom, cx: s.cx, cy: s.cy, reason: s.reason || 'manual' });
    at = s.end;
  }
  if (at < dur - 1e-4 || !out.length) wide(at, dur, out.length ? 'close' : 'open');
  return out;
}

/** Where to park the playhead to see a zoom once the spring has settled. */
export const settlePoint = s => s.start + Math.min(0.75, (s.end - s.start) * 0.6);
