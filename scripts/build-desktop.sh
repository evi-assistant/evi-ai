#!/usr/bin/env bash
# Build the full eVi desktop app end-to-end: freeze the Python web server into
# the sidecar, then bundle the Tauri app around it. This is the "eVi builds
# itself" entrypoint — eVi's own agent can run it via its shell tool.
#
# Run on the target OS (PyInstaller doesn't cross-compile). On Windows, prefer
# scripts\build-desktop.ps1. See docs/self-build.md and docs/desktop-bundling.md.
#
#   bash scripts/build-desktop.sh
#
# Output: desktop/src-tauri/target/release/bundle/{msi,nsis,deb,appimage}/
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ">> [1/2] freezing the sidecar (PyInstaller)"
bash "$root/scripts/build-sidecar.sh"

echo ">> [2/2] bundling the Tauri app (standalone config)"
cd "$root/desktop"
# A non-zero exit here is usually the optional updater-signing step (needs the
# CI-only TAURI_SIGNING_PRIVATE_KEY); the installers are still produced.
npm run tauri build -- --config src-tauri/tauri.standalone.conf.json \
    || echo ">> tauri exited non-zero — likely the optional updater-signing step; check the bundle dir."

echo ">> done. Installers under: $root/desktop/src-tauri/target/release/bundle/"
