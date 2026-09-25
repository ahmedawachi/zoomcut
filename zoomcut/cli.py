"""Command line for zoomcut."""
from __future__ import annotations
import argparse, json, os, signal, sys, time

from . import wallpapers
from .analyze import analyze, activity_track
from .director import DirectorConfig, plan, keyframes
from .project import new_project, save, load, import_style_preset, PRESETS, _deep_update
from .render import render, still
from .util import ZoomcutError, platform_name, output_dir
from . import recorder


def _style_overrides(a) -> dict:
    st: dict = {"background": {}}
    if a.background:
        b = a.background
        if b in ("gradient", "color"):
            st["background"]["type"] = b
        elif os.path.isfile(b):
            st["background"].update({"type": "image", "path": os.path.abspath(b)})
        else:
            st["background"].update({"type": "wallpaper", "name": b})
    if a.dim is not None:
        st["background"]["dim"] = a.dim
    if a.blur is not None:
        st["background"]["blur"] = a.blur
    if a.padding is not None:
        st["paddingRatio"] = a.padding
    return st


def _director_cfg(a) -> DirectorConfig:
    cfg = DirectorConfig()
    for k in ("max_zoom", "min_zoom", "context", "min_shot", "wide_hold"):
        v = getattr(a, k, None)
        if v is not None:
            setattr(cfg, k, v)
    return cfg


def _build(source: str, a) -> dict:
    an = analyze(source)
    shots = plan(an, _director_cfg(a))
    w, h = PRESETS[a.size]
    pj = new_project(source, keyframes(shots), shots,
                     style=_style_overrides(a),
                     output={"width": w, "height": h, "fps": a.fps},
                     director=_director_cfg(a))
    if getattr(a, "style_preset", None):
        with open(a.style_preset) as f:
            imported = import_style_preset(json.load(f))
        _deep_update(pj["style"], imported["style"])
        if imported["spring"]:
            pj["camera"]["spring"] = imported["spring"]
        if getattr(a, "preset_zooms", False) and imported["manualKeys"]:
            pj["camera"]["keys"] = imported["manualKeys"]
    pj["analysis"] = {"cuts": an.cuts, "duration": an.duration,
                      "activity": activity_track(an)}
    return pj


def _print_shots(pj: dict) -> None:
    print(f"  {'start':>6} {'end':>6} {'dur':>5}  {'zoom':>5} {'centre':>15}  what")
    for s in pj["camera"].get("shots", []):
        print(f"  {s['start']:6.2f} {s['end']:6.2f} {s['end']-s['start']:5.2f}  "
              f"{s['zoom']:5.2f}  ({s['cx']:.3f}, {s['cy']:.3f})  {s['reason']}")


def _progress(label: str):
    state = {"last": -1}
    def cb(n: int, total: int):
        pct = int(n * 100 / max(total, 1))
        if pct != state["last"]:
            state["last"] = pct
            print(f"\r  {label} {pct:3d}%  ({n}/{total} frames)", end="", flush=True)
    return cb


def cmd_wallpapers(a):
    for w in wallpapers.discover():
        print(f"  {'*' if w['ready'] else ' '} {w['name']:<32} {w['kind']}")
    print(f"\ndefault on this machine: {wallpapers.default_name()}")
    return 0


def cmd_record(a):
    ok, why = recorder.available()
    if not ok:
        print(f"error: {why}", file=sys.stderr)
        return 2
    region = tuple(int(v) for v in a.region.split(",")) if a.region else None
    mode = a.mode
    if getattr(a, "window", None) and mode == "display":
        mode = "window"          # --window implies window mode
    rec = recorder.start(a.output, mode=mode, region=region, display=a.display,
                         window_id=getattr(a, "window", None),
                         cursor=not a.no_cursor, clicks=a.clicks, limit=a.seconds)
    if a.seconds:
        print(f"recording {a.seconds}s -> {rec.path}")
        time.sleep(a.seconds + 0.5)
    else:
        print(f"recording -> {rec.path}\npress Ctrl-C (or Enter) to stop")
        try:
            stopped = False
            def on_int(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGINT, on_int)
            input()
        except (KeyboardInterrupt, EOFError):
            pass
    path = recorder.stop(rec)
    from .util import probe
    info = probe(path)
    # the backend picks the container, so hand the real path to whoever is next
    a.recorded_path = path
    print(f"\nsaved {path}  ({info['width']}x{info['height']}, {info['duration']:.2f}s)")
    return 0


