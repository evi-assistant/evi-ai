# eVi feature reference

A catalog of everything eVi does — what each feature is, how to use it from the
CLI / REPL / Web, and where to configure it. Each area has a deep-dive guide in
[features/](features/README.md); setup-heavy items link to
[configuration.md](configuration.md); surface coverage is in
[cli-parity.md](cli-parity.md); the Claude-Code comparison is in
[claude-code-comparison.md](claude-code-comparison.md); what comes next is in
[roadmap.md](roadmap.md).

> Conventions: **CLI** = `evi <cmd>`; **REPL** = a `/slash` command inside
> `evi chat`; **Web** = the browser/desktop UI. Everything is local-first and
> opt-in unless noted.

---

## Core chat

**Interactive chat** — `evi chat` (REPL) or the Web chat view. Streams tokens,
runs tools, shows a **working-status** bar (spinner + elapsed timer + live token
count) while a turn runs.

**Session modes** — Chat / Cowork / Code presets gate the tool set. REPL: pick
at launch with `evi chat -m code` (an unknown mode exits 2); Web: the mode
switcher; per-turn headless: `evi run -m code`.

**Headless** — `evi run "prompt" [-m mode] [-y] [--format text|json|stream-json]
[--schema f] [--max-turns N] [--cwd DIR] [--model ID] [--backend NAME]
[--resume ID | --continue] [--policy implementer [--scope GLOB]]`. One-shot
for scripts/CI; with no prompt argument it reads the prompt from stdin.
Example: `{ echo "Review this diff for bugs:"; git diff; } | evi run -f json`. `--format json` prints the **run report**
(`schema_version` 1): session and transcript, backend, model, folder and its
trust state, turns used, `stop_reason` (the agent's own reason in
`done_reason`), the answer, every tool call (name, arguments, status,
duration, output, and a denied call's reason), the denied tools, usage summed
over the run, duration, `files_changed`, the project checks it ran (`checks`,
or `checks_skipped` saying why none ran) and the stop-hook blocks
(`stop_blocks`). `--format stream-json` prints one JSON event per line,
`RunStart` first and `RunResult` (the report) last, in the same versioned
envelope as the web UI's stream. `--cwd` runs in another folder (its
`.evi.toml` only if trusted), `--backend` picks a saved backend, `--resume` /
`--continue` add the run to a past session. The exit code says why the run
stopped: 0 done, 1 error, 2 usage error, 3 max_turns, 4 guardrail, 5
checks_failed, 6 stop_hook_exhausted, 130 interrupted (Ctrl+C or SIGINT,
which still prints the result). Nothing prompts: without `-y`, a call outside
`[auto] auto_approve` is denied (`[auto] unattended_policy`); the run still
ends `done` when the model finishes, and the report lists those calls in
`denied_tools`. `--policy implementer` (not with `-y`) lets a coding run edit
files in its folder and run tests, linters and read-only git there, and
nothing else; `--scope GLOB` names a protected file it may edit
([Implementer policy](features/permissions-and-sandbox.md#implementer-policy)).
`--evidence DIR` writes an [evidence bundle](features/evidence.md) once the
run is over.
[Guide](features/structured-and-batch.md#the-run-report).

**Verify before done** — once a turn has changed files and the model would
end it, eVi runs the project's checks (ruff on the changed files, the
targeted tests, `npm test`, `cargo check`, your `[checks] commands`) and
hands failures back to the model, up to `[checks] max_rounds` failing rounds,
then the turn ends `checks_failed`. On by default under `--policy
implementer`, and also in Code mode with `[checks] verify = "always"`; never
in an untrusted folder. Blocking stop hooks (`protocol = "json"`) run after the
checks. `evi run` prints each check on stderr; the web chat shows the
checks, a stop block as a note, and the files the turn's tools wrote in a
footer under its reply.
[Guide](configuration.md#verify-before-done--checks-in-configtoml).

**Batch** — `evi batch prompts.jsonl -o out.jsonl -j 4 [--max-turns N]`. Runs
many prompts (each its own agent), optional parallelism. Input is
`.jsonl`/`.json` objects (`{prompt, id?, mode?, schema?}`) or one prompt per
line. Each result line is the item's `id` and `prompt` plus its run report.

**Variants** — `evi variants "prompt" -n 3` — N independent takes on one prompt.

**Steering** (Web) — type while a turn runs: Send becomes **Queue** and the
message is applied at the turn's next step instead of interrupting it
(`POST /api/session/{id}/steer`). If the turn ends first, it is sent as a normal
message. [Guide](features/steering.md).

**Stopping a turn** — Web: **Stop** next to Send, or **Stop turn** on an open
permission prompt (`POST /api/session/{id}/cancel`); REPL: Ctrl+C (a second
one within 2 s quits); `evi run`: Ctrl+C / SIGINT. The turn ends with `Done`
reason `interrupted`: text so far is kept and marked as cut off, running
commands have their whole process tree killed, unanswered calls read
`(cancelled)`, and the session takes your next message at once.
[Guide](features/steering.md#stopping-a-turn).

## Models & backends

**First run** (Web/desktop) — when nothing answers yet, a first-run screen
offers what the machine already has, cheapest first: an AI coding CLI that is
installed, a local server that is running, an API key in the environment, then
a model download. Picking one is a **verified switch**: eVi sends a real
completion (and one with a tool), shows *Chat ✓ · Tools ✓*, and rolls the
switch back if it doesn't answer (`POST /api/backend/use` with `verify`,
`POST /api/backend/verify`). [Guide](features/onboarding.md).

**Managed local runtime** — eVi downloads a pinned llama.cpp build and a small
GGUF model, runs `llama-server` on loopback and points `[llm]` at it: no
Ollama, no account, no admin rights. `evi runtime setup|status|stop|gpu`;
`evi model list|use|remove` for the catalog; Web: Settings → Model & Backend →
Local models, with **Use your GPU** (CUDA on Windows/NVIDIA, Metal on macOS)
and support for your own `llama-server` (`[runtime] server_path`).
[Guide](features/runtime.md).

**Backends** — LM Studio, Ollama, llama.cpp, any OpenAI-compatible endpoint
(presets: `evi models preset`), and six CLI-agent backends that drive a CLI you
already use (`claude_agent`, `codex`, `gemini`, `amp`, `qwen`, `copilot`).
`evi backend add|list|use|remove` registers several and switches between them;
see [configuration.md](configuration.md#the-cli-agent-backends-at-a-glance).

## Tools

Dozens of built-in tools (the exact count is in HANDOFF.md's generated facts),
grouped in categories that each have a `[tools]` toggle in `config.toml` (Web:
Settings → Tools): `fs`, `code` (`run_python`, `python_symbols`, `check_file`,
`monitor`), `shell`, `web`
(search/fetch), `image` (ComfyUI), `vision`, `memory`, `pdf`, `sqlite`, `index`
(semantic search), `git`, `ocr`, `calendar`, `computer`, `voice`, `mcp`,
`subagent`, `federation`. REPL: `/tools` lists active; `/notools`,
`/forcetool <name>` per turn. `run_command`, `run_python` and `monitor`
commands run as a process tree with no stdin: a timeout or a stop kills the
whole tree and returns the output so far. A command that exits while a
background job it started still holds its output returns 2 s later; the job
keeps running, and `run_command`'s result says so.

**Sandboxed code** — `[tools] sandbox = true` runs `run_python`, `run_command`
and `monitor` commands under bwrap (Linux) / sandbox-exec (macOS): read-only FS +
no network. Falls back gracefully. A sandboxed `run_python` counts as reading
code, so the default policy runs it without asking.

## Permissions & guardrails

**Permission modes** — `[auto] mode = ask|accept_edits|plan|yolo` + an
`auto_approve` category list + first-match `rules` (`allow|deny <tool> [arg]`).
REPL `/auto`, `/plan`. Each call is checked by what it does: reading code
(`code_read`, in the default list) runs unasked, running it (`code`: `run_python`,
linters that run project code) asks, and a shell line is `shell` whatever tool
runs it (`monitor` commands too). `evi lint`, `evi doctor` and Settings →
Permissions offer a one-line fix for a list still holding the old default's
`code`. **Trusted dirs/domains** auto-approve fs/code_read/web under given
paths/hosts. **MCP allowlist** (`tools.mcp_allow`) gates which servers load.
**Unattended runs** (scheduled tasks, `evi run`, subagents with no UI, web
workflow and recipe runs, SDK agents without a permission callback) deny what
would ask unless `[auto] unattended_policy = "allow"`; federation, A2A, routine
webhooks without `yes` and channel pushes always deny it and never run code by
category (only an explicit allow rule lets `run_python` or a shell line run
there); delegate CLI-agent backends are refused there, and by
`evi workflow run` (which still approves every tool), unless
`[llm] allow_cli_agent_unattended`; and the commands these runs start, like
sandboxed ones, get no `*_KEY` / `*_TOKEN` / `*_SECRET` variables.
[Guide](features/permissions-and-sandbox.md#unattended-runs).
**Implementer policy** — `evi run --policy implementer` (and every
`evi task start`) decides every call without asking: reads and edits inside
the run's folder (protected files only when a `--scope` entry names them;
`.evi/tasks/**`, `.git` and eVi's home never), shell lines whose every command
is on a short allowlist (pytest, ruff, `npm test`, `cargo check|test|clippy`,
read-only git) with arguments that stay in the folder, and nothing else: no
network, MCP, `run_python`, memory or subagents. In an untrusted folder it
runs no commands at all. A guardrail, not a sandbox: tests run the
repository's code, so the worktree is the blast radius.
[Guide](features/permissions-and-sandbox.md#implementer-policy).

**Secret redaction** — API keys and tokens a tool printed show as
`[REDACTED:<kind>]` in transcripts (from now on; older ones aren't
rewritten), tool-call displays (web events and `/history`, CLI tool lines and
permission prompts), `evi run`'s run report and `stream-json` events (tool
names, arguments and outputs, and errors), scheduled-task logs, subagent
traces and crash reports. Found by token shape and by the keys this machine
has (env `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD`, eVi's own config
files). The model's own context isn't redacted. [Guide](features/secret-redaction.md).

**Director attestation** — eVi never marks its own work verified: a task's
verdict counts only with an attestation signed by the director's key
(`evi/attest.py`, OS keyring or `~/.evi/director/`). In every mode an agent's
calls that touch that folder, set or clear `EVI_AGENT` / `EVI_DIRECTOR` or run
director tooling (`evi task verify|reject`, `evi attest`) are denied. Every
process an agent starts carries `EVI_AGENT=1`, and so does every process
eVi's agent-hosting commands start (`evi chat`, `evi run`, `evi web` and the
desktop app's server, `evi task start` …): director tooling and `evi trust`
refuse to run there. `evi attest init` makes the key; `evi task verify` and
`evi task reject` sign with it ([Task queue](#task-queue)).
[Security](security.md#the-directors-attestation-key).

**Content guardrails** — `~/.evi/guardrails.toml`, three layers: regex
(block/redact), `[[judge]]` (the LLM classifies vs a policy), `[[classifier]]`
(a local HF model, `[moderation]` extra). `evi guardrails list|test`. See
[configuration.md](configuration.md#guardrails--eviguardrailstoml).

## Memory & context

**Memory** — the `memory` tool persists notes to `~/.evi/memory/`; supports
**tags**. Auto-recalled into context.

**Context management** — automatic compaction (`[llm] compact_*`), cut at a
turn boundary and kept in the transcript, so a revived web chat resumes from
the summary. REPL `/context` (`/ctx`) shows a per-bucket breakdown
(system/you/assistant/tools); Web: click the usage chip. `/compact` forces it.

**Predicted outputs** — `/predict <text|file>` speculative-decoding hint.

## Sessions

Per-day JSONL transcripts (`tools.transcripts`), [redacted](features/secret-redaction.md)
as they are written. `evi sessions list|show|resume|
fork|continue|handoff|search|export|purge`; REPL `/recent`. **Cross-device handoff**:
`evi sessions handoff` → resume on another machine after `evi sync` (or open
`/?session=<id>`). A resumed session's tool results are paired with their
calls, so strict backends accept it. **Notes** (another session's message, a
channel push, the Dispatch panel) join a chat as framed user messages, wait for
a running turn to end, are kept in the transcript and show as notes in the web
chat. **Import**: `evi sessions import [--write]` brings your Claude
Code and Codex conversations into eVi's store (read-only on the source; a dry
run until `--write`). **Checkpoints/rewind**: `evi rewind` (Web: rewind dialog)
undoes file writes. [Guide](features/sessions.md).

## Skills, commands, styles

**Slash commands** — built-in `/cmd` controls plus `~/.evi/commands/<name>.md` →
`/name` templates ([guide](features/slash-commands.md)). **Skills** —
`~/.evi/skills/<name>/SKILL.md` instruction packets the model loads on demand via
`invoke_skill` ([guide](features/skills.md)). **Output styles** — `evi style` /
`[llm] output_style` layer a persona onto the system prompt. **Keybindings** —
`~/.evi/keybindings.toml` maps a key to a REPL slash command.

## Hooks

`~/.evi/hooks.toml` — run a `command` (argv) or POST a `url` around events.
Tool events (`before_tool_call`/`after_tool_call`, glob `match`, `veto_on_nonzero`)
and lifecycle events (`user_prompt_submit`/`before_compact`/`stop`). A
`[[stop]]` hook with `protocol = "json"` gets the event as JSON on stdin and
can send the model back to work (print `{"decision": "block", "reason": …}`
or exit 2), at most `[hooks] max_blocks` (3) times per turn, then the turn
ends `stop_hook_exhausted`; its errors never block
([guide](features/hooks.md#blocking-stop-hooks-protocol--json),
[example](../examples/hooks/pytest-gate.toml)). See
[configuration.md](configuration.md#hooks--evihookstoml).

## Plugins

`evi plugin add <dir|git-url>` installs a bundle under `~/.evi/plugins/<name>/`
that can carry `commands/`, `skills/`, `hooks.toml`, `mcp.json`, and
`agents.toml` (subagent profiles), all auto-discovered. **Marketplace**:
`evi plugin search <q>` / `evi plugin install <name>` resolve from
`~/.evi/marketplace.json` + `[plugins] index_urls`; `evi plugin index init|add`.

## Agents & orchestration

**Subagents** — the `delegate` tool runs a scoped sub-agent by profile
(built-in `explore`/`plan` + plugin/user `agents.toml`); `evi agents` lists them,
`evi agents new` scaffolds a custom one. A profile's tools are cut to your
`[tools]` toggles, `explore` is read-only, and a subagent asks through the chat
that started it. **Agent teams** — a shared, claimable
task list (`evi team new/run`): a lead decomposes a goal into dependency-ordered
tasks and teammates drain it in parallel (`evi/teams.py`).
**Parallel research** — `parallel_research` tool fans out read-only explorers.
**Workflows** — `~/.evi/workflows/<name>.toml`, multi-step with parallel blocks +
`{step}`/`{var}` interpolation; `evi workflow new|run`. **Dispatch** (Web 🗂) —
lists live sessions + runnable workflows. **Federation** — delegate a task to a
trusted **peer eVi** (`~/.evi/peers.json`, `delegate_peer` tool, `[federation]
serve` to answer). **Code review** — `evi review --multi` multi-agent review
with read-only reviewers.
**Multi-model routing** — `evi route` picks a model per turn. **Ultracode** —
`evi ultracode "<task>"` / `/ultra` / `/effort ultracode` runs one hard task
through an exhaustive decompose → fan-out solvers → adversarial verify →
synthesize pipeline; the solvers only read files, so it answers in text and
never edits ([guide](features/ultracode.md)).

## Task queue

**Tasks in the repository** — `evi task new --title … --acceptance …
--scope GLOB --check COMMAND` files a task in `.evi/tasks/<id>.toml`, tracked
in git. `evi task start <id>` (in a trusted repository) claims it with a
lease, runs it on branch `task/<id>` in `.worktrees/task__<id>` under the
implementer policy with the verify gate on, then runs the task's required
checks itself, writes the run report under `~/.evi/tasks/` and leaves the
task `implemented`, or `open` again (exit codes as `evi run`'s; 2 when
nothing ran). Nothing is committed: you review and commit the work on the
branch, `evi task submit` it, and only your `evi task verify` (after the
review gate, `evi review --multi --exit-code`) or `evi task reject --reason`
gives it a verdict, signed with the director key (`evi attest init`,
`evi attest status`). Both refuse in any process an eVi agent may have
started. `evi task list` and `evi task show` (`--format json`) show a
verdict only with a valid signature at the branch's current head; a
hand-edited or outdated one reads `unverified (…)`. `evi task claim`,
`evi task release` and `evi task reopen` manage leases and send work back.
An MCP client can file, start, steer and cancel tasks through
`evi mcp serve --categories agent`, never verify them.
[Guide](features/tasks.md).

**Evidence bundles** — every task run leaves a bundle under
`~/.evi/evidence/`: the diff against the task's base, eVi's own run of the
required checks (pytest's junit report included), a redacted transcript
excerpt and a `report.json` with a JSON Schema; the task records its path and
hash, and `evi task verify` signs that hash and refuses a bundle that changed.
`evi run --evidence DIR` writes one for a plain run; `evi evidence collect`
collects one on demand, `evi evidence commit-body` prints a task's commit
message with `Implemented-by`, `Task` and `Evidence` trailers (and
`Verified-by` / `Attestation` only from a valid signature), and
`evi evidence prune` removes old bundles (`[evidence] keep_days`, 30), never
one a task names. `evi stats --provenance` counts the commits, eVi's share and
review rounds in the history, counting only attested trailers.
[Guide](features/evidence.md).

## MCP

**Client**: `~/.evi/mcp.json` servers, `tools.mcp` on; `evi mcp list-tools`.
**Server**: `evi mcp serve` exposes eVi's tools/memory/commands over stdio+HTTP.
`--categories …,agent` adds the director's task API for one repository
(`task_create`, `task_start`, `run_status`, `run_steer`, `run_cancel`,
`run_result` …): runs one at a time as `evi task start` runs them, no tool
that gives a verdict, `--token` required over HTTP even on loopback, only
`memory`, `calendar` and `web` beside it, and never from the web app.
[Guide](features/mcp.md#the-agent-category-the-directors-task-api).

## Automation

**Recipes** — `evi recipe` saved multi-turn flows. **Routines** — webhook →
recipe (`evi routine`, `POST /api/routine/{token}`). **Scheduled tasks** —
`evi schedule add --cron … (--prompt | --eval <suite>)`; runs on cron, incl.
**scheduled evals** for drift watch. **Session channels** — push an external
alert into a live web session (`POST /api/session/{id}/channel`), as a note the
model reads as information, not instructions.

**Chat channels (Telegram)** — message eVi from your phone through a bot you
create. Off until enabled; an unknown sender gets a pairing code and no agent
turn until you approve it (`evi channel approve <code>` or Settings →
Channels). Only direct chats are answered unless `allow_groups`. Approved
senders get their own narrow toolset (`[channels] telegram_tools`, memory +
skills by default) under the channel's own policy, never `[auto]`: only the
`telegram_auto_approve` categories (none by default) and the channel's own
memory and skill reads run, everything else is denied, and memory is
recall-only by default (`memory_mode`).
`evi channel status|approve|revoke|serve`.
[Guide](features/channels.md).

## Structured output & evals

**Structured outputs** — JSON-Schema-constrained replies: REPL `/schema <file>`,
`evi run --schema`, Web `/api/chat output_schema`. **Evals** — `evi eval
new|run` runs prompt→assertion suites (contains/regex/equals + **LLM-judge**
rubric) with a pass-rate; exits non-zero to gate CI.

## Voice & vision

**TTS** — `[voice] engine = system|coqui|f5|piper`; `evi voice speak|engines`;
Web Settings → Voice; REPL `/speak` auto-speaks replies. **STT** — `evi voice
listen` (faster-whisper, `[stt]` extra); always-on `AutoListener`. **Vision** —
attach images (`/image`, Web 📎) to VLM models. **Image gen** — ComfyUI via the
`image` tool.

## Web & desktop

**Settings** — full screen over `config.toml`, its sections grouped under
Assistant / This computer / Extend / App, each with a **How this works**
explainer and, where there is live state, a health banner (backend answering,
missing tool dependencies, unreachable integrations or specialty models, an
open server surface). [Guide](features/web-desktop.md). **Access** — without
an access token the server answers only this machine; `evi web-config token
rotate` / `show` manage the token, and the desktop app (LAN access) and the
Docker image generate their own. A browser's state-changing request must come
from eVi's own pages (or carry the token), one turn runs per session, and an
unanswered permission prompt is denied after `[web] decision_timeout_s`.
[Security](security.md). **Multi-user** —
`[web] multi_user` + `~/.evi/users.json`; each user gets an **isolated
workspace** (sessions/transcripts/memory under `~/.evi/users/<name>/`, the
memory tools included), host-level settings are owner-only, and two logins
that would share a folder can't sign in. **Safe rendering** — replies render
as markdown with highlighting and mermaid diagrams from libraries that ship
with eVi (offline, pinned), through a sanitizer and a strict
Content-Security-Policy, so model output can't run script or make the page
load anything; the UI can't be framed by another site, and the desktop window
gets IPC only from the eVi server and stays on it.
**Deep links** — `evi://session/<id>` / `evi://workflow/<name>`; `evi link`.
**Desktop** — native menus, tray, an **updater that asks before it installs**
(Update & Restart / Not now), a core-only update channel with **Restart now**,
and the first-run screen. **Empty chat** — three starter prompts (Say hi / What
can you do? / Give me a quick tour). **Status line** — customizable REPL status
(`[statusline]`).

## Observability

**OpenTelemetry** — opt-in traces/metrics around tool calls (`[telemetry]
traces/metrics` + `otlp_endpoint`, `[otel]` extra). **Local stats** — `evi
stats` aggregates sessions/tools/tokens from transcripts; `evi stats
--provenance` reads the repository's git history instead
([Provenance](features/evidence.md#provenance-counts)). **Crash reporting** —
opt-in Sentry (`[telemetry] crash_reports` + `dsn`, `[telemetry]` extra).

## SDK (programmatic)

**Agent SDK** — `evi.sdk` is the curated public library surface: `build_agent()`
(the convenience constructor the CLI itself delegates to), the streaming `Agent`
+ all event types, `@tool` (a `ctx` parameter gets the calling session's
`ToolContext`), subagents, structured output, `run_headless`,
sessions/checkpoints, MCP, ultracode/workflows, and telemetry — all under one
stable namespace (`py.typed`). An agent built without a `permission_callback`
fails closed: pass one, or `unattended_policy="allow"`, for tools nobody
auto-approved. Guide: [sdk.md](sdk.md); runnable scripts:
[examples/python/](../examples/python/).

## Machine ops

**Sync** — `evi sync push|pull` git-syncs portable `~/.evi` state. **Backup** —
`evi backup`. **Profiles** — `evi profile` per-machine config overlays.
**Worktrees** — `evi worktree` for parallel work, in
`<repo>/.worktrees/<branch>` (kept out of `git status` through
`.git/info/exclude`). eVi's git calls ignore
what a repository's own config would run, so `evi worktree create` runs no
hooks (the repo's or your global post-checkout), no repo-local filter drivers,
and no Git LFS download — run `git lfs pull` in the new worktree if it uses
LFS. A repository whose `core.worktree` points outside it is refused. See
[Permissions & sandbox → Repositories you didn't
write](features/permissions-and-sandbox.md#repositories-you-didnt-write).
A worktree shares its repository's folder trust, and `EVI.md`, `.evi.toml`
and the anatomy come from the worktree alone. `evi worktree chat <branch>
[--mode chat|cowork|code]` opens a chat inside a branch's worktree.
**Isolated runs** keep an agent off the checkout you work in:
`evi run --worktree <branch|auto> [--base REF]` runs in a worktree and its
report says what it left there ([the run
report](features/structured-and-batch.md#the-run-report)); `evi task start`
runs a task on its own `task/<id>` branch ([Task queue](#task-queue));
`evi team run --worktree`, a workflow step's `isolate = "worktree"`
(`evi workflow run --worktree` for every step) and an `agents.toml` profile's
`isolate = "worktree"` (honoured by `delegate`) give each task a `fix-…`
branch and worktree of its own. A live isolated run keeps its worktree
locked, nothing is merged back, and runs isolated on a thread of their own
refuse delegate CLI-agent backends and go without `monitor`, the git tools
and MCP tools — see [Agents → Isolated
runs](features/agents.md#isolated-runs-git-worktrees). **`evi worktree prune
[--into BRANCH] [--dry-run]`** removes the worktrees under `.worktrees/` whose
work is safe elsewhere: unlocked, on a branch, clean (no uncommitted or
untracked files, whatever `status.showUntrackedFiles` says), holding no other
worktree, and merged into `--into` (default: `HEAD` of the folder you run it
in, so run it from the main checkout). It keeps every other one and says
why, removes without `--force` so git checks once more, and leaves branches
alone (delete a merged one with `git branch -d`); `--dry-run` (`-n`) only
lists what it would remove.
**Doctor** — `evi doctor` (Web: Help → Diagnostics); `evi doctor --dev`
checks a development checkout instead (venv, git `safe.directory`, test and
lint tools, `pytest --collect-only`, line endings) and prints fix commands
without running them — see [self-build.md](self-build.md).
**Folder trust** — `evi trust [PATH]` approves a repo's `.evi.toml` (exactly
the version you saw), `--status` / `--list` / `--revoke` inspect or undo it;
`evi chat` asks on first use, and untrusted folders ignore their project
config — see [Permissions & sandbox](features/permissions-and-sandbox.md).
**Fine-tune export** — `evi finetune export` → JSONL training set.

## Packaging & release

PyPI (`v*` tag, Trusted Publishing + **sigstore** signing), **Docker** image
(`docker.yml` → GHCR), desktop installers (`desktop-v*`, 3-OS), a reusable
**`evi-run` GitHub Action**, and **CodeQL + gitleaks** security scanning. A
`v*` tag publishes nothing until the browser e2e suite passes and the
CHANGELOG carries that version; the Docker image and desktop release follow
the PyPI publish. See [releasing.md](releasing.md). To build locally in one
command (sidecar + Tauri), run `scripts/build-desktop.{ps1,sh}` — see
[self-build.md](self-build.md) for developing/building eVi with eVi (the
`EVI.md` bootstrap).
