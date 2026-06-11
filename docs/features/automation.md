# Recipes, Routines, Scheduled tasks, Channels

These four features make up eVi's **automation** surface. They let you save reusable workflows, fire them over HTTP, run prompts on a cron schedule, and push outside alerts into a live conversation — all while staying local-first and single-user.

## Overview

| Feature | What it is | Where it lives |
|---|---|---|
| **Recipes** | A saved, ordered list of prompts that run through **one shared conversation**, so later steps can build on earlier answers. | `~/.evi/recipes/*.toml` |
| **Routines** | A recipe bound to an unguessable webhook token. `POST /api/routine/<token>` runs the recipe headless, so an external service can kick off an eVi workflow over HTTP. | `~/.evi/routines.json` |
| **Scheduled tasks** | A saved prompt (or an eval suite) plus a cron expression. A background scheduler fires it on schedule and writes the output to a log file. | `~/.evi/scheduled/*.json` (one file per task), logs in `~/.evi/logs/scheduled/` |
| **Channels** | A way to push an external alert/notification into a **live web session** so the assistant sees it on its next turn. | In-memory on the running web session (not persisted) |

Use **recipes** for repeatable multi-step flows you run by hand (a morning standup, a release-notes draft). Promote a recipe to a **routine** when you want a cron box, IFTTT, a GitHub Action, or a home-automation hub to trigger it over HTTP. Use **scheduled tasks** for single recurring prompts (a daily digest) or to watch for model drift with **scheduled evals**. Use **channels** to nudge an already-open chat with a fresh fact (a build finished, a sensor tripped).

## How it works

### Recipes

A recipe is a TOML file with a `name`, an optional `description`, and one or more `[[steps]]`. Each step has a required `prompt` and an optional `label`. When you run a recipe, eVi builds a single agent and sends each step's prompt **in order through the same conversation**, so step 3 can refer to what steps 1 and 2 produced.

Recipes are loaded/validated by `evi/recipes.py`:
- A recipe with no `[[steps]]` (or a step missing a non-empty `prompt`) raises a `RecipeError`.
- The recipe name is slugified for the filename, which also blocks path traversal — `_slug()` reduces any name to its bare filename with `.toml` stripped.
- `list` silently **skips** malformed `.toml` files rather than aborting the whole listing.

### Routines

A routine (`evi/routines.py`) stores a `name`, the `recipe` it runs, an unguessable `token` (generated with `secrets.token_urlsafe(18)`), an `enabled` flag, and a `yes` flag. The token **is** the capability — anyone with it can trigger the routine.

