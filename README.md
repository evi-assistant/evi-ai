# Evi — personal AI assistant

Local-first personal assistant. Chat with a model on **your** hardware,
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
| **Python 3.11+** | Core runtime                     | 3.12 tested            |
| **Git 2.17+**   | Optional — `evi worktree`        | 2.28+ for `init -b`    |
| **An LLM backend** | One of LM Studio / Ollama / llama-server / any OpenAI-compatible endpoint | LM Studio default |
| **NVIDIA GPU**  | Optional — speeds up local LLMs and ComfyUI image gen | CPU fallback works |

## Quickstart

> **Package name:** the project is **Evi**, but the PyPI distribution is
> **`evi-ai`** (the bare `evi` name was taken). Once published you'll
> `pip install evi-ai`; the import package and CLI command are both still
> `evi` (`import evi`, run `evi`). The steps below use an editable install
> from a clone, which doesn't depend on the published name.

```bash
git clone <repo> evi && cd evi
python -m venv .venv
.venv\Scripts\Activate.ps1            # Windows
# source .venv/bin/activate            # Linux / macOS

pip install -e .

# Pick a backend. LM Studio + a tool-capable model is the easiest first run.
# In LM Studio: load qwen2.5-7b-instruct → Developer → Start Server.

evi models recommend                    # honest read on what'll fit
evi chat                                # off you go
```

`/help` inside the REPL lists every slash command (goal tracking, plan
mode, auto-approve, model switching, user-defined templates).

## Three-machine setup

Evi is built to span machines. A typical setup:

| Machine          | Role                | Backend           |
|------------------|---------------------|-------------------|
| AI server (P40)  | LLM host + web UI   | Ollama or llama-server, `evi web` on port 8000 |
| Desktop (16 GB)  | Full power workstation | Local LM Studio |
| Laptop (2 GB)    | Thin client           | Profile points at the AI server |

See [docs/multi-machine.md](docs/multi-machine.md) for the wiring.

## Major commands

```text
evi chat                           Start the REPL
evi web --host 0.0.0.0             Run the FastAPI + SSE web UI
evi dream                          Curate long-term memory from yesterday's chats
evi models recommend / list / use  Hardware-aware model selection
evi models pull <ref>              Pull via Ollama tag or hf:<repo>:<file>
evi schedule add / list / run-now  Cron-style scheduled prompts
evi scheduler                      Foreground daemon for scheduled tasks
evi worktree create <branch>       Spin up a git worktree for parallel work
evi profile add home --backend …   Per-machine config overlays
evi voice listen / speak           STT + TTS
evi mcp list-tools                 Show MCP server-provided tools
evi mcp serve                      Run Evi AS an MCP server (other agents use Evi's tools)
```

## What's built

| Phase | Feature                                          | Status |
|-------|--------------------------------------------------|--------|
| 1     | Foundation, CLI, agent loop, fs/code tools       | ✅     |
| 2     | Gmail / Microsoft 365 email                      | ⏸ deferred |
| 3     | ComfyUI image generation                         | ✅     |
| 4     | FastAPI + SSE web UI                             | ✅     |
| 5     | Tauri desktop shell (local + remote modes)       | ✅     |
| 6     | Persistent memory + scoped subagents             | ✅     |
| 7     | MCP (Model Context Protocol) integration         | ✅     |
| 8     | Skills + scheduled tasks                         | ✅     |
| 9     | Backends, model registry, hardware recommender, profiles | ✅ |
| 10    | EVI.md, slash commands, /goal, plan mode         | ✅     |
| 11    | Hooks, auto mode, git worktrees                  | ✅     |
| 12    | Transcripts, dreaming, web search, voice TTS, computer use | ✅ |
| 13    | STT, web UI parity, polish                       | ✅     |

## Layout

```
evi/                core library
  agent.py          agent loop with permission + hooks
  backends/         LM Studio / Ollama / llama.cpp / OpenAI-compat
  llm/              client + subagent runner
  tools/            built-in tool catalog (fs, code, image, web, voice, …)
  mcp/              MCP client bridge + manager
  memory.py         long-term memory store
  skills.py         user skill packets
  scheduler.py      APScheduler driver
  dream.py          memory-consolidation runner
  hardware.py       GPU + RAM detection
  recommend.py      curated model picks per VRAM
  …
  apps/             frontends that consume the core (shipped in the wheel)
    cli/main.py     Typer CLI
    web/server.py   FastAPI + SSE
    web/static/     chat UI (vanilla JS)
desktop/            Tauri 2 shell (NOT a Python package; local-spawn or EVI_REMOTE_URL)
tests/              pytest
docs/               deeper guides
scripts/            install + dev helpers
```

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
```

See [docs/configuration.md](docs/configuration.md) for the full reference.

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
pytest -q              # 183 tests, ~12 s
ruff check evi apps    # style + bug-pattern lint
```

See [docs/development.md](docs/development.md) for architecture notes.

## Safety posture

- **Default tool toggles** lean conservative: `shell`, `subagent`, `web`,
  `voice`, `computer`, `gmail`, `outlook`, `image`, and `mcp` are all OFF
  until you flip them in `config.toml`.
- **`auto.auto_approve`** lists categories that run without prompting.
  The defaults are `fs, code, memory, skills, image`. `computer` is never
  in this list — every mouse click / keystroke prompts.
- **Hook vetoes** (`~/.evi/hooks.toml`) can block any tool by glob match.
- **Soft-delete memory** sends "forgotten" entries to `~/.evi/memory/.attic/`
  so the dreaming engine can't permanently lose anything.

## License

MIT (or whatever you prefer — pick when publishing).
