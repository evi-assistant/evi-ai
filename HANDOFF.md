# eVi — Project Handoff & Migration Notes

_Last updated: 2026-07-01 · v1.0.0 (desktop 1.0.0) · **PUBLIC**_

This is the working-state handoff for eVi. The 1.0 public launch is done: the repo is public under the `evi-assistant` org, the PyPI package `evi-assistant` and the desktop app are both at 1.0.0, and the `evi-skills` catalog is public. Read **Current status**, **Open items**, and **Gotchas** first, then follow **Migration** if you're moving to another machine.

---

## 1. What eVi is

A local-first personal AI assistant: **one shared Python core (`evi/`) behind three frontends**, talking to local LLM backends.

| Frontend | What it is |
|---|---|
| **CLI** | `evi` — Typer/Rich REPL (`evi/apps/cli/main.py`) |
| **Web** | FastAPI + SSE app (`evi/apps/web/server.py`) + vanilla-JS single-page UI in `static/` |
| **Desktop** | Tauri 2 shell (`desktop/`) wrapping the web UI, shipping a frozen Python sidecar |

- **Local LLM backends:** LM Studio (`:1234`, default), Ollama (`:11434`), llama.cpp (`:8080`, auto-discovers ports 8080–8090), or any OpenAI-compatible endpoint.
- **Distribution:** PyPI dist name is **`evi-assistant`**; the import package and CLI command both stay **`evi`**. Install with `pip install evi-assistant`, then `import evi` / run `evi`.
- **License:** MIT. **Python floor:** 3.13 (`requires-python = ">=3.13"`).

## 2. Current status

**1.0 shipped (2026-07-01):**

- **Public repo:** `https://github.com/evi-assistant/evi-ai` (transferred from the old private `dmang-dev/evi-ai`). All `[project.urls]` point at the `evi-assistant` org.
- **PyPI:** `evi-assistant` **v1.0.0** — Development Status classifier flipped to **Production/Stable**.
- **Desktop:** **v1.0.0**, full Windows/macOS/Linux signed matrix release; the in-app updater serves directly from the public repo (the private release-mirror channel is retired).
- **Skills:** `evi-skills` catalog is public.
- **Site:** landing page live at **https://evi-assistant.github.io/** — lives in the dedicated **`evi-assistant/evi-assistant.github.io`** org-pages repo (that special repo name is what serves at the domain root; a project-pages site could only serve under `/evi-ai/`).
- **No breaking API changes from 0.40.0** — 1.0.0 marks stability + public repo + a coordinated launch across the package, desktop app, and skills catalog.
- Version is consistent across `pyproject.toml`, `evi/__init__.py`, and `desktop/src-tauri/tauri.conf.json` (all `1.0.0`).

**Tests:** **1364 passed, 4 skipped** on the local `.venv` (32 e2e deselected by default via `addopts = -m 'not e2e'`); ~1451 collected including e2e. Live count: `pytest --collect-only -q`. Ruff clean.

> ✅ **Local `.venv` is now Python 3.13.14** (recreated 2026-07-01; full suite green on Windows/3.13). The 3.11 line is retired. `.venv-build` should likewise be recreated with `py -3.13` when next freezing the sidecar.

**1.0.0 bundles the whole 0.34–0.40 line:** specialty SLMs + the 7 capability chips, the guard-model guardrail layer, the models.dev catalog, the config linter (`evi lint`), completion notifications, pluggable web search, `evi skill add`, the project-intelligence pack (anatomy map, bug ledger, session reflection), the VS Code extension, local FIM completion, federation, ultracode, and the full CLI/web/desktop parity set.

## 3. Feature inventory

Core is ~135 top-level modules under `evi/` plus subpackages `evi/backends`, `evi/llm`, `evi/tools`, `evi/mcp`, `evi/apps`, `evi/sdk`.

**Agent + LLM layer (`evi/llm/`)**
- `agent.py` — the agent loop; `Agent.chat` streams completions and dispatches tool calls (yields TextDelta / ToolCall / ToolResult / Done / Error).
- `client.py` — dispatches to the configured backend (OpenAI-compatible chat client).
- `subagent.py` — scoped Agent runner backing `delegate_*` tools via `SUBAGENT_PROFILES`.
- `responses.py` — opt-in OpenAI Responses API path (`[llm] api = "responses"`); local backends stay on Chat Completions.
- `specialty.py` — specialty-model registry: route one task (OCR/vision/etc.) to a small dedicated model without swapping the main model, configured under `[models]`.

