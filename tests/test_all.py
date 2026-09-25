#!/usr/bin/env python3
"""Zoomcut end-to-end test suite. Run: python3 tests/test_all.py"""
from __future__ import annotations
import http.client, json, math, os, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.parse
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zoomcut.analyze import analyze, activity_track, Analysis
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
_a8 = analyze(os.path.join(ROOT, "docs", "demo-recording.mp4"))
check("analysis keeps its per-cell changes in 8 bits (a quarter of float32)",
      str(_a8.diffs.dtype) == "uint8", str(_a8.diffs.dtype))
check("...and still hands the director float32 heat",
      str(_a8.heat(0, _a8.duration).dtype) == "float32")
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

# the editor's activity lane
_mv = analyze(clips["moving box"])
tr = activity_track(_mv)
check("activity track: 10 bins a second from t=0",
      tr["t0"] == 0.0 and abs(tr["dt"] - 0.1) < 1e-9, f"t0={tr['t0']} dt={tr['dt']}")
check("activity track: covers the whole clip",
      len(tr["v"]) == math.ceil(_mv.duration / tr["dt"] - 1e-9), f"{len(tr['v'])} bins")
check("activity track: normalised to [0, 1], peak at 1",
      all(0.0 <= v <= 1.0 for v in tr["v"]) and max(tr["v"]) == 1.0, str(max(tr["v"])))
check("activity track: a dead-still clip is flat zero",
      set(activity_track(analyze(clips["static"]))["v"]) <= {0.0})
_syn = Analysis(path="", width=1, height=1, duration=0.2, afps=20, grid=(1, 1),
                times=[0.02, 0.07, 0.12, 0.17], energy=[0.2, 1.0, 0.6, 0.6])
_sv = activity_track(_syn)["v"]
check("activity track: a bin keeps its spike (max, not mean)",
      len(_sv) == 2 and _sv[0] == 1.0 and abs(_sv[1] - 0.605) < 0.002, str(_sv))
_long = Analysis(path="", width=1, height=1, duration=1000.0, afps=20, grid=(1, 1),
                 times=[i / 20 for i in range(1, 20000)], energy=[0.0] * 19999)
_lt = activity_track(_long)
check("activity track: a long clip is capped at max_points",
      len(_lt["v"]) == 1500 and abs(_lt["dt"] - 1000 / 1500) < 1e-9, f"{len(_lt['v'])} bins")

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
    # the editor's preview background is the thumbnail, the export's is the
    # full decode: for a video wallpaper they once came out in different colours
    _dyn = next((w["name"] for w in ws if w["kind"] == "dynamic"), None)
    if not _dyn:
        skip("a video wallpaper's preview has the export's colour", "no video wallpapers here")
    else:
        import numpy as _np
        from PIL import Image as _PImg
        from zoomcut.render import _cover
        _full = _np.asarray(_cover(_PImg.open(wallpapers.materialise(_dyn)).convert("RGB"), 640, 400), _np.float32)
        _thumb = _np.asarray(_PImg.open(wallpapers.thumbnail(_dyn, 640, 400)).convert("RGB"), _np.float32)
        # a hue shift, not brightness: BT.709 read as BT.601 lifts red and
        # green and drops blue (~7 levels apart); resampling moves all three alike
        _bias = (_thumb - _full).reshape(-1, 3).mean(axis=0)
        check("a video wallpaper's preview has the export's colour",
              float(_bias.max() - _bias.min()) < 1.5, f"per-channel bias {_bias.round(2).tolist()}")

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

# cancelling, or any failure mid-render, must leave neither a half-written
# mp4 nor an ffmpeg behind. Popen is wrapped to see every process render()
# starts.
import zoomcut.render as _R
_spawned = []
_RealPopen = _R.subprocess.Popen


class _TrackedPopen(_RealPopen):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _spawned.append(self)


def _render_fails(label, want, **kw):
    """render() raises `want`, removes its output and reaps both ffmpegs."""
    _spawned.clear()
    outp = os.path.join(TMP, "abort.mp4")
    _R.subprocess.Popen = _TrackedPopen
    try:
        render(pj, outp, **kw)
        check(f"{label}: raises", False, "render finished")
    except want as e:
        check(f"{label}: raises {want.__name__}", True)
    except Exception as e:
        check(f"{label}: raises {want.__name__}", False, repr(e))
    finally:
        _R.subprocess.Popen = _RealPopen
    check(f"{label}: no partial file is left",
          not os.path.exists(outp) and not os.path.exists(os.path.join(TMP, ".abort.partial.mp4")))
    # other helpers (wallpaper discovery, say) may run too: count the ffmpegs
    ff = [p for p in _spawned if "ffmpeg" in os.path.basename(str(p.args[0]))]
    check(f"{label}: no ffmpeg is left running",
          len(ff) == 2 and all(p.poll() is not None for p in _spawned),
          f"{[(os.path.basename(str(p.args[0])), p.poll()) for p in _spawned]}")


_render_fails("cancel before the first frame", ZoomcutError, cancel=lambda: True)
_ticks = {"n": 0}


def _cancel_later():
    _ticks["n"] += 1
    return _ticks["n"] > 40


_render_fails("cancel part-way through", ZoomcutError, cancel=_cancel_later)


def _boom(n, total):
    raise ValueError("progress callback failed")


_render_fails("any exception mid-render", ValueError, progress=_boom)
_keep = os.path.join(TMP, "keep-me.mp4")
with open(_keep, "wb") as f:
    f.write(b"an earlier export")
try:
    render(pj, _keep, cancel=lambda: True)
except ZoomcutError:
    pass
with open(_keep, "rb") as f:
    check("a failed render never costs the file already at its path", f.read() == b"an earlier export")
check("...and leaves no hidden partial beside it", not os.path.exists(os.path.join(TMP, ".keep-me.partial.mp4")))
render(pj, out)
check("a normal render still works after an aborted one",
      (probe(out)["width"], probe(out)["height"]) == (1280, 720))

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
import zoomcut
import zoomcut.server as srv
import zoomcut.media as media
from PIL import Image as _Img

# everything the app writes goes under TMP, never the real output folder or cache
srv.OUT_DIR = os.path.join(TMP, "out")
media.ROOT = os.path.join(TMP, "media")
media.POSTERS = os.path.join(TMP, "posters")
DEMO_CLIP = os.path.join(ROOT, "docs", "demo-recording.mp4")

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


