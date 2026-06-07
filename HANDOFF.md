# Evi — Project Handoff & Migration Notes

_Last updated: 2026-06-06 · version 0.22.0_

This is a working-state snapshot for picking the project up on another machine.
Read the **Status**, **Open items**, and **Gotchas** sections first, then follow
**Migration**.

---

## 1. What Evi is

Local-first personal AI assistant living at `C:\evi`.

- **Shared Python core** (`evi/`) consumed by **three frontends**:
  - CLI — `evi` (Typer/Rich REPL)
  - Web — FastAPI + SSE at `evi/apps/web/` (single-page `static/index.html`)
  - Desktop — Tauri 2 shell in `desktop/` that wraps the web UI
- Talks to **local LLM backends**: LM Studio (`:1234`), Ollama (`:11434`),
  llama.cpp (`:8080`), or any OpenAI-compatible server.
- Distributed as PyPI package **`evi-ai`**; the import package stays **`evi`**.
- Big feature set already built across Phases 1–47: memory, MCP bridge, skills,
  scheduler, multi-backend model management, hooks/permissions, worktrees,
  dream engine, web+computer+voice(STT/TTS)+vision+OCR+PDF+SQLite tools,
  calendar, self-update, citations, routing, auth, etc.

## 2. Current status

- **Tests:** `570 passed, 1 skipped` (re-verified on this machine 2026-06-06,
  ~33s under the fresh `.venv`). Clean ruff.
- **Desktop:** standalone Tauri build works end-to-end on Windows — produces MSI
  + NSIS installers (~78–79 MB) embedding a ~72 MB PyInstaller sidecar.
- **Under version control now.** `git init` done — initial commit `208ea02`
  ("Evi v0.21.2", 235 files) on `main` (origin/main set). Working tree clean.
- **Migrated to this machine (`dusti`).** The old `.venv` (base interpreter
  under `C:\Users\Dustin Kost\…`) was dead on arrival; recreated with
  `py -3.11` (3.11.9) + all extras. cargo/node/npm are present here.
- We are mid **Phase 48** (desktop bundling polish + "no LLM backend" UX).
  Version **bumped to 0.22.0**; CHANGELOG + `docs/desktop-bundling.md` updated.
  Remaining Phase-48 work is the desktop sidecar rebuild → verify → installers.

### Work done in Phase 48 (now committed; the 0.22.0 changes are documented)

| Area | Change |
|---|---|
| Sidecar launch | `--onefile` → `--onedir` in `scripts/build-sidecar.*` → ~2.7 s launch (was ~16 s) |
| `desktop/src-tauri/src/main.rs` | Loading-shim pattern (non-blocking), `resource_dir()` sidecar resolution + adjacent-exe fallbacks, `CREATE_NO_WINDOW`, server log to `~/.evi/logs/desktop-server.log`, `app.windows: []` fix, tesseract env wiring |
| `desktop/src-tauri/tauri.standalone.conf.json` | `externalBin` → `bundle.resources` (ships the onedir folder) |
| `desktop/dist-shim/index.html` | "Starting Evi…" spinner that polls `/api/health` and redirects |
| **NEW `evi/portprobe.py`** | Socket-first port check, OpenAI-`/models`-shape validation, **llama.cpp 8080→8090 discovery**, `localhost`→`127.0.0.1` normalization |
| `evi/backends/llamacpp.py` | Auto-discovers a live llama.cpp on 8080–8090 when the configured port isn't one (`discover_ports=True`, cached) |
| `evi/apps/web/server.py` | `/api/backend/status` now probes **concurrently**, **caches 3 s**, validates OpenAI shape (kills 8080 false-positives), and reports llama.cpp's resolved port; `/api/backend/start` (ollama auto-start), `/api/backend/open-download` |
| `evi/apps/web/static/index.html` | "⚠ No local LLM backend" banner with Start/Install/Recheck; gates message send |
| Tests | NEW `tests/test_portprobe.py`; rewrote `tests/test_backend_status.py`; +5 in `tests/test_backends.py`; fixed a time-bomb date in `tests/test_transcripts.py` |

Bugs fixed this session: 8080 false-positive (any service answering `<500`
counted as an LLM); 13.8 s → ~1.5 s status latency; the Windows `localhost`
IPv6 (`::1`) connect stall; the stale-date transcript test.

## 3. Open items / TODO (in priority order)

**Phase 48 is complete.** Remaining desktop/distribution work:

1. **Verify the desktop-release CI on macOS + Linux.** `.github/workflows/
   desktop-release.yml` now exists (Win/mac/Linux matrix, `desktop-v*` tags).
   Only the Windows path is proven; treat the first green mac/Linux run as
   verification. Trigger via a `desktop-v*` tag or manual `workflow_dispatch`.
