# Structured Outputs & Batch

## Overview

Two related features let you treat eVi like a programmable text-to-data tool instead of just a chat assistant:

- **Structured Outputs** constrain a single turn's reply to a **JSON Schema** you supply. Where eVi's `/json` just forces the model to return *some* JSON object, Structured Outputs force a *specific shape* — named fields, types, required keys. This is what you want for extraction, classification, and any case where a script downstream needs to parse the answer reliably.
- **Batch** is the local analog of a cloud "Batch API": feed in a file of many prompts and eVi runs each one through its own headless agent — optionally several in parallel — emitting one JSON result per line. Good for evals, bulk extraction, translation runs, and other unattended jobs.

The two compose: each batch item can carry its own schema, so you can run hundreds of schema-constrained extractions in one command.

## How it works

### Structured Outputs

A JSON Schema is wrapped into the OpenAI-style `response_format` object that eVi's agent already forwards to the backend:

```json
{
  "type": "json_schema",
  "json_schema": { "name": "output", "schema": { ... }, "strict": true }
}
```

The wrapping is done in `evi/structured.py`:

- `load_schema(spec)` accepts **either** a file path **or** an inline JSON string (it decides by checking whether the trimmed text starts with `{`). It returns the parsed schema dict, raising `SchemaError` if the file is missing, the JSON is invalid, or the top level isn't an object.
- `as_response_format(schema, name="output", strict=True)` wraps a bare schema into the `json_schema` envelope shown above. It is smart about **not double-wrapping**: if you already passed a full `{"type": "json_schema", ...}` object, or a `{"name", "schema"}` pair, it respects that as-is.

That `response_format` is passed through `agent.chat(...)` and only attached to the backend request when non-`None` (see `evi/llm/agent.py`). Backends that support schema-constrained decoding (OpenAI, LM Studio, recent llama.cpp / Ollama) honor it; backends that don't simply ignore it and fall back to best-effort JSON — eVi does not crash or hard-fail on an unsupporting backend.

### Batch

`evi/batch.py` is a small, model-agnostic engine:

- `parse_batch_file(path)` reads the input and normalizes it to a list of `{"id", "prompt", ...}` dicts. It chooses a parser by file extension:
  - `.jsonl` / `.ndjson` — one JSON object per line; blank lines skipped.
  - `.json` — a single JSON array of objects.
  - anything else — one prompt per non-blank line, with `#` comment lines ignored.
  - Every item without an explicit `id` is assigned its list index, so outputs can be correlated back to inputs.
  - A missing file or a malformed/empty-`prompt` item raises `BatchError`.
- `run_batch(items, run_one, parallel=1)` runs each item through the supplied `run_one` callable, preserving input order. With `parallel > 1` it uses a `ThreadPoolExecutor` (capped at the number of items). Crucially, **a failure in one item never aborts the batch** — per-item exceptions are caught and recorded as `{"id", "prompt", "error": "<Type>: <message>"}`.
- `to_jsonl(results)` renders the result list as JSONL (one compact JSON object per line, `ensure_ascii=False`).

The CLI's `batch` command supplies a `run_one` that builds a fresh agent per item, applies the item's `mode`/`schema` (falling back to the command flags), and runs it through the headless engine.

### Headless engine (shared substrate)

Both `evi run` and `evi batch` drain a single agent turn-loop via `run_headless(agent, prompt, max_turns=12, response_format=None)` in `evi/headless.py`. It collects streamed text, tool results (truncated to 2000 chars each), usage stats, and any error into a `HeadlessResult`, and forwards `response_format` to the backend when given. `to_json(res)` serializes it to `{"text", "tools", "usage", "error"}`.

## Setup

There is **no dedicated config section** for Structured Outputs or Batch — they are stateless features driven entirely by command flags, slash-command arguments, and request fields. They reuse your existing model/backend configuration.

