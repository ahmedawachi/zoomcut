#!/usr/bin/env python3
"""Zoomcut end-to-end test suite. Run: python3 tests/test_all.py"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile, threading, time
import urllib.parse
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zoomcut.analyze import analyze
from zoomcut.director import plan, keyframes, DirectorConfig
from zoomcut.project import new_project, save, load, import_style_preset
from zoomcut.render import render, still, crop_box, target_at, Spring
from zoomcut.util import probe, ZoomcutError
from zoomcut import wallpapers, recorder, winlist

TMP = tempfile.mkdtemp(prefix="zoomcut-test-")
# --quick skips the parts that need a real desktop (wallpapers, window list,
# screen recording) so the suite can run on a headless CI machine.
QUICK = "--quick" in sys.argv
PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, str(detail)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    return cond


def skip(name, why=""):
    SKIP.append(name)
    print(f"  SKIP  {name}" + (f"   ({why})" if why else ""))


def section(t):
    print(f"\n\033[1m{t}\033[0m")


def mkclip(path, spec, dur=4.0, size="640x400", fps=30):
    """Build a synthetic clip from an ffmpeg lavfi filter spec."""
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", spec, "-t", str(dur), "-r", str(fps), "-s", size,
                    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", path],
                   check=True)
    return path


def mkmoving(path, box, xexpr, yexpr, dur=4.0, size="640x400", fps=30):
    """A small bright box moving over a dark background.

    NB: drawbox's w/h expressions do NOT animate with t (they evaluate once
    and yield NaN), which silently produces a completely static clip. overlay's
    x/y expressions do animate, so movement is built that way.
    """
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=0x101418:s={size}",
                    "-f", "lavfi", "-i", f"color=c=0x66ddff:s={box}",
                    "-filter_complex", f"[0][1]overlay=x='{xexpr}':y='{yexpr}'",
                    "-t", str(dur), "-r", str(fps),
                    "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", path],
                   check=True)
    return path


def invariants(shots, dur, label):
    ks = keyframes(shots)
    ok = True
    ok &= check(f"{label}: opens on the whole window",
                abs(ks[0][0]) < 1e-9 and ks[0][1] == 1.0, f"{ks[0]}")
    ok &= check(f"{label}: closes on the whole window", ks[-1][1] == 1.0, f"{ks[-1]}")
    # key times are quantised to 1ms; past the last key target_at holds it
    ok &= check(f"{label}: covers the clip", abs(ks[-1][0] - dur) < 2e-3, f"{ks[-1][0]} vs {dur}")
    ok &= check(f"{label}: keys monotonic in time",
                all(b[0] >= a[0] - 1e-9 for a, b in zip(ks, ks[1:])))
    bad = [k for k in ks if k[1] < 1.0 or not (0.5 / k[1] - 1e-12 <= k[2] <= 1 - 0.5 / k[1] + 1e-12)
           or not (0.5 / k[1] - 1e-12 <= k[3] <= 1 - 0.5 / k[1] + 1e-12)]
    ok &= check(f"{label}: every crop inside the frame", not bad, str(bad[:2]))
    ok &= check(f"{label}: shots tile with no gaps",
                all(abs(a.end - b.start) < 1e-6 for a, b in zip(shots, shots[1:])))
    ok &= check(f"{label}: no shot runs backwards", all(s.end > s.start for s in shots))
    return ok


# ==========================================================================
section("1 · camera maths")
s = Spring(0.0, 200, 2.25, 36)
for _ in range(120):
    s.step(1.0, 1 / 60)
check("spring converges to its target", abs(s.x - 1.0) < 0.01, f"x={s.x}")
s2 = Spring(0.0, 200, 2.25, 36)
overshoot = max(s2.step(1.0, 1 / 60) for _ in range(120))
check("spring overshoot stays under 5%", overshoot < 1.05, f"peak={overshoot:.3f}")
x0, y0, bw, bh = crop_box(1.0, 0.5, 0.5, 1000, 800)
check("zoom 1.0 crops the entire frame", (x0, y0, bw, bh) == (0.0, 0.0, 1000.0, 800.0))
for z, cx, cy in [(2.0, 0.0, 0.0), (2.0, 1.0, 1.0), (1.5, -5, 9), (1.0, 0.5, 0.5), (3.7, .9, .1)]:
    x0, y0, bw, bh = crop_box(z, cx, cy, 1000, 800)
    if not (x0 >= -1e-9 and y0 >= -1e-9 and x0 + bw <= 1000 + 1e-6 and y0 + bh <= 800 + 1e-6):
        check(f"crop_box keeps z={z} c=({cx},{cy}) in bounds", False, f"{x0},{y0},{bw},{bh}")
        break
else:
    check("crop_box clamps every out-of-range camera state", True)
check("target_at before the first key holds it", target_at([[1, 2, .5, .5]], 0.0) == (2, .5, .5))
check("target_at after the last key holds it", target_at([[1, 2, .5, .5]], 99) == (2, .5, .5))
check("target_at interpolates", abs(target_at([[0, 1, .5, .5], [2, 2, .5, .5]], 1.0)[0] - 1.5) < 1e-9)
check("target_at survives an empty key list", target_at([], 3.0) == (1.0, 0.5, 0.5))

# ==========================================================================
section("2 · analysis + director on synthetic clips")
clips = {}
clips["static"] = mkclip(os.path.join(TMP, "static.mp4"), "color=c=0x101418:s=640x400")
clips["one cut"] = mkclip(os.path.join(TMP, "onecut.mp4"),
                          "color=c=0x101418:s=640x400,drawbox=x=0:y=0:w=640:h=400:"
                          "color=0xdddddd:t=fill:enable='gte(t,2)'")
# pathological: the whole screen strobing. Must not crash; nobody expects a
# tasteful zoom out of it.
clips["strobe"] = mkclip(os.path.join(TMP, "strobe.mp4"),
                         "color=c=0x101418:s=640x400,drawbox=x=0:y=0:w=640:h=400:"
                         "color=0xdddddd:t=fill:enable='lt(mod(t\\,0.4)\\,0.2)'")
clips["moving box"] = mkmoving(os.path.join(TMP, "moving.mp4"), "70x70",
                               "40+t*120", "160")
# localised activity in the top-left, no whole-screen change: the realistic
# "someone is typing up here" case
clips["corner action"] = mkmoving(os.path.join(TMP, "corner.mp4"), "80x26",
                                  "16+mod(t*70,150)", "20")
for name, p in clips.items():
    a = analyze(p)
    invariants(plan(a), a.duration, name)
check("a cut in the middle is detected",
      any(1.7 < c < 2.4 for c in analyze(clips["one cut"]).cuts),
      str(analyze(clips["one cut"]).cuts))
check("a dead-still clip reports no cuts", analyze(clips["static"]).cuts == [],
      str(analyze(clips["static"]).cuts))
check("a dead-still clip stays wide the whole time",
      all(s.zoom == 1.0 for s in plan(analyze(clips["static"]))))
check("a strobing screen does not crash the director",
      len(plan(analyze(clips["strobe"]))) >= 1)
corner = plan(analyze(clips["corner action"]))
zoomed = [s for s in corner if s.zoom > 1.01]
check("action in a corner produces a zoom", len(zoomed) > 0)
if zoomed:
    s0 = zoomed[0]
    h = 0.5 / s0.zoom
    check("...framed at the top of the screen where the action is",
          (s0.cy - h) < 0.15, f"crop top {s0.cy-h:.3f}")
    check("...and the action is fully inside the crop, not sliced",
          (s0.cx - h) <= 0.14 and (s0.cx + h) >= 0.37,
          f"crop x [{s0.cx-h:.3f},{s0.cx+h:.3f}]")

# ==========================================================================
section("3 · edge cases that would ruin a first run")
short = mkclip(os.path.join(TMP, "short.mp4"), "color=c=red:s=320x240", 0.6)
try:
    invariants(plan(analyze(short)), analyze(short).duration, "0.6s clip")
except Exception as e:
    check("a 0.6s clip still plans", False, repr(e))
tiny = mkclip(os.path.join(TMP, "tiny.mp4"), "color=c=blue:s=160x120", 2.0)
try:
    a = analyze(tiny)
    invariants(plan(a), a.duration, "160x120 clip")
except Exception as e:
    check("a 160x120 clip still plans", False, repr(e))
try:
    analyze(os.path.join(TMP, "does-not-exist.mp4"))
    check("missing file raises a clear error", False)
except Exception as e:
    check("missing file raises a clear error", isinstance(e, (ZoomcutError, Exception)))
tall = mkclip(os.path.join(TMP, "tall.mp4"), "color=c=0x223344:s=400x900", 2.0, size="400x900")
try:
    a = analyze(tall)
    pj = new_project(tall, keyframes(plan(a)), plan(a))
    still(pj, 1.0, os.path.join(TMP, "tall.png"), width=640)
    check("a portrait recording composites without error", True)
except Exception as e:
    check("a portrait recording composites without error", False, repr(e))
if recorder.IS_MAC:
    try:
        recorder._prepare_path(os.path.join(TMP, ".hidden.mov"), ".mov")
        check("dot-file output is rejected up front (macOS)", False)
    except ZoomcutError:
        check("dot-file output is rejected up front (macOS)", True)
else:
    # only screencapture has the dot-file quirk; elsewhere it is a valid name
    ok = recorder._prepare_path(os.path.join(TMP, ".hidden.mkv"), ".mkv")
    check("dot-file output is allowed off macOS", ok.endswith(".hidden.mkv"), ok)
try:
    recorder.start(os.path.join(TMP, "x.mov"), mode="region", region=None)
    check("region mode without a region is rejected", False)
except ZoomcutError:
    check("region mode without a region is rejected", True)
try:
    recorder.start(os.path.join(TMP, "x.mov"), mode="window", window_id=999999999)
    check("recording a dead window id is rejected", False)
except ZoomcutError:
    check("recording a dead window id is rejected", True)

# ==========================================================================
section("4 \u00b7 project file + style-preset import")
a = analyze(clips["moving box"])
sh = plan(a)
pj = new_project(clips["moving box"], keyframes(sh), sh)
pp = save(pj, os.path.join(TMP, "p.json"))
check("project round-trips through disk", load(pp)["camera"]["keys"] == pj["camera"]["keys"])
ss = {"json": {"config": {
    "backgroundGradient": {"start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 1},
                           "stops": [{"color": "#3F37C9"}, {"color": "#8C87DF"}]},
    "backgroundPaddingRatio": 10, "backgroundType": "system",
    "backgroundSystemName": "macOS/tahoe-light.jpg", "backgroundBlur": 0,
    "windowBorderRadius": 12, "shadowIntensity": 0.75, "shadowDistance": 25,
    "shadowBlur": 20, "screenMovementSpring": {"mass": 2.25, "stiffness": 200, "damping": 40}},
    "scenes": [{"zoomRanges": [{"zoom": 1.9, "manualTargetPoint": {"x": .76, "y": .49},
                                "startTime": 19112, "endTime": 22031, "isDisabled": False}]}]}}
imp = import_style_preset(ss)
check("preset: wallpaper name imported", imp["style"]["background"]["name"] == "Tahoe Light")
check("preset: padding ~0.07", abs(imp["style"]["paddingRatio"] - 0.0704) < 0.002)
check("preset: spring imported", imp["spring"]["stiffness"] == 200)
check("preset: zoom ranges imported as keys", len(imp["manualKeys"]) == 2)
check("preset: bare (unwrapped) config also works",
      import_style_preset(ss["json"])["style"]["paddingRatio"] > 0)

# ==========================================================================
section("5 \u00b7 wallpapers")
ws = wallpapers.discover()
if QUICK or not ws:
    skip("wallpaper checks", "no desktop pictures available" if not ws else "--quick")
else:
    check("wallpapers found on this machine", len(ws) > 0, str(len(ws)))
    check("a default wallpaper is chosen", bool(wallpapers.default_name()))
    p = wallpapers.materialise(wallpapers.default_name())
    check("default wallpaper decodes to a PNG", os.path.isfile(p) and os.path.getsize(p) > 1000)
    check("fuzzy name lookup matches a close name",
          os.path.isfile(wallpapers.materialise(ws[0]["name"].split()[0])))
    try:
        wallpapers.materialise("definitely-not-a-wallpaper-xyz")
        check("unknown wallpaper raises", False)
    except ZoomcutError:
        check("unknown wallpaper raises", True)

# ==========================================================================
section("6 \u00b7 window listing")
if QUICK:
    skip("window listing", "--quick")
else:
  try:
    wl = winlist.pickable()
    check("windows can be listed", isinstance(wl, list))
    check("listed windows have id/app/size",
          all({"id", "app", "width", "height"} <= set(w) for w in wl))
    check("only real windows are offered (layer 0, big enough)",
          all(w["layer"] == 0 and w["width"] >= 200 for w in wl))
  except Exception as e:
    check("windows can be listed", False, repr(e))

# ==========================================================================
section("7 · render")
out = os.path.join(TMP, "render.mp4")
pj = new_project(clips["moving box"], keyframes(sh), sh,
                 output={"width": 1280, "height": 720, "fps": 30, "crf": 20, "preset": "veryfast"})
render(pj, out)
i = probe(out)
check("render produces the requested size", (i["width"], i["height"]) == (1280, 720), str(i))
check("render length matches the source", abs(i["duration"] - 4.0) < 0.2, str(i["duration"]))
pj2 = json.loads(json.dumps(pj))
pj2["trim"] = [1.0, 3.0]
out2 = os.path.join(TMP, "trim.mp4")
render(pj2, out2)
check("trim produces a 2s clip", abs(probe(out2)["duration"] - 2.0) < 0.2, str(probe(out2)["duration"]))
pj3 = json.loads(json.dumps(pj))
pj3["style"]["background"] = {"type": "gradient", "gradient": {"from": "#112233", "to": "#aabbcc",
                                                               "p0": [0, 0], "p1": [1, 1]}, "dim": 0, "blur": 0}
render(pj3, os.path.join(TMP, "grad.mp4"))
check("gradient background renders", os.path.getsize(os.path.join(TMP, "grad.mp4")) > 1000)
pj4 = json.loads(json.dumps(pj))
pj4["source"] = "/nope/missing.mp4"
try:
    render(pj4, os.path.join(TMP, "x.mp4"))
    check("missing source fails loudly", False)
except ZoomcutError:
    check("missing source fails loudly", True)

# Banding: the background never moves, so anything h264 posterises there
# sits on screen for the whole clip. An absolute pixel threshold is not
# portable - x264 builds differ, and a photo wallpaper self-dithers while a
# gradient does not - so this renders the SAME gradient twice on THIS
# encoder, with and without the dither, and checks the dither earns its keep.
import numpy as np
import zoomcut.render as _R

GRADIENT_STYLE = {"background": {"type": "gradient", "dim": 0.12, "blur": 0,
                                 "gradient": {"from": "#3F37C9", "to": "#8C87DF",
                                              "p0": [0, 0], "p1": [1, 1]}}}


def flat_runs(path, w=1280, h=720):
    """Mean length of constant-luma runs in the background strip, in pixels."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "0.5", "-i", path,
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True).stdout
    g = np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w)
    runs = []
    for row in g[100:600:9, 0:120]:
        idx = np.flatnonzero(np.diff(row.astype(np.int16)) != 0)
        if len(idx) > 1:
            runs.append(np.diff(idx).mean())
    return float(np.mean(runs)) if runs else float("inf")


