# Desktop bundling

How the Tauri desktop app is built, and how to make it a **standalone**
installer that doesn't need Python on the target machine.

## Two run modes (no rebuild needed)

The Rust shell (`desktop/src-tauri/src/main.rs`) decides at launch:

1. **Remote** — if `EVI_REMOTE_URL` is set, it skips spawning anything and
   loads that URL. The desktop app is a thin window onto a server you run
   elsewhere.
2. **Local** — otherwise it starts a local server and points the webview
   at it. In local mode it prefers, in order:
   - a **bundled sidecar** binary named `evi-server[.exe]` sitting next to
     the app executable (the standalone path — no Python needed), else
   - **system Python**: `python -m uvicorn evi.apps.web.server:app` from the
     repo root (the developer / source-checkout path; needs `evi-ai[web]`
     installed).

So the *same* app binary works as a thin shell over system Python **and**
as a standalone app — it just depends on whether a sidecar was bundled.

## Default build (thin client / dev — no sidecar)

```
cd desktop
npm install
npm run build       # → installer under src-tauri/target/release/bundle/
```

This installer still needs Python 3.11+ and `pip install evi-ai[web]` on
the target machine (local mode falls back to system Python). Good for your
own machines.

## Standalone build (embeds a frozen server)

Two steps, both **on the OS you're targeting** (PyInstaller and Tauri do
not cross-compile — build the Windows app on Windows, the macOS app on
macOS, etc.):

### 1. Freeze the server into a sidecar

```
pip install -e '.[web,pdf,index,build-desktop]'
scripts/build-sidecar.sh          # or scripts\build-sidecar.ps1 on Windows
```

This runs PyInstaller over `scripts/sidecar_entry.py` (which imports the
FastAPI `app` object directly and runs uvicorn), producing
`dist/evi-server[.exe]`, then stages it at
`desktop/src-tauri/binaries/evi-server-<target-triple>` — the name Tauri's
`externalBin` mechanism expects.

> **Scope — the "practical" tier.** The sidecar bundles **web + pdf +
> index** Python deps, so chat, tools, image-gen, the web UI, PDF reading,
> and semantic search all work with no Python on the target machine.
> Deliberately left out (opt-in via a system Python, to keep the binary
> ~150–250 MB and dodge native-dep pain): **STT** (`faster-whisper` +
> PortAudio) and **computer-use** (`pyautogui`). A couple of models still
> download on first use regardless — the embedding model (from your
> backend) and, if you add STT later, the Whisper model.

### 1b. (optional) Bundle Tesseract for offline OCR

Tesseract is an external **binary**, not a Python dep — PyInstaller can't
freeze it. To make OCR work in the standalone app, stage the binary +
its `tessdata` next to the sidecar:

```
desktop/src-tauri/binaries/
    evi-server-<triple>          # the frozen server
    tesseract[.exe]              # the OCR binary for THIS os
    tessdata/eng.traineddata     # language data
```

At launch the Rust shell detects `tesseract[.exe]` next to the sidecar and
sets `EVI_TESSERACT_CMD` (+ `TESSDATA_PREFIX` if `tessdata/` is present)
for the server process; `evi/tools/ocr.py` honours those. Acquiring a
portable tesseract is OS-specific (no clean universal zip) — grab it from
your package manager's install dir, or `evi-tools install tesseract` then
copy `~/.evi/tools/bin/tesseract`. If you skip this step the app still
runs; OCR just reports tesseract missing.

### 2. Build the app with the sidecar merged in

```
cd desktop
npm run tauri build -- --config src-tauri/tauri.standalone.conf.json
```

`tauri.standalone.conf.json` is a small overlay that adds
`bundle.externalBin: ["binaries/evi-server"]`. It's kept **separate** from
the default `tauri.conf.json` on purpose: declaring `externalBin` there
would make the plain `npm run build` fail whenever a sidecar hasn't been
frozen. Tauri places the sidecar next to the app binary at install time
(triple suffix stripped), which is exactly where `sidecar_path()` in
`main.rs` looks for it.

## Toolchain setup (Windows, no-admin friendly)

The Tauri build needs Rust + Node + the **MSVC C++ Build Tools** (Rust's
linker on Windows) + WebView2 (preinstalled on Win10+). On a non-admin box
you can get most of it without elevation:

```powershell
# Node (portable zip, no admin) — extract a win-x64 zip from nodejs.org to
#   %LOCALAPPDATA%\node-lts and add it to your user PATH.
# Rust (per-user, no admin):
#   download https://win.rustup.rs/x86_64 → rustup-init.exe
rustup-init.exe -y --default-toolchain stable --default-host x86_64-pc-windows-msvc --profile minimal
# Tauri CLI:
cd desktop ; npm install
```

The **MSVC C++ Build Tools require admin** — run this in an elevated shell
(this is the one step that needs it):

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools --accept-package-agreements --accept-source-agreements --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
# …or download https://aka.ms/vs/17/release/vs_BuildTools.exe and run it with
#   --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended
```

`npx tauri info` reports exactly what's present/missing — use it to confirm
before building.

## Verification status

- **Sidecar build: verified on Windows (2026-05-29).**
  `scripts/build-sidecar.ps1` froze a working 72.7 MB `evi-server.exe`
  (practical tier: web+pdf+index). `evi-server.exe --check` loads all
  bundled deps (`fitz`, `numpy`, uvicorn protocols) and the server boots +
  answers `/api/health`. No PyInstaller flag tuning was needed beyond the
  `--collect-all pymupdf/numpy` + `--hidden-import fitz` already in the
  script.
- **Tauri app build: verified end-to-end on Windows (2026-05-29).** After
  installing the MSVC C++ Build Tools, `npm run tauri build -- --config
  src-tauri/tauri.standalone.conf.json` compiled (469 crates), linked, and
  produced both installers with the `evi-server` sidecar embedded:
  `Evi_0.1.0_x64_en-US.msi` (~79 MB) and `Evi_0.1.0_x64-setup.exe` (NSIS,
  ~78 MB). Two fixes were needed and are now in the repo:
  - `Cargo.toml` declared a `[lib] evi_desktop_lib` with no `src/lib.rs`
    (leftover from the create-tauri-app mobile template) — removed; it's a
    plain binary crate.
  - `tauri.conf.json` `bundle.icon` listed only `icon.png`; the Windows
    resource + WiX/NSIS bundlers need an `.ico`. Generated the full icon
    set with `npm run tauri icon` (from `icons/source.png`) and pointed
    `bundle.icon` at the standard 32/128/128@2x/icns/ico list.

## Size + signing notes

- Expect ~80–150 MB per sidecar (Python runtime + fastapi/uvicorn/pydantic
  + evi). One-file PyInstaller binaries self-extract to a temp dir on first
  launch, adding a small startup delay; use `--onedir` if that matters.
- Code signing / notarization (macOS) and Authenticode (Windows) are out
  of scope here but required for friction-free distribution — wire them
  into the Tauri bundler config when you have certificates.
