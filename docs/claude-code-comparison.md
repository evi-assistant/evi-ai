# eVi vs Claude Code — feature comparison

How eVi's capabilities line up against the full [Claude Code docs](https://code.claude.com/docs/en/overview)
surface (reviewed 2026-06-08). eVi is **local-first, single-user, privacy-first**,
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

## Extension & customization

| Claude Code | eVi equivalent | Status |
|---|---|---|
| CLAUDE.md / memory | `EVI.md`/`AGENTS.md` project context + memory (+ tags) | ✅ |
| Nested CLAUDE.md (large codebases) | merges EVI.md/AGENTS.md up the tree (Ph 76) | ✅ |
| Skills | `SkillStore` | ✅ |
| Subagents | `delegate_*` + `parallel_research` (Ph 61) | ✅ |
| Output styles | `[llm] output_style` (Ph 69) | ✅ |
| Hooks (incl. HTTP hooks) | `hooks.toml` (command **or** `url`) | ✅ |
| Status line | `[statusline]` (Ph 72) | ✅ |
| Keybindings | — | ❌ buildable |
| Custom slash commands | `~/.evi/commands` frontmatter/$ARGS/@file/namespacing (Ph 62) | ✅ |

## Plugins & MCP

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Plugins (bundle commands/skills/agents/hooks) | `evi plugin add/list/remove` (Ph 68, 75, 80) | ⚠️ commands + skills + hooks + MCP (subagent profiles pending) |
| Plugin marketplaces / discovery | install from dir or git URL | ❌ no curated index |
| MCP client (connect to servers) | `MCPManager`, `mcp.json` | ✅ |
| MCP server (publish eVi's tools) | `evi mcp serve` (stdio + HTTP) | ✅ |
| Managed MCP (allowlists) | per-tool allow on publish + `tools.mcp_allow` (Ph 78) | ✅ |

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
| Worktrees (isolate parallel sessions) | git worktrees | ✅ |
| Dynamic workflows (script many subagents) | recipes + parallel research | ⚠️ no orchestration DSL |
| Agent teams / agent view (dispatch dashboard) | tabs + subagents | ❌ no multi-session dashboard |
| Ultraplan / Ultrareview (cloud) | local `review --multi` / plan mode | 🚫 cloud |

## Automation & triggers

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Routines (schedule / webhook trigger) | scheduler + `evi routine` webhook→recipe (Ph 73) | ✅ |
| Channels (push alerts into a session) | routines (inbound webhook) | ⚠️ no push-into-live-session |
| GitHub Actions / GitLab CI | runnable via `evi run` headless | ⚠️ no packaged action |

## Config · models · cost

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Settings (global + project) | config + profiles + `.evi.toml` (Ph 74) + settings UI | ✅ |
| Env vars / `.claude` dir | `EVI_*` env + `~/.evi` | ✅ |
| Model config / aliases / fast mode | model picker, routing, `fast_mode` | ✅ |
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
| Structured outputs | `/json`, `response_format` | ✅ |
| Permissions SDK | permission policy (Ph 66) | ✅ |
| Public Agent SDK (library) | headless covers automation | 🚫 deferred (no stable lib surface) |
| Session storage (S3/Redis) | local JSONL transcripts | 🚫 local-first |
| OpenTelemetry / monitoring | opt-in crash reporting (Sentry-compatible) | ⚠️ no metrics/traces |
| Tool search (1000s of tools) | category-filtered tools | 🚫 not needed at scale |

## Org / enterprise / compliance

| Claude Code | eVi equivalent | Status |
|---|---|---|
| Admin setup, server-managed settings, team onboarding | — | 🚫 single-user |
| Authentication (accounts/SSO) | web auth token; local backends need none | 🚫 local |
| ZDR / legal / data-usage | private by design (everything local) | 🚫 N/A |

## Summary — buildable gaps

✅ **Shipped in 0.31.0:**
- **Phase 75** — plugin **skills** (commands + skills now bundle; hooks/MCP/subagents still pending).
- **Phase 76** — nested project context (merge `EVI.md`/`AGENTS.md` up the tree).
- **Phase 77** — auto-mode trusted directories + domains.
- **Phase 78** — consume-side MCP server allowlist (`tools.mcp_allow`).

✅ **Also shipped (local, since 0.31.0 — pending the Actions billing block):**
Phase 79 in-app update progress, **Phase 80** plugin hooks + MCP servers,
**Phase 81** HTTP hook type, **Phase 82** configurable keybindings
(`keybindings.toml`).

Still open (lighter / later): subagent profiles in plugins, deep links, a plugin
marketplace index, "channels" push-into-session, a packaged CI action, an agent
dispatch view, context-window viz, OpenTelemetry. See [roadmap.md](roadmap.md)
for the full Phase 83–94 plan. Explicitly **not** planned: cloud/enterprise
backends, IDE extensions, mobile, agentic browser, cost/analytics dashboards,
public Agent SDK.
