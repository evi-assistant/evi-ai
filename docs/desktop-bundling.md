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
     repo root (the developer / source-checkout path; needs `evi-assistant[web]`
     installed).

So the *same* app binary works as a thin shell over system Python **and**
as a standalone app — it just depends on whether a sidecar was bundled.

## Default build (thin client / dev — no sidecar)

```
cd desktop
npm install
npm run build       # → installer under src-tauri/target/release/bundle/
```

This installer still needs Python 3.11+ and `pip install evi-assistant[web]` on
the target machine (local mode falls back to system Python). Good for your
own machines.

## Standalone build (embeds a frozen server)

Two steps, both **on the OS you're targeting** (PyInstaller and Tauri do
not cross-compile — build the Windows app on Windows, the macOS app on
macOS, etc.):

### 1. Freeze the server into a sidecar

```
# Build from an ISOLATED venv — see the warning below.
python -m venv .venv-build         # py -3.11 -m venv .venv-build on Windows
.venv-build/bin/pip install -e '.[web,pdf,index,build-desktop]'
scripts/build-sidecar.sh          # or scripts\build-sidecar.ps1 on Windows
```

> **⚠ Build from an isolated venv, not your fat dev `.venv`.** The build
> scripts pass `--collect-submodules evi`, which pulls *every* `evi.tools.*`
> module into the analysis. If your `.venv` has the `stt`/`computer`/`rerank`
> extras installed, that drags **torch + faster-whisper + sounddevice + av**
> into the supposedly-"practical" sidecar and balloons it from ~128 MB to
> >1 GB. `build-sidecar.{ps1,sh}` therefore prefer a **`.venv-build`** if one
> exists (then `.venv`, then system Python). Create `.venv-build` with only
> `web,pdf,index,build-desktop` installed and the sidecar stays lean.

This runs PyInstaller over `scripts/sidecar_entry.py` (which imports the
FastAPI `app` object directly and runs uvicorn) in **`--onedir`** mode,
producing a `dist/evi-server/` **folder** (`evi-server[.exe]` +
`_internal/`), then stages the whole folder at
`desktop/src-tauri/binaries/evi-server/` for Tauri's `bundle.resources`.

> **Why onedir, not onefile.** A `--onefile` exe self-extracts ~70 MB to a
> temp dir on *every* launch, which cost ~13–16 s of cold start. `--onedir`
> runs in place, so the app window appears in ~2–3 s. The trade-off is
> shipping a folder instead of one file — handled transparently by
> `bundle.resources` (below).

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
`bundle.resources` shipping the staged `binaries/evi-server/` folder. It's
kept **separate** from the default `tauri.conf.json` on purpose: declaring
the resource bundle there would make the plain `npm run build` fail
whenever a sidecar hasn't been frozen. Tauri unpacks the folder under the
app's resource dir at install time; `main.rs` resolves the binary from
`resource_dir()` (`<resources>/evi-server/evi-server[.exe]`), with
adjacent-exe fallbacks for the dev/staged layouts.

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

- **0.22.0 — verified end-to-end on Windows (2026-06-06).** Switched to
  `--onedir` + `bundle.resources`. Rebuilt the sidecar from an isolated
  `.venv-build` (127.9 MB; `evi-server --check` passes including
  `python_multipart`), built both installers
  (`eVi_0.1.0_x64_en-US.msi` 59.5 MB, `eVi_0.1.0_x64-setup.exe` 46.0 MB),
  and confirmed the built `evi-desktop.exe` resolves + spawns the sidecar,
  which serves `/api/health` 200 and the no-backend banner on a free port.
  Toolchain: Rust stable 1.96, MSVC BuildTools 2022, Tauri CLI 2.11,
  WebView2 148. (Installers are smaller than the 0.21.x onefile ones because
  the onedir folder's many small Python files compress better in MSI/NSIS.)
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
  `eVi_0.1.0_x64_en-US.msi` (~79 MB) and `eVi_0.1.0_x64-setup.exe` (NSIS,
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
  + evi). We ship **`--onedir`** (a folder, not one file): onefile binaries
  self-extract to a temp dir on *every* launch, which cost ~13–16 s here —
  onedir runs in place in ~2–3 s. The build scripts already use onedir.
- Code signing / notarization (macOS) and Authenticode (Windows) are out
  of scope here but required for friction-free distribution — wire them
  into the Tauri bundler config when you have certificates.

## All-Python fallback: pywebview

The Tauri shell is the shipped desktop app, but building it needs the full
Rust + Node + MSVC toolchain. If you only want a desktop **window** around
the web UI without that toolchain, **[pywebview](https://pywebview.flowrl.com/)**
is an all-Python alternative: start `evi web` on a local port and point a
`webview.create_window(...)` at `http://127.0.0.1:<port>`. It uses the OS
WebView (WebView2/WebKit), so the result is lightweight, but it is *not* a
distributable installer — it still needs Python + `evi-assistant[web]` on the
machine. Treat it as a dev/personal-use convenience, not a replacement for
the standalone Tauri build.

## Backend discovery in the standalone app

The bundled server does **not** bundle an LLM — it talks to whatever local
backend is running. Two pieces make that robust on a fresh machine:

- **llama.cpp port fallback.** llama.cpp defaults to `:8080`; when that's
  taken people bump to the next free port. `evi/portprobe.py`'s
  `discover_llamacpp_url` scans `8080..8090` and the llama.cpp backend
  (`discover_ports=True`) auto-picks the one actually serving an
  OpenAI-shaped `/v1/models`, so a busy default port doesn't hide it.
- **No-backend UX.** If nothing answers, the web UI shows a "⚠ No local LLM
  backend" banner with Start / Install / Recheck actions (see
  `GET /api/backend/status`) rather than failing silently — important in
  the standalone app, where the user may not have a backend installed yet.
