# eVi vs Claude Code — feature comparison

How eVi's capabilities line up against the full [Claude Code docs](https://code.claude.com/docs/en/overview)
surface. The Claude Code column was reviewed on 2026-11-19 (Claude Code CLI
2.1.328, Claude Desktop v6.6395.1); the eVi column was re-checked against
eVi's code on 2026-11-19 (eVi 1.1.0 plus the unreleased phase-1 work). eVi is **local-first, single-user, privacy-first**,
so a number of Claude Code's cloud/enterprise features are intentionally out of
scope rather than "missing". What eVi plans next is in [roadmap.md](roadmap.md).
Other coding agents (Codex, OpenCode, OpenClaw, Hermes, Pi and more) are
compared in [competitors.md](competitors.md).

**Legend:** ✅ have · ⚠️ partial · ❌ gap (buildable, on the roadmap or a
candidate for it) · 🚫 not planned (philosophy mismatch / separate big track /
on the roadmap's [Not doing](roadmap.md#not-doing-this-cycle) list).

## Core

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Agentic loop + built-in tools | `evi/llm/agent.py`; dozens of built-in tools (count in HANDOFF.md's generated facts) | ✅ |
| Common workflows (explore/fix/refactor/test) | chat, `evi review`, recipes | ✅ |
| Interactive mode + keyboard shortcuts | `prompt_toolkit` REPL (tab-complete, history, `keybindings.toml`); a line REPL, not a full-screen TUI | ✅ |
| Goals (`/goal`: keep working until a condition is met) | `/goal` adds the goal to the prompt; no auto-continue or judge yet (`loop-tools-goal-loop`, planned) | ⚠️ |
| Plan mode (read-only research, approve a plan) | `/plan` refuses every tool; read-only research with an Implement handoff is planned (`plan-mode-and-todo-core`) | ⚠️ |
| Task list tools (TaskCreate/TaskUpdate) | — | ❌ planned (`plan-mode-and-todo-core`) |
| Code Mode (model writes a script that calls several tools; experimental, 2.1.323) | — | 🚫 programmatic tool calling is on the Not doing list |
| `ultrathink` keyword (one turn at the highest effort, 2.1.311) | `/effort` sets effort per session; no per-turn keyword | ⚠️ |
| Advisor (a stronger model consulted when stuck or before done) | `compaction_model`, `fast_model` and judge guardrails; no advisor | ❌ planned (goal-loop escalation) |
| Scheduled tasks | scheduler (APScheduler), `evi schedule` / `evi scheduler` (cron) | ✅ |
| Voice dictation (`/voice` into the prompt) | STT + CLI voice loop + file transcription; no mic in the web/desktop composer | ⚠️ |

## Sessions & conversation

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Name / resume / branch / switch | `evi sessions` (list/show/resume/continue/fork/title); edit/branch/reroll | ✅ |
| Checkpointing (rewind edits + convo) | file rewind (`evi rewind`, web dialog) + web convo edit/branch/reroll; no `/rewind` in the REPL; shell-made changes are not captured (in Claude Code neither) | ⚠️ |
| Continue / resume / fork | `evi sessions continue` / `fork` (Ph 71) | ✅ |
| Session recap (`/recap`) | — | ❌ planned (`faithful-resume-and-session-recall`) |
| Side questions (`/btw`, Desktop side chat pane) | — | ❌ |
| Remote control (drive a local session from a phone; replies stream live since 2.1.326) | Telegram chat channel (paired senders, own narrow toolset; [channels.md](features/channels.md)) + `evi sessions handoff` + remote-backend mode | ⚠️ one message = one turn; no live attach to a running session |
| Send a message while it works | steering: queued, applied at the turn's next step ([steering.md](features/steering.md)) | ⚠️ web/desktop only |
| Stop a running turn | Stop button (web/desktop, `POST /api/session/<id>/cancel`), Ctrl+C in `evi chat` and `evi run` (exit 130); commands' process trees are killed ([steering.md](features/steering.md#stopping-a-turn)) | ✅ |
| Import from other tools (`/import` codex/gemini/cursor: instructions, MCP, commands, skills) | `evi sessions import` (Claude Code + Codex session logs → eVi sessions) | ⚠️ sessions only, no config import |
| Deep links (open from a URL, prefilled prompt) | `evi://session/<id>`, `evi://workflow/<name>`, `evi link` | ⚠️ navigation only, no prompt prefill |
| Archive, storage view, auto-cleanup | `tools.cleanup_period_days` + `evi sessions purge` | ⚠️ no archive or storage view |

## Permissions & safety

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Permission modes (ask/accept-edits/plan/yolo) | `auto.mode` (Ph 66) | ✅ |
| Fine-grained allow/deny rules | `auto.rules`, matched per command in a shell line; category rules (`allow shell`) | ✅ |
| "Don't ask again" grants from the prompt | per-call or whole-session only | ❌ planned (`approvals-v2`) |
| Auto mode (classifier reviews each action; default start mode since 2.1.328) | — | 🚫 local-judge auto-review is on the Not doing list |
| `denyInsteadOfPrompting` (2.1.319) | `unattended_policy = deny` for runs with no UI; interactive sessions always ask | ⚠️ |
| `blockWritesOutsideWorkingDirectories` (2.1.314) | the implementer policy and `--confine` scopes confine writes; no general switch for interactive sessions | ⚠️ |
| Workspace trust (trust a folder before using its config; prompt names what loads since 2.1.326) | `evi trust`; an untrusted repo's `.evi.toml` is ignored, and a trusted one can only tighten security | ✅ |
| Protected files (instructions, settings, CI) | built-in protected paths ask in every mode (`EVI.md`, `.evi.toml`, `.github/workflows/**`, …) | ✅ |
| Auto-mode config (trusted dirs / domains) | `auto.trusted_dirs` + `trusted_domains` (Ph 77) | ✅ |
| Sandboxed bash/code | `[tools] sandbox` (bubblewrap / Seatbelt) | ⚠️ none on Windows; whole filesystem readable |
| Worktree safety (refuse shared-branch rewrites, ask before removing dirty worktrees) | destructive-command guard (force push, `reset --hard`) above every mode | ⚠️ |
| Security / vuln review | guardrails, `security.yml` (pip/cargo audit), `review --multi` | ✅ |
| Content moderation / safety filter | `guardrails.toml` — regex (block/redact) **+ LLM-judge + offline ML classifier + dedicated guard model** (Llama Guard / ShieldGemma) on input/output | ✅ (no in-harness equivalent in Claude Code) |

## Extension & customization

| Claude Code | eVi equivalent | Status |
|---|---|---|
| CLAUDE.md / memory | `EVI.md`/`AGENTS.md` project context + memory (+ tags) | ✅ |
| AGENTS.md fallback, `.claude/rules`, `@import` | EVI.md and AGENTS.md only | ⚠️ planned (`instructions-and-skills-interop`) |
| Nested CLAUDE.md (large codebases) | merges EVI.md/AGENTS.md up the tree (Ph 76) | ✅ |
| Skills | `SkillStore` | ✅ |
| Subagents | `delegate_*` + `parallel_research` (Ph 61) | ✅ synchronous |
| Fork / background subagents, reopen finished ones (2.1.302, 2.1.319) | — | 🚫 background subagents deferred (single GPU slot) |
| Output styles | `[llm] output_style` (Ph 69) | ✅ |
| Hooks (30+ events; command/http/prompt/agent; rewrite input, answer permissions; `onFailure: block`) | `hooks.toml` (command/`url`; tool + lifecycle events: user_prompt_submit/before_compact/stop/session_start/session_end; conditional `arg_match`); observe and veto only, guards fail closed | ⚠️ no input rewrite or permission decisions |
| Mods (in-process UI and behaviour plugins) | — | 🚫 in-process middleware is on the Not doing list |
| Status line | `[statusline]` (Ph 72) | ✅ REPL only |
| Keybindings | `keybindings.toml` + `evi keybindings` (Ph 82) | ✅ |
| Skills tool-scoping (allowed/disallowed-tools) | SKILL.md `allowed-tools`/`disallowed-tools` scope the rest of the turn the skill is used in: other tools are refused at dispatch, subagents inherit it (`evi/skillscope.py`) | ✅ |
| Custom slash commands | `~/.evi/commands` frontmatter/$ARGS/@file/namespacing (Ph 62) | ✅ |
| Nested skills (subfolders) | recursive `SKILL.md` scan | ✅ |
| `/add-dir` (extra working dirs) | `/add-dir` → session trusted_dirs | ✅ no trust prompt |
| `!cmd` shell passthrough | `!cmd` in the REPL (output folded into context) | ✅ |
| AskUserQuestion (clarifying Qs) | `ask_user` tool (terminal REPL only; web/headless say "ask in your reply") | ⚠️ |

## Plugins & MCP

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Plugins (bundle commands/skills/agents/hooks) | `evi plugin add/init/list/remove`; bundles commands+skills+hooks+MCP+**subagent profiles**; installs from dir / git / **.zip** / URL; **`bin/` on PATH** | ✅ |
| Plugin scaffolding | `evi plugin init <name>` (starter command + skill) | ✅ |
| Plugin marketplaces / discovery | `evi plugin search/install` + `[plugins] index_urls` (curated index) | ⚠️ no versions, pinning or update (`ecosystem-extension-supply-chain`) |
| Plugin evals (`claude plugin eval`, no-plugin baseline) | `evi eval` runs without skills, hooks or MCP | ❌ |
| MCP client (connect to servers) | `MCPManager`, `mcp.json` (tools only; no OAuth, resources or reconnect) | ✅ |
| MCP server (publish eVi's tools) | `evi mcp serve` (stdio + HTTP) | ✅ |
| Managed MCP (allowlists + output cap) | per-tool allow on publish + `tools.mcp_allow` (Ph 78) + `tools.mcp_max_output_chars` | ✅ |

## Platforms

| Claude Code | eVi equivalent | Status |
|---|---|---|
| CLI | Typer CLI | ✅ |
| Desktop app | Tauri desktop (menus/tray/updater) | ✅ |
| Desktop panes (diff/Changes card, terminal, browser, previews, iOS simulator) | files-changed footer and rewind dialog | ❌ changes panel planned (`ux-surfaces-changes-panel`); 🚫 terminal/preview panes |
| Web | FastAPI + SSE web UI | ✅ |
| Turns that survive a closed tab (cloud sessions "catching up", reattach) | a turn is tied to one HTTP stream | ❌ planned (`server-owned-turns-and-input`) |
| Headless / print mode | `evi run` (Ph 65): `--format` `text`, `json` or `stream-json`, `--max-turns`, `--cwd`, `--model`, `--backend`, `--resume` / `--continue`; exit code says why it stopped ([structured-and-batch.md](features/structured-and-batch.md#the-run-report)) | ✅ |
| VS Code / JetBrains extensions | VS Code extension (`editors/vscode/`: local ghost-text completion + a chat panel); JetBrains via `evi mcp serve`; an ACP agent is planned (`acp-server`) | ⚠️ VS Code only |
| Mobile / web cloud platform | Telegram channel for the phone; no hosted platform | ⚠️ phone via Telegram; 🚫 cloud |
| Computer use | `computer` tool | ✅ |
| Chrome / browser automation | — | 🚫 deferred for MCP browser servers |
| Terminal status (OSC 7501) and notifications | `[notify]` on turn completion | ⚠️ no needs-input signal (`steer-stop-surfaces`) |
| Slack integration (Claude Tag, approval cards) | — (Telegram is the one chat channel) | ❌ not before the channel runtime ([roadmap.md](roadmap.md)) |

## Review · planning · orchestration

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Code review (multi-agent; severity labels and suggested fixes since 2.1.328) | `evi review --multi` (four lenses, BLOCKING/SUGGESTION) | ✅ no finding verification |
| Run agents in parallel | `parallel_research` (Ph 61) | ✅ |
| Worktrees (isolate parallel sessions) | git worktrees + `[worktree] base_ref` default | ✅ |
| Dynamic workflows (script many subagents; pause at usage limits) | recipes + workflows + ultracode | ✅ TOML DAG + fixed pipeline |
| Agent teams (shared task list) | `evi team` — lead decomposes, teammates claim+drain (`evi/teams.py`) | ✅ |
| Agent view (live dispatch dashboard) | Dispatch panel + `GET /api/dispatch/stream` (SSE, busy dots) | ✅ |
| Background commands and Monitor | `monitor` (300 s cap), `run_command` (600 s cap); no background processes | ❌ planned (`loop-tools-background-processes`) |
| PR monitor with CI status and auto-fix (Desktop) | — | ❌ |
| Ultrareview (CI gate) | `evi review --multi --exit-code` / `--json` + `/ultrareview` | ✅ local |
| Ultraplan (cloud) | local plan mode | 🚫 cloud |

## Automation & triggers

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Routines (schedule / webhook trigger) | scheduler + `evi routine` webhook→recipe (Ph 73) | ✅ |
| Channels (push alerts into a session) | `POST /api/session/{id}/channel` — note by default, or `run:true` drives a live agent turn | ✅ |
| GitHub Actions / GitLab CI | packaged `evi-run` composite action + ready `examples/github/pr-review.yml` | ✅ no PR comments, no GitLab template |

## Config · models · cost

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Settings (global + project) | config + profiles + `.evi.toml` (Ph 74) + settings UI | ✅ |
| Env vars / `.claude` dir | `EVI_*` env + `~/.evi` | ✅ |
| Model config / aliases / fast mode | model picker, routing, `fast_mode` | ✅ |
| Fallback model | `[llm] fallback_models` (retry the turn down the chain on a setup failure) | ✅ no retry of a broken stream (`stream-loop-resilience-reasoning`) |
| Extended thinking on/off | `reasoning_effort` off/low/medium/high/max (`/effort`); thinking that servers send beside the answer shows as thinking (unreleased) | ✅ |
| Transcript retention (cleanupPeriodDays) | `tools.cleanup_period_days` + `evi sessions purge` | ✅ |
| Prompt caching | `cache_prompt` | ✅ |
| Compaction (auto-compact; idle compaction since 2.1.299) | structured, incremental compaction; `/compact <focus>`; `compaction_model`; web "compact up to here" | ✅ no idle compaction |
| Context window display | usage chip + status line + `/context` per-bucket breakdown, system prompt by part | ✅ |
| Cloud backends (Bedrock/Vertex/Foundry) | `openai_compat` (covers gateways/proxies); `gemini` via an API key or Vertex AI | 🚫 no native Bedrock/Foundry, by design |
| Use a subscription instead of an API key | CLI-agent backends: `claude_agent` (Claude Max/Pro), `codex` (ChatGPT), `amp`, `qwen`, `copilot` | ✅ eVi-unique |
| Run a local model with no setup | managed llama.cpp runtime: `evi runtime setup`, model catalog, GPU switch ([runtime.md](features/runtime.md)) | ✅ eVi-unique |
| Cost management / analytics dashboards | token usage shown | 🚫 local = free |

## SDK & programmatic

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Headless mode | `evi run --format json` (the versioned run report: session, turns, `stop_reason`, tool calls, usage, `files_changed`) and `--format stream-json` (NDJSON events in the web stream's envelope, `RunResult` last) (Ph 65) | ✅ |
| Custom tools | `@tool` decorator | ✅ |
| File checkpointing | `evi rewind` (Ph 64) | ✅ |
| Structured outputs | `/json` + JSON-Schema (`/schema`, `evi run --schema`) | ✅ backend-enforced, no client-side validation |
| Batch API | `evi batch <file>` → JSONL (parallel) | ✅ |
| Evals | `evi eval` (assertions + LLM-as-judge; `--eval` on a schedule) | ✅ |
| Usage analytics | `evi stats` (local; sessions/tools/**by-category**/busy days) | ✅ local-only |
| Transcript search (Ctrl+R / resume) | `evi sessions search <query>` (snippets) | ✅ |
| Responses API built-in tools | `[llm] responses_tools` (web_search/code_interpreter/…) | ✅ opt-in |
| Multi-user / teams | `[web] multi_user` + `users.json` (per-user tokens + isolated sessions/transcripts/memory) | ✅ opt-in |
| Federation (agent↔agent across machines) | `evi peer` / `delegate_peer` / `/api/federate` | ✅ eVi-unique |
| Permissions SDK | permission policy (Ph 66) | ✅ |
| Public Agent SDK (library) | `evi.sdk` — curated re-export + `build_agent()` + examples ([sdk.md](sdk.md)) | ✅ |
| Session storage (S3/Redis) | local JSONL transcripts | 🚫 local-first |
| OpenTelemetry / monitoring | opt-in OpenTelemetry traces + metrics for tool calls (`[telemetry]`, `[otel]` extra) + crash reporting (Sentry-compatible) | ⚠️ opt-in; no LLM-call spans |
| Tool search (1000s of tools) | `[tools] tool_search` defers the long tail behind a `search_tools` meta-tool | ✅ opt-in |
| LSP tool (definitions, references, diagnostics) | `check_file` diagnostics and format-on-edit, `python_symbols` outline | ❌ |

## Org / enterprise / compliance

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Admin setup, server-managed settings, team onboarding | — | 🚫 single-user |
| Authentication (accounts/SSO) | web auth token; local backends need none | 🚫 local |
| ZDR / legal / data-usage | private by design (everything local) | 🚫 N/A |
| Cloud sessions, self-hosted runners, Projects, Artifacts | — | 🚫 cloud |

## Summary — what changed in this review (2026-11-19)

Claude Code shipped 2.1.294 to 2.1.328 between 2026-10-08 and 2026-11-19.
New rows above cover Code Mode, auto mode as the default start, `ultrathink`,
`denyInsteadOfPrompting`, blocking writes outside the working folders, idle
compaction, background and fork subagents, the Desktop side chat and Changes
card with PR monitor, OSC 7501 status and Remote Control streaming. Several
eVi rows were corrected to ⚠️ where the earlier table overstated them: `/goal`
(a prompt prefix), plan mode (refuses every tool), steering (web and desktop
only), `ask_user` (terminal only), hooks (veto only), the sandbox (none on
Windows) and voice (no microphone in the web app).

eVi 1.1.0 (2026-10-31) shipped all of Phase 0 (folder trust, per-command
shell rules, protected paths, the SSRF guard, web and desktop hardening,
unattended runs that fail closed), the eVi-fixes-eVi loop (task queue,
implementer policy, verify gate, director attestation, evidence, eval gates)
and Phase 2's small-model work (tool-call repair, edit recovery, loop guard,
token budget, the tool-result pipeline, structured compaction). Unreleased so
far: the system prompt by part in `/context`, "compact up to here", recall
evals and thinking read from servers that send it beside the answer. The rest
is in [roadmap.md](roadmap.md).

## Summary — since 1.0 (1.0.1 → 1.0.53)

Released in 1.0.1 to 1.0.53:

- **CLI-agent backends** — six backends route a turn through a coding CLI you
  already have, on its own subscription or login (Claude Code, Codex, Gemini
  CLI, Amp, Qwen Code, GitHub Copilot CLI).
- **Zero-config local AI** — the managed llama.cpp runtime: download, catalog,
  GPU, your own build.
- **First run that proves it works** — ranked options, a verified switch with
  rollback, starter prompts.
- **Phone and history** — the Telegram chat channel; `evi sessions import`
  from Claude Code and Codex; steering a running turn.
- **Security fixes** — 1.0.53 is a security release (see
  [CHANGELOG.md](../CHANGELOG.md)).

## Summary — specialty SLMs + opencode/Cursor gleanings (0.34.0)

Beyond Claude Code, eVi pulled in the genuinely-local wins from **opencode** and
**Cursor** and added small **specialty models**:

- **Specialty SLMs** — `[models]` ocr/vision/stt/tts/**guard/diarize/doc_layout**
  registry; `describe_image` + OCR-VLM routing (Moondream2 / Qwen2.5-VL / GLM-OCR),
  Kokoro TTS, faster-whisper turbo. A small model per task, no main-model swap.
  **(0.37.0)** added a **safety-guard** layer (Llama Guard / ShieldGemma →
  `[[guard]]` rule), **speaker diarization** (`evi voice diarize`, pyannote),
  and **document layout/OCR** (`ocr_image engine=doc`, Docling). **Capability
  chips** now cover 🔧 tools, 🛡 guard, ◆ embeddings/reranker (plus the existing
  👁 vision / 🧠 thinking / ⌨ infill / 🎤 audio).
- **Working folder** — per-session cwd (`/cd`, `--cwd`, web `📁` chip).
- **opencode core** — real **shell tool**, `apply_patch` (multi-hunk),
  format-on-edit + `check_file` diagnostics (LSP-lite), persistent `/plan` toggle,
  `evi init` (AGENTS.md, already discovered up-tree).
- **Cursor gleanings (local-first only)** — local **FIM completion** engine
  (`evi complete` / `/api/complete`) so eVi is a local Tab/Copilot backend;
  Bugbot-style review (`.evi/BUGBOT.md` + `evi review-remember` + severity).
  (`evi edit` already existed.) Cursor's cloud bits (Cloud Agents, remote PR
  Bugbot, Design Mode, Slack, enterprise) remain out by the local-first rule.
- **Deferred (deeper work):** a full language-server integration (eVi ships
  the lighter check_file/format-on-edit instead). The VS Code extension that
  renders the FIM completions as ghost-text has since shipped (`editors/vscode/`).

## Summary — S/M parity batch (0.33.0)

✅ **Closed in 0.33.0** (the "close every buildable small/medium gap" pass):

- **Model fallback chain** — `[llm] fallback_models` retries the turn down the chain on a setup failure.
- **Extended thinking off** — `reasoning_effort = "off"` (plus `/effort off`); centralized in `reasoning.py`.
- **Transcript retention** — `tools.cleanup_period_days` (auto-prune on startup) + `evi sessions purge`.
- **Transcript search** — `evi sessions search <query>` with snippets.
- **MCP output cap** — `tools.mcp_max_output_chars` truncates chatty tool results.
- **Conditional hooks** — `arg_match` gates a hook on tool arguments, not just the tool name.
- **Session lifecycle hooks** — `session_start` / `session_end`.
- **CI-gating review** — `evi review --multi --exit-code` / `--json` + `/ultrareview`.
- **Plugins** — `evi plugin init` scaffold; install from **.zip**/URL; `bin/` on PATH; recursive (nested) skill discovery; `/reload-skills`.
- **`/add-dir`** — trust an extra directory for the session.
- **`!cmd`** — REPL shell passthrough (output folded into context).
- **`ask_user` tool** — AskUserQuestion parity (interactive-only, graceful no-op elsewhere).
- **`worktree.base_ref`** — default fork point for `evi worktree create`.
- **Usage by category** — `evi stats` attributes tool calls per category.

✅ **Already shipped (0.31.0 → 0.32.0):** the full Phase 75–94 roadmap (plugin
skills/hooks/MCP/subagent-profiles, nested project context, trusted dirs/domains,
MCP allowlist, keybindings, channels, packaged CI action, cross-device handoff,
context-window breakdown, OpenTelemetry, fine-tune export, voice engines, CodeQL +
gitleaks, Docker→GHCR, sigstore signing), plus `evi://` deep links, the plugin
marketplace index, and the public **Agent SDK** (`evi.sdk`; see [sdk.md](sdk.md)).

⚠️ **Deferred (need an architectural addition or are low-value), not philosophy gaps:**
- **Nested subagent spawning** (subagents that spawn subagents) — deliberately gated as a runaway-cost guard.
- **Custom REPL themes** — terminal colour themes (output styles already cover response persona).

🚫 **Not planned (by design):** native cloud/enterprise backends (Bedrock/Foundry),
a JetBrains plugin, a mobile app (the phone path is the Telegram channel), an
agentic browser, hosted session storage, cost/analytics dashboards,
accounts/SSO/admin — eVi is local-first and single-user.
