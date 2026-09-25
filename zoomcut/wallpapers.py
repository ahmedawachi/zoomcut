"""Find the desktop wallpapers installed on this machine and cache them as PNGs.

    macOS    /System/Library/Desktop Pictures  (.heic stills, .mov animated ones)
    Windows  C:\\Windows\\Web\\**               (.jpg/.png) + the current wallpaper
    Linux    /usr/share/backgrounds, /usr/share/wallpapers, ~/.local/share/...

Anything Pillow can already open is used as-is; only the formats it cannot
(macOS HEIC, animated .mov wallpapers) get converted once and cached.
"""
from __future__ import annotations
import glob, os, shutil
import threading
from pathlib import Path

from .util import IS_MAC, IS_WIN, IS_LINUX, ffmpeg, run, cache_dir, probe, ZoomcutError

CACHE = Path(cache_dir()) / "wallpapers"
# the animated wallpapers are a whole day cycle; this is the bright daytime part
DYNAMIC_FRAME_RATIO = 0.42
MAX_WIDTH = 3840
DIRECT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip().replace(" ", "_")


def _add(found: dict, path: str, kind: str) -> None:
    stem = Path(path).stem
    if stem and stem not in found:
        found[stem] = {"name": stem, "kind": kind, "source": path}


def _discover_macos(found: dict) -> None:
    base = Path("/System/Library/Desktop Pictures")
    for p in sorted(glob.glob(str(base / "*.heic"))):
        _add(found, p, "still")
    hidden = base / ".wallpapers"
    for p in sorted(glob.glob(str(hidden / "*" / "*.heic"))):
        _add(found, p, "still")
    for p in sorted(glob.glob(str(hidden / "*" / "*.mov"))):
        _add(found, p, "dynamic")
    extra = Path(os.path.expanduser("~/Library/Application Support/com.apple.mobileAssetDesktop"))
    for p in sorted(glob.glob(str(extra / "**" / "*.heic"), recursive=True)):
        _add(found, p, "still")


def _discover_windows(found: dict) -> None:
    roots = [os.path.expandvars(r"%SystemRoot%\Web\Wallpaper"),
             os.path.expandvars(r"%SystemRoot%\Web\4K\Wallpaper"),
             os.path.expandvars(r"%SystemRoot%\Web\Screen")]
    for root in roots:
        for ext in ("jpg", "jpeg", "png"):
            for p in sorted(glob.glob(os.path.join(root, "**", f"*.{ext}"), recursive=True)):
                _add(found, p, "still")
    # whatever is on the desktop right now (Windows keeps a decoded copy)
    cur = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Themes\TranscodedWallpaper")
    if os.path.isfile(cur):
        found.setdefault("Current desktop", {"name": "Current desktop", "kind": "raw",
                                             "source": cur})


