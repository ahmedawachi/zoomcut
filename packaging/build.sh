#!/usr/bin/env bash
# Build a standalone Zoomcut executable for the machine you run this on.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install numpy pillow pyinstaller >/dev/null
rm -rf build dist
python3 -m PyInstaller packaging/zoomcut.spec --noconfirm --distpath dist --workpath build
echo
echo "built: dist/zoomcut$( [ "$OS" = "Windows_NT" ] && echo .exe || true )"