2. **Code-sign the desktop installers** — they're unsigned, so SmartScreen /
   Gatekeeper warn. Needs an Authenticode cert + Apple Developer ID wired
   into `tauri-action`. See `docs/releasing.md`.

**Done (2026-06-06):**

- ✅ **Desktop rebuild track verified end-to-end.** Rebuilt the onedir sidecar
  with the new portprobe/server code (127.9 MB; `evi-server --check` OK),
  regenerated both installers (`Evi_0.1.0_x64_en-US.msi` 59.5 MB,
  `Evi_0.1.0_x64-setup.exe` 46.0 MB), and confirmed the built
  `evi-desktop.exe` resolves + spawns the sidecar, which serves
  `/api/health` 200 and the no-backend banner. Toolchain installed: Rust
  stable 1.96 (was a 2022 nightly), via the existing rustup; MSVC 2022 +
  WebView2 + Tauri CLI 2.11 were already present.
- ✅ **Fixed: `python-multipart` missing from the `web` extra** — the
  `/api/transcribe` + `/api/upload` endpoints need it at route-registration
  time, so the standalone server crashed on boot. Was only present via the
  `mcp` extra. Now declared + a PyInstaller `--hidden-import`.
- ✅ **Fixed: sidecar build bloat** — `build-sidecar.{ps1,sh}` now prefer an
  isolated **`.venv-build`** so a fat dev `.venv` (stt/computer/rerank) can't
  drag torch/av/sounddevice into the practical-tier sidecar.
- ✅ **Version bump → 0.22.0** (`evi/__init__.py`, `pyproject.toml`) + a
  Phase-48 **CHANGELOG** entry + **`docs/desktop-bundling.md`** updated
  (onedir/`bundle.resources`, pywebview all-Python fallback, llama.cpp
  8080–8090 port fallback, the build-venv + multipart notes). Memory refreshed.
