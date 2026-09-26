"""Local web app: record, auto-cut, preview, export - all on 127.0.0.1.

Stdlib only. One session lives in the server process; the browser is just a
view onto it.
"""
from __future__ import annotations
import filecmp, itertools, json, math, mimetypes, os, re, shutil, subprocess, threading, time, traceback
import webbrowser
import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import wallpapers, recorder, media, __version__
from .analyze import analyze, activity_track
from .director import DirectorConfig, plan, keyframes, Shot, _activity_box, _frame_shot, _place
from .project import (new_project, save, load, import_style_preset, PRESETS, DEFAULT_SPRING,
                      _deep_update)
from .render import render, still
from .util import ZoomcutError, probe, output_dir, platform_name, have, clamp, popen, patient, IS_WIN
from .winlist import pickable, WindowListError

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# ZOOMCUT_DEMO=1 serves a fixed window list instead of the real one, so the
# documentation screenshots never contain anybody's actual window titles.
DEMO = os.environ.get("ZOOMCUT_DEMO") == "1"
DEMO_WINDOWS = [
    {"id": 101, "app": "Safari", "title": "Acme Dashboard", "x": 0, "y": 0,
     "width": 1440, "height": 900, "layer": 0, "pid": 0},
    {"id": 102, "app": "Terminal", "title": "~/projects/acme", "x": 0, "y": 0,
     "width": 1100, "height": 720, "layer": 0, "pid": 0},
    {"id": 103, "app": "Code", "title": "server.py — acme-api", "x": 0, "y": 0,
     "width": 1600, "height": 1000, "layer": 0, "pid": 0},
    {"id": 104, "app": "Figma", "title": "Design system", "x": 0, "y": 0,
     "width": 1512, "height": 945, "layer": 0, "pid": 0},
]
OUT_DIR = output_dir()

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# names a request may address us by: loopback, plus an address serve() was
# explicitly bound to
HOSTS = set(LOOPBACK)
# Content types are spelled out, never guessed: on Windows the registry can
# map .js to text/plain, and browsers refuse a module script served that way.
STATIC = re.compile(r"[a-z0-9][a-z0-9_-]*\.(js|css|svg|png|ico)")
STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
                "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon"}
FILE_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
              ".mkv": "video/x-matroska", ".webm": "video/webm", ".png": "image/png",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".json": "application/json"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".bmp", ".tif", ".tiff"}
EXPORT_SUFFIXES = ("-zoomcut.mp4", "-preview.mp4", ".zoomcut.mp4")
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                "slow", "slower", "veryslow")
MAX_UPLOAD = 64 << 30
MAX_JSON = 32 << 20
RECENT_LIMIT = 40
PROBE_BUDGET = 1.5          # seconds of ffprobe per /api/recent
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                 *(f"{p}{d}" for p in ("COM", "LPT") for d in "0123456789\u00b9\u00b2\u00b3")}
MAX_THUMBS = 400            # wallpaper thumbnails kept on disk


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.rec: recorder.Recording | None = None
        self.analysis = None
        self.project: dict | None = None
        self.progress = _idle_progress()
        self.cancel = threading.Event()
        self.render_thread: threading.Thread | None = None
        self.allowed: set[str] = set()
        self.media = media.Prep(allow=self.allow)

    def allow(self, path: str) -> str:
        p = os.path.abspath(path)
        self.allowed.add(p)
        return p


def _idle_progress() -> dict:
    return {"state": "idle", "pct": 0, "message": "", "output": None,
            "started": None, "preview": False}


S = Session()


def _plan_project(source: str, style: dict | None, director: dict | None,
                  output: dict | None, style_preset: str | None = None,
                  preset_zooms: bool = False) -> dict:
    an = analyze(source)
    cfg = DirectorConfig()
    for k, v in (director or {}).items():
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, float(v))
    shots = plan(an, cfg)
    pj = new_project(source, keyframes(shots), shots, style=style,
                     output=output, director=cfg)
    if style_preset and os.path.isfile(style_preset):
        with open(style_preset) as f:
            imported = import_style_preset(json.load(f))
        _deep_update(pj["style"], imported["style"])
        if imported["spring"]:
            pj["camera"]["spring"] = imported["spring"]
        if preset_zooms and imported["manualKeys"]:
            pj["camera"]["keys"] = imported["manualKeys"]
    pj["analysis"] = {"cuts": an.cuts, "duration": an.duration,
                      "activity": activity_track(an)}
    S.analysis = an
    return pj


SHOT_KEYS = ("start", "end", "zoom", "cx", "cy", "reason")


def _rekey(pj: dict) -> dict:
    """Rebuild camera keys from the (possibly edited) shot list. No shots
    means no keys, which holds the camera on the whole window."""
    shots = [Shot(**{k: s[k] for k in SHOT_KEYS}) for s in pj["camera"].get("shots", [])]
    shots.sort(key=lambda s: s.start)
    pj["camera"]["keys"] = keyframes(shots)
    return pj


