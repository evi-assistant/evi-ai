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
    model=None,                  # a different model id on the same backend
    backend=None,                # a registered backend (entry or name) for this agent
    client=None,                 # a pre-built OpenAI-compatible client
    tools=None,                  # explicit tools (Tool objects OR @tool fns)
    tool_categories=None,        # select built-ins by category, e.g. ["fs","code"]
    enable_memory=None,          # None = follow config toggle; True/False forces
    enable_skills=None,          # None = follow config toggle
    enable_project=True,         # load EVI.md / project context
    enable_hooks=True,           # load ~/.evi/hooks.toml
    enable_guardrails=True,      # load ~/.evi/guardrails.toml (if enabled)
    memory_root=None,            # MemoryStore folder; None = ~/.evi/memory (or user_root/memory)
    user_root=None,              # a multi-user login's data folder (~/.evi/users/<name>)
    permission_callback=None,    # (tool_name, args_json, category) -> bool; None = nobody to ask
    permission_batch_callback=None,  # [(tool_name, args_json, category), …] -> [bool, …]
    transcripts=None,            # a TranscriptStore to log turns to
    session_id=None,
    frontend="cli",              # "cli" | "web": the surface the agent serves
    unattended_policy=None,      # "deny" | "allow" with no callback; None = [auto] unattended_policy
) -> Agent
```

Tool selection precedence:

1. `tools=[...]` — use exactly these (a `Tool`, or a `@tool`-decorated function,
   which the SDK resolves from the registry). Pass `tools=[]` for a no-tool agent.
2. `tool_categories=[...]` — select built-ins by category, ignoring config toggles.
3. neither — select built-ins per the `[tools]` toggles in your config.

Per-user data:

- `memory_root` is the folder for the agent's `MemoryStore` (its system
  prompt's memory index) when memory is on. `None` means the shared
  `~/.evi/memory`, unless `user_root` is set.
- `user_root` is a multi-user web login's data folder, which the web server
  passes for each login's chats. The memory tools then keep that user's
  notes in `user_root/memory`, and `memory_root` defaults to that folder too.
  Tools see it as `ctx.user_root` (see [Custom tools](#custom-tools)).
- `user_root=None` means: the user of the tool call that is building this
  agent, if there is one, so a subagent works for its parent's user.
  Otherwise single-user. An agent built on a thread you start yourself
  without `contextvars.copy_context()` sees no tool call, so pass `user_root`
  explicitly there.

> The CLI's own agent builder delegates to `build_agent` — it adds only its
> runtime concerns (spawning MCP servers, interactive permission prompts). There
> is one source of truth for how an Agent is assembled.

### Permissions: an agent with no callback fails closed

An agent built without `permission_callback` or `permission_batch_callback`
has nobody to ask, so it is **unattended**. Calls that are pre-approved still
run: the `[auto] auto_approve` categories (by default `fs`, `code`, `memory`,
`skills` and `image`), your `allow` rules and trusted dirs and domains. Any
other call that would ask is **not run**, and the model gets `PERMISSION
DENIED: <tool>(<args>) was not run. This run is unattended (nobody can approve
a tool call), …`. Your own `@tool`s are in categories nobody auto-approved, so
they are denied too until you choose one of these:

- **Decide each call yourself**: pass `permission_callback=` (a function of
  `tool_name, args_json, category` that returns `True` to run the call), or
  `permission_batch_callback=` for one answer list per batch of calls.
- **Run what would ask**: pass `unattended_policy="allow"`. Use it for an agent
  whose tool list you control (as in [Custom tools](#custom-tools)); it lets
  every tool the agent has run without asking.
- **Pre-approve a category** in `~/.evi/config.toml`, e.g. add `"math"` to
  `[auto] auto_approve`. This applies to every eVi session, not just your
  program.

`unattended_policy=None` (the default) follows `[auto] unattended_policy`,
which is `"deny"` unless the user set `"allow"`. Whatever the policy, an
unattended agent never writes a protected path, never reads a protected secret
(`.env`, a key) that no explicit `allow` rule covers, and never runs a command
the destructive-command guard flags. `run_headless` reports how the run settled
such calls in `HeadlessResult.policy`: `"deny"`, `"allow"`, `"allow-all"`
(after `agent.enable_auto_all()`) or `"interactive"` (a callback decides),
naming an `[auto] mode` that lets calls through (`"allow-all (mode=yolo)"`).
See [Unattended runs](features/permissions-and-sandbox.md#unattended-runs).

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
`HeadlessResult(text, tools, usage, error, policy)`.

## Custom tools

```python
from evi.sdk import tool, build_agent, run_headless