def cmd_doctor(a):
    """Check everything Zoomcut needs, and say plainly what is missing."""
    from . import wallpapers
    from .util import tool, have, INSTALL_HINT
    from .winlist import pickable, WindowListError
    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK  ' if good else 'MISS'}  {label}" + (f"  —  {detail}" if detail else ""))

    print(f"Zoomcut {__import__('zoomcut').__version__} on {platform_name()}\n")
    if have("ffmpeg"):
        line(True, "ffmpeg", tool("ffmpeg"))
        line(have("ffprobe"), "ffprobe", tool("ffprobe") if have("ffprobe") else "missing")
    else:
        line(False, "ffmpeg", INSTALL_HINT.get(platform_name(), "see https://ffmpeg.org"))

    avail, why = recorder.available()
    line(avail, f"screen recording ({recorder.backend_name() or 'no backend'})",
         "" if avail else why)

    try:
        n = len(pickable())
        line(True, "window picking", f"{n} window(s) you could record")
    except WindowListError as e:
        line(False, "window picking", str(e).split("\n")[0])

    wl = wallpapers.discover()
    print(f"  OK    backgrounds  —  {len(wl)} wallpaper(s)"
          f"{' (gradient only)' if not wl else ''}")
    print(f"  OK    output folder  —  {output_dir()}")

    print("\n" + ("Everything Zoomcut needs is here. Run `zoomcut` to start."
                  if ok else "Fix the MISS lines above, then run `zoomcut doctor` again."))
    return 0 if ok else 1


def cmd_windows(a):
    from .winlist import pickable
    ws = pickable()
    if not ws:
        print("no pickable windows found")
        return 0
    print(f"  {'id':>7}  {'app':<24} {'size':>11}  title")
    for w in ws:
        print(f"  {w['id']:>7}  {w['app'][:24]:<24} {w['width']:>5}x{w['height']:<5}  {w['title'][:44]}")
    print("\nrecord one with:  zoomcut shoot --mode window --window <id>")
    return 0


def cmd_analyze(a):
    pj = _build(a.source, a)
    out = a.output or os.path.splitext(a.source)[0] + ".zoomcut.json"
    save(pj, out)
    print(f"{len(pj['camera']['shots'])} shots, cuts at "
          f"{', '.join(f'{c:.2f}s' for c in pj['analysis']['cuts']) or '(none)'}")
    _print_shots(pj)
    print(f"\nwrote {out}")
    return 0


def cmd_render(a):
    pj = load(a.project)
    if a.source:
        pj["source"] = os.path.abspath(a.source)
    out = a.output or os.path.splitext(a.project)[0] + ".mp4"
    render(pj, out, preview=a.preview, progress=_progress("rendering"))
    print(f"\nwrote {out}")
    return 0


def cmd_auto(a):
    pj = _build(a.source, a)
    out = a.output or os.path.splitext(a.source)[0] + ".zoomcut.mp4"
    if a.save_project:
        save(pj, a.save_project)
        print(f"project -> {a.save_project}")
    print(f"{len(pj['camera']['shots'])} shots:")
    _print_shots(pj)
    render(pj, out, preview=a.preview, progress=_progress("rendering"))
    print(f"\nwrote {out}")
    return 0


def cmd_shoot(a):
    """record -> analyse -> render, the whole cycle in one command."""
    rc = cmd_record(a)
    if rc:
        return rc
    a.source = getattr(a, "recorded_path", None) or a.output
    a.output = a.final or os.path.splitext(a.source)[0] + ".zoomcut.mp4"
    return cmd_auto(a)