**Local-LLM backends (`evi/backends/`)** — `base.py` interface + `lmstudio.py`, `ollama.py`, `llamacpp.py` (auto-discovers a live llama.cpp on 8080–8090), `openai_compat.py`, `presets.py`, `factory.py` (picks backend from `LLMSettings.backend`).

**Model capability chips (`evi/capabilities.py`)** — **seven** best-effort chips (id-substring heuristics, with models.dev ground-truth override where known):

| Chip | | Chip | |
|---|---|---|---|
| 👁 Vision | `vision` | 🔧 Tools | `tools` |
| 🧠 Thinking | `reasoning` | 🛡 Guard | `guard` |
| ⌨ Infill | `infill` | ◆ Embeddings | `embed` |
| 🎤 Audio | `audio` | | |

**Models registry / catalog** — `recommend.py` (hand-curated registry + hardware-aware recommendation), `modelsdev.py` (models.dev catalog: context limit, modalities, tool/reasoning flags, pricing; baked snapshot `evi/data/models-catalog.json`, `evi models refresh` pulls the full catalog), `hardware.py` (GPU+RAM detection), `downloads.py` (Ollama / `hf:` pulls).

**Project-intelligence pack** — `anatomy.py` (token-estimated file index → `.evi/anatomy.md`, auto-injected), `bugledger.py` (append-only symptom→root-cause→fix ledger `.evi/bug-ledger.jsonl`, backs `record_fix`/`search_fixes`), `reflect.py` (session reflection into memory; `evi reflect` or a `session_end` hook).

**Code/dev tooling** — `pyanalyze.py` (AST symbol outline → `python_symbols` tool; Reflex Rust fast-walk with `[ast]` extra, else stdlib), `complete.py` (local FIM completion; `evi complete` + `/api/complete`), `configlint.py` (`evi lint` — static skill/hook/command/agent validation, also CI gate for evi-skills), `codeintel.py` (formatters/linters by extension; backs `format_on_edit` + `check_file`), `doctor.py` (`evi doctor` — environment checks).

**Tools (`evi/tools/`)** — `base.py` holds `REGISTRY` (`@tool`); `register_builtin_tools()` imports 22 modules: fs, code, shell, memory, skills, subagent, websearch, git, index, calendar, pdf, sqlite, ocr, rerank, monitor, image_comfy, voice, computer, federation, ask, vision_tool, bugledger. Notable: `resolver.py` (`search_tools` meta-tool — defers the long tail of tool schemas), `monitor.py` (bounded watch until regex/timeout), `ask.py` (`ask_user`), `shell.py`, `code.py` (`run_python` subprocess; sandboxed via `sandbox.py` when `[tools] sandbox` on).

**Multi-agent orchestration** — `ultracode.py` (fixed decompose→solve→critic→synthesize pipeline over one task), `workflows.py` (declarative TOML DAG, `parallel=true`, `~/.evi/workflows/*.toml`), `teams.py` (dynamic claimable shared task list with `blocked_by`), `recipes.py` (ordered prompts in one shared conversation).

**MCP (`evi/mcp/`)** — client side (`bridge.py`, `manager.py`, `servers.py`) consumes other servers' tools; `publish.py` (`evi mcp serve`) runs eVi **as** an MCP server (curated tools + `evi://memory/<name>` resources + command prompts over stdio and streamable HTTP).

**Federation** — `federation.py` + `tools/federation.py`: delegate a subtask to a trusted peer eVi via `POST /api/federate`; peers in `~/.evi/peers.json`; off by default (`[federation] serve = true`).

**Plugins & skills** — `plugins.py` (installable `plugin.toml` bundles under `~/.evi/plugins/`), `marketplace.py` (`evi plugin search`/`install`), `skills.py` + `skillscope.py` (active-skill tool scoping via SKILL.md frontmatter).

**Other core subsystems** — memory (`memory.py`, `dream.py`), semantic search (`embeddings.py`, `index.py`, `search.py`), session data/analytics (`transcripts.py`, `sessions.py`, `stats.py`, `finetune.py`, `context_report.py`), scheduling (`scheduler.py`, `scheduled.py`, `routines.py`), routing (`routing.py`), safety stack (`hooks.py`, `permissions.py`, `guardrails.py`, `guardmodel.py`, `moderation.py` — regex→judge→classifier), evals (`evals.py`), observability (`otel.py`, `reporting.py`), portable state (`sync.py`, `backup.py`, `update.py`, `worktree.py`, `profiles.py`, `users.py`), media (`voice.py`, `audio_input.py`, `diarize.py`, `vision.py`, `doclayout.py`), and the embeddable SDK (`sdk/builder.py`).

