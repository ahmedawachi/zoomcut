"""Shared helpers: platform detection, tool discovery, ffmpeg probing."""
from __future__ import annotations
import json, os, shutil, subprocess, sys, time

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
FROZEN = getattr(sys, "frozen", False)

# Windows: never flash a console window for a helper process
_NO_WINDOW = 0x08000000 if IS_WIN else 0


class ZoomcutError(RuntimeError):
    pass


def platform_name() -> str:
    return "macOS" if IS_MAC else "Windows" if IS_WIN else "Linux" if IS_LINUX else sys.platform


def bundle_dir() -> str:
    """Where our own files live, whether run from source or from a bundle."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _search_paths(name: str) -> list[str]:
    exe = name + (".exe" if IS_WIN else "")
    out = [os.path.join(bundle_dir(), exe), os.path.join(bundle_dir(), "bin", exe)]
    if FROZEN:
        here = os.path.dirname(sys.executable)
        out += [os.path.join(here, exe), os.path.join(here, "bin", exe)]
    if IS_MAC:
        out += [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    elif IS_LINUX:
        out += [f"/usr/bin/{name}", f"/usr/local/bin/{name}", f"/snap/bin/{name}"]
    else:
        out += [rf"C:\Program Files\ffmpeg\bin\{exe}",
                rf"C:\ffmpeg\bin\{exe}",
                os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Links\{exe}")]
    return out


_TOOL_CACHE: dict[str, str] = {}

INSTALL_HINT = {
    "macOS": "brew install ffmpeg",
    "Windows": "winget install Gyan.FFmpeg   (or: choco install ffmpeg)",
    "Linux": "sudo apt install ffmpeg   (or your distro's equivalent)",
}


def tool(name: str) -> str:
    """Absolute path to a helper binary (ffmpeg/ffprobe), or raise."""
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    env = os.environ.get(f"ZOOMCUT_{name.upper()}")
    found = env if env and os.path.isfile(env) else None
    if not found and FROZEN:
        # a packaged build carries an ffmpeg known to work with it; prefer that
        # to whatever else is on PATH
        found = next((p for p in _search_paths(name)[:4] if os.path.isfile(p)), None)
    found = found or shutil.which(name)
    if not found:
        for p in _search_paths(name):
            if os.path.isfile(p):
                found = p
                break
    if not found:
        raise ZoomcutError(
            f"{name} was not found.\n"
            f"Zoomcut needs ffmpeg to read and write video.\n"
            f"Install it with:  {INSTALL_HINT.get(platform_name(), 'see https://ffmpeg.org')}\n"
            f"Or point Zoomcut at it:  ZOOMCUT_{name.upper()}=/path/to/{name}")
    _TOOL_CACHE[name] = found
    return found


def have(name: str) -> bool:
    try:
        tool(name)
        return True
    except ZoomcutError:
        return False


# kept for call sites that only want to assert availability
def require(name: str) -> str:
    return tool(name)


def ffmpeg() -> str:
    return tool("ffmpeg")


def ffprobe() -> str:
    return tool("ffprobe")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    if _NO_WINDOW:
        kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kw)


def popen(cmd: list[str], **kw) -> subprocess.Popen:
    if _NO_WINDOW:
        kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.Popen(cmd, **kw)


def output_dir() -> str:
    """Where finished videos go, following each platform's convention."""
    env = os.environ.get("ZOOMCUT_OUTPUT_DIR")
    if env:
        return os.path.expanduser(env)
    home = os.path.expanduser("~")
    if IS_MAC:
        return os.path.join(home, "Movies", "Zoomcut")
    if IS_WIN:
        base = os.environ.get("USERPROFILE", home)
        vids = os.path.join(base, "Videos")
        return os.path.join(vids if os.path.isdir(vids) else base, "Zoomcut")
    xdg = run(["xdg-user-dir", "VIDEOS"]).stdout.strip() if shutil.which("xdg-user-dir") else ""
    return os.path.join(xdg or os.path.join(home, "Videos"), "Zoomcut")


def cache_dir() -> str:
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Zoomcut", "cache")
    if IS_MAC:
        return os.path.expanduser("~/Library/Caches/zoomcut")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "zoomcut")


def probe(path: str) -> dict:
    """Return {width, height, duration, fps, nb_frames, has_audio} for a media file."""
    p = run([ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    if p.returncode != 0:
        raise ZoomcutError(f"ffprobe failed on {path}:\n{(p.stderr or '').strip()}")
    data = json.loads(p.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ZoomcutError(f"{path} has no video stream")
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": duration,
        "fps": fps,
        "nb_frames": int(video.get("nb_frames") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def patient(fn, *args, seconds: float = 5.0):
    """Run a file operation, retrying while Windows says the file is in use.

    On Windows a file that ffmpeg has only just closed - or that the virus
    scanner is still reading - refuses to be deleted or replaced for a
    moment afterwards, so one attempt fails now and then. Everywhere else a
    single attempt is the whole story."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return fn(*args)
        except PermissionError:
            if not IS_WIN or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v