_band = {}
_dither_was = _R.DITHER
for _mode, _amp in (("dithered", _dither_was), ("control", 0.0)):
    _R.DITHER = _amp
    _pj = new_project(clips["moving box"], keyframes(sh), sh, style=GRADIENT_STYLE,
                      output={"width": 1280, "height": 720, "fps": 30,
                              "crf": 20, "preset": "veryfast"})
    _out = os.path.join(TMP, f"band-{_mode}.mp4")
    render(_pj, _out)
    _band[_mode] = flat_runs(_out)
_R.DITHER = _dither_was

if _band["control"] < 15:
    skip("background dithering measurably reduces banding",
         f"this encoder barely bands here (control {_band['control']:.1f}px)")
else:
    check("background dithering measurably reduces banding",
          _band["dithered"] < _band["control"] * 0.6,
          f"dithered {_band['dithered']:.1f}px vs undithered {_band['control']:.1f}px")

# ==========================================================================
section("8 · web app (served on a throwaway port, shut down after)")
from http.server import ThreadingHTTPServer
import zoomcut.server as srv

httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
PORT = httpd.server_address[1]
th = threading.Thread(target=httpd.serve_forever, daemon=True)
th.start()
BASE = f"http://127.0.0.1:{PORT}"


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


try:
    code, body, _ = get("/")
    check("GET / serves the app", code == 200 and b"Zoomcut" in body)
    code, body, _ = get("/api/state")
    st = json.loads(body)
    check("GET /api/state works", code == 200 and "permission" in st)
    code, body, _ = get("/api/wallpapers")
    wp_body = json.loads(body) if code == 200 else {}
    check("GET /api/wallpapers answers with a catalogue",
          code == 200 and isinstance(wp_body.get("wallpapers"), list), str(body)[:150])
    if not wp_body.get("wallpapers"):
        skip("wallpaper catalogue is empty here", "no desktop pictures installed")
    code, body, _ = get("/api/windows")
    check("GET /api/windows lists windows", code == 200 and "windows" in json.loads(body))
    code, body, _ = get("/api/nope")
    check("unknown endpoint 404s cleanly", code == 404)

    code, j = post("/api/analyze", {"source": clips["moving box"], "size": "1080p", "fps": 30})
    check("POST /api/analyze plans a project", code == 200 and j["project"]["camera"]["shots"],
          json.dumps(j)[:200])
    nshots = len(j["project"]["camera"]["shots"])

    code, j = post("/api/analyze", {"source": "/definitely/not/here.mov"})
    check("analyse of a missing file 400s with a message", code == 400 and "no such file" in j.get("error", ""))

    code, j = post("/api/still", {"t": 1.0, "width": 480})
    check("POST /api/still renders a frame", code == 200 and os.path.isfile(j.get("path", "")))
    stillpath = j.get("path", "")
    code, body, hdrs = get("/api/file?path=" + urllib.parse.quote(stillpath))
    check("the still is served back", code == 200 and body[:4] == b"\x89PNG")

    code, body, _ = get("/api/file?path=/etc/passwd")
    check("arbitrary files are NOT served", code == 404)
    code, body, _ = get("/api/file?path=" + urllib.parse.quote(os.path.expanduser("~/.ssh/id_rsa")))
    check("private files are NOT served", code == 404)

    # edit a shot, confirm the camera keys follow
    proj = post("/api/project", {})[1]["project"]
    shots = proj["camera"]["shots"]
    shots[-1]["zoom"] = 1.4
    shots[-1]["cx"] = 0.5
    shots[-1]["cy"] = 0.5
    code, j = post("/api/project", {"shots": shots})
    check("editing a shot rewrites the camera keys",
          code == 200 and any(abs(k[1] - 1.4) < 1e-6 for k in j["project"]["camera"]["keys"]))

    code, j = post("/api/render", {"preview": True})
    check("POST /api/render starts a render", code == 200 and j.get("output"))
    outp = j.get("output")
    deadline = time.time() + 300
    prog = {}
    while time.time() < deadline:
        prog = json.loads(get("/api/progress")[1])
        if prog.get("state") in ("done", "error"):
            break
        time.sleep(0.5)
    check("the render finishes", prog.get("state") == "done", str(prog)[:200])
    if prog.get("state") == "done":
        check("the rendered file exists", os.path.isfile(prog["output"]))
        code, body, hdrs = get("/api/file?path=" + urllib.parse.quote(prog["output"]),
                               {"Range": "bytes=0-1023"})
        check("video is served with Range support (so it can seek)",
              code == 206 and len(body) == 1024 and "Content-Range" in hdrs)

    code, j = post("/api/save", {"path": os.path.join(TMP, "ui.json")})
    check("POST /api/save writes a project", code == 200 and os.path.isfile(j["path"]))
    code, j = post("/api/load", {"path": os.path.join(TMP, "ui.json")})
    check("POST /api/load reads it back", code == 200 and len(j["project"]["camera"]["shots"]) == nshots)
