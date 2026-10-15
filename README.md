# eVi — personal AI assistant

[![PyPI](https://img.shields.io/pypi/v/evi-assistant.svg)](https://pypi.org/project/evi-assistant/)
[![Downloads](https://static.pepy.tech/badge/evi-assistant)](https://pepy.tech/project/evi-assistant)
[![Docker (GHCR)](https://img.shields.io/badge/docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/evi-assistant/evi-ai/pkgs/container/evi-ai)
[![GHCR pulls](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fghcr-badge.elias.eu.org%2Fapi%2Fevi-assistant%2Fevi-ai&query=%24.downloadCount&label=ghcr%20pulls&logo=docker&color=2496ED)](https://github.com/evi-assistant/evi-ai/pkgs/container/evi-ai)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
<!--
  Docker pull-count badges. GHCR does not expose pulls via the GitHub API, so both
  of these read the number off the GHCR package page:
    • ghcr-badge (used above) — public hosted instance, no setup:
      https://ghcr-badge.elias.eu.org  →  /api/evi-assistant/evi-ai  (JSON) or /shield/... (SVG)
    • pkgbadge (https://github.com/will-luck/pkgbadge) — SELF-HOST only (image
      ghcr.io/will-luck/pkgbadge, port 8080, PKGBADGE_PACKAGES=evi-assistant/evi-ai).
      Once hosted, swap in:
      [![GHCR pulls](https://img.shields.io/endpoint?url=https://YOUR-HOST/evi-assistant/evi-ai/pulls.json)](https://github.com/evi-assistant/evi-ai/pkgs/container/evi-ai)
-->


**eVi 1.0 — shipped and public.** Local-first personal assistant. Chat with a model on **your** hardware,
let it use tools, generate images, automate scheduled tasks, drive your
browser, and reach the same core from a terminal, a web app, or a
native desktop window.

> One Python core, three frontends, no cloud round-trips.

```
       ┌──────────────────────────────┐
       │       evi (core library)     │
       │  Agent · Tools · Memory ·    │
       │  Skills · Hooks · MCP ·      │
       │  Scheduler · Dream · …       │
       └────┬──────────┬──────────┬───┘
            │          │          │
        ┌───▼──┐   ┌───▼──┐   ┌───▼────┐
        │ CLI  │   │ Web  │   │Desktop │
        │      │   │ SSE  │   │ Tauri  │
        └──────┘   └──────┘   └────────┘
```

## Requirements

| Component       | Why                              | Notes                  |
|-----------------|----------------------------------|------------------------|
| **Python 3.12+** | Core runtime                     | CI tests 3.12 and 3.13; the `[ast]` extra needs 3.13 |
| **Git 2.17+**   | Optional — `evi worktree`        | 2.28+ for `init -b`    |
| **An LLM backend** | LM Studio / Ollama / llama-server / any OpenAI-compatible endpoint, or a CLI agent you already use (Claude Code, Codex, …) | None installed? `evi runtime setup` downloads one |
| **NVIDIA GPU**  | Optional — speeds up local LLMs and ComfyUI image gen | CPU fallback works |

## Quickstart

> **Package name:** the project is **eVi**, but the PyPI distribution is
> **`evi-assistant`** (the bare `evi` name was taken). Install with
> `pip install evi-assistant`; the import package and CLI command are both still
> `evi` (`import evi`, run `evi`).

```bash
pip install 'evi-assistant[web]'         # from PyPI, with the web UI
evi web                                  # open the printed URL: first run offers what this machine has
```

No model server yet? The first-run screen (web and desktop) finds AI coding
CLIs you already use, local servers that are running and API keys in your
environment, and otherwise downloads a small local model for you, then checks
that it actually answers ([first run](docs/features/onboarding.md)). From a
terminal:

```bash
evi runtime setup                        # download eVi's own llama.cpp + a small model, no account
evi web                                  # runs that model while it runs; `evi chat` can use it too
# or, with Ollama / LM Studio already serving a model:
evi models recommend                     # honest read on what'll fit
evi chat                                 # off you go
```

The desktop app (Windows, macOS, Linux) is on the
[releases page](https://github.com/evi-assistant/evi-ai/releases); it needs no
Python. See [Code signing](#code-signing) for what to expect on first launch.

Or work from a clone (editable install):

```bash
git clone https://github.com/evi-assistant/evi-ai.git evi && cd evi
python -m venv .venv                   # Python 3.12 or newer
.venv\Scripts\Activate.ps1            # Windows
# source .venv/bin/activate            # Linux / macOS

pip install -e '.[web,dev]'
# or: scripts/dev-bootstrap.sh / scripts\dev-bootstrap.ps1, then `evi doctor --dev`

# Pick a backend. LM Studio + a tool-capable model is the easiest first run.
# In LM Studio: load qwen2.5-7b-instruct → Developer → Start Server.

evi models recommend                    # honest read on what'll fit
evi chat                                # off you go
```

`/help` inside the REPL lists every slash command (goal tracking, plan
mode, auto-approve, model switching, user-defined templates).

## Three-machine setup

eVi is built to span machines. A typical setup:

| Machine          | Role                | Backend           |
|------------------|---------------------|-------------------|
| AI server (P40)  | LLM host + web UI   | Ollama or llama-server, `evi web` on port 8000 |
| Desktop (16 GB)  | Full power workstation | Local LM Studio |
| Laptop (2 GB)    | Thin client           | Profile points at the AI server |

See [docs/multi-machine.md](docs/multi-machine.md) for the wiring. Serving the
LAN takes an access token (`evi web-config token rotate`): without one, eVi
answers only the machine it runs on. [docs/security.md](docs/security.md) has
the rules.

## Major commands

```text
evi chat                           Start the REPL
evi web                            Run the FastAPI + SSE web UI (127.0.0.1:8000)
evi runtime setup / status / gpu   eVi's own llama.cpp runtime: download, run, use the GPU
evi model list / use / remove      The managed runtime's model catalog
evi backend add / use / list       Register backends (incl. Claude Code, Codex, … CLI agents)
evi models recommend / list / use  Hardware-aware model selection (Ollama, LM Studio, …)
evi models pull <ref>              Pull via Ollama tag or hf:<repo>:<file>
evi channel status / approve       Telegram: reach eVi from your phone (off by default)
evi sessions list / resume / import  Past chats; import Claude Code and Codex history
evi trust [PATH]                   Trust a repo's .evi.toml (untrusted folders ignore it)
evi doctor                         Check the environment, backends and keys
evi dream                          Curate long-term memory from yesterday's chats
evi schedule add / list / run-now  Cron-style scheduled prompts
evi scheduler                      Foreground daemon for scheduled tasks
evi worktree create <branch>       Spin up a git worktree for parallel work
evi profile add home --backend …   Per-machine config overlays
evi voice listen / speak           STT + TTS
evi mcp list-tools                 Show MCP server-provided tools
evi mcp serve                      Run eVi AS an MCP server (other agents use eVi's tools)
```

Every command and route: the generated facts in [HANDOFF.md](HANDOFF.md), and
the [feature catalog](docs/features.md).

## What's built

Everything below has shipped (1.0 and the 1.0.x releases since). Gmail /
Microsoft 365 email is the one deferred surface — scaffolded but off by default.
What comes next is in the [roadmap](docs/roadmap.md).

| Feature                                                       | Status |
|---------------------------------------------------------------|--------|
| Foundation, CLI, agent loop, fs/code tools                    | ✅     |
| ComfyUI image generation                                      | ✅     |
| FastAPI + SSE web UI                                          | ✅     |
| Tauri desktop shell (local + remote modes)                    | ✅     |
| Persistent memory + scoped subagents                          | ✅     |
| MCP (Model Context Protocol) integration                      | ✅     |
| Skills + scheduled tasks                                      | ✅     |
| Backends, model registry, hardware recommender, profiles      | ✅     |
| EVI.md, slash commands, /goal, plan mode                      | ✅     |
| Hooks, auto mode, git worktrees                               | ✅     |
| Transcripts, dreaming, web search, voice TTS, computer use    | ✅     |
| STT, web UI parity, polish                                    | ✅     |
| Federation + A2A, ultracode, workflows, agent teams, evals    | ✅     |
| CLI-agent backends (Claude Code, Codex, Gemini, Amp, Qwen, Copilot) | ✅ |
| VS Code extension (local completion + chat)                   | ✅     |
| Managed llama.cpp runtime (catalog, GPU, your own build)      | ✅     |
| First-run screen with a verified backend switch               | ✅     |
| Telegram chat channel, session import, steering a running turn | ✅    |
| Folder trust, per-command shell rules, protected paths        | ✅     |
| Gmail / Microsoft 365 email                                   | ⏸ deferred |

## Layout

```
evi/                core library
  llm/              agent loop (agent.py), client, subagents, CLI-agent drivers
  backends/         LM Studio / Ollama / llama.cpp / OpenAI-compat / CLI-agent backends
  tools/            built-in tool catalog (fs, code, shell, web, memory, git, …)
  runtime/          managed llama.cpp runtime: download, catalog, server supervisor
  channels/         Telegram chat channel + pairing
  mcp/              MCP client bridge + manager, `evi mcp serve`
  sdk/              embeddable Agent SDK (build_agent)
  memory.py         long-term memory store
  skills.py         user skill packets
  config.py         config.toml schema + loading
  permissions.py    permission modes + rules
  trust.py          folder trust for project config
  sessionimport.py  Claude Code / Codex history import
  …
  apps/             frontends that consume the core (shipped in the wheel)
    cli/main.py     Typer CLI
    web/server.py   FastAPI + SSE
    web/static/     chat UI (vanilla JS)
  data/             model catalogs (models.dev snapshot, managed-runtime models)
desktop/            Tauri 2 shell (NOT a Python package; local-spawn or EVI_REMOTE_URL)
editors/vscode/     VS Code extension (TypeScript)
tests/              pytest: unit, tests/e2e (Playwright), tests/cli_e2e, tests/desktop_e2e
docs/               deeper guides (features/ has one page per area)
scripts/            install, build, release and dev helpers
```

The full module map, with counts, is generated into [HANDOFF.md](HANDOFF.md)
by `python scripts/handoff_facts.py write`.

## Configuration

Per-user config lives in `~/.evi/`. Highlights:

```
~/.evi/
  config.toml         primary settings — backend, tools, auto-approve
  profiles/*.toml     overlay profiles for per-machine config
  memory/*.md         long-term memory; `.attic/` holds soft-deleted entries
  skills/<name>/      installed skill packets (SKILL.md + assets)
  commands/<name>.md  user-defined slash command templates
  scheduled/*.json    saved scheduled prompts
  hooks.toml          before/after_tool_call hooks
  mcp.json            MCP server list
  transcripts/        per-session JSONL (input to dreaming)
  logs/               run logs (dreams, scheduled tasks)
  runtime/            managed llama.cpp runtime + its server log
  models/             downloaded GGUF models
  channels/           Telegram pairing state (who is paired or waiting)
```

See [docs/configuration.md](docs/configuration.md) for the full reference, the
[feature catalog](docs/features.md) for what every feature does + how to use it,
and [surface parity](docs/cli-parity.md) for the CLI ↔ Web ↔ Desktop map.

## Optional dependency groups

```bash
pip install -e '.[web]'        # FastAPI + uvicorn for `evi web`
pip install -e '.[mcp]'        # Model Context Protocol client
pip install -e '.[scheduler]'  # APScheduler for cron-style tasks
pip install -e '.[downloads]'  # huggingface_hub for `evi models pull hf:...`
pip install -e '.[web-tools]'  # DuckDuckGo search + BeautifulSoup
pip install -e '.[stt]'        # faster-whisper + sounddevice
pip install -e '.[computer]'   # pyautogui for mouse/keyboard control
pip install -e '.[dev]'        # pytest + ruff
```

Or all at once:

```bash
pip install -e '.[dev,web,mcp,scheduler,downloads,web-tools,stt,computer]'
```

## Development

```bash
pip install -e '.[dev]'
pytest -q                          # the unit suite (E2E are opt-in: -m e2e)
pytest --collect-only -q           # how many tests there are, today
ruff check evi tests scripts       # style + bug-pattern lint
```

See [docs/development.md](docs/development.md) for architecture notes, and
[docs/self-build.md](docs/self-build.md) for developing/building eVi **with eVi**
(the `EVI.md` project context + the one-command `scripts/build-desktop` build).

## Safety posture

- **Default tool toggles** lean conservative: `shell`, `subagent`, `web`,
  `voice`, `computer`, `gmail`, `outlook`, `image`, and `mcp` are all OFF
  until you flip them in `config.toml`.
- **`auto.auto_approve`** lists categories that run without prompting.
  The defaults are `fs, code, memory, skills, image`. `computer` is never
  in this list — every mouse click / keystroke prompts.
- **The web server answers only this machine** until you set an access token,
  and `evi web` won't bind a LAN address without one. The desktop app and the
  Docker image generate their own when they serve the network. Another
  website can't drive eVi through your browser, a session runs one turn at a
  time, and an unanswered permission prompt is denied after 5 minutes. See
  [Security](docs/security.md).
- **Hook vetoes** (`~/.evi/hooks.toml`) can block any tool by glob match.
- **Soft-delete memory** sends "forgotten" entries to `~/.evi/memory/.attic/`
  so the dreaming engine can't permanently lose anything.
- **Repositories you didn't write** can't configure eVi until you trust them
  (`evi trust`), and a trusted `.evi.toml` can only tighten security. Writes to
  eVi's own instructions, config, hooks and CI files ask in every mode, and the
  destructive-command guard asks before lines like `rm -rf ~` or
  `git push --force`. See [Permissions & sandbox](docs/features/permissions-and-sandbox.md).

## Code signing

**Status today:** the desktop installers are **not yet OS-code-signed**.
Windows SmartScreen and macOS Gatekeeper may warn on first launch (on macOS,
right-click the app and choose **Open**). What is signed: in-app updates
(minisign, verified by the app before it installs anything), the core-only
update channel (a minisign-signed manifest plus SHA-256 per platform) and the
PyPI package (Sigstore attestations through Trusted Publishing).

Windows Authenticode signing through SignPath Foundation is being set up; the
[Code signing policy](docs/code-signing.md) covers its rollout, who approves
releases, and how to verify a download. macOS notarization needs a paid Apple
Developer ID; whether to get one is an open question in the [roadmap](docs/roadmap.md).

Free code signing provided by [SignPath.io](https://about.signpath.io),
certificate by [SignPath Foundation](https://signpath.org).

## License

MIT - see [LICENSE](LICENSE).
