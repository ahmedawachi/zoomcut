#!/usr/bin/env python3
"""Render a synthetic screen recording used for the documentation shots.

    python3 tools/make_demo_clip.py [out.mp4]

It draws a fictional app - sidebar, search field, a result card - and acts out
a short session: idle, typing, a view change, a wait, then a result. That gives
the docs something realistic to show without putting anybody's actual screen in
the repository.
"""
from __future__ import annotations
import os, subprocess, sys
from PIL import Image, ImageDraw, ImageFont

W, H, FPS, DUR = 1600, 1000, 30, 12.0
BG, PANEL, LINE = (14, 15, 19), (22, 24, 30), (38, 41, 52)
INK, DIM, FAINT = (232, 234, 242), (150, 154, 174), (98, 102, 122)
ACC, OK = (108, 99, 255), (62, 207, 142)
PROMPT = "quarterly revenue by region"


def font(size, weight="Regular"):
    try:
        f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
        f.set_variation_by_name(weight)
        return f
    except Exception:
        for p in ("/System/Library/Fonts/Avenir Next.ttc",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "C:\\Windows\\Fonts\\segoeui.ttf"):
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()


F = {k: font(s, w) for k, (s, w) in {
    "h": (30, "Semibold"), "b": (21, "Regular"), "s": (17, "Regular"),
    "t": (23, "Regular"), "n": (25, "Semibold"), "k": (15, "Medium")}.items()}


def chrome(d):
    d.rectangle([0, 0, W, H], fill=BG)
    d.rectangle([0, 0, 280, H], fill=PANEL)
    d.line([(280, 0), (280, H)], fill=LINE)
    for i, (label, on) in enumerate([("Overview", False), ("Reports", True), ("Segments", False),
                                     ("Exports", False), ("Settings", False)]):
        y = 96 + i * 46
        if on:
            d.rounded_rectangle([16, y - 9, 264, y + 28], radius=9, fill=(38, 36, 70))
        d.ellipse([34, y + 3, 48, y + 17], outline=ACC if on else FAINT, width=2)
        d.text((62, y), label, font=F["b"], fill=INK if on else DIM)
    d.text((34, 38), "Northwind", font=F["n"], fill=INK)
    d.line([(280, 74), (W, 74)], fill=LINE)
    d.text((320, 30), "Reports", font=F["h"], fill=INK)


def frame(t: float) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    chrome(d)

    if t < 4.3:                                    # search view: someone types
        d.text((470, 250), "What do you want to see?", font=F["h"], fill=DIM)
        d.rounded_rectangle([460, 330, 1400, 420], radius=14, fill=(26, 28, 36), outline=LINE, width=2)
        typed = ""
        if t > 1.15:
            n = int(min(len(PROMPT), (t - 1.15) / 2.55 * len(PROMPT)))
            typed = PROMPT[:n]
        d.text((492, 360), typed, font=F["t"], fill=INK)
        if typed and int(t * 2) % 2 == 0:
            x = 492 + d.textlength(typed, font=F["t"]) + 3
            d.line([(x, 356), (x, 394)], fill=INK, width=2)
        if len(typed) == len(PROMPT):
            d.rounded_rectangle([1320, 348, 1384, 402], radius=11, fill=ACC)
            d.text((1344, 363), "->", font=F["b"], fill=(255, 255, 255))
        return im

    d.rounded_rectangle([1000, 120, 1400, 178], radius=16, fill=(34, 36, 46))
    d.text((1028, 137), PROMPT, font=F["s"], fill=INK)

    if t < 8.25:                                   # working
        dots = "." * (1 + int((t * 2) % 3))
        d.text((330, 250), f"Gathering figures{dots}", font=F["b"], fill=DIM)
        d.text((330 + 250, 250), f"{int(t - 4.3)}s", font=F["s"], fill=FAINT)
        return im

    d.text((330, 232), "Result", font=F["k"], fill=FAINT)
    d.rounded_rectangle([322, 264, 1420, 690], radius=16, fill=PANEL, outline=LINE, width=2)
    d.rounded_rectangle([322, 264, 1420, 336], radius=16, fill=(28, 30, 40))
    d.text((352, 288), "Revenue by region", font=F["n"], fill=INK)
    d.rounded_rectangle([1270, 284, 1392, 318], radius=10, fill=(18, 48, 39))
    d.text((1294, 292), "Done", font=F["k"], fill=OK)
    rows = [("North", "1,284,900", 0.92), ("South", "912,400", 0.66),
            ("Europe", "755,120", 0.54), ("APAC", "401,880", 0.29)]
    for i, (name, val, frac) in enumerate(rows):
        y = 372 + i * 74
        if t > 9.3 + i * 0.28:
            d.rounded_rectangle([348, y - 14, 1394, y + 46], radius=11, fill=(30, 32, 43))
        d.text((372, y), name, font=F["b"], fill=INK)
        bw = int(560 * frac * min(1.0, max(0.0, (t - 8.6 - i * 0.22) / 0.55)))
        d.rounded_rectangle([560, y + 6, 560 + max(bw, 3), y + 26], radius=10, fill=ACC)
        d.text((1230, y), val, font=F["b"], fill=DIM)
    if t > 10.6:
        d.text((330, 726), "North leads on volume; APAC grew fastest this quarter.",
               font=F["b"], fill=DIM)
    return im


def main(out="docs/demo-recording.mp4"):
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    p = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p", out],
        stdin=subprocess.PIPE)
    for i in range(int(DUR * FPS)):
        p.stdin.write(frame(i / FPS).tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/demo-recording.mp4")