The webhook endpoint `POST /api/routine/<token>` (in the web server):
- Bypasses the normal web auth token (external callers don't have it) and instead validates the path token with a constant-time compare (`secrets.compare_digest`).
- Returns `404` if the token matches nothing **or** the routine is disabled.
- Runs the recipe **headless** (no streaming UI) and returns JSON: `{"ok": true, "routine": <name>, "results": [{label, prompt, text, error}, …]}`.
- Permission policy: by default a routine runs **restricted** — only your auto-approved tool categories are allowed; everything else is **denied, never prompted** (`permission_callback` returns `False`). Set `yes` to auto-approve **all** tools for that routine (`agent.enable_auto_all()`).

### Scheduled tasks

A scheduled task (`evi/scheduled.py`) is one JSON file per task under `~/.evi/scheduled/`, stored separately so concurrent writers don't trample each other; writes are atomic (temp file + rename). Each task has an `id` (random hex), `name`, `cron` (`"min hour dom month dow"`), `prompt`, `kind` (`"prompt"` or `"eval"`), `enabled`, and run bookkeeping (`last_run`, `last_status`).

The scheduler (`evi/scheduler.py`) wraps APScheduler's `BackgroundScheduler`:
- On `start()` it schedules every **enabled** task. Tasks with an invalid cron string are **logged and skipped** (not fatal).
- When a job fires, eVi builds a **fresh one-shot `Agent`** (no history carries between runs), sends the prompt with a system note that says it's running unattended, captures the assistant text (plus a tool trace), and writes it to `~/.evi/logs/scheduled/<id>_<timestamp>.log`. The run status is recorded on the task (`"ok"` or `"error: …"`).
- For `kind="eval"`, the `prompt` field holds the **suite name**; the run executes the suite and records status as `ok (P/T)` or `fail (P/T)` so drift shows up in `evi schedule list`.
- `misfire_grace_time` is 300s, so a job that's slightly late still fires.
- The scheduler runs from **either** the standalone `evi scheduler` daemon **or** automatically inside `evi web` (started in the app's lifespan). If APScheduler isn't installed, the web app logs that the scheduler didn't start and keeps serving — it does **not** crash.

### Channels

Channels are the lightest mechanism. `POST /api/session/<id>/channel` with a JSON body `{"text": "...", "source": "..."}` appends a system note (`[channel:<source>] <text>`) to that live session's history so the assistant sees it on its next turn, and records it in the session's channel log. The matching `GET` returns recent channel messages (for a UI badge). The sender authenticates with the **normal web token** (unlike routines). Channel context is **live-session only** — it is **not** persisted across reloads.

## Setup

### eVi home and config file

Everything lives under `~/.evi/` (override the whole location with the `EVI_HOME` environment variable). The primary config is `~/.evi/config.toml`. Tool toggles in `[tools]` (e.g. `memory`, `skills`, `mcp`) affect what scheduled tasks and routines can do, since each builds an agent from your current config.

Paths created/used by these features:

| Path | Used by |
|---|---|
| `~/.evi/recipes/*.toml` | Recipes |
| `~/.evi/routines.json` | Routines |
| `~/.evi/scheduled/*.json` | Scheduled tasks (one file each) |
| `~/.evi/logs/scheduled/` | Scheduled-task output logs |

### Optional pip extra (scheduler only)

The scheduler dependency is **optional** and imported lazily. Recipes, routines, and channels need no extra. To run scheduled tasks, install the `scheduler` extra (pulls in `apscheduler>=3.10`):

```bash
pip install 'evi-assistant[scheduler]'
```

If you try `evi scheduler` (or rely on `evi web`'s built-in scheduler) without it, you get a clear message: `scheduler requires apscheduler — install with: pip install 'evi-assistant[scheduler]'`.

### Defaults

- New routines: `enabled = true`, `yes = false` (restricted permissions).
- New scheduled tasks: `enabled = true`, `kind = "prompt"` (unless you pass `--eval` or `--disabled`).
- Channel `source` defaults to `"channel"` and is truncated to 64 chars.

## Usage

### Recipes — `evi recipe`

```text
evi recipe new <name> [--overwrite]   # write a starter ~/.evi/recipes/<name>.toml
evi recipe list                       # list saved recipes (name, step count, description)
evi recipe show <name>                # print a recipe's numbered steps
evi recipe run <name> [--yes | -y]    # run the steps in one shared conversation
```

`evi recipe run` streams each turn (text + tool activity) to the console. `--yes`/`-y` auto-approves every tool call for an unattended run.

#### Web / Desktop — the Routes & Recipes panel

The web and desktop apps expose recipes and multi-model routes together under
**Settings → Routes & Recipes**:

- **Recipes** — each saved recipe is listed with its steps; a **Run** button
  runs the recipe headless server-side (`POST /api/recipes/run`, one shared
  agent across steps, auto-approved) and shows each step's output inline.
- **Routes** — add / list / remove the multi-model routing rules in
  `~/.evi/routes.json` (`GET`/`POST /api/routes`, `POST /api/routes/remove`),
  the same store the CLI's `evi route` commands manage. Each rule maps a set of
  keywords to a model; the default and classifier models are set under
  Settings → Model & Backend.

Authoring still happens in TOML for recipes (`evi recipe new`), but everyday
browsing and running no longer needs the CLI.

**Route indicator.** When routing is on (`[llm] router_enabled = true`), each turn
emits a `RouteInfo` event and the **model chip** (footer) shows the model that
actually handled it — e.g. `qwen2.5-coder:14b-instruct-q4_K_M (code)` for a turn
that matched the `code` route, or just the default model for an unmatched turn.
The session **mode** (Chat / Cowork / Code) is shown separately by the mode
switch. A common "best of both" setup: keep a chat model as the default and add a
`code` route to a coder model —

```bash
evi models use qwen2.5:14b-instruct-q4_K_M           # chat default
evi route add code --model qwen2.5-coder:14b-instruct-q4_K_M \
  --keywords "code,debug,refactor,traceback,pytest,regex,function"
evi route enable                                     # router_enabled = true
```

### Routines — `evi routine`

```text
evi routine add <name> --recipe <recipe> [--yes] [--overwrite]   # -r is short for --recipe
evi routine list                                                 # names, target recipes, tokens
evi routine remove <name>
evi routine run <name>                                           # run locally, exactly as the webhook would
```

`add` validates that the recipe exists first, then prints the curl command to trigger it. `--yes` here makes the routine auto-approve **all** tools when triggered. The webhook itself is `POST /api/routine/<token>` against your running `evi web` server.

### Scheduled tasks — `evi schedule` and `evi scheduler`

```text
evi schedule add --name <name> --cron "<crontab>" (--prompt "<text>" | --eval <suite>) [--disabled]
evi schedule list                  # id, name, cron, last status, on/off
evi schedule remove <task_id>
evi schedule enable <task_id>
evi schedule disable <task_id>     # kept on disk, just won't fire
evi schedule run-now <task_id>     # run immediately, ignoring cron; prints the log path

evi scheduler [--reload-interval <seconds>]   # run the scheduler in the foreground (Ctrl-C to stop)
```

You must pass **either** `--prompt` or `--eval` (not neither). `evi scheduler` re-syncs jobs with disk every `--reload-interval` seconds (default 60), so `evi schedule add/remove/...` changes are picked up without a restart. If you run `evi web`, the scheduler starts automatically — you don't need a separate `evi scheduler` process.

### Channels — HTTP only

```text
POST /api/session/<session_id>/channel   body: {"text": "...", "source": "..."}
GET  /api/session/<session_id>/channel   -> {"messages": [...]}
```

Authenticate with your normal web token. `text` is required; `source` is optional.

## Examples

### Example 1 — a "morning standup" recipe, run by hand

Create the file and edit the steps:

```bash
evi recipe new morning-standup
```

`~/.evi/recipes/morning-standup.toml`:

```toml
name = "morning-standup"
description = "Calendar + commits, then a summary"

[[steps]]
label = "Calendar"
prompt = "What's on my calendar today?"

[[steps]]
label = "Commits"
prompt = "List my git commits from yesterday in this repo."

[[steps]]
prompt = "Write a 3-bullet standup from the two answers above."
```

Run it (the third step builds on the first two, since they share one conversation):

```bash
evi recipe run morning-standup --yes
```

### Example 2 — promote that recipe to a webhook routine

```bash
# Bind a routine to the recipe (restricted permissions by default):
evi routine add standup --recipe morning-standup

# eVi prints the trigger command, e.g.:
#   curl -X POST http://localhost:8000/api/routine/JX8s...redacted...

# Make sure the web server is running:
evi web

# Trigger it from anywhere (cron box, CI, IFTTT):
curl -X POST http://localhost:8000/api/routine/JX8s...redacted...
```

The response is JSON with one entry per step:

```json
{
  "ok": true,
  "routine": "standup",
  "results": [
    {"label": "Calendar", "prompt": "What's on my calendar today?", "text": "…", "error": null},
    {"label": "Commits",  "prompt": "List my git commits…",        "text": "…", "error": null},
    {"label": "",         "prompt": "Write a 3-bullet standup…",   "text": "…", "error": null}
  ]
}
```

If a tool the recipe needs isn't in your auto-approved categories, it's denied (not prompted). Add `--yes` when creating the routine (`evi routine add standup --recipe morning-standup --yes`) only if you trust it to auto-approve **all** tools.

### Example 3 — a daily scheduled digest

```bash
# Requires: pip install 'evi-assistant[scheduler]'
evi schedule add \
  --name "daily-digest" \
  --cron "0 9 * * *" \
  --prompt "Summarize today's top tech news in 5 bullets."

evi schedule list
# on  3f9a1c2d daily-digest  cron=0 9 * * * last=(never run)

# Test it right now without waiting for 9am:
evi schedule run-now 3f9a1c2d
# ran 3f9a1c2d → /home/you/.evi/logs/scheduled/3f9a1c2d_20260609_090000.log

# Let it fire on schedule (or just run `evi web`, which starts the scheduler too):
evi scheduler
```

### Example 4 — a scheduled eval (drift watch)

```bash
evi schedule add \
  --name "nightly-eval" \
  --cron "0 2 * * *" \
  --eval my-suite
```

The task's `kind` is `eval` and its `prompt` field holds the suite name. After it runs, `evi schedule list` shows a pass ratio in the status, e.g. `last=ok (12/12)` or `last=fail (10/12)`.

### Example 5 — push a channel alert into a live web session

```bash
curl -X POST http://localhost:8000/api/session/<session_id>/channel \
  -H "Authorization: Bearer $EVI_WEB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "CI build #482 finished: PASSED", "source": "ci"}'
```

Response: `{"ok": true, "source": "ci", "pending": 1}`. The assistant sees `[channel:ci] CI build #482 finished: PASSED` as a system note on its next turn.

## Notes / limits

- **Recipes share one conversation; scheduled tasks do not.** Recipe steps run in a single shared agent so they can reference each other. Each scheduled-task firing builds a fresh agent with **no** memory of prior runs.
- **Routine tokens are the only auth on the webhook.** Anyone with the token can trigger the routine — treat `~/.evi/routines.json` and the printed curl commands as secrets. The endpoint deliberately bypasses the web auth token so external callers can reach it; it uses a constant-time token compare.
- **Routines fail closed on permissions.** Unless a routine is marked `yes`, any tool that isn't in your auto-approved categories is **denied, never prompted** (no human is there to approve). Use `yes` only for routines you fully trust.
- **Scheduler is fail-open.** Missing APScheduler doesn't crash `evi web` — the scheduler simply doesn't start and the rest of the app keeps serving. Tasks with a bad cron string are logged and skipped, not fatal.
- **Disable vs remove.** `evi schedule disable` keeps the task file on disk; it just won't fire. `evi routine`'s `enabled = false` similarly makes the webhook return `404`.
- **Channels are ephemeral.** Channel pushes are live-session-only context and are **not** persisted across reloads; if the session is gone, `GET …/channel` returns an empty list.
- **Path-traversal safety.** Recipe names are slugified to a bare filename, and scheduled-task ids reject `/`, `\`, and `..`, so neither can write outside their directory.
- **Run scope of headless routines.** Routine runs (and `evi routine run`) use `run_recipe_headless`, returning per-step `{label, prompt, text, error}` — a failing step records its `error` but the remaining steps still run.

Source files: `C:\evi\evi\recipes.py`, `C:\evi\evi\routines.py`, `C:\evi\evi\scheduled.py`, `C:\evi\evi\scheduler.py`, with CLI commands in `C:\evi\evi\apps\cli\main.py` and HTTP endpoints in `C:\evi\evi\apps\web\server.py`.
