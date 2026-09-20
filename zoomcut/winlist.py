"""Enumerate the windows a user could pick, on macOS, Windows and Linux.

Each backend returns the same shape:

    {id, app, title, x, y, width, height, layer, pid}

Coordinates are in the platform's own screen units (points on macOS, pixels
elsewhere) with the origin at the top-left of the primary display.
"""
from __future__ import annotations
import ctypes, ctypes.util, os, plistlib, re, shutil

from .util import IS_MAC, IS_WIN, IS_LINUX, run

_SKIP_APPS = {
    "Window Server", "Dock", "Control Center", "Notification Center",
    "SystemUIServer", "Spotlight", "Wallpaper", "coreautha",
    "Windows Input Experience", "Program Manager", "Windows Shell Experience Host",
}
_SKIP_TITLES = {"", "Default IME", "MSCTFIME UI", "Desktop", "Program Manager"}


class WindowListError(RuntimeError):
    pass


# ---------------------------------------------------------------- macOS
kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListExcludeDesktopElements = 1 << 4
kCFPropertyListXMLFormat_v1_0 = 100


def _mac_windows() -> list[dict]:
    """CoreGraphics via ctypes - a stock macOS python has no pyobjc, so we let
    CoreFoundation serialise the window list to a plist and read that."""
    try:
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics") or
                         "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or
                         "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError as e:                                        # pragma: no cover
        raise WindowListError(f"could not load CoreGraphics: {e}")

    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cf.CFPropertyListCreateData.restype = ctypes.c_void_p
    cf.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.c_uint64, ctypes.POINTER(ctypes.c_void_p)]
    cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    arr = cg.CGWindowListCopyWindowInfo(
        kCGWindowListExcludeDesktopElements | kCGWindowListOptionOnScreenOnly, 0)
    if not arr:
        raise WindowListError("CGWindowListCopyWindowInfo returned nothing")
    data = None
    try:
        err = ctypes.c_void_p()
        data = cf.CFPropertyListCreateData(None, ctypes.c_void_p(arr),
                                           kCFPropertyListXMLFormat_v1_0, 0, ctypes.byref(err))
        if not data:
            raise WindowListError("could not serialise the window list")
        n = cf.CFDataGetLength(ctypes.c_void_p(data))
        buf = ctypes.string_at(cf.CFDataGetBytePtr(ctypes.c_void_p(data)), n)
    finally:
        if data:
            cf.CFRelease(ctypes.c_void_p(data))
        cf.CFRelease(ctypes.c_void_p(arr))

    out = []
    for w in plistlib.loads(buf):
        b = w.get("kCGWindowBounds") or {}
        out.append({
            "id": int(w.get("kCGWindowNumber", 0)),
            "app": str(w.get("kCGWindowOwnerName", "") or ""),
            "title": str(w.get("kCGWindowName", "") or ""),
            "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
            "width": int(b.get("Width", 0)), "height": int(b.get("Height", 0)),
            "layer": int(w.get("kCGWindowLayer", 0)),
            "pid": int(w.get("kCGWindowOwnerPID", 0)),
        })
    return out


# ---------------------------------------------------------------- Windows
def _win_windows() -> list[dict]:
    user32 = ctypes.windll.user32                                # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32                            # type: ignore[attr-defined]
    try:
        dwm = ctypes.windll.dwmapi                               # type: ignore[attr-defined]
    except Exception:
        dwm = None
    user32.SetProcessDPIAware()

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    DWMWA_CLOAKED = 14
    out: list[dict] = []

    def frame(hwnd) -> RECT:
        r = RECT()
        # the real visible frame, without the invisible resize border
        if dwm is not None:
            ok = dwm.DwmGetWindowAttribute(ctypes.c_void_p(hwnd),
                                           ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
                                           ctypes.byref(r), ctypes.sizeof(r))
            if ok == 0 and (r.right - r.left) > 0:
                return r
        user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        return r

    def cloaked(hwnd) -> bool:
        if dwm is None:
            return False
        v = ctypes.c_int(0)
        if dwm.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
                                     ctypes.byref(v), ctypes.sizeof(v)) == 0:
            return bool(v.value)       # hidden UWP / other virtual desktop
        return False

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    def each(hwnd, _lparam):
        if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)) or cloaked(hwnd):
            return True
        n = user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 1)
        title = buf.value
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        app = _win_process_name(kernel32, pid.value) or title
        r = frame(hwnd)
        out.append({
            "id": int(ctypes.cast(hwnd, ctypes.c_void_p).value or 0),
            "app": app, "title": title,
            "x": r.left, "y": r.top,
            "width": r.right - r.left, "height": r.bottom - r.top,
            "layer": 0, "pid": pid.value,
        })
        return True

    user32.EnumWindows(each, None)
    return out


