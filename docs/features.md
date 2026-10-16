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

**Headless** — `evi run "prompt" [-m mode] [-y] [--format json] [--schema f]`.
One-shot for scripts/CI. Example: `git diff | evi run "review for bugs" -y`.
Nothing prompts: without `-y`, a call outside `[auto] auto_approve` is denied
(`[auto] unattended_policy`), and the JSON result names the run's `policy`.

**Batch** — `evi batch prompts.jsonl -o out.jsonl -j 4`. Runs many prompts (each
its own agent), optional parallelism. Input is `.jsonl`/`.json` objects
(`{prompt, id?, mode?, schema?}`) or one prompt per line.

**Variants** — `evi variants "prompt" -n 3` — N independent takes on one prompt.

**Steering** (Web) — type while a turn runs: Send becomes **Queue** and the
message is applied at the turn's next step instead of interrupting it
(`POST /api/session/{id}/steer`). If the turn ends first, it is sent as a normal
message. [Guide](features/steering.md).

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
`/forcetool <name>` per turn.

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
there); delegate CLI-agent backends are refused unless
`[llm] allow_cli_agent_unattended`.
[Guide](features/permissions-and-sandbox.md#unattended-runs).

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

Per-day JSONL transcripts (`tools.transcripts`). `evi sessions list|show|resume|
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
and lifecycle events (`user_prompt_submit`/`before_compact`/`stop`). See
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

## MCP

**Client**: `~/.evi/mcp.json` servers, `tools.mcp` on; `evi mcp list-tools`.
**Server**: `evi mcp serve` exposes eVi's tools/memory/commands over stdio+HTTP.

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
stats` aggregates sessions/tools/tokens from transcripts. **Crash reporting** —
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
**Worktrees** — `evi worktree` for parallel work. eVi's git calls ignore
what a repository's own config would run, so `evi worktree create` runs no
hooks (the repo's or your global post-checkout), no repo-local filter drivers,
and no Git LFS download — run `git lfs pull` in the new worktree if it uses
LFS. A repository whose `core.worktree` points outside it is refused. See
[Permissions & sandbox → Repositories you didn't
write](features/permissions-and-sandbox.md#repositories-you-didnt-write).
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
