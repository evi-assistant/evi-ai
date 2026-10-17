# Hooks (tool + lifecycle, command/url)

## Overview

Hooks let you run your own code (or call a webhook) automatically at key moments while eVi works: right **before** and right **after** every tool call, and around three **lifecycle** events in a turn. A hook can simply *observe* (audit a log, fire a desktop notification, POST to a webhook) or it can *veto* — block a tool call, reject a user prompt, or skip history compaction — based on its own logic.

Because eVi is local-first and single-user, hooks are your escape hatch for custom policy and integration without touching eVi's source: audit trails, "never write outside this folder" guards, secret-scanners on prompts, Slack/webhook notifications, and so on.

Hooks live in one file, `~/.evi/hooks.toml` (Windows: `%USERPROFILE%\.evi\hooks.toml`), and are loaded fresh each time the CLI starts. Installed plugins can also ship their own `hooks.toml`, which is merged in automatically. The web and desktop app load them once and reload them when you Save in Settings → Hooks or install or remove a plugin.

## How it works

A hook is an entry in `hooks.toml` keyed by the **event** it fires on. There are five events:

| Event | When it fires | Can veto? | What veto does |
|-------|---------------|-----------|----------------|
| `before_tool_call` | Before a tool runs | Yes | Blocks the tool; the hook's stderr becomes the result the model sees |
| `after_tool_call` | After a tool returns | No | Veto is ignored (notification only); a non-zero exit is just logged |
| `user_prompt_submit` | Before each turn, before the model sees your prompt | Yes | Blocks the prompt |
| `before_compact` | Before history compaction | Yes | Keeps history intact (skips compaction) |
| `stop` | Once per turn, when the reply is complete | Only with `protocol = "json"` | A plain `stop` hook is a notification and never blocks. A [blocking stop hook](#blocking-stop-hooks-protocol--json) runs each time the model would end its turn and can send it back to work |

`before_tool_call` and `after_tool_call` are **tool-scoped**: their `match` is a glob over the tool name. The three lifecycle events (`user_prompt_submit`, `before_compact`, `stop`) are **not tied to a tool**, so they use `match = "*"` (the default).

A plain `stop` hook fires exactly once per completed turn, just before the turn ends. A re-roll is a turn of its own and fires it once. A subagent the turn spawns (`delegate`, `parallel_research`, …) does not fire it, nor `user_prompt_submit`: subagents inherit only the tool hooks (see [Where hooks apply](#where-hooks-apply)). `stop` does not fire for a prompt that a `user_prompt_submit` hook or an input guardrail blocked, or for a turn that ended in an LLM error. eVi waits for it before reporting the turn done, so keep it quick (or give it a short `timeout`).

Each hook does one of two things:

- **`command`** — an argv list that eVi spawns as a subprocess. It is **not** shell-evaluated; if you want shell features, invoke a shell explicitly (e.g. `["bash", "-c", "…"]`).
- **`url`** — eVi sends an HTTP `POST` with a JSON body instead of spawning anything. A `2xx` response counts as success (exit code `0`); any other status becomes the "exit code", so a `4xx`/`5xx` from a `before_*` URL hook with `veto_on_nonzero = true` blocks the action.

A hook must define **either** `command` **or** `url` (an entry with neither is skipped as malformed).

### Veto semantics

Veto only matters on the three vetoable events and only when the hook sets `veto_on_nonzero = true`. If such a hook exits non-zero, it vetoes:

- **Tool veto** — the call is blocked and the model receives a result like
  `BLOCKED BY HOOK 'no-system-writes': <hook stderr or stdout>`.
- **Prompt veto** (`user_prompt_submit`) — the turn is blocked before the model runs.
- **Compaction veto** (`before_compact`) — compaction is skipped, history left intact.

Multiple hooks for the same event run in order; the **first** vetoing hook wins and stops the rest. Order is: your `~/.evi/hooks.toml` hooks first, then plugin hooks, so your own rules are evaluated first.

Permission gating happens **before** before-hooks. So a tool call is first checked against your auto-approve / permission settings, and only if approved does it reach `before_tool_call` hooks.

### Environment variables passed to `command` hooks

When eVi spawns a `command` hook it inherits your environment plus these:

| Variable | Value |
|----------|-------|
| `EVI_HOOK_EVENT` | The event name (`before_tool_call`, `user_prompt_submit`, …) |
| `EVI_HOOK_TOOL` | The tool name for tool events; the **event name** for lifecycle events |
| `EVI_HOOK_ARGS_JSON` | Tool call arguments as JSON; for `user_prompt_submit`, the prompt text; for `before_compact`, the count of messages being compacted; for a plain `stop` hook, the finish reason (`stop`, `length`, `max_turns_reached`, `checks_failed`, `stop_hook_exhausted`, …); for a [blocking stop hook](#blocking-stop-hooks-protocol--json), the event object it also gets on stdin. Whole unless it is too big for one environment variable (see [Big payloads](#big-payloads)) |
| `EVI_HOOK_ARGS_FILE` | Path of a temp file holding the **whole** payload above, exactly. Set when `EVI_HOOK_ARGS_JSON` was cut, and always for a hook with `args_via_file = true`; otherwise unset, so small payloads never touch the disk. Deleted when the hook exits |
| `EVI_HOOK_ARGS_TRUNCATED` | `1` when `EVI_HOOK_ARGS_JSON` was cut; unset otherwise |
| `EVI_HOOK_RESULT` | Only for `after_tool_call`: the tool's stringified output, **truncated to 4 KB** |
| `EVI_CWD` | The session working folder the tools resolve relative paths against (set by `/cd`, `--cwd`, the web folder chip); the process folder when none is set |
| `EVI_EFFORT` | The active reasoning effort (`low` / `medium` / `high` / `max`) |

A `command` hook **runs in the session working folder** (`EVI_CWD`), so a "stay inside this folder" guard resolves a relative `path` the same way the tool will. Its **stdin is the null device**: a hook that reads stdin gets end-of-file straight away instead of waiting on the terminal (or the desktop app's server pipe) until its timeout. The one exception is a blocking stop hook, which reads its event from stdin. A NUL character in the payload, which no environment variable can hold, reaches `EVI_HOOK_ARGS_JSON` as the JSON escape `\u0000`.

#### Big payloads

One environment variable holds about 32,000 characters on Windows and 128 KiB on Linux. A payload that fits (nearly every tool call and prompt) reaches `EVI_HOOK_ARGS_JSON` whole. A bigger one (a pasted log, a large `write_file`) is handled like this:

- **A guard** (`veto_on_nonzero = true` on `before_tool_call`, `user_prompt_submit` or `before_compact`) is **not run** on it, and the action is **blocked**: a guard must never judge input it can only partly see, since padding could push a write's `path` past any cut. The model sees why. To check input of any size, set `args_via_file = true` on the hook and read the payload from `EVI_HOOK_ARGS_FILE` (see [Example 2](#example-2--scan-prompts-for-secrets-with-a-python-script-cross-platform)).
- **Any other hook** runs with a shortened `EVI_HOOK_ARGS_JSON`, `EVI_HOOK_ARGS_TRUNCATED=1`, and the whole payload in `EVI_HOOK_ARGS_FILE`. Tool arguments stay valid JSON with every key: only their longest string values are cut, all to the same length, each ending in `...[cut N chars; see EVI_HOOK_ARGS_FILE]`. When the arguments are too big even with every long string cut to that marker (thousands of string values, say), they keep their beginning instead, which is not valid JSON. A prompt keeps its beginning.

The args file is private to your user and deleted when the hook exits; a background process the hook starts must copy it before then. One that can't be deleted at that moment (on Windows, while such a process still has it open) is removed on the next hook run or when eVi exits.

### What a `url` hook POSTs

eVi sends `Content-Type: application/json`, `User-Agent: evi-hook`, and a JSON body:

```json
{ "event": "after_tool_call", "tool": "generate_image", "args_json": "{…}", "result": "…", "effort": "medium", "cwd": "/home/me/project" }
```

`result` is included only for `after_tool_call` (and is likewise capped at 4 KB). `args_json` is never cut, and `cwd` is the session working folder. A blocking stop hook's `url` is POSTed its event object instead (below).

### Blocking stop hooks (`protocol = "json"`)

A `[[stop]]` hook with `protocol = "json"` is a gate on the end of the turn. It runs each time the model would end its turn, before the turn ends, and it can send the model back to work: "the tests still fail, fix them first". This is how you keep a small model from declaring success early. It doesn't run for a plan-mode turn, or when the backend cut the reply off (its token limit or content filter).

```toml
[[stop]]
name     = "pytest-gate"
protocol = "json"
timeout  = 600
command  = ["python", "/path/to/gate.py"]
```

[`examples/hooks/pytest-gate.toml`](../../examples/hooks/pytest-gate.toml) is a working one: it runs the tests the turn touched and blocks while they fail. Give such a hook a `timeout` that covers your tests: the default is 30 seconds, and a hook that times out doesn't block.

**What the hook gets.** One JSON object on stdin (the same text is in `EVI_HOOK_ARGS_JSON`):

```json
{"event": "stop", "session_id": "3f9a1c2d7b40", "cwd": "/home/me/project",
 "stop_hook_active": false, "iteration": 1, "files_changed": ["calc.py", "tests/test_calc.py"]}
```

| Field | What it holds |
|---|---|
| `event` | Always `"stop"`. |
| `session_id` | The session the turn belongs to. |
| `cwd` | The session working folder. The hook runs there. |
| `stop_hook_active` | `true` once a stop hook has already sent the model back during this turn. |
| `iteration` | Which would-be end of the turn this is: 1 the first time the model tries to finish, 2 the next, and so on. |
| `files_changed` | The files the turn changed, relative to `cwd`: what its write tools wrote, plus what its commands changed when the [project checks](../configuration.md#verify-before-done--checks-in-configtoml) are on. |

**How it answers.**

- **Block:** print `{"decision": "block", "reason": "..."}` on stdout (as the whole output, or as the last line that is a JSON object), or exit with code **2**, with the reason on stderr.
- **Let the turn end:** exit 0 without that JSON.
- **Anything else is a hook error, never a block:** a timeout, another exit code, a hook that can't start. eVi logs it and the turn ends as usual, so a broken gate never traps a turn.

A `url` hook with the protocol is POSTed the event object itself and blocks by sending the same `{"decision": "block", …}` JSON in a 2xx response. Any other status is an error, so it doesn't block.

**What a block does.** The reason goes back to the model as a framed note (redacted, and cut to 6,000 characters), and the turn goes on. When several blocking hooks are configured they run in order and the first block wins; the rest don't run that time. In the web and desktop chat the block shows inline as a note (`⛔ stop hook "pytest-gate" blocked the end of the turn (iteration 1) — eVi keeps going`, then the reason), and a footer under the turn's final reply lists the files its tools wrote. `evi chat` and `evi run` print a `stop hook <name> blocked the end (iteration N): <reason>` line (`evi run` on stderr). In `evi run`'s run report each block is a `stop_blocks` entry (`hook`, `reason`, `iteration`), and in its event stream a `StopBlocked` event; see [The run report](structured-and-batch.md#the-run-report). Each reply after a block uses one of the run's `--max-turns` round trips.

**The cap: `[hooks] max_blocks`** in `config.toml` (default 3) is how many times per user turn the stop hooks may send the model back. Exactly:

- each block while fewer than `max_blocks` blocks have happened in this turn sends the model back, adds a note and a `stop_blocks` entry;
- the next block ends the turn with stop reason **`stop_hook_exhausted`** (`evi run` exits **6**). That last block adds no note and no `stop_blocks` entry, so its reason is not recorded;
- so a hook that always blocks runs `max_blocks` + 1 times: with the default, three blocks go back to the model and the fourth ends the turn;
- `max_blocks = 0` means the first block ends the turn; a negative or non-numeric value means 3;
- the count starts again with every message you send. A hook error doesn't count.

**Order with the project checks.** When the [project checks](../configuration.md#verify-before-done--checks-in-configtoml) are on, they run first. A failing check round goes back to the model on its own and the stop hooks don't run that time; the hooks run once the checks pass (or when none ran).

**What stays the same.** Only `protocol = "json"` blocks. A plain `[[stop]]` hook that happens to exit 2 is still a notification, run once when the turn ends, and a blocking hook is not run a second time as one. `protocol` is only for `[[stop]]`: on any other event the entry is skipped as malformed (and Settings → Hooks refuses to save it). Like every lifecycle hook, a blocking stop hook doesn't run for a subagent, and safe mode turns it off. eVi loads hooks only from your own `hooks.toml` and installed plugins, never from a repository, so a repository can't add a gate to your turns.

## Setup

There are no config flags in `config.toml` to enable hooks and no pip extras — the feature is always on, driven entirely by the presence of `~/.evi/hooks.toml`. If the file doesn't exist, no hooks run. The one hook setting in `config.toml` is `[hooks] max_blocks`, the cap on [blocking stop hooks](#blocking-stop-hooks-protocol--json).

Create `~/.evi/hooks.toml`. Each event is a TOML array-of-tables (`[[event_name]]`). Fields per entry:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | string | derived from event + match | Label shown in veto messages / logs |
| `match` | string (glob) | `"*"` | Globs the tool name (tool events only); use `"*"` for lifecycle events |
| `command` | string or list of strings | — | argv; a bare string is wrapped to a one-element list, **not** shell-expanded |
| `url` | string | `""` | If set, POST instead of spawning a command |
| `timeout` | number (seconds) | `30.0` | Subprocess / HTTP timeout |
| `veto_on_nonzero` | bool | `false` | Only meaningful for `before_*` / `user_prompt_submit` / `before_compact` |
| `args_via_file` | bool | `false` | The hook reads its payload from `EVI_HOOK_ARGS_FILE`, which is then always written. Lets a guard check payloads too big for `EVI_HOOK_ARGS_JSON` instead of blocking them ([Big payloads](#big-payloads)) |
| `protocol` | string | `""` | `"json"` on a `[[stop]]` hook makes it a [blocking stop hook](#blocking-stop-hooks-protocol--json); not allowed on other events |

A complete reference file:

```toml
# ~/.evi/hooks.toml

[[before_tool_call]]
name            = "audit"
match           = "*"                       # glob over tool names: fs.*, write_file, …
command         = ["bash", "-c", "echo $EVI_HOOK_TOOL >> ~/.evi/logs/tools.log"]
timeout         = 5

[[before_tool_call]]
name            = "no-system-writes"
match           = "write_file"
command         = ["bash", "-c", 'echo "$EVI_HOOK_ARGS_JSON" | grep -qv "/etc/"']
veto_on_nonzero = true                      # non-zero exit blocks the tool

[[after_tool_call]]
name    = "notify"
match   = "generate_image"
command = ["notify-send", "Image ready"]

[[after_tool_call]]
name    = "webhook"
match   = "*"
url     = "https://example.com/evi-hook"    # POST instead of spawning a command

[[user_prompt_submit]]                       # fires before each turn
name            = "no-secrets"
command         = ["python3", "/home/me/check_prompt.py"]   # reads EVI_HOOK_ARGS_FILE
veto_on_nonzero = true
args_via_file   = true                      # check prompts of any length

[[before_compact]]                           # before history compaction; veto keeps it intact
name    = "log-compaction"
command = ["bash", "-c", "echo compacting >> ~/.evi/logs/compaction.log"]

[[stop]]                                     # after a turn completes (a notification)
name    = "ding"
command = ["notify-send", "eVi finished a turn"]

[[stop]]                                     # before the turn may end; can send the model back
name     = "pytest-gate"
protocol = "json"                           # the event arrives as JSON on stdin
command  = ["python3", "/home/me/gate.py"]  # prints {"decision": "block", "reason": "…"} to block
timeout  = 600
```

Hooks are merged from your `hooks.toml` plus any `hooks.toml` shipped by installed plugins. There's no separate `evi hooks` CLI subcommand — you manage hooks by editing the file. (`evi plugin list` shows how many hooks each installed plugin contributes.)

## Usage

Hooks aren't invoked by hand — they run automatically once configured. The workflow is:

1. Edit `~/.evi/hooks.toml` (see Setup), or use **Settings → Hooks** / the CLI below.
2. Start or restart the CLI (`evi chat`, `evi run`, …). Hooks are loaded when it starts, so restart it after editing `hooks.toml`. The web and desktop app pick up a Save in Settings → Hooks at once, with no restart.
3. Drive eVi normally — hooks fire as tools run and turns progress.

### CLI — `evi hooks`

```text
evi hooks path                       # print the config file path
evi hooks list                       # every loaded hook (yours + plugin), grouped by event
evi hooks test <tool> [--event …]    # which hooks WOULD fire for a tool name (nothing runs)
```

`hooks test` is match-resolution only — it shows the hooks whose `match` glob
hits the tool name and flags the ones that can veto, without executing anything.

### Web / Desktop — the Hooks editor

**Settings → Hooks** shows every loaded hook as a chip (event, name, veto flag)
above a raw `hooks.toml` editor. **Save** validates the whole file first — bad
TOML, a malformed entry, and crucially **typo'd event names** (e.g.
`[[before_toolcall]]`), which the runtime loader would otherwise skip
silently — and reports the error inline instead of writing a broken file.
Plugin-supplied hooks appear in the chips but aren't editable here (they live
in the plugin). Save applies at once to every open chat (one shared registry,
reloaded in place). Backed by `GET`/`POST /api/hooks`.

### Where hooks apply

Hooks run wherever the CLI builds an agent (the `evi chat` REPL, `evi run` and the other agent commands) and for an agent built with the Python SDK's `build_agent` (on by default). They also guard web and desktop chat tabs; the workflow, recipe, routine and ultracode runs the web app starts; federation (`/api/federate`) and A2A tasks; and Telegram bridge turns. Eval runs (`/api/evals/run`) skip them so scores don't depend on the machine. In multi-user mode every user's sessions are guarded by the owner's `hooks.toml`. `session_start` and `session_end` still fire only in the CLI REPL, not in web sessions. Safe mode (`--safe-mode`) turns hooks off everywhere.

A subagent spawned from a chat (`delegate`, `delegate_explore`, `parallel_research`) inherits the **tool hooks** (`before_tool_call`, `after_tool_call`) of the agent that spawned it and works in the same folder, so a veto guard binds it too. It does not inherit the lifecycle hooks: its task is not a prompt you typed, so `user_prompt_submit` doesn't screen it, and its end is not the end of your turn, so `stop` doesn't fire for it.

Where to watch the effects:

- `command` hooks write wherever you point them (e.g. `~/.evi/logs/tools.log` in the audit example).
- When a `before_tool_call` hook vetoes, the model receives a `BLOCKED BY HOOK '…': …` tool result and typically tells you the action was blocked.
- A vetoed `user_prompt_submit` stops the turn before the model runs.
- When a blocking stop hook sends the model back, the chat shows its reason as a note and the model carries on; `evi run` prints a `stop hook … blocked the end` line on stderr and records the block in the report's `stop_blocks`.

## Examples

### Example 1 — Audit every tool call, and block writes to system paths

This logs every tool name, and refuses any `write_file` whose arguments mention `/etc/`. The guard uses `grep -qv`: it exits **non-zero** (vetoes) when `/etc/` *is* present.

```toml
# ~/.evi/hooks.toml

[[before_tool_call]]
name    = "audit"
match   = "*"
command = ["bash", "-c", "echo \"$(date -Iseconds) $EVI_HOOK_TOOL $EVI_HOOK_ARGS_JSON\" >> ~/.evi/logs/tools.log"]
timeout = 5

[[before_tool_call]]
name            = "no-system-writes"
match           = "write_file"
command         = ["bash", "-c", 'echo "$EVI_HOOK_ARGS_JSON" | grep -qv "/etc/"']
veto_on_nonzero = true
```

When the model tries to write to `/etc/passwd`, the guard exits non-zero and the model sees roughly:

```
BLOCKED BY HOOK 'no-system-writes': (no message)
```

(The bracketed message is the hook's stderr/stdout — empty here because `grep -q` is silent. Print to stderr in your guard if you want a custom reason shown to the model.)

### Example 2 — Scan prompts for secrets with a Python script (cross-platform)

A `user_prompt_submit` hook reads the prompt and rejects it if it looks like an API key or password. This works on Windows too, since it spawns Python directly rather than relying on a shell. With `args_via_file = true` it reads the prompt from `EVI_HOOK_ARGS_FILE`, so it can check a pasted prompt of any length; a guard that reads `EVI_HOOK_ARGS_JSON` instead blocks a prompt too long for that variable ([Big payloads](#big-payloads)).

```toml
# ~/.evi/hooks.toml

[[user_prompt_submit]]
name            = "no-secrets"
command         = ["python3", "C:/Users/me/.evi/check_prompt.py"]
veto_on_nonzero = true
args_via_file   = true
```

```python
# C:/Users/me/.evi/check_prompt.py
import os, re, sys

with open(os.environ["EVI_HOOK_ARGS_FILE"], encoding="utf-8") as f:
    prompt = f.read()
if re.search(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*\S", prompt):
    print("Prompt looks like it contains a secret; blocked.", file=sys.stderr)
    sys.exit(1)   # non-zero + veto_on_nonzero -> the turn is blocked
sys.exit(0)
```

### Example 3 — Webhook notification (no command spawned)

POST a JSON payload to an external endpoint after any tool runs. No subprocess, no shell — eVi makes the HTTP call itself.

```toml
# ~/.evi/hooks.toml

[[after_tool_call]]
name    = "webhook"
match   = "*"
url     = "https://hooks.example.com/evi"
timeout = 10
```

The endpoint receives `POST` with body `{"event": "after_tool_call", "tool": "<name>", "args_json": "<json>", "result": "<≤4 KB output>"}`. To use the same mechanism as a *gate*, attach `url` + `veto_on_nonzero = true` to a `[[before_tool_call]]`: a `4xx`/`5xx` response then blocks the call.

## Notes / limits

- **A missing config is no hooks; a broken guard blocks.** A missing or malformed `hooks.toml` is treated as "no hooks", not an error. One bad entry is skipped with a warning while the rest load. Plugin-hook scanning failures never break your core hooks. But once a guard (`veto_on_nonzero`) is configured, anything that stops it from judging an action blocks the action: it can't be started (exit `126`), it times out (`124`), its input is too big for `EVI_HOOK_ARGS_JSON` and it doesn't set `args_via_file`, or eVi's hook runner itself fails. A hook that only observes never blocks, and its failure never breaks the turn.
- **Commands are not shell-evaluated.** `command` is argv. A bare-string `command` becomes a one-element argv list — it is **not** split on spaces or expanded. For pipes, globs, redirects, or `$VAR` expansion, call a shell explicitly (`["bash", "-c", "…"]`) — note this means `bash`-based examples need a shell available (use the Python form on Windows).
- **Timeouts.** Default `timeout` is `30.0` seconds (subprocess or HTTP). A timed-out hook reports exit code `124`; a command that fails to exec reports `126`. For a vetoing before-hook, a timeout therefore counts as a non-zero exit and **blocks** the action — keep guard hooks fast and set a tight `timeout`.
- **`after_tool_call` and plain `stop` hooks never block.** Their veto is ignored; a non-zero `after_tool_call` exit is only logged. Only a `[[stop]]` hook with `protocol = "json"` can keep a turn going, at most `[hooks] max_blocks` times ([Blocking stop hooks](#blocking-stop-hooks-protocol--json)), and its errors and timeouts never block.
- **4 KB result cap.** `EVI_HOOK_RESULT` (and the `result` field in a URL POST) is truncated to 4 KB to avoid blowing OS env/arg limits. Don't rely on receiving the full tool output.
- **Big arguments.** `EVI_HOOK_ARGS_JSON` holds a payload whole up to the OS limit for one environment variable (about 32,000 characters on Windows, 128 KiB on Linux). Past it, a guard without `args_via_file` blocks and other hooks get a shortened copy plus `EVI_HOOK_ARGS_FILE` ([Big payloads](#big-payloads)). A URL hook's `args_json` field is never cut.
- **Permission first, then hooks.** Tool calls pass through eVi's permission / auto-approve gate before before-hooks run; a denied tool never reaches a hook.
- **`EVI_HOOK_TOOL` for lifecycle events is the event name**, not a tool — there is no tool involved. For tool events it's the fully-qualified tool name (e.g. `write_file`, or `<server>.<tool>` for MCP tools).
- **Security.** Hooks run arbitrary local commands with your user's privileges and full environment. Treat plugin-supplied `hooks.toml` files as code: review a plugin's hooks before installing it, since its hooks are merged into your registry and execute on every matching event.
- **The CLI reloads on restart.** The CLI loads hooks once when it starts, so edits to `hooks.toml` take effect on its next launch. The web and desktop app reload on Save in Settings → Hooks and on plugin install or remove.
- See also the concise summaries in [docs/configuration.md](../configuration.md#hooks--evihookstoml) and [docs/features.md](../features.md#hooks). The authoritative behavior is in `evi/hooks.py`.
