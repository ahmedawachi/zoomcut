#!/usr/bin/env python3
"""Put a pinned, verified ffmpeg and ffprobe into a packaged Zoomcut.

    python3 packaging/fetch_ffmpeg.py --dest dist/zoomcut
    python3 packaging/fetch_ffmpeg.py --dest dist/zoomcut --platform linux-x64 --sources out

Every archive is checked against its sha256 in packaging/ffmpeg.json before
anything is taken out of it. The binaries land in <dest>/bin (and, on Linux,
their shared libraries in <dest>/lib, which the binaries find through their
$ORIGIN/../lib rpath). The GPL text and a notice naming the exact build go to
<dest>/licenses/ffmpeg/, and --sources copies the matching ffmpeg source
tarball out so a release can carry it next to the apps.

On the platform it was fetched for, the result is run to prove it: it must
report its version, encode with libx264, and have that platform's screen
grabber (gdigrab on Windows, x11grab on Linux).
"""
from __future__ import annotations
import argparse, hashlib, json, os, platform, posixpath, re, shutil, ssl, stat
import subprocess, sys, tarfile, urllib.error, urllib.request, zipfile
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "ffmpeg.json")
# the one name per library a binary actually asks for: libavcodec.so.62
SONAME = re.compile(r"lib[^/]+\.so\.\d+")


def host_platform() -> str:
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64" if m in ("x86_64", "amd64") else m
    return {"darwin": f"darwin-{arch}", "win32": f"win32-{arch}"}.get(sys.platform, f"linux-{arch}")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Said on every download. Some of the hosts answer Python's own
# "Python-urllib/3.x" with a 403, as bot traffic.
USER_AGENT = "zoomcut-build (+https://github.com/ahmedawachi/zoomcut)"


def download(url: str, dst: str) -> None:
    """urllib first. A Python without CA certificates (python.org's macOS
    installer until 'Install Certificates' has been run) falls back to curl,
    which uses the system's trust store."""
    tmp = dst + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError) or not shutil.which("curl"):
            raise SystemExit(f"could not download {url}: {e}")
        subprocess.run(["curl", "-fsSL", "--retry", "3", "-A", USER_AGENT, "-o", tmp, url], check=True)
    os.replace(tmp, dst)


def fetch(url: str, want: str, cache: str) -> str:
    """The archive, from the cache when a verified copy is already there."""
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, f"{want[:12]}-{os.path.basename(urlparse(url).path)}")
    if os.path.isfile(path) and sha256(path) == want:
        return path
    print(f"  downloading {url}")
    download(url, path)
    got = sha256(path)
    if got != want:
        os.remove(path)
        raise SystemExit(f"checksum mismatch for {url}\n  expected {want}\n  got      {got}\n"
                         "Refusing to use it. If the build was replaced upstream on purpose, "
                         "re-pin it in packaging/ffmpeg.json.")
    return path


