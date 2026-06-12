# Hooks (tool + lifecycle, command/url)

## Overview

Hooks let you run your own code (or call a webhook) automatically at key moments while eVi works: right **before** and right **after** every tool call, and around three **lifecycle** events in a turn. A hook can simply *observe* (audit a log, fire a desktop notification, POST to a webhook) or it can *veto* — block a tool call, reject a user prompt, or skip history compaction — based on its own logic.

Because eVi is local-first and single-user, hooks are your escape hatch for custom policy and integration without touching eVi's source: audit trails, "never write outside this folder" guards, secret-scanners on prompts, Slack/webhook notifications, and so on.

Hooks live in one file, `~/.evi/hooks.toml` (Windows: `%USERPROFILE%\.evi\hooks.toml`), and are loaded fresh at startup. Installed plugins can also ship their own `hooks.toml`, which is merged in automatically.

## How it works

A hook is an entry in `hooks.toml` keyed by the **event** it fires on. There are five events:

| Event | When it fires | Can veto? | What veto does |
|-------|---------------|-----------|----------------|
| `before_tool_call` | Before a tool runs | Yes | Blocks the tool; the hook's stderr becomes the result the model sees |
| `after_tool_call` | After a tool returns | No | Veto is ignored (notification only); a non-zero exit is just logged |
| `user_prompt_submit` | Before each turn, before the model sees your prompt | Yes | Blocks the prompt |
| `before_compact` | Before history compaction | Yes | Keeps history intact (skips compaction) |
| `stop` | After a turn completes | No | Notification only; never blocks |

`before_tool_call` and `after_tool_call` are **tool-scoped**: their `match` is a glob over the tool name. The three lifecycle events (`user_prompt_submit`, `before_compact`, `stop`) are **not tied to a tool**, so they use `match = "*"` (the default).

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
| `EVI_HOOK_ARGS_JSON` | Tool call arguments as JSON; for `user_prompt_submit`, the prompt text; for `before_compact`, the count of messages being compacted |
| `EVI_HOOK_RESULT` | Only for `after_tool_call`: the tool's stringified output, **truncated to 4 KB** |

### What a `url` hook POSTs

eVi sends `Content-Type: application/json`, `User-Agent: evi-hook`, and a JSON body:

```json
{ "event": "after_tool_call", "tool": "generate_image", "args_json": "{…}", "result": "…" }
```

`result` is included only for `after_tool_call` (and is likewise capped at 4 KB).

## Setup

There are no config flags in `config.toml` to enable hooks and no pip extras — the feature is always on, driven entirely by the presence of `~/.evi/hooks.toml`. If the file doesn't exist, no hooks run.

Create `~/.evi/hooks.toml`. Each event is a TOML array-of-tables (`[[event_name]]`). Fields per entry:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | string | derived from event + match | Label shown in veto messages / logs |
| `match` | string (glob) | `"*"` | Globs the tool name (tool events only); use `"*"` for lifecycle events |
| `command` | string or list of strings | — | argv; a bare string is wrapped to a one-element list, **not** shell-expanded |
| `url` | string | `""` | If set, POST instead of spawning a command |
| `timeout` | number (seconds) | `30.0` | Subprocess / HTTP timeout |
| `veto_on_nonzero` | bool | `false` | Only meaningful for `before_*` / `user_prompt_submit` / `before_compact` |

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
command         = ["python3", "/home/me/check_prompt.py"]   # prompt is in EVI_HOOK_ARGS_JSON
veto_on_nonzero = true

[[before_compact]]                           # before history compaction; veto keeps it intact
name    = "log-compaction"
command = ["bash", "-c", "echo compacting >> ~/.evi/logs/compaction.log"]

