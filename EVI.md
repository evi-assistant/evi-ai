# EVI.md — project context for developing eVi *with* eVi

This file is auto-loaded as project context when you run eVi from this repo
(`evi/project.py` reads `EVI.md` at the tree root). It's the **bootstrap doc**:
it teaches eVi's own agent how to navigate, change, test, and build this
codebase — so future updates can go through eVi instead of an external tool.

> You are working **on eVi itself**, at `C:\evi`. Prefer eVi's own conventions
> below over generic habits. When in doubt, read the relevant
> `docs/features/<area>.md` guide first.

## What eVi is

Local-first, single-user, privacy-first personal AI assistant. One Python core
(`evi/`) behind three frontends: **CLI** (`evi`, Typer/Rich), **Web** (FastAPI +
SSE), **Desktop** (Tauri 2 wrapping the web UI). Talks to local LLM backends
(LM Studio / Ollama / llama.cpp / any OpenAI-compatible). Full subsystem map:
[docs/features.md](docs/features.md); per-area deep dives in
[docs/features/](docs/features/README.md).

## Repo map (start here)

- `evi/config.py` — the config spine; everything hangs off `Config`.
- `evi/llm/agent.py` — the `Agent` tool-calling loop (the heart).
- `evi/backends/` — model backends (`factory.py` picks one).
- `evi/tools/` — the tool registry (`base.py`) + each tool.
- `evi/apps/cli/main.py` — every CLI command (Typer).
- `evi/apps/web/server.py` + `static/index.html` — web/desktop UI + `/api/*`.
- `desktop/` — the Tauri shell (Rust + `src-tauri/tauri*.conf.json`).
- `tests/` — unit (`test_*.py`), CLI e2e (`cli_e2e/`), browser e2e (`e2e/`).
- `docs/` — user docs (mirrored to the public wiki); `examples/` — drop-in samples.

## Environments — two venvs, never conflate

- **`.venv`** (`py -3.11`, ALL extras) — run the app + tests. Use
  `.venv\Scripts\python.exe` (Windows) / `.venv/bin/python` (POSIX).
- **`.venv-build`** — *only* for freezing the desktop sidecar. NEVER add
  torch/stt/computer/rerank here (PyInstaller `--collect-submodules evi` would
  balloon the sidecar from ~75 MB to >1 GB).

## Build / test / lint (the loop)

```bash
# unit (fast; e2e auto-excluded via addopts -m 'not e2e')
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check evi tests scripts

# CLI end-to-end (real `evi` subprocess; no browser)
.venv/Scripts/python.exe -m pytest tests/cli_e2e -m e2e --timeout=120

# Browser end-to-end (needs the [e2e] extra + chromium once)
.venv/Scripts/python.exe -m pytest tests/e2e -m e2e --timeout=120

# Build the desktop app (sidecar + Tauri) — the "eVi builds itself" entrypoint
powershell -File scripts\build-desktop.ps1     # Windows
bash scripts/build-desktop.sh                  # POSIX
```

See [docs/self-build.md](docs/self-build.md) for the full self-development loop
and [TESTING.md](TESTING.md) for the four test layers.

## Conventions (follow these)

- **Tests are mandatory.** Every change ships with a test. Any UI/`/api/*` change
  ships an e2e test in `tests/e2e/`; any CLI change is covered in
  `tests/cli_e2e/`. "A feature without a test is assumed broken."
- **Commits**: Conventional Commits (`feat(web): …`, `fix:`, `test:`, `docs:`).
  Multi-line messages via `git commit -F <file>` (PowerShell/bash heredocs mangle
  apostrophes/pipes). Don't push or release unless asked.
- **Docs ↔ wiki**: user-facing docs live in `docs/`; when you change them, mirror
  the touched files to the `dmang-dev/evi-ai-releases` wiki (see self-build.md).
- **DRY**: extract shared setup into a helper rather than copy >5 lines between
  two entry points.
- **Windows/PowerShell gotcha**: don't pipe a native exe's stderr with `*>>` /
  `2>&1` under `$ErrorActionPreference='Stop'` (it wraps stderr as a terminating
  error — e.g. PyInstaller's INFO banner). Use cmd/bash OS redirection.
- **Encoding**: keep CLI output ASCII-safe where practical (Windows console is
  cp1252); Rich handles em-dashes, but avoid box-drawing/✓/→ in plain prints.

## Current state (keep in mind)

- Releases are **PAUSED** on a GitHub Actions billing block (account-level; a
  human-only fix — do **not** touch billing or try to release). Local `main` is
  far ahead of the last published release; keep building locally.
- The test suite is green (`pytest -q` ≈ 980+ passing). Keep it that way.

## How to make a change (suggested flow)

1. Read the relevant `docs/features/<area>.md` + the module(s) it names.
2. Make the change with eVi's file tools.
3. Add/extend tests; run `pytest -q` (and the matching e2e suite).
4. `ruff check`. Update the feature doc + `examples/` if behavior changed.
5. Commit with a Conventional Commit message (only if asked to commit).