**VS Code extension** — `editors/vscode/` (TypeScript): ghost-text FIM via `/api/complete` + a chat webview via `/api/chat`. Deliberately kept out of the Python package.

## 4. Build / test / release

### Run the tests

```powershell
# Full unit suite (repo .venv):
.\.venv\Scripts\python.exe -m pytest -q --timeout=30
# Helper scripts: scripts\test.ps1 (Windows) / scripts/test.sh (Unix)  — bundle --timeout=15
# Verify the collect count (~1367 unit / ~1451 total):
.\.venv\Scripts\python.exe -m pytest --collect-only -q
# Lint:
.\.venv\Scripts\python.exe -m ruff check evi tests scripts
# E2E (opt-in, Ubuntu-only in CI): needs .[e2e] + playwright chromium
.\.venv\Scripts\python.exe -m pytest tests/e2e -m e2e --timeout=120
```

### Two-venv model

| Venv | Purpose |
|---|---|
| **`.venv`** | Fat local **dev** env — all extras (incl. stt/computer/rerank → torch/av/sounddevice). |
| **`.venv-build`** | Isolated env used **only** to freeze the desktop sidecar (`.[web,pdf,index,build-desktop]`). |

The sidecar freeze uses PyInstaller `--collect-submodules evi`, which pulls **every** `evi.tools.*` module. Building from the fat `.venv` would drag torch/av/sounddevice into the "practical tier" sidecar, ballooning it from ~75 MB to >1 GB — hence the separate lean build env. The build scripts auto-prefer `.venv-build` when present.

### Cut a PyPI release (`release.yml`)

Trigger: push a `v*.*.*` tag (also `workflow_dispatch` for manual re-publish). Steps: sanity-check the tag equals `pyproject` version → `python -m build` → install `.[dev,web,mcp,scheduler,downloads,web-tools,index]` + pytest → publish to PyPI via OIDC **Trusted Publishing** (no API token) → sigstore keyless signing → GitHub release with auto notes + dist artifacts.

**Bump two files** (`pyproject.toml [project] version` **and** `evi/__init__.py __version__`), then:

```powershell
git add pyproject.toml evi/__init__.py CHANGELOG.md
git commit -m "release: X.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

> The workflow's tag-vs-version tripwire only checks `pyproject.toml`, **not** `evi/__init__.py` — a forgotten `__init__` bump would ship a mismatched `evi --version` without failing CI.

### Cut a desktop release (`desktop-release.yml`)

Trigger: push a `desktop-v*` tag (versions independently of the package; `workflow_dispatch` with blank input = artifacts-only). Matrix: windows/macos/ubuntu (`fail-fast: false`). Per OS: setup Python 3.13 + Rust + Node, freeze the sidecar in a fresh `.venv-build` + `evi-server --check`, `npm install`, then `tauri-action` with `--config src-tauri/tauri.standalone.conf.json`. Publishes a **non-draft** release; signs updater artifacts (`.sig` + `latest.json`) with `TAURI_SIGNING_PRIVATE_KEY[_PASSWORD]`.

**Bump four files** (all currently `1.0.0`): `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml`, `desktop/package.json`, and the `evi-desktop` entry in `desktop/src-tauri/Cargo.lock`. Then:

```powershell
git tag desktop-vX.Y.Z
git push origin desktop-vX.Y.Z
```

Updater endpoint: `https://github.com/evi-assistant/evi-ai/releases/latest/download/latest.json`. The version in `latest.json` must increase for clients to update. Updater signing is **minisign**, not OS code-signing — SmartScreen/Gatekeeper still warn (see Open items).

### Freeze the sidecar / build the whole desktop app (Windows)

```powershell
py -3.13 -m venv .venv-build
scripts\build-sidecar.ps1
desktop\src-tauri\binaries\evi-server\evi-server.exe --check
powershell -File scripts\build-desktop.ps1   # end-to-end
```

PyInstaller does **not** cross-compile — each OS's installer must be built on that OS (the CI matrix handles this per-runner).