- **Config file** — these features run against whatever backend and model you've configured in `~/.evi/config.toml` (the same `[model]` / backend settings the rest of eVi uses). No keys need to be added to enable Structured Outputs or Batch.
- **Pip extras** — none required. Both `evi/structured.py` and `evi/batch.py` are pure standard library (`json`, `pathlib`, `concurrent.futures`).
- **Defaults**:
  - `as_response_format`: `name="output"`, `strict=true`.
  - `run_headless`: `max_turns=12`.
  - `evi run`: `--format text`, no schema.
  - `evi batch`: `--parallel 1` (sequential), output to **stdout** unless `--out` is given.
- **Backend support** — schema enforcement depends on the backend. Point eVi at a backend that supports OpenAI-style `response_format` (OpenAI, LM Studio, recent llama.cpp / Ollama) for hard schema guarantees; others fall back to best-effort JSON.

## Usage

### REPL slash command

Inside the interactive REPL:

- `/schema <file|inline-json> [prompt]` — constrain the **next** turn to a JSON Schema.
  - With a trailing prompt, it constrains that turn immediately.
  - With no prompt, it "arms" the schema for your next message (`schema armed for your next message`).
  - `/schema off` clears an armed schema (`schema cleared`).
- `/json <prompt>` — the lighter sibling: forces a JSON **object** (`{"type": "json_object"}`) without a specific shape.

(Listed in REPL help as `/schema <file> [prompt]` — "constrain the next turn to a JSON Schema".)

### CLI: single headless run

```
evi run "<prompt>" --schema <file|inline-json> [--format text|json] [--mode chat|cowork|code] [--yes]
```

- `--schema` accepts a file path or inline JSON. A bad schema prints `error: ...` to stderr and exits with code **2**.
- `--format json` prints the full `{text, tools, usage, error}` envelope; `text` (default) prints just the final answer.
- If `prompt` is omitted, eVi reads it from stdin.
- Without `--yes`, tool calls that aren't already auto-approved are **denied** (so a scripted run never blocks waiting for a permission prompt).

### CLI: batch

```
evi batch <input_file> [--out results.jsonl] [--parallel N] [--mode <preset>] [--yes]
```

- `<input_file>` — `.jsonl` / `.json`, or a plain one-prompt-per-line file.
- `--out` / `-o` — write JSONL results to a file; omit to print to stdout. When writing to a file, eVi prints a summary like `wrote 12 results (11 ok) -> results.jsonl`.
- `--parallel` / `-j` — run N items concurrently.
- `--mode` / `-m` — default tool preset for items that don't specify one.
- `--yes` / `-y` — auto-approve tool calls (otherwise non-approved tools are denied).
- Per-item `mode` and `schema` fields in the input file **override** the command-line flags.

### Web UI / API

The web chat endpoint accepts an optional `output_schema` field:

```
POST /api/chat
{ "session_id": "...", "message": "...", "output_schema": { ...JSON Schema... } }
```

- `output_schema` may be a JSON Schema **object** or an inline-JSON **string** (a string is run through `load_schema`).
- An invalid schema returns **HTTP 400** with `bad schema: <reason>`.
- The reply streams back over SSE exactly like a normal turn, but the model's output is constrained to the schema.

## Examples

### Example 1 — Extract structured contact data with `evi run`

Create a schema file `contact.json`:

```json
{
  "type": "object",
  "properties": {
    "name":  { "type": "string" },
    "email": { "type": "string" },
    "title": { "type": "string" }
  },
  "required": ["name", "email"],
  "additionalProperties": false
}
```

Run a one-shot extraction and get the parsed JSON envelope:

```bash
echo "Jane Doe, VP Engineering, jane@acme.io" | \
  evi run "Extract the contact fields from this signature." \
  --schema contact.json --format json
```

The `text` field of the result will contain a JSON object matching the schema, e.g.:

```json
{"text":"{\"name\":\"Jane Doe\",\"email\":\"jane@acme.io\",\"title\":\"VP Engineering\"}","tools":[],"usage":{"prompt":120,"completion":24,"total":144},"error":null}
```

You can also pass the schema inline instead of a file:

```bash
evi run "Classify the sentiment." \
  --schema '{"type":"object","properties":{"sentiment":{"type":"string","enum":["pos","neg","neu"]}},"required":["sentiment"]}' \
  "I absolutely love this product"
```