def _write(dest: str, rel: str, data: bytes) -> str:
    out = os.path.join(dest, *rel.split("/"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    return out


def take_zip(path: str, take: dict[str, str], dest: str) -> list[str]:
    out = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for src, rel in take.items():
            if src not in names:
                raise SystemExit(f"{os.path.basename(path)} has no {src}")
            out.append(_write(dest, rel, z.read(src)))
    return out


def take_tar(path: str, take: dict[str, str], dest: str) -> list[str]:
    out = []
    with tarfile.open(path) as t:
        members = {m.name: m for m in t.getmembers()}

        def real(m):
            """Follow symlinks and hard links inside the archive to the file."""
            for _ in range(16):
                if m.issym():
                    m = members[posixpath.normpath(posixpath.join(posixpath.dirname(m.name), m.linkname))]
                elif m.islnk():
                    m = members[m.linkname]
                else:
                    return m
            raise SystemExit(f"link loop at {m.name}")

        for src, rel in take.items():
            if src.endswith("/"):
                # a library folder: one regular file per soname, links flattened
                picked = [m for n, m in members.items()
                          if n.startswith(src) and SONAME.fullmatch(posixpath.basename(n))]
                if not picked:
                    raise SystemExit(f"{os.path.basename(path)} has no libraries under {src}")
                for m in picked:
                    data = t.extractfile(real(m)).read()
                    out.append(_write(dest, rel + posixpath.basename(m.name), data))
            else:
                if src not in members:
                    raise SystemExit(f"{os.path.basename(path)} has no {src}")
                out.append(_write(dest, rel, t.extractfile(real(members[src])).read()))
    return out


def install_licenses(meta: dict, plat: str, src_tar: str, dest: str) -> None:
    """The GPL text from ffmpeg's own source, and a notice saying exactly
    which build this is and where its source lives."""
    lic = os.path.join(dest, "licenses", "ffmpeg")
    os.makedirs(lic, exist_ok=True)
    version = meta["version"]
    with tarfile.open(src_tar) as t:
        for name in ("COPYING.GPLv3", "LICENSE.md"):
            data = t.extractfile(f"ffmpeg-{version}/{name}").read()
            with open(os.path.join(lic, name), "wb") as f:
                f.write(data)
    with open(os.path.join(lic, "NOTICE.txt"), "w", encoding="utf-8") as f:
        f.write(f"""This copy of Zoomcut includes ffmpeg {version} ({plat}), built by {meta['builder']}.

ffmpeg is free software licensed under the GNU General Public License v3
(this build enables GPL components such as x264); see COPYING.GPLv3 and
LICENSE.md next to this file. Zoomcut itself is MIT licensed and runs ffmpeg
as a separate program.

The exact ffmpeg source for this version is attached to every Zoomcut
release as ffmpeg-{version}.tar.xz, and is also published at
  https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz
The build itself: {meta['builder_url']}
Its build scripts: {meta['build_scripts']}
""")


def verify(dest: str, plat: str) -> None:
    exe = ".exe" if plat.startswith("win32") else ""
    ff = os.path.join(dest, "bin", "ffmpeg" + exe)
    fp = os.path.join(dest, "bin", "ffprobe" + exe)

    def run(*args):
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"{' '.join(args)} failed:\n{p.stderr[-800:]}")
        return p.stdout
    print("  " + run(ff, "-hide_banner", "-version").splitlines()[0])
    run(fp, "-hide_banner", "-version")
    if "libx264" not in run(ff, "-hide_banner", "-encoders"):
        raise SystemExit("the bundled ffmpeg cannot encode with libx264")
    grab = {"win32": "gdigrab", "linux": "x11grab"}.get(plat.split("-")[0])
    if grab and grab not in run(ff, "-hide_banner", "-devices"):
        raise SystemExit(f"the bundled ffmpeg has no {grab}, so it cannot record the screen")
    print(f"  verified: libx264{' and ' + grab if grab else ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", required=True, help="the packaged Zoomcut folder (bin/ goes inside)")
    ap.add_argument("--platform", default=host_platform(), help="darwin-arm64, win32-x64 or linux-x64")
    ap.add_argument("--cache", default=os.path.join(ROOT, "build", "ffmpeg-cache"))
    ap.add_argument("--sources", help="also copy the matching ffmpeg source tarball here")
    a = ap.parse_args(argv)

    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    meta = manifest["platforms"].get(a.platform)
    if not meta:
        raise SystemExit(f"no ffmpeg is pinned for {a.platform}; "
                         f"known: {', '.join(manifest['platforms'])}")
    print(f"ffmpeg {meta['version']} for {a.platform}, built by {meta['builder']}")
    written = []
    for arc in meta["archives"]:
        path = fetch(arc["url"], arc["sha256"], a.cache)
        taker = take_zip if path.endswith(".zip") else take_tar
        written += taker(path, arc["take"], a.dest)
    for p in written:
        if os.sep + "bin" + os.sep in p or os.sep + "lib" + os.sep in p:
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    src = manifest["sources"][meta["version"]]
    src_tar = fetch(src["url"], src["sha256"], a.cache)
    install_licenses(meta, a.platform, src_tar, a.dest)
    if a.sources:
        os.makedirs(a.sources, exist_ok=True)
        shutil.copy(src_tar, os.path.join(a.sources, f"ffmpeg-{meta['version']}.tar.xz"))

    size = sum(os.path.getsize(p) for p in written)
    print(f"  {len(written)} files, {size / 1e6:.0f} MB, into {a.dest}")
    if a.platform == host_platform():
        verify(a.dest, a.platform)
    else:
        print("  (fetched for another platform - not run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
