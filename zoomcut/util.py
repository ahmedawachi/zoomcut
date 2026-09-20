"""Shared helpers: ffmpeg probing and subprocess plumbing."""
from __future__ import annotations
import json, shutil, subprocess


class ZoomcutError(RuntimeError):
    pass


def require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise ZoomcutError(
            f"{tool!r} not found on PATH. Install it (brew install ffmpeg) and retry."
        )
    return path


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(path: str) -> dict:
    """Return {width, height, duration, fps, nb_frames, has_audio} for a media file."""
    require("ffprobe")
    p = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    if p.returncode != 0:
        raise ZoomcutError(f"ffprobe failed on {path}:\n{p.stderr.strip()}")
    data = json.loads(p.stdout)
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


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v