def post(path, obj, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def raw(method, path, body=b"", headers=None, send=None):
    """Full control over the request, for what urllib will not send."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    try:
        c.putrequest(method, path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        if send:
            send(c.sock)
        elif body:
            c.send(body)
        r = c.getresponse()
        data = r.read()
        return r.status, data, dict(r.getheaders())
    finally:
        c.close()


def upload(name, data, headers=None):
    h = {"X-Zoomcut-Upload": "1", "Content-Type": "application/octet-stream",
         **(headers or {})}
    req = urllib.request.Request(BASE + "/api/upload?name=" + urllib.parse.quote(name),
                                 data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def fileurl(p):
    return "/api/file?path=" + urllib.parse.quote(p)


def wait_media(timeout=180):
    deadline = time.time() + timeout
    m = {}
    while time.time() < deadline:
        m = json.loads(get("/api/media")[1])
        if m.get("state") in ("ready", "error"):
            break
        time.sleep(0.25)
    return m


def wait_render(timeout=300):
    deadline = time.time() + timeout
    p = {}
    while time.time() < deadline:
        p = json.loads(get("/api/progress")[1])
        if p.get("state") in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    return p


try:
    # ---------------------------------------------------------- hardening
    code, body, hdrs = get("/api/state", {"Host": "evil.example"})
    check("a foreign Host header is refused (DNS rebinding)",
          code == 403 and json.loads(body).get("error") == "forbidden host", f"{code} {body[:80]}")
    code, _, _ = get("/", {"Host": "evil.example:%d" % PORT})
    check("...on every path, port or not", code == 403)
    ok_hosts = [get("/api/state", {"Host": h})[0] for h in
                (f"localhost:{PORT}", f"[::1]:{PORT}", "127.0.0.1", "LOCALHOST")]
    check("loopback Host names are accepted", ok_hosts == [200] * 4, str(ok_hosts))
    srv.HOSTS.add("10.9.8.7")                 # what serve(host="10.9.8.7") does
    try:
        code_b = get("/api/state", {"Host": f"10.9.8.7:{PORT}"})[0]
    finally:
        srv.HOSTS.discard("10.9.8.7")
    check("an address the server was explicitly bound to is accepted too", code_b == 200, str(code_b))
    code, j = post("/api/project", {}, {"Origin": "http://evil.example"})
    check("a POST from a foreign Origin is refused",
          code == 403 and j.get("error") == "forbidden origin", f"{code} {j}")
    code, j = post("/api/project", {}, {"Origin": "null"})
    check("a POST from an opaque (null) Origin is refused", code == 403, f"{code} {j}")
    code, j = post("/api/project", {}, {"Origin": "http://[::1"})
    check("a malformed Origin is refused, not a crash", code == 403, f"{code} {j}")
    code, j = post("/api/project", {}, {"Origin": f"http://localhost:{PORT}"})
    check("a POST from our own Origin gets through", code != 403, f"{code} {j}")
    code, body, _ = raw("POST", "/api/project", b"{}",
                        {"Content-Type": "text/plain", "Content-Length": "2"})
    check("a JSON endpoint refuses a text/plain body (no preflight-free POSTs)",
          code == 415 and json.loads(body).get("error") == "expected application/json",
          f"{code} {body[:80]}")
    code, body, _ = raw("POST", "/api/record/start", headers={"Content-Length": "0"})
    check("a bare POST cannot start a recording", code == 415 and srv.S.rec is None, str(code))
    code, j = post("/api/project", {}, {"Content-Type": "application/json; charset=utf-8"})
    check("application/json with a charset is fine", code in (200, 400) and code != 415, str(code))
    code, body, _ = raw("POST", "/api/project", b"{nope",
                        {"Content-Type": "application/json", "Content-Length": "5"})
    check("a malformed JSON body is a 400, not a 500", code == 400, f"{code} {body[:80]}")
    code, body, hdrs = raw("POST", "/api/upload?name=x.mp4", b"abc",
                           {"Content-Length": "3", "Content-Type": "video/mp4"})
    check("an upload without X-Zoomcut-Upload is refused", code == 403, str(code))
    code, body, hdrs = raw("OPTIONS", "/api/project", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    check("a CORS preflight is never granted",
          not any(k.lower().startswith("access-control") for k in hdrs), str(hdrs))
    xs = {label: get("/api/state", hd)[0] for label, hd in (
        ("cross-site", {"Sec-Fetch-Site": "cross-site"}), ("same-site", {"Sec-Fetch-Site": "same-site"}),
        ("foreign referer", {"Referer": "https://evil.example/page"}),
        ("foreign origin", {"Origin": "https://evil.example"}))}
    check("another site cannot use the API through GETs either (img, video, fetch)",
          all(c == 403 for c in xs.values()), str(xs))
    ok_src = [get("/api/state", hd)[0] for hd in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                                                  {"Referer": f"http://127.0.0.1:{PORT}/"})]
    check("...while our own page and the address bar still can", ok_src == [200] * 3, str(ok_src))
    code, _, hdrs = get("/", {"Sec-Fetch-Site": "cross-site"})
    check("the page opens from a link but refuses to be framed",
          code == 200 and hdrs.get("X-Frame-Options") == "DENY"
          and "frame-ancestors 'none'" in hdrs.get("Content-Security-Policy", ""), str(hdrs))

    # ---------------------------------------------------------- static
    code, body, hdrs = get("/")
    check("GET / serves the app", code == 200 and b"Zoomcut" in body)
    check("the page is served no-cache as HTML",
          hdrs.get("Cache-Control") == "no-cache" and hdrs.get("Content-Type", "").startswith("text/html"),
          str(hdrs))
    code, body, _ = get("/index.html")
    check("GET /index.html serves the app too", code == 200 and b"Zoomcut" in body)
    for fname, ctype in (("app.js", "text/javascript; charset=utf-8"),
                         ("app.css", "text/css; charset=utf-8")):
        code, body, hdrs = get("/" + fname)
        check(f"GET /{fname} is served as {ctype.split(';')[0]}",
              code == 200 and hdrs.get("Content-Type") == ctype
              and hdrs.get("Cache-Control") == "no-cache", f"{code} {hdrs.get('Content-Type')}")
    bad = {p: get(p)[0] for p in ("/..%2fserver.py", "/zoomcut/server.py", "/server.py",
                                  "/%2e%2e/server.py", "/APP.JS", "/nope.js", "/web/app.js")}
    check("nothing outside web/ is served (traversal, subfolders, other types)",
          all(c == 404 for c in bad.values()), str(bad))
    code, body, hdrs = get("/nope.js")
    check("a missing static file is a JSON 404",
          code == 404 and hdrs.get("Content-Type") == "application/json")

    # ---------------------------------------------------------- state
    code, body, _ = get("/api/state")
    st = json.loads(body)
    check("GET /api/state works", code == 200 and "permission" in st)
    check("state carries the version", st.get("version") == zoomcut.__version__, str(st.get("version")))
    check("state says whether clicks can be highlighted",
          st.get("clicksSupported") is (srv.platform_name() == "macOS"), str(st.get("clicksSupported")))
    check("idle progress has elapsed 0",
          st["progress"].get("state") == "idle" and st["progress"].get("elapsed") == 0.0,
          str(st["progress"]))
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
    code, body, _ = get("/api/media")
    m = json.loads(body)
    check("GET /api/media with no project is idle",
          code == 200 and m.get("state") == "idle" and m.get("source") is None, str(m))

    # ---------------------------------------------------------- analyse
    code, j = post("/api/analyze", {"source": clips["moving box"], "size": "1080p", "fps": 30,
                                    "output": {"width": 1281, "height": 721, "fps": 24}})
    out_o = (j.get("project") or {}).get("output", {})
    check("analyse: an output object overrides size/fps (sanitised)",
          code == 200 and (out_o.get("width"), out_o.get("height"), out_o.get("fps")) == (1280, 720, 24),
          str(out_o))
    code, j = post("/api/analyze", {"source": clips["moving box"], "output": {"preset": "warp"}})
    check("analyse: a bad output is a 400", code == 400, f"{code} {j}")

    code, j = post("/api/analyze", {"source": clips["moving box"], "size": "1080p", "fps": 30})
    check("POST /api/analyze plans a project", code == 200 and j["project"]["camera"]["shots"],
          json.dumps(j)[:200])
    nshots = len(j["project"]["camera"]["shots"])
    act = j["project"].get("analysis", {}).get("activity") or {}
    check("the project carries an activity track",
          act.get("t0") == 0.0 and act.get("dt", 0) > 0 and len(act.get("v", [])) > 10
          and "cuts" in j["project"]["analysis"], str(act)[:120])
    dur = j["project"]["sourceInfo"]["duration"]
    code, j = post("/api/suggest", {"t0": 0.5, "t1": min(dur, 3.0)})
    check("suggest frames the activity for a hand-placed zoom",
          code == 200 and j.get("zoom", 0) >= 1.3
          and all(0.5 / j["zoom"] - 1e-9 <= j[k] <= 1 - 0.5 / j["zoom"] + 1e-9 for k in ("cx", "cy")),
          f"{code} {j}")
    code, j = post("/api/suggest", {"t0": 2.0, "t1": 1.0})
    check("a backwards suggest range is a 400", code == 400, f"{code} {j}")

    code, j = post("/api/analyze", {"source": "/definitely/not/here.mov"})
    check("analyse of a missing file 400s with a message", code == 400 and "no such file" in j.get("error", ""))
    _txt = os.path.join(TMP, "notes-not-video.mov")
    with open(_txt, "w") as f:
        f.write("hello\n" * 100)
    code, j = post("/api/analyze", {"source": _txt})
    check("analyse of a file that is not a video says so in plain words",
          code == 400 and "can't read" in j.get("error", "") and "ffprobe" not in j.get("error", ""), str(j))

    # ---------------------------------------------------------- media prep
    m = wait_media()
    check("media prep finishes for the analysed clip",
          m.get("state") == "ready" and m.get("pct") == 100
          and m.get("source") == os.path.abspath(clips["moving box"]), str(m)[:300])
    if m.get("state") == "ready":
        proxy, strip = m["proxy"], m["strip"]
        with open(proxy, "rb") as f:
            head = f.read(12)
        check("the proxy is an mp4", head[4:8] == b"ftyp", repr(head))
        pinfo = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", proxy],
            capture_output=True, text=True).stdout)["streams"]
        vs = [s for s in pinfo if s["codec_type"] == "video"]
        check("the proxy is h264 yuv420p, video only",
              len(pinfo) == 1 and vs and vs[0]["codec_name"] == "h264" and vs[0]["pix_fmt"] == "yuv420p",
              str([(s["codec_type"], s.get("codec_name"), s.get("pix_fmt")) for s in pinfo]))
        check("the proxy keeps the source size under 1920 (even)",
              vs and vs[0]["width"] == 640 and vs[0]["height"] == 400, str(vs[0] if vs else None))
        check("the proxy is inside the media cache",
              os.path.dirname(os.path.dirname(proxy)) == os.path.abspath(media.ROOT), proxy)
        code, body, hdrs = get(fileurl(proxy), {"Range": "bytes=0-1023"})
        check("the proxy is served with Range support as video/mp4",
              code == 206 and len(body) == 1024 and hdrs.get("Content-Type") == "video/mp4"
              and "Content-Range" in hdrs, f"{code} {hdrs.get('Content-Type')}")
        want = min(90, max(8, round(dur)))
        check("strip meta: frame count from the duration",
              strip["count"] == want and strip["cols"] == 10
              and strip["rows"] == math.ceil(want / 10), str(strip))
        check("strip meta: 96px tiles at the source aspect",
              strip["th"] == 96 and strip["tw"] == 154 and strip["tw"] % 2 == 0, str(strip))
        check("strip meta: the tiles cover the clip",
              abs(strip["interval"] * strip["count"] - dur) < 1e-6, str(strip))
        code, body, hdrs = get(fileurl(strip["path"]))
        check("the strip is a JPEG served as image/jpeg",
              code == 200 and body[:3] == b"\xff\xd8\xff" and hdrs.get("Content-Type") == "image/jpeg")
        sim = _Img.open(strip["path"])
        check("the strip is exactly cols x rows tiles",
              sim.size == (strip["cols"] * strip["tw"], strip["rows"] * strip["th"]),
              f"{sim.size} vs {strip}")
        again = media.Prep()
        again.start(clips["moving box"])
        check("a prepared source is ready instantly from the cache",
              again.status()["state"] == "ready" and again.status()["proxy"] == proxy)

    # ---------------------------------------------------------- stills + files
    code, j = post("/api/still", {"t": 1.0, "width": 480})
    check("POST /api/still renders a frame", code == 200 and os.path.isfile(j.get("path", "")))
    stillpath = j.get("path", "")
    check("the still goes to the output folder under test",
          os.path.dirname(stillpath) == os.path.abspath(srv.OUT_DIR), stillpath)
    code, body, hdrs = get(fileurl(stillpath))
    check("the still is served back as image/png",
          code == 200 and body[:4] == b"\x89PNG" and hdrs.get("Content-Type") == "image/png")

    code, body, _ = get("/api/file?path=/etc/passwd")
    check("arbitrary files are NOT served", code == 404)
    code, body, _ = get("/api/file?path=" + urllib.parse.quote(os.path.expanduser("~/.ssh/id_rsa")))
    check("private files are NOT served", code == 404)

    # ---------------------------------------------------------- editing
    proj = post("/api/project", {})[1]["project"]
    shots = proj["camera"]["shots"]
    shots[-1]["zoom"] = 1.4
    shots[-1]["cx"] = 0.5
    shots[-1]["cy"] = 0.5
    code, j = post("/api/project", {"shots": shots})
    check("editing a shot rewrites the camera keys",
          code == 200 and any(abs(k[1] - 1.4) < 1e-6 for k in j["project"]["camera"]["keys"]))
    good_shots = j["project"]["camera"]["shots"]

    code, j = post("/api/project", {"shots": [{"start": 0.5, "end": 1.5, "zoom": 2}]})
    check("a shot without a reason is accepted as manual",
          code == 200 and j["project"]["camera"]["shots"][0]["reason"] == "manual", f"{code} {j}")
    code, j = post("/api/project", {"shots": [
        {"start": 2.0, "end": 3.0, "zoom": 20, "cx": -3, "cy": 7, "reason": "x", "id": "k1"},
        {"start": 0.0, "end": 2.0, "zoom": 0.2, "cx": 0.5, "cy": 0.5}]})
    sh2 = (j.get("project") or {}).get("camera", {}).get("shots", [])
    check("edited shots are sorted and clamped",
          code == 200 and [s["start"] for s in sh2] == [0.0, 2.0]
          and sh2[0]["zoom"] == 1.0 and sh2[1]["zoom"] == 8.0
          and (sh2[1]["cx"], sh2[1]["cy"]) == (0.0, 1.0), str(sh2))
    check("the editor's own shot keys survive", sh2 and sh2[1].get("id") == "k1", str(sh2))
    before = post("/api/project", {})[1]["project"]
    rejected = {}
    for label, body_ in (("start >= end", {"shots": [{"start": 2, "end": 2}]}),
                         ("non-numeric zoom", {"shots": [{"start": 0, "end": 1, "zoom": "big"}]}),
                         ("shots not a list", {"shots": {"start": 0}}),
                         ("NaN start", {"shots": [{"start": float("nan"), "end": 1}]}),
                         ("bad spring", {"spring": {"mass": "heavy"}}),
                         ("bad preset", {"output": {"preset": "ludicrous"}}),
                         ("output not an object", {"output": 5}),
                         ("trim backwards", {"trim": [2.0, 1.0]}),
                         ("trim before zero", {"trim": [-1.0, None]}),
                         ("trim past the end", {"trim": [0.0, dur + 5]}),
                         ("trim not a pair", {"trim": "all"}),
                         ("missing background image",
                          {"style": {"background": {"type": "image", "path": "/no/such.png"}}}),
                         ("background image that is not an image",
                          {"style": {"background": {"type": "image", "path": clips["static"]}}}),
                         ("style not an object", {"style": 3}),
                         ("a number too large for a float", {"trim": [10 ** 400, None]}),
                         ("a valid shot beside an invalid trim",
                          {"shots": [{"start": 0, "end": 1}], "trim": [5, 1]})):
        rejected[label] = post("/api/project", body_)[0]
    check("every bad edit is a 400, not a 500",
          all(c == 400 for c in rejected.values()), str({k: v for k, v in rejected.items() if v != 400}))
    check("a rejected edit leaves the project untouched",
          post("/api/project", {})[1]["project"] == before)
    code, j = post("/api/still", {"t": "soon"})
    check("a still at a non-numeric time is a 400", code == 400, f"{code} {j}")

    code, j = post("/api/project", {"shots": []})
    check("deleting every shot holds the camera wide",
          code == 200 and j["project"]["camera"]["keys"] == [], str(j)[:200])
    code, j = post("/api/project", {"spring": {"mass": 0, "stiffness": 99999, "damping": 40}})
    check("the spring is clamped and stored on the camera",
          code == 200 and j["project"]["camera"]["spring"] == {"mass": 0.1, "stiffness": 2000.0,
                                                                "damping": 40.0},
          str(j.get("project", {}).get("camera", {}).get("spring")))
    code, j = post("/api/project", {"output": {"width": 1001, "height": 10, "fps": 500,
                                               "crf": -3, "preset": "veryfast", "bogus": 1}})
    o = j.get("project", {}).get("output", {})
    check("output is sanitised (even, clamped, unknown keys dropped)",
          code == 200 and (o.get("width"), o.get("height"), o.get("fps"), o.get("crf"),
                           o.get("preset")) == (1000, 64, 120, 0, "veryfast") and "bogus" not in o,
          str(o))
    code, j = post("/api/project", {"output": {"width": 99999, "height": 1080.9}})
    o = j.get("project", {}).get("output", {})
    check("output sizes are capped at 7680 and rounded down to even",
          (o.get("width"), o.get("height")) == (7680, 1080), str(o))
    code, j = post("/api/project", {"trim": [1.0, dur]})
    check("a trim to the very end is stored as null",
          code == 200 and j["project"]["trim"] == [1.0, None], str(j.get("project", {}).get("trim")))
    code, j = post("/api/project", {"trim": [0.5, 2.5]})
    check("a trim inside the clip is stored as given",
          code == 200 and j["project"]["trim"] == [0.5, 2.5])
    bgpng = os.path.join(TMP, "my background.png")
    shutil.copy(stillpath, bgpng)
    code, j = post("/api/project", {"style": {"background": {"type": "image", "path": bgpng}}})
    check("an image background is accepted",
          code == 200 and j["project"]["style"]["background"]["path"] == os.path.abspath(bgpng),
          f"{code} {j.get('error')}")
    code, body, _ = get(fileurl(bgpng))
    check("...and served, so the preview can show it", code == 200 and body[:4] == b"\x89PNG")
    code, _, _ = get("/api/wallpaper-thumb?name=" + urllib.parse.quote(bgpng) + "&w=320&h=200")
    check("the thumbnail endpoint serves wallpapers only, never a path on disk", code == 404, str(code))
    # back to a quick, fully-specified state for the renders below
    code, j = post("/api/project", {"shots": good_shots, "trim": [0.0, None],
                                    "output": {"width": 1920, "height": 1080, "fps": 30,
                                               "crf": 20, "preset": "veryfast"},
                                    "style": {"background": before["style"]["background"]}})
    check("edits can be combined in one request", code == 200 and
          len(j["project"]["camera"]["shots"]) == nshots, f"{code} {j.get('error')}")

    # ---------------------------------------------------------- render
    code, j = post("/api/render/cancel", {})
    check("cancel with nothing running is a 409", code == 409, str(code))
    code, j = post("/api/render", {"preview": True})
    check("POST /api/render starts a render", code == 200 and j.get("output"))
    code2, j2 = post("/api/render", {"preview": True})
    check("a second render while one runs is a 409", code2 == 409, str(code2))
    prog = wait_render()
    check("the render finishes", prog.get("state") == "done", str(prog)[:200])
    check("progress carries started / preview / elapsed",
          isinstance(prog.get("started"), float) and prog.get("preview") is True
          and prog.get("elapsed", -1) >= 0 and "ended" not in prog, str(prog))
    if prog.get("state") == "done":
        check("the rendered file exists", os.path.isfile(prog["output"]))
        check("the preview lands in the output folder under test",
              os.path.dirname(prog["output"]) == os.path.abspath(srv.OUT_DIR), prog["output"])
        code, body, hdrs = get(fileurl(prog["output"]), {"Range": "bytes=0-1023"})
        check("video is served with Range support (so it can seek)",
              code == 206 and len(body) == 1024 and "Content-Range" in hdrs)
        code, body, hdrs = get(fileurl(prog["output"]), {"Range": "bytes=-100"})
        size = os.path.getsize(prog["output"])
        check("a suffix Range returns the last bytes",
              code == 206 and len(body) == 100
              and hdrs.get("Content-Range") == f"bytes {size - 100}-{size - 1}/{size}", str(hdrs))
        time.sleep(0.05)
        check("elapsed stops counting once the render is done",
              json.loads(get("/api/progress")[1])["elapsed"] == prog["elapsed"])

    code, j = post("/api/render", {"preview": True, "name": "../../My Export.mov"})
    named = os.path.join(os.path.abspath(srv.OUT_DIR), "My Export.mp4")
    check("a render name is a bare .mp4 in the output folder",
          code == 200 and j.get("output") == named, str(j))
    prog = wait_render()
    check("the named render finishes", prog.get("state") == "done" and os.path.isfile(named),
          str(prog)[:200])
    code, j = post("/api/render", {"output": clips["moving box"]})
    check("an export that would overwrite the recording is refused",
          code == 400 and os.path.getsize(clips["moving box"]) > 0, f"{code} {j}")
    _src = clips["moving box"]
    _swapped = os.path.join(os.path.dirname(_src), os.path.basename(_src).upper())
    if os.path.exists(_swapped):                  # case-insensitive: macOS, Windows
        code, j = post("/api/render", {"output": _swapped})
        check("...in any spelling of its name", code == 400 and os.path.getsize(_src) > 0, f"{code} {j}")
    else:
        skip("an export in another case is the recording", "this filesystem is case-sensitive")
    _link = os.path.join(TMP, "link-to-recording.mp4")
    try:
        os.symlink(_src, _link)
    except (OSError, NotImplementedError, AttributeError):
        skip("an export through a link to the recording is refused", "cannot make links here")
    else:
        code, j = post("/api/render", {"output": _link})
        check("...or through a link to it", code == 400 and os.path.getsize(_src) > 0, f"{code} {j}")

    cancel_out = os.path.join(os.path.abspath(srv.OUT_DIR), "cancel-me.mp4")
    code, j = post("/api/render", {"name": "cancel-me"})
    code_c, jc = post("/api/render/cancel", {})
    check("a running render can be cancelled", code == 200 and code_c == 200, f"{code} {code_c} {jc}")
    prog = wait_render()
    check("a cancelled render reports cancelled", prog.get("state") == "cancelled", str(prog)[:200])
    check("a cancelled render leaves no partial file", not os.path.exists(cancel_out))

    # ---------------------------------------------------------- save / load
    code, j = post("/api/save", {})
    check("save defaults to <recording>.zoomcut.json in the output folder",
          code == 200 and j.get("path") == os.path.join(os.path.abspath(srv.OUT_DIR),
                                                        "moving.zoomcut.json"), str(j))
    code, j = post("/api/save", {"path": os.path.join(TMP, "ui.json")})
    check("POST /api/save writes a project", code == 200 and os.path.isfile(j["path"]))
    srv.S.media.stop()
    srv.S.analysis = None
    code, j = post("/api/load", {"path": os.path.join(TMP, "ui.json")})
    check("POST /api/load reads it back", code == 200 and len(j["project"]["camera"]["shots"]) == nshots)
    ms = srv.S.media.status()
    deadline = time.time() + 60
    while time.time() < deadline:
        code_s, js = post("/api/suggest", {"t0": 0.5, "t1": 3.0})
        if code_s != 409:
            break
        time.sleep(0.25)
    check("after a load, new zooms are framed once the analysis warms up", code_s == 200,
          f"{code_s} {js}")
    check("loading a project starts media prep",
          ms["state"] in ("working", "ready") and ms["source"] == os.path.abspath(clips["moving box"]),
          str(ms)[:200])
    with open(os.path.join(TMP, "junk.json"), "w") as f:
        f.write("[1, 2")
    code, j = post("/api/load", {"path": os.path.join(TMP, "junk.json")})
    check("loading a broken project is a 400", code == 400, f"{code} {j}")
    code, j = post("/api/load", {"path": os.path.join(TMP, "nope.zoomcut.json")})
    check("loading a missing project is a 400", code == 400, f"{code} {j}")
    ui_json = os.path.join(TMP, "ui.json")
    with open(ui_json) as f:
        _pj = json.load(f)
    _pj["source"] = os.path.join(TMP, "no-longer-here.mov")
    with open(os.path.join(TMP, "gone.zoomcut.json"), "w") as f:
        json.dump(_pj, f)
    code, j = post("/api/load", {"path": os.path.join(TMP, "gone.zoomcut.json")})
    m = json.loads(get("/api/media")[1])
    check("a project whose recording is gone says so, instead of showing another clip",
          code == 200 and m["state"] == "error" and m["source"] == _pj["source"] and m["proxy"] is None,
          str(m)[:200])
    _twin = os.path.join(TMP, "elsewhere", os.path.basename(clips["moving box"]))
    os.makedirs(os.path.dirname(_twin), exist_ok=True)
    shutil.copy(clips["moving box"], _twin)
    post("/api/analyze", {"source": _twin, "size": "1080p", "fps": 30})
    code, j = post("/api/save", {})
    code2, j2 = post("/api/save", {})
    check("a recording that shares a name never overwrites the other one's project",
          code == 200 and j.get("path", "").endswith("moving (2).zoomcut.json")
          and j2.get("path") == j.get("path"), f"{j} {j2}")
    srv.S.analysis = None
    for _ in range(5):
        post("/api/load", {"path": ui_json})
    _warmers = [t for t in threading.enumerate() if t.name == "zoomcut-analysis" and t.is_alive()]
    check("loads in quick succession run one background analysis, not one each",
          len(_warmers) <= 1, str(len(_warmers)))

    # ---------------------------------------------------------- upload
    with open(DEMO_CLIP, "rb") as f:
        demo = f.read()
    code, j = upload("demo-recording.mp4", demo)
    imports = os.path.join(os.path.abspath(srv.OUT_DIR), "imports")
    up1 = j.get("path", "")
    check("a video upload lands in imports/",
          code == 200 and j.get("kind") == "video" and os.path.dirname(up1) == imports
          and j.get("size") == len(demo), str(j))
    if os.path.isfile(up1):
        with open(up1, "rb") as f:
            check("the upload is byte-identical", f.read() == demo)
    code, j = upload("demo-recording.mp4", demo)
    check("dropping the same file again reuses the first copy",
          code == 200 and j.get("path") == up1 and j.get("existing") is True
          and sorted(n for n in os.listdir(imports) if n.startswith("demo-recording")) == ["demo-recording.mp4"],
          f"{j} {os.listdir(imports)}")
    with open(clips["moving box"], "rb") as f:
        code, j = upload("demo-recording.mp4", f.read())
    check("a different file under the same name never overwrites",
          code == 200 and os.path.basename(j.get("path", "")) == "demo-recording (2).mp4", str(j))
    code, body, _ = get(fileurl(j.get("path", "")), {"Range": "bytes=0-9"})
    check("an upload can be played back straight away", code == 206 and len(body) == 10)
    code, j = upload("not really.mp4", b"this is a text file with a video name\n" * 40)
    check("an upload that is not a readable video is refused in plain words",
          code == 415 and "can't read" in j.get("error", "") and "ffprobe" not in j.get("error", ""),
          str(j))
    check("...and leaves nothing in imports",
          not [n for n in os.listdir(imports) if n.startswith("not really")], str(os.listdir(imports)))
    with open(bgpng, "rb") as f:
        code, j = upload("../../etc/Back ground!.PNG", f.read())
    check("an image upload lands in backgrounds/ under a clean name",
          code == 200 and j.get("kind") == "image"
          and j.get("path") == os.path.join(os.path.abspath(srv.OUT_DIR), "backgrounds",
                                            "Back ground.PNG"), str(j))
    code, j = upload("notes.txt", b"hello")
    check("an upload that is neither video nor image is a 415", code == 415, str(code))
    code, j = upload("README", b"hello")
    check("an upload with no extension is a 415", code == 415, str(code))
    code, body, _ = raw("POST", "/api/upload?name=a.mp4", headers={"X-Zoomcut-Upload": "1"})
    check("an upload without a Content-Length is a 411", code == 411, str(code))
    code, body, _ = raw("POST", "/api/upload?name=a.mp4", headers={
        "X-Zoomcut-Upload": "1", "Content-Length": str((64 << 30) + 1)})
    check("an upload over 64 GiB is a 413", code == 413, str(code))
    code, body, _ = raw("POST", "/api/upload?name=a.mp4", headers={
        "X-Zoomcut-Upload": "1", "Content-Length": "\u00b2"})
    check("a Content-Length that is not ASCII digits is a 411, not a 500", code == 411, f"{code} {body[:80]}")

    def _short(sock):
        sock.sendall(b"only ten b")
        sock.shutdown(socket.SHUT_WR)
    code, body, _ = raw("POST", "/api/upload?name=short.mp4", headers={
        "X-Zoomcut-Upload": "1", "Content-Length": "100000"}, send=_short)
    check("a truncated upload is a 400", code == 400, f"{code} {body[:80]}")
    check("...and leaves nothing behind",
          not [n for n in os.listdir(imports) if n.startswith("short") or n.endswith(".part")],
          str(os.listdir(imports)))

    # ---------------------------------------------------------- recents
    outd = os.path.abspath(srv.OUT_DIR)
    shutil.copy(clips["corner action"], os.path.join(outd, "capture-test.mov"))
    for junk in (".hidden.mp4", "half.mp4.part", "capture-test.mov.pad.mov", "notes.txt"):
        with open(os.path.join(outd, junk), "wb") as f:
            f.write(b"x")
    code, body, _ = get("/api/recent")
    rec = json.loads(body) if code == 200 else {}
    items = rec.get("items", [])
    by_name = {i["name"]: i for i in items}
    check("GET /api/recent answers with the output folder",
          code == 200 and rec.get("outDir") == srv.OUT_DIR, str(rec)[:200])
    check("recents: dot-files, .part, .pad.mov and non-media are skipped",
          not {".hidden.mp4", "half.mp4.part", "capture-test.mov.pad.mov", "notes.txt"} & set(by_name),
          str(sorted(by_name)))
    kinds = {n: i["kind"] for n, i in by_name.items()}
    check("recents: recordings, exports, imports and projects are told apart",
          kinds.get("capture-test.mov") == "recording" and kinds.get("moving-preview.mp4") == "export"
          and kinds.get("demo-recording.mp4") == "import" and kinds.get("moving.zoomcut.json") == "project",
          str(kinds))
    check("recents: an export under a name of its own is still an export",
          kinds.get("My Export.mp4") == "export", str(kinds.get("My Export.mp4")))
    check("recents: newest first",
          all(a["mtime"] >= b["mtime"] for a, b in zip(items, items[1:])))
    ct = by_name.get("capture-test.mov", {})
    check("recents: a recording is probed for its size and length",
          ct.get("width") == 640 and ct.get("height") == 400 and abs((ct.get("duration") or 0) - 4.0) < 0.2,
          str(ct))
    pjs = by_name.get("moving.zoomcut.json", {})
    check("recents: a project names its recording",
          pjs.get("source") == os.path.abspath(clips["moving box"]) and pjs.get("width") == 640, str(pjs))
    code, body, _ = get(fileurl(os.path.join(outd, "capture-test.mov")), {"Range": "bytes=0-9"})
    check("recents: every listed file can then be served", code == 206)
    srv.PROBE_BUDGET, _budget = -1.0, srv.PROBE_BUDGET
    shutil.copy(clips["static"], os.path.join(outd, "capture-late.mov"))
    late = {i["name"]: i for i in json.loads(get("/api/recent")[1])["items"]}
    srv.PROBE_BUDGET = _budget
    check("recents: past the probing budget sizes are left null, memoised ones kept",
          late["capture-late.mov"]["duration"] is None
          and late["capture-test.mov"]["duration"] is not None, str(late.get("capture-late.mov")))
    srv.OUT_DIR, _out = os.path.join(TMP, "never-created"), srv.OUT_DIR
    code, body, _ = get("/api/recent")
    srv.OUT_DIR = _out
    check("recents: a missing output folder is an empty list",
          code == 200 and json.loads(body)["items"] == [], str(body[:100]))

    # ---------------------------------------------------------- posters
    cap = os.path.join(outd, "capture-test.mov")
    code, body, hdrs = get("/api/poster?path=" + urllib.parse.quote(cap))
    ok_poster = code == 200 and body[:3] == b"\xff\xd8\xff"
    check("a poster is a JPEG with an hour's caching",
          ok_poster and hdrs.get("Content-Type") == "image/jpeg"
          and hdrs.get("Cache-Control") == "max-age=3600", f"{code} {hdrs}")
    if ok_poster:
        import io as _io
        check("a poster is 320 px wide", _Img.open(_io.BytesIO(body)).size[0] == 320)
    check("posters are cached under the posters folder",
          len([n for n in os.listdir(media.POSTERS) if n.endswith(".jpg")]) == 1)
    code, _, _ = get("/api/poster?path=/etc/passwd")
    check("a poster of an unlisted file is a 404", code == 404)
    code, _, _ = get("/api/poster?path=" + urllib.parse.quote(os.path.join(TMP, "never.mp4")))
    check("a poster of a missing file is a 404", code == 404)

    # ---------------------------------------------------------- misc
    code, j = post("/api/reveal", {"path": "/definitely/not/here.mp4"})
    check("revealing an unknown path is a 404", code == 404)
    if QUICK or not wp_body.get("wallpapers"):
        skip("large wallpaper thumbnails", "--quick" if QUICK else "no wallpapers")
    else:
        code, body, _ = get("/api/wallpaper-thumb?name=%s&w=1600&h=900"
                            % urllib.parse.quote(wp_body["default"] or wp_body["wallpapers"][0]))
        import io as _io
        check("a wallpaper thumbnail can be asked for at preview size",
              code == 200 and _Img.open(_io.BytesIO(body)).size == (1600, 904), str(code))
finally:
    _live = srv.S.media.proc
    srv.S.media.stop()
    srv.shutdown_session()
    httpd.shutdown()
    httpd.server_close()
    th.join(timeout=5)
check("the test server is shut down", not th.is_alive())
check("no media ffmpeg is left running", _live is None or _live.poll() is not None)

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
        # 2x on a retina panel, 1x on an ordinary external display
        check("captured at the window's size (at the display's scale)",
              any(abs(info["width"] - tgt["width"] * k) <= 4
                  and abs(info["height"] - tgt["height"] * k) <= 4 for k in (1, 2)),
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

# ==========================================================================
section("11 · editor media + request helpers")
lay = media.strip_layout(12.0, 1600, 1000)
check("strip layout: one tile a second, 10 across",
      (lay["count"], lay["cols"], lay["rows"], lay["tw"], lay["th"]) == (12, 10, 2, 154, 96)
      and lay["interval"] == 1.0, str(lay))
check("strip layout: at least 8 tiles, at least 64px wide",
      (lambda l: (l["count"], l["rows"], l["tw"]) == (8, 1, 64))(media.strip_layout(0.6, 400, 900)))
check("strip layout: at most 90 tiles, at most 256px wide",
      (lambda l: (l["count"], l["rows"], l["tw"]) == (90, 9, 256))(media.strip_layout(3600, 3840, 1080)))

_kf = os.path.join(TMP, "key.bin")
with open(_kf, "wb") as f:
    f.write(b"one")
_k1 = media.source_key(_kf)
check("a media key is 16 hex and stable",
      len(_k1) == 16 and all(c in "0123456789abcdef" for c in _k1) and media.source_key(_kf) == _k1)
with open(_kf, "wb") as f:
    f.write(b"two!")
check("a media key changes when the file does", media.source_key(_kf) != _k1)

_pr = os.path.join(TMP, "prune")
for i in range(15):
    d = os.path.join(_pr, f"e{i:02d}")
    os.makedirs(d)
    with open(os.path.join(d, "blob"), "wb") as f:
        f.write(b"x" * 1000)
    os.utime(d, (1_000_000 + i * 10, 1_000_000 + i * 10))
media.prune(_pr, keep=os.path.join(_pr, "e00"))
left = sorted(os.listdir(_pr))
check("media cache keeps the newest 12 entries (and the one in use)",
      left == ["e00"] + [f"e{i:02d}" for i in range(4, 15)], str(left))
media.prune(_pr, budget=3500)
check("media cache also fits a byte budget, oldest going first",
      sorted(os.listdir(_pr)) == ["e12", "e13", "e14"], str(sorted(os.listdir(_pr))))
_pf = os.path.join(TMP, "prune-files")
os.makedirs(_pf)
for i in range(5):
    with open(os.path.join(_pf, f"p{i}.jpg"), "wb") as f:
        f.write(b"x")
    os.utime(os.path.join(_pf, f"p{i}.jpg"), (2_000_000 + i, 2_000_000 + i))
media.prune_files(_pf, 3)
check("poster cache keeps the newest files", sorted(os.listdir(_pf)) == ["p2.jpg", "p3.jpg", "p4.jpg"])

media.ROOT = os.path.join(TMP, "media-2")
_prep = media.Prep()
_prep.start(clips["moving box"])
_prep.start(clips["corner action"])        # supersedes the first straight away


def _settle(p, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline and p.status()["state"] == "working":
        time.sleep(0.1)
    return p.status()


_ms = _settle(_prep)
check("a newer media job supersedes the older one",
      _ms["state"] == "ready" and _ms["source"] == os.path.abspath(clips["corner action"]), str(_ms)[:200])
_parts = [os.path.join(b, n) for b, _, fs in os.walk(media.ROOT) for n in fs if n.endswith(".part")]
check("a superseded job leaves no temp files", not _parts, str(_parts))
check("a finished job holds no process", _prep.proc is None)
_bogus = os.path.join(TMP, "not-a-video.mp4")
with open(_bogus, "w") as f:
    f.write("definitely not a video\n" * 50)
_prep.start(_bogus)
_ms = _settle(_prep)
check("media prep of a broken file reports a short error",
      _ms["state"] == "error" and _ms["message"] and "\n" not in _ms["message"]
      and len(_ms["message"]) <= 200, str(_ms))
_prep.stop()
check("stopping media prep returns it to idle", _prep.status()["state"] == "idle")
_long = mkclip(os.path.join(TMP, "long-for-media.mp4"), "testsrc2=s=1280x720", dur=30, size="1280x720")
_prep.start(_long)
_deadline = time.time() + 30
while time.time() < _deadline and _prep.proc is None and _prep.status()["state"] == "working":
    time.sleep(0.01)
_p = _prep.proc
_prep.stop()
check("stopping media prep mid-job kills its ffmpeg",
      _p is not None and _p.poll() is not None,
      "never saw the job running" if _p is None else f"poll={_p.poll()}")

check("names: folders are dropped", srv._safe_name("../../etc/passwd") == "passwd")
check("names: either slash is a folder", srv._safe_name("a/b\\c.mov") == "c.mov")
check("names: no dot-files", srv._safe_name(".hidden.mov") == "hidden.mov")
check("names: shell characters are dropped",
      srv._safe_name("rm -rf $(x);.mov") == "rm -rf (x).mov", srv._safe_name("rm -rf $(x);.mov"))
check("names: Windows device names are defused", srv._safe_name("CON.mp4") == "_CON.mp4")
import zoomcut.util as _u
_calls = {"n": 0}


def _locked_twice():
    _calls["n"] += 1
    if _calls["n"] <= 2:
        raise PermissionError(32, "The process cannot access the file because it is being used")
    return "done"


_was_win = _u.IS_WIN
try:
    _u.IS_WIN = True
    check("on Windows a file still held for a moment is waited out, not given up on",
          _u.patient(_locked_twice) == "done" and _calls["n"] == 3, str(_calls))
    _u.IS_WIN, _calls["n"] = False, 0
    try:
        _u.patient(_locked_twice)
        check("elsewhere a locked file fails at once", False)
    except PermissionError:
        check("elsewhere a locked file fails at once", _calls["n"] == 1)
finally:
    _u.IS_WIN = _was_win
_dev = [srv._safe_name(n) for n in ("NUL.x.mov", "nul .x.mov", "COM0.mp4", "LPT\u00b9.png", "console.log.mov")]
check("names: a device name before the first dot is defused too",
      _dev == ["_NUL.x.mov", "_nul .x.mov", "_COM0.mp4", "_LPT\u00b9.png", "console.log.mov"], str(_dev))
_pf2 = os.path.join(TMP, "prune-keep")
os.makedirs(_pf2)
for i in range(4):
    with open(os.path.join(_pf2, f"t{i}.jpg"), "wb") as f:
        f.write(b"x")
    os.utime(os.path.join(_pf2, f"t{i}.jpg"), (3_000_000 + i, 3_000_000 + i))
media.prune_files(_pf2, 2, keep_path=os.path.join(_pf2, "t0.jpg"))
check("pruning spares the file about to be served", sorted(os.listdir(_pf2)) == ["t0.jpg", "t2.jpg", "t3.jpg"],
      str(sorted(os.listdir(_pf2))))
_ln = srv._safe_name("x" * 300 + ".mp4")
check("names: at most 120 characters, extension kept", len(_ln) == 120 and _ln.endswith(".mp4"))
check("names: letters beyond ASCII survive", srv._safe_name("عرض.mov") == "عرض.mov")
check("export names are forced to .mp4",
      [srv._mp4_name(n) for n in ("demo.mov", "v1.2", "clip.MP4")] == ["demo.mp4", "v1.2.mp4", "clip.mp4"])
try:
    srv._mp4_name("../")
    check("an empty export name is refused", False)
except ZoomcutError:
    check("an empty export name is refused", True)
check("thumbnail sizes snap to 8 inside [32, 1920]",
      [srv._thumb_dim(v, 7) for v in ("5", "99999", "1000", "1003", "1005", None, "abc", "nan")]
      == [32, 1920, 1000, 1000, 1008, 7, 7, 7])
check("host names are compared without their port",
      [srv._hostname(h) for h in ("127.0.0.1:8765", "[::1]:8765", "::1", "LocalHost", "evil.example:80")]
      == ["127.0.0.1", "::1", "::1", "localhost", "evil.example"])

# ==========================================================================
section("12 · the editor's camera is the renderer's camera")
# The browser preview runs its own copy of the keyframing and the spring
# (zoomcut/web/camera.js), so what plays in the editor is what exports. This
# holds the two together: change one without the other and it fails here.
_node = shutil.which("node")
if not _node:
    skip("the editor's camera matches the renderer", "node is not installed")
else:
    _web = os.path.join(ROOT, "zoomcut", "web")
    _dir = os.path.join(TMP, "camera-js")
    os.makedirs(_dir, exist_ok=True)
    # .mjs, so any Node from 14 up treats it as a module without a package.json
    shutil.copy(os.path.join(_web, "camera.js"), os.path.join(_dir, "camera.mjs"))
    with open(os.path.join(_dir, "check.mjs"), "w") as f:
        f.write("""import fs from 'fs';
import { keyframes, simulate, cameraAt, shotsFromSegs, segsFromShots } from './camera.mjs';
const d = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sim = simulate(d.keys, d.spring, d.fps, d.t0, d.t1);
console.log(JSON.stringify({
  keys: keyframes(d.shots),
  states: d.times.map(t => cameraAt(sim, t)),
  roundtrip: keyframes(shotsFromSegs(segsFromShots(d.shots), d.dur)),
}));
""")
    _an = analyze(clips["moving box"])
    _shots = plan(_an)
    _keys = keyframes(_shots)
    _spring = {"mass": 1.7, "stiffness": 260.0, "damping": 31.0}
    _fps, _t0, _t1 = 30, 0.4, _an.duration

    def _py_states(keys):
        """render()'s camera loop: frame n is the state after n+1 steps."""
        k, m, c = _spring["stiffness"], _spring["mass"], _spring["damping"]
        z0, x0, y0 = target_at(keys, _t0)
        sz, sx, sy = Spring(math.log(z0), k, m, c), Spring(x0, k, m, c), Spring(y0, k, m, c)
        out = []
        for n in range(int((_t1 - _t0) * _fps)):
            tz, tx, ty = target_at(keys, _t0 + n / _fps)
            out.append((math.exp(sz.step(math.log(tz), 1 / _fps)),
                        sx.step(tx, 1 / _fps), sy.step(ty, 1 / _fps)))
        return out

    _want = _py_states(_keys)
    _in = os.path.join(_dir, "in.json")
    with open(_in, "w") as f:
        json.dump({"shots": [s.to_dict() for s in _shots], "keys": _keys, "spring": _spring,
                   "fps": _fps, "t0": _t0, "t1": _t1, "dur": _an.duration,
                   "times": [_t0 + n / _fps for n in range(len(_want))]}, f)
    _p = subprocess.run([_node, os.path.join(_dir, "check.mjs"), _in], capture_output=True, text=True)
    if _p.returncode != 0:
        check("the editor's camera module runs under node", False, _p.stderr[-400:])
    else:
        _got = json.loads(_p.stdout)
        _kd = max((abs(a - b) for ka, kb in zip(_got["keys"], _keys) for a, b in zip(ka, kb)), default=0)
        check("the editor keyframes shots exactly like the director",
              len(_got["keys"]) == len(_keys) and _kd < 1e-9, f"{len(_got['keys'])} vs {len(_keys)}, max diff {_kd}")
        _sd = max(abs(a - b) for sa, sb in zip(_got["states"], _want) for a, b in zip(sa, sb))
        check("the editor's spring follows the renderer's frame by frame (custom spring, trim, 30 fps)",
              len(_got["states"]) == len(_want) and _sd < 1e-9, f"max diff {_sd:.2e} over {len(_want)} frames")
        _rt = _py_states(_got["roundtrip"])
        _rd = max(abs(a - b) for sa, sb in zip(_rt, _want) for a, b in zip(sa, sb))
        check("editing zooms only (wide shots filled back in) leaves the camera unchanged",
              _rd < 1e-9, f"max diff {_rd:.2e}")

# ==========================================================================
section("13 · the server the desktop app runs")
# The desktop shell starts `zoomcut ui --port 0 --no-open --exit-with-stdin`,
# reads the address from the first line, and closes stdin to stop it - or
# dies, which closes it too. Exercised here exactly that way.
_env = {**os.environ, "ZOOMCUT_OUTPUT_DIR": os.path.join(TMP, "desktop-out"), "ZOOMCUT_DEMO": "1"}
_proc = subprocess.Popen([sys.executable, "-m", "zoomcut", "ui", "--port", "0", "--no-open",
                          "--exit-with-stdin"], cwd=ROOT, env=_env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
_url = None
_deadline = time.time() + 60
while time.time() < _deadline:
    _line = _proc.stdout.readline()
    if not _line:
        break
    if "zoomcut ui  ->" in _line:
        _url = _line.split("->", 1)[1].strip()
        break
check("asked for port 0, the server reports the port it really got",
      bool(_url) and not _url.endswith(":0/"), str(_url))
if _url:
    try:
        with urllib.request.urlopen(_url + "api/state", timeout=30) as _r:
            check("...and answers on it", _r.status == 200)
    except OSError as _e:
        check("...and answers on it", False, str(_e))
_proc.stdin.close()
try:
    _rc = _proc.wait(timeout=30)
except subprocess.TimeoutExpired:
    _proc.kill()
    _rc = None
check("closing its stdin stops the server cleanly", _rc == 0, f"exit {_rc}")
if _url:
    # nothing may still be listening (a bind would trip over TIME_WAIT)
    _port = int(_url.rstrip("/").rsplit(":", 1)[1])
    _s = socket.socket()
    _s.settimeout(3)
    try:
        _s.connect(("127.0.0.1", _port))
        _free = False
    except OSError:
        _free = True
    finally:
        _s.close()
    check("...and nothing is left listening on its port", _free)
_proc.stdout.close()

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
