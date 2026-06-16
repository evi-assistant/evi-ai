# Python SDK examples

Runnable samples for the eVi **Agent SDK** (`evi.sdk`). They need a local model
backend reachable per `~/.evi/config.toml` (Ollama / llama.cpp / vLLM / any
OpenAI-compatible endpoint), except where noted.

Run any of them from the repo root, e.g.:

```bash
python examples/python/quickstart.py
```

| File | Shows |
|---|---|
| [`quickstart.py`](quickstart.py) | `build_agent()` + `run_headless()` — one prompt to completion. |
| [`streaming.py`](streaming.py) | Iterate `Agent.chat()` and react to typed events (text, tool calls, route, done). |
| [`custom_tool.py`](custom_tool.py) | Define tools with `@tool` and pass them to `build_agent(tools=[...])`. |
| [`subagents.py`](subagents.py) | Fan work out with `run_subagents_parallel()`. |
| [`structured_output.py`](structured_output.py) | Constrain output to a JSON Schema via `as_response_format()`. |
| [`headless_ci.py`](headless_ci.py) | Machine-readable JSON result + exit code for a pipeline step. |

Full guide: [../../docs/sdk.md](../../docs/sdk.md).
