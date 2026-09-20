"""Screen recording via macOS's built-in `screencapture`.

Two things about `screencapture -v` matter and are handled here:

  * It writes VARIABLE frame rate and stops the file at the last on-screen
    change, so a recording that ends on a still screen comes back short. We
    measure wall-clock time and clone the final frame to make up the gap -
    otherwise the end of every demo silently disappears.
  * It finalises the file cleanly on SIGINT, which is how stop() works.
"""
from __future__ import annotations
import os, signal, subprocess, tempfile, time
from dataclasses import dataclass, field

from .util import probe, run, ZoomcutError

MODES = ("window", "display", "region", "interactive")


@dataclass
class Recording:
    path: str
    mode: str
    started: float
    proc: subprocess.Popen | None = None
    wall: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def available() -> tuple[bool, str]:
    """Is screen recording usable right now? (probes the TCC permission)"""
    if not os.path.exists("/usr/sbin/screencapture") and not run(["which", "screencapture"]).stdout.strip():
        return False, "screencapture not found (is this macOS?)"
    # NB: screencapture silently refuses to write dot-files (and still exits 0),
    # so the probe name must not start with a dot.
    tmp = os.path.join(tempfile.gettempdir(), f"zoomcut-perm-{os.getpid()}.png")
    p = run(["screencapture", "-x", "-t", "png", "-R", "0,0,8,8", tmp])
    ok = p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
    try:
        os.remove(tmp)
    except OSError:
        pass
    if not ok:
        return False, ("Screen Recording permission is not granted. Give it to your terminal "
                       "in System Settings > Privacy & Security > Screen & System Audio Recording, "
                       "then restart the terminal.")
    return True, "ready"


def start(path: str, mode: str = "display", region: tuple[int, int, int, int] | None = None,
          display: int = 1, window_id: int | None = None, cursor: bool = True,
          clicks: bool = False, audio: bool = False,
          limit: float | None = None) -> Recording:
    if mode not in MODES:
        raise ZoomcutError(f"mode must be one of {MODES}, got {mode!r}")
    ok, why = available()
    if not ok:
        raise ZoomcutError(why)
    if not path.lower().endswith(".mov"):
        path = os.path.splitext(path)[0] + ".mov"
    if os.path.basename(path).startswith("."):
        # screencapture will not write hidden files, and reports success anyway
        raise ZoomcutError(f"output name cannot start with a dot: {os.path.basename(path)}")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if os.path.exists(path):
        os.remove(path)

    cmd = ["screencapture", "-v", "-x"]
    if cursor:
        cmd.append("-C")
    if clicks:
        cmd.append("-k")
    if audio:
        cmd.append("-g")
    if limit:
        cmd.append(f"-V{float(limit):g}")
    if mode == "window":
        if not window_id:
            raise ZoomcutError("window mode needs window_id (see zoomcut windows)")
        from .windows import find
        w = find(int(window_id))
        if w is None:
            raise ZoomcutError(f"window {window_id} is gone - list them again and retry")
        # -o drops the drop shadow, so the capture is exactly the window and
        # our own shadow is the only one in the final frame
        cmd += ["-o", f"-l{int(window_id)}"]
    elif mode == "region":
        if not region or len(region) != 4:
            raise ZoomcutError("region mode needs region=(x, y, w, h)")
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            raise ZoomcutError(f"region must have positive size, got {w}x{h}")
        cmd.append(f"-R{x},{y},{w},{h}")
    elif mode == "display":
        cmd.append(f"-D{int(display)}")
    else:  # interactive - macOS puts up its own region/window picker
        cmd += ["-i", "-Jvideo"]
    cmd.append(path)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return Recording(path=path, mode=mode, started=time.time(), proc=proc,
                     meta={"cmd": cmd, "cursor": cursor, "clicks": clicks,
                           "window_id": window_id, "region": region, "display": display})


def stop(rec: Recording, pad: bool = True, timeout: float = 30.0) -> str:
    """Stop cleanly and return the finished file."""
    if rec.proc is None:
        raise ZoomcutError("recording was never started")
    rec.wall = time.time() - rec.started
    if rec.proc.poll() is None:
        rec.proc.send_signal(signal.SIGINT)
        try:
            rec.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rec.proc.kill()
            rec.proc.wait(timeout=5)
    # screencapture flushes asynchronously; give the file a moment to appear
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(rec.path) and os.path.getsize(rec.path) > 0:
            break
        time.sleep(0.2)
    if not os.path.exists(rec.path) or os.path.getsize(rec.path) == 0:
        err = (rec.proc.stderr.read() or b"").decode(errors="replace")
        raise ZoomcutError(f"recording produced no file. screencapture said:\n{err[:800]}")
    if pad:
        pad_to_wall(rec.path, rec.wall)
    return rec.path


def pad_to_wall(path: str, wall: float, tolerance: float = 0.25) -> str:
    """Clone the last frame so the file lasts as long as the recording did."""
    try:
        info = probe(path)
    except ZoomcutError:
        return path
    gap = float(wall) - float(info["duration"] or 0.0)
    if gap <= tolerance or wall <= 0:
        return path
    tmp = path + ".pad.mov"
    p = run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path,
             "-vf", f"tpad=stop_mode=clone:stop_duration={gap:.3f}",
             "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", tmp])
    if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, path)
    else:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path
