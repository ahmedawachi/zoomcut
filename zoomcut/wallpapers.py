"""Find the macOS wallpapers installed on this Mac and cache them as PNGs.

macOS ships stills as .heic and the newer animated ones (Tahoe, Sonoma) as
.mov. Both are decoded once into ~/.cache/zoomcut/wallpapers and reused.
"""
from __future__ import annotations
import os, glob, subprocess
from pathlib import Path

from .util import run, ZoomcutError

SYSTEM_DIR = Path("/System/Library/Desktop Pictures")
CACHE = Path(os.path.expanduser("~/.cache/zoomcut/wallpapers"))
# the animated wallpapers are a full day cycle; this is the bright daytime part
DYNAMIC_FRAME_RATIO = 0.42
MAX_WIDTH = 3840


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip().replace(" ", "_")


def discover() -> list[dict]:
    """[{name, kind, source, cached}] for every wallpaper we can decode."""
    found: dict[str, dict] = {}
    for p in sorted(glob.glob(str(SYSTEM_DIR / "*.heic"))):
        found[Path(p).stem] = {"name": Path(p).stem, "kind": "still", "source": p}
    hidden = SYSTEM_DIR / ".wallpapers"
    for p in sorted(glob.glob(str(hidden / "*" / "*.heic"))):
        found.setdefault(Path(p).stem, {"name": Path(p).stem, "kind": "still", "source": p})
    for p in sorted(glob.glob(str(hidden / "*" / "*.mov"))):
        found.setdefault(Path(p).stem, {"name": Path(p).stem, "kind": "dynamic", "source": p})
    extra = Path(os.path.expanduser("~/Library/Application Support/com.apple.mobileAssetDesktop"))
    for p in sorted(glob.glob(str(extra / "**" / "*.heic"), recursive=True)):
        found.setdefault(Path(p).stem, {"name": Path(p).stem, "kind": "still", "source": p})
    out = []
    for w in found.values():
        w["cached"] = str(CACHE / (_safe(w["name"]) + ".png"))
        w["ready"] = os.path.exists(w["cached"])
        out.append(w)
    return sorted(out, key=lambda w: w["name"].lower())


def materialise(name_or_path: str) -> str:
    """Return a PNG path for a wallpaper name (or pass through an image path)."""
    if os.path.isfile(name_or_path) and not name_or_path.lower().endswith((".heic", ".mov")):
        return name_or_path
    entry = None
    want = str(name_or_path).strip().lower()
    catalog = discover()
    for w in catalog:                                   # exact
        if w["name"].lower() == want or w["source"] == name_or_path:
            entry = w
            break
    if entry is None:                                   # fuzzy: "Tahoe Light" -> "Tahoe Day"
        # ignore stop-word-ish fragments: a bare "a" matches half the catalogue
        tokens = [t for t in want.replace("-", " ").replace("_", " ").split() if len(t) >= 3]
        scored = []
        for w in catalog:
            name = w["name"].lower()
            hits = sum(1 for t in tokens if t in name)
            if hits:
                scored.append((hits, -len(name), w))
        if scored:
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            entry = scored[0][2]
    if entry is None:
        if os.path.isfile(name_or_path):
            entry = {"name": Path(name_or_path).stem,
                     "kind": "dynamic" if name_or_path.lower().endswith(".mov") else "still",
                     "source": name_or_path,
                     "cached": str(CACHE / (_safe(Path(name_or_path).stem) + ".png"))}
        else:
            raise ZoomcutError(f"no wallpaper named {name_or_path!r} on this Mac")
    dst = Path(entry["cached"])
    if dst.exists() and dst.stat().st_size > 0:
        return str(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = entry["source"]
    if entry["kind"] == "dynamic":
        from .util import probe
        dur = probe(src)["duration"] or 1.0
        p = run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{dur * DYNAMIC_FRAME_RATIO:.2f}",
                 "-i", src, "-frames:v", "1", "-y", str(dst)])
        if p.returncode != 0 or not dst.exists():
            raise ZoomcutError(f"could not extract a frame from {src}:\n{p.stderr[:400]}")
    else:
        p = run(["sips", "-s", "format", "png", "--resampleWidth", str(MAX_WIDTH),
                 src, "--out", str(dst)])
        if p.returncode != 0 or not dst.exists():
            # sips refuses some HDR heics; ffmpeg can usually still read them
            p2 = run(["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-frames:v", "1",
                      "-y", str(dst)])
            if p2.returncode != 0 or not dst.exists():
                raise ZoomcutError(f"could not decode wallpaper {src}:\n{p.stderr[:300]}")
    return str(dst)


def default_name() -> str | None:
    """Prefer the current OS's headline wallpaper, else any Tahoe/Sonoma one."""
    names = [w["name"] for w in discover()]
    for want in ("Tahoe Day", "Tahoe", "Sonoma Horizon", "Sonoma"):
        for n in names:
            if n.lower() == want.lower():
                return n
    for n in names:
        if "tahoe" in n.lower():
            return n
    return names[0] if names else None
