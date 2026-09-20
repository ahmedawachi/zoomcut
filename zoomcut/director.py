"""Auto-director: turn an Analysis into a camera track.

Rules the camera follows, in priority order:

 1. Open on the whole window, uncropped (zoom == 1.0 exactly).
 2. Every view change ("cut") is revealed wide, and the camera pulls out
    slightly BEFORE the cut lands so the reveal is never half-framed.
 3. Inside a stretch with no cut, pick ONE shot and HOLD it. The camera never
    creeps while nothing is happening - stillness is the default, motion is
    earned.
 4. A shot frames the union of everything that moved during that stretch, plus
    context around it, and never zooms past max_zoom.
 5. Shots shorter than min_shot are merged away, and near-identical
    consecutive shots are merged, so the result is a few deliberate moves
    rather than constant fidgeting.
 6. Close on the whole window again.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import numpy as np

from .analyze import Analysis


@dataclass
class DirectorConfig:
    max_zoom: float = 1.60
    min_zoom: float = 1.22      # below this a move isn't worth making
    context: float = 1.20       # grow the activity box by this much for breathing room
    min_span: float = 0.55      # a shot must show at least this fraction of the frame
    wide_hold: float = 1.10     # how long to stay wide after a cut
    lead: float = 0.35          # pull wide this long before a cut lands
    min_shot: float = 1.00      # shots shorter than this get merged
    end_wide: float = 0.75      # ease back to the full window for the finish
    open_wide: float = 1.00     # hold the full window at the start
    split_after: float = 7.0    # a long quiet stretch may split into two shots
    split_move: float = 0.18    # ...if the action moved at least this far
    energy_floor: float = 0.0008
    coverage_tol: float = 0.08  # how much activity coverage may be traded away
                                # for a cleaner crop edge
    edge_weight: float = 0.30   # how hard to avoid cutting through interface
                                # structure (sidebars, toolbars, text columns)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Shot:
    start: float
    end: float
    zoom: float
    cx: float
    cy: float
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp_center(z: float, cx: float, cy: float) -> tuple[float, float]:
    half = 0.5 / max(z, 1e-6)
    return (min(max(cx, half), 1.0 - half), min(max(cy, half), 1.0 - half))


def _place(marginal, half: float, cfg: DirectorConfig, structure=None) -> float:
    """Where to put a crop of half-width `half` on one axis.

    Scores every possible position on three things:
      * how much of the activity it captures (what we came for),
      * whether its two edges land on quiet parts of the interface rather than
        slicing through a sidebar, toolbar or column of text,
      * how centred it is, as a gentle tie-breaker.

    Edge-snapping falls out of this: when the action hugs a border, the window
    flush against it both covers everything and has one edge on the frame
    boundary, which costs nothing.
    """
    n = len(marginal)
    w = int(round(half * 2.0 * n))
    w = max(1, min(n, w))
    if w >= n:
        return 0.5
    pre = np.concatenate([[0.0], np.cumsum(marginal.astype(np.float64))])
    cover = pre[w:] - pre[:n - w + 1]                    # coverage per start
    best = float(cover.max())
    if best <= 0:
        return 0.5
    starts = np.arange(len(cover))
    score = cover / best
    if structure is not None and cfg.edge_weight > 0:
        st = np.asarray(structure, dtype=np.float64)
        if len(st) == n and float(st.max()) > 0:
            st = st / float(st.max())
            # a crop edge at the frame boundary cuts nothing, so it costs zero
            left = np.where(starts == 0, 0.0, st[starts])
            ends = starts + w
            right = np.where(ends >= n, 0.0, st[np.minimum(ends, n - 1)])
            score = score - cfg.edge_weight * (left + right) / 2.0
    centres = (starts + w / 2.0) / n
    score = score - 0.05 * np.abs(centres - 0.5)
    # only consider placements that stay near the best coverage available
    eligible = cover >= best * (1.0 - cfg.coverage_tol)
    score = np.where(eligible, score, -np.inf)
    return float(centres[int(np.argmax(score))])


def _frame_shot(box: tuple[float, float, float, float], cfg: DirectorConfig,
                heat=None, sx=None, sy=None) -> tuple[float, float, float]:
    """Fit a normalised activity box into a shot -> (zoom, cx, cy)."""
    x0, y0, x1, y1 = box
    bw = max((x1 - x0) * cfg.context, cfg.min_span)
    bh = max((y1 - y0) * cfg.context, cfg.min_span)
    z = min(1.0 / max(bw, 1e-6), 1.0 / max(bh, 1e-6), cfg.max_zoom)
    z = max(z, 1.0)
    half = 0.5 / z
    if heat is not None:
        cx = _place(np.asarray(heat).sum(axis=0), half, cfg, sx)
        cy = _place(np.asarray(heat).sum(axis=1), half, cfg, sy)
    else:
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return (z, *_clamp_center(z, cx, cy))


def _wmedian(vals: np.ndarray, w: np.ndarray) -> float:
    o = np.argsort(vals)
    v, ww = vals[o], w[o]
    c = np.cumsum(ww)
    if c[-1] <= 0:
        return float(np.mean(vals)) if len(vals) else 0.0
    return float(v[int(np.searchsorted(c, c[-1] * 0.5))])


def _heat_box(heat, cfg: DirectorConfig):
    """Robust box around where the action actually is.

    A screen recording often has a second, unrelated thing moving (a toast, a
    clock, a spinner in the corner). A union of per-frame boxes would stretch
    to cover both and end up framing nothing. Instead we take the heat-weighted
    median as the centre and a weighted MAD as the spread, which locks onto the
    dominant blob and barely notices the other one.
    """
    gh, gw = heat.shape
    ys, xs = np.mgrid[0:gh, 0:gw]
    w = heat.reshape(-1).astype(np.float64)
    if w.sum() <= 0:
        return None
    x = xs.reshape(-1).astype(np.float64) + 0.5
    y = ys.reshape(-1).astype(np.float64) + 0.5
    mx, my = _wmedian(x, w), _wmedian(y, w)
    madx = _wmedian(np.abs(x - mx), w)
    mady = _wmedian(np.abs(y - my), w)
    hx = max(madx * 2.2, 0.5)
    hy = max(mady * 2.2, 0.5)
    return ((mx - hx) / gw, (my - hy) / gh, (mx + hx) / gw, (my + hy) / gh)


def _activity_box(a: Analysis, t0: float, t1: float, cfg: DirectorConfig):
    """Where the action is in [t0, t1) -> (box, heat), or (None, None)."""
    heat = a.heat(t0, t1)
    if heat is None:
        return None, None
    t = np.asarray(a.times)
    en = np.asarray(a.energy)
    if not ((t >= t0) & (t < t1) & (en > cfg.energy_floor)).any():
        return None, None
    box = _heat_box(heat, cfg)
    if box is None:
        return None, None
    return box, heat


def _centroid(a: Analysis, t0: float, t1: float, cfg: DirectorConfig):
    box, _ = _activity_box(a, t0, t1, cfg)
    if box is None:
        return None
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def plan(a: Analysis, cfg: DirectorConfig | None = None) -> list[Shot]:
    cfg = cfg or DirectorConfig()
    dur = float(a.duration)
    cuts = [c for c in a.cuts if cfg.lead < c < dur - 0.3]

    # --- spans between cuts -------------------------------------------------
    edges = [0.0] + cuts + [dur]
    shots: list[Shot] = []

    for i in range(len(edges) - 1):
        s, e = edges[i], edges[i + 1]
        is_cut_start = i > 0
        # reveal the change wide (and, for a cut, we already pulled out early)
        want = cfg.wide_hold if is_cut_start else cfg.open_wide
        wide_end = min(s + min(want, max(0.45, (e - s) * 0.5)), e)
        if wide_end > s + 0.05:
            shots.append(Shot(s, wide_end, 1.0, 0.5, 0.5,
                              "reveal" if is_cut_start else "open"))
        rest_s, rest_e = wide_end, e
        if rest_e - rest_s < 0.05:
            continue

        pieces = [(rest_s, rest_e)]
        if rest_e - rest_s > cfg.split_after:
            mid = (rest_s + rest_e) / 2.0
            c1 = _centroid(a, rest_s, mid, cfg)
            c2 = _centroid(a, mid, rest_e, cfg)
            if c1 and c2 and (abs(c1[0] - c2[0]) + abs(c1[1] - c2[1])) > cfg.split_move:
                pieces = [(rest_s, mid), (mid, rest_e)]

        for ps, pe in pieces:
            box, heat = _activity_box(a, ps, pe, cfg)
            if box is None and is_cut_start:
                # nothing moved after the cut - frame what the cut revealed
                box, heat = _activity_box(a, s, min(s + 0.6, e), cfg)
            if box is None:
                shots.append(Shot(ps, pe, 1.0, 0.5, 0.5, "idle-wide"))
                continue
            z, cx, cy = _frame_shot(box, cfg, heat, a.struct_x, a.struct_y)
            if z < cfg.min_zoom:
                shots.append(Shot(ps, pe, 1.0, 0.5, 0.5, "idle-wide"))
            else:
                shots.append(Shot(ps, pe, z, cx, cy, "focus"))

    shots = _merge(shots, cfg)
    shots = _apply_lead(shots, cuts, cfg)
    shots = _close_wide(shots, dur, cfg)
    return _merge(shots, cfg)


STRUCTURAL = {"open", "reveal", "close"}


def _similar(a: Shot, b: Shot) -> bool:
    """Would these two shots look like the same shot?

    Structural shots (open / reveal / close) never fuse: each one marks a real
    moment in the edit, and two identical wide shots back to back are harmless
    anyway - the camera target is flat across both, so nothing moves.
    """
    if a.reason in STRUCTURAL or b.reason in STRUCTURAL:
        return False
    return (abs(a.zoom - b.zoom) < 0.12
            and abs(a.cx - b.cx) < 0.05 and abs(a.cy - b.cy) < 0.05)


def _merge(shots: list[Shot], cfg: DirectorConfig) -> list[Shot]:
    """Drop runt shots and fuse neighbours that would look identical."""
    if not shots:
        return shots
    out = [shots[0]]
    for sh in shots[1:]:
        prev = out[-1]
        if _similar(prev, sh):
            prev.end = sh.end
            continue
        if sh.end - sh.start < cfg.min_shot and sh.reason not in STRUCTURAL:
            # too brief to register as its own move - give the time to whoever
            # can use it, preferring to extend the shot already on screen
            prev.end = sh.end
            continue
        out.append(sh)
    # a runt at the head can survive the loop above
    while len(out) > 1 and out[0].end - out[0].start < min(cfg.min_shot, cfg.open_wide):
        out[1].start = out[0].start
        out.pop(0)
    return out


def _apply_lead(shots: list[Shot], cuts: list[float], cfg: DirectorConfig) -> list[Shot]:
    """Start a post-cut wide shot slightly before the cut actually lands."""
    for i, sh in enumerate(shots):
        if i == 0 or sh.reason != "reveal":
            continue
        if not any(abs(sh.start - c) < 0.25 for c in cuts):
            continue
        prev = shots[i - 1]
        new_start = max(prev.start + cfg.min_shot, sh.start - cfg.lead)
        if new_start < sh.start:
            prev.end = new_start
            sh.start = new_start
    return [s for s in shots if s.end - s.start > 0.05]


def _close_wide(shots: list[Shot], dur: float, cfg: DirectorConfig) -> list[Shot]:
    """End on the whole window so the last thing the viewer sees is complete."""
    if not shots:
        return shots
    last = shots[-1]
    last.end = dur
    if last.zoom <= 1.001:
        last.reason = "close"
        return shots
    t = dur - cfg.end_wide
    if t - last.start < cfg.min_shot:
        # no room for both the zoom and a pull-out: the pull-out wins, so the
        # clip never ends mid-crop
        last.zoom, last.cx, last.cy, last.reason = 1.0, 0.5, 0.5, "close"
        return shots
    last.end = t
    shots.append(Shot(t, dur, 1.0, 0.5, 0.5, "close"))
    return shots


def keyframes(shots: list[Shot]) -> list[list[float]]:
    """Shots -> (t, zoom, cx, cy) targets. Two keys per shot keeps the target
    flat for the shot's whole duration, so the camera holds perfectly still."""
    keys: list[list[float]] = []
    for sh in shots:
        z = round(max(sh.zoom, 1.0), 4)
        # re-clamp AFTER rounding: 0.2857142 -> 0.2857 would sit just outside
        # round FIRST, clamp SECOND - clamping after rounding would be undone
        cx, cy = _clamp_center(z, round(sh.cx, 6), round(sh.cy, 6))
        keys.append([round(sh.start, 3), z, cx, cy])
        keys.append([round(sh.end, 3), z, cx, cy])
    return keys
