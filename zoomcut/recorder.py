"""Screen recording, with a backend per platform.

    macOS    screencapture      (native, captures a window by id)
    Windows  ffmpeg gdigrab     (desktop, a region, or a window by title)
    Linux    ffmpeg x11grab     (desktop or a region; a window becomes its region)

All of them produce a file Zoomcut can analyse, and all of them stop cleanly.
Two platform quirks are handled here rather than surprising you later:

  * macOS `screencapture -v` writes VARIABLE frame rate and ends the file at
    the last on-screen change, so a recording that finishes on a still screen
    comes back short. We measure wall-clock time and clone the final frame.
  * macOS `screencapture` silently refuses to write dot-files and still exits
    0, so those names are rejected up front.
"""
from __future__ import annotations
import os, signal, subprocess, tempfile, time
from dataclasses import dataclass, field

from .util import (IS_MAC, IS_WIN, IS_LINUX, ZoomcutError, ffmpeg, probe, run, popen,
                   platform_name, have)
from . import winlist

MODES = ("window", "display", "region", "interactive")
CAPTURE_FPS = 30


@dataclass
class Recording:
    path: str
    mode: str
    started: float
    proc: subprocess.Popen | None = None
    backend: str = ""
    wall: float = 0.0
    errlog: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def errors(self) -> str:
        """Whatever the recorder printed, however it was captured."""
        if self.errlog and os.path.exists(self.errlog):
            try:
                with open(self.errlog, errors="replace") as f:
                    return f.read()
            except OSError:
                return ""
        if self.proc is not None and self.proc.stderr is not None:
            try:
                return (self.proc.stderr.read() or b"").decode(errors="replace")
            except Exception:
                return ""
        return ""


def backend_name() -> str:
    return "screencapture" if IS_MAC else "gdigrab" if IS_WIN else "x11grab" if IS_LINUX else ""


def _ffmpeg_devices() -> str:
    try:
        return run([ffmpeg(), "-hide_banner", "-devices"]).stdout or ""
    except ZoomcutError:
        return ""


def available() -> tuple[bool, str]:
    """Can we record right now? Second element explains why not."""
    if IS_MAC:
        if not run(["which", "screencapture"]).stdout.strip() and \
           not os.path.exists("/usr/sbin/screencapture"):
            return False, "screencapture not found (is this really macOS?)"
        # NB: screencapture will not write dot-files, so the probe must not use one
        tmp = os.path.join(tempfile.gettempdir(), f"zoomcut-perm-{os.getpid()}.png")
        try:
            # bounded: a probe waiting on a permission prompt must not stall
            # every request that asks whether we can record
            p = run(["screencapture", "-x", "-t", "png", "-R", "0,0,8,8", tmp], timeout=10)
            ok = p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
        except subprocess.TimeoutExpired:
            ok = False
        try:
            os.remove(tmp)
        except OSError:
            pass
        if not ok:
            return False, ("Screen Recording permission is not granted. Give it to this app in "
                           "System Settings > Privacy & Security > Screen & System Audio "
                           "Recording, then restart it.")
        return True, "ready"

    if not have("ffmpeg"):
        return False, "ffmpeg was not found - Zoomcut needs it to capture the screen."
    devices = _ffmpeg_devices()
    if IS_WIN:
        if "gdigrab" not in devices:
            return False, "this ffmpeg build has no gdigrab device, so it cannot capture the screen."
        return True, "ready"
    if IS_LINUX:
        if "x11grab" not in devices:
            return False, "this ffmpeg build has no x11grab device, so it cannot capture the screen."
        if not os.environ.get("DISPLAY"):
            return False, ("no X11 display found (DISPLAY is unset). Zoomcut captures through "
                           "X11; on a Wayland session, log in with 'GNOME on Xorg' or run under "
                           "XWayland.")
        if (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
            return True, ("ready, but this is a Wayland session - X11 capture usually only sees "
                          "XWayland windows. Recording the whole display may come out black.")
        return True, "ready"
    return False, f"screen recording is not supported on {platform_name()}"


def _even(n: int) -> int:
    """h.264 needs even dimensions."""
    n = int(n)
    return n - (n % 2)


def _prepare_path(path: str, ext: str) -> str:
    if not path.lower().endswith(ext):
        path = os.path.splitext(path)[0] + ext
    if IS_MAC and os.path.basename(path).startswith("."):
        raise ZoomcutError(
            f"output name cannot start with a dot: {os.path.basename(path)} "
            "(screencapture refuses to write hidden files, and reports success anyway)")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    return path


# ---------------------------------------------------------------- macOS
def _start_macos(path, mode, region, display, window_id, cursor, clicks, audio, limit):
    path = _prepare_path(path, ".mov")
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
        # -o drops the window's own drop shadow, so ours is the only one
        cmd += ["-o", f"-l{int(window_id)}"]
    elif mode == "region":
        x, y, w, h = region
        cmd.append(f"-R{int(x)},{int(y)},{int(w)},{int(h)}")
    elif mode == "display":
        cmd.append(f"-D{int(display)}")
    else:
        cmd += ["-i", "-Jvideo"]
    cmd.append(path)
    return path, popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE), cmd, None


