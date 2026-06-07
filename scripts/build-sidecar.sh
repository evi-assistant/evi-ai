#!/usr/bin/env bash
# Freeze the eVi web server into a single `evi-server` binary (the desktop
# "sidecar") with PyInstaller, then stage it for the Tauri bundle.
#
# MUST be run on the SAME OS you're building the desktop app for —
# PyInstaller does not cross-compile. See docs/desktop-bundling.md.
#
# Prereqs (in your venv):
#   pip install -e '.[web]' pyinstaller
#
# Output: dist/evi-server/  →  copied to desktop/src-tauri/binaries/evi-server/
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
# Prefer $EVI_PYTHON, then an isolated build venv (.venv-build), then .venv,
# then python3. A fat dev .venv with the stt/computer/rerank extras would drag
# torch + faster-whisper + sounddevice + av into the "practical tier" sidecar
# (--collect-submodules evi pulls every evi.tools.* module), ballooning it from
# ~75 MB to >1 GB. Create the isolated venv once with: python3 -m venv .venv-build
if [ -n "${EVI_PYTHON:-}" ]; then py="$EVI_PYTHON"
elif [ -x "$root/.venv-build/bin/python" ]; then py="$root/.venv-build/bin/python"
elif [ -x "$root/.venv/bin/python" ]; then py="$root/.venv/bin/python"
else py="python3"; fi

# Practical tier: bundle web + pdf + index. STT (faster-whisper/PortAudio)
# and computer-use stay opt-in via a system Python — they're large and
# native-dep-heavy. OCR works via a bundled `tesseract` binary (see
# docs/desktop-bundling.md), not a Python dep.
echo ">> ensuring practical extras are installed in the build venv"
"$py" -m pip install -q -e "$root[web,pdf,index]"

# --onedir (NOT --onefile): a folder with the evi-server binary + _internal/.
# It launches near-instantly (no per-launch self-extraction). Tauri bundles
# the whole folder via bundle.resources; main.rs resolves the binary from
# the resource dir.
echo ">> PyInstaller build (--onedir; web + pdf + index)"
"$py" -m PyInstaller \
    --onedir \
    --noconfirm \
    --name evi-server \
    --collect-submodules evi \
    --collect-data evi \
    --collect-submodules uvicorn \
    --collect-submodules fastapi \
    --collect-all pymupdf \
    --collect-all numpy \
    --add-data "$root/docs:docs" \
    --hidden-import fitz \
    --hidden-import python_multipart \
    --hidden-import multipart \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan.on \
    --distpath "$root/dist" \
    --workpath "$root/build/pyinstaller" \
    --specpath "$root/build" \
    "$root/scripts/sidecar_entry.py"

# Stage the whole onedir folder for the Tauri resources bundle.
dst="$root/desktop/src-tauri/binaries/evi-server"
rm -rf "$dst"
mkdir -p "$(dirname "$dst")"
cp -R "$root/dist/evi-server" "$dst"
echo ">> staged onedir: $dst"
echo ">> now build the app:  cd desktop && npm run tauri build -- --config src-tauri/tauri.standalone.conf.json"
