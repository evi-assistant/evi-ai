# Build the full eVi desktop app end-to-end: freeze the Python web server into
# the sidecar, then bundle the Tauri app around it. This is the "eVi builds
# itself" entrypoint - eVi's own agent can run it via its shell tool.
#
# Run on Windows (PyInstaller doesn't cross-compile). See docs/self-build.md
# and docs/desktop-bundling.md.
#
#   powershell -File scripts\build-desktop.ps1
#
# Output: desktop\src-tauri\target\release\bundle\{msi,nsis}\
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path "$here\..").Path

Write-Host ">> [1/2] freezing the sidecar (PyInstaller)"
& "$here\build-sidecar.ps1"

Write-Host ">> [2/2] bundling the Tauri app (standalone config)"
# A GNU-toolchain build (no MSVC Build Tools) loads WebView2Loader.dll at
# runtime: stage it and bundle it next to the exe. Prints nothing for MSVC.
$py = if ($env:EVI_PYTHON) { $env:EVI_PYTHON } elseif (Test-Path "$root\.venv-build\Scripts\python.exe") { "$root\.venv-build\Scripts\python.exe" } elseif (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }
$extra = @()
$overlay = & $py "$here\stage_webview2_loader.py"
if ($LASTEXITCODE -ne 0) { throw "staging WebView2Loader.dll failed (see above)" }
if ($overlay) { $extra = @("--config", $overlay) }
Push-Location "$root\desktop"
try {
    # Native exe; a non-zero exit here is usually the optional updater-signing
    # step (needs the CI-only TAURI_SIGNING_PRIVATE_KEY). The installers are
    # still produced, so we report rather than abort.
    npm run tauri build -- --config src-tauri\tauri.standalone.conf.json @extra
    if ($LASTEXITCODE -ne 0) {
        Write-Host ">> tauri exited $LASTEXITCODE - likely the optional updater-signing step; check the bundle dir below."
    }
}
finally {
    Pop-Location
}

Write-Host ">> done. Installers:"
$target = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { "$root\desktop\src-tauri\target" }
Get-ChildItem -Recurse "$target\release\bundle" -Include *.msi, *.exe -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Host "   $($_.FullName)" }