# ---------------------------------------------------------------- ffmpeg backends
def _ffmpeg_record_cmd(path, grab, inp, size, offset, cursor, limit, extra=None):
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", grab, "-framerate", str(CAPTURE_FPS),
           "-draw_mouse", "1" if cursor else "0"]
    if size:
        cmd += ["-video_size", f"{_even(size[0])}x{_even(size[1])}"]
    if offset and grab == "gdigrab":
        cmd += ["-offset_x", str(int(offset[0])), "-offset_y", str(int(offset[1]))]
    cmd += list(extra or [])
    cmd += ["-i", inp]
    if limit:
        cmd += ["-t", f"{float(limit):g}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", path]
    return cmd


def _cmd_windows(path, mode, region, display, window_id, cursor, limit):
    """Build the gdigrab command. Split out from _start_windows so it can be
    unit-tested from any platform."""
    size = offset = None
    inp = "desktop"
    if mode == "region":
        x, y, w, h = region
        offset, size = (x, y), (w, h)
    elif mode == "window":
        w = winlist.find(window_id)
        if w is None:
            raise ZoomcutError(f"window {window_id} is gone - list them again and retry")
        if w["title"]:
            inp = f"title={w['title']}"
        else:
            offset, size = (w["x"], w["y"]), (w["width"], w["height"])
    elif mode == "display":
        ss = winlist.screen_size()
        if ss:
            size = ss
    return _ffmpeg_record_cmd(path, "gdigrab", inp, size, offset, cursor, limit)


def _open_errlog(path: str):
    log = path + ".log"
    return log, open(log, "wb")


def _start_windows(path, mode, region, display, window_id, cursor, clicks, audio, limit):
    path = _prepare_path(path, ".mkv")
    cmd = _cmd_windows(path, mode, region, display, window_id, cursor, limit)
    log, fh = _open_errlog(path)
    return path, popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                       stderr=fh), cmd, log


def _cmd_linux(path, mode, region, display, window_id, cursor, limit):
    """Build the x11grab command (see _cmd_windows for why it is separate)."""
    disp = os.environ.get("DISPLAY", ":0.0")
    if mode == "region":
        x, y, w, h = region
    elif mode == "window":
        win = winlist.find(window_id)
        if win is None:
            raise ZoomcutError(f"window {window_id} is gone - list them again and retry")
        x, y, w, h = win["x"], win["y"], win["width"], win["height"]
    else:
        ss = winlist.screen_size()
        if not ss:
            raise ZoomcutError(
                "could not work out the display size (install xdpyinfo or xrandr), "
                "so please record a region instead: --mode region --region x,y,w,h")
        x, y, w, h = 0, 0, ss[0], ss[1]
    inp = f"{disp}+{int(x)},{int(y)}"
    return _ffmpeg_record_cmd(path, "x11grab", inp, (w, h), None, cursor, limit)


def _start_linux(path, mode, region, display, window_id, cursor, clicks, audio, limit):
    path = _prepare_path(path, ".mkv")
    cmd = _cmd_linux(path, mode, region, display, window_id, cursor, limit)
    log, fh = _open_errlog(path)
    return path, popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                       stderr=fh), cmd, log


_BACKENDS = {"macOS": _start_macos, "Windows": _start_windows, "Linux": _start_linux}


