# Agent SDK (`evi.sdk`)

eVi is a **library first** and a CLI/app second. The same primitives the CLI and
the web app are built on are importable under one stable namespace —
`evi.sdk` — so you can embed an eVi agent in your own Python program, build a
custom tool, fan work out to subagents, get schema-valid JSON, or run a turn
headless in CI.

> **Why a namespace re-export?** The internal module layout (`evi.llm`,
> `evi.tools`, `evi.mcp`, …) can move between releases. `evi.sdk` is the curated,
> supported surface — import from here and your code keeps working.

This page is the reference. For runnable code see
[`examples/python/`](../examples/python/) and its
[README](../examples/python/README.md). For *which OpenAI/vendor model features*
the core supports, see [sdk-coverage.md](sdk-coverage.md) (a different axis — that
doc is about the wire API, this one is about the Python surface).

## Install

eVi installs as a normal Python package; the SDK ships with it (`py.typed`, so
your type checker sees the annotations):

```bash
pip install -e .            # from a checkout
# or your normal eVi install — the SDK is in-tree, nothing extra to add
```

You need a model backend reachable per `~/.evi/config.toml` (Ollama, llama.cpp,
vLLM, or any OpenAI-compatible endpoint). `build_agent()` reads that config.

## Quick start

```python
from evi.sdk import build_agent, run_headless

agent = build_agent()                       # batteries: tools + memory + skills + hooks
print(run_headless(agent, "What is eVi?").text)
```

## `build_agent` — the convenience constructor

