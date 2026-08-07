# Freeze the eVi web server into a single `evi-server.exe` (the desktop
# "sidecar") with PyInstaller, then stage it for the Tauri bundle.
#
# MUST be run on Windows to produce a Windows sidecar — PyInstaller does not
# cross-compile. See docs/desktop-bundling.md.
#
# Prereqs (in your venv):
#   pip install -e '.[web]' pyinstaller
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path "$here\..").Path
# Prefer an isolated build venv (.venv-build) if present. A fat dev .venv with
# the stt/computer/rerank extras installed would drag torch + faster-whisper +
# sounddevice + av into the "practical tier" sidecar (--collect-submodules evi
# pulls every evi.tools.* module), ballooning it from ~75 MB to >1 GB. Create
# the isolated venv once with: py -3.13 -m venv .venv-build
$py = if (Test-Path "$root\.venv-build\Scripts\python.exe") { "$root\.venv-build\Scripts\python.exe" } elseif (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }

# Practical tier: bundle web + web-tools + pdf + index. STT + computer-use stay
# opt-in via a system Python. OCR works via a bundled tesseract binary.
# NOTE: `web` (fastapi/uvicorn — the server) and `web-tools` (ddgs/beautifulsoup4
# — web_search/web_fetch) are DISTINCT extras; both are needed or web search
# fails in the frozen app with "ddgs not installed".
Write-Host ">> ensuring practical extras are installed in the build venv"
# `claude-agent` installs the Claude Agent SDK so the `claude_agent` backend works
# in the frozen sidecar. We collect only its PYTHON modules below — NOT its
# `_bundled/claude(.exe)` (a ~250 MB vendored CLI that breaks the Linux AppImage
# bundler and is useless in a cross-OS bundle anyway). The SDK falls back to the
# system `claude` on PATH when its bundled copy is absent, which is what
# `claude_agent` needs regardless.
& $py -m pip install -q -e "$root[web,web-tools,pdf,index,claude-agent]"

# --onedir (NOT --onefile): a folder with evi-server.exe + _internal/. It
# launches near-instantly (no per-launch self-extraction), at the cost of
# being a directory. Tauri bundles the whole folder via bundle.resources;
# main.rs resolves evi-server.exe from the resource dir.
Write-Host ">> PyInstaller build (--onedir; web + web-tools + pdf + index)"
& $py -m PyInstaller `
    --onedir `
    --noconfirm `
    --name evi-server `
    --collect-submodules evi `
    --collect-data evi `
    --collect-submodules uvicorn `
    --collect-submodules fastapi `
    --collect-all pymupdf `
    --collect-all numpy `
    --collect-all ddgs `
    --collect-all bs4 `
    --collect-all soupsieve `
    --collect-submodules claude_agent_sdk `
    --collect-submodules mcp `
    --add-data "$root\docs;docs" `
    --hidden-import fitz `
    --hidden-import python_multipart `
    --hidden-import multipart `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan.on `
    --distpath "$root\dist" `
    --workpath "$root\build\pyinstaller" `
    --specpath "$root\build" `
    "$root\scripts\sidecar_entry.py"

# Stage the whole onedir folder for the Tauri resources bundle.
$src = "$root\dist\evi-server"
$dst = "$root\desktop\src-tauri\binaries\evi-server"
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
Copy-Item $src $dst -Recurse -Force
Write-Host ">> staged onedir: $dst"
Write-Host ">> now build the app:  cd desktop; npm run tauri build -- --config src-tauri\tauri.standalone.conf.json"
Write-Host ">> (or just run scripts\build-desktop.ps1 to do both steps)"
