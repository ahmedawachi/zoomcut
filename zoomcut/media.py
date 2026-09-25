"""Media for the editor: a proxy every browser can play, a timeline filmstrip,
and poster frames for the recents list.

The editor previews the recording live in the browser. Recordings are .mov or
.mkv, often variable frame rate and 1440p-4K, which browsers either refuse or
seek slowly, so each source gets a small h264 copy with a keyframe every 15
frames, plus one sprite of evenly spaced frames for the timeline.

Everything is cached by the source's identity (path, size, mtime) and pruned
by count and by bytes: a thumbnail cache once filled a disk.
"""
from __future__ import annotations
import hashlib, json, math, os, shutil, subprocess, tempfile, threading
from typing import Callable

from . import util
from .util import ZoomcutError, probe, ffmpeg, patient

ROOT: str | None = None        # tests point these at a temp dir
POSTERS: str | None = None
MAX_ENTRIES = 12
MAX_BYTES = 4 << 30
MAX_POSTERS = 200
PROXY_MAX_W = 1920
STRIP_COLS = 10
STRIP_H = 96
STRIP_KEYS = ("count", "cols", "rows", "tw", "th", "interval")
# past this the strip decodes keyframes only: a tile covers over a second
# there, and a full decode of a long recording would stall at 90% for ages
STRIP_NOKEY_AFTER = 120.0

# a recents grid asks for dozens of posters at once; each is an ffmpeg
_POSTER_SLOTS = threading.BoundedSemaphore(3)


def media_root() -> str:
    return ROOT or os.path.join(util.cache_dir(), "media")


def poster_root() -> str:
    return POSTERS or os.path.join(util.cache_dir(), "posters")


def source_key(path: str) -> str:
    """Identity of a file's current contents, so an edited or re-recorded file
    never picks up a stale proxy."""
    p = os.path.abspath(path)
    st = os.stat(p)
    return hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


def _even(n: int) -> int:
    return int(n) - int(n) % 2


def strip_layout(duration: float, sw: int, sh: int) -> dict:
    """Geometry of the filmstrip: tile i covers [i*interval, (i+1)*interval)."""
    count = min(90, max(8, int(round(duration))))
    tw = min(256, max(64, _even(round(STRIP_H * sw / max(sh, 1)))))
    return {"count": count, "cols": STRIP_COLS, "rows": math.ceil(count / STRIP_COLS),
            "tw": tw, "th": STRIP_H, "interval": duration / count}


def _unlink(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _stop(proc) -> None:
    """Kill, not terminate: a SIGTERMed ffmpeg finalises its output first, and
    with +faststart that means rewriting the whole file we are discarding."""
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _ffmpeg(args: list[str], duration: float, on_frac: Callable[[float], None],
            track: Callable[[subprocess.Popen], None]) -> None:
    """Run ffmpeg, reporting `-progress` as a 0..1 fraction of duration.

    stderr goes to a temp file rather than a pipe: nobody reads it until the
    end, and a chatty failure could otherwise fill the pipe and hang us.
    """
    cmd = [ffmpeg(), "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats", "-y", *args]
    with tempfile.TemporaryFile() as err:
        proc = util.popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=err, text=True)
        track(proc)
        try:
            for line in proc.stdout:
                k, _, v = line.strip().partition("=")
                # out_time_ms is microseconds too - a long-standing ffmpeg quirk
                if k in ("out_time_us", "out_time_ms") and v.isdigit() and duration > 0:
                    on_frac(min(1.0, int(v) / 1e6 / duration))
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            err.seek(0)
            why = _last_line(err.read().decode(errors="replace"))
            raise ZoomcutError(why or f"ffmpeg exited with status {rc}")


def cached(source: str) -> dict | None:
    """The finished proxy + strip for source, or None if any part is missing."""
    try:
        d = os.path.join(media_root(), source_key(source))
        with open(os.path.join(d, "strip.json")) as f:
            meta = json.load(f)
        strip = {"path": os.path.join(d, "strip.jpg"), **{k: meta[k] for k in STRIP_KEYS}}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    proxy = os.path.join(d, "proxy.mp4")
    if not (os.path.isfile(proxy) and os.path.isfile(strip["path"])):
        return None
    try:
        os.utime(d, None)                   # reuse counts as recent for pruning
    except OSError:
        pass
    return {"proxy": proxy, "strip": strip}


