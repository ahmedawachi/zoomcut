"""Local web app: record, auto-cut, preview, export - all on 127.0.0.1.

Stdlib only. One session lives in the server process; the browser is just a
view onto it.
"""
from __future__ import annotations
import json, mimetypes, os, re, threading, time, traceback, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import wallpapers, recorder
from .analyze import analyze
from .director import DirectorConfig, plan, keyframes, Shot
from .project import new_project, save, load, import_style_preset, PRESETS, _deep_update
from .render import render, still
from .util import ZoomcutError, probe
from .windows import pickable, WindowListError

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
OUT_DIR = os.path.expanduser("~/Movies/Zoomcut")


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.rec: recorder.Recording | None = None
        self.analysis = None
        self.project: dict | None = None
        self.progress = {"state": "idle", "pct": 0, "message": "", "output": None}
        self.allowed: set[str] = set()

    def allow(self, path: str) -> str:
        p = os.path.abspath(path)
        self.allowed.add(p)
        return p


S = Session()


def _plan_project(source: str, style: dict | None, director: dict | None,
                  output: dict | None, style_preset: str | None = None,
                  preset_zooms: bool = False) -> dict:
    an = analyze(source)
    cfg = DirectorConfig()
    for k, v in (director or {}).items():
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, float(v))
    shots = plan(an, cfg)
    pj = new_project(source, keyframes(shots), shots, style=style,
                     output=output, director=cfg)
    if style_preset and os.path.isfile(style_preset):
        with open(style_preset) as f:
            imported = import_style_preset(json.load(f))
        _deep_update(pj["style"], imported["style"])
        if imported["spring"]:
            pj["camera"]["spring"] = imported["spring"]
        if preset_zooms and imported["manualKeys"]:
            pj["camera"]["keys"] = imported["manualKeys"]
    pj["analysis"] = {"cuts": an.cuts, "duration": an.duration}
    S.analysis = an
    return pj


def _rekey(pj: dict) -> dict:
    """Rebuild camera keys from the (possibly edited) shot list."""
    shots = [Shot(**{k: s[k] for k in ("start", "end", "zoom", "cx", "cy", "reason")})
             for s in pj["camera"].get("shots", [])]
    shots.sort(key=lambda s: s.start)
    if shots:
        pj["camera"]["keys"] = keyframes(shots)
    return pj


