# eVi vs Claude Code — feature comparison

How eVi's capabilities line up against the full [Claude Code docs](https://code.claude.com/docs/en/overview)
surface (reviewed 2026-06-16, eVi 0.33.0). eVi is **local-first, single-user, privacy-first**,
so a number of Claude Code's cloud/enterprise features are intentionally out of
scope rather than "missing".

**Legend:** ✅ have · ⚠️ partial · ❌ gap (buildable, on the roadmap) · 🚫 not
planned (philosophy mismatch / separate big track).

## Core

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Agentic loop + built-in tools | `evi/llm/agent.py`; 20+ tools | ✅ |
| Common workflows (explore/fix/refactor/test) | chat, `evi review`, recipes | ✅ |
| Interactive mode + keyboard shortcuts | `prompt_toolkit` REPL (tab-complete, history, `keybindings.toml`) | ✅ |
| Goals | `/goal`, `agent.goal` | ✅ |
| Scheduled tasks | scheduler (APScheduler), `evi scheduled` | ✅ |
| Voice dictation | STT + voice loop | ✅ |

## Sessions & conversation

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Name / resume / branch / switch | `evi sessions` (list/show/resume/continue/fork/title); edit/branch/reroll | ✅ |
| Checkpointing (rewind edits + convo) | file rewind (Ph 64) + convo edit/branch/reroll | ✅ |
| Continue / resume / fork | `evi sessions continue` / `fork` (Ph 71) | ✅ |
| Remote control (drive from a phone) | remote-backend mode + multi-machine | ⚠️ no cross-device session handoff |
| Deep links (open from a URL) | — | 🚫 niche |

## Permissions & safety

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Permission modes (ask/accept-edits/plan/yolo) | `auto.mode` (Ph 66) | ✅ |
| Fine-grained allow/deny rules | `auto.rules` (Ph 66) | ✅ |
| Auto-mode config (trusted dirs / domains) | `auto.trusted_dirs` + `trusted_domains` (Ph 77) | ✅ |
| Sandboxed bash/code | `[tools] sandbox` (Ph 67) | ✅ |
| Security / vuln review | guardrails, `security.yml` (pip/cargo audit), `review --multi` | ✅ |
| Content moderation / safety filter | `guardrails.toml` — regex (block/redact) **+ LLM-judge + offline ML classifier** on input/output | ✅ (no in-harness equivalent in Claude Code) |

## Extension & customization

| Claude Code | eVi equivalent | Status |
|---|---|---|
| CLAUDE.md / memory | `EVI.md`/`AGENTS.md` project context + memory (+ tags) | ✅ |
| Nested CLAUDE.md (large codebases) | merges EVI.md/AGENTS.md up the tree (Ph 76) | ✅ |
| Skills | `SkillStore` | ✅ |
| Subagents | `delegate_*` + `parallel_research` (Ph 61) | ✅ |
| Output styles | `[llm] output_style` (Ph 69) | ✅ |
| Hooks (incl. HTTP hooks) | `hooks.toml` (command/`url`; tool + lifecycle events: user_prompt_submit/before_compact/stop/**session_start**/**session_end**; **conditional `arg_match`**) | ✅ |
| Status line | `[statusline]` (Ph 72) | ✅ |
| Keybindings | `keybindings.toml` + `evi keybindings` (Ph 82) | ✅ |
| Skills tool-scoping (allowed/disallowed-tools) | SKILL.md `allowed-tools`/`disallowed-tools` → `evi/skillscope.py` scopes the toolset while a skill is active | ✅ |
| Custom slash commands | `~/.evi/commands` frontmatter/$ARGS/@file/namespacing (Ph 62) | ✅ |
| Nested skills (subfolders) | recursive `SKILL.md` scan | ✅ |
| `/add-dir` (extra working dirs) | `/add-dir` → session trusted_dirs | ✅ |
| `!cmd` shell passthrough | `!cmd` in the REPL (output folded into context) | ✅ |
| AskUserQuestion (clarifying Qs) | `ask_user` tool (interactive-only; graceful no-op in web/headless) | ✅ |

