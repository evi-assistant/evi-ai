# Automate with hooks

Run your own command — or fire an HTTP webhook — automatically at key moments
while eVi works: around every tool call, and at three points in a turn. A hook
can just observe (audit a log, POST to Slack) or it can *veto* — block a tool,
reject a prompt, or skip compaction — all without touching eVi's source.

> **Uses:** config (`~/.evi/hooks.toml`) · **Applies to:** the CLI, web, and
> desktop, and any headless / workflow run — they all load the same registry.

## The seven events

A hook is an entry in `hooks.toml` keyed by the event it fires on. Get the name
exactly right: the loader **silently skips** an entry under an unknown event, so
a typo like `[[before_toolcall]]` looks fine but never fires.

| Event | Fires | Can veto? |
|-------|-------|-----------|
| `before_tool_call` | before a tool runs | yes — blocks the tool |
| `after_tool_call` | after a tool returns | no (notification only) |
| `user_prompt_submit` | before each turn, before the model sees the prompt | yes — blocks the prompt |
| `before_compact` | before history compaction | yes — keeps history intact |
| `stop` | after a turn completes | no (notification only) |
| `session_start` | when an interactive session begins (setup) | no (notification only) |
| `session_end` | when an interactive session ends (teardown) | no (notification only) |

The first two are **tool-scoped**: their `match` is a glob over the tool name
(`"*"`, `write_file`, `fs.*`). The five lifecycle events aren't tied to a tool,
so they use `match = "*"`.

## A real hooks.toml

Copy [`examples/hooks.toml`](https://github.com/evi-assistant/evi-ai/blob/main/examples/hooks.toml)
to `~/.evi/hooks.toml` (Windows: `%USERPROFILE%\.evi\hooks.toml`) and trim it.
The feature is always on — it's driven entirely by that file's presence, no
config flag, no pip extra.

```toml
# ~/.evi/hooks.toml

# Audit every tool call to a log.
[[before_tool_call]]
name    = "audit"
match   = "*"
command = ["bash", "-c", "echo $EVI_HOOK_TOOL $EVI_HOOK_ARGS_JSON >> ~/.evi/logs/tools.log"]
timeout = 5

# Block writes outside the home directory (veto).
[[before_tool_call]]
name            = "no-system-writes"
match           = "write_file"
command         = ["python3", "-c", "import os,sys,json; a=json.loads(os.environ['EVI_HOOK_ARGS_JSON']); p=os.path.realpath(a.get('path','')); sys.exit(0 if p.startswith(os.path.expanduser('~')) else 1)"]
veto_on_nonzero = true

# Notify an external service when a turn finishes (veto ignored on `stop`).
[[stop]]
name = "turn-done-webhook"
match = "*"
url  = "https://example.com/evi-hook"
```

Each entry sets **either** `command` (an argv list eVi spawns — **not**
shell-evaluated; use `["bash", "-c", "…"]` for pipes or `$VAR`) **or** `url`
(eVi POSTs JSON itself). A `command` hook inherits `EVI_HOOK_EVENT`,
`EVI_HOOK_TOOL`, and `EVI_HOOK_ARGS_JSON` in its environment (plus
`EVI_HOOK_RESULT` on `after_tool_call`).

## Vetoing

Veto only applies to the three vetoable events, and only when the entry sets
`veto_on_nonzero = true`. Such a hook that exits non-zero (or, for a `url` hook,
returns a `4xx`/`5xx`) blocks the action; for a tool, the model sees a
`BLOCKED BY HOOK '<name>': <stderr>` result. A timeout counts as non-zero and
blocks too — keep guard hooks fast and set a tight `timeout` (default `30.0`s).

## Verifying it fires

Hooks load once at process start, so **restart after editing**. Then inspect
without running anything:

```bash
evi hooks path                 # print the config file path
evi hooks list                 # every loaded hook, grouped by event
evi hooks test write_file      # which hooks WOULD fire for a tool name
```

`hooks test` is match-resolution only — it flags the hooks whose glob hits the
name (and which can veto) without executing them. If a hook you wrote is missing
from `hooks list`, suspect a mistyped event name first. The web/desktop
**Settings → Hooks** editor validates the whole file on save and catches those
typos before writing.

## Where to go next

- [Guard an agent's inputs and outputs](guardrails.md) — a purpose-built policy
  layer (regex / judge / classifier) that complements a `user_prompt_submit` hook.
- [Review a pull request in CI](headless-ci-review.md) — hooks fire in headless
  runs too, so the same audit and guard rules apply unattended.