### Example 2 — Batch extraction with per-item schemas

Input file `jobs.jsonl` — each line is its own job, and the first two pin a schema (the schema path is resolved per item, so `contact.json` from Example 1 is reused):

```jsonl
{"id": "a1", "prompt": "Extract contacts from: Bob Lee, CTO, bob@x.io", "schema": "contact.json"}
{"id": "a2", "prompt": "Extract contacts from: Amy Ng, amy@y.io", "schema": "contact.json"}
{"id": "a3", "prompt": "Summarize in one sentence: the quick brown fox..."}
```

Run all three, four at a time, writing results to a file:

```bash
evi batch jobs.jsonl --out out.jsonl --parallel 4 --yes
```

eVi prints a summary (`wrote 3 results (3 ok) -> out.jsonl`) and `out.jsonl` contains one JSON object per line, keyed by your `id`:

```jsonl
{"id": "a1", "prompt": "Extract contacts from: Bob Lee, CTO, bob@x.io", "text": "{\"name\":\"Bob Lee\",\"email\":\"bob@x.io\",\"title\":\"CTO\"}", "error": null, "usage": {"prompt": 90, "completion": 22, "total": 112}}
{"id": "a2", "prompt": "Extract contacts from: Amy Ng, amy@y.io", "text": "{\"name\":\"Amy Ng\",\"email\":\"amy@y.io\"}", "error": null, "usage": {"prompt": 84, "completion": 18, "total": 102}}
{"id": "a3", "prompt": "Summarize in one sentence: the quick brown fox...", "text": "A fast brown fox leaps over a lazy dog.", "error": null, "usage": {"prompt": 60, "completion": 12, "total": 72}}
```

A plain one-prompt-per-line file works too (no schema, no ids needed — eVi assigns indexes):

```text
# translation batch — one prompt per line, blank lines and # comments ignored
Translate to French: Good morning
Translate to French: See you tomorrow
```

```bash
evi batch prompts.txt -j 2
```

## Notes / limits

- **Schema enforcement is backend-dependent.** Only backends that support OpenAI-style `response_format` actually *guarantee* the shape. Others ignore it and return best-effort JSON — design your downstream parsing to tolerate the occasional non-conforming reply when running against such a backend.
- **No double-wrapping.** You can pass a bare schema, a `{"name","schema"}` pair, or a full `{"type":"json_schema",...}` envelope; `as_response_format` detects and preserves the richer forms. To control the `strict` flag or schema `name`, pass the full envelope yourself.
- **`/schema` vs `/json`.** `/json` forces an unstructured JSON object; `/schema` forces a specific schema. Use `/schema off` to clear an armed schema between turns.
- **Batch is fail-open per item.** One bad prompt or one item-level exception never aborts the run — the failing item gets an `error` string and the rest proceed. Always inspect each output line's `error` field; a present `error` means that item failed even though the batch "succeeded".
- **Unattended permission policy.** In both `evi run` and `evi batch`, without `--yes` any tool call not already on your auto-approve list is **denied** rather than prompting (a scripted run can't answer a prompt). Pass `--yes` to auto-approve all tool calls for genuinely unattended jobs — and only do so when you trust the prompts, since it removes the human-in-the-loop guard.
- **Parallelism caveats.** `--parallel` uses threads; the effective worker count is capped at the number of items. Higher concurrency can hit backend rate limits or local-model memory limits — tune `N` to your hardware/backend.
- **Tool output truncation.** Headless results truncate each tool's captured output to 2000 characters, and `max_turns` defaults to 12 — long multi-tool reasoning chains may be cut short in headless/batch mode.
- **Input validation.** `evi run`/`evi batch` exit with code **2** on a bad schema or unparseable/empty batch input; an empty batch (no prompts) exits **1**. A headless `run` whose turn errored (text mode) exits **1** with the error on stderr.
- **Privacy.** Like the rest of eVi, these run locally against your configured backend; no data leaves your machine beyond what your chosen backend itself sends.
