# eVi feature guides

Deep-dive docs for every feature area — overview, how it works, setup, usage
(CLI/REPL/Web), and worked examples. For the one-page catalog see
[../features.md](../features.md); for config keys see
[../configuration.md](../configuration.md); for surface coverage see
[../cli-parity.md](../cli-parity.md); for who can reach the web server (access
tokens, the desktop app's LAN token, containers) see
[../security.md](../security.md).

## Guides

- [First run](onboarding.md) — what the first-run screen offers, the verified backend switch and its rollback
- [Managed local runtime](runtime.md) — eVi's own llama.cpp: setup, model catalog, GPU, your own build (`evi runtime`, `evi model`)
- [Chat channels (Telegram)](channels.md) — message eVi from your phone; pairing, the channel toolset (`evi channel`)
- [Steering a running turn](steering.md) — type while eVi works; applied at the next step
- [Agents & Orchestration](agents.md) — subagents, `delegate`, parallel research, workflows, dispatch, federation
- [Ultracode](ultracode.md) — exhaustive multi-agent pipeline for one hard task (`evi ultracode` / `/ultra`)
- [Content Guardrails](guardrails.md) — regex + LLM-judge + offline classifier rules
- [Plugins & Marketplace](plugins.md) — bundled commands/skills/hooks/MCP/agents + `plugin search/install`
- [Skills](skills.md) — markdown instruction packets the model loads on demand (`invoke_skill`)
- [Slash commands](slash-commands.md) — built-in `/cmd` controls + user/plugin command templates
- [Evals & LLM-as-judge](evals.md) — `evi eval` suites, judge rubrics, scheduled evals
- [Voice](voice.md) — TTS engines (system/coqui/f5/piper), STT, AutoSpeaker/Listener
- [Sessions, Resume, Handoff, Import, Checkpoints](sessions.md) — including `evi sessions import` from Claude Code and Codex
- [Hooks](hooks.md) — tool + lifecycle events, command or HTTP
- [Structured Outputs & Batch](structured-and-batch.md) — JSON-Schema output + `evi batch`
- [Permissions & Sandbox](permissions-and-sandbox.md) — modes, rules, trusted dirs/domains, OS sandbox
- [Secret redaction](secret-redaction.md) — API keys and tokens become `[REDACTED:<kind>]` in transcripts, logs and tool-call displays
- [MCP](mcp.md) — client (`mcp.json`) + server (`evi mcp serve`)
- [Recipes, Routines, Scheduled tasks, Channels](automation.md) — session channels (alerts into a live web session)
- [Web & Desktop](web-desktop.md) — settings (groups, explainers, health banners), multi-user, deep links, updater
- [Observability](observability.md) — OpenTelemetry, `evi stats`, crash reports
- [Memory & Context](memory-context.md) — memory tags, `/context`, compaction
