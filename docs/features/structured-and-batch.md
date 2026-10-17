# Structured Outputs & Batch

## Overview

Two related features let you treat eVi like a programmable text-to-data tool instead of just a chat assistant:

- **Structured Outputs** constrain a single turn's reply to a **JSON Schema** you supply. Where eVi's `/json` just forces the model to return *some* JSON object, Structured Outputs force a *specific shape* — named fields, types, required keys. This is what you want for extraction, classification, and any case where a script downstream needs to parse the answer reliably.
- **Batch** is the local analog of a cloud "Batch API": feed in a file of many prompts and eVi runs each one through its own headless agent — optionally several in parallel — emitting one JSON result per line. Good for evals, bulk extraction, translation runs, and other unattended jobs.

The two compose: each batch item can carry its own schema, so you can run hundreds of schema-constrained extractions in one command.

Both sit on eVi's headless engine, which `evi run` exposes for a single prompt. Every headless run produces a versioned **run report**: what the run used, what it did, why it stopped and what it changed. `evi run --format json` prints it, `--format stream-json` streams the run's events and ends with it, each `evi batch` line carries one, and the exit code of `evi run` says why the run stopped. See [The run report](#the-run-report).

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
- `run_batch(items, run_one, parallel=1)` runs each item through the supplied `run_one` callable, preserving input order. With `parallel > 1` it uses a `ThreadPoolExecutor` (capped at the number of items). Crucially, **a failure in one item never aborts the batch** — per-item exceptions are caught and recorded as `{"id", "prompt", "error": "<Type>: <message>", "stop_reason": "error"}`.
- `to_jsonl(results)` renders the result list as JSONL (one compact JSON object per line, `ensure_ascii=False`).

The CLI's `batch` command supplies a `run_one` that builds a fresh agent per item, applies the item's `mode`/`schema` (falling back to the command flags), runs it through the headless engine, and returns the item's `id` and `prompt` followed by its full run report.

### Headless engine (shared substrate)

Both `evi run` and `evi batch` drain a single agent turn-loop via `run_headless(agent, prompt, max_turns=12, response_format=None)` in `evi/headless.py`. It forwards `response_format` to the backend when given and collects the run into a `HeadlessResult`, which is the run report: `to_dict()` gives it as a dict, and `to_json(res)` as the one line of JSON `evi run --format json` prints. Its keyword options are what `evi run` builds on: `cancel_on_sigint` (Ctrl+C cancels the turn and comes back as the result), `on_event` (called with every event as it arrives, which feeds `stream-json`), `track_files` (the folder whose changes become `files_changed`) and `report_errors` (an exception during the turn becomes the report's `error` instead of propagating).

The event shapes live in `evi/events.py`: `SCHEMA` is the JSON Schema (draft 2020-12) every event of a stream validates against, and `report_schema()` is the run report's part of it.

### The run report

`evi run --format json` prints one JSON object. Every field is always present (`null` when it doesn't apply):

| Field | What it holds |
|---|---|
| `schema_version` | `1`. Adding a field keeps the version; renaming or removing one, or changing what one means, bumps it. |
| `session_id` | The session the run used. With `--resume` / `--continue`, the session it added to. |
| `transcript_path` | The session's transcript file; `null` when it wasn't logged (`[tools] transcripts` off, safe mode). |
| `backend`, `model` | The backend kind (`lmstudio`, `openai_compat`, …; with `--backend`, the saved backend's kind) and the model id. |
| `cwd` | The working folder (`--cwd`, or the current directory). |
| `trust` | That folder's [trust state](../configuration.md#trusting-a-folder): `trusted`; `changed` (trusted, but its `.evi.toml` is new or edited since); `untrusted` (its `.evi.toml`, if any, was ignored); `null` when not checked (`evi batch`). |
| `turns_used` | Model round trips the run made. |
| `max_turns` | The limit it ran with (`--max-turns`). |
| `stop_reason` | Why the run stopped, from a fixed list (see below). |
| `done_reason` | The agent's own reason for ending the turn, as it gave it: `stop`, `length`, `max_turns_reached`, `interrupted`, `guardrail_blocked`, `hook_blocked`, `checks_failed`, `stop_hook_exhausted`, … `null` when the turn ended without one (an error, a run stopped before its turn). |
| `text` | The model's answer: the text it streamed, trimmed. When the [verify gate](#checks-and-stop-hook-blocks) sent the model back (a stop-hook block, a failing check round), only what it wrote after the last of those, so a `--schema` run still gets one JSON object. Not redacted. |
| `error` | What went wrong, redacted; `null` when nothing did. |
| `tool_calls` | One entry per call, in call order: `name`; `args` (the JSON arguments, redacted, then cut to 2000 characters); `status` (`ok`, `error`, `denied` by the permission checks or the run's policy, `blocked` by a hook, or `cancelled`: stopped, or never answered); `duration_ms` (from the call until its result was reported; the calls of one batch run in parallel and report together; `null` when no result came); `output` (redacted, then cut to 2000 characters; `null` when no result came); and, on a `denied` call only, `reason`: why it was denied (the policy's reason, the guard's, an unattended refusal), redacted, its first line cut to 500 characters. |
| `denied_tools` | The tools whose calls the permission checks (or the run's [policy](permissions-and-sandbox.md#implementer-policy)) denied, each named once, in order. A call a hook blocked isn't listed. |
| `usage` | `{"prompt", "completion", "total"}` tokens, summed over every model call of the run; `null` when the backend reported none. |
| `duration_s` | How long the run took, in seconds. |
| `files_changed` | What the run changed under `cwd` (see below); `null` when it wasn't tracked or couldn't be worked out. |
| `checks` | The project checks the run ran before it could end (see [below](#checks-and-stop-hook-blocks)), in order; `[]` when none ran. |
| `checks_skipped` | Why no check ran although files changed (the folder isn't trusted); `null` when nothing was skipped. |
| `stop_blocks` | Each time a blocking stop hook sent the model back, in order (see [below](#checks-and-stop-hook-blocks)); `[]` when none did. |
| `policy` | How a call that would ask was settled: `implementer` (`--policy implementer`, which decides every call), `deny` (the default), `allow` (`[auto] unattended_policy = "allow"`) or `allow-all` (`-y`), with an `[auto] mode` that lets calls through named after it, such as `allow-all (mode=yolo)`. |
| `redactions` | What was redacted from the tool calls and the error, by kind: `{"github_token": 1}`. |
| `worktree` | What a run isolated with `--worktree` left in its git worktree (see below); `null` for a run that wasn't isolated, and in `evi batch`. |

Redaction (`[REDACTED:<kind>]`, see [Secret redaction](secret-redaction.md)) happens before anything is cut short, so no prefix of a secret survives the cut. A tool's name, arguments and output and the error are redacted; the model's answer is not, so a secret the model repeats appears in `text`.

**`files_changed`** lists `{"path", "status", "added", "deleted"}` per file: `path` relative to `cwd`, `status` `added`, `modified` or `deleted`, and `added` / `deleted` the line counts (`null` when unknown, such as for a binary file). Only `evi run` tracks it (`evi batch` items share one folder, so theirs is `null`).

- In a git work tree it compares `git status` from before and after the run, so a file that was already dirty shows only if the run changed it again. Files touched by commits the run made are included, and line counts are against the commit checked out when the run started (a new file: its line count). A new file that a `.gitignore` pattern matches is not listed. git runs with repository hooks, filters and fsmonitor off.
- Outside git, or in a folder git ignores, it compares the size and modification time of the files under `cwd`, skipping `.git`, `node_modules`, virtualenvs and caches, with line counts only for new files. Over 5,000 files, or a walk that takes over a second (a home directory, say), gives `null`.
- It is also `null` when git fails or reports more than 20,000 dirty files.
- What eVi itself writes is left out: its home folder and transcripts when they are inside `cwd`, and the file stdout or stderr is redirected to (`evi run --format stream-json … > run.ndjson`).

**`worktree`** is filled in for a run isolated with [`--worktree`](#cli-single-headless-run). It holds `{"path", "branch", "created", "base", "head", "commits", "dirty", "dirty_files", "diffstat"}`:

- `path`: the worktree's folder, which is also the report's `cwd` and where `files_changed` is tracked.
- `branch`: the branch checked out there when the run ended; `null` on a detached HEAD.
- `created`: `true` when this run made the worktree, `false` when it reused one.
- `base` and `head`: the full commit ids checked out when the run started and when it ended.
- `commits`: the commits from `base` to `head`, oldest first, as `{"sha", "subject"}` (at most 200).
- `dirty`: whether uncommitted or untracked files are left; `dirty_files` gives their `git status --porcelain` lines (at most 200). Untracked files count whatever `status.showUntrackedFiles` says.
- `diffstat`: `{"files", "insertions", "deletions"}` of the work tree against `base`: commits, uncommitted edits and new files together.

A value git couldn't give is `null`. Nothing is merged: the branch waits for you to review it (`git log main..fix-1`, `git diff main...fix-1`) and merge it. `evi.events.report_schema()` describes this object too.

#### Checks and stop-hook blocks

Before a run that changed files may end, eVi can run the project's checks (under `--policy implementer`, or with `[checks] verify = "always"` in Code mode; see [Verify before done](../configuration.md#verify-before-done--checks-in-configtoml)) and then any [blocking stop hooks](hooks.md#blocking-stop-hooks-protocol--json). A failing round or a block goes back to the model and the run goes on; each reply after one uses a `--max-turns` round trip.

- **`checks`** holds one entry per check run: `name` (`ruff`, `pytest`, `npm test`, `cargo check`, or a configured command's program), `cmd` (the command line, redacted), `exit` (its exit code; `null` when it didn't finish: timed out, stopped, couldn't start, or the policy or the permission checks refused it, and `tail` says which), `duration_ms`, `tail` (the end of its output, redacted), `round` (1 for the first time the checks ran in the turn, 2 for the next, …) and `source` (`pyproject`, `ruff.toml` or `.ruff.toml` for ruff, `pytest`, `package.json`, `Cargo.toml`, `config` for your `[checks] commands`, `project` for a trusted `.evi.toml`'s).
- **`checks_skipped`** says why no check ran in an untrusted folder, for example ``/home/you/project is not a trusted folder (`evi trust`), so no check ran: a check runs the folder's own code``.
- **`stop_blocks`** holds `{"hook", "reason", "iteration"}` for each block that sent the model back: the hook's name, its reason (redacted) and which would-be end of the turn it blocked (1, 2, …). The block that ends the run as `stop_hook_exhausted` isn't listed.

The run ends `checks_failed` (exit 5) when the checks still fail after `[checks] max_rounds` failing rounds, and `stop_hook_exhausted` (exit 6) when the hooks block once more after `[hooks] max_blocks` blocks. Whatever the format, `evi run` also prints a line on stderr for each check, each failing round, a skip and each block.

### Stop reasons and exit codes

`stop_reason` is one of these, and `evi run` exits with the code next to it in every format:

| `stop_reason` | Exit | Meaning |
|---|---|---|
| `done` | 0 | The model finished. Tool calls denied along the way don't change that: the agent carries on past a denial, so they are listed in `denied_tools` (status `denied` in `tool_calls`), and the text format notes them on stderr. A reply the backend cut at its token limit is `done` too, with `done_reason` `length`. |
| `error` | 1 | The backend failed (the turn's `Error` event), the turn raised, or the agent couldn't be built (an MCP server that won't start, a delegate CLI-agent backend refused). `error` says what happened; `text` keeps what streamed before it. |
| `max_turns` | 3 | The run used all its `--max-turns` model round trips before the model finished. |
| `step_budget`, `loop_detected` | 3 | Reserved for run limits of the task loop; no run stops this way yet. |
| `permission_denied` | 4 | Reserved for a turn that stops on a denial (its `Done` reason says so). Today the agent never stops on one, so a denied call alone never gives this. |
| `guardrail` | 4 | An input guardrail or a `user_prompt_submit` hook refused the prompt, or the backend ended the reply with `content_filter`. |
| `checks_failed` | 5 | The project checks still failed after `[checks] max_rounds` failing rounds ([Checks and stop-hook blocks](#checks-and-stop-hook-blocks)). `checks` shows what failed. |
| `stop_hook_exhausted` | 6 | A blocking stop hook kept blocking: it blocked once more after `[hooks] max_blocks` blocks had already sent the model back. `stop_blocks` lists those blocks. |
| `interrupted` | 130 | Ctrl+C or a SIGINT cancelled the run (128 + SIGINT). See [Stopping a turn](steering.md#stopping-a-turn). |

Exit code **2** is a usage error: no run happened, nothing goes to stdout, and the reason goes to stderr. That covers no prompt, an unknown `--format`, `--max-turns` below 1, `--resume` together with `--continue`, a session id that doesn't exist or no session to continue, a `--backend` name that isn't saved, a `--cwd` that isn't a directory, a bad `--schema`, `--base` without `--worktree`, a `--worktree` that can't be opened (an invalid branch name, a branch checked out elsewhere, one that differs from an existing branch only in case, a worktree another run holds locked, a folder outside git), an unknown `--policy`, `--policy` together with `--yes`, `--scope` without `--policy`, a `--scope` entry that is empty, absolute or climbs out of the folder with `..`, and an `--evidence` folder a bundle can't go in.

### stream-json: the run as events

`evi run --format stream-json` prints one JSON object per line (NDJSON) while the run goes:

1. **`RunStart`**: `session_id`, `backend`, `model`, `cwd`, `max_turns`, `policy`, and `resumed` (`true` with `--resume` / `--continue`). It comes once the agent is built, so the session id is known even if the run is killed later. A run that fails before that (a refused `--backend`, an agent that can't be built, a Ctrl+C while the prompt is read from stdin) prints only the `RunResult` line.
2. **The turn's events**, as the web chat receives them: `TextDelta`, `ThinkingDelta`, `ToolCall`, `ToolResult`, `ToolProgress`, `UsageStats`, `LogProbs`, `Guardrail`, `RouteInfo`, `Steering`, `Error` and `Done`, and from the [verify gate](#checks-and-stop-hook-blocks):
   - `CheckResult` — one check run: `name`, `cmd`, `exit` (`null` when it didn't finish or was refused), `duration_ms`, `tail`, `round`, `source`, as in the report's `checks`;
   - `ChecksSkipped` — files changed but no check ran: `reason`;
   - `ChecksFailed` — a failing round went back to the model: `round`, `max_rounds`, `failed` (the names of the failing checks);
   - `StopBlocked` — a blocking stop hook sent the model back: `hook`, `reason`, `iteration`;
   - `FilesChanged` — the paths the turn's write tools wrote (relative to the working folder when inside it), sent just before `Done`. It is for display (the web chat's footer under the reply) and never goes into the conversation.
3. **`RunResult`**: the run report's fields, always the last line, also after an error or a Ctrl+C.

Every line carries the same envelope: `schema_version` (`1`), `kind` (what happened), `seq` (1, 2, 3, … in the order sent) and `ts` (UTC, ISO 8601 with milliseconds), followed by the kind's own fields:

```json
{"schema_version": 1, "kind": "ToolCall", "seq": 3, "ts": "2026-10-16T09:30:12.345Z", "name": "read_file", "arguments": "{\"path\": \"README.md\"}"}
```

It is the same versioned envelope the web UI's SSE stream sends (`POST /api/chat`, including slash commands, and a re-roll), so one parser reads both. The web stream adds its own kinds (`PermissionRequest`, `PermissionExpired`, `SystemMessage`), and its `seq` starts again at 1 for each response. In every stream a tool call's name and arguments, a tool's name, output and citations, an `Error` or `Guardrail` message, a `CheckResult`'s `cmd` and `tail`, and a `StopBlocked` or `ChecksSkipped` reason are redacted; the agent's history keeps what the tool really returned. A new kind, or a new field on a kind, can arrive without a version bump (only renaming or removing one, or changing what one means, bumps `schema_version`), so a consumer should skip kinds and keys it doesn't know.

To check a stream against the schema in Python (`pip install jsonschema`):

```python
import json
import jsonschema
from evi import events

validator = jsonschema.Draft202012Validator(events.SCHEMA)
with open("run.ndjson", encoding="utf-8") as f:
    for line in f:
        validator.validate(json.loads(line))
```

`events.report_schema()` validates a `--format json` report the same way.

## Setup

There is **no dedicated config section** for Structured Outputs or Batch — they are stateless features driven entirely by command flags, slash-command arguments, and request fields. They reuse your existing model/backend configuration.

- **Config file** — these features run against whatever backend and model you've configured in `~/.evi/config.toml` (the same `[model]` / backend settings the rest of eVi uses). No keys need to be added to enable Structured Outputs or Batch.
- **Pip extras** — none required. Both `evi/structured.py` and `evi/batch.py` are pure standard library (`json`, `pathlib`, `concurrent.futures`).
- **Defaults**:
  - `as_response_format`: `name="output"`, `strict=true`.
  - `run_headless`: `max_turns=12`.
  - `evi run`: `--format text`, `--max-turns 12`, no schema, the current directory, a new session.
  - `evi batch`: `--parallel 1` (sequential), `--max-turns 12` per item, output to **stdout** unless `--out` is given.
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
evi run "<prompt>" [--format text|json|stream-json] [--schema <file|inline-json>]
        [--mode chat|cowork|code] [--yes] [--max-turns N] [--cwd DIR]
        [--model ID] [--backend NAME] [--resume ID | --continue]
        [--worktree BRANCH|auto [--base REF]]
        [--policy implementer [--scope GLOB]...]
        [--evidence DIR]
```

- **Prompt** — the argument, or, when it is omitted, everything on stdin. The two don't combine: with a prompt argument, piped input is ignored, so put the text you want the model to read in the prompt (or pipe the whole prompt).
- `--format` / `-f` — `text` (default) prints the answer on stdout (nothing after an error) and a one-line note on stderr when the run didn't simply finish or had calls denied. `json` prints the [run report](#the-run-report). `stream-json` prints [the run as events](#stream-json-the-run-as-events), one per line. While `evi run` runs, eVi's own console output (MCP warnings, notes) goes to stderr, so stdout holds only the answer, the report or the events.
- `--schema` — a file path or inline JSON. A bad schema prints `error: ...` to stderr and exits **2**.
- `--max-turns N` — the most model round trips the run may take (default 12, at least 1). A run that hits it stops with `stop_reason` `max_turns` and exits **3**.
- `--cwd` / `-C DIR` — run in `DIR` instead of the current directory: `EVI.md` comes from there, its `.evi.toml` applies only if the folder is [trusted](../configuration.md#trusting-a-folder) (`evi trust`), and `files_changed` is tracked there. An untrusted folder still runs, with its project config off; the report's `trust` says which case applied.
- `--model ID` — the model id for this run.
- `--backend NAME` — run on a saved backend (`evi backend list`; the name is matched without regard to case). A delegate CLI-agent backend (`codex`, `gemini`, `amp`, `qwen`, `copilot`) is refused, as for the configured backend, unless `[llm] allow_cli_agent_unattended = true`: the error names `--backend` and the run exits **1**.
- `--resume` / `-r ID` — add this run to the session `ID` (`evi sessions list`): its history is loaded first, and the report keeps that session id and transcript. A copy of the session handed off from another device is taken in first, as `evi sessions resume` does.
- `--continue` / `-c` — add this run to the most recently active session. That is the newest session anywhere, not the newest for this folder, so prefer `--resume` when several jobs run side by side.
- `--mode` / `-m` — the tool preset: `chat`, `cowork` or `code`.
- `--yes` / `-y` — approve every tool call. Without it, tool calls that aren't already auto-approved are **denied** (so a scripted run never blocks waiting for a permission prompt), and the run still counts as `done` if the model finishes. Not with `--policy`, which decides every call itself.
- `--worktree BRANCH|auto` — run isolated in a git worktree, so the checkout you work in is never touched. The worktree is `<repo>/.worktrees/<branch>` (a `/` in the name becomes `__`) of the repository at `--cwd`, or here. `<repo>` is always the main checkout, also when you start inside another worktree, so worktrees never nest, and `.git/info/exclude` keeps `.worktrees/` out of `git status`. `auto` makes a fresh branch, `fix-1`, `fix-2`, …; a branch name reuses that branch's worktree under `.worktrees/`, checks an existing branch out in a new one, or creates the branch. The worktree becomes the run's folder: `EVI.md` and `.evi/anatomy.md` come from it alone, its `.evi.toml` applies when the repository is trusted (a worktree shares its repository's [trust](../configuration.md#trusting-a-folder)), and `files_changed` is tracked there. While the run goes on, the worktree stays locked (`git worktree lock`, reason `eVi isolated run (pid N on HOST)`), so [`evi worktree prune`](../features.md#machine-ops) and other runs leave it alone; the lock comes off when the run ends. The report's [`worktree`](#the-run-report) says what the run left there, and the text format prints it in one line on stderr. Nothing is merged back. A branch checked out anywhere else (your main checkout above all), a name that differs from an existing branch only in case (Windows and macOS treat them as one branch), a worktree another run holds locked, an invalid name or a folder outside git is a usage error (exit **2**) and nothing runs. Here `auto` always means a new branch, so a branch named `auto` can't be used with `--worktree`.
- `--base REF` — with `--worktree`, the branch or commit a new branch starts from (default: `[worktree] base_ref`, else `HEAD`). A branch or worktree that already exists is used as it is.
- `--policy implementer` — run under the [implementer policy](permissions-and-sandbox.md#implementer-policy) instead of the permission flow: every tool call is allowed or denied with a reason, never asked. The run may read and edit files in its folder (the worktree with `--worktree`, else the git top level of `--cwd` or the current folder), run tests, linters and read-only git commands there when the folder is trusted, and nothing else: no network, MCP tools, `run_python`, memory or subagents. Once the turn has changed files, the project's checks run before it may end ([Checks and stop-hook blocks](#checks-and-stop-hook-blocks)). The report's `policy` is `implementer`, and each denied call carries its `reason`. Without `--worktree` a note on stderr recommends it, and in an untrusted folder a note says no commands will run. An unknown policy, or `--policy` with `--yes`, is a usage error (exit **2**).
- `--scope GLOB` — with `--policy`, a `files_in_scope` entry relative to the run's folder; repeat it for more. An entry matters only for protected files: one that names a protected file explicitly (`.github/workflows/ci.yml`, or `.github/workflows/*.yml`) lets the run edit it; `**` and other broad globs grant none, and some files are never granted. It doesn't narrow what else the run may edit. An entry that is empty, absolute or climbs out with `..`, or `--scope` without `--policy`, is a usage error (exit **2**).
- `--evidence DIR` — once the run is over, whatever its `stop_reason`, write an [evidence bundle](evidence.md) to `DIR`: the run's folder (its worktree with `--worktree`) as `diff.patch` and `diffstat.txt` against where it started (the worktree's base, else the commit checked out when the run began, so changes left uncommitted from before show too: use `--worktree` for a clean diff), the checks the run ran, a redacted excerpt of its transcript, and `report.json` holding the run report and the environment. The bundle's path and hash go to stderr (`evidence: <path> (sha256:…)`), and the folder is recorded so a task may name it as its evidence. `DIR` is resolved against the folder you run `evi run` in and must be new, empty or an older bundle, outside `~/.evi/evidence/`, and not the bundle a task of the repository names; anything else is a usage error (exit **2**) and nothing runs. A bundle that can't be written is a note on stderr and leaves the exit code alone.

### CLI: batch

```
evi batch <input_file> [--out results.jsonl] [--parallel N] [--mode <preset>] [--yes] [--max-turns N]
```

- `<input_file>` — `.jsonl` / `.json`, or a plain one-prompt-per-line file.
- `--out` / `-o` — write JSONL results to a file; omit to print to stdout. When writing to a file, eVi prints a summary like `wrote 12 results (11 ok) -> results.jsonl`.
- `--parallel` / `-j` — run N items concurrently.
- `--mode` / `-m` — default tool preset for items that don't specify one.
- `--yes` / `-y` — auto-approve tool calls (otherwise non-approved tools are denied).
- `--max-turns N` — the most model round trips each item may take (default 12).
- Per-item `mode` and `schema` fields in the input file **override** the command-line flags.

Each output line is the item's `id` and `prompt` followed by its [run report](#the-run-report), the same fields `evi run --format json` prints (`files_changed` is `null` and `trust` is `null`). An item that raised before it could report gets only `id`, `prompt`, `error` and `"stop_reason": "error"`. The command itself exits 0 once it has written the results, whatever each item's `stop_reason`.

### Web UI / API

The web chat endpoint accepts an optional `output_schema` field:

```
POST /api/chat
{ "session_id": "...", "message": "...", "output_schema": { ...JSON Schema... } }
```

- `output_schema` may be a JSON Schema **object** or an inline-JSON **string** (a string is run through `load_schema`).
- An invalid schema returns **HTTP 400** with `bad schema: <reason>`.
- The reply streams back over SSE exactly like a normal turn, in the [versioned event envelope](#stream-json-the-run-as-events), but the model's output is constrained to the schema.

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

Run a one-shot extraction and get the run report:

```bash
evi run "Extract the contact fields from this signature: Jane Doe, VP Engineering, jane@acme.io" \
  --schema contact.json --format json
```

The report is printed on one line; spread out, it reads:

```json
{
  "schema_version": 1,
  "session_id": "3f9a1c2d7b40",
  "transcript_path": "/home/you/.evi/transcripts/2026-10-16/3f9a1c2d7b40.jsonl",
  "backend": "lmstudio",
  "model": "qwen2.5-7b-instruct",
  "cwd": "/home/you/project",
  "trust": "untrusted",
  "turns_used": 1,
  "max_turns": 12,
  "stop_reason": "done",
  "done_reason": "stop",
  "text": "{\"name\":\"Jane Doe\",\"email\":\"jane@acme.io\",\"title\":\"VP Engineering\"}",
  "error": null,
  "tool_calls": [],
  "denied_tools": [],
  "usage": {"prompt": 120, "completion": 24, "total": 144},
  "duration_s": 1.284,
  "files_changed": [],
  "checks": [],
  "checks_skipped": null,
  "stop_blocks": [],
  "policy": "deny",
  "redactions": {},
  "worktree": null
}
```

The `text` field holds the JSON object matching the schema; `jq -r .text` pulls it out. To pipe the input in instead, send the whole prompt on stdin and leave the prompt argument off:

```bash
{ echo "Extract the contact fields from this signature:"; cat signature.txt; } | \
  evi run --schema contact.json --format json | jq -r .text
```

You can also pass the schema inline instead of a file:

```bash
evi run --schema '{"type":"object","properties":{"sentiment":{"type":"string","enum":["pos","neg","neu"]}},"required":["sentiment"]}' \
  "Classify the sentiment: I absolutely love this product"
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

eVi prints a summary (`wrote 3 results (3 ok) -> out.jsonl`) and `out.jsonl` holds one JSON object per line, keyed by your `id`, each with a full run report. The first line, for example:

```jsonl
{"id": "a1", "prompt": "Extract contacts from: Bob Lee, CTO, bob@x.io", "schema_version": 1, "session_id": "9c04e1b2aa31", "transcript_path": "/home/you/.evi/transcripts/2026-10-16/9c04e1b2aa31.jsonl", "backend": "lmstudio", "model": "qwen2.5-7b-instruct", "cwd": "/home/you/project", "trust": null, "turns_used": 1, "max_turns": 12, "stop_reason": "done", "done_reason": "stop", "text": "{\"name\":\"Bob Lee\",\"email\":\"bob@x.io\",\"title\":\"CTO\"}", "error": null, "tool_calls": [], "denied_tools": [], "usage": {"prompt": 90, "completion": 22, "total": 112}, "duration_s": 0.912, "files_changed": null, "checks": [], "checks_skipped": null, "stop_blocks": [], "policy": "allow-all", "redactions": {}, "worktree": null}
```

To see how each item went:

```bash
jq -c '{id, stop_reason, text}' out.jsonl
```

```jsonl
{"id":"a1","stop_reason":"done","text":"{\"name\":\"Bob Lee\",\"email\":\"bob@x.io\",\"title\":\"CTO\"}"}
{"id":"a2","stop_reason":"done","text":"{\"name\":\"Amy Ng\",\"email\":\"amy@y.io\"}"}
{"id":"a3","stop_reason":"done","text":"A fast brown fox leaps over a lazy dog."}
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

### Example 3 — Follow a run as it goes, then pick it up again

Stream a run's events, keep them, and act on the result line:

```bash
evi run --format stream-json -m code -y "Add a .gitignore for Python" > run.ndjson
echo "exit $?"
jq -c 'select(.kind == "ToolCall") | .name' run.ndjson   # what it called
tail -n 1 run.ndjson | jq '{stop_reason, files_changed}'    # how it ended
```

Continue the same session with a follow-up, using the id from the first line:

```bash
sid=$(head -n 1 run.ndjson | jq -r .session_id)
evi run --resume "$sid" --format json "Now add a pytest cache entry too" | jq .stop_reason
```

### Example 4 — Let a run work in a worktree of its own

Give the change a branch and a folder of its own, so a run that goes wrong can't break the checkout you work in:

```bash
evi run --worktree auto -m code -y --format json "Add a .gitignore for Python" > run.json
jq .worktree run.json
```

```json
{
  "path": "/home/you/project/.worktrees/fix-1",
  "branch": "fix-1",
  "created": true,
  "base": "3f9a1c2d7b40e8a5c6d1f20b9e7a4c3d2b1a0f9e",
  "head": "3f9a1c2d7b40e8a5c6d1f20b9e7a4c3d2b1a0f9e",
  "commits": [],
  "dirty": true,
  "dirty_files": ["?? .gitignore"],
  "diffstat": {"files": 1, "insertions": 12, "deletions": 0}
}
```

Your checkout is unchanged. Review the work in `.worktrees/fix-1`, commit it there and merge `fix-1` yourself; a later `evi run --worktree fix-1 …` carries on in the same worktree. Once the branch is merged and its worktree is clean, `evi worktree prune` removes the worktree.

### Example 5 — An implementer run that has to pass its tests

Instead of `-y`, give the run the [implementer policy](permissions-and-sandbox.md#implementer-policy): it may edit files in its worktree and run the tests, linters and read-only git there, and nothing else. In a [trusted](../configuration.md#trusting-a-folder) repository the checks for the files it changed run before it may finish:

```bash
evi run --policy implementer --worktree auto -m code --format json \
  "Fix the rounding bug in calc.py; tests/test_calc.py shows it" > run.json
echo "exit $?"   # 0 done, 5 checks_failed, …
jq '{stop_reason, checks: [.checks[] | {name, exit, round}], denied_tools}' run.json
```

A run whose first fix failed the tests and whose second passed reads like this (the repository has a ruff config, so ruff runs too):

```json
{
  "stop_reason": "done",
  "checks": [
    {"name": "ruff", "exit": 0, "round": 1},
    {"name": "pytest", "exit": 1, "round": 1},
    {"name": "ruff", "exit": 0, "round": 2},
    {"name": "pytest", "exit": 0, "round": 2}
  ],
  "denied_tools": []
}
```

While it runs, stderr shows each check (`check pytest (round 1) exit 1 in 1.9s — …`) and each round that went back to the model. A call the policy refused is listed in `denied_tools`, and `jq '.tool_calls[] | select(.status == "denied") | .reason' run.json` says why.

## Notes / limits

- **Schema enforcement is backend-dependent.** Only backends that support OpenAI-style `response_format` actually *guarantee* the shape. Others ignore it and return best-effort JSON — design your downstream parsing to tolerate the occasional non-conforming reply when running against such a backend.
- **No double-wrapping.** You can pass a bare schema, a `{"name","schema"}` pair, or a full `{"type":"json_schema",...}` envelope; `as_response_format` detects and preserves the richer forms. To control the `strict` flag or schema `name`, pass the full envelope yourself.
- **`/schema` vs `/json`.** `/json` forces an unstructured JSON object; `/schema` forces a specific schema. Use `/schema off` to clear an armed schema between turns.
- **Batch is fail-open per item.** One bad prompt or one item-level exception never aborts the run — the failing item gets `"stop_reason": "error"` and an `error` string, and the rest proceed. Check each line's `stop_reason`: anything but `done` means that item didn't simply finish, even though the batch "succeeded".
- **Unattended permission policy.** In both `evi run` and `evi batch`, without `--yes` any tool call not already on your auto-approve list is **denied** rather than prompting (a scripted run can't answer a prompt). The run carries on and still ends `done` (exit 0) when the model finishes; the report lists the refused tools in `denied_tools`, so check that list when a job needs its tools. Pass `--yes` to auto-approve all tool calls for genuinely unattended jobs — and only do so when you trust the prompts, since it removes the human-in-the-loop guard. For a coding job, `evi run --policy implementer --worktree …` is the narrower choice: edits in the run's folder, tests and linters, nothing else.
- **Parallelism caveats.** `--parallel` uses threads; the effective worker count is capped at the number of items. Higher concurrency can hit backend rate limits or local-model memory limits — tune `N` to your hardware/backend.
- **Tool calls are cut short in the report.** Each call's arguments and output are redacted and then cut to 2000 characters, and `max_turns` defaults to 12 — raise `--max-turns` for long multi-tool tasks (a run that runs out exits **3**).
- **Exit codes.** `evi run` exits with the code for its `stop_reason` in every format (0 done, 1 error, 3 max_turns, 4 guardrail, 5 checks_failed, 6 stop_hook_exhausted, 130 interrupted; [table above](#stop-reasons-and-exit-codes)) and **2** on a usage error. `evi batch` exits **2** on unparseable input or a bad item, **1** when the file holds no prompts, and **0** once it has written its results.
- **Privacy.** Like the rest of eVi, these run locally against your configured backend; no data leaves your machine beyond what your chosen backend itself sends.
- **A killed worktree run leaves its lock behind.** A `--worktree` run that is killed outright (not stopped with Ctrl+C) can't take its lock off, so prune keeps that worktree and the next run on its branch is refused. See [Troubleshooting](../troubleshooting.md#a-worktree-cant-be-locked-for-this-run).