### CI workflows (7 total in `.github/workflows/`)

| Workflow | Trigger | Notes |
|---|---|---|
| `ci.yml` | push/PR to main, weekly, dispatch | Lint + unit gate. Push→1 cheap ubuntu/3.13 job; full 3-OS matrix only on PR/weekly/dispatch (macOS bills ~10×). |
| `release.yml` | `v*.*.*` tag, dispatch | PyPI Trusted Publishing + sigstore + GitHub release. |
| `desktop-release.yml` | `desktop-v*` tag, dispatch | 3-OS Tauri matrix, non-draft, minisign-signed updater. |
| `security.yml` | push/PR, weekly, dispatch | pip-audit, cargo-audit, gitleaks, CodeQL (python+js). |
| `e2e.yml` | PR, weekly, dispatch | Playwright UI tests, Ubuntu only, fake in-thread backend. |
| `docker.yml` | `v*.*.*` tag, dispatch | GHCR image (web+scheduler), push opt-in. |
| `evi-run-example.yml` | — | Example workflow. |

Build backend: setuptools ≥68 + wheel. **21** optional-dependency extras (`email, web, mcp, scheduler, downloads, web-tools, computer, stt, pdf, index, calendar, rerank, ast, diarize, doc, telemetry, otel, moderation, e2e, build-desktop, dev`).

## 5. Open items / next steps

None block the 1.0 launch. Everything below is optional/hardening or forward-looking.

**Still open:**

- **OS code-signing** for the desktop installers (Windows Authenticode, Apple Developer ID) — updater minisign signing is done, but SmartScreen/Gatekeeper still warn. Needs certs/secrets (user-provided).
- **Tracked — Dependabot alert #1: `glib` unsoundness** (RUSTSEC-2024-0429 / GHSA-wrw7-89jp-8q8g, medium). **Left open and tracked, not dismissed.** Transitive in `desktop/src-tauri/Cargo.lock` via `tauri 2.11.2 → webkit2gtk 2.0.2 → gtk-rs 0.18` (pins `glib 0.18.5`; vulnerable `< 0.20.0`). **No non-breaking fix exists** — `glib 0.20` needs the gtk-rs 0.20 generation, which Tauri's Linux webkitgtk binding doesn't use yet, so `cargo update -p glib` can't advance it (also why Dependabot hasn't opened a PR). Risk is low: Linux GTK-webview only (Windows=WebView2, macOS=WKWebView never exercise it), eVi's own Rust never calls `glib::VariantStrIter`, crash-class not RCE. **Unblock:** a Tauri release on gtk-rs 0.20 → `cargo update` + rebuild + retag `desktop-v*`. Recheck **by number**: `gh api repos/evi-assistant/evi-ai/dependabot/alerts/1 -q .state` (the list/GraphQL endpoints lag).
- **Delete the local mirror backup** (`evi-backup.git`) once confident the scrubbed public history is clean — kept as a safety net for now.

**Done 2026-07-01 (post-launch cleanup session):**

- ✅ **Secret scanning + push protection ENABLED**; `dependabot_security_updates` on. (Left off: `secret_scanning_non_provider_patterns`, `secret_scanning_validity_checks` — noisier.)
- ✅ **Local `.venv` recreated on Python 3.13.14** — full suite **1364 passed / 4 skipped** on Windows/3.13; the 3.11 line is retired.
- ✅ **Uncommitted paths resolved** — `tests/test_console_encoding.py` tracked; `.claude/` + `desktop/src-tauri/permissions/autogenerated/` gitignored.
- ✅ **CI hardening** — `release.yml` now fails unless tag == `pyproject` == `evi/__init__.py`; `.gitignore` generalized to `.venv*/`.
- ✅ **Docs refreshed** to 1.0 — `README.md`, `docs/releasing.md`, `TESTING.md`, `desktop-release.yml` comments.
- ✅ **GitHub Pages site** live at https://evi-assistant.github.io/ — moved into the dedicated `evi-assistant/evi-assistant.github.io` repo to serve at the domain root (the `site/` copy + `pages.yml` were removed from evi-ai and its project-pages was disabled).
- ✅ **Legacy releases repo** `dmang-dev/evi-ai-releases` archived (assets stay downloadable for old clients).

**Roadmap (evaluated 2026-07-01 — see the A2A/Hermes analysis; not yet built):**

