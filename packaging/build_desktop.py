#!/usr/bin/env python3
"""Build the desktop app for the machine this runs on.

    python3 packaging/build_desktop.py                 # installers into desktop/dist/
    python3 packaging/build_desktop.py --dir           # just the unpacked app, quicker
    python3 packaging/build_desktop.py --skip-server   # reuse dist/zoomcut as it is

Three steps, each usable on its own:

1. PyInstaller builds the Zoomcut server into dist/zoomcut - a folder build,
   which answers in about 0.3 s where a one-file build takes about 10 s.
2. packaging/fetch_ffmpeg.py puts the pinned, checksum-verified ffmpeg into it.
   That folder is also the command-line download, so it works without an
   ffmpeg install too.
3. electron-builder wraps it in a native app: a .dmg on macOS, an
   installer on Windows, an AppImage and a .deb on Linux.

Needs Python with numpy, Pillow and PyInstaller, and Node 22 or newer.
"""
from __future__ import annotations
import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESKTOP = os.path.join(ROOT, "desktop")
SERVER = os.path.join(ROOT, "dist", "zoomcut")

# What electron-builder reads to sign and notarize. A workflow hands a secret
# that was never added over as an empty string, and electron-builder takes an
# empty CSC_LINK for a certificate path - the current folder - and fails.
SIGNING = ("CSC_LINK", "CSC_KEY_PASSWORD", "CSC_NAME", "WIN_CSC_LINK", "WIN_CSC_KEY_PASSWORD",
           "APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID")


def run(cmd: list[str], cwd: str = ROOT, env: dict | None = None) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, env=env)


def signing_env() -> dict:
    """The environment, without signing variables that are set but empty."""
    return {k: v for k, v in os.environ.items() if v or k not in SIGNING}


def tool(name: str) -> str:
    """npm is npm.cmd on Windows; a bare name only works through a shell."""
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} was not found - the desktop app needs Node 22 or newer")
    return found


def build_server() -> None:
    shutil.rmtree(SERVER, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", os.path.join(HERE, "zoomcut.spec"), "--noconfirm",
         "--distpath", os.path.join(ROOT, "dist"),
         "--workpath", os.path.join(ROOT, "build", "pyinstaller")])


def add_ffmpeg(sources: str | None) -> None:
    cmd = [sys.executable, os.path.join(HERE, "fetch_ffmpeg.py"), "--dest", SERVER]
    if sources:
        cmd += ["--sources", sources]
    run(cmd)


def add_docs() -> None:
    for name in ("LICENSE", "README.md"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(SERVER, name))


def build_app(unpacked: bool) -> None:
    npm = tool("npm")
    if not os.path.isdir(os.path.join(DESKTOP, "node_modules")):
        run([npm, "ci", "--no-audit", "--no-fund"], cwd=DESKTOP)
    cmd = [npm, "run", "dist:dir" if unpacked else "dist"]
    run(cmd, cwd=DESKTOP, env=signing_env())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", action="store_true", help="only the unpacked app, no installers")
    ap.add_argument("--skip-server", action="store_true", help="reuse dist/zoomcut")
    ap.add_argument("--server-only", action="store_true", help="stop after the server and ffmpeg")
    ap.add_argument("--sources", help="also copy ffmpeg's source tarball here (for releases)")
    a = ap.parse_args(argv)

    if not a.skip_server:
        build_server()
        add_ffmpeg(a.sources)
        add_docs()
    elif not os.path.isdir(SERVER):
        raise SystemExit("dist/zoomcut does not exist yet - build it without --skip-server first")
    if a.server_only:
        return 0
    build_app(a.dir)
    out = os.path.join(DESKTOP, "dist")
    print("\nbuilt:")
    for name in sorted(os.listdir(out)):
        p = os.path.join(out, name)
        if os.path.isfile(p) and not name.endswith((".blockmap", ".yml", ".yaml")):
            print(f"  desktop/dist/{name}  ({os.path.getsize(p) / 1e6:.0f} MB)")
        elif os.path.isdir(p) and (name.endswith("unpacked") or name.startswith("mac")):
            print(f"  desktop/dist/{name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
