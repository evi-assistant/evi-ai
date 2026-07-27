# Build an agent with the SDK

Construct a fully-wired eVi agent in a few lines of Python, run a prompt to
completion, then give it a tool of your own.

> **Uses:** Python SDK (`evi.sdk`) · **You'll need:** eVi installed
> (`pip install evi-assistant`) and a local model backend reachable per
> `~/.evi/config.toml` — see [Run a model on your own hardware](run-a-local-model.md).

## The smallest useful agent

`build_agent()` reads your `~/.evi/config.toml` and wires everything the CLI
would: the enabled tools, memory, skills, project context, hooks, and
guardrails. `run_headless()` drives one prompt to completion and hands back the
final text plus token usage — no streaming loop to manage.

```python
from evi.sdk import build_agent, run_headless

agent = build_agent()
result = run_headless(agent, "In one sentence, what is eVi?")
print(result.text)
```

Runnable version: [`examples/python/quickstart.py`](../examples/python/quickstart.py)

```bash
python examples/python/quickstart.py
```

## Give it a custom tool

The `@tool` decorator turns a plain function into a tool. It reads the type
hints to build the JSON schema and the first docstring line as the description —
you write an ordinary function and hand it to `build_agent(tools=[...])`.

```python
from evi.sdk import build_agent, run_headless, tool

@tool(category="math", description="Add two integers and return the sum")
def add(a: int, b: int) -> int:
    return a + b

agent = build_agent(tools=[add])
print(run_headless(agent, "What is 12 + 30?").text)
```

Passing `tools=[...]` replaces the default toolset entirely — the agent sees
only what you list. To *add* to the built-ins instead, select categories with
`build_agent(tool_categories=[...])`.

Runnable version: [`examples/python/custom_tool.py`](../examples/python/custom_tool.py)

## Streaming instead of run-to-completion

When you want tokens as they arrive — for a UI, or to react to tool calls —
iterate `agent.chat(...)` and match on the event types instead of calling
`run_headless`:

```python
from evi.sdk import build_agent, TextDelta, ToolCall, Done

agent = build_agent()
for event in agent.chat("Summarise the README in three bullets."):
    if isinstance(event, TextDelta):
        print(event.text, end="", flush=True)
    elif isinstance(event, ToolCall):
        print(f"\n[calling {event.name}]")
    elif isinstance(event, Done):
        break
```

Runnable version with the full event set:
[`examples/python/streaming.py`](../examples/python/streaming.py)

## More SDK recipes

The [`examples/python/`](../examples/python/) directory also has runnable
scripts for **structured output** (`structured_output.py`), **subagents**
(`subagents.py`), and **headless / CI** use (`headless_ci.py`). The complete API
reference is [`docs/sdk.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/sdk.md).

## Where to go next

- [Review a pull request in CI](headless-ci-review.md) — put an agent in a
  GitHub Action.
- [Guard an agent's inputs and outputs](guardrails.md) — add safety layers.