@tool(category="math", description="Add two integers")
def add(a: int, b: int) -> int:
    return a + b

# Nobody is asked about tool calls here, and "math" isn't an auto-approved
# category, so say that this agent's calls may run. Its only tool is `add`.
agent = build_agent(tools=[add], unattended_policy="allow")
print(run_headless(agent, "What is 19 + 23?").text)
```

Without `unattended_policy="allow"` (or a `permission_callback`, see
[Permissions](#permissions-an-agent-with-no-callback-fails-closed)) the call is
denied and the model answers without the tool.

`@tool` reads the function's type hints to build the JSON schema and the first
docstring line as the description (override with `description=`). A tool may
return a `str`, a JSON-able object, or a `ToolOutput(text, citations)` to attach
source citations. Use `register_builtin_tools()` if you need to populate the full
built-in `REGISTRY` yourself.

`@tool(trust="untrusted")` marks a tool whose output is text an outsider wrote
(a web page, another server); the default is `"trusted"`, and any other value
raises `ValueError`. It is a label for now: nothing treats the two
differently yet.

### The calling session: `ctx`

A tool that needs to know which session called it declares a `ctx`
parameter. The agent passes it the call's `ToolContext`. `ctx` never appears
in the schema, so the model can't see it, and a `ctx` argument the model
sends is replaced by the real context:

```python
from evi.sdk import tool, ToolContext

@tool(category="custom", description="Where this session works")
def where(ctx: ToolContext | None = None) -> str:
    return ctx.cwd if ctx and ctx.cwd else "."
```

Give `ctx` the default `None`: a call made outside an agent (`Tool.call` with
no context, `evi mcp serve`) passes `None`. The fields:

| Field | Holds |
|---|---|
| `agent` | The `Agent` running the call (`None` outside one). |
| `session_id` | That agent's session id. |
| `cwd` | The session's working folder as `Agent.cwd` holds it; `""` means the process's current folder. |
| `turn_index` | The agent's user-turn count, 1 for its first message. A revived or resumed session counts on from its history. |
| `user_root` | A multi-user web login's data folder (`~/.evi/users/<name>`); `None` in single-user mode. |
| `cancel_token` | A `threading.Event` a long tool can poll. Nothing sets it yet. |
| `parent` | The context of the tool call whose tool built this agent (a subagent); `None` at the top level. |
| `root` | (property) The top-level session's context, at the end of the `parent` chain. |

The name `ctx` is reserved. A tool that took a model argument called `ctx`
now gets the `ToolContext` in its place, so rename that argument. Only a
parameter named `ctx` counts (not `**kwargs`), and MCP tools are exempt:
their arguments all go to the server.

### Per-session state: `binder`

A tool whose state must belong to one session, not to the process, gets a
`binder`: a function that takes the `ToolContext` and returns the function
that session runs. Build the `Tool` directly (`@tool` takes no binder):

```python
import json
from evi.sdk import Tool, ToolContext, build_agent

def note_binder(ctx: ToolContext):
    notes: list[str] = []          # this session's own list

    def note(text: str) -> str:
        notes.append(text)
        return json.dumps(notes)

    return note

