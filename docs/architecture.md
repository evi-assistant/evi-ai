# Architecture

A walk through how eVi's pieces fit together. Read this once when you join
the project; refer back to the per-module docs when you need detail.

## One core, three frontends

```
          CLI (evi/apps/cli/main.py)
                    │
        Web (evi/apps/web/server.py)─── browser (evi/apps/web/static/)
                    │
       Desktop (desktop/) ──────────── Tauri webview → Web UI
                    │
                    ▼
        ┌─────────────────────┐
        │       evi.Agent     │   evi/llm/agent.py
        │  ┌───────────────┐  │
        │  │ system prompt │  │   composed from base + memory + skills + project
        │  ├───────────────┤  │
        │  │  chat history │  │
        │  ├───────────────┤  │
        │  │ tool dispatch │  │   permission → hooks → tool.call → hooks
        │  └───────────────┘  │
        └────┬───────┬────────┘
             │       │
             ▼       ▼
     ┌──────────┐ ┌──────────────────────────────┐
     │ Backend  │ │  Tool registry               │
     │ (OpenAI- │ │  evi.tools.base.REGISTRY     │
     │ compat   │ │  decorator builds JSON-schema │
     │ chat)    │ │  from type hints              │
     └──────────┘ └──────────────────────────────┘
```

The three frontends create *their own* `Agent` instance; nothing is shared
in-process across frontends. The web UI keeps one `Agent` per `session_id`
in a dict; the CLI has one per `evi chat` invocation; Tauri runs the same
web server underneath, so it gets web-mode sessions.

## The agent loop

`Agent.chat(user_msg)` is a generator that yields `Event`s
(`TextDelta`, `ToolCall`, `ToolResult`, `Done`, `Error`). Each turn:

1. Optionally prepend a `[ongoing goal: …]` reminder if `agent.goal` is set.
2. Optionally append a plan-only suffix and pass `tools=None` if
   `plan_mode_once` is set.
3. Open an OpenAI streaming completion against the configured backend.
4. As deltas arrive: text → `TextDelta`, tool-call deltas accumulate.
5. If the model finishes with tool calls, dispatch each through
   `_invoke_tool`: permission check → before-hooks (may veto) → tool call →
   after-hooks → result back to the LLM. Loop.
6. Else yield `Done`.

Per-turn cap: `max_turns=6` for the main agent, `max_turns=8` for the
dream agent. Prevents tool-call ping-pong loops.

## System prompt composition

`Agent._compose_system_prompt()` stitches together:

1. The base prompt ("You are eVi, …").
2. `## Memory index` — one-line summaries of every entry in
   `~/.evi/memory/`. Pulled live so dreaming + manual edits show up.
3. `## Available skills` — name + description of every
   `~/.evi/skills/<name>/SKILL.md`. The body is loaded on demand via the
   `invoke_skill` tool.
4. `## Project context (<path>)` — the `EVI.md` / `AGENTS.md` files from
   the working folder up to the git top level (outside git, up to the
   filesystem root), one per folder, outermost first. A file above the
   repository never applies inside it. These load whether or not the folder
   is trusted: they are instructions, not settings.

Refreshed on `Agent.__init__` and `Agent.reset()`. Not refreshed mid-turn
— if you edit memory while a session is running, the new state appears
on the next turn.

## Tool framework

```python
@tool(description="Read a UTF-8 text file.", category="fs")
def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")
```

The decorator inspects the function's type hints and docstring, builds an
OpenAI-shaped JSON schema, and registers a `Tool` dataclass in
`REGISTRY`. The agent passes `[t.openai_schema() for t in tools]` to the
LLM each turn.

Tool *categories* govern:
- Whether the tool is enabled (`tools.<category>` in `config.toml`)
- Whether tool calls auto-approve (`auto.auto_approve` list)

New tool? Decorate a function with `@tool(category="…")`, import the
module from `evi/apps/cli/main.py` and `evi/apps/web/server.py` for the side
effect, add a config toggle if you want one.

## Permission flow

`Agent.permission_callback: (name, args_json, category) → bool`.

Before any prompt, `Agent._permission_decision` asks `evi.permissions.decide()`
for `allow` / `deny` / `ask`: hard-deny, then protected paths (a write to one
asks in every mode), then the mode, the rules (matched on the tool name or
category, per command for a shell line), secret reads, auto-approve categories
and trusted scopes. `/auto on` (`auto_all`) is passed to `decide()` as `yolo`,
so hard-deny and protected paths still apply under it. The
destructive-command guard (`evi/shell_guard.py`) then turns anything but a
`deny` into a prompt for a matching shell line. Only an `ask` reaches the
callback, invoked synchronously while the turn's calls are
gated. With no callback, an `ask` is allowed, except a protected write or a
guard hit, which is denied. Returning `False` surfaces `"PERMISSION DENIED"`
as the tool result; the model adapts on the next turn. Details:
[Permissions & sandbox](features/permissions-and-sandbox.md).