- **A2A (Agent2Agent) adapter — recommended, effort ≈ M.** Keep the zero-dep LAN federation as the "private fast path"; ADD an optional A2A adapter behind an `[a2a]` extra as the "interop path" — expose an Agent Card at `/.well-known/agent-card.json` + `message/send`/`tasks/*` mapped onto the existing agent loop (tool-gating, streaming, bearer already exist), plus an A2A client for foreign agents. **Do NOT rip out federation** — it's a feature-subset of A2A but serves a different niche (private, same-owner, zero-ceremony). Build against the official `a2a-sdk` (several v1.0 JSON-RPC wire strings were flagged unverified). Defer push/OAuth2/mTLS/signed-cards (L).
- **Hermes borrow — autonomous skill synthesis (S/M).** Let a scoped agent write a new `SKILL.md` from a successful multi-step transcript (extends `dream.py`/`skills.py`), gated by review. Secondary: Python-RPC subagent pipelines (S/M); run Hermes-4 as a steerable local backend (S — preset only). eVi already matches Hermes on nearly everything else.

## 6. Gotchas (still true)

- **Use the venv Python.** System `python` lacks the web deps; run everything via `.venv\Scripts\python.exe`.
- **Keep `.venv-build` lean.** Don't add torch/av/sounddevice to it — the practical-tier sidecar balloons >1 GB. See `docs/desktop-bundling.md`.
- **Windows `localhost` IPv6 stall:** connecting to a closed `::1` port is *dropped* (SYN filtered), not refused, so it blocks the full timeout. `evi/portprobe.py` pins probes to `127.0.0.1`. Keep this in mind for any new local-port code.
- **Desktop runtime:** Tauri picks a random free port injected via `window.__EVI_PORT__`; the sidecar logs to `~/.evi/logs/desktop-server.log` (block-buffered — may look empty until the process exits).
- **PowerShell 5.1 traps:** `$ErrorActionPreference="Stop"` aborts on a native command's *stderr* (rustup/npm warnings); `if(){}` is a statement, not an expression; `-WindowStyle Hidden` ≠ `CREATE_NO_WINDOW`; `setx` truncates at 1024 chars (use `[Environment]::SetEnvironmentVariable(...,"User")`).
- **Tauri config:** avoid `"//"` comment keys — the strict schema rejects them.
- **Two egg-info dirs at repo root:** `evi_assistant.egg-info` (current) and a stale `evi_ai.egg-info` (pre-rename). Both git-ignored; the stale one is harmless residue, safe to delete (regenerated on build).

## 7. Migration to another machine

### 7a. Copy vs. recreate

Copy the repo, but **skip these reproducible/large dirs** (all in `.gitignore`) and recreate them:

- `.venv/`, `.venv-build/`, `venv/`, `env/` (`.venv313/` too — git-ignores itself via its internal `.gitignore`)
- `desktop/node_modules/`, `desktop/src-tauri/target/` (Rust cache, ~1.6 GB), `desktop/src-tauri/gen/`, `desktop/src-tauri/binaries/` (staged sidecar, ~250 MB)
- `build/`, `dist/`, `*.egg-info/`, `__pycache__/`

A clean source tree is only a few MB — zip the repo excluding the above, or copy the git working tree.

**Copy the user-data dir `%USERPROFILE%\.evi\`** — this is real state:
`config.toml`, `evi-updater.key` / `.key.pub` / `.pass` (**signing keys — losing them breaks future update verification**), `tokens/` (OAuth), `models/`, `profiles/`, `skills/`, `commands/`, `transcripts/`, `indices/`, `images/`, `screenshots/`, `uploads/`, `scheduled/`, `logs/`, `checkpoints/`, `memory/`, `recipes/`, `styles/`, `routes.json`.

### 7b. Set up on the new machine

```powershell
# 1. Install toolchains (Python 3.13 always; Rust + MSVC C++ Build Tools + Node LTS only for desktop)
# 2. Place the repo (ideally at the SAME path — see 7c), then:
cd C:\evi
py -3.13 -m venv .venv                          # 3.13 floor — do NOT use plain `python -m venv`
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[web,mcp,scheduler,downloads,web-tools,computer,stt,pdf,index,calendar,rerank,email,dev]"

# 3. Sanity check
.\.venv\Scripts\python -m pytest -q             # ~1367 unit tests
.\.venv\Scripts\python -m evi --version         # 1.0.0