def prepare(source: str, progress: Callable[[int, str], None] | None = None,
            alive: Callable[[], bool] | None = None,
            track: Callable[[subprocess.Popen], None] | None = None) -> dict | None:
    """Build (or reuse) the proxy and filmstrip for source.

    Returns {"proxy": path, "strip": {...}}, or None once `alive` says the job
    was superseded - its results are not wanted, so nothing is published.
    """
    report = progress or (lambda pct, msg: None)
    alive = alive or (lambda: True)
    track = track or (lambda proc: None)
    src = os.path.abspath(source)
    if not os.path.isfile(src):
        raise ZoomcutError(f"no such file: {src}")
    hit = cached(src)
    if hit:
        return hit
    root = media_root()
    d = os.path.join(root, source_key(src))
    os.makedirs(d, exist_ok=True)
    info = probe(src)
    dur = float(info.get("duration") or 0.0)
    if dur <= 0:
        raise ZoomcutError("the recording reports no duration")
    tag = os.urandom(4).hex()               # two jobs never share a temp name
    proxy = os.path.join(d, "proxy.mp4")

    if not os.path.isfile(proxy):
        tmp = f"{proxy}.{tag}.part"
        try:
            report(0, "building preview")
            _ffmpeg(["-i", src, "-map", "0:v:0", "-an", "-sn", "-dn",
                     "-vf", f"scale='trunc(min({PROXY_MAX_W},iw)/2)*2':-2,format=yuv420p",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-preset", "veryfast", "-crf", "21",
                     "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
                     "-movflags", "+faststart", "-f", "mp4", tmp],
                    dur, lambda f: report(int(f * 90), "building preview"), track)
            if not alive():
                return None
            patient(os.replace, tmp, proxy)
        finally:
            _unlink(tmp)
    if not alive():
        return None

    lay = strip_layout(dur, int(info["width"]), int(info["height"]))
    strip = os.path.join(d, "strip.jpg")
    tmp = f"{strip}.{tag}.part"
    tw, th = lay["tw"], lay["th"]
    try:
        report(90, "building timeline strip")
        _ffmpeg([*(["-skip_frame", "nokey"] if dur > STRIP_NOKEY_AFTER else []),
                 "-i", proxy, "-an",
                 "-vf", f"fps={lay['count']}/{dur:.6f},"
                        f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},"
                        f"trim=end_frame={lay['count']},tile={lay['cols']}x{lay['rows']}",
                 "-frames:v", "1", "-q:v", "4", "-update", "1",
                 "-f", "image2", "-c:v", "mjpeg", tmp],
                dur, lambda f: None, track)
        if not alive():
            return None
        patient(os.replace, tmp, strip)
    finally:
        _unlink(tmp)
    meta = os.path.join(d, "strip.json")
    with open(f"{meta}.{tag}.part", "w") as f:
        json.dump(lay, f)
    patient(os.replace, f"{meta}.{tag}.part", meta)
    try:
        os.utime(d, None)
    except OSError:
        pass
    prune(root, keep=d)
    return {"proxy": proxy, "strip": {"path": strip, **lay}}


def _tree_size(path: str) -> int:
    total = 0
    for base, _, files in os.walk(path):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total


def prune(root: str, keep: str | None = None, entries: int = MAX_ENTRIES,
          budget: int = MAX_BYTES) -> list[str]:
    """Delete the oldest entries until at most `entries` remain and they fit in
    `budget` bytes. The entry in use is never deleted. Returns what went."""
    keep = os.path.abspath(keep) if keep else None
    ents = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for n in names:
        p = os.path.abspath(os.path.join(root, n))
        try:
            if os.path.isdir(p):
                ents.append([os.path.getmtime(p), _tree_size(p), p])
        except OSError:
            pass
    ents.sort()                                         # oldest first
    total = sum(e[1] for e in ents)
    gone = []
    for mt, size, p in ents:
        if len(ents) - len(gone) <= entries and total <= budget:
            break
        if p == keep:
            continue
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p)
        total -= size
    return gone


def prune_files(folder: str, keep: int, keep_path: str | None = None) -> None:
    """Keep only the newest `keep` finished files in a flat cache folder, and
    never `keep_path` - the file about to be served."""
    try:
        files = [os.path.join(folder, n) for n in os.listdir(folder) if not n.endswith(".part")]
        files = sorted((f for f in files if os.path.isfile(f)), key=os.path.getmtime, reverse=True)
    except OSError:
        return
    spare = os.path.abspath(keep_path) if keep_path else None
    for f in files[keep:]:
        if os.path.abspath(f) != spare:
            _unlink(f)


