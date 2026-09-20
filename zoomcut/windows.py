"""List on-screen windows via CoreGraphics, using ctypes only.

pyobjc is not installed on a stock macOS python, so we call
CGWindowListCopyWindowInfo directly and let CoreFoundation serialise the
result to an XML property list, which plistlib can read. That avoids walking
CFDictionary/CFArray by hand.
"""
from __future__ import annotations
import ctypes, ctypes.util, plistlib

kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListExcludeDesktopElements = 1 << 4
kCGNullWindowID = 0
kCFPropertyListXMLFormat_v1_0 = 100

_SKIP_OWNERS = {"Window Server", "Dock", "Control Center", "Notification Center",
                "SystemUIServer", "Spotlight", "Wallpaper", "coreautha"}


class WindowListError(RuntimeError):
    pass


def _frameworks():
    cg_path = ctypes.util.find_library("CoreGraphics") or \
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    cf_path = ctypes.util.find_library("CoreFoundation") or \
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    return ctypes.CDLL(cg_path), ctypes.CDLL(cf_path)


def list_windows(on_screen_only: bool = True) -> list[dict]:
    """[{id, app, title, x, y, width, height, layer, pid}] front-most first."""
    try:
        cg, cf = _frameworks()
    except OSError as e:                                   # pragma: no cover
        raise WindowListError(f"could not load CoreGraphics: {e}")

    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cf.CFPropertyListCreateData.restype = ctypes.c_void_p
    cf.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_uint32, ctypes.c_uint64,
                                            ctypes.POINTER(ctypes.c_void_p)]
    cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    opts = kCGWindowListExcludeDesktopElements
    if on_screen_only:
        opts |= kCGWindowListOptionOnScreenOnly
    arr = cg.CGWindowListCopyWindowInfo(opts, kCGNullWindowID)
    if not arr:
        raise WindowListError("CGWindowListCopyWindowInfo returned nothing")
    data = None
    try:
        err = ctypes.c_void_p()
        data = cf.CFPropertyListCreateData(None, ctypes.c_void_p(arr),
                                           kCFPropertyListXMLFormat_v1_0, 0,
                                           ctypes.byref(err))
        if not data:
            raise WindowListError("could not serialise the window list")
        n = cf.CFDataGetLength(ctypes.c_void_p(data))
        buf = ctypes.string_at(cf.CFDataGetBytePtr(ctypes.c_void_p(data)), n)
    finally:
        if data:
            cf.CFRelease(ctypes.c_void_p(data))
        cf.CFRelease(ctypes.c_void_p(arr))

    raw = plistlib.loads(buf)
    out = []
    for w in raw:
        b = w.get("kCGWindowBounds") or {}
        entry = {
            "id": int(w.get("kCGWindowNumber", 0)),
            "app": str(w.get("kCGWindowOwnerName", "") or ""),
            "title": str(w.get("kCGWindowName", "") or ""),
            "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
            "width": int(b.get("Width", 0)), "height": int(b.get("Height", 0)),
            "layer": int(w.get("kCGWindowLayer", 0)),
            "pid": int(w.get("kCGWindowOwnerPID", 0)),
        }
        out.append(entry)
    return out


def pickable(min_size: int = 200) -> list[dict]:
    """Just the real, user-facing windows - what a picker should offer."""
    seen = set()
    out = []
    for w in list_windows():
        if w["layer"] != 0:                       # menu bar, dock, overlays
            continue
        if w["width"] < min_size or w["height"] < min_size:
            continue
        if w["app"] in _SKIP_OWNERS or not w["app"]:
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
