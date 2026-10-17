# eVi — Project Handoff & Migration Notes

_Last updated: 2026-10-14 · **PUBLIC** · version, counts, workflows, extras, module map, routes and CLI: the generated facts in [§9](#9-generated-facts), from `python scripts/handoff_facts.py write`_

This is the working-state handoff for eVi. The 1.0 public launch is done: the repo is public under the `evi-assistant` org, the PyPI package is `evi-assistant`, the desktop app ships for Windows/macOS/Linux, and the `evi-skills` catalog is public. Since 1.0.5 the desktop channel **auto-follows** the core (every PyPI `v*` release also cuts the matching `desktop-v*` build), so the two no longer drift. Read **Current status**, **Open items**, and **Gotchas** first, then follow **Migration** if you're moving to another machine.

**Facts are commands.** Numbers in this file come from §9, which a script writes; nothing else here should state a version or a count. Released versions: `git tag --sort=-creatordate | head` and `CHANGELOG.md`.

---

## 1. What eVi is

A local-first personal AI assistant: **one shared Python core (`evi/`) behind three frontends**, talking to local LLM backends.

| Frontend | What it is |
|---|---|
| **CLI** | `evi` — Typer/Rich REPL (`evi/apps/cli/main.py`) |
| **Web** | FastAPI + SSE app (`evi/apps/web/server.py`) + vanilla-JS single-page UI in `static/` |
| **Desktop** | Tauri 2 shell (`desktop/`) wrapping the web UI, shipping a frozen Python sidecar |

- **Local LLM backends:** LM Studio (`:1234`, the default), Ollama (`:11434`), llama.cpp (`:8080`, auto-discovers ports 8080–8090), or any OpenAI-compatible endpoint; plus **eVi's own managed llama.cpp runtime** (downloads a pinned `llama-server` and a catalog model, `evi runtime setup`) and **six CLI-agent backends** that drive a CLI the user already pays for or is logged in to.
- **Distribution:** PyPI dist name is **`evi-assistant`**; the import package and CLI command both stay **`evi`**. Install with `pip install evi-assistant`, then `import evi` / run `evi`.
- **License:** MIT. **Python floor:** 3.12 (`requires-python = ">=3.12"`; lowered from 3.13 in 1.0.36 — installs on Ubuntu 24.04 LTS. The `[ast]` extra still needs 3.13, marker-gated).

## 2. Current status

**1.0 shipped 2026-07-01.** The current version is in §9 (read from `pyproject.toml`); `CHANGELOG.md` says what each release contained.

- **Public repo:** `https://github.com/evi-assistant/evi-ai` (transferred from the old private `dmang-dev/evi-ai`). All `[project.urls]` point at the `evi-assistant` org.
- **PyPI:** `evi-assistant` (Development Status **Production/Stable**). The 1.0.1→1.0.5 line was **PyPI + Docker only** — see the CLI-agent backends note below for why. From 1.0.6 on, desktop follows every release.
- **Desktop:** Windows/macOS/Linux build matrix. In-app updates are **minisign-signed** and verified before install, and the app **asks before it upgrades** (1.0.47). The installers are **not OS-code-signed yet** (SmartScreen/Gatekeeper warn; see Open items). The in-app updater serves directly from the public repo (the private release-mirror channel is retired). `release.yml` **auto-invokes** `desktop-release.yml` on every `v*` tag (reusable `workflow_call`), so the frozen sidecar is re-built from the released `evi/` and desktop no longer lags PyPI.
- **Sidecar update channel:** installed desktop apps also pull a newer **core** in the background from the fixed-tag `sidecar-latest` release (minisign-signed manifest + sha256, applied on next launch, with a **Core update ready — Restart now** prompt since 1.0.47). A core-only fix reaches existing installs without a reinstall — but **any `desktop/src-tauri` Rust change still needs a full `v*` release**, because the shell itself must be rebuilt.
- **Skills:** `evi-skills` catalog is public.
- **Site:** landing page live at **https://evi-ai.dev** (custom domain; also `evi-assistant.github.io` → 301 to it). Lives in the dedicated **`evi-assistant/evi-assistant.github.io`** org-pages repo; custom domain set via a `CNAME` file (`evi-ai.dev`) + Cloudflare DNS (grey-cloud A/AAAA → GitHub Pages IPs, `www` CNAME). HTTPS enforced, Let's Encrypt cert. `docs-publish.yml` regenerates the site docs and the GitHub wiki from `docs/` (publishing needs the `DOCS_PUBLISH_TOKEN` secret; without it the job builds and verifies only).
- **No breaking API changes from 0.40.0** — 1.0.0 marks stability + public repo + a coordinated launch across the package, desktop app, and skills catalog.
- **PyPI version** must match across `pyproject.toml` and `evi/__init__.py` — the `release.yml` gate asserts tag == both. **Desktop version** is **derived from the release tag** at build time (`scripts/set-desktop-version.py`, run by `desktop-release.yml`), so the four Tauri version files are never hand-bumped and can't drift from the core.

**Tests:** counts per layer are in §9; `python -m pytest --collect-only -q` gives the collected total (parametrize expands it). The default run deselects e2e and desktop tests (`addopts = -m 'not e2e and not desktop'`). Ruff clean. **Test layers:** unit; UI e2e (`tests/e2e`, `-m e2e`, Playwright + a fake in-thread backend; the release gate since Phase 0 batch 1); CLI e2e (`tests/cli_e2e`); and native desktop (`tests/desktop_e2e`, `-m desktop`, Windows-only — drives the real `evi-desktop.exe` via tauri-driver + msedgedriver; needs `.webdrivers/msedgedriver.exe` matching the WebView2 runtime). Rust: `cargo test --locked` in `desktop/src-tauri`. Details: `TESTING.md`.

> ⚠ If `tests/test_worktree.py` fails locally with `NotADirectoryError` (WinError 267), a POSIX-style git (msys2/devkitPro/Cygwin) is shadowing Git for Windows on PATH. 1.0.15 made `repo_root()` resilient to this, so it should no longer bite — but the same PATH shadowing also breaks bare `python`, so invoke the venv's `python.exe` explicitly.

**Dev environment:** any Python 3.12+ venv works. `scripts/dev-bootstrap.sh` / `scripts\dev-bootstrap.ps1` creates one outside the checkout (and `--worktree <branch>` makes a director-owned worktree under `.worktrees/`), and `evi doctor --dev` checks the venv, git `safe.directory`, test and lint tools, `pytest --collect-only` and line endings, printing fix commands. `EVI.md` is the implementer contract eVi follows when it works on itself ([docs/self-build.md](docs/self-build.md)).

**1.0.0 bundles the whole 0.34–0.40 line:** specialty SLMs + the 7 capability chips, the guard-model guardrail layer, the models.dev catalog, the config linter (`evi lint`), completion notifications, pluggable web search, `evi skill add`, the project-intelligence pack (anatomy map, bug ledger, session reflection), the VS Code extension, local FIM completion, federation, ultracode, and the full CLI/web/desktop parity set.

### CLI-agent backends (1.0.1 → 1.0.5)

**Six** backends let eVi route a turn through **another AI coding CLI using its own subscription or login**. They are NOT OpenAI-compatible on the wire; each wraps a local CLI behind a shared shim (`evi/llm/cli_agent.py`) that adapts the CLI's streamed output into the `chat.completions` surface eVi expects. Pick one in **Settings → Model & Backend** (no URL/key) or `evi backend add <name> --kind <kind>`. `claude_agent` drives eVi's own tools (full parity); the rest are **chat / delegate** providers (the CLI runs its own tools). Since Phase 0 batch 2 the drivers pass the conversation over stdin, time-bound each turn and leave no process behind, and `gemini` needs a Gemini API key or Vertex AI (details: `docs/configuration.md` § The CLI-agent backends at a glance).

| kind | CLI | Auth | Since |
|---|---|---|---|
| `claude_agent` | `claude` | Claude **Max/Pro** login | 1.0.3 |
| `codex` | `codex` | ChatGPT **Plus/Pro** login | 1.0.4 |
| `gemini` | `gemini` | Gemini **API key** or **Vertex AI** | 1.0.4 |
| `amp` | `amp` | Amp **subscription** (`amp login` / `AMP_API_KEY`) | 1.0.5 |
| `qwen` | `qwen` | Qwen **free** OAuth | 1.0.5 |
| `copilot` | `copilot` | GitHub **Copilot** login | 1.0.5 |

- **1.0.5 (2026-07-11):** `amp`, `qwen`, `copilot`. `amp` + `qwen` both speak Claude Code's `stream-json`, so the event parser is shared in `cli_agent.py` (`emit_claude_events` + `cc_usage`/`cc_error_message`). **Mistral Vibe was evaluated and rejected** — it needs `MISTRAL_API_KEY` (no subscription/free-login path), which breaks the "no API key" thesis and duplicates the existing `openai_compat`→Mistral route.
- **1.0.4:** `codex` + `gemini`, plus the shared-shim refactor that pulled the reusable core out of `claude_agent`.
- **1.0.3:** `claude_agent` (first CLI-agent backend). **1.0.1/1.0.2:** agent-identity fix + multi-backend registry / fan-out (see CHANGELOG).

**Desktop delivery:** the frozen sidecar carries these backends to desktop users, but they still need the corresponding external CLI on the user's PATH — the sidecar spawns them via `shutil.which`.

### Since 1.0.36 (1.0.37 → 1.0.53)

| Release | What | Docs |
|---|---|---|
| 1.0.37–1.0.38 | Federation Peers panel overhaul (the two tokens, live posture banner) | `docs/configuration.md` § Federation |
| 1.0.39–1.0.42 | Settings: "How this works" explainers, live health banners (`/api/integrations/status`, `/api/voice/status`, `/api/specialty/status`), the unsaved-changes cue | `docs/features/web-desktop.md` |
| 1.0.43–1.0.46, 1.0.48 | **Managed llama.cpp runtime**: zero-config setup, model catalog, GPU (CUDA/Metal), Job Object reaping, your own `llama-server`, relaunch on desktop start | `docs/features/runtime.md` |
| 1.0.47 | Desktop: confirm before upgrading; **Core update ready — Restart now** | `docs/features/web-desktop.md` |
| 1.0.49–1.0.50 | **First run**: ranked options (installed CLIs, running servers, API keys, then a download), verified switch with rollback, setup disclosure, named steps, starter prompts, grouped settings; Host-header check on the unauthenticated API | `docs/features/onboarding.md` |
| 1.0.51 | **Telegram chat channel** (pairing, own toolset) and **`evi sessions import`** (Claude Code, Codex) | `docs/features/channels.md`, `docs/features/sessions.md` |
| 1.0.52 | **Steering**: a message sent mid-turn applies at the next step | `docs/features/steering.md` |
| 1.0.53 | Security hotfix (upload paths, cross-site requests, untrusted `.evi.toml`, TTS, path-like names, opening links) | `CHANGELOG.md` |

### Phase 0 (in progress)

The plan from 1.0.53 on is **`docs/roadmap.md`** (six phases, each item with an owner, size and dependencies; Phase 0 is "Foundations and fixes"). Work happens on the `phase-0` branch in batches, each verified by the director before commit. Batches 1 and 2 are done (unreleased, in `CHANGELOG.md` `[Unreleased]`): release gating on e2e + CHANGELOG + actionlint + workflow-injection + docs freshness, the EVI.md implementer contract and dev bootstrap, per-command shell rules, hardened git calls, `@file` inlining only from command templates, the `web_fetch` network guard, pinned runtime downloads, folder trust for project config, the permission engine and protected paths, CLI-agent driver fixes, MCP SDK 2.1–2.3, cargo major bumps, and the managed runtime sizing its context window. The roadmap's status column is the source of truth for what is done: when an item lands, flip its Status cell and the shipped/done counts above the tables (`tests/test_handoff_facts.py` fails while they disagree).

## 3. Feature inventory

The module map (every module, per package) is generated in §9. The narrative:

**Agent + LLM layer (`evi/llm/`)**
- `agent.py` — the agent loop; `Agent.chat` streams completions and dispatches tool calls (yields TextDelta / ToolCall / ToolResult / Steering / Done / Error). `Agent.steer` queues a mid-turn follow-up for the next step.
- `client.py` — dispatches to the configured backend (OpenAI-compatible chat client).
- `subagent.py` — scoped Agent runner backing `delegate_*` tools via `SUBAGENT_PROFILES`.
- `responses.py` — opt-in OpenAI Responses API path (`[llm] api = "responses"`); local backends stay on Chat Completions.
- `specialty.py` — specialty-model registry: route one task (OCR/vision/etc.) to a small dedicated model without swapping the main model, configured under `[models]`.
- `cli_agent.py` + `*_agent.py` — the CLI-agent drivers.

**Local-LLM backends (`evi/backends/`)** — `base.py` interface + `lmstudio.py`, `ollama.py`, `llamacpp.py` (auto-discovers a live llama.cpp on 8080–8090), `openai_compat.py`, the CLI-agent backends, `presets.py`, `registry.py` (`~/.evi/backends.json`), `factory.py` (picks backend from `LLMSettings.backend`).

**Managed runtime (`evi/runtime/`)** — stdlib-only so it runs in the frozen sidecar: `llamacpp_runtime.py` (pinned llama.cpp release, SHA-256 pins, CPU/CUDA build choice, detection of an existing `llama-server`), `catalog.py` (curated GGUF catalog in `evi/data/runtime_models.json`, pinned commits and hashes, hardware-fit recommendation, context-window sizing), `llama_server.py` (one supervised server per process), `setup.py` (setup / model switch / GPU switch / autostart / `context_size` hand-back), `reap.py` (Windows Job Object). Re-pin with `scripts/pin_runtime_hashes.py`.

**Channels (`evi/channels/`)** — `telegram.py` (long-polling loop, pairing gate, one headless turn per message with `[channels] telegram_tools`), `pairing.py` (`~/.evi/channels/telegram.json`), `telegram_api.py` (stdlib Bot API client).

**Model capability chips (`evi/capabilities.py`)** — **seven** best-effort chips (id-substring heuristics, with models.dev ground-truth override where known):

| Chip | | Chip | |
|---|---|---|---|
| 👁 Vision | `vision` | 🔧 Tools | `tools` |
| 🧠 Thinking | `reasoning` | 🛡 Guard | `guard` |
| ⌨ Infill | `infill` | ◆ Embeddings | `embed` |
| 🎤 Audio | `audio` | | |

**Models registry / catalog** — `recommend.py` (hand-curated registry + hardware-aware recommendation), `modelsdev.py` (models.dev catalog: context limit, modalities, tool/reasoning flags, pricing; baked snapshot `evi/data/models-catalog.json`, `evi models refresh` pulls the full catalog), `hardware.py` (GPU+RAM detection), `downloads.py` (Ollama / `hf:` pulls), `firstrun.py` (Ollama install plan).

**Project-intelligence pack** — `anatomy.py` (token-estimated file index → `.evi/anatomy.md`, auto-injected), `bugledger.py` (append-only symptom→root-cause→fix ledger `.evi/bug-ledger.jsonl`, backs `record_fix`/`search_fixes`), `reflect.py` (session reflection into memory; `evi reflect` or a `session_end` hook).

**Code/dev tooling** — `pyanalyze.py` (AST symbol outline → `python_symbols` tool; Reflex Rust fast-walk with `[ast]` extra, else stdlib), `complete.py` (local FIM completion; `evi complete` + `/api/complete`), `configlint.py` (`evi lint` — static skill/hook/command/agent/permission validation, also CI gate for evi-skills), `codeintel.py` (formatters/linters by extension; backs `format_on_edit` + `check_file`), `doctor.py` (`evi doctor`, `evi doctor --dev`).

**Tools (`evi/tools/`)** — `base.py` holds `REGISTRY` (`@tool`); `register_builtin_tools()` imports the modules in `_BUILTIN_TOOL_MODULES` (listed in §9). Notable: `resolver.py` (`search_tools` meta-tool — defers the long tail of tool schemas), `monitor.py` (bounded watch until regex/timeout), `ask.py` (`ask_user`), `shell.py`, `code.py` (`run_python` subprocess; sandboxed via `sandbox.py` when `[tools] sandbox` on), `availability.py` (which categories lack a dependency).

**Safety stack** — `permissions.py` (modes + first-match rules; category rules), `shellsplit.py` (splits a shell line into commands so rules match per command), `shell_guard.py` (destructive-command guard), `trust.py` (per-repo folder trust, `~/.evi/trusted_folders.json`; `evi trust`), `project.py` (tighten-only `.evi.toml` overlay), `gitsafe.py` (eVi's own git calls without the repo's hooks, filters or programs), `netguard.py` (`web_fetch` address and redirect checks), `hooks.py`, `guardrails.py`, `guardmodel.py`, `moderation.py` (regex→judge→classifier), `sandbox.py`, `safemode.py`.

**Multi-agent orchestration** — `ultracode.py` (fixed decompose→solve→critic→synthesize pipeline over one task), `workflows.py` (declarative TOML DAG, `parallel=true`, `~/.evi/workflows/*.toml`), `teams.py` (dynamic claimable shared task list with `blocked_by`), `recipes.py` (ordered prompts in one shared conversation).

**MCP (`evi/mcp/`)** — client side (`bridge.py`, `manager.py`, `servers.py`) consumes other servers' tools; `publish.py` (`evi mcp serve`) runs eVi **as** an MCP server (curated tools + `evi://memory/<name>` resources + command prompts over stdio and streamable HTTP; tokenless HTTP serve is loopback-only). Works with MCP SDK 1.30 through 2.3.

**Federation** — `federation.py` + `tools/federation.py`: delegate a subtask to a trusted peer eVi via `POST /api/federate`; peers in `~/.evi/peers.json`; off by default (`[federation] serve = true`). `a2a.py`: the A2A agent card + `POST /a2a` (`[federation] a2a = true`) and a `delegate_a2a` tool.

**Plugins & skills** — `plugins.py` (installable `plugin.toml` bundles under `~/.evi/plugins/`), `marketplace.py` (`evi plugin search`/`install`), `skills.py` + `skillscope.py` (active-skill tool scoping via SKILL.md frontmatter).

**Other core subsystems** — memory (`memory.py`, `dream.py`), semantic search (`embeddings.py`, `index.py`, `search.py`), session data/analytics (`transcripts.py`, `sessions.py`, `sessionimport.py`, `stats.py`, `finetune.py`, `context_report.py`), scheduling (`scheduler.py`, `scheduled.py`, `routines.py`), routing (`routing.py`), evals (`evals.py`), observability (`otel.py`, `reporting.py`), portable state (`sync.py`, `backup.py`, `update.py`, `worktree.py`, `profiles.py`, `users.py`), media (`voice.py`, `audio_input.py`, `diarize.py`, `vision.py`, `doclayout.py`), and the embeddable SDK (`sdk/builder.py`).

**VS Code extension** — `editors/vscode/` (TypeScript): ghost-text FIM via `/api/complete` + a chat webview via `/api/chat`. Deliberately kept out of the Python package.

## 4. Build / test / release

### Run the tests

```powershell
# Full unit suite (any 3.12+ venv; the repo's own is .venv):
.\.venv\Scripts\python.exe -m pytest -q --timeout=30
# Helper scripts: scripts\test.ps1 (Windows) / scripts/test.sh (Unix)  — bundle --timeout=15
# How many tests there are today:
.\.venv\Scripts\python.exe -m pytest --collect-only -q
# Lint (what CI runs):
.\.venv\Scripts\python.exe -m ruff check evi tests scripts .github/actions
# E2E (Ubuntu in CI, and the release gate): needs .[e2e] + playwright chromium
.\.venv\Scripts\python.exe -m pytest tests/e2e -m e2e --timeout=180
# Regenerate the facts in §9 (and check they're current), when cutting a release:
.\.venv\Scripts\python.exe scripts\handoff_facts.py write
.\.venv\Scripts\python.exe scripts\handoff_facts.py check
# Every CLI command and /api route added since 1.0.36 is mentioned under docs/ (needs the tags):
.\.venv\Scripts\python.exe scripts\handoff_facts.py docs-coverage
```

### Two-venv model

| Venv | Purpose |
|---|---|
| **`.venv`** | Fat local **dev** env — all extras (incl. stt/computer/rerank → torch/av/sounddevice). |
| **`.venv-build`** | Isolated env used **only** to freeze the desktop sidecar (`.[web,pdf,index,build-desktop]`). |

The sidecar freeze uses PyInstaller `--collect-submodules evi`, which pulls **every** `evi.tools.*` module. Building from the fat `.venv` would drag torch/av/sounddevice into the "practical tier" sidecar, ballooning it from ~75 MB to >1 GB — hence the separate lean build env. The build scripts auto-prefer `.venv-build` when present.

### Cut a PyPI release (`release.yml`)

Trigger: push a `v*.*.*` tag (also `workflow_dispatch` for manual re-publish). Steps: the **browser e2e suite** (`e2e.yml`, called first; nothing publishes if it fails) → sanity-check the tag equals `pyproject` and `evi/__init__.py` → the **CHANGELOG check** (`python -m tests.test_changelog_lint --tag … --date …`: the newest entry is this version, dated within a day of the tagged commit, and the file lints clean) → `python -m build` → ruff + pytest → publish to PyPI via OIDC **Trusted Publishing** (no API token) → sigstore keyless signing → GitHub release → `desktop-release.yml` and `docker.yml`. Full procedure: `docs/releasing.md`.

**Bump two files** (`pyproject.toml [project] version` **and** `evi/__init__.py __version__`), move `CHANGELOG.md`'s `[Unreleased]` to the new version, regenerate §9 (`python scripts/handoff_facts.py write`), then:

```powershell
git add pyproject.toml evi/__init__.py CHANGELOG.md HANDOFF.md
git commit -m "release: X.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

> The workflow's tripwire asserts tag == `pyproject.toml` == `evi/__init__.py`, so a forgotten bump in either file fails the release before it publishes. Lint and the unit suite run ahead of the publish step, so a lint failure can't reach PyPI either.

### Cut a desktop release (`desktop-release.yml`)

**Normally you don't cut this by hand** — `release.yml` calls it via `workflow_call` on every `v*` tag (the `desktop` job → `desktop-release.yml` with `release_tag: desktop-v<version>`, `secrets: inherit`), so a PyPI release automatically produces the matching desktop release. It runs *after* `build-and-publish`, so the desktop release publishes last and `releases/latest` (the updater endpoint) resolves to it.

Three triggers: **`workflow_call`** (from `release.yml`, the normal path), a **`desktop-v*` tag push** (a shell-only rebuild without a core release), or **`workflow_dispatch`** (blank `release_tag` = artifacts-only, no release). Matrix: windows/macos/ubuntu (`fail-fast: false`). Per OS: setup Python 3.13 + Rust + Node, **sync the desktop version to the tag** (`scripts/set-desktop-version.py`, after the Rust cache so it doesn't churn crate caches), freeze the sidecar in a fresh `.venv-build` + `evi-server --check`, `npm install`, then `tauri-action` with `--config src-tauri/tauri.standalone.conf.json`. Publishes a **non-draft** release; signs updater artifacts (`.sig` + `latest.json`) with `TAURI_SIGNING_PRIVATE_KEY[_PASSWORD]`. A separate `publish-sidecar` job zips each OS's sidecar, signs the manifest and publishes the `sidecar-latest` release.

**You no longer bump the four Tauri version files by hand** — the version-sync step derives them from the tag at build time. (The committed values are just dev defaults for local `tauri dev`.) To force a manual desktop-only rebuild:

```powershell
git tag desktop-vX.Y.Z
git push origin desktop-vX.Y.Z
```

Updater endpoint: `https://github.com/evi-assistant/evi-ai/releases/latest/download/latest.json`. The version in `latest.json` must increase for clients to update. Updater signing is **minisign**, not OS code-signing — SmartScreen/Gatekeeper still warn (see Open items).

> ⚠ **`releases/latest` fragility:** the updater endpoint resolves to whichever release GitHub marks "latest." The auto-follow ordering (desktop publishes after the PyPI `v*` release) keeps that pointing at the desktop release, but it's timing-dependent — a PyPI-only path or a failed desktop build would leave `latest` without a `latest.json`. The **sidecar update channel** sidesteps this for core updates (a dedicated, static manifest URL); shell updates still depend on it.

### Freeze the sidecar / build the whole desktop app (Windows)

```powershell
py -3.13 -m venv .venv-build
scripts\build-sidecar.ps1
desktop\src-tauri\binaries\evi-server\evi-server.exe --check
powershell -File scripts\build-desktop.ps1   # end-to-end
```

PyInstaller does **not** cross-compile — each OS's installer must be built on that OS (the CI matrix handles this per-runner). The desktop shell needs Rust 1.90+.

### CI workflows

The list of workflows, their triggers and jobs is generated in §9. What each is for:

| Workflow | Notes |
|---|---|
| `ci.yml` | Lint + unit gate. Push→1 cheap ubuntu/3.13 job; full 3-OS × py3.12/3.13 matrix only on PR/weekly/dispatch (macOS bills ~10×). Also: `mcp-sdk` (tests/test_mcp* on mcp 1.30.*, 2.0.* and the newest release the `[mcp]` pin allows; not on push), `rust` (cargo check + test of the desktop shell, every run), `rust-windows` (same on Windows; not on push), `actionlint`, and `workflow-injection` (zizmor + a strict no-`${{ }}`-in-scripts check). |
| `release.yml` | Calls `e2e.yml` first; the CHANGELOG/tag check, then PyPI Trusted Publishing + sigstore + GitHub release. On a tag push it then calls `desktop-release.yml` and `docker.yml`. |
| `desktop-release.yml` | 3-OS Tauri matrix, non-draft, minisign-signed updater; the Windows leg runs the shell's tests first (and `tests/desktop_e2e` when `desktop_e2e` is set); `publish-sidecar` for the core-only channel. |
| `security.yml` | pip-audit, cargo-audit, gitleaks, CodeQL (python+js). |
| `e2e.yml` | Playwright UI tests, Ubuntu only, fake in-thread backend; quarantined tests run non-blocking. |
| `docker.yml` | GHCR image (web+scheduler), published after PyPI; push opt-in on dispatch. |
| `docs-freshness.yml` | Fails a change that adds a CLI command or web route without touching `docs/` (`docs-skip` label or `Docs-skip:` trailer waives). |
| `docs-publish.yml` | Builds and verifies the site docs, cookbook and wiki; publishes on push to main. |
| `evi-run-example.yml` | Example workflow for the `evi-run` action (tools off unless `auto_approve` is ticked). |

Build backend: setuptools ≥68 + wheel. The optional-dependency extras are listed in §9.

## 5. Open items / next steps

The plan is **`docs/roadmap.md`**; this section keeps only operational items that aren't roadmap work.

- **OS code-signing** for the desktop installers — updater minisign signing is done, but SmartScreen/Gatekeeper still warn. Researched 2026-07-11; also an open question in the roadmap:
  - **Windows — FREE path: [SignPath Foundation](https://signpath.org/)** (free code signing for OSS; eVi qualifies). OV cert from Sectigo, HSM-held key, GitHub Actions connector → wire into `desktop-release.yml`. Caveat: OV (not paid EV) doesn't get *instant* SmartScreen reputation — warnings clear as download history builds. The rollout status lives in `docs/code-signing.md` (application submitted July 2026). ([OSSign](https://ossign.org/) = newer free-for-OSS alt; Azure Artifact Signing ≈ $10/mo.)
  - **macOS — NO free option.** Notarization hard-requires the paid **Apple Developer ID ($99/yr)**; Apple offers no free notarization. Without it Gatekeeper warns "unidentified developer." Free reality = ship unsigned + document the user workaround (right-click → **Open**, or `xattr -dr com.apple.quarantine /Applications/eVi.app`). Pay $99/yr only if clean Mac installs are wanted.
- **Sidecar update channel — implemented 2026-07-11, CI publish verified on v1.0.10.** `desktop/src-tauri/src/sidecar_update.rs` (fetch signed manifest → minisign-verify [reuses the app-updater key] → sha256 → extract to `%APPDATA%/eVi/sidecar/<ver>/` → `--check` → flip `active` pointer; `SHELL_ABI` gate; apply-on-next-launch; `EVI_SIDECAR_UPDATE=0` or `[desktop] sidecar_auto_update = false` opts out) + the `main.rs` hook (`sidecar_path()` prefers a staged sidecar, never an older one than the bundled core); `scripts/build-sidecar-manifest.py` + `scripts/zip-sidecar.py`; the `publish-sidecar` job. The **`tauri signer sign` → base64-sig → `minisign-verify` interop is proven**: the base64-decoded `.minisig` (algo `ED`/BLAKE2b-prehashed, key id `fc51ff1047a84542`) and its trusted-comment global sig both verify (Ed25519) against the `PUBKEY` hardcoded in `sidecar_update.rs` (`RWT8Uf8QR6hFQ…`). *(A pip-managed runtime core — bundle Python, `pip install -U evi-assistant` — was considered and rejected: fragile offline, native-wheel/platform friction, worse startup.)*
- **Tracked — Dependabot alert #1: `glib` unsoundness** (RUSTSEC-2024-0429 / GHSA-wrw7-89jp-8q8g, medium). **Left open and tracked, not dismissed.** Transitive in `desktop/src-tauri/Cargo.lock` via `tauri 2.12.1 (wry 0.57) → webkit2gtk 2.0.2 → gtk-rs 0.18` (still pins `glib 0.18.5`, rechecked 2026-10; vulnerable `< 0.20.0`; status also noted in `desktop/src-tauri/Cargo.toml`). **No non-breaking fix exists** — `glib 0.20` needs the gtk-rs 0.20 generation, which Tauri's Linux webkitgtk binding doesn't use yet, so `cargo update -p glib` can't advance it (also why Dependabot hasn't opened a PR). Risk is low: Linux GTK-webview only (Windows=WebView2, macOS=WKWebView never exercise it), eVi's own Rust never calls `glib::VariantStrIter`, crash-class not RCE. **Unblock:** a Tauri release on gtk-rs 0.20 → `cargo update` + rebuild + retag `desktop-v*`. Recheck **by number**: `gh api repos/evi-assistant/evi-ai/dependabot/alerts/1 -q .state` (the list/GraphQL endpoints lag).
- **Delete the local mirror backup** (`evi-backup.git`) once confident the scrubbed public history is clean — kept as a safety net for now.
- **Native-desktop e2e in CI.** `tests/desktop_e2e/` (opt-in `desktop` marker, Windows-only) drives the real `evi-desktop.exe` via `tauri-driver` + `msedgedriver`/WebView2. `desktop-release.yml` runs it on the Windows leg when its `desktop_e2e` input is set; locally it needs `.webdrivers/msedgedriver.exe` (gitignored) matching the installed **WebView2 runtime** (not the Edge browser version) + `selenium`.
- **Docs publishing token.** `docs-publish.yml` only publishes the site and wiki once the `DOCS_PUBLISH_TOKEN` secret exists; until then it builds and verifies.

**Done 2026-07-01 (post-launch cleanup session):** secret scanning + push protection enabled (`dependabot_security_updates` on; `secret_scanning_non_provider_patterns` and `secret_scanning_validity_checks` left off as noisier); uncommitted paths resolved (`tests/test_console_encoding.py` tracked; `.claude/` + `desktop/src-tauri/permissions/autogenerated/` gitignored); `release.yml` fails unless tag == `pyproject` == `evi/__init__.py`; `.gitignore` generalized to `.venv*/`; GitHub Pages site live at https://evi-ai.dev; legacy releases repo `dmang-dev/evi-ai-releases` archived (assets stay downloadable for old clients).

**Earlier roadmap notes** (A2A follow-ups, the Hermes skill-synthesis idea) moved into `docs/roadmap.md`: A2A streaming, push notifications, OAuth2/mTLS and file parts are in its "Not doing this cycle" list; reviewed skill drafting is Phase 5 (`skill-drafting-curator`).

## 6. Gotchas (still true)

- **Use the venv Python.** System `python` lacks the web deps; run everything via the venv's `python.exe`.
- **Network-share checkouts.** Git refuses a repo owned by another user (an SMB share, a mounted drive) unless it is in `safe.directory`; `evi doctor --dev` checks for this and prints the command.
- **Keep `.venv-build` lean.** Don't add torch/av/sounddevice to it — the practical-tier sidecar balloons >1 GB. See `docs/desktop-bundling.md`.
- **`--collect-all <pkg>` can drag in huge vendored binaries.** `claude_agent_sdk` (the `[claude-agent]` extra) vendors a ~250 MB `_bundled/claude(.exe)` CLI; `--collect-all claude_agent_sdk` bundled it into the sidecar and broke the Linux AppImage build (`failed to run linuxdeploy`) in 1.0.7/1.0.8. Fixed in 1.0.9 with `--collect-submodules claude_agent_sdk` + `--collect-submodules mcp` (Python only; the SDK falls back to the system `claude` on PATH). Prefer `--collect-submodules` over `--collect-all` unless you truly need a package's data files.
- **Test desktop-build changes with an artifacts-only run first.** `gh workflow run desktop-release.yml --ref main -f release_tag=""` builds all 3 OSes and uploads artifacts **without** publishing a release — validate a Linux/AppImage or freeze change there before cutting a `v*` tag (avoids shipping a half-built desktop release).
- **Windows `localhost` IPv6 stall:** connecting to a closed `::1` port is *dropped* (SYN filtered), not refused, so it blocks the full timeout. `evi/portprobe.py` pins probes to `127.0.0.1`. Keep this in mind for any new local-port code.
- **Desktop runtime:** the shell prefers port 8473 (a stable origin keeps the webview's `localStorage`) and falls back to a random free port, injected via `window.__EVI_PORT__`; the sidecar logs to `~/.evi/logs/desktop-server.log` (block-buffered — may look empty until the process exits). The managed llama.cpp server logs to `~/.evi/runtime/logs/llama-server.log`.
- **The managed runtime lives in an eVi process.** `evi web` and the desktop app relaunch it; `evi runtime …` / `evi model use` run it only while the command runs. See `docs/features/runtime.md`.
- **PowerShell 5.1 traps:** `$ErrorActionPreference="Stop"` aborts on a native command's *stderr* (rustup/npm warnings); `if(){}` is a statement, not an expression; `-WindowStyle Hidden` ≠ `CREATE_NO_WINDOW`; `setx` truncates at 1024 chars (use `[Environment]::SetEnvironmentVariable(...,"User")`).
- **Tauri config:** avoid `"//"` comment keys — the strict schema rejects them.
- **Two egg-info dirs at repo root:** `evi_assistant.egg-info` (current) and a stale `evi_ai.egg-info` (pre-rename). Both git-ignored; the stale one is harmless residue, safe to delete (regenerated on build).

## 7. Migration to another machine

### 7a. Copy vs. recreate

Copy the repo, but **skip these reproducible/large dirs** (all in `.gitignore`) and recreate them:

- `.venv/`, `.venv-build/`, `venv/`, `env/` (`.venv313/` too — git-ignores itself via its internal `.gitignore`)
- `desktop/node_modules/`, `desktop/src-tauri/target/` (Rust cache, ~1.6 GB), `desktop/src-tauri/gen/`, `desktop/src-tauri/binaries/` (staged sidecar, ~250 MB)
- `build/`, `dist/`, `*.egg-info/`, `__pycache__/`, `.worktrees/`

A clean source tree is only a few MB — zip the repo excluding the above, or copy the git working tree.

**Copy the user-data dir `%USERPROFILE%\.evi\`** — this is real state:
`config.toml`, `evi-updater.key` / `.key.pub` / `.pass` (**signing keys — losing them breaks future update verification**), `tokens/` (OAuth), `backends.json`, `peers.json`, `users.json`, `trusted_folders.json`, `hooks.toml`, `mcp.json`, `channels/` (Telegram pairings), `profiles/`, `skills/`, `commands/`, `transcripts/`, `indices/`, `images/`, `screenshots/`, `uploads/`, `scheduled/`, `logs/`, `checkpoints/`, `memory/`, `recipes/`, `styles/`, `routes.json`. `models/` and `runtime/` are large and re-downloadable (the managed runtime fetches them again on request).

### 7b. Set up on the new machine

```powershell
# 1. Install toolchains (Python 3.12+ always; Rust 1.90+ + MSVC C++ Build Tools + Node LTS only for desktop)
# 2. Place the repo (ideally at the SAME path — see 7c), then:
cd C:\evi
py -3.13 -m venv .venv                          # 3.12 is the floor; 3.13 is what CI and the sidecar use
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[web,mcp,scheduler,downloads,web-tools,computer,stt,pdf,index,calendar,rerank,email,dev]"
#    (or: scripts\dev-bootstrap.ps1, then `evi doctor --dev`)

# 3. Sanity check
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m evi --version         # matches pyproject.toml

# 4. (Desktop only) freeze sidecar + build
py -3.13 -m venv .venv-build
powershell -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1
cd desktop ; npm install ; npm run tauri build -- --config src-tauri\tauri.standalone.conf.json
```

Toolchains: Python 3.12+ (everything); Rust + Cargo, MSVC C++ Build Tools, Node LTS + npm, WebView2 (desktop only, WebView2 preinstalled on Win10/11); tesseract / ffmpeg optional (OCR / audio, else those tools degrade). Restore `%USERPROFILE%\.evi\` from your copy afterward.

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

> Session JSONLs can be large (tens of MB); resuming replays them into context, so expect a slower first turn. `evi sessions import --source claude-code` can also copy them into eVi's own store.

## 8. Layout cheatsheet

```
C:\evi
├─ evi/                       shared Python core (module map in §9)
│  ├─ __init__.py             __version__ (must equal pyproject.toml)
│  ├─ llm/                    agent.py (the loop), client.py, subagent.py, responses.py, specialty.py, CLI-agent drivers
│  ├─ backends/               lmstudio, ollama, llamacpp (8080–8090 discovery), openai_compat, CLI-agent backends, registry, factory
│  ├─ runtime/                managed llama.cpp: download + pins, catalog, server supervisor, setup
│  ├─ channels/               Telegram chat channel + pairing
│  ├─ tools/                  base.py (REGISTRY) + the built-in tool modules; resolver.py, monitor.py, ask.py …
│  ├─ mcp/                    bridge.py, manager.py, servers.py, publish.py
│  ├─ apps/{cli,web}/         Typer CLI + FastAPI/SSE web (routes listed in §9)
│  ├─ sdk/                    embeddable SDK (builder.py)
│  └─ data/                   models-catalog.json (models.dev snapshot), runtime_models.json (managed catalog)
├─ desktop/                   Tauri 2 shell (NOT a Python package)
│  ├─ dist-shim/index.html    loading spinner
│  ├─ package.json            version auto-synced from the release tag in CI
│  └─ src-tauri/              Rust src/ (main.rs, sidecar_update.rs), Cargo.toml, tauri.conf.json + tauri.standalone.conf.json,
│                             capabilities/, permissions/, icons/, binaries/ (staged sidecar)
├─ editors/vscode/            VS Code extension (TypeScript; FIM + chat webview)
├─ scripts/                   build-sidecar.*, build-desktop.*, dev-bootstrap.*, handoff_facts.py, check_docs_freshness.py,
│                             pin_runtime_hashes.py, build-site-docs.py, build-wiki.py, test.*, install.*
├─ docs/                      features.md + features/<area>.md, configuration.md, roadmap.md, releasing.md …
├─ tests/                     pytest suite (counts in §9) + tests/e2e (Playwright), tests/cli_e2e, tests/desktop_e2e
├─ .github/workflows/         listed in §9
├─ EVI.md                     the implementer contract (auto-loaded as project context)
├─ CHANGELOG.md
└─ pyproject.toml             dist name evi-assistant, version, requires-python, extras (all in §9), MIT

%USERPROFILE%\.evi\                        user data (config, signing keys, models, transcripts, memory, …)
%USERPROFILE%\.claude\projects\<mangled>\  Claude Code chat history + cross-session memory
```

## 9. Generated facts

<!-- BEGIN GENERATED FACTS (scripts/handoff_facts.py write; do not edit by hand) -->

_Generated from the working tree by `python scripts/handoff_facts.py write`; `python scripts/handoff_facts.py check` fails when this block is stale._

**Package:** `evi-assistant` 1.0.53 · Python `>=3.12` · console scripts `evi`

| Fact | Count |
|---|---|
| Top-level modules in `evi/` | 99 |
| Subpackages | 10 |
| Built-in tool modules (`_BUILTIN_TOOL_MODULES`) | 22 |
| Built-in tools (`@tool` functions under `evi/tools/`) | 61 |
| Web routes (method + path) | 111, 106 under `/api/` |
| CLI: top-level `evi` commands and groups | 62 (37 groups) |
| CLI: every command and group, nested | 249 (42 groups) |
| Test files, unit (`tests/test_*.py`) | 217 |
| Test files, UI e2e (`tests/e2e/test_*.py`) | 26 |
| Test files, CLI e2e (`tests/cli_e2e/test_*.py`) | 2 |
| Test files, desktop e2e (`tests/desktop_e2e/test_*.py`) | 1 |
| Optional-dependency extras | 22 |
| GitHub workflows | 9 |

Test files, not tests: the number of tests changes with nearly every commit. `python -m pytest --collect-only -q` counts them.

**Extras (22):** `email`, `web`, `mcp`, `scheduler`, `downloads`, `web-tools`, `computer`, `stt`, `pdf`, `index`, `calendar`, `rerank`, `ast`, `diarize`, `doc`, `claude-agent`, `telemetry`, `otel`, `moderation`, `e2e`, `build-desktop`, `dev`

**Built-in tool modules (22):** `fs`, `code`, `shell`, `memory`, `skills`, `subagent`, `websearch`, `git`, `index`, `calendar`, `pdf`, `sqlite`, `ocr`, `rerank`, `monitor`, `image_comfy`, `voice`, `computer`, `federation`, `ask`, `vision_tool`, `bugledger`

**Workflows** (`.github/workflows/`)

| File | Name | Triggers | Jobs |
|---|---|---|---|
| `ci.yml` | ci | `push`, `pull_request`, `schedule`, `workflow_dispatch` | `test`, `mcp-sdk`, `rust`, `rust-windows`, `actionlint`, `workflow-injection` |
| `desktop-release.yml` | desktop-release | `push`, `workflow_dispatch`, `workflow_call` | `build`, `publish-sidecar` |
| `docker.yml` | docker | `workflow_call`, `workflow_dispatch` | `build-push` |
| `docs-freshness.yml` | docs-freshness | `pull_request`, `push` | `docs-freshness` |
| `docs-publish.yml` | docs-publish | `push`, `pull_request`, `workflow_dispatch` | `publish` |
| `e2e.yml` | e2e | `push`, `pull_request`, `schedule`, `workflow_dispatch`, `workflow_call` | `ui` |
| `evi-run-example.yml` | evi-run (example) | `workflow_dispatch` | `ask` |
| `release.yml` | release | `push`, `workflow_dispatch` | `e2e`, `build-and-publish`, `desktop`, `docker` |
| `security.yml` | security | `push`, `pull_request`, `schedule`, `workflow_dispatch` | `python-audit`, `rust-audit`, `secret-scan`, `codeql` |

**Module map**

| Package | Modules |
|---|---|
| `evi/` (99) | a2a, anatomy, attest, audio_input, backup, batch, bugledger, calendar, capabilities, checkpoints, checks, citations, codeintel, commands, complete, config, configlint, context_report, debug, deeplinks, diarize, doclayout, doctor, downloads, dream, embedcap, embeddings, evals, evals_fake, events, evidence, federation, finetune, firstrun, gitsafe, guardmodel, guardrails, hardware, headless, history, hooks, index, keybindings, marketplace, memory, modelsdev, moderation, modes, netguard, notify, obsidian, otel, permissions, plugins, policy, portprobe, proc, profiles, project, pyanalyze, reasoning, recipes, recommend, redact, reflect, repl_input, reporting, review, routines, routing, safemode, sandbox, scheduled, scheduler, search, sessionimport, sessions, shell_guard, shellsplit, skills, skillscope, stats, statusline, structured, styles, sync, tasks, teams, toolcalling, transcripts, trust, ultracode, update, users, vision, voice, workdir, workflows, worktree |
| `evi/apps/cli/` (1) | main |
| `evi/apps/web/` (3) | ids, mdlite, server |
| `evi/backends/` (14) | amp_agent, base, claude_agent, codex_agent, copilot_agent, factory, gemini_agent, llamacpp, lmstudio, ollama, openai_compat, presets, qwen_agent, registry |
| `evi/channels/` (5) | inbox, pairing, policy, telegram, telegram_api |
| `evi/llm/` (12) | agent, amp_agent, claude_agent, cli_agent, client, codex_agent, copilot_agent, gemini_agent, qwen_agent, responses, specialty, subagent |
| `evi/mcp/` (5) | agent_tools, bridge, manager, publish, servers |
| `evi/runtime/` (5) | catalog, llama_server, llamacpp_runtime, reap, setup |
| `evi/sdk/` (1) | builder |
| `evi/tools/` (25) | ask, availability, base, bugledger, calendar, code, computer, federation, fs, git, image_comfy, index, memory, monitor, ocr, pdf, rerank, resolver, shell, skills, sqlite, subagent, vision_tool, voice, websearch |

**Web routes**

| Prefix | Routes |
|---|---|
| `/` (1) | `GET /` |
| `/.well-known` (1) | `GET /.well-known/agent-card.json` |
| `/a2a` (1) | `POST /a2a` |
| `/api/auth` (1) | `GET /api/auth/check` |
| `/api/backend` (7) | `POST /api/backend/install`, `POST /api/backend/open-download`, `GET /api/backend/pull`, `POST /api/backend/start`, `GET /api/backend/status`, `POST /api/backend/use`, `POST /api/backend/verify` |
| `/api/backends` (4) | `GET /api/backends`, `POST /api/backends`, `DELETE /api/backends/{name}`, `PATCH /api/backends/{name}` |
| `/api/channels` (4) | `GET /api/channels/telegram`, `POST /api/channels/telegram/approve`, `POST /api/channels/telegram/restart`, `POST /api/channels/telegram/revoke` |
| `/api/chat` (1) | `POST /api/chat` |
| `/api/checkpoints` (1) | `GET /api/checkpoints` |
| `/api/complete` (1) | `POST /api/complete` |
| `/api/config` (2) | `GET /api/config`, `POST /api/config` |
| `/api/decide` (1) | `POST /api/decide` |
| `/api/dispatch` (4) | `GET /api/dispatch`, `GET /api/dispatch/stream`, `POST /api/dispatch/ultracode`, `POST /api/dispatch/workflow/{name}` |
| `/api/docs` (2) | `GET /api/docs`, `GET /api/docs/{slug}` |
| `/api/doctor` (1) | `GET /api/doctor` |
| `/api/evals` (2) | `GET /api/evals`, `POST /api/evals/run` |
| `/api/federate` (1) | `POST /api/federate` |
| `/api/guardrails` (2) | `GET /api/guardrails`, `POST /api/guardrails` |
| `/api/health` (1) | `GET /api/health` |
| `/api/hooks` (2) | `GET /api/hooks`, `POST /api/hooks` |
| `/api/integrations` (1) | `GET /api/integrations/status` |
| `/api/mcp` (8) | `GET /api/mcp`, `POST /api/mcp`, `POST /api/mcp/remove`, `GET /api/mcp/serve`, `POST /api/mcp/serve`, `GET /api/mcp/serve/config`, `POST /api/mcp/serve/stop`, `POST /api/mcp/toggle` |
| `/api/model-picker` (2) | `GET /api/model-picker`, `POST /api/model-picker` |
| `/api/modes` (1) | `GET /api/modes` |
| `/api/peers` (7) | `GET /api/peers`, `POST /api/peers`, `POST /api/peers/bind-lan`, `POST /api/peers/preflight`, `POST /api/peers/remove`, `POST /api/peers/scan`, `POST /api/peers/serve` |
| `/api/plugins` (3) | `GET /api/plugins`, `POST /api/plugins/install`, `POST /api/plugins/remove` |
| `/api/project` (2) | `GET /api/project/trust`, `POST /api/project/trust` |
| `/api/recipes` (2) | `GET /api/recipes`, `POST /api/recipes/run` |
| `/api/reset` (1) | `POST /api/reset` |
| `/api/rewind` (1) | `POST /api/rewind` |
| `/api/routes` (3) | `GET /api/routes`, `POST /api/routes`, `POST /api/routes/remove` |
| `/api/routine` (1) | `POST /api/routine/{token}` |
| `/api/runtime` (11) | `GET /api/runtime/catalog`, `GET /api/runtime/detect`, `POST /api/runtime/gpu`, `POST /api/runtime/locate`, `GET /api/runtime/log`, `POST /api/runtime/model/remove`, `POST /api/runtime/model/use`, `GET /api/runtime/plan`, `POST /api/runtime/setup`, `GET /api/runtime/status`, `POST /api/runtime/stop` |
| `/api/session` (18) | `GET /api/session/cwd`, `POST /api/session/cwd`, `POST /api/session/{session_id}/branch`, `POST /api/session/{session_id}/cancel`, `GET /api/session/{session_id}/channel`, `POST /api/session/{session_id}/channel`, `GET /api/session/{session_id}/context`, `POST /api/session/{session_id}/edit`, `POST /api/session/{session_id}/handoff`, `GET /api/session/{session_id}/history`, `POST /api/session/{session_id}/message`, `POST /api/session/{session_id}/mode`, `POST /api/session/{session_id}/name`, `POST /api/session/{session_id}/reroll`, `POST /api/session/{session_id}/steer`, `POST /api/session/{session_id}/title`, `POST /api/session/{session_id}/truncate`, `GET /api/session/{session_id}/usage` |
| `/api/specialty` (1) | `GET /api/specialty/status` |
| `/api/stats` (1) | `GET /api/stats` |
| `/api/styles` (1) | `GET /api/styles` |
| `/api/system` (1) | `GET /api/system` |
| `/api/transcribe` (1) | `POST /api/transcribe` |
| `/api/upload` (1) | `POST /api/upload` |
| `/api/voice` (1) | `GET /api/voice/status` |
| `/api/whoami` (1) | `GET /api/whoami` |
| `/images` (1) | `GET /images/{name:path}` |
| `/uploads` (1) | `GET /uploads/{session_id}/{name}` |

**CLI**

| Command | Subcommands |
|---|---|
| `evi` (commands) | anatomy, batch, chat, complete, doctor, dream, edit, init, link, lint, reflect, review, review-remember, rewind, run, scheduler, search, setup, stats, tail, tools, trust, ultracode, variants, web |
| `evi agents` | list, new |
| `evi attest` | init, status |
| `evi backend` | add, list, remove, use |
| `evi backup` | create, restore |
| `evi calendar` | add, list, peek, remove |
| `evi channel` | approve, revoke, serve, status |
| `evi command` | list, new |
| `evi config` | path, show |
| `evi eval` | baseline, compare, list, new, run |
| `evi eval baseline` | record |
| `evi evidence` | collect, commit-body, prune |
| `evi finetune` | export |
| `evi guardrails` | list, path, test |
| `evi hooks` | list, path, test |
| `evi keybindings` | list, path |
| `evi mcp` | add, add-http, disable, enable, list-servers, list-tools, path, remove, serve, serve-config |
| `evi model` | list, remove, use |
| `evi models` | active, backend, info, list, preset, pull, recommend, refresh, specialty, use |
| `evi models specialty` | clear, list, set |
| `evi obsidian` | pull, push, status, sync |
| `evi peer` | add, doctor, list, remove, run, scan |
| `evi plugin` | add, disable, enable, index, init, install, list, remove, search |
| `evi plugin index` | add, init |
| `evi profile` | active, add, list, path, remove, show |
| `evi recipe` | list, new, run, show |
| `evi route` | add, classifier, disable, enable, list, preset, remove, test |
| `evi routine` | add, list, remove, run |
| `evi runtime` | gpu, setup, status, stop |
| `evi schedule` | add, disable, enable, list, remove, run-now |
| `evi sessions` | continue, export, fork, handoff, import, list, purge, resume, search, show, title |
| `evi skill` | add, import, list, remove, show |
| `evi style` | list, show, use |
| `evi sync` | init, pull, push, status |
| `evi task` | claim, list, new, reject, release, reopen, show, start, submit, verify |
| `evi team` | add, clear, list, new, run |
| `evi update` | check, from-wheel, history, prune, rollback, settings |
| `evi voice` | backend, diarize, engines, listen, loop, speak, transcribe |
| `evi web-config` | token, users |
| `evi web-config token` | clear, rotate, show |
| `evi web-config users` | add, list, remove |
| `evi workflow` | list, new, run, show |
| `evi worktree` | chat, create, list, prune, remove |

<!-- END GENERATED FACTS -->
