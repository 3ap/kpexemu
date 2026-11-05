#!/bin/sh
# Builds the Windows zip: emulator + PyPy + SDL2 + run-kpex.bat.
#
#   packaging/windows/build-zip.sh DOWNLOADS_DIR OUTPUT.zip
#
# DOWNLOADS_DIR must contain pypy.zip (PyPy for Windows) and sdl.zip (SDL2 for Windows).
# PYPY_NAME is the top folder inside pypy.zip, e.g. pypy3.11-v8.0.1-win64.
set -eu

downloads=$1
output=$(realpath -m "$2")
: "${PYPY_NAME:?set PYPY_NAME to the folder name inside pypy.zip}"

root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
out=$work/kpex-emulator

mkdir -p "$out"
cp -r "$root/gui.py" "$root/run.py" "$root/kpex" "$root/tools" \
      "$root/README.md" "$root/HARDWARE.md" "$out/"
find "$out" -name __pycache__ -prune -exec rm -rf {} +

# Windows wants CRLF line endings in .bat files
sed 's/\r*$/\r/' "$root/packaging/windows/run-kpex.bat" > "$out/run-kpex.bat"

unzip -q "$downloads/pypy.zip" -d "$out"
mv "$out/$PYPY_NAME" "$out/pypy"
unzip -q "$downloads/sdl.zip" SDL2.dll README-SDL.txt -d "$out"

rm -f "$output"
(cd "$work" && zip -qr9 "$output" kpex-emulator)
echo "built $output ($(du -h "$output" | cut -f1))"