finally:
    httpd.shutdown()
    httpd.server_close()
    th.join(timeout=5)
check("the test server is shut down", not th.is_alive())

# ==========================================================================
section("9 · the real thing: record a window, auto-cut it, render it")
ok, why = recorder.available()
if QUICK:
    skip("live recording cycle", "--quick")
    ok = False
elif not ok:
    skip("live recording cycle", why)
if ok:
    wins = [w for w in winlist.pickable() if w["app"] in ("Finder", "System Settings")] \
        or winlist.pickable()
    if not wins:
        check("a window is available to record", False)
    else:
        tgt = wins[0]
        cap = os.path.join(TMP, "window-capture.mov")
        rec = recorder.start(cap, mode="window", window_id=tgt["id"], cursor=True)
        time.sleep(3.0)
        path = recorder.stop(rec)
        info = probe(path)
        check("window recording produced a file", os.path.getsize(path) > 0)
        check("captured at the window's size (2x retina)",
              abs(info["width"] - tgt["width"] * 2) <= 4 and abs(info["height"] - tgt["height"] * 2) <= 4,
              f"{info['width']}x{info['height']} vs window {tgt['width']}x{tgt['height']}")
        check("recording padded to real elapsed time (no silent truncation)",
              info["duration"] > 2.5, f"{info['duration']:.2f}s for a 3.0s recording")
        a = analyze(path)
        sh = plan(a)
        invariants(sh, a.duration, "real capture")
        pj = new_project(path, keyframes(sh), sh,
                         output={"width": 1280, "height": 720, "fps": 30,
                                 "crf": 22, "preset": "veryfast"})
        fin = os.path.join(TMP, "window-final.mp4")
        render(pj, fin)
        fi = probe(fin)
        check("the full record -> auto-cut -> render cycle works",
              (fi["width"], fi["height"]) == (1280, 720) and fi["duration"] > 2.0, str(fi))
        os.remove(path)
        os.remove(fin)