`build_agent` assembles an [`Agent`](#agent) from your config with sensible
defaults. Every argument is keyword-only:

```python
build_agent(
    *,
    config=None,                 # a Config; defaults to Config.load()
    system_prompt=None,          # override the base system prompt
    client=None,                 # a pre-built OpenAI-compatible client
    tools=None,                  # explicit tools (Tool objects OR @tool fns)
    tool_categories=None,        # select built-ins by category, e.g. ["fs","code"]
    enable_memory=None,          # None = follow config toggle; True/False forces
    enable_skills=None,          # None = follow config toggle
    enable_project=True,         # load EVI.md / project context
    enable_hooks=True,           # load ~/.evi/hooks.toml
    enable_guardrails=True,      # load ~/.evi/guardrails.toml (if enabled)
    permission_callback=None,    # tool-permission prompt; None = non-interactive
    permission_batch_callback=None,
    transcripts=None,            # a TranscriptStore to log turns to
    session_id=None,
) -> Agent
```

Tool selection precedence:

1. `tools=[...]` — use exactly these (a `Tool`, or a `@tool`-decorated function,
   which the SDK resolves from the registry). Pass `tools=[]` for a no-tool agent.
2. `tool_categories=[...]` — select built-ins by category, ignoring config toggles.
3. neither — select built-ins per the `[tools]` toggles in your config.

> The CLI's own agent builder delegates to `build_agent` — it adds only its
> runtime concerns (spawning MCP servers, interactive permission prompts). There
> is one source of truth for how an Agent is assembled.

## Streaming events

`Agent.chat(prompt)` is a generator of typed events. Match on the ones you care
about; ignore the rest.

```python
from evi.sdk import build_agent, TextDelta, ToolCall, ToolResult, RouteInfo, Done, Error

for ev in build_agent().chat("List the Python files here"):
    if isinstance(ev, RouteInfo):   print(f"[{ev.route} -> {ev.model}]")
    elif isinstance(ev, TextDelta): print(ev.text, end="")
    elif isinstance(ev, ToolCall):  print(f"\n[call] {ev.name}")
    elif isinstance(ev, ToolResult):print(f"[result] {ev.name}")
    elif isinstance(ev, Error):     print(ev.message); break
    elif isinstance(ev, Done):      break
```

Event types (all exported from `evi.sdk`):

| Event | Fields | Meaning |
|---|---|---|
| `TextDelta` | `text` | A chunk of assistant text. |
| `ThinkingDelta` | `text` | Reasoning chunk (models that emit `<think>`). |
| `ToolCall` | `name`, `arguments` | The model asked to call a tool. |
| `ToolResult` | `name`, `output`, `citations` | A tool finished. |
| `ToolProgress` | `names`, `elapsed` | A slow (`long=True`) tool is still running. |
| `UsageStats` | `prompt_tokens`, `completion_tokens`, `total_tokens` | Real token counts. |
| `LogProbs` | `tokens`, `avg_logprob`, … | Per-token confidence (if enabled). |
| `Guardrail` | `direction`, `blocked`, `message`, … | A guardrail fired. |
| `RouteInfo` | `route`, `model` | The per-turn routing decision. |
| `Done` | `reason` | Turn complete. |
| `Error` | `message` | Turn failed. |

For a non-streaming convenience, use `run_headless(agent, prompt)` → a
`HeadlessResult(text, tools, usage, error)`.

## Custom tools

```python
from evi.sdk import tool, build_agent, run_headless

@tool(category="math", description="Add two integers")
def add(a: int, b: int) -> int:
    return a + b

agent = build_agent(tools=[add])
print(run_headless(agent, "What is 19 + 23?").text)
```

`@tool` reads the function's type hints to build the JSON schema and the first
docstring line as the description (override with `description=`). A tool may
return a `str`, a JSON-able object, or a `ToolOutput(text, citations)` to attach
source citations. Use `register_builtin_tools()` if you need to populate the full
built-in `REGISTRY` yourself.

## Subagents

```python
from evi.sdk import run_subagents_parallel

results = run_subagents_parallel(
    ["Summarise JWTs", "Summarise UUIDs"],
    system_prompt="You are terse.",
    tool_categories=(),       # grant tool categories per subagent
    max_workers=4,
)
for task, answer in results:   # input order preserved
    print(task, "->", answer)
```

`run_subagent(...)` is the single-task form. Subagent *profiles* (named
role + tool presets, extendable by plugins) are available via
`SUBAGENT_PROFILES`, `all_profiles()`, and `get_profile(name)`.

## Structured output (JSON Schema)

```python
import json
from evi.sdk import as_response_format, build_agent, run_headless

schema = {"type": "object",
          "properties": {"sentiment": {"type": "string"}},
          "required": ["sentiment"], "additionalProperties": False}

agent = build_agent(tools=[])
rf = as_response_format(schema, name="verdict")
out = run_headless(agent, "Classify: 'I love this'.", response_format=rf)
print(json.loads(out.text))    # validates against schema
```

`load_schema(spec)` loads a schema from a file path, inline JSON, or a named
preset; `as_response_format(...)` wraps a plain schema into the
`response_format` shape the backend expects.

## Headless / CI

```python
from evi.sdk import build_agent, run_headless, to_json

res = run_headless(build_agent(tool_categories=["fs"]), "Does a README exist?")
print(to_json(res))            # {"text":..., "tools":[...], "usage":{...}, "error":...}
raise SystemExit(1 if res.error else 0)
```

See [`headless_ci.py`](../examples/python/headless_ci.py).

## Sessions & checkpoints

```python
from evi.sdk import list_sessions, history_from_transcript, export_markdown
from evi.sdk import list_checkpoints, rewind

for s in list_sessions(limit=5):
    print(s.session_id, s.when, s.summary)

# undo file writes an agent made this session
for path, restored in rewind():
    print("restored", path)
```

## MCP

`MCPManager` is the client side — connect to MCP servers from `mcp.json` and
expose their tools to an agent. (To publish *eVi itself* as an MCP server, use
`evi mcp serve`; see [features/mcp.md](features/mcp.md).)

## Orchestration: ultracode & workflows

```python
from evi.sdk import run_ultracode, make_runner, UltraConfig, build_agent

run_one = make_runner(lambda sp: build_agent(system_prompt=sp, tool_categories=["code"]))
result = run_ultracode("design a retry policy", run_one=run_one, cfg=UltraConfig(breadth=3))
print(result.answer)
```

`fan_out(fn, items, max_workers=8)` is the shared concurrency primitive (results
in input order); `run_workflow(...)` runs a declarative multi-step workflow. See
[features/ultracode.md](features/ultracode.md) and
[features/agents.md](features/agents.md).

## Telemetry

```python
from evi.sdk import otel
otel.init_telemetry()                 # honors ~/.evi/config.toml [otel]
with otel.span("my-operation"):
    ...
```

See [features/observability.md](features/observability.md).

## Stability

Everything in `evi.sdk.__all__` is the supported surface and follows the
project's semver intent. Reaching into `evi.llm.*` / `evi.tools.*` internals
directly is unsupported and may break between releases — if something you need
isn't re-exported, open an issue so it can be added to the curated surface.
