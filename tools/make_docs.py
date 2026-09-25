#!/usr/bin/env python3
"""Regenerate the documentation images in docs/.

    python3 tools/make_docs.py

Builds the synthetic demo recording (so nothing personal ends up in the repo),
then renders a raw-vs-Zoomcut comparison from it.

The app screenshot is captured separately, because it needs a browser:

    ZOOMCUT_DEMO=1 ZOOMCUT_OUTPUT_DIR=$(mktemp -d) \\
        python3 -m zoomcut ui --port 8811 --no-open &
    curl -s -X POST -H 'Content-Type: application/json' \\
         -d '{"source":"docs/demo-recording.mp4"}' http://127.0.0.1:8811/api/analyze
    npx playwright screenshot --channel chrome --device "Desktop Chrome HiDPI" \\
        --viewport-size "1460, 1080" --wait-for-selector "#frame.live" \\
        --wait-for-timeout 1500 "http://127.0.0.1:8811/?t=10.4" docs/screenshot-app.png
    kill %1

ZOOMCUT_DEMO=1 makes the server serve a fixed window list, so the screenshot
never contains anybody's real window titles. The editor boots asynchronously
and only goes live once the preview proxy is ready - hence waiting for
#frame.live; Chrome's one-shot --screenshot fires before either happens.
"""
from __future__ import annotations
import os, subprocess, sys
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DOCS = os.path.join(ROOT, "docs")
CLIP = os.path.join(DOCS, "demo-recording.mp4")
AT = 10.4          # the moment the result card is on screen

from zoomcut.analyze import analyze                                  # noqa: E402
from zoomcut.director import plan, keyframes                         # noqa: E402
from zoomcut.project import new_project                              # noqa: E402
from zoomcut.render import still                                     # noqa: E402
from tools.make_brand import font, BG, DIM, INK                      # noqa: E402


def build_clip():
    if not os.path.exists(CLIP):
        from tools.make_demo_clip import main as make
        make(CLIP)
    return CLIP


def comparison(out=os.path.join(DOCS, "example.png")):
    a = analyze(CLIP)
    shots = plan(a)
    pj = new_project(CLIP, keyframes(shots), shots)
    zoomed = os.path.join(DOCS, ".tmp-zoom.png")
    still(pj, AT, zoomed, width=1500)

    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(AT), "-i", CLIP,
                          "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True).stdout
    import io
    left = Image.open(io.BytesIO(raw)).convert("RGB")
    right = Image.open(zoomed).convert("RGB")

    PW, PH, GAP, PAD, TOP = 1120, 630, 46, 46, 92
    W = PAD * 2 + PW * 2 + GAP
    H = TOP + PH + PAD
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    def place(im, x):
        r = min(PW / im.width, PH / im.height)
        im = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
        box = Image.new("RGB", (PW, PH), (10, 11, 15))
        box.paste(im, ((PW - im.width) // 2, (PH - im.height) // 2))
        img.paste(box, (x, TOP))

    place(left, PAD)
    place(right, PAD + PW + GAP)
    lf = font(30, "Semibold")
    sf = font(23, "Regular")
    d.text((PAD, 30), "the raw recording", font=lf, fill=DIM)
    d.text((PAD + PW + GAP, 30), "what Zoomcut exports", font=lf, fill=INK)
    d.text((PAD + int(d.textlength("what Zoomcut exports", font=lf)) + 18, 36),
           "— same frame, framed", font=sf, fill=(108, 112, 138))
    img.save(out)
    os.remove(zoomed)
    print("wrote", out, img.size)


if __name__ == "__main__":
    build_clip()
    comparison()