# ==========================================================================
section("10 \u00b7 cross-platform capture commands")
# The Windows and Linux recorders build ffmpeg command lines. Those are pure
# functions, so their behaviour is checked here whatever platform we run on -
# the parts that need the actual OS are covered by CI on that OS.
_real_find, _real_size = winlist.find, winlist.screen_size
winlist.find = lambda i: {"id": i, "app": "Safari", "title": "Acme Dashboard",
                          "x": 40, "y": 60, "width": 1281, "height": 801,
                          "layer": 0, "pid": 1}
winlist.screen_size = lambda: (2560, 1440)
try:
    w_win = recorder._cmd_windows("o.mkv", "window", None, 1, 42, True, None)
    check("windows: a window is grabbed by title", "title=Acme Dashboard" in w_win)
    check("windows: gdigrab is the demuxer", "gdigrab" in w_win)
    w_reg = recorder._cmd_windows("o.mkv", "region", (10, 20, 641, 481), 1, None, False, None)
    check("windows: a region sets offsets", "-offset_x" in w_reg and "10" in w_reg)
    check("windows: odd sizes are rounded even (h264 needs it)", "640x480" in w_reg,
          " ".join(w_reg))
    check("windows: cursor can be turned off",
          w_reg[w_reg.index("-draw_mouse") + 1] == "0")
    l_reg = recorder._cmd_linux("o.mkv", "region", (10, 20, 641, 481), 1, None, True, None)
    check("linux: x11grab is the demuxer", "x11grab" in l_reg)
    check("linux: the region is encoded in the input spec",
          any(a.endswith("+10,20") for a in l_reg), " ".join(l_reg))
    check("linux: odd sizes are rounded even", "640x480" in l_reg)
    l_win = recorder._cmd_linux("o.mkv", "window", None, 1, 7, True, None)
    check("linux: a window becomes its rectangle",
          any(a.endswith("+40,60") for a in l_win) and "1280x800" in l_win, " ".join(l_win))
    l_disp = recorder._cmd_linux("o.mkv", "display", None, 1, None, True, 5)
    check("linux: whole display uses the screen size", "2560x1440" in l_disp)
    check("a time limit is passed through", "-t" in l_disp and "5" in l_disp)
    check("every backend encodes to h264 yuv420p",
          all("libx264" in c and "yuv420p" in c for c in (w_win, w_reg, l_reg, l_disp)))
    winlist.screen_size = lambda: None
    try:
        recorder._cmd_linux("o.mkv", "display", None, 1, None, True, None)
        check("linux: unknown screen size is a clear error", False)
    except ZoomcutError as e:
        check("linux: unknown screen size is a clear error", "region" in str(e).lower())
