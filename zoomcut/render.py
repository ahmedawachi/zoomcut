"""Compositor: project + recording -> finished video.

The recording is drawn as a rounded, shadowed window floating on a background,
and a spring-driven virtual camera zooms and pans over it. Moves are
integrated through a damped spring (mass/stiffness/damping) rather than an
easing curve, so they settle like physics instead of like a tween.
"""
from __future__ import annotations
import math, os, subprocess
from typing import Callable
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .util import ffmpeg, ZoomcutError
from . import wallpapers

Image.MAX_IMAGE_PIXELS = None
SS = 4  # supersampling used for antialiasing the window mask
# Sub-LSB noise added to the background before encoding. The background never
# moves, so any banding h264 introduces sits on screen for the whole clip;
# this gives the encoder something to hide the contour in. Exposed as a
# constant so the test suite can render an undithered control and compare.
DITHER = 1.2


# --------------------------------------------------------------------------
# camera
# --------------------------------------------------------------------------
class Spring:
    """Damped spring, integrated with fixed substeps for stability."""
    __slots__ = ("x", "v", "k", "m", "c", "sub")

    def __init__(self, x0: float, stiffness: float, mass: float, damping: float, sub: int = 8):
        self.x, self.v = float(x0), 0.0
        self.k, self.m, self.c, self.sub = stiffness, mass, damping, sub

    def step(self, target: float, dt: float) -> float:
        h = dt / self.sub
        for _ in range(self.sub):
            self.v += ((self.k * (target - self.x) - self.c * self.v) / self.m) * h
            self.x += self.v * h
        return self.x


def target_at(keys: list[list[float]], t: float) -> tuple[float, float, float]:
    if not keys:
        return 1.0, 0.5, 0.5
    if t <= keys[0][0]:
        return tuple(keys[0][1:4])
    for a, b in zip(keys, keys[1:]):
        if a[0] <= t <= b[0]:
            span = b[0] - a[0]
            u = 0.0 if span <= 0 else (t - a[0]) / span
            return tuple(a[i] + (b[i] - a[i]) * u for i in (1, 2, 3))
    return tuple(keys[-1][1:4])


def crop_box(z: float, cx: float, cy: float, sw: int, sh: int):
    """Camera state -> a source-pixel box, always inside the frame."""
    z = max(1.0, z)
    half = 0.5 / z
    cx = min(max(cx, half), 1.0 - half)
    cy = min(max(cy, half), 1.0 - half)
    bw, bh = sw / z, sh / z
    x0 = min(max(cx * sw - bw / 2.0, 0.0), sw - bw)
    y0 = min(max(cy * sh - bh / 2.0, 0.0), sh - bh)
    return x0, y0, bw, bh