[[stop]]                                     # after a turn completes (notification; never blocks)
name    = "ding"
command = ["notify-send", "eVi finished a turn"]
```

Hooks are merged from your `hooks.toml` plus any `hooks.toml` shipped by installed plugins. There's no separate `evi hooks` CLI subcommand — you manage hooks by editing the file. (`evi plugin list` shows how many hooks each installed plugin contributes.)

## Usage

Hooks aren't invoked by hand — they run automatically once configured. The workflow is:

1. Edit `~/.evi/hooks.toml` (see Setup), or use **Settings → Hooks** / the CLI below.
2. Start or restart eVi (`evi chat`, `evi web`, the desktop app, or any agent run). Hooks are loaded at process start, so restart after editing to pick up changes.
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
in the plugin). Backed by `GET`/`POST /api/hooks`.

This applies across every front end (CLI REPL, FastAPI/SSE web UI, Tauri desktop) and to headless / workflow runs, since they all load the same `HookRegistry`.

Where to watch the effects:

- `command` hooks write wherever you point them (e.g. `~/.evi/logs/tools.log` in the audit example).
- When a `before_tool_call` hook vetoes, the model receives a `BLOCKED BY HOOK '…': …` tool result and typically tells you the action was blocked.
- A vetoed `user_prompt_submit` stops the turn before the model runs.

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

A `user_prompt_submit` hook reads the prompt from `EVI_HOOK_ARGS_JSON` and rejects it if it looks like an API key or password. This works on Windows too, since it spawns Python directly rather than relying on a shell.

```toml
# ~/.evi/hooks.toml

[[user_prompt_submit]]
name            = "no-secrets"
command         = ["python3", "C:/Users/me/.evi/check_prompt.py"]
veto_on_nonzero = true
```

```python
# C:/Users/me/.evi/check_prompt.py
import os, re, sys

prompt = os.environ.get("EVI_HOOK_ARGS_JSON", "")
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

- **Fail-open by design.** A missing or malformed `hooks.toml` is treated as "no hooks", not an error. One bad entry is skipped with a warning while the rest load. Plugin-hook scanning failures never break your core hooks. Lifecycle hook execution is wrapped so that exceptions return "no veto" rather than crashing the turn.
- **Commands are not shell-evaluated.** `command` is argv. A bare-string `command` becomes a one-element argv list — it is **not** split on spaces or expanded. For pipes, globs, redirects, or `$VAR` expansion, call a shell explicitly (`["bash", "-c", "…"]`) — note this means `bash`-based examples need a shell available (use the Python form on Windows).
- **Timeouts.** Default `timeout` is `30.0` seconds (subprocess or HTTP). A timed-out hook reports exit code `124`; a command that fails to exec reports `126`. For a vetoing before-hook, a timeout therefore counts as a non-zero exit and **blocks** the action — keep guard hooks fast and set a tight `timeout`.
- **`after_tool_call` and `stop` never block.** Their veto is ignored; a non-zero `after_tool_call` exit is only logged.
- **4 KB result cap.** `EVI_HOOK_RESULT` (and the `result` field in a URL POST) is truncated to 4 KB to avoid blowing OS env/arg limits. Don't rely on receiving the full tool output.
- **Permission first, then hooks.** Tool calls pass through eVi's permission / auto-approve gate before before-hooks run; a denied tool never reaches a hook.
- **`EVI_HOOK_TOOL` for lifecycle events is the event name**, not a tool — there is no tool involved. For tool events it's the fully-qualified tool name (e.g. `write_file`, or `<server>.<tool>` for MCP tools).
- **Security.** Hooks run arbitrary local commands with your user's privileges and full environment. Treat plugin-supplied `hooks.toml` files as code: review a plugin's hooks before installing it, since its hooks are merged into your registry and execute on every matching event.
- **Reload requires a restart.** Hooks are loaded once at process start; edits to `hooks.toml` take effect on the next launch.
- See also the concise summaries in [docs/configuration.md](configuration.md#hooks--evihookstoml) and [docs/features.md](features.md#hooks). The authoritative behavior is in `evi/hooks.py`.
