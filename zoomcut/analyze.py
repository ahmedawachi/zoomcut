"""Motion analysis: turn a screen recording into a timeline of beats.

The recording is decoded once at a low resolution and a low frame rate. For
every sampled frame we measure which parts of the screen changed. From that we
derive:

  * global_motion - how much the whole screen changed (spikes = view changes)
  * cuts          - timestamps where the screen changed wholesale
  * activity      - per frame: how much changed, and the bounding box of what
                    changed (normalised 0..1 source coordinates)

Everything downstream (the auto-director) works off this and never touches the
pixels again.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import math, subprocess
import numpy as np

from .util import probe, ffmpeg, ZoomcutError

ANALYSIS_FPS = 20
GRID_W = 96
# A cell counts as "changed" when it moves more than this many 8-bit levels.
# Screen text antialiasing and video compression noise sit well below 6.
CELL_FLOOR = 6.0
# ...or more than this fraction of the frame's own peak change, which keeps
# slow, subtle changes (a caret, a spinner) visible without amplifying noise.
CELL_REL = 0.30


@dataclass
class Analysis:
    path: str
    width: int
    height: int
    duration: float
    afps: int
    grid: tuple[int, int]
    times: list[float] = field(default_factory=list)
    global_motion: list[float] = field(default_factory=list)
    energy: list[float] = field(default_factory=list)
    bbox: list[list[float]] = field(default_factory=list)   # x0,y0,x1,y1 normalised
    cuts: list[float] = field(default_factory=list)
    # per-cell change magnitudes, (N, gh, gw) uint8 - they are whole levels
    # 0..255, so 8 bits hold them exactly at a quarter of float32's memory.
    # One row per entry in .times. Kept for the director's heat maps (the
    # server keeps it while you edit); never serialised.
    diffs: object = None
    # how "busy" each column / row of the interface is (mean gradient of the
    # average frame). The director uses this to avoid putting a crop edge
    # through a sidebar, toolbar or column of text.
    struct_x: object = None
    struct_y: object = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("diffs", "struct_x", "struct_y"):
            d.pop(k, None)
        d["grid"] = list(self.grid)
        return d

    def heat(self, t0: float, t1: float) -> "np.ndarray | None":
        """Summed per-cell change over [t0, t1). None if the window is empty."""
        if self.diffs is None:
            return None
        t = np.asarray(self.times)
        m = (t >= t0) & (t < t1)
        if not m.any():
            return None
        # summed in float32, exactly as when diffs were stored as float32
        h = self.diffs[m].sum(axis=0, dtype=np.float32)
        return h if float(h.sum()) > 0 else None


def _decode_gray(path: str, afps: int, gw: int, gh: int) -> np.ndarray:
    cmd = [
        ffmpeg(), "-nostdin", "-v", "error", "-i", path,
        "-vf", f"fps={afps},scale={gw}:{gh}:flags=area,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise ZoomcutError(f"ffmpeg decode failed:\n{p.stderr.decode(errors='replace')[:2000]}")
    buf = p.stdout
    n = len(buf) // (gw * gh)
    if n < 2:
        raise ZoomcutError(
            f"{path}: only {n} frame(s) decoded at {afps}fps - the clip is too short to analyse."
        )
    return np.frombuffer(buf[: n * gw * gh], dtype=np.uint8).reshape(n, gh, gw)


def analyze(path: str, afps: int = ANALYSIS_FPS, grid_w: int = GRID_W) -> Analysis:
    info = probe(path)
    sw, sh = info["width"], info["height"]
    gw = int(grid_w)
    gh = max(8, int(round(gw * sh / sw)))
    frames = _decode_gray(path, afps, gw, gh)                    # uint8

    # |frame - previous| in 8 bits, a chunk at a time: a 10-minute recording
    # used to peak at 1.6 GB here, almost all of it float32 copies of values
    # that are whole numbers 0..255. The arithmetic below that reaches the
    # director stays in float32, so the plan comes out bit-for-bit the same.
    n = len(frames) - 1
    diffs = np.empty((max(n, 0), gh, gw), dtype=np.uint8)            # (N-1, gh, gw)
    for i in range(0, max(n, 0), 2048):
        chunk = frames[i:i + 2049].astype(np.int16)
        diffs[i:i + 2048] = np.abs(np.diff(chunk, axis=0))
    times = [(i + 1) / afps for i in range(n)]

    flat = diffs.reshape(n, -1)
    global_motion = flat.mean(axis=1, dtype=np.float32)

    peak = flat.max(axis=1).astype(np.float32)
    thresh = np.maximum(CELL_FLOOR, peak * CELL_REL)[:, None, None]
    active = diffs >= thresh

    ys, xs = np.mgrid[0:gh, 0:gw]
    energy = np.zeros(n, dtype=np.float32)
    bboxes = np.zeros((n, 4), dtype=np.float32)
    for i in range(n):
        m = active[i]
        cnt = int(m.sum())
        if cnt == 0:
            energy[i] = 0.0
            bboxes[i] = (0.0, 0.0, 1.0, 1.0)
            continue
        w = diffs[i][m].astype(np.float32)
        energy[i] = float(w.mean() * cnt / (gw * gh))
        cx, cy = xs[m].astype(np.float32), ys[m].astype(np.float32)
        # weighted percentiles keep one stray cell from blowing the box open
        order = np.argsort(cx); cxs, wxs = cx[order], w[order]
        cw = np.cumsum(wxs) / max(float(wxs.sum()), 1e-6)
        x0 = float(cxs[np.searchsorted(cw, 0.03)]) if cnt > 3 else float(cx.min())
        x1 = float(cxs[min(np.searchsorted(cw, 0.97), cnt - 1)]) if cnt > 3 else float(cx.max())
        order = np.argsort(cy); cys, wys = cy[order], w[order]
        cw = np.cumsum(wys) / max(float(wys.sum()), 1e-6)
        y0 = float(cys[np.searchsorted(cw, 0.03)]) if cnt > 3 else float(cy.min())
        y1 = float(cys[min(np.searchsorted(cw, 0.97), cnt - 1)]) if cnt > 3 else float(cy.max())
        bboxes[i] = ((x0) / gw, (y0) / gh, (x1 + 1) / gw, (y1 + 1) / gh)

    cuts = _find_cuts(global_motion, times)

    np.multiply(diffs, active, out=diffs)          # keep only the active cells, in place
    masked = diffs
    del active
    mean_frame = frames.mean(axis=0).astype(np.float32)
    gx = np.zeros(gw, dtype=np.float32)
    gx[1:] = np.abs(np.diff(mean_frame, axis=1)).mean(axis=0)
    gy = np.zeros(gh, dtype=np.float32)
    gy[1:] = np.abs(np.diff(mean_frame, axis=0)).mean(axis=1)
    return Analysis(
        diffs=masked, struct_x=gx, struct_y=gy,
        path=path, width=sw, height=sh, duration=info["duration"] or (n + 1) / afps,
        afps=afps, grid=(gw, gh), times=times,
        global_motion=[float(v) for v in global_motion],
        energy=[float(v) for v in energy],
        bbox=[[float(v) for v in b] for b in bboxes],
        cuts=cuts,
    )


def activity_track(a: Analysis, rate: float = 10.0, max_points: int = 1500) -> dict:
    """How busy the screen is over time, for the editor's timeline.

    Each bin keeps the MAX energy of its samples, so a single click still
    shows as a spike instead of being averaged away. Scaled by the 98th
    percentile rather than the peak, so one full-screen change does not
    flatten everything else to nothing.
    """
    dur = float(a.duration or 0.0)
    dt = max(1.0 / rate, dur / max_points)
    n = max(1, int(math.ceil(dur / dt - 1e-9))) if dur > 0 else 0
    v = np.zeros(n, dtype=np.float64)
    if n and a.times:
        t = np.asarray(a.times, dtype=np.float64)
        e = np.asarray(a.energy, dtype=np.float64)
        idx = np.clip((t / dt).astype(np.int64), 0, n - 1)
        np.maximum.at(v, idx, e)
    nz = v[v > 0]
    if nz.size:
        ref = float(np.percentile(nz, 98)) or float(nz.max())
        v = np.clip(v / ref, 0.0, 1.0)
    return {"t0": 0.0, "dt": dt, "v": [round(float(x), 3) for x in v]}


def _find_cuts(gm: np.ndarray, times: list[float]) -> list[float]:
    """A cut is a global-motion spike that stands far above the local baseline."""
    if len(gm) < 3:
        return []
    med = float(np.median(gm))
    mad = float(np.median(np.abs(gm - med))) or 1e-3
    # absolute floor stops a dead-still clip from calling its own noise a cut
    level = max(med + 12.0 * mad, 1.2)
    cuts: list[float] = []
    for i, v in enumerate(gm):
        if v < level:
            continue
        if i and gm[i - 1] >= level:      # keep only the leading edge of a spike
            continue
        if cuts and times[i] - cuts[-1] < 0.35:
            continue
        cuts.append(round(times[i], 3))
    return cuts