# ---------------------------------------------------------------- public
def start(path: str, mode: str = "window", region=None, display: int = 1,
          window_id: int | None = None, cursor: bool = True, clicks: bool = False,
          audio: bool = False, limit: float | None = None) -> Recording:
    if mode not in MODES:
        raise ZoomcutError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "interactive" and not IS_MAC:
        raise ZoomcutError("'interactive' picking is macOS only - "
                           "use --mode window, display or region here")
    if mode == "region" and (not region or len(region) != 4):
        raise ZoomcutError("region mode needs region=(x, y, w, h)")
    if mode == "region" and (int(region[2]) <= 0 or int(region[3]) <= 0):
        raise ZoomcutError(f"region must have positive size, got {region[2]}x{region[3]}")
    if mode == "window":
        if not window_id:
            raise ZoomcutError("window mode needs window_id (see: zoomcut windows)")
        try:
            if winlist.find(window_id) is None:
                raise ZoomcutError(
                    f"window {window_id} is gone - list them again and retry")
        except winlist.WindowListError:
            pass          # cannot enumerate here; let the backend try anyway
    ok, why = available()
    if not ok:
        raise ZoomcutError(why)

    fn = _BACKENDS.get(platform_name())
    if fn is None:
        raise ZoomcutError(f"screen recording is not supported on {platform_name()}")
    path, proc, cmd, errlog = fn(path, mode, region, display, window_id,
                                 cursor, clicks, audio, limit)

    rec = Recording(path=path, mode=mode, started=time.time(), proc=proc,
                    backend=backend_name(), errlog=errlog,
                    meta={"cmd": cmd, "cursor": cursor, "clicks": clicks,
                          "window_id": window_id, "region": region, "display": display})
    # an ffmpeg backend that cannot open its input dies immediately; surface
    # that now instead of handing back a Recording that was never recording
    if not IS_MAC:
        time.sleep(0.7)
        if proc.poll() is not None:
            err = rec.errors().strip()
            if mode == "window" and IS_WIN:
                # matching by window title failed; its rectangle always works
                w = winlist.find(window_id)
                if w:
                    return start(path, "region", (w["x"], w["y"], w["width"], w["height"]),
                                 display, None, cursor, clicks, audio, limit)
            raise ZoomcutError(f"the recorder stopped immediately:\n{err[:800]}")
    return rec


def stop(rec: Recording, pad: bool = True, timeout: float = 30.0) -> str:
    """Stop cleanly and return the finished file."""
    if rec.proc is None:
        raise ZoomcutError("recording was never started")
    rec.wall = time.time() - rec.started
    if rec.proc.poll() is None:
        try:
            if IS_MAC:
                rec.proc.send_signal(signal.SIGINT)
            else:
                # ffmpeg finalises the container when told 'q' on stdin
                try:
                    rec.proc.stdin.write(b"q")
                    rec.proc.stdin.flush()
                except (BrokenPipeError, OSError, AttributeError):
                    rec.proc.terminate()
            rec.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rec.proc.terminate()
            try:
                rec.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rec.proc.kill()
                rec.proc.wait(timeout=5)
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(rec.path) and os.path.getsize(rec.path) > 0:
            break
        time.sleep(0.2)
    if not os.path.exists(rec.path) or os.path.getsize(rec.path) == 0:
        raise ZoomcutError(
            f"recording produced no file. The recorder said:\n{rec.errors()[:800]}")
    if rec.errlog and os.path.exists(rec.errlog):
        try:
            os.remove(rec.errlog)
        except OSError:
            pass
    if pad and IS_MAC:
        pad_to_wall(rec.path, rec.wall)
    return rec.path


def pad_to_wall(path: str, wall: float, tolerance: float = 0.25) -> str:
    """Clone the last frame so the file lasts as long as the recording did.

    Only the macOS backend needs this: it writes variable frame rate and stops
    the file at the last on-screen change. The ffmpeg backends are constant
    frame rate and already truthful.
    """
    try:
        info = probe(path)
    except ZoomcutError:
        return path
    gap = float(wall) - float(info["duration"] or 0.0)
    if gap <= tolerance or wall <= 0:
        return path
    tmp = path + ".pad.mov"
    p = run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", path,
             "-vf", f"tpad=stop_mode=clone:stop_duration={gap:.3f}",
             "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", tmp])
    if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, path)
    elif os.path.exists(tmp):
        os.remove(tmp)
    return path
