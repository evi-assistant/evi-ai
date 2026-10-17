# Get structured JSON output

Force the model to return JSON that matches a schema you supply — named fields,
types, required keys — so a script downstream can parse the reply reliably
instead of hoping the prose happens to be valid JSON.

> **Uses:** Python SDK (`evi.sdk`) + CLI (`evi run --schema`, or `/schema` in the
> REPL) · **You'll need:** eVi installed and a backend that supports
> OpenAI-style `response_format` (OpenAI, LM Studio, recent llama.cpp / Ollama).

## Schema vs. "just JSON"

eVi's `/json` forces the model to return *some* JSON object. **Structured
Outputs** go further: they constrain the reply to a *specific* JSON Schema. A
bare schema is wrapped into the OpenAI-style envelope the agent already forwards
to the backend:

```json
{ "type": "json_schema",
  "json_schema": { "name": "output", "schema": { ... }, "strict": true } }
```

Backends that support this honor it; others fall back to best-effort JSON — eVi
never hard-fails on an unsupporting backend.

## From the SDK

Two helpers do the work: `load_schema()` reads a schema from a file path *or* an
inline JSON string, and `as_response_format()` wraps it into the envelope above.
Pass the result to `run_headless(..., response_format=...)`:

```python
import json
from evi.sdk import as_response_format, build_agent, run_headless

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
    "required": ["name", "email"],
    "additionalProperties": False,
}

agent = build_agent(tools=[])              # no tools for a pure extraction task
rf = as_response_format(SCHEMA, name="contact")
result = run_headless(agent, "Extract: Jane Doe, jane@acme.io", response_format=rf)
data = json.loads(result.text)             # matches SCHEMA
print(data)
```

`as_response_format(schema, *, name="output", strict=True)` does **not**
double-wrap: if you already hand it a full `{"type": "json_schema", ...}`
envelope or a `{"name", "schema"}` pair, it respects that as-is. Use
`load_schema("contact.json")` in place of the inline dict to read from a file.

Runnable version: [`examples/python/structured_output.py`](../examples/python/structured_output.py)

```bash
python examples/python/structured_output.py
```

## From the CLI

`evi run` takes a `--schema` flag — a file path or inline JSON. The default
text format prints just the answer, which is the JSON object itself:

```bash
evi run "Extract the contact fields from this signature: Jane Doe, VP Engineering, jane@acme.io" \
  --schema contact.json
```

```json
{"name":"Jane Doe","email":"jane@acme.io","title":"VP Engineering"}
```

`--format json` prints the run report instead (`schema_version` 1): the
answer is its `text` field, next to why the run stopped (`stop_reason`), the
tool calls it made (`tool_calls`), the token `usage` summed over the run, the
session id and more. Read the answer back out with `jq`:

```bash
evi run "Extract the contact fields from this signature: Jane Doe, VP Engineering, jane@acme.io" \
  --schema contact.json --format json | jq -r .text
```

The prompt is either the argument or, when you leave the argument off,
everything on stdin; the two don't combine, so pipe the whole prompt:

```bash
{ echo "Extract the contact fields from this signature:"; cat signature.txt; } | \
  evi run --schema contact.json
```

`--schema` also accepts inline JSON directly (handy for a one-off enum
classification). A bad schema prints `error: ...` to stderr and exits **2**.
Every other exit code says why the run stopped: **0** when the model finished,
**1** on an error (the backend failed, say), **3** when it ran out of
`--max-turns`. Check it before you parse the answer. Inside the REPL,
`/schema <file|inline-json> [prompt]` constrains the next turn (with no prompt it
arms the schema for your next message); `/schema off` clears it.

## Notes

- **Enforcement is backend-dependent.** Only backends that support
  `response_format` *guarantee* the shape; others return best-effort JSON, so
  tolerate the occasional non-conforming reply against those.
- To control `strict` or the schema `name`, pass the full envelope yourself —
  `as_response_format` leaves richer forms untouched.

The full field reference, including every field of the run report and every
exit code, lives in
[`docs/features/structured-and-batch.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/structured-and-batch.md),
which also covers running many schema-constrained extractions at once with
`evi batch` (each result line carries its own run report).

## Where to go next

- [Build an agent with the SDK](agent-sdk-quickstart.md) — the `build_agent` /
  `run_headless` basics this recipe builds on.
- [Review a pull request in CI](headless-ci-review.md) — feed the run report
  into an unattended job.