## Plugins & MCP

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Plugins (bundle commands/skills/agents/hooks) | `evi plugin add/init/list/remove`; bundles commands+skills+hooks+MCP+**subagent profiles**; installs from dir / git / **.zip** / URL; **`bin/` on PATH** | ✅ |
| Plugin scaffolding | `evi plugin init <name>` (starter command + skill) | ✅ |
| Plugin marketplaces / discovery | `evi plugin search/install` + `[plugins] index_urls` (curated index) | ✅ |
| MCP client (connect to servers) | `MCPManager`, `mcp.json` | ✅ |
| MCP server (publish eVi's tools) | `evi mcp serve` (stdio + HTTP) | ✅ |
| Managed MCP (allowlists + output cap) | per-tool allow on publish + `tools.mcp_allow` (Ph 78) + `tools.mcp_max_output_chars` | ✅ |

## Platforms

| Claude Code | eVi equivalent | Status |
|---|---|---|
| CLI | Typer CLI | ✅ |
| Desktop app | Tauri desktop (menus/tray/updater) | ✅ |
| Web | FastAPI + SSE web UI | ✅ |
| Headless / print mode | `evi run` (Ph 65) | ✅ |
| VS Code / JetBrains extensions | — | 🚫 separate track |
| Mobile / web cloud platform | — | 🚫 local-first |
| Computer use | `computer` tool | ✅ |
| Chrome / browser automation | — | 🚫 deferred for MCP browser servers |
| Slack integration | — | ❌ integrations backlog |

## Review · planning · orchestration

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Code review (multi-agent) | `evi review --multi` (Ph 70) | ✅ |
| Run agents in parallel | `parallel_research` (Ph 61) | ✅ |
| Worktrees (isolate parallel sessions) | git worktrees + `[worktree] base_ref` default | ✅ |
| Dynamic workflows (script many subagents) | recipes + workflows + ultracode | ✅ TOML DAG + fixed pipeline |
| Agent teams (shared task list) | `evi team` — lead decomposes, teammates claim+drain (`evi/teams.py`) | ✅ |
| Agent view (live dispatch dashboard) | Dispatch panel + `GET /api/dispatch/stream` (SSE, busy dots) | ✅ |
| Ultrareview (CI gate) | `evi review --multi --exit-code` / `--json` + `/ultrareview` | ✅ local |
| Ultraplan (cloud) | local plan mode | 🚫 cloud |

## Automation & triggers

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Routines (schedule / webhook trigger) | scheduler + `evi routine` webhook→recipe (Ph 73) | ✅ |
| Channels (push alerts into a session) | `POST /api/session/{id}/channel` — note by default, or `run:true` drives a live agent turn | ✅ |
| GitHub Actions / GitLab CI | packaged `evi-run` composite action + ready `examples/github/pr-review.yml` | ✅ |

## Config · models · cost

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Settings (global + project) | config + profiles + `.evi.toml` (Ph 74) + settings UI | ✅ |
| Env vars / `.claude` dir | `EVI_*` env + `~/.evi` | ✅ |
| Model config / aliases / fast mode | model picker, routing, `fast_mode` | ✅ |
| Fallback model | `[llm] fallback_models` (retry the turn down the chain) | ✅ |
| Extended thinking on/off | `reasoning_effort` off/low/medium/high/max (`/effort`) | ✅ |
| Transcript retention (cleanupPeriodDays) | `tools.cleanup_period_days` + `evi sessions purge` | ✅ |
| Prompt caching | `cache_prompt` | ✅ |
| Context window display | usage chip + status line | ⚠️ no interactive sim |
| Cloud backends (Bedrock/Vertex/Foundry) | `openai_compat` (covers gateways/proxies) | 🚫 by design |
| Cost management / analytics dashboards | token usage shown | 🚫 local = free |

## SDK & programmatic

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Headless mode | `evi run --format json` (Ph 65) | ✅ |
| Custom tools | `@tool` decorator | ✅ |
| File checkpointing | `evi rewind` (Ph 64) | ✅ |
| Structured outputs | `/json` + JSON-Schema (`/schema`, `evi run --schema`) | ✅ |
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
| OpenTelemetry / monitoring | opt-in crash reporting (Sentry-compatible) | ⚠️ no metrics/traces |
| Tool search (1000s of tools) | `[tools] tool_search` defers the long tail behind a `search_tools` meta-tool | ✅ opt-in |

## Org / enterprise / compliance

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Admin setup, server-managed settings, team onboarding | — | 🚫 single-user |
| Authentication (accounts/SSO) | web auth token; local backends need none | 🚫 local |
| ZDR / legal / data-usage | private by design (everything local) | 🚫 N/A |

## Summary — specialty SLMs + opencode/Cursor gleanings (0.34.0)

Beyond Claude Code, eVi pulled in the genuinely-local wins from **opencode** and
**Cursor** and added small **specialty models**:

- **Specialty SLMs** — `[models]` ocr/vision/stt/tts registry; `describe_image`
  + OCR-VLM routing (Moondream2 / Qwen2.5-VL / GLM-OCR), Kokoro TTS, faster-whisper
  turbo. A small model per task, no main-model swap.
- **Working folder** — per-session cwd (`/cd`, `--cwd`, web `📁` chip).
- **opencode core** — real **shell tool**, `apply_patch` (multi-hunk),
  format-on-edit + `check_file` diagnostics (LSP-lite), persistent `/plan` toggle,
  `evi init` (AGENTS.md, already discovered up-tree).
- **Cursor gleanings (local-first only)** — local **FIM completion** engine
  (`evi complete` / `/api/complete`) so eVi is a local Tab/Copilot backend;
  Bugbot-style review (`.evi/BUGBOT.md` + `evi review-remember` + severity).
  (`evi edit` already existed.) Cursor's cloud bits (Cloud Agents, remote PR
  Bugbot, Design Mode, Slack, enterprise) remain out by the local-first rule.
- **Deferred (need a separate client / deeper work):** a VS Code/LSP extension
  to render the FIM completions as ghost-text; a full language-server
  integration (eVi ships the lighter check_file/format-on-edit instead).

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
- **Skill tool-scoping** (`allowed-tools`/`disallowed-tools` in skill frontmatter) — needs a persistent "active-skill" mode in the agent loop (skills are currently one-shot instruction packets).
- **Nested subagent spawning** (subagents that spawn subagents) — deliberately gated as a runaway-cost guard.
- **Custom REPL themes** — terminal colour themes (output styles already cover response persona).

🚫 **Not planned (by design):** cloud/enterprise backends (Bedrock/Vertex/Foundry),
IDE extensions, mobile, agentic browser, hosted session storage, cost/analytics
dashboards, accounts/SSO/admin — eVi is local-first and single-user.