def cmd_still(a):
    pj = load(a.project)
    still(pj, a.time, a.output, width=a.width)
    print(f"wrote {a.output}")
    return 0


def cmd_ui(a):
    from .server import serve
    serve(host=a.host, port=a.port, open_browser=not a.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("zoomcut", description=(
        "Record your screen and get a polished, edited-looking video with "
        "automatic zooms - no manual keyframing."))
    sub = p.add_subparsers(dest="cmd", required=True)

    def style_args(s):
        s.add_argument("--background", help="wallpaper name, image path, 'gradient' or 'color'")
        s.add_argument("--dim", type=float, help="darken the background 0..1 (default 0.12)")
        s.add_argument("--blur", type=float, help="blur the background in px (default 0)")
        s.add_argument("--padding", type=float, help="margin as a fraction of the short edge")
        s.add_argument("--size", choices=sorted(PRESETS), default="1440p")
        s.add_argument("--fps", type=int, default=60)
        s.add_argument("--style-preset", dest="style_preset",
                       help="an editor project .json to take the look from")
        s.add_argument("--preset-zooms", dest="preset_zooms", action="store_true",
                       help="also use that file's zoom ranges instead of the automatic ones")

    def director_args(s):
        s.add_argument("--max-zoom", type=float, dest="max_zoom")
        s.add_argument("--min-zoom", type=float, dest="min_zoom")
        s.add_argument("--context", type=float)
        s.add_argument("--min-shot", type=float, dest="min_shot")
        s.add_argument("--wide-hold", type=float, dest="wide_hold")

    def record_args(s):
        s.add_argument("--mode", choices=recorder.MODES, default="display")
        s.add_argument("--region", help="x,y,w,h in points (mode=region)")
        s.add_argument("--window", type=int, help="window id to record (mode=window)")
        s.add_argument("--display", type=int, default=1)
        s.add_argument("--seconds", type=float, help="stop automatically after N seconds")
        s.add_argument("--no-cursor", action="store_true")
        s.add_argument("--clicks", action="store_true", help="highlight mouse clicks")

    s = sub.add_parser("wallpapers", help="list the macOS wallpapers available here")
    s.set_defaults(func=cmd_wallpapers)

    s = sub.add_parser("doctor", help="check that everything Zoomcut needs is installed")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("windows", help="list windows you can record")
    s.set_defaults(func=cmd_windows)

    s = sub.add_parser("record", help="record the screen")
    s.add_argument("-o", "--output", default="recording.mov")
    record_args(s)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("analyze", help="detect beats and write a project file")
    s.add_argument("source")
    s.add_argument("-o", "--output")
    style_args(s); director_args(s)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("render", help="render a project file to video")
    s.add_argument("project")
    s.add_argument("-o", "--output")
    s.add_argument("--source", help="override the recording path")
    s.add_argument("--preview", action="store_true", help="fast 720p draft")
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("still", help="render one frame of a project")
    s.add_argument("project")
    s.add_argument("time", type=float)
    s.add_argument("-o", "--output", default="still.png")
    s.add_argument("--width", type=int, default=1280)
    s.set_defaults(func=cmd_still)

    s = sub.add_parser("auto", help="analyse + render an existing recording")
    s.add_argument("source")
    s.add_argument("-o", "--output")
    s.add_argument("--save-project")
    s.add_argument("--preview", action="store_true")
    style_args(s); director_args(s)
    s.set_defaults(func=cmd_auto)

    s = sub.add_parser("shoot", help="record, analyse and render in one go")
    s.add_argument("-o", "--output", default="recording.mov", help="where to keep the raw capture")
    s.add_argument("--final", help="the finished video (default: alongside the capture)")
    s.add_argument("--save-project")
    s.add_argument("--preview", action="store_true")
    record_args(s); style_args(s); director_args(s)
    s.set_defaults(func=cmd_shoot)

    s = sub.add_parser("ui", help="open the local web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_ui)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["ui"]          # running the app with no arguments opens it
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except ZoomcutError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
