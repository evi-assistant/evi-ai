# eVi feature guides

Deep-dive docs for every feature area — overview, how it works, setup, usage
(CLI/REPL/Web), and worked examples. For the one-page catalog see
[../features.md](../features.md); for config keys see
[../configuration.md](../configuration.md); for surface coverage see
[../cli-parity.md](../cli-parity.md).

## Guides

- [Agents & Orchestration](agents.md) — subagents, `delegate`, parallel research, workflows, dispatch, federation
- [Content Guardrails](guardrails.md) — regex + LLM-judge + offline classifier rules
- [Plugins & Marketplace](plugins.md) — bundled commands/skills/hooks/MCP/agents + `plugin search/install`
- [Evals & LLM-as-judge](evals.md) — `evi eval` suites, judge rubrics, scheduled evals
- [Voice](voice.md) — TTS engines (system/coqui/f5/piper), STT, AutoSpeaker/Listener
- [Sessions, Resume, Handoff, Checkpoints](sessions.md)
- [Hooks](hooks.md) — tool + lifecycle events, command or HTTP
- [Structured Outputs & Batch](structured-and-batch.md) — JSON-Schema output + `evi batch`
- [Permissions & Sandbox](permissions-and-sandbox.md) — modes, rules, trusted dirs/domains, OS sandbox
- [MCP](mcp.md) — client (`mcp.json`) + server (`evi mcp serve`)
- [Recipes, Routines, Scheduled tasks, Channels](automation.md)
- [Web & Desktop](web-desktop.md) — settings, multi-user, deep links, updater, working indicator
- [Observability](observability.md) — OpenTelemetry, `evi stats`, crash reports
- [Memory & Context](memory-context.md) — memory tags, `/context`, compaction