# 4. (Desktop only) freeze sidecar + build
py -3.13 -m venv .venv-build
powershell -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1
cd desktop ; npm install ; npm run tauri build -- --config src-tauri\tauri.standalone.conf.json
```

Toolchains: Python 3.13 (everything); Rust + Cargo, MSVC C++ Build Tools, Node LTS + npm, WebView2 (desktop only, WebView2 preinstalled on Win10/11); tesseract / ffmpeg optional (OCR / audio, else those tools degrade). Restore `%USERPROFILE%\.evi\` from your copy afterward.

### 7c. Carry over Claude Code chat history + memory

Claude Code stores per-project session transcripts as JSONL under:

```
%USERPROFILE%\.claude\projects\<mangled-project-path>\
```

The folder name is the project's absolute path with the drive colon and **every** path separator replaced by `-`:

| Project path | Mangled folder |
|---|---|
| `C:\evi` | `C--evi` |
| `D:\code\evi` | `D--code-evi` |
| `/home/you/evi` | `-home-you-evi` |

The folder contains one `*.jsonl` per session (plus a per-session sidecar dir) and a `memory/` subdir (the cross-session auto-memory: `MEMORY.md` + topic notes).

**To carry it over:**

1. Copy the whole `%USERPROFILE%\.claude\projects\<mangled>\` folder to the new machine's `%USERPROFILE%\.claude\projects\`.
2. **Match the path, or rename the folder** to the new machine's mangled path (per the table). If the folder name doesn't match the project's path, Claude Code won't link the history.
3. In the new project dir, start Claude Code and `--resume` (or `--continue` for the latest).
4. Optionally copy global `%USERPROFILE%\.claude\` settings for identical config — that's machine/global, not project state.

> Session JSONLs can be large (tens of MB); resuming replays them into context, so expect a slower first turn.

## 8. Layout cheatsheet

```
C:\evi
├─ evi/                       shared Python core (~135 top-level modules)
│  ├─ __init__.py             __version__ = "1.0.0"
│  ├─ capabilities.py         7 model-capability chips
│  ├─ anatomy.py bugledger.py reflect.py   project-intelligence pack
│  ├─ pyanalyze.py complete.py configlint.py codeintel.py doctor.py
│  ├─ ultracode.py workflows.py teams.py recipes.py   orchestration
│  ├─ federation.py plugins.py marketplace.py skills.py skillscope.py
│  ├─ portprobe.py workdir.py sandbox.py recommend.py modelsdev.py
│  ├─ backends/               lmstudio, ollama, llamacpp (8080–8090 discovery), openai_compat, factory
│  ├─ llm/                    agent.py, client.py, subagent.py, responses.py, specialty.py
│  ├─ tools/                  base.py (REGISTRY) + 22 builtin modules; resolver.py, monitor.py, ask.py …
│  ├─ mcp/                    bridge.py, manager.py, servers.py, publish.py
│  ├─ apps/{cli,web}/         Typer CLI + FastAPI/SSE web (~74 /api/* routes)
│  ├─ sdk/                    embeddable SDK (builder.py)
│  └─ data/                   models-catalog.json (baked models.dev snapshot)
├─ desktop/                   Tauri 2 shell (NOT a Python package)
│  ├─ dist-shim/index.html    loading spinner
│  ├─ package.json            version 1.0.0
│  └─ src-tauri/              Rust src/, Cargo.toml, tauri.conf.json + tauri.standalone.conf.json,
│                             capabilities/, permissions/, icons/, binaries/ (staged sidecar)
├─ editors/vscode/            VS Code extension (TypeScript; FIM + chat webview)
├─ scripts/                   build-sidecar.*, build-desktop.ps1, sidecar_entry.py, test.*, install.*
├─ docs/                      EVI.md is authoritative; features.md, releasing.md, desktop-bundling.md …
├─ tests/                     pytest suite (~1451 total; e2e opt-in) + tests/e2e (Playwright)
├─ .github/workflows/         ci, release, desktop-release, security, e2e, docker, evi-run-example
├─ CHANGELOG.md
└─ pyproject.toml             dist name evi-assistant, version 1.0.0, requires-python >=3.13, MIT

%USERPROFILE%\.evi\                        user data (config, signing keys, models, transcripts, memory, …)
%USERPROFILE%\.claude\projects\<mangled>\  Claude Code chat history + cross-session memory
```
