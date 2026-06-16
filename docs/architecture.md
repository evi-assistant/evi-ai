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
4. `## Project context (<path>)` — full body of the nearest `EVI.md`
   walking up from cwd.

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

If the tool's category is in `auto.auto_approve` (or `auto_all` is set
for the session), the callback is skipped. Otherwise it's invoked
synchronously inside `_invoke_tool`. Returning `False` surfaces
`"PERMISSION DENIED"` as the tool result; the model adapts on the next
turn.

**CLI**: callback is `_cli_permission_prompt` which uses `console.input`.

**Web**: callback runs on a worker thread (the agent loop is sync). It
generates a `decision_id`, pushes a `PermissionRequest` SSE event with
it, and blocks on a `threading.Event`. The browser POSTs to
`/api/decide`; the endpoint flips the event; the worker unblocks.

**Scheduler / scripted runs**: no callback set → default-allow.

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

**Tauri desktop** — Two modes:
- Local (default): spawns `py -3.13 -m uvicorn evi.apps.web.server:app` as a
  child, polls `/api/health`, opens webview at the local port.
- Remote (`EVI_REMOTE_URL` set): skips the spawn, just navigates.

## Testing

`pytest` against the in-process `Agent` with stubbed `OpenAI` clients +
`httpx.MockTransport` for HTTP boundaries. Web tests use Starlette's
`TestClient`; permission-flow integration is unit-tested instead of
end-to-end because TestClient buffers SSE responses.
