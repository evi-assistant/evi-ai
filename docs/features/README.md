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
- [The Companion](companion.md) — the opt-in, read-only proactive watcher: check-ins, the morning brief, the feed, proposals you approve (`evi companion`, Settings → Companion)
- [Steering a running turn](steering.md) — type while eVi works; applied at the next step
- [Agents & Orchestration](agents.md) — subagents, `delegate`, parallel research, workflows, dispatch, federation
- [Task queue](tasks.md) — tasks tracked in the repository, run in a worktree under the implementer policy, verified only by the director's signature (`evi task`, `evi attest`; over MCP with `evi mcp serve --categories agent`)
- [Evidence bundles and provenance](evidence.md) — what each task run did (diff, eVi's own checks with junit, a redacted transcript excerpt), commit messages with provenance trailers, `evi stats --provenance` (`evi evidence`)
- [Ultracode](ultracode.md) — exhaustive multi-agent pipeline for one hard task (`evi ultracode` / `/ultra`)
- [Content Guardrails](guardrails.md) — regex + LLM-judge + offline classifier rules
- [Plugins & Marketplace](plugins.md) — bundled commands/skills/hooks/MCP/agents + `plugin search/install`
- [Skills](skills.md) — markdown instruction packets the model loads on demand (`invoke_skill`)
- [Slash commands](slash-commands.md) — built-in `/cmd` controls + user/plugin command templates
- [Evals](evals.md) — `evi eval` suites that check the answer and what the run did (tools, files, commands) in a throwaway workspace under the implementer policy; pass rates over N runs, reports, `evi eval compare`, the fake backend, the `evals/self` qualification suite and its baseline gate (`evi eval baseline record`), `evi models recommend --verify`, judge rubrics, scheduled evals
- [Voice](voice.md) — TTS engines (system/coqui/f5/piper), STT, AutoSpeaker/Listener
- [Sessions, Resume, Handoff, Import, Checkpoints](sessions.md) — including `evi sessions import` from Claude Code and Codex
- [Hooks](hooks.md) — tool + lifecycle events, command or HTTP
- [Structured Outputs & Batch](structured-and-batch.md) — JSON-Schema output + `evi batch`
- [Permissions & Sandbox](permissions-and-sandbox.md) — modes, rules, trusted dirs/domains, OS sandbox
- [Secret redaction](secret-redaction.md) — API keys and tokens become `[REDACTED:<kind>]` in transcripts, logs and tool-call displays
- [MCP](mcp.md) — client (`mcp.json`) + server (`evi mcp serve`), and the opt-in `agent` category: the director's task API
- [Recipes, Routines, Scheduled tasks, Channels](automation.md) — session channels (alerts into a live web session)
- [Web & Desktop](web-desktop.md) — settings (groups, explainers, health banners), multi-user, deep links, updater
- [Observability](observability.md) — OpenTelemetry, `evi stats`, crash reports
- [Memory & Context](memory-context.md) — memory tags, `/context`, compaction