# --------------------------------------------------------------------------
# background / chrome
# --------------------------------------------------------------------------
def _hex_rgb(s: str) -> tuple[int, int, int]:
    s = str(s).lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ZoomcutError(f"bad colour {s!r}, expected #rrggbb")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _gradient(w: int, h: int, c0, c1, p0, p1) -> np.ndarray:
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    dx, dy = float(p1[0] - p0[0]), float(p1[1] - p0[1])
    den = dx * dx + dy * dy or 1.0
    t = np.clip(((xs - p0[0]) * dx + (ys - p0[1]) * dy) / den, 0, 1).astype(np.float32)
    a = np.array(c0, dtype=np.float32)
    b = np.array(c1, dtype=np.float32)
    return a[None, None, :] + (b - a)[None, None, :] * t[:, :, None]


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    tr, ir = w / h, img.width / img.height
    if ir > tr:
        nw = int(round(img.height * tr))
        img = img.crop(((img.width - nw) // 2, 0, (img.width + nw) // 2, img.height))
    else:
        nh = int(round(img.width / tr))
        img = img.crop((0, (img.height - nh) // 2, img.width, (img.height + nh) // 2))
    return img.resize((w, h), Image.LANCZOS)


def build_background(style: dict, ow: int, oh: int) -> np.ndarray:
    bg_cfg = style.get("background", {})
    kind = bg_cfg.get("type", "wallpaper")
    if kind in ("wallpaper", "image"):
        src = bg_cfg.get("path") if kind == "image" else bg_cfg.get("name")
        if not src and kind == "wallpaper":
            src = wallpapers.default_name()
        if not src:
            kind = "gradient"
        else:
            png = wallpapers.materialise(src)
            img = _cover(Image.open(png).convert("RGB"), ow, oh)
            blur = float(bg_cfg.get("blur") or 0.0)
            if blur > 0:
                img = img.filter(ImageFilter.GaussianBlur(radius=blur))
            bg = np.asarray(img, dtype=np.float32)
    if kind == "gradient":
        g = bg_cfg.get("gradient", {})
        bg = _gradient(ow, oh, _hex_rgb(g.get("from", "#3F37C9")), _hex_rgb(g.get("to", "#8C87DF")),
                       g.get("p0", [0, 0]), g.get("p1", [1, 1]))
    elif kind == "color":
        bg = np.zeros((oh, ow, 3), np.float32) + np.array(_hex_rgb(bg_cfg.get("color", "#3F37C9")), np.float32)

    dim = float(bg_cfg.get("dim") or 0.0)
    if dim > 0:
        bg = bg * (1.0 - dim)
    if DITHER > 0:
        rng = np.random.default_rng(7)
        bg = bg + rng.uniform(-DITHER, DITHER, size=bg.shape).astype(np.float32)
    return bg


def _rounded(w: int, h: int, r: float, inset: float = 0.0) -> np.ndarray:
    im = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(im).rounded_rectangle(
        [inset * SS, inset * SS, w * SS - 1 - inset * SS, h * SS - 1 - inset * SS],
        radius=max(0.0, (r - inset)) * SS, fill=255)
    return np.asarray(im.resize((w, h), Image.LANCZOS), dtype=np.float32) / 255.0


class Stage:
    """Pre-computes everything that does not change frame to frame."""

    def __init__(self, project: dict, ow: int, oh: int, sw: int, sh: int):
        style = project["style"]
        self.ow, self.oh, self.sw, self.sh = ow, oh, sw, sh

        pad = float(style.get("paddingRatio", 0.0702))
        margin = pad * min(ow, oh)
        avail_w, avail_h = ow - 2 * margin, oh - 2 * margin
        if avail_w <= 8 or avail_h <= 8:
            raise ZoomcutError(f"paddingRatio {pad} leaves no room for the window")
        scale = min(avail_w / sw, avail_h / sh)
        cw, ch = int(round(sw * scale)), int(round(sh * scale))
        cw -= cw % 2
        ch -= ch % 2
        self.cw, self.ch = max(2, cw), max(2, ch)
        self.cx0, self.cy0 = (ow - self.cw) // 2, (oh - self.ch) // 2
        radius = max(0.0, float(style.get("cornerRadius", 0.012)) * self.cw)

        bg = build_background(style, ow, oh)

        shcfg = style.get("shadow", {})
        alpha = float(shcfg.get("alpha", 0.52))
        if alpha > 0:
            off = int(round(float(shcfg.get("distance", 0.0271)) * oh))
            blur = max(0.1, float(shcfg.get("blur", 0.0215)) * oh)
            sm = Image.new("L", (ow, oh), 0)
            ImageDraw.Draw(sm).rounded_rectangle(
                [self.cx0, self.cy0 + off, self.cx0 + self.cw - 1, self.cy0 + self.ch - 1 + off],
                radius=radius, fill=255)
            sm = sm.filter(ImageFilter.GaussianBlur(radius=blur))
            sa = (np.asarray(sm, dtype=np.float32) / 255.0 * alpha)[:, :, None]
            col = np.array(shcfg.get("color", [12, 10, 34]), dtype=np.float32)
            bg = bg * (1.0 - sa) + col[None, None, :] * sa

        self.base = np.clip(bg, 0, 255).astype(np.uint8)
        self.canvas = self.base.copy()

        mask = _rounded(self.cw, self.ch, radius)[:, :, None]
        hi = float(style.get("edgeHighlight", 0.13))
        if hi > 0 and radius >= 1:
            inner = _rounded(self.cw, self.ch, radius, inset=1.4)
            stroke = np.clip(mask[:, :, 0] - inner, 0.0, 1.0)[:, :, None] * hi
        else:
            stroke = np.zeros_like(mask)
        rect = self.base[self.cy0:self.cy0 + self.ch, self.cx0:self.cx0 + self.cw].astype(np.float32)
        self.A = (mask * (1.0 - stroke)).astype(np.float32)
        self.B = (rect * (1.0 - mask) * (1.0 - stroke)
                  + np.float32(255.0) * stroke).astype(np.float32)

    def compose(self, src: Image.Image, z: float, cx: float, cy: float, resample) -> np.ndarray:
        x0, y0, bw, bh = crop_box(z, cx, cy, self.sw, self.sh)
        content = src.resize((self.cw, self.ch), resample, box=(x0, y0, x0 + bw, y0 + bh))
        arr = np.asarray(content, dtype=np.float32)
        np.copyto(self.canvas[self.cy0:self.cy0 + self.ch, self.cx0:self.cx0 + self.cw],
                  np.clip(arr * self.A + self.B, 0, 255).astype(np.uint8))
        return self.canvas


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------
def _partial(out_path: str) -> str:
    """Where a render is written until it has finished: hidden, beside the
    target, same extension so ffmpeg picks the same muxer."""
    d, b = os.path.split(out_path)
    root, ext = os.path.splitext(b)
    return os.path.join(d, f".{root}.partial{ext}")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _kill(*procs: subprocess.Popen) -> None:
    """Kill and reap. Pipes are closed only once the process is dead, so a
    flush into a dead encoder fails fast instead of blocking."""
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
    for p in procs:
        for pipe in (p.stdin, p.stdout):
            if pipe and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def render(project: dict, out_path: str, preview: bool = False,
           progress: Callable[[int, int], None] | None = None,
           cancel: Callable[[], bool] | None = None) -> str:
    """Render project to out_path. `cancel` is polled once per frame.

    The video is written beside out_path under a hidden name and moved into
    place only once it is complete. If anything goes wrong - including a
    cancel - both ffmpegs are killed and only that partial file is deleted: a
    failed export never sits in the folder looking finished, and never costs
    you a file that was already at out_path.
    """
    src_path = project["source"]
    if not os.path.isfile(src_path):
        raise ZoomcutError(f"source recording not found: {src_path}")
    info = project.get("sourceInfo") or {}
    sw, sh = int(info["width"]), int(info["height"])

    out = dict(project["output"])
    ow, oh = int(out["width"]), int(out["height"])
    if preview:
        scale = 1280 / max(ow, 1)
        ow, oh = int(ow * scale) // 2 * 2, int(oh * scale) // 2 * 2
    fps = int(out.get("fps", 60))

    t0, t1 = (project.get("trim") or [0.0, None])[:2]
    t0 = float(t0 or 0.0)
    duration = float(info.get("duration") or 0.0)
    t1 = float(t1) if t1 else duration
    if t1 <= t0:
        raise ZoomcutError(f"trim range [{t0}, {t1}] is empty")
    total = max(1, int(round((t1 - t0) * fps)))

    stage = Stage(project, ow, oh, sw, sh)
    keys = project["camera"]["keys"]
    sp = project["camera"].get("spring") or {}
    k, m, c = float(sp.get("stiffness", 200)), float(sp.get("mass", 2.25)), float(sp.get("damping", 36))

    z0, cx0, cy0 = target_at(keys, t0)
    s_z = Spring(math.log(max(z0, 1e-6)), k, m, c)
    s_x = Spring(cx0, k, m, c)
    s_y = Spring(cy0, k, m, c)

    tmp = _partial(out_path)
    dec_cmd = [ffmpeg(), "-nostdin", "-v", "error"]
    if t0 > 0:
        dec_cmd += ["-ss", f"{t0:.4f}"]
    dec_cmd += ["-i", src_path, "-t", f"{t1 - t0:.4f}",
                "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    enc_cmd = [ffmpeg(), "-nostdin", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}", "-r", str(fps), "-i", "-",
               "-c:v", "libx264", "-preset", "medium" if preview else out.get("preset", "slow"),
               "-crf", str(22 if preview else out.get("crf", 17)),
               "-pix_fmt", "yuv420p",
               "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
               "-x264-params", "aq-mode=3:aq-strength=1.0",
               "-movflags", "+faststart", tmp]

    resample = Image.BICUBIC if preview else Image.LANCZOS
    fsize = sw * sh * 3
    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 8)
    try:
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10 ** 8)
    except BaseException:
        _kill(dec)
        dec.stderr.close()
        raise
    n = 0
    try:
        while True:
            if cancel and cancel():
                raise ZoomcutError("render cancelled")
            buf = dec.stdout.read(fsize)
            if len(buf) < fsize:
                break
            t = t0 + n / fps
            tz, tx, ty = target_at(keys, t)
            z = math.exp(s_z.step(math.log(max(tz, 1e-6)), 1.0 / fps))
            cx = s_x.step(tx, 1.0 / fps)
            cy = s_y.step(ty, 1.0 / fps)
            src = Image.frombuffer("RGB", (sw, sh), buf, "raw", "RGB", 0, 1)
            enc.stdin.write(stage.compose(src, z, cx, cy, resample).tobytes())
            n += 1
            if progress and n % 30 == 0:
                progress(n, total)
        # inside the try: this flushes the last frames, and a dead encoder
        # must be handled like any other failure
        enc.stdin.close()
    except BaseException as e:
        _kill(dec, enc)
        why = enc.stderr.read().decode(errors="replace")[:1500] if isinstance(e, BrokenPipeError) else ""
        dec.stderr.close()
        enc.stderr.close()
        _remove(tmp)
        if isinstance(e, BrokenPipeError):
            raise ZoomcutError("ffmpeg encoder exited early:\n" + why) from None
        raise
    dec.stdout.close()
    dec_err = dec.stderr.read().decode(errors="replace")
    enc_err = enc.stderr.read().decode(errors="replace")
    dec.wait()
    rc = enc.wait()
    if rc != 0 or not os.path.exists(tmp):
        _remove(tmp)
        raise ZoomcutError(f"encode failed (rc={rc}):\n{enc_err[:1500]}\n{dec_err[:500]}")
    if n == 0:
        _remove(tmp)
        raise ZoomcutError("no frames were decoded from the source")
    try:
        os.replace(tmp, out_path)
    except OSError as e:          # Windows: the old export is open in a player
        _remove(tmp)
        raise ZoomcutError(f"could not write {out_path}: {e.strerror or e} - is it open somewhere?")
    if progress:
        progress(n, total)
    return out_path


def still(project: dict, t: float, out_path: str, width: int = 1280) -> str:
    """Render one frame, with the camera wound forward to time t."""
    info = project.get("sourceInfo") or {}
    sw, sh = int(info["width"]), int(info["height"])
    out = project["output"]
    ow, oh = int(out["width"]), int(out["height"])
    scale = width / ow
    ow, oh = int(ow * scale) // 2 * 2, int(oh * scale) // 2 * 2
    fps = int(out.get("fps", 60))
    stage = Stage(project, ow, oh, sw, sh)
    keys = project["camera"]["keys"]
    sp = project["camera"].get("spring") or {}
    k, m, c = float(sp.get("stiffness", 200)), float(sp.get("mass", 2.25)), float(sp.get("damping", 36))
    t_start = float((project.get("trim") or [0.0])[0] or 0.0)
    z0, cx0, cy0 = target_at(keys, t_start)
    s_z, s_x, s_y = Spring(math.log(max(z0, 1e-6)), k, m, c), Spring(cx0, k, m, c), Spring(cy0, k, m, c)
    z, cx, cy = z0, cx0, cy0
    for i in range(max(0, int((t - t_start) * fps)) + 1):
        tz, tx, ty = target_at(keys, t_start + i / fps)
        z = math.exp(s_z.step(math.log(max(tz, 1e-6)), 1.0 / fps))
        cx, cy = s_x.step(tx, 1.0 / fps), s_y.step(ty, 1.0 / fps)
    p = subprocess.run([ffmpeg(), "-nostdin", "-v", "error", "-ss", f"{t:.4f}",
                        "-i", project["source"], "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    raw = p.stdout[:sw * sh * 3]
    if len(raw) < sw * sh * 3:
        raise ZoomcutError(f"could not read a frame at {t}s")
    src = Image.frombuffer("RGB", (sw, sh), raw, "raw", "RGB", 0, 1)
    Image.fromarray(stage.compose(src, z, cx, cy, Image.LANCZOS)).save(out_path)
    return out_path
