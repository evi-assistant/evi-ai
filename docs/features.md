# eVi feature reference

A catalog of everything eVi does — what each feature is, how to use it from the
CLI / REPL / Web, and where to configure it. Setup-heavy items link to
[configuration.md](configuration.md); surface coverage is in
[cli-parity.md](cli-parity.md); the Claude-Code comparison is in
[claude-code-comparison.md](claude-code-comparison.md).

> Conventions: **CLI** = `evi <cmd>`; **REPL** = a `/slash` command inside
> `evi chat`; **Web** = the browser/desktop UI. Everything is local-first and
> opt-in unless noted.

---

## Core chat

**Interactive chat** — `evi chat` (REPL) or the Web chat view. Streams tokens,
runs tools, shows a **working-status** bar (spinner + elapsed timer + live token
count) while a turn runs.

**Session modes** — Chat / Cowork / Code presets gate the tool set. REPL: pick
at launch; Web: the mode switcher; per-turn headless: `evi run -m code`.

**Headless** — `evi run "prompt" [-m mode] [-y] [--format json] [--schema f]`.
One-shot for scripts/CI. Example: `git diff | evi run "review for bugs" -y`.

**Batch** — `evi batch prompts.jsonl -o out.jsonl -j 4`. Runs many prompts (each
its own agent), optional parallelism. Input is `.jsonl`/`.json` objects
(`{prompt, id?, mode?, schema?}`) or one prompt per line.

**Variants** — `evi variants "prompt" -n 3` — N independent takes on one prompt.

## Tools

20+ built-in tools, each gated by a `[tools]` toggle in `config.toml` (Web:
Settings → Tools): `fs`, `code` (`run_python`, sandboxable), `shell`, `web`
(search/fetch), `image` (ComfyUI), `vision`, `memory`, `pdf`, `sqlite`, `index`
(semantic search), `git`, `ocr`, `calendar`, `computer`, `voice`, `mcp`,
`subagent`, `federation`. REPL: `/tools` lists active; `/notools`,
`/forcetool <name>` per turn.

**Sandboxed code** — `[tools] sandbox = true` runs `run_python` under bwrap
(Linux) / sandbox-exec (macOS): read-only FS + no network. Falls back gracefully.

## Permissions & guardrails

**Permission modes** — `[auto] mode = ask|accept_edits|plan|yolo` + an
`auto_approve` category list + first-match `rules` (`allow|deny <tool> [arg]`).
REPL `/auto`, `/plan`. **Trusted dirs/domains** auto-approve fs/web under given
paths/hosts. **MCP allowlist** (`tools.mcp_allow`) gates which servers load.

**Content guardrails** — `~/.evi/guardrails.toml`, three layers: regex
(block/redact), `[[judge]]` (the LLM classifies vs a policy), `[[classifier]]`
(a local HF model, `[moderation]` extra). `evi guardrails list|test`. See
[configuration.md](configuration.md#guardrails--eviguardrailstoml).

## Memory & context

**Memory** — the `memory` tool persists notes to `~/.evi/memory/`; supports
**tags**. Auto-recalled into context.

**Context management** — automatic compaction (`[llm] compact_*`). REPL
`/context` (`/ctx`) shows a per-bucket breakdown (system/you/assistant/tools);
Web: click the usage chip. `/compact` forces it.

**Predicted outputs** — `/predict <text|file>` speculative-decoding hint.

## Sessions

Per-day JSONL transcripts (`tools.transcripts`). `evi sessions list|show|resume|
fork|continue|handoff`; REPL `/recent`. **Cross-device handoff**: `evi sessions
handoff` → resume on another machine after `evi sync` (or open `/?session=<id>`).
**Checkpoints/rewind**: `evi rewind` (Web: rewind dialog) undoes file writes.

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
(built-in `explore`/`plan` + plugin `agents.toml`); `evi agents` lists profiles.
**Parallel research** — `parallel_research` tool fans out read-only explorers.
**Workflows** — `~/.evi/workflows/<name>.toml`, multi-step with parallel blocks +
`{step}`/`{var}` interpolation; `evi workflow new|run`. **Dispatch** (Web 🗂) —
lists live sessions + runnable workflows. **Federation** — delegate a task to a
trusted **peer eVi** (`~/.evi/peers.json`, `delegate_peer` tool, `[federation]
serve` to answer). **Code review** — `evi review --multi` multi-agent review.
**Multi-model routing** — `evi route` picks a model per turn. **Ultracode** —
`evi ultracode "<task>"` / `/ultra` / `/effort ultracode` runs one hard task
through an exhaustive decompose → fan-out solvers → adversarial verify →
synthesize pipeline ([guide](features/ultracode.md)).

## MCP

**Client**: `~/.evi/mcp.json` servers, `tools.mcp` on; `evi mcp list-tools`.
**Server**: `evi mcp serve` exposes eVi's tools/memory/commands over stdio+HTTP.

## Automation

**Recipes** — `evi recipe` saved multi-turn flows. **Routines** — webhook →
recipe (`evi routine`, `POST /api/routine/{token}`). **Scheduled tasks** —
`evi schedule add --cron … (--prompt | --eval <suite>)`; runs on cron, incl.
**scheduled evals** for drift watch. **Channels** — push an external alert into a
live web session (`POST /api/session/{id}/channel`).

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

**Settings** — full screen over `config.toml` (9 sections). **Multi-user** —
`[web] multi_user` + `~/.evi/users.json`; each user gets an **isolated
workspace** (sessions/transcripts/memory under `~/.evi/users/<name>/`).
**Deep links** — `evi://session/<id>` / `evi://workflow/<name>`; `evi link`.
**Desktop** — native menus, tray, **silent auto-updater** (with in-app progress),
first-run wizard. **Status line** — customizable REPL status (`[statusline]`).

## Observability

**OpenTelemetry** — opt-in traces/metrics around tool calls (`[telemetry]
traces/metrics` + `otlp_endpoint`, `[otel]` extra). **Local stats** — `evi
stats` aggregates sessions/tools/tokens from transcripts. **Crash reporting** —
opt-in Sentry (`[telemetry] crash_reports` + `dsn`, `[telemetry]` extra).

## Machine ops

**Sync** — `evi sync push|pull` git-syncs portable `~/.evi` state. **Backup** —
`evi backup`. **Profiles** — `evi profile` per-machine config overlays.
**Worktrees** — `evi worktree` for parallel work. **Doctor** — `evi doctor`
(Web: Help → Diagnostics). **Fine-tune export** — `evi finetune export` →
JSONL training set.

## Packaging & release

PyPI (`v*` tag, Trusted Publishing + **sigstore** signing), **Docker** image
(`docker.yml` → GHCR), desktop installers (`desktop-v*`, 3-OS), a reusable
**`evi-run` GitHub Action**, and **CodeQL + gitleaks** security scanning. See
[releasing.md](releasing.md). To build locally in one command (sidecar + Tauri),
run `scripts/build-desktop.{ps1,sh}` — see [self-build.md](self-build.md) for
developing/building eVi with eVi (the `EVI.md` bootstrap).