def _win_process_name(kernel32, pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = ctypes.c_ulong(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0]
    finally:
        kernel32.CloseHandle(h)
    return ""


# ---------------------------------------------------------------- Linux (X11)
def _linux_windows() -> list[dict]:
    if shutil.which("wmctrl"):
        p = run(["wmctrl", "-lGpx"])
        if p.returncode == 0:
            out = []
            for line in p.stdout.splitlines():
                # id desktop pid x y w h wm_class host title
                parts = line.split(None, 9)
                if len(parts) < 10:
                    continue
                wid, _desk, pid, x, y, w, h, wm_class, _host, title = parts
                app = wm_class.split(".")[-1] if wm_class and wm_class != "N/A" else title
                try:
                    out.append({"id": int(wid, 16), "app": app, "title": title.strip(),
                                "x": int(x), "y": int(y), "width": int(w), "height": int(h),
                                "layer": 0, "pid": int(pid)})
                except ValueError:
                    continue
            return out
    if shutil.which("xdotool"):
        ids = run(["xdotool", "search", "--onlyvisible", "--name", "."]).stdout.split()
        out = []
        for wid in ids:
            name = run(["xdotool", "getwindowname", wid]).stdout.strip()
            geo = run(["xdotool", "getwindowgeometry", "--shell", wid]).stdout
            g = dict(re.findall(r"^(\w+)=(-?\d+)$", geo, re.M))
            if not g.get("WIDTH"):
                continue
            out.append({"id": int(wid), "app": name.split(" - ")[-1] or name, "title": name,
                        "x": int(g.get("X", 0)), "y": int(g.get("Y", 0)),
                        "width": int(g["WIDTH"]), "height": int(g["HEIGHT"]),
                        "layer": 0, "pid": 0})
        return out
    raise WindowListError(
        "Listing windows on Linux needs wmctrl or xdotool.\n"
        "Install one (sudo apt install wmctrl) or record a region or the whole display instead.")


# ---------------------------------------------------------------- public
def list_windows() -> list[dict]:
    """Every on-screen window, or WindowListError explaining why we cannot say.

    Anything the platform backend throws is wrapped: callers should be able to
    fall back to region capture on one exception type, not guess at whatever
    ctypes or a missing helper decided to raise.
    """
    backend = (_mac_windows if IS_MAC else _win_windows if IS_WIN
               else _linux_windows if IS_LINUX else None)
    if backend is None:
        raise WindowListError(f"window listing is not supported on {os.name}")
    try:
        return backend()
    except WindowListError:
        raise
    except Exception as e:
        raise WindowListError(f"could not list windows: {type(e).__name__}: {e}") from e


def pickable(min_size: int = 200) -> list[dict]:
    """Just the real, user-facing windows - what a picker should offer."""
    seen, out = set(), []
    for w in list_windows():
        if w["layer"] != 0:
            continue
        if w["width"] < min_size or w["height"] < min_size:
            continue
        if w["app"] in _SKIP_APPS or not w["app"]:
            continue
        if w["title"] in _SKIP_TITLES and not IS_MAC:
            continue
        if w["id"] in seen:
            continue
        seen.add(w["id"])
        out.append(w)
    return out


def find(window_id: int) -> dict | None:
    for w in list_windows():
        if w["id"] == int(window_id):
            return w
    return None


def screen_size() -> tuple[int, int] | None:
    """Primary display size, used when a backend must be told what to grab."""
    if IS_LINUX:
        if shutil.which("xdpyinfo"):
            m = re.search(r"dimensions:\s+(\d+)x(\d+)", run(["xdpyinfo"]).stdout)
            if m:
                return int(m.group(1)), int(m.group(2))
        if shutil.which("xrandr"):
            m = re.search(r"current\s+(\d+)\s*x\s*(\d+)", run(["xrandr"]).stdout)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None
    if IS_WIN:
        user32 = ctypes.windll.user32                            # type: ignore[attr-defined]
        user32.SetProcessDPIAware()
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    return None