- ✅ **`git init`** — initial commit on `main` (was the old item #5).
- ✅ **`apps/` namespace** — verified resolved; no stray top-level `apps/`
  (frontends live under `evi/apps/`).
- ✅ **`.venv` recreated** on this machine (`py -3.11`, all extras) +
  **`.venv-build`** created (web/pdf/index/build-desktop only, for the sidecar).

## 4. Known issues & gotchas

- **Use the venv Python.** System `python` lacks the web deps. Use **`.venv`**
  (Python 3.11.9 on this machine) — it has `evi` installed editable plus all
  extras. Run tests/tools via `.venv\Scripts\python.exe`. A second
  **`.venv-build`** (web/pdf/index/build-desktop only) exists purely for
  freezing the sidecar — do NOT add the heavy extras to it.
- **Sidecar build venv must stay lean.** `build-sidecar.{ps1,sh}` prefer
  `.venv-build`; building from the fat `.venv` pulls torch/av/sounddevice into
  the practical-tier sidecar (>1 GB). See `docs/desktop-bundling.md`.
- **Windows `localhost` IPv6 stall:** connecting to a closed `::1` port is
  *dropped* (SYN filtered), not refused, so it blocks for the full timeout.
  `portprobe` pins probes to `127.0.0.1` to avoid multi-second stalls. Keep this
  in mind for any new local-port code.
- **This machine has a flaky non-LLM server on `:8080`** (returns 404 HTML /
  `RemoteProtocolError`, variable latency). That's environment noise, not an Evi
  bug — the probe correctly rejects it now.
- **Desktop runtime details:** Tauri picks a random free port and injects it via
  `window.__EVI_PORT__`; the sidecar logs to `~/.evi/logs/desktop-server.log`
  (block-buffered — may look empty until the process exits).
- **PowerShell 5.1 traps:** `$ErrorActionPreference="Stop"` aborts on a native
  command's *stderr* (rustup/npm warnings); `if(){}` is a statement, not an
  expression; `-WindowStyle Hidden` ≠ `CREATE_NO_WINDOW`; `setx` truncates at
  1024 chars (use `[Environment]::SetEnvironmentVariable(...,"User")`).
- **Tauri config:** avoid `"//"` comment keys (strict schema rejects them).

## 5. Toolchains

| Need | For |
|---|---|
| Python 3.11+ (3.12 OK) | everything Python |
| Rust + Cargo | desktop shell build only |
| MSVC C++ Build Tools | Rust linker on Windows (desktop only) |
| Node LTS + npm | `tauri` CLI (desktop only) |
| WebView2 runtime | desktop run (preinstalled on Win10/11) |
| tesseract / ffmpeg (optional) | OCR / audio tools (else those tools degrade) |

The PyPI package name is `evi-ai`; optional extras:
`email, web, mcp, scheduler, downloads, web-tools, computer, stt, pdf, index,
calendar, rerank, build-desktop, dev`.

---

## 6. Migration to another system

### 6a. What to copy vs. recreate

**Copy the repo `C:\evi`, but SKIP these reproducible/large dirs**
(all are in `.gitignore`):

- `.venv/` (recreate)
- `desktop/src-tauri/target/` — **1.6 GB** Rust build cache (recreate)
- `desktop/src-tauri/binaries/` — **250 MB** staged sidecar (rebuild)
- `desktop/node_modules/`, `build/`, `dist/`, `*.egg-info/`, `__pycache__/`

A clean copy of the source is only a few MB. Easiest: zip `C:\evi` excluding the
above, or just `git init` first and copy the working tree.

**Copy the user-data dir `%USERPROFILE%\.evi\`** — this is real state:
`config.toml`, `tokens/` (OAuth — **sensitive**, currently empty), `models/`,
`profiles/`, `skills/`, `commands/`, `transcripts/`, `indices/`, `images/`,
`screenshots/`, `uploads/`, `scheduled/`, `logs/`.

### 6b. Set up on the new machine

```powershell
# 1. Install toolchains (Python always; Rust+MSVC+Node only if building desktop)
# 2. Place the repo (ideally at the SAME path — see 6c for why), then:
cd C:\evi
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[web,mcp,scheduler,downloads,web-tools,computer,stt,pdf,index,calendar,rerank,email,dev]"

# 3. Sanity check
.\.venv\Scripts\python -m pytest -q          # expect ~570 passed
.\.venv\Scripts\python -m evi --version

# 4. (Desktop only) rebuild sidecar + installers
powershell -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1
cd desktop ; npm install ; npm run tauri build -- --config src-tauri\tauri.standalone.conf.json
```

Restore `%USERPROFILE%\.evi\` from your copy (or re-run `evi setup`).

### 6c. Keeping the Claude Code chat history / context  ← the important part

Claude Code stores per-project session transcripts as JSONL under:

```
%USERPROFILE%\.claude\projects\<mangled-project-path>\
```

For this project the folder is **`C--evi`** (the cwd `C:\evi` with the colon and
backslash turned into dashes). It contains:

- `*.jsonl` — one file per session. **`f21b243a-…jsonl` (21 MB) is THIS
  conversation.** `cd37adb2-…jsonl` is an earlier one.
- `f21b243a-…/` — per-session sidecar dir.
- `memory/` — the cross-session auto-memory: `MEMORY.md` + `project_evi.md`.

**To carry the history over:**

1. Copy the whole `%USERPROFILE%\.claude\projects\C--evi\` folder to the new
   machine's `%USERPROFILE%\.claude\projects\`.
2. **Match the path, or rename the folder.** The folder name is derived from the
   project's absolute path. If the new machine also puts the project at `C:\evi`,
   copy it as-is. If the path differs, rename the folder to the new mangled path:
   - Windows `D:\code\evi` → `D--code-evi`
   - macOS/Linux `/home/you/evi` → `-home-you-evi`
   (Replace the drive colon and every path separator with `-`.) If the folder
   name doesn't match the project's path, Claude Code won't link the history to
   the project.
3. In the new project dir, start Claude Code and **`--resume`** (or `--continue`
   for the latest). It reads the `.jsonl` and restores the conversation.
4. Optionally also copy your global `%USERPROFILE%\.claude\` settings if you want
   identical config — but that's machine/global, not project state.

> Note: chat history can be large (this one is 21 MB). Resuming replays it into
> context; expect a slower first turn.

---

## 7. Handy commands

```powershell
# Tests / lint (always via the venv)
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check evi tests scripts

# Run the web UI from source
.\.venv\Scripts\python -m uvicorn evi.apps.web.server:app --host 127.0.0.1 --port 8000

# Backend availability check (the Phase-48 UX)
#   GET /api/backend/status  → {configured, candidates[], any_reachable, ollama_installed}

# Desktop dev/run path needs cargo + node on PATH:
#   $env:USERPROFILE\.cargo\bin  and  $env:LOCALAPPDATA\node-lts
```

## 8. Layout cheatsheet

```
C:\evi
├─ evi/                     core library
│  ├─ portprobe.py          (NEW) local-server probing + llama.cpp port discovery
│  ├─ backends/             llamacpp.py has the 8080–8090 fallback
│  └─ apps/{cli,web}/       CLI + FastAPI frontends
├─ desktop/                 Tauri 2 shell
│  ├─ dist-shim/index.html  loading spinner
│  └─ src-tauri/            main.rs, tauri.conf.json, tauri.standalone.conf.json
├─ scripts/                 build-sidecar.*, evi-tools.*, sidecar_entry.py, install.*
├─ docs/                    desktop-bundling.md, sdk-coverage.md, etc.
├─ tests/                   pytest suite
└─ pyproject.toml           dist name evi-ai, import evi
```
```
%USERPROFILE%\.evi\         user data (config, tokens, models, transcripts, …)
%USERPROFILE%\.claude\projects\C--evi\   Claude Code chat history + memory
```
