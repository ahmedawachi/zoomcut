#!/usr/bin/env python3
"""Run the test suite as if this machine were another platform.

    python3 tools/sim_platform.py linux
    python3 tools/sim_platform.py windows

It flips the IS_MAC / IS_WIN / IS_LINUX flags that the capture, window-listing
and wallpaper code branch on, then runs the quick suite. The real OS calls
still cannot happen - that is what CI on each platform is for - but every
branch of our own logic is exercised, which catches things like assuming a
machine has wallpapers installed or that a quirk of one OS applies to all.
"""
from __future__ import annotations
import os, runpy, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TARGETS = {"mac": ("macOS", (True, False, False)),
           "windows": ("Windows", (False, True, False)),
           "linux": ("Linux", (False, False, True))}


def main(target: str, argv: list[str]) -> int:
    if target not in TARGETS:
        print(f"usage: sim_platform.py [{'|'.join(TARGETS)}] [test args]", file=sys.stderr)
        return 2
    name, (is_mac, is_win, is_linux) = TARGETS[target]

    import zoomcut.util as u
    u.IS_MAC, u.IS_WIN, u.IS_LINUX = is_mac, is_win, is_linux
    u.platform_name = lambda: name

    import zoomcut.recorder as rec, zoomcut.winlist as wl
    import zoomcut.wallpapers as wp, zoomcut.server as srv
    for m in (rec, wl, wp):
        m.IS_MAC, m.IS_WIN, m.IS_LINUX = is_mac, is_win, is_linux
    rec.platform_name = srv.platform_name = u.platform_name
    if not is_mac:
        os.environ.pop("DISPLAY", None)

    print(f"### simulating {name}")
    print(f"### available(): {rec.available()}")
    print(f"### wallpapers: {len(wp.discover())} (default: {wp.default_name()})")
    sys.argv = ["test_all.py", *(argv or ["--quick"])]
    runpy.run_path(os.path.join(ROOT, "tests", "test_all.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2:]))