class Handler(BaseHTTPRequestHandler):
    server_version = "zoomcut"

    def log_message(self, fmt, *args):      # quiet by default
        pass

    # ---------------------------------------------------------------- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _serve_file(self, path: str):
        """Serve a produced file, with Range support so <video> can seek."""
        p = os.path.abspath(path)
        if p not in S.allowed or not os.path.isfile(p):
            return self._json({"error": "not available"}, 404)
        size = os.path.getsize(p)
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/windows":
                try:
                    return self._json({"windows": pickable()})
                except WindowListError as e:
                    return self._json({"windows": [], "error": str(e)})
            if u.path == "/api/wallpapers":
                return self._json({"wallpapers": [w["name"] for w in wallpapers.discover()],
                                   "default": wallpapers.default_name()})
            if u.path == "/api/state":
                rec = S.rec
                return self._json({
                    "recording": bool(rec and rec.running),
                    "recordSeconds": (time.time() - rec.started) if (rec and rec.running) else 0,
                    "hasProject": S.project is not None,
                    "progress": S.progress,
                    "permission": list(recorder.available()),
                    "outDir": OUT_DIR,
                })
            if u.path == "/api/project":
                return self._json({"project": S.project})
            if u.path == "/api/progress":
                return self._json(S.progress)
            if u.path == "/api/file":
                return self._serve_file((q.get("path") or [""])[0])
            return self._json({"error": "no such endpoint"}, 404)
        except Exception as e:                                   # pragma: no cover
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        try:
            if u.path == "/api/record/start":
                if S.rec and S.rec.running:
                    return self._json({"error": "already recording"}, 409)
                os.makedirs(OUT_DIR, exist_ok=True)
                name = b.get("name") or time.strftime("capture-%Y%m%d-%H%M%S")
                path = os.path.join(OUT_DIR, f"{name}.mov")
                region = b.get("region")
                S.rec = recorder.start(
                    path, mode=b.get("mode", "window"),
                    region=tuple(region) if region else None,
                    display=int(b.get("display") or 1),
                    window_id=b.get("windowId"),
                    cursor=bool(b.get("cursor", True)),
                    clicks=bool(b.get("clicks", False)))
                return self._json({"ok": True, "path": S.rec.path, "mode": S.rec.mode})

            if u.path == "/api/record/stop":
                if not S.rec:
                    return self._json({"error": "not recording"}, 409)
                path = recorder.stop(S.rec)
                S.allow(path)
                info = probe(path)
                S.rec = None
                return self._json({"ok": True, "path": path, "info": info})

            if u.path == "/api/analyze":
                src = b.get("source") or ""
                src = os.path.expanduser(src)
                if not os.path.isfile(src):
                    return self._json({"error": f"no such file: {src}"}, 400)
                S.allow(src)
                size = b.get("size") or "1440p"
                w, h = PRESETS.get(size, PRESETS["1440p"])
                out = {"width": w, "height": h, "fps": int(b.get("fps") or 60)}
                pj = _plan_project(src, b.get("style"), b.get("director"), out,
                                   b.get("stylePreset"), bool(b.get("presetZooms")))
                S.project = pj
                return self._json({"project": pj})

            if u.path == "/api/project":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                pj = S.project
                if "shots" in b:
                    pj["camera"]["shots"] = b["shots"]
                    _rekey(pj)
                if "style" in b:
                    _deep_update(pj["style"], b["style"])
                if "output" in b:
                    pj["output"].update(b["output"])
                if "trim" in b:
                    pj["trim"] = b["trim"]
                S.project = pj
                return self._json({"project": pj})

            if u.path == "/api/still":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                os.makedirs(OUT_DIR, exist_ok=True)
                t = float(b.get("t") or 0.0)
                out = os.path.join(OUT_DIR, ".preview-still.png")
                still(S.project, t, out, width=int(b.get("width") or 1100))
                S.allow(out)
                return self._json({"ok": True, "path": out, "t": t})

            if u.path == "/api/render":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                if S.progress.get("state") == "running":
                    return self._json({"error": "a render is already running"}, 409)
                preview = bool(b.get("preview"))
                os.makedirs(OUT_DIR, exist_ok=True)
                base = os.path.splitext(os.path.basename(S.project["source"]))[0]
                out = b.get("output") or os.path.join(
                    OUT_DIR, f"{base}{'-preview' if preview else '-zoomcut'}.mp4")
                out = os.path.abspath(os.path.expanduser(out))
                pj = json.loads(json.dumps(S.project))
                S.progress = {"state": "running", "pct": 0, "message": "starting", "output": None}

                def work():
                    def cb(n, total):
                        S.progress["pct"] = int(n * 100 / max(total, 1))
                        S.progress["message"] = f"{n}/{total} frames"
                    try:
                        render(pj, out, preview=preview, progress=cb)
                        S.allow(out)
                        S.progress = {"state": "done", "pct": 100,
                                      "message": "finished", "output": out}
                    except Exception as e:
                        traceback.print_exc()
                        S.progress = {"state": "error", "pct": 0,
                                      "message": str(e), "output": None}
                threading.Thread(target=work, daemon=True).start()
                return self._json({"ok": True, "output": out})

            if u.path == "/api/save":
                if not S.project:
                    return self._json({"error": "nothing analysed yet"}, 400)
                out = os.path.abspath(os.path.expanduser(
                    b.get("path") or os.path.join(OUT_DIR, "project.zoomcut.json")))
                save(S.project, out)
                S.allow(out)
                return self._json({"ok": True, "path": out})

            if u.path == "/api/load":
                p = os.path.abspath(os.path.expanduser(b.get("path") or ""))
                S.project = load(p)
                S.allow(p)
                S.allow(S.project["source"])
                return self._json({"project": S.project})

            if u.path == "/api/reveal":
                p = os.path.abspath(os.path.expanduser(b.get("path") or ""))
                if p in S.allowed and os.path.exists(p):
                    os.system(f"open -R {json.dumps(p)}")
                    return self._json({"ok": True})
                return self._json({"error": "not available"}, 404)

            return self._json({"error": "no such endpoint"}, 404)
        except ZoomcutError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:                                   # pragma: no cover
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def serve(host="127.0.0.1", port=8765, open_browser=True):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    ok, why = recorder.available()
    print(f"zoomcut ui  ->  {url}")
    print(f"screen recording: {'ready' if ok else 'BLOCKED - ' + why}")
    print("press Ctrl-C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
