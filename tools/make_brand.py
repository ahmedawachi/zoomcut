#!/usr/bin/env python3
"""Regenerate the Zoomcut brand assets in docs/.

    python3 tools/make_brand.py

The mark is a screen with a zoom region inside it. The wordmark uses the same
indigo -> lavender gradient as the app's header, so the README, the app and the
icon all read as one thing.
"""
from __future__ import annotations
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
INK = (233, 234, 240)
DIM = (150, 154, 174)
BG = (15, 16, 20)
G0 = (108, 99, 255)      # indigo
G1 = (150, 140, 240)     # lavender
SS = 4                   # supersampling


def font(size: int, weight: str = "Bold"):
    try:
        f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
        f.set_variation_by_name(weight)
        return f
    except Exception:
        idx = {"Heavy": 8, "Bold": 0, "Medium": 5, "Regular": 7}.get(weight, 0)
        return ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", size, index=idx)


def gradient(w: int, h: int, c0=G0, c1=G1, diagonal=True) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = ((x / max(w - 1, 1)) + (y / max(h - 1, 1))) / 2 if diagonal else x / max(w - 1, 1)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c0, c1))
    return img


def mark(size: int) -> Image.Image:
    """The logo mark: a screen with a zoom region framed inside it."""
    s = size * SS
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    pad = s * 0.055
    stroke = s * 0.085
    # the screen
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s * 0.235,
                        outline=255, width=int(stroke))
    # the zoom region inside it
    ix0, iy0, ix1, iy1 = s * 0.285, s * 0.285, s * 0.715, s * 0.715
    d.rounded_rectangle([ix0, iy0, ix1, iy1], radius=s * 0.105, fill=255)
    # notch it out of the screen edge so the region reads as "on top"
    halo = Image.new("L", (s, s), 0)
    ImageDraw.Draw(halo).rounded_rectangle(
        [ix0 - stroke * 0.85, iy0 - stroke * 0.85, ix1 + stroke * 0.85, iy1 + stroke * 0.85],
        radius=s * 0.15, fill=255)
    m = Image.composite(Image.new("L", (s, s), 0), m, halo)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([ix0, iy0, ix1, iy1], radius=s * 0.105, fill=255)
    m = m.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(gradient(size, size), (0, 0), m)
    return out


def text_gradient(text: str, f: ImageFont.FreeTypeFont) -> Image.Image:
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    x0, y0, x1, y1 = probe.textbbox((0, 0), text, font=f)
    w, h = x1 - x0 + 4, y1 - y0 + 4
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).text((-x0 + 2, -y0 + 2), text, font=f, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(gradient(w, h, diagonal=False), (0, 0), m)
    return out


def banner(w=2400, h=620) -> Image.Image:
    img = Image.new("RGB", (w, h), BG)
    # soft indigo glow behind the wordmark
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse(
        [w * 0.22, -h * 0.30, w * 0.78, h * 1.10], fill=205)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=h * 0.30))
    tint = Image.new("RGB", (w, h), (46, 40, 104))
    img = Image.composite(tint, img, glow).convert("RGB")
    img = Image.blend(Image.new("RGB", (w, h), BG), img, 0.85)

    msize = int(h * 0.31)
    lg = mark(msize)
    wm = text_gradient("Zoomcut", font(int(h * 0.31), "Heavy"))
    gap = int(h * 0.05)
    group_w = lg.width + gap + wm.width
    gx = (w - group_w) // 2
    gy = int(h * 0.235)
    img.paste(lg, (gx, gy + (wm.height - lg.height) // 2), lg)
    img.paste(wm, (gx + lg.width + gap, gy), wm)

    d = ImageDraw.Draw(img)
    tag = "Record your screen. Get a video that looks edited."
    tf = font(int(h * 0.080), "Medium")
    tw = d.textbbox((0, 0), tag, font=tf)
    d.text(((w - (tw[2] - tw[0])) // 2, int(h * 0.605)), tag, font=tf, fill=DIM)

    sub = "automatic zooms  ·  no keyframing  ·  macOS, Windows, Linux"
    sf = font(int(h * 0.053), "Medium")
    sw = d.textbbox((0, 0), sub, font=sf)
    d.text(((w - (sw[2] - sw[0])) // 2, int(h * 0.755)), sub, font=sf, fill=(108, 112, 138))
    return img


def icon(size=512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bgm = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(bgm).rounded_rectangle(
        [0, 0, size * SS - 1, size * SS - 1], radius=size * SS * 0.225, fill=255)
    bgm = bgm.resize((size, size), Image.LANCZOS)
    img.paste(Image.new("RGB", (size, size), BG), (0, 0), bgm)
    lg = mark(int(size * 0.62))
    img.paste(lg, ((size - lg.width) // 2, (size - lg.height) // 2), lg)
    return img


def app_icons() -> None:
    """Platform icon files for the packaged executables."""
    import shutil, subprocess, tempfile
    base = icon(1024)
    ico = os.path.join(OUT, "zoomcut.ico")
    base.save(ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    png = os.path.join(OUT, "zoomcut.png")
    base.resize((512, 512), Image.LANCZOS).save(png)
    if shutil.which("iconutil"):                       # macOS only
        tmp = tempfile.mkdtemp()
        iconset = os.path.join(tmp, "zoomcut.iconset")
        os.makedirs(iconset, exist_ok=True)
        for size in (16, 32, 64, 128, 256, 512):
            base.resize((size, size), Image.LANCZOS).save(
                os.path.join(iconset, f"icon_{size}x{size}.png"))
            base.resize((size * 2, size * 2), Image.LANCZOS).save(
                os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
        out = os.path.join(OUT, "zoomcut.icns")
        if subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out]).returncode == 0:
            print("wrote docs/zoomcut.icns")
        shutil.rmtree(tmp, ignore_errors=True)
    print("wrote docs/zoomcut.ico, docs/zoomcut.png")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    banner().save(os.path.join(OUT, "banner.png"))
    icon().save(os.path.join(OUT, "logo.png"))
    mark(512).save(os.path.join(OUT, "mark.png"))
    app_icons()
    print("wrote docs/banner.png, docs/logo.png, docs/mark.png")