# --------------------------------------------------------------------------
# validation: a bad value from the editor is a clear 400, never a 500
# --------------------------------------------------------------------------
def _num(v, what: str) -> float:
    if isinstance(v, bool):
        raise ZoomcutError(f"{what} must be a number, got {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        raise ZoomcutError(f"{what} must be a number, got {str(v)[:40]!r}") from None
    if not math.isfinite(f):
        raise ZoomcutError(f"{what} must be a finite number, got {v!r}")
    return f


def _clean_shots(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise ZoomcutError("shots must be a list")
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ZoomcutError(f"shot {i} must be an object")
        start, end = _num(s.get("start"), f"shot {i} start"), _num(s.get("end"), f"shot {i} end")
        if not start < end:
            raise ZoomcutError(f"shot {i} must start before it ends ({start} >= {end})")
        c = dict(s)          # keys the editor keeps for itself survive
        c.update(start=start, end=end,
                 zoom=clamp(_num(s.get("zoom", 1.0), f"shot {i} zoom"), 1.0, 8.0),
                 cx=clamp(_num(s.get("cx", 0.5), f"shot {i} cx"), 0.0, 1.0),
                 cy=clamp(_num(s.get("cy", 0.5), f"shot {i} cy"), 0.0, 1.0),
                 reason=s["reason"] if isinstance(s.get("reason"), str) else "manual")
        out.append(c)
    out.sort(key=lambda s: s["start"])
    return out


SPRING_LIMITS = {"mass": (0.1, 20.0), "stiffness": (1.0, 2000.0), "damping": (0.1, 500.0)}


def _clean_spring(raw, current: dict | None) -> dict:
    if not isinstance(raw, dict):
        raise ZoomcutError("spring must be an object")
    out = dict(DEFAULT_SPRING)
    out.update(current or {})
    for k, (lo, hi) in SPRING_LIMITS.items():
        if k in raw:
            out[k] = clamp(_num(raw[k], f"spring {k}"), lo, hi)
    return out


def _clean_output(raw) -> dict:
    """Only the keys the renderer understands, each within what it can do."""
    if not isinstance(raw, dict):
        raise ZoomcutError("output must be an object")
    out = {}
    for k in ("width", "height"):
        if k in raw:                                   # h264 wants even sizes
            out[k] = int(clamp(math.floor(_num(raw[k], k)) // 2 * 2, 64, 7680))
    if "fps" in raw:
        out["fps"] = int(clamp(round(_num(raw["fps"], "fps")), 1, 120))
    if "crf" in raw:
        out["crf"] = int(clamp(round(_num(raw["crf"], "crf")), 0, 51))
    if "preset" in raw:
        if raw["preset"] not in X264_PRESETS:
            raise ZoomcutError(f"preset must be one of {', '.join(X264_PRESETS)}")
        out["preset"] = raw["preset"]
    return out


def _clean_trim(raw, duration: float) -> list:
    if raw is None:
        return [0.0, None]
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ZoomcutError("trim must be [start, end or null]")
    t0 = _num(raw[0], "trim start")
    t1 = None if raw[1] is None else _num(raw[1], "trim end")
    end = t1 if t1 is not None else (duration if duration > 0 else math.inf)
    if t0 < 0 or not t0 < end or (duration > 0 and end > duration + 1e-3):
        raise ZoomcutError(f"trim [{t0}, {t1}] does not fit the recording (0 to {duration:.3f}s)")
    if t1 is not None and duration > 0 and t1 >= duration - 1e-3:
        t1 = None                                      # "to the end", said plainly
    return [t0, t1]


def _clean_style(raw, current: dict) -> dict:
    if not isinstance(raw, dict):
        raise ZoomcutError("style must be an object")
    if "background" in raw and not isinstance(raw["background"], dict):
        raise ZoomcutError("style.background must be an object")
    st = json.loads(json.dumps(current))
    _deep_update(st, raw)
    bg = st.get("background") or {}
    if "background" in raw and bg.get("type") == "image":
        p = bg.get("path")
        p = os.path.abspath(os.path.expanduser(p)) if isinstance(p, str) and p else ""
        if not (p and os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXT):
            raise ZoomcutError(f"background image not found or not an image: {bg.get('path')!r}")
        bg["path"] = S.allow(p)                        # the preview loads it
    return st


def _safe_name(raw: str) -> str:
    """A bare file name we can create anywhere: no folders, no dot-file, and
    nothing a filesystem would misread."""
    base = re.split(r"[\\/]", str(raw or ""))[-1]
    keep = "".join(c for c in base if c.isalnum() or c in " ._-()").strip(" .")
    stem, ext = os.path.splitext(keep)
    stem = stem[:max(1, 120 - len(ext))].rstrip(" .")
    # Windows treats "NUL.x.mov" as the NUL device too: it only reads up to
    # the first dot, and ignores trailing spaces
    if stem.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED:
        stem = "_" + stem
    return (stem + ext)[:120] if stem else ""


def _mp4_name(raw: str) -> str:
    name = _safe_name(raw)
    stem, ext = os.path.splitext(name)
    if ext.lower() in VIDEO_EXT:
        name = stem
    if not name:
        raise ZoomcutError("the export needs a file name")
    return name + ".mp4"


def _claim(folder: str, name: str) -> tuple[str, str]:
    """A free destination - 'clip.mov', 'clip (2).mov', ... - and its .part.
    The .part is created exclusively, so two uploads never pick one name."""
    stem, ext = os.path.splitext(name)
    for i in itertools.count(1):
        dst = os.path.join(folder, name if i == 1 else f"{stem} ({i}){ext}")
        if os.path.exists(dst):
            continue
        try:
            os.close(os.open(dst + ".part", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        return dst, dst + ".part"


def _identical(folder: str, part: str, size: int) -> str | None:
    """An earlier upload with the same bytes, if there is one."""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for n in names:
        p = os.path.join(folder, n)
        if p == part or n.endswith(".part") or not os.path.isfile(p):
            continue
        try:
            if os.path.getsize(p) == size and filecmp.cmp(p, part, shallow=False):
                return p
        except OSError:
            continue
    return None


def _thumb_dim(raw, default: int) -> int:
    """Steps of 8 inside [32, 1920], so a client cannot fill the cache with a
    variant per pixel."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return int(clamp(math.floor(v / 8 + 0.5) * 8, 32, 1920))


def _thumb(name: str, w: int = 224, h: int = 126) -> str:
    """Small JPEG of a wallpaper, generated once and cached. Only catalogue
    names: wallpapers.resolve() would also take any image path, which would
    turn this into a way to probe the disk. The cache is pruned as it grows,
    because every size a page asks for is another file."""
    catalogue = {e["source"] for e in wallpapers.discover()}
    if not name or wallpapers.resolve(name)["source"] not in catalogue:
        raise ZoomcutError("not a wallpaper on this machine")
    path = wallpapers.thumbnail(name, w, h)
    media.prune_files(os.path.dirname(path), MAX_THUMBS, keep_path=path)
    return path


def _project_path(source: str) -> str:
    """OUT_DIR/<recording>.zoomcut.json - or '<recording> (2)...' when that
    name already holds the project of a different recording (an import and a
    capture can share a name)."""
    stem = os.path.splitext(os.path.basename(source))[0]
    for i in itertools.count(1):
        p = os.path.join(OUT_DIR, f"{stem}.zoomcut.json" if i == 1 else f"{stem} ({i}).zoomcut.json")
        if not os.path.exists(p):
            return p
        item: dict = {}
        _project_summary(p, os.path.getsize(p), item)
        if item.get("source") and _same_file(item["source"], source):
            return p


def _unreadable(path: str, e: Exception) -> ZoomcutError:
    """ffprobe's own words are for the terminal; the app says what happened."""
    last = [ln.strip() for ln in str(e).splitlines() if ln.strip()][-1:] or [""]
    detail = last[0].split(": ")[-1][:120]
    return ZoomcutError(f"Zoomcut can't read {os.path.basename(path)} as a video"
                        + (f" ({detail})" if detail else "")
                        + ". Is it a finished recording?")


def _same_file(a: str, b: str) -> bool:
    """Would writing a clobber b? Paths are compared as the filesystem sees
    them - macOS and Windows ignore case, and links point elsewhere."""
    a, b = os.path.abspath(a), os.path.abspath(b)
    if os.path.normcase(a) == os.path.normcase(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:                       # a does not exist yet: nothing to clobber
        return False


def _hostname(netloc: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '::1'."""
    h = netloc.strip().lower()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h
    return h.split(":")[0] if h.count(":") == 1 else h


def _origin_host(origin: str) -> str:
    """'http://[::1]:8765' -> '::1'; "null" and anything unparsable -> ''."""
    try:
        return urlparse(origin).hostname or ""
    except ValueError:                                # e.g. "http://[::1"
        return ""


def _director(pj: dict) -> DirectorConfig:
    cfg = DirectorConfig()
    for k, v in ((pj.get("camera") or {}).get("director") or {}).items():
        if hasattr(cfg, k) and isinstance(v, (int, float)) and not isinstance(v, bool):
            setattr(cfg, k, float(v))
    return cfg


_warm = {"running": False, "next": None}


def _warm_analysis(src: str) -> None:
    """A loaded project has no analysis in memory; build it in the background
    so the first hand-placed zoom after a load can be framed too. One at a
    time: loads in quick succession queue only the latest source, and an
    analysis that is already there is not redone."""
    if S.analysis is not None and os.path.abspath(S.analysis.path) == os.path.abspath(src):
        return
    with S.lock:
        _warm["next"] = src
        if _warm["running"]:
            return
        _warm["running"] = True

    def work():
        while True:
            with S.lock:
                job, _warm["next"] = _warm["next"], None
                if job is None:
                    _warm["running"] = False
                    return
            if not (S.project and S.project.get("source") == job):
                continue                        # superseded while it waited
            try:
                an = analyze(job)
            except Exception:                                # pragma: no cover
                continue
            if S.project and S.project.get("source") == job:
                S.analysis = an
    threading.Thread(target=work, daemon=True, name="zoomcut-analysis").start()


def _suggest(t0: float, t1: float) -> dict | None:
    """Frame a hand-placed zoom the way the director would: on the activity in
    [t0, t1), with the crop's edges kept off busy interface. None when nothing
    moved there. A weak suggestion is pushed to a zoom worth making - asking
    for a zoom means wanting one."""
    an, pj = S.analysis, S.project
    if an is None or not pj or os.path.abspath(an.path) != os.path.abspath(pj["source"]):
        return None
    cfg = _director(pj)
    box, heat = _activity_box(an, t0, t1, cfg)
    if box is None:                                          # widen once, then give up
        box, heat = _activity_box(an, max(0.0, t0 - 1.0), t1 + 1.0, cfg)
    if box is None:
        return None
    z, cx, cy = _frame_shot(box, cfg, heat, an.struct_x, an.struct_y)
    if z < max(cfg.min_zoom, 1.3):
        z = max(cfg.min_zoom, 1.5)
        half = 0.5 / z
        h = np.asarray(heat)
        cx = _place(h.sum(axis=0), half, cfg, an.struct_x)
        cy = _place(h.sum(axis=1), half, cfg, an.struct_y)
    half = 0.5 / z
    cx, cy = clamp(cx, half, 1 - half), clamp(cy, half, 1 - half)
    return {"zoom": round(z, 3), "cx": round(cx, 4), "cy": round(cy, 4)}


def _start_media(src: str) -> None:
    """Media prep is a nicety: the editor falls back to stills without it, so
    it must never turn a good analyse or load into an error."""
    try:
        S.media.ensure(src, retry=True)
    except Exception:                                        # pragma: no cover
        traceback.print_exc()


def _progress() -> dict:
    p = dict(S.progress)
    started, ended = p.get("started"), p.pop("ended", None)
    p["elapsed"] = round((ended or time.time()) - started, 2) if started else 0.0
    return p


def _reveal(p: str) -> None:
    """Show p in the file manager: no shell, and nobody waits for it."""
    name = platform_name()
    if name == "macOS":
        cmd = ["open", "-R", p]
    elif name == "Windows":
        cmd = ["explorer", f"/select,{p}"]
    elif shutil.which("xdg-open"):
        cmd = ["xdg-open", os.path.dirname(p)]
    else:
        raise ZoomcutError("no file manager to open: xdg-open is not installed")
    popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# recents
# --------------------------------------------------------------------------
_PROBED: dict[tuple[str, int], dict | None] = {}


def _probe_memo(p: str, mtime_ns: int, deadline: float) -> dict | None:
    k = (p, mtime_ns)
    if k in _PROBED:
        return _PROBED[k]
    if time.monotonic() > deadline:
        return None                 # past the budget: the list matters more
    try:
        info = probe(p)
    except Exception:
        info = None                 # half-written, or not really a video
    if len(_PROBED) > 2000:
        _PROBED.clear()
    _PROBED[k] = info
    return info


def _num_or_none(v, cast):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return cast(v)


def _recent() -> dict:
    found = []
    for folder, imports in ((OUT_DIR, False), (os.path.join(OUT_DIR, "imports"), True)):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            low = e.name.lower()
            if e.name.startswith(".") or low.endswith((".part", ".pad.mov")):
                continue
            ext = os.path.splitext(low)[1]
            if imports:
                if ext not in VIDEO_EXT:
                    continue
                kind = "import"
            elif low.endswith(".zoomcut.json"):
                kind = "project"
            elif ext in VIDEO_EXT:
                # the app records .mov (macOS) or .mkv and always exports .mp4,
                # so an .mp4 here is an export whatever it was named
                exported = low.endswith(EXPORT_SUFFIXES) or (
                    ext in (".mp4", ".m4v") and not low.startswith("capture-"))
                kind = "export" if exported else "recording"
            else:
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            found.append((st.st_mtime, st.st_mtime_ns, st.st_size, e.path, kind))
    found.sort(key=lambda t: t[0], reverse=True)

    deadline = time.monotonic() + PROBE_BUDGET
    items = []
    for mtime, mtime_ns, size, path, kind in found[:RECENT_LIMIT]:
        p = S.allow(path)
        item = {"path": p, "name": os.path.basename(p), "kind": kind, "size": size,
                "mtime": mtime, "duration": None, "width": None, "height": None,
                "source": None}
        if kind == "project":
            info = _project_summary(p, size, item)
        else:
            info = _probe_memo(p, mtime_ns, deadline)
        if info:
            item["duration"] = _num_or_none(info.get("duration"), float)
            item["width"] = _num_or_none(info.get("width"), int)
            item["height"] = _num_or_none(info.get("height"), int)
        items.append(item)
    return {"outDir": OUT_DIR, "items": items}


def _project_summary(p: str, size: int, item: dict) -> dict | None:
    """A project's source and sourceInfo, without trusting the file."""
    if size >= 5 << 20:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            pj = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(pj, dict):
        return None
    src = pj.get("source")
    if isinstance(src, str) and src:
        item["source"] = src
        if os.path.isfile(src):
            S.allow(src)
    info = pj.get("sourceInfo")
    return info if isinstance(info, dict) else None


class Handler(BaseHTTPRequestHandler):
    server_version = "zoomcut"

    def log_message(self, fmt, *args):      # quiet by default
        pass

    # ---------------------------------------------------------------- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body: bytes, ctype: str, cache: str = "no-cache"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if ctype.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, code: int, msg: str) -> bool:
        """Answer an error before the body was read. A small body is drained
        first: closing a socket with unread data resets it, and the client
        may never see the answer."""
        if self.command == "POST":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= 1 << 20:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass
        self.close_connection = True
        self._json({"error": msg}, code)
        return False

    def _guard(self) -> bool:
        """Only our own page may talk to us.

        We bind 127.0.0.1, but any page the user visits can still aim a
        request here, and DNS rebinding can even read the answer. A loopback
        Host defeats the rebinding; a loopback Origin plus the JSON / upload
        header requirements force a CORS preflight, which is never answered.
        """
        host = self.headers.get("Host")
        if host is not None and _hostname(host) not in HOSTS:
            return self._refuse(403, "forbidden host")
        origin = self.headers.get("Origin")
        if origin is not None and _origin_host(origin) not in HOSTS:
            return self._refuse(403, "forbidden origin")
        # a GET can have side effects too (thumbnails, posters, media prep),
        # and any page can aim an <img> at us. Browsers say where a request
        # came from; only our own page, or the address bar, may use the API.
        if urlparse(self.path).path.startswith("/api/"):
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site and site not in ("same-origin", "none"):
                return self._refuse(403, "forbidden cross-site request")
            ref = self.headers.get("Referer")
            if not site and ref and _origin_host(ref) not in HOSTS:
                return self._refuse(403, "forbidden referer")
        return True

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ZoomcutError("bad Content-Length") from None
        if n > MAX_JSON:
            raise ZoomcutError("request body is too large")
        if n <= 0:
            return {}
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ZoomcutError("request body is not valid JSON") from None
        if not isinstance(b, dict):
            raise ZoomcutError("request body must be a JSON object")
        return b

    def _serve_file(self, path: str):
        """Serve a produced file, with Range support so <video> can seek."""
        p = os.path.abspath(path)
        if p not in S.allowed or not os.path.isfile(p):
            return self._json({"error": "not available"}, 404)
        size = os.path.getsize(p)
        ctype = (FILE_TYPES.get(os.path.splitext(p)[1].lower())
                 or mimetypes.guess_type(p)[0] or "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:                                   # "bytes=-N": the last N
                    start = max(0, size - int(m.group(2)))
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def _upload(self, q: dict):
        """Stream a dropped file to disk. Browsers never reveal a dropped
        file's path, so the page sends the bytes instead."""
        if self.headers.get("X-Zoomcut-Upload") != "1":
            return self._refuse(403, "uploads need the X-Zoomcut-Upload header")
        n = (self.headers.get("Content-Length") or "").strip()
        if not re.fullmatch(r"[0-9]+", n):
            return self._refuse(411, "uploads need a Content-Length")
        n = int(n)
        if n > MAX_UPLOAD:
            return self._refuse(413, "that file is larger than 64 GiB")
        name = _safe_name((q.get("name") or [""])[0])
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXT:
            kind, folder = "video", os.path.join(OUT_DIR, "imports")
        elif ext in IMAGE_EXT:
            kind, folder = "image", os.path.join(OUT_DIR, "backgrounds")
        else:
            return self._refuse(415, f"not a video or image Zoomcut can use: {name or 'unnamed'}")
        try:
            os.makedirs(folder, exist_ok=True)
            dst, part = _claim(folder, name)
        except OSError as e:
            return self._refuse(400, f"cannot write to {folder}: {e.strerror or e}")
        self.connection.settimeout(120)            # a stalled client must not pin a thread
        got = 0
        try:
            with open(part, "wb") as f:
                while got < n:
                    chunk = self.rfile.read(min(1 << 20, n - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if got < n:
                raise ZoomcutError(f"the upload ended early ({got} of {n} bytes)")
            if kind == "video":
                try:
                    probe(part)
                except ZoomcutError as e:
                    media._unlink(part)
                    return self._json({"error": str(_unreadable(name, e))}, 415)
                # dropping the same file twice should not make a second copy
                twin = _identical(folder, part, n)
                if twin:
                    media._unlink(part)
                    return self._json({"ok": True, "path": S.allow(twin), "kind": kind,
                                       "size": got, "existing": True})
            patient(os.replace, part, dst)
        except (OSError, ZoomcutError) as e:
            media._unlink(part)
            self.close_connection = True
            try:
                self._json({"error": str(e) if isinstance(e, ZoomcutError)
                            else f"the upload failed: {e}"}, 400)
            except OSError:
                pass                                # the client is already gone
            return
        return self._json({"ok": True, "path": S.allow(dst), "kind": kind, "size": got})

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self._send(f.read(), "text/html; charset=utf-8")
            m = STATIC.fullmatch(u.path[1:])
            if m and os.path.isfile(os.path.join(WEB, m.group(0))):
                with open(os.path.join(WEB, m.group(0)), "rb") as f:
                    return self._send(f.read(), STATIC_TYPES[m.group(1)])
            if u.path == "/api/windows":
                if DEMO:
                    return self._json({"windows": DEMO_WINDOWS})
                try:
                    return self._json({"windows": pickable()})
                except WindowListError as e:
                    return self._json({"windows": [], "error": str(e)})
            if u.path == "/api/wallpapers":
                return self._json({"wallpapers": [w["name"] for w in wallpapers.discover()],
                                   "default": wallpapers.default_name()})

            if u.path == "/api/wallpaper-thumb":
                name = (q.get("name") or [""])[0]
                w = _thumb_dim((q.get("w") or [None])[0], 224)
                h = _thumb_dim((q.get("h") or [None])[0], 126)
                try:
                    path = _thumb(name, w, h)
                except Exception:
                    return self._json({"error": "no thumbnail"}, 404)
                with open(path, "rb") as f:
                    return self._send(f.read(), "image/jpeg", "max-age=86400")
            if u.path == "/api/state":
                rec = S.rec
                return self._json({
                    "recording": bool(rec and rec.running),
                    "recordSeconds": (time.time() - rec.started) if (rec and rec.running) else 0,
                    "hasProject": S.project is not None,
                    "progress": _progress(),
                    "permission": list(recorder.available()),
                    "outDir": OUT_DIR,
                    "platform": platform_name(),
                    "backend": recorder.backend_name(),
                    "ffmpeg": have("ffmpeg"),
                    "modes": list(recorder.MODES) if platform_name() == "macOS"
                             else [m for m in recorder.MODES if m != "interactive"],
                    "version": __version__,
                    # highlighting clicks is a screencapture option
                    "clicksSupported": platform_name() == "macOS",
                })
            if u.path == "/api/project":
                return self._json({"project": S.project})
            if u.path == "/api/progress":
                return self._json(_progress())
            if u.path == "/api/file":
                return self._serve_file((q.get("path") or [""])[0])
            if u.path == "/api/media":
                src = (S.project or {}).get("source")
                if src and not os.path.isfile(src):
                    return self._json(media._state(os.path.abspath(src), "error",
                                                   message="the recording is not there any more"))
                if src:
                    S.media.ensure(src)
                return self._json(S.media.status())
            if u.path == "/api/recent":
                return self._json(_recent())
            if u.path == "/api/poster":
                p = os.path.abspath((q.get("path") or [""])[0])
                if p not in S.allowed or not os.path.isfile(p):
                    return self._json({"error": "not available"}, 404)
                try:
                    jpg = media.poster(p)
                except ZoomcutError:
                    return self._json({"error": "no poster for that file"}, 404)
                with open(jpg, "rb") as f:
                    return self._send(f.read(), "image/jpeg", "max-age=3600")
            return self._json({"error": "no such endpoint"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return                              # the browser gave up on this one
        except Exception as e:                                   # pragma: no cover
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":         # raw bytes: never through _body()
                return self._upload(parse_qs(u.query))
            # a cross-origin page cannot send this without a preflight
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._refuse(415, "expected application/json")
            b = self._body()

            if u.path == "/api/record/start":
                if S.rec and S.rec.running:
                    return self._json({"error": "already recording"}, 409)
                os.makedirs(OUT_DIR, exist_ok=True)
                name = b.get("name") or time.strftime("capture-%Y%m%d-%H%M%S")
                path = os.path.join(OUT_DIR, f"{name}.mov")
                region = b.get("region")
                S.rec = recorder.start(
                    path, mode=b.get("mode", "window"),
                    region=tuple(region) if region else None,
                    display=int(_num(b.get("display") or 1, "display")),
                    window_id=b.get("windowId"),
                    cursor=bool(b.get("cursor", True)),
                    clicks=bool(b.get("clicks", False)))
                return self._json({"ok": True, "path": S.rec.path, "mode": S.rec.mode})

            if u.path == "/api/record/stop":
                if not S.rec:
                    return self._json({"error": "not recording"}, 409)
                path = recorder.stop(S.rec)
                S.allow(path)
                info = probe(path)
                S.rec = None
                return self._json({"ok": True, "path": path, "info": info})

            if u.path == "/api/analyze":
                src = b.get("source") or ""
                src = os.path.expanduser(str(src))
                if not os.path.isfile(src):
                    return self._json({"error": f"no such file: {src}"}, 400)
                S.allow(src)
                size = str(b.get("size") or "1440p")
                w, h = PRESETS.get(size, PRESETS["1440p"])
                out = {"width": w, "height": h, **_clean_output({"fps": b.get("fps") or 60})}
                if b.get("output") is not None:
                    out.update(_clean_output(b["output"]))
                try:
                    probe(src)
                except ZoomcutError as e:
                    raise _unreadable(src, e) from None
                pj = _plan_project(src, b.get("style"), b.get("director"), out,
                                   b.get("stylePreset"), bool(b.get("presetZooms")))
                S.project = pj
                _start_media(pj["source"])
                return self._json({"project": pj})

            if u.path == "/api/project":
                with S.lock:
                    if not S.project:
                        return self._json({"error": "nothing analysed yet"}, 400)
                    # edit a copy: a 400 halfway through leaves the project as it was
                    pj = json.loads(json.dumps(S.project))
                    if "shots" in b:
                        pj["camera"]["shots"] = _clean_shots(b["shots"])
                        _rekey(pj)
                    if "spring" in b:
                        pj["camera"]["spring"] = _clean_spring(b["spring"],
                                                               pj["camera"].get("spring"))
                    if "style" in b:
                        pj["style"] = _clean_style(b["style"], pj["style"])
                    if "output" in b:
                        pj["output"].update(_clean_output(b["output"]))
                    if "trim" in b:
                        dur = float((pj.get("sourceInfo") or {}).get("duration") or 0.0)
                        pj["trim"] = _clean_trim(b["trim"], dur)
                    S.project = pj
                return self._json({"project": pj})

            if u.path == "/api/still":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                os.makedirs(OUT_DIR, exist_ok=True)
                t = _num(b.get("t") or 0.0, "t")
                width = int(clamp(_num(b.get("width") or 1100, "width"), 64, 3840))
                out = os.path.join(OUT_DIR, ".preview-still.png")
                still(S.project, t, out, width=width)
                S.allow(out)
                return self._json({"ok": True, "path": out, "t": t})

            if u.path == "/api/render":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                preview = bool(b.get("preview"))
                base = os.path.splitext(os.path.basename(S.project["source"]))[0]
                if b.get("name"):
                    out = os.path.join(OUT_DIR, _mp4_name(b["name"]))
                else:
                    out = b.get("output") or os.path.join(
                        OUT_DIR, f"{base}{'-preview' if preview else '-zoomcut'}.mp4")
                out = os.path.abspath(os.path.expanduser(out))
                if _same_file(out, S.project["source"]):
                    return self._json({"error": "the export would overwrite the recording"}, 400)
                with S.lock:
                    if S.progress.get("state") == "running":
                        return self._json({"error": "a render is already running"}, 409)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    pj = json.loads(json.dumps(S.project))
                    ev = S.cancel = threading.Event()
                    base_progress = {"started": time.time(), "preview": preview}
                    S.progress = {"state": "running", "pct": 0, "message": "starting",
                                  "output": None, **base_progress}

                def work():
                    def cb(n, total):
                        S.progress["pct"] = int(n * 100 / max(total, 1))
                        S.progress["message"] = f"{n}/{total} frames"
                    try:
                        render(pj, out, preview=preview, progress=cb, cancel=ev.is_set)
                        S.allow(out)
                        S.progress = {"state": "done", "pct": 100, "message": "finished",
                                      "output": out, "ended": time.time(), **base_progress}
                    except Exception as e:
                        if ev.is_set():
                            S.progress = {"state": "cancelled", "pct": 0, "message": "cancelled",
                                          "output": None, "ended": time.time(), **base_progress}
                        else:
                            traceback.print_exc()
                            S.progress = {"state": "error", "pct": 0, "message": str(e),
                                          "output": None, "ended": time.time(), **base_progress}
                S.render_thread = threading.Thread(target=work, daemon=True, name="zoomcut-render")
                S.render_thread.start()
                return self._json({"ok": True, "output": out})

            if u.path == "/api/render/cancel":
                with S.lock:
                    running = S.progress.get("state") == "running"
                    if running:
                        S.cancel.set()
                if not running:
                    return self._json({"error": "nothing is rendering"}, 409)
                return self._json({"ok": True})

            if u.path == "/api/save":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                out = os.path.abspath(os.path.expanduser(
                    b.get("path") or _project_path(S.project["source"])))
                try:
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    save(S.project, out)
                except OSError as e:
                    return self._json({"error": f"could not save {out}: {e.strerror or e}"}, 400)
                S.allow(out)
                return self._json({"ok": True, "path": out})

            if u.path == "/api/load":
                p = os.path.abspath(os.path.expanduser(str(b.get("path") or "")))
                try:
                    pj = load(p)
                except (OSError, ValueError, AttributeError) as e:   # AttributeError: not an object
                    return self._json({"error": f"could not load {p}: {e}"}, 400)
                if not isinstance(pj.get("source"), str):
                    return self._json({"error": f"{p} names no recording"}, 400)
                S.project = pj
                S.allow(p)
                S.allow(pj["source"])
                bg = (pj.get("style") or {}).get("background") or {}
                if bg.get("type") == "image" and isinstance(bg.get("path"), str) \
                        and os.path.isfile(bg["path"]):
                    S.allow(bg["path"])
                if os.path.isfile(pj["source"]):
                    _start_media(pj["source"])
                    _warm_analysis(pj["source"])
                return self._json({"project": S.project})

            if u.path == "/api/suggest":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                t0, t1 = _num(b.get("t0"), "t0"), _num(b.get("t1"), "t1")
                if not 0 <= t0 < t1:
                    raise ZoomcutError(f"t0 must be before t1, got {t0} and {t1}")
                s = _suggest(t0, t1)
                if s is None:
                    return self._json({"error": "nothing moved there to frame"}, 409)
                return self._json(s)

            if u.path == "/api/reveal":
                p = os.path.abspath(os.path.expanduser(str(b.get("path") or "")))
                if p in S.allowed and os.path.exists(p):
                    _reveal(p)
                    return self._json({"ok": True})
                return self._json({"error": "not available"}, 404)

            return self._json({"error": "no such endpoint"}, 404)
        except ZoomcutError as e:
            return self._json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:                                   # pragma: no cover
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def shutdown_session() -> None:
    """Leave nothing behind: finish a recording in progress (a killed
    recorder leaves a file that will not open), stop media prep and any
    render."""
    rec = S.rec
    if rec is not None and rec.running:
        try:
            recorder.stop(rec)
        except Exception:                                    # pragma: no cover
            traceback.print_exc()
        S.rec = None
    S.media.stop()
    S.cancel.set()
    t = S.render_thread
    if t and t.is_alive():
        t.join(timeout=10)


def _exit_with_stdin(httpd) -> None:
    """Stop when whoever started us closes our stdin - or dies, which closes
    it too. The desktop app runs the server this way, so a crashed or
    force-quit app can never leave a server behind."""
    import sys

    def watch():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        httpd.shutdown()
    threading.Thread(target=watch, daemon=True, name="zoomcut-stdin").start()
    if IS_WIN:
        threading.Thread(target=_exit_with_parent, args=(httpd,), daemon=True,
                         name="zoomcut-parent").start()


def _exit_with_parent(httpd) -> None:
    """Windows: also watch the process that started us. Killing it does not
    always end the read on the other side of its pipe to us. Waits on its
    process handle when we may open one - Chromium's browser process can
    refuse - and otherwise looks for it in the process list twice a second,
    which needs no rights on it at all."""
    import ctypes
    from ctypes import wintypes
    if not hasattr(ctypes, "WinDLL"):
        return
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
    parent = os.getppid()
    handle = k32.OpenProcess(SYNCHRONIZE, False, parent)
    if handle:
        k32.WaitForSingleObject(handle, INFINITE)
    else:
        while _pid_running(parent):
            time.sleep(0.5)
    httpd.shutdown()


def _pid_running(pid: int) -> bool:
    """Windows: is there a process with this id? From the process list, so it
    works for processes we are not allowed to open."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    size = 1024
    while True:
        ids = (wintypes.DWORD * size)()
        used = wintypes.DWORD()
        if not k32.K32EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(used)):
            return True                      # cannot tell, so do not stop
        if used.value < ctypes.sizeof(ids):  # the whole list fitted
            return pid in ids[:used.value // ctypes.sizeof(wintypes.DWORD)]
        size *= 4


def serve(host="127.0.0.1", port=8765, open_browser=True, exit_with_stdin=False):
    httpd = ThreadingHTTPServer((host, port), Handler)
    port = httpd.server_address[1]            # the real one, when asked for port 0
    url = f"http://{host}:{port}/"
    bound = _hostname(host)
    if bound not in LOOPBACK:
        if bound in ("0.0.0.0", "::", ""):
            # a wildcard has no one name to accept, so only loopback works
            url = f"http://127.0.0.1:{port}/"
            print("note: requests are only answered when addressed to this machine by a loopback\n"
                  "      name; bind one specific address with --host to reach it from elsewhere")
        else:
            HOSTS.add(bound)
            print(f"warning: serving on {host} - anyone who can reach that address can record\n"
                  "         this screen and read the recordings. There is no login.")
    # the address first, and flushed: a parent reading a pipe waits for it
    print(f"zoomcut ui  ->  {url}", flush=True)
    ok, why = recorder.available()
    print(f"screen recording: {'ready' if ok else 'BLOCKED - ' + why}")
    print("press Ctrl-C to stop", flush=True)
    if exit_with_stdin:
        _exit_with_stdin(httpd)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutdown_session()