def _discover_linux(found: dict) -> None:
    roots = ["/usr/share/backgrounds", "/usr/share/wallpapers",
             "/usr/share/pixmaps/backgrounds",
             os.path.expanduser("~/.local/share/backgrounds"),
             os.path.expanduser("~/Pictures/Wallpapers")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for ext in ("jpg", "jpeg", "png", "webp"):
            for p in sorted(glob.glob(os.path.join(root, "**", f"*.{ext}"), recursive=True))[:400]:
                _add(found, p, "still")
    if shutil.which("gsettings"):
        uri = run(["gsettings", "get", "org.gnome.desktop.background", "picture-uri"]).stdout
        uri = (uri or "").strip().strip("'\"")
        if uri.startswith("file://"):
            from urllib.parse import unquote
            p = unquote(uri[7:])
            if os.path.isfile(p):
                found.setdefault("Current desktop", {"name": "Current desktop",
                                                     "kind": "still", "source": p})


def discover() -> list[dict]:
    """[{name, kind, source, cached, ready}] for every wallpaper we can decode."""
    found: dict[str, dict] = {}
    try:
        if IS_MAC:
            _discover_macos(found)
        elif IS_WIN:
            _discover_windows(found)
        elif IS_LINUX:
            _discover_linux(found)
    except OSError:
        pass
    out = []
    for w in found.values():
        direct = Path(w["source"]).suffix.lower() in DIRECT_SUFFIXES
        w["cached"] = w["source"] if direct else str(CACHE / (_safe(w["name"]) + ".png"))
        w["ready"] = direct or os.path.exists(w["cached"])
        out.append(w)
    return sorted(out, key=lambda w: w["name"].lower())


def resolve(name_or_path: str) -> dict:
    """Find a wallpaper by name, fuzzily, or accept a path."""
    want = str(name_or_path).strip().lower()
    catalog = discover()
    entry = None
    for w in catalog:
        if w["name"].lower() == want or w["source"] == name_or_path:
            entry = w
            break
    if entry is None:
        # fuzzy: "Tahoe Light" should still find "Tahoe Day". Fragments shorter
        # than three characters match half the catalogue, so they are ignored.
        tokens = [t for t in want.replace("-", " ").replace("_", " ").split() if len(t) >= 3]
        scored = []
        for w in catalog:
            hits = sum(1 for t in tokens if t in w["name"].lower())
            if hits:
                scored.append((hits, -len(w["name"]), w))
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
            raise ZoomcutError(f"no wallpaper named {name_or_path!r} on this machine")
    return entry


def materialise(name_or_path: str) -> str:
    """A full-size path Pillow can open. Only for rendering - a thumbnail must
    use thumbnail(), which never decodes the whole image."""
    if os.path.isfile(name_or_path) and Path(name_or_path).suffix.lower() in DIRECT_SUFFIXES:
        return name_or_path
    entry = resolve(name_or_path)

    if Path(entry["source"]).suffix.lower() in DIRECT_SUFFIXES:
        return entry["source"]

    final = Path(entry["cached"])
    if final.exists() and final.stat().st_size > 0:
        return str(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    src = entry["source"]
    # decode into a private temp name and move it into place when complete:
    # a second caller must never open a half-written cache file
    dst = final.with_name(f"{final.stem}.{os.getpid()}.{threading.get_ident()}.part.png")

    if entry["kind"] == "dynamic":
        dur = probe(src)["duration"] or 1.0
        p = run([ffmpeg(), "-nostdin", "-v", "error", "-ss", f"{dur * DYNAMIC_FRAME_RATIO:.2f}",
                 "-i", src, "-frames:v", "1", "-y", str(dst)])
        if p.returncode != 0 or not dst.exists():
            dst.unlink(missing_ok=True)
            raise ZoomcutError(f"could not extract a frame from {src}:\n{(p.stderr or '')[:400]}")
        return _publish(dst, final)

    if IS_MAC and shutil.which("sips"):
        p = run(["sips", "-s", "format", "png", "--resampleWidth", str(MAX_WIDTH),
                 src, "--out", str(dst)])
        if p.returncode == 0 and dst.exists():
            return _publish(dst, final)
    # ffmpeg reads HEIC and most raw formats too
    p = run([ffmpeg(), "-nostdin", "-v", "error", "-i", src, "-frames:v", "1", "-y", str(dst)])
    if p.returncode != 0 or not dst.exists():
        try:                                    # last resort: let Pillow try
            from PIL import Image
            Image.open(src).convert("RGB").save(dst, "PNG")
        except Exception:
            dst.unlink(missing_ok=True)
            raise ZoomcutError(f"could not decode wallpaper {src}")
    return _publish(dst, final)


def _publish(tmp: Path, final: Path) -> str:
    os.replace(tmp, final)
    return str(final)


def default_name() -> str | None:
    """The wallpaper this OS is best known for, else anything we found."""
    names = [w["name"] for w in discover()]
    if not names:
        return None
    lower = {n.lower(): n for n in names}
    for want in ("tahoe day", "tahoe", "sonoma horizon", "sonoma",
                 "current desktop", "img0", "windows", "warty-final-ubuntu"):
        if want in lower:
            return lower[want]
    for n in names:
        if any(k in n.lower() for k in ("tahoe", "sequoia", "sonoma", "bloom", "windows")):
            return n
    return names[0]


def _cover_resize(im, w: int, h: int):
    from PIL import Image
    im = im.convert("RGB")
    tr, ir = w / h, im.width / im.height
    if ir > tr:
        nw = int(im.height * tr)
        im = im.crop(((im.width - nw) // 2, 0, (im.width + nw) // 2, im.height))
    else:
        nh = int(im.width / tr)
        im = im.crop((0, (im.height - nh) // 2, im.width, (im.height + nh) // 2))
    return im.resize((w, h), Image.LANCZOS)


def thumbnail(name_or_path: str, w: int = 224, h: int = 126) -> str:
    """A small cached JPEG of a wallpaper.

    Deliberately does NOT go through materialise(): decoding a 6000px HEIC to
    a full-resolution PNG just to show a 224px tile filled hundreds of
    megabytes of cache the first time the picker was opened. Each format is
    routed to whatever can downscale it cheaply.
    """
    from PIL import Image
    entry = resolve(name_or_path)
    # "_rgb": dynamic thumbnails used to go straight to JPEG, which read the
    # video's BT.709 colour as BT.601 and came out warmer than the render
    tag = "_rgb" if entry["kind"] == "dynamic" else ""
    dst = Path(cache_dir()) / "thumbs" / f"{_safe(entry['name'])}_{w}x{h}{tag}.jpg"
    if dst.exists() and dst.stat().st_size > 0:
        return str(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = entry["source"]
    suffix = Path(src).suffix.lower()

    # already a normal image: Pillow can decode straight to roughly the size
    if suffix in DIRECT_SUFFIXES:
        im = Image.open(src)
        im.draft("RGB", (w * 2, h * 2))          # JPEG decodes downscaled
        _cover_resize(im, w, h).save(dst, "JPEG", quality=82)
        return str(dst)

    # animated wallpaper: one scaled frame out of the middle of the day
    if entry["kind"] == "dynamic":
        try:
            dur = probe(src)["duration"] or 1.0
        except ZoomcutError:
            dur = 1.0
        # through RGB, exactly as materialise() decodes it for the render, so
        # the preview's background is the colour the export's will be
        tmp = dst.with_suffix(".src.png")
        p = run([ffmpeg(), "-nostdin", "-v", "error", "-ss",
                 f"{dur * DYNAMIC_FRAME_RATIO:.2f}", "-i", src, "-frames:v", "1",
                 "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                        f"crop={w}:{h},format=rgb24", "-y", str(tmp)])
        if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            Image.open(tmp).convert("RGB").save(dst, "JPEG", quality=88)
            tmp.unlink(missing_ok=True)
            return str(dst)
        tmp.unlink(missing_ok=True)

    # HEIC: sips resamples as it decodes. ffmpeg refuses a simple -vf on
    # Apple's tiled HEICs ("simple and complex filtering cannot be used
    # together"), so it only gets a turn without one.
    if shutil.which("sips"):
        tmp = dst.with_suffix(".src.jpg")
        p = run(["sips", "-s", "format", "jpeg", "-Z", str(max(w, h) * 2),
                 src, "--out", str(tmp)])
        if p.returncode == 0 and tmp.exists():
            _cover_resize(Image.open(tmp), w, h).save(dst, "JPEG", quality=82)
            tmp.unlink(missing_ok=True)
            return str(dst)
        tmp.unlink(missing_ok=True)
    tmp = dst.with_suffix(".src.png")
    p = run([ffmpeg(), "-nostdin", "-v", "error", "-i", src, "-frames:v", "1",
             "-y", str(tmp)])
    if p.returncode == 0 and tmp.exists():
        _cover_resize(Image.open(tmp), w, h).save(dst, "JPEG", quality=82)
        tmp.unlink(missing_ok=True)
        return str(dst)
    tmp.unlink(missing_ok=True)
    try:
        _cover_resize(Image.open(src), w, h).save(dst, "JPEG", quality=82)
        return str(dst)
    except Exception as e:
        raise ZoomcutError(f"could not make a thumbnail for {src}: {e}")