def poster(path: str) -> str:
    """A ~320px JPEG of an early frame, cached by the file's identity."""
    p = os.path.abspath(path)
    folder = poster_root()
    dst = os.path.join(folder, source_key(p) + ".jpg")
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        try:
            os.utime(dst, None)
        except OSError:
            pass
        return dst
    os.makedirs(folder, exist_ok=True)
    with _POSTER_SLOTS:
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return dst                  # another request made it while we queued
        try:
            dur = float(probe(p).get("duration") or 0.0)
        except ZoomcutError:
            dur = 0.0
        tmp = f"{dst}.{os.urandom(4).hex()}.part"
        try:
            # a frame into the clip skips a black first frame; 0 is the fallback
            # for files whose reported duration overshoots the real stream
            for t in dict.fromkeys((min(1.0, dur * 0.3), 0.0)):
                r = util.run([ffmpeg(), "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}",
                              "-i", p, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
                              "-update", "1", "-f", "image2", "-c:v", "mjpeg", tmp])
                if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                    patient(os.replace, tmp, dst)
                    break
            else:
                raise ZoomcutError(f"could not read a frame from {os.path.basename(p)}")
        finally:
            _unlink(tmp)
    prune_files(folder, MAX_POSTERS)
    return dst


def _state(source: str | None = None, state: str = "idle", pct: int = 0, message: str = "",
           proxy: str | None = None, strip: dict | None = None) -> dict:
    return {"source": source, "state": state, "pct": pct, "message": message,
            "proxy": proxy, "strip": strip}


class Prep:
    """The session's one media job.

    A newer source supersedes the running job: its ffmpeg is killed, and a
    generation token makes sure anything it still reports is ignored.
    `allow` is told about every path a finished job publishes.
    """

    def __init__(self, allow: Callable[[str], object] | None = None):
        self.lock = threading.Lock()
        self.gen = 0
        self.proc: subprocess.Popen | None = None
        self.allow = allow or (lambda p: p)
        self.st = _state()

    def status(self) -> dict:
        with self.lock:
            st = dict(self.st)
        if st["strip"]:
            st["strip"] = dict(st["strip"])
        return st

    def ensure(self, source: str, retry: bool = False) -> None:
        """Start on source unless that job is already running or done. `retry`
        (an explicit analyse/load) also re-runs a failed or finished job - a
        finished one is a cache hit, unless the file changed underneath it."""
        src = os.path.abspath(source)
        with self.lock:
            st = self.st["state"]
            same = self.st["source"] == src
        if st == "idle" or not same or (retry and st in ("error", "ready")):
            self.start(src)

    def start(self, source: str) -> None:
        src = os.path.abspath(source)
        with self.lock:
            self.gen += 1
            gen, old = self.gen, self.proc
            self.proc = None
            self.st = _state(src, "working", message="preparing preview")
        _stop(old)
        hit = cached(src) if os.path.isfile(src) else None
        if hit:                                             # ready instantly
            self._publish(gen, hit)
            return
        threading.Thread(target=self._work, args=(gen, src), daemon=True,
                         name="zoomcut-media").start()

    def stop(self) -> None:
        """Abandon whatever is running (server shutdown, tests)."""
        with self.lock:
            self.gen += 1
            old, self.proc = self.proc, None
            self.st = _state()
        _stop(old)

    def _set(self, gen: int, **kw) -> bool:
        with self.lock:
            if gen != self.gen:
                return False
            self.st.update(kw)
            return True

    def _track(self, gen: int, proc: subprocess.Popen) -> None:
        with self.lock:
            if gen == self.gen:
                self.proc = proc
                return
        _stop(proc)                         # superseded before it even started

    def _publish(self, gen: int, res: dict) -> None:
        self.allow(res["proxy"])
        self.allow(res["strip"]["path"])
        self._set(gen, state="ready", pct=100, message="ready",
                  proxy=res["proxy"], strip=res["strip"])

    def _work(self, gen: int, src: str) -> None:
        try:
            res = prepare(src, progress=lambda pct, msg: self._set(gen, pct=pct, message=msg),
                          alive=lambda: self.gen == gen, track=lambda p: self._track(gen, p))
        except Exception as e:
            msg = _last_line(str(e).split("\n")[0]) or type(e).__name__
            self._set(gen, state="error", pct=0, message=msg)
            return
        finally:
            with self.lock:
                if gen == self.gen:
                    self.proc = None
        if res is not None:
            self._publish(gen, res)