note_tool = Tool(
    name="note",
    description="Keep a note for this session",
    parameters={"type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"]},
    func=lambda text: "unbound",   # unused: calls run the binder's function
    category="custom",
    binder=note_binder,
)
agent = build_agent(tools=[note_tool], unattended_policy="allow")
```

- An agent binds the tool at its first call. `Tool.bind(ctx)` returns a copy
  that runs the binder's function, keeps the name, description and schema,
  and points back at the original (`bound_from`). The tool list the model
  sees doesn't change.
- The agent reuses that binding until its `session_id` changes or `reset()`
  starts a new conversation. The next call then binds again.
- The binder sees the context as it was at that first call, so `turn_index`
  and `cwd` don't move afterwards: read live values from `ctx.agent`.
- A binder may run more than once for one session (two first calls racing;
  one result is kept), so it should only build the function, with no other
  side effects.
- If the binder raises, that call returns `ERROR: <type>: <message>`. The
  agent and its other tools are unaffected.
- Two agents given the same tool, or one agent's bound copy, never share
  state. Called outside an agent, the tool is bound for that one call.

## Subagents

```python
from evi.sdk import run_subagents_parallel

results = run_subagents_parallel(
    ["Summarise JWTs", "Summarise UUIDs"],
    system_prompt="You are terse.",
    tool_categories=(),       # tool categories (or names) per subagent; () = no tools
    max_workers=4,
)
for task, answer in results:   # input order preserved
    print(task, "->", answer)
```

`run_subagent(...)` is the single-task form. Subagent *profiles* (named
role + tool presets, extendable by plugins) are available via
`SUBAGENT_PROFILES`, `all_profiles()`, and `get_profile(name)`.

What a subagent may use:

- `tool_categories=` takes categories or tool names, and the subagent gets
  only the matching tools your `[tools]` toggles enable (safe mode applied). A
  category with no toggle, such as your own `@tool(category="math")`, counts as
  enabled.
- `tools=` is an exact list of registered tool names instead, whatever the
  toggles say; only safe mode still drops memory, skills and MCP tools.
- `memory=` (single-task form) is the `MemoryStore` its memory tools use.
  Without it they use the login's `user_root/memory` in multi-user mode, else
  `~/.evi/memory`.

How it is asked:

- Called from inside a tool of an agent that has a permission callback, a
  subagent asks through that callback, one prompt at a time however many run
  at once, and inherits the agent's `enable_auto_all()`.
- Called from your own code, it has nobody to ask: a call that would ask
  follows `unattended_policy=` (`"deny"` | `"allow"`; `None` takes the spawning
  agent's, else `[auto] unattended_policy`). Pass `"allow"` only with a
  read-only `tools=` list. A delegate CLI-agent backend (`codex`, `gemini`,
  `amp`, `qwen`, `copilot`) is then refused, and the result is `ERROR:
  subagent failed: …`, unless `[llm] allow_cli_agent_unattended = true`.

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
print(to_json(res))            # {"text":..., "tools":[...], "usage":{...}, "error":..., "policy":"deny"}
raise SystemExit(1 if res.error else 0)
```

Nothing prompts in CI: `fs` is auto-approved by default, so its calls run, and
a call outside `[auto] auto_approve` is denied (`"policy": "deny"`). See
[`headless_ci.py`](../examples/python/headless_ci.py).

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

run_one = make_runner(lambda sp, model: build_agent(system_prompt=sp, model=model))
result = run_ultracode("design a retry policy", run_one=run_one, cfg=UltraConfig(breadth=3))
print(result.answer)
```

`make_runner` calls the factory with the stage's system prompt and model id
(`None` for the default). Every stage runs unattended under `[auto]
unattended_policy`, and the solvers get only the read-only file tools of
`UltraConfig.mode` (`read_file`, `list_dir`, `find_files`, `search_files`), so
the pipeline returns text and never edits files.

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