finally:
    winlist.find, winlist.screen_size = _real_find, _real_size

# thumbnails must never decode a full-resolution copy: doing so filled
# hundreds of MB of cache the first time the wallpaper picker was opened
if wallpapers.discover():
    import shutil as _sh
    from zoomcut.util import cache_dir as _cd
    _sh.rmtree(os.path.join(_cd(), "thumbs"), ignore_errors=True)
    _name = wallpapers.discover()[0]["name"]
    _t = wallpapers.thumbnail(_name)
    from PIL import Image as _Im
    check("a wallpaper thumbnail is the size asked for", _Im.open(_t).size == (224, 126))
    check("a thumbnail is small on disk", os.path.getsize(_t) < 120_000,
          f"{os.path.getsize(_t)} bytes")
    check("thumbnailing leaves no half-converted files",
          not [f for f in os.listdir(os.path.dirname(_t)) if ".src." in f])
else:
    skip("thumbnail checks", "no wallpapers on this machine")

from zoomcut.cli import main as _cli
_rc = _cli(["doctor"])
check("doctor runs and reports a status", _rc in (0, 1), f"exit {_rc}")

check("backend name matches the platform", recorder.backend_name() in
      ("screencapture", "gdigrab", "x11grab"), recorder.backend_name())
for bad in ("nope", "", "Window"):
    try:
        recorder.start("x.mov", mode=bad)
        check(f"mode {bad!r} is rejected", False)
    except ZoomcutError:
        check(f"mode {bad!r} is rejected", True)

tail = f", {len(SKIP)} skipped" if SKIP else ""
print(f"\n\033[1m{len(PASS)} passed, {len(FAIL)} failed{tail}\033[0m")
if FAIL:
    print("failed:")
    for name, detail in FAIL:
        print(f"  {name}" + (f"   [{detail}]" if detail else ""))
    # surface each failure as a GitHub annotation, so a red run says what
    # broke without anyone having to open the log
    if os.environ.get("GITHUB_ACTIONS") == "true":
        plat = f"{sys.platform} py{sys.version_info.major}.{sys.version_info.minor}"
        for name, detail in FAIL:
            msg = f"{name}" + (f" -- {detail}" if detail else "")
            msg = msg.replace("\n", " ")[:400]
            print(f"::error title=zoomcut ({plat})::{msg}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