**CLI**: callback is `_cli_permission_prompt` which uses `console.input`
(`y`/`a` this call, `n`, `s` the rest of the session after a y/N
confirmation).

**Web**: callback runs on a worker thread (the agent loop is sync). It
generates a `decision_id`, pushes a `PermissionRequest` SSE event with
it, and blocks on a `threading.Event`. The browser POSTs to
`/api/decide`; the endpoint flips the event; the worker unblocks.

**Scheduler / scripted runs**: no callback set → default-allow for an `ask`
(a protected write or a destructive-guard hit is denied).

## Config layers

`Config.load()` merges three layers: `~/.evi/config.toml`, the active profile
(`~/.evi/profiles/<name>.toml`), and a repository's `.evi.toml`, found from the
working folder up to the git top level. The `.evi.toml` applies only when
`evi/trust.py` says the folder is trusted and the file is the version that was
approved; even then `evi/project.py` lets it set only model/sampling
preferences and tighten `[tools]` / `[auto]`. `Config.load()` never prompts:
`evi chat` asks before it loads the config, and every other surface runs with
an untrusted project layer off. A key no section knows is warned about, not
fatal.

`Config.save()` writes the user layer only: `config.toml` as it is on disk,
with the fields the caller changed patched in, so profile and project values
never reach it.

## Backends

`evi.backends.Backend` is the ABC. Four implementations:

| Kind            | Chat? | Model listing | Pull API |
|-----------------|-------|---------------|----------|
| `lmstudio`      | ✅    | `/v1/models`  | ❌       |
| `ollama`        | ✅    | `/api/tags` (rich) | ✅ (`/api/pull` streaming) |
| `llamacpp`      | ✅    | `/v1/models` (one loaded) | ❌ |
| `openai_compat` | ✅    | best-effort   | ❌       |

`get_backend(settings)` dispatches by `llm.backend` string. The chat
client is OpenAI-SDK across all four; model-management methods on
non-Ollama backends raise `NotImplementedError` and the CLI tells the
user to use `hf:<repo>` direct downloads instead.

## Storage layout (`~/.evi/`)

```
config.toml         primary config; profiles overlay this
profiles/*.toml     per-machine overlays selected by EVI_PROFILE / --profile
trusted_folders.json  folders / repositories whose .evi.toml applies (evi trust)
memory/             markdown notes, .attic/ holds soft-deleted
skills/<name>/      SKILL.md + skill-local assets
commands/<name>.md  user-defined slash command templates with {args}
scheduled/<id>.json one file per saved scheduled prompt
hooks.toml          before/after_tool_call hook entries
mcp.json            MCP server launch configs
images/             ComfyUI output cache
models/             huggingface_hub downloads
screenshots/        computer-use screenshots
transcripts/<day>/<session>.jsonl   session logs feeding the dream engine
logs/dreams/        per-dream audit logs
logs/scheduled/     per-task run logs
```

## Subagents

`evi.llm.subagent.run_subagent(...)` builds a scoped `Agent` with a
focused system prompt and a restricted tool category set, runs it to
completion, returns concatenated text plus a brief tool trace. Used by:

- `evi/tools/subagent.py` — `delegate_explore` (fs only) and
  `delegate_plan` (no tools) tools the main agent can call.
- `evi/dream.py` — the dream agent itself is a subagent with category
  `("memory", "fs")` and the dream system prompt.

## Lifecycles

**MCP** — CLI lazy-starts on first `_build_agent`, `atexit` cleans up.
Web starts it inside the FastAPI lifespan context.

**Scheduler** — CLI: `evi scheduler` runs as a foreground daemon. Web:
also starts in the lifespan so `evi web` covers chat + scheduled jobs in
one process.

**Managed llama.cpp runtime** — `evi web` (which the desktop sidecar runs)
relaunches it in the background at start (`evi.runtime.setup.ensure_running`).
Setup, a model swap, the GPU switch and each relaunch write the server's
window (`-c`) into `[llm] context_size`, and open web sessions pick it up on
their next turn (`Agent.refresh_context_size`). If `[llm]` has moved to
another backend, startup gives the earlier `context_size` back instead.

**CLI-agent backends** — `codex`, `gemini`, `amp`, `qwen` and `copilot` run
as child processes started by `evi.proc.spawn`, with the conversation on
stdin. On Windows each run gets its own Job Object (the CLI is created
suspended and resumed once inside it); on POSIX it leads its own session.
`kill_tree` therefore ends the whole tree when a turn times out, and any
still running when eVi exits are killed.

**Tauri desktop** — Two modes:
- Local (default): spawns `py -3.13 -m uvicorn evi.apps.web.server:app` as a
  child, polls `/api/health`, opens webview at the local port.
- Remote (`EVI_REMOTE_URL` set): skips the spawn, just navigates.

## Testing

`pytest` against the in-process `Agent` with stubbed `OpenAI` clients +
`httpx.MockTransport` for HTTP boundaries. Web tests use Starlette's
`TestClient`; permission-flow integration is unit-tested instead of
end-to-end because TestClient buffers SSE responses.
