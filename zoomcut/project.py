"""The project file: everything needed to re-render a clip, as plain JSON.

It is produced automatically from a recording (source + analysis + camera
plan + style) and is meant to be editable by hand or by the UI.
"""
from __future__ import annotations
import json, os
from dataclasses import dataclass

from .director import DirectorConfig, Shot
from .util import probe

SCHEMA = 1

PRESETS = {
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
    "4k":    (3840, 2160),
}

DEFAULT_STYLE = {
    "background": {
        "type": "wallpaper",          # wallpaper | image | gradient | color
        "name": None,                 # wallpaper name; None -> this Mac's default
        "path": None,                 # for type=image
        "gradient": {"from": "#3F37C9", "to": "#8C87DF",
                     "p0": [0.0, 0.0], "p1": [1.0, 1.0]},
        "color": "#3F37C9",
        "blur": 0.0,
        "dim": 0.12,
    },
    "paddingRatio": 0.0702,           # of the short output edge, per side
    "cornerRadius": 0.0120,           # of the window width
    "shadow": {"distance": 0.0271, "blur": 0.0215, "alpha": 0.52,
               "color": [12, 10, 34]},
    "edgeHighlight": 0.13,
}

DEFAULT_OUTPUT = {"width": 2560, "height": 1440, "fps": 60, "crf": 17, "preset": "slow"}

# Camera spring. zeta ~0.80: fast settle with a barely-there overshoot, which
# reads as deliberate rather than mechanical.
DEFAULT_SPRING = {"mass": 2.25, "stiffness": 200.0, "damping": 36.0}


def new_project(source: str, keys: list[list[float]], shots: list[Shot] | None = None,
                style: dict | None = None, output: dict | None = None,
                director: DirectorConfig | None = None, trim: list | None = None) -> dict:
    info = probe(source)
    st = json.loads(json.dumps(DEFAULT_STYLE))
    if style:
        _deep_update(st, style)
    out = dict(DEFAULT_OUTPUT)
    if output:
        out.update(output)
    return {
        "schema": SCHEMA,
        "source": os.path.abspath(source),
        "sourceInfo": info,
        "trim": trim or [0.0, None],
        "output": out,
        "style": st,
        "camera": {
            "spring": dict(DEFAULT_SPRING),
            "keys": keys,
            "shots": [s.to_dict() for s in (shots or [])],
            "director": (director or DirectorConfig()).to_dict(),
        },
    }


def _deep_update(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v
    return dst


def save(project: dict, path: str) -> str:
    with open(path, "w") as f:
        json.dump(project, f, indent=2)
    return path


def load(path: str) -> dict:
    with open(path) as f:
        p = json.load(f)
    if p.get("schema") != SCHEMA:
        raise ValueError(f"{path}: unsupported project schema {p.get('schema')!r}")
    return p


# --------------------------------------------------------------------------
# Style presets
# --------------------------------------------------------------------------
def import_style_preset(data: dict) -> dict:
    """Read look settings out of an editor project JSON.

    Understands the common shape used by screen-recording editors: a `config`
    object holding background / padding / corner-radius / shadow / spring
    settings, and optional `scenes[].zoomRanges` describing manual zooms.
    Accepts either a wrapped export ({"json": {...}, ...}) or the inner object,
    and ignores anything it cannot map.

    Returns {"style": {...}, "spring": {...} | None, "manualKeys": [...]}.
    """
    root = data.get("json", data)
    cfg = root.get("config", {}) or {}
    style: dict = {"background": {}}

    grad = cfg.get("backgroundGradient") or {}
    stops = grad.get("stops") or []
    if len(stops) >= 2:
        style["background"]["gradient"] = {
            "from": stops[0].get("color", "#3F37C9"),
            "to": stops[-1].get("color", "#8C87DF"),
            "p0": [grad.get("start", {}).get("x", 0.0), grad.get("start", {}).get("y", 0.0)],
            "p1": [grad.get("end", {}).get("x", 1.0), grad.get("end", {}).get("y", 1.0)],
        }
    btype = cfg.get("backgroundType")
    if btype == "system" and cfg.get("backgroundSystemName"):
        # e.g. "macOS/tahoe-light.jpg" -> "tahoe"
        stem = os.path.splitext(os.path.basename(cfg["backgroundSystemName"]))[0]
        style["background"]["type"] = "wallpaper"
        style["background"]["name"] = stem.replace("-", " ").title()
    elif btype in ("gradient", "color"):
        style["background"]["type"] = btype
        if cfg.get("backgroundColor"):
            style["background"]["color"] = cfg["backgroundColor"]
    if cfg.get("backgroundBlur") is not None:
        style["background"]["blur"] = float(cfg["backgroundBlur"])

    if cfg.get("backgroundPaddingRatio") is not None:
        # stored as a percentage in the source format
        style["paddingRatio"] = max(0.0, min(0.25, float(cfg["backgroundPaddingRatio"]) / 142.0))
    if cfg.get("windowBorderRadius") is not None:
        style["cornerRadius"] = max(0.0, float(cfg["windowBorderRadius"]) / 1000.0)

    sh = {}
    if cfg.get("shadowIntensity") is not None:
        sh["alpha"] = max(0.0, min(1.0, float(cfg["shadowIntensity"]) * 0.7))
    if cfg.get("shadowDistance") is not None:
        sh["distance"] = float(cfg["shadowDistance"]) / 922.0
    if cfg.get("shadowBlur") is not None:
        sh["blur"] = float(cfg["shadowBlur"]) / 930.0
    if sh:
        style["shadow"] = sh

    spring = None
    sm = cfg.get("screenMovementSpring") or {}
    if sm:
        spring = {"mass": float(sm.get("mass", 2.25)),
                  "stiffness": float(sm.get("stiffness", 200.0)),
                  "damping": float(sm.get("damping", 40.0))}

    manual_keys: list[list[float]] = []
    for scene in root.get("scenes", []) or []:
        for zr in scene.get("zoomRanges", []) or []:
            if zr.get("isDisabled"):
                continue
            pt = zr.get("manualTargetPoint") or {"x": 0.5, "y": 0.5}
            z = float(zr.get("zoom", 1.0))
            t0 = float(zr.get("startTime", 0)) / 1000.0
            t1 = float(zr.get("endTime", 0)) / 1000.0
            if t1 > t0:
                manual_keys.append([round(t0, 3), z, float(pt["x"]), float(pt["y"])])
                manual_keys.append([round(t1, 3), z, float(pt["x"]), float(pt["y"])])

    return {"style": style, "spring": spring, "manualKeys": manual_keys}
