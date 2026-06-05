# Roadmap

A scratch list of Evi-native ideas not yet built. Phases we've shipped
are tracked in `CHANGELOG.md` and the project memory; this is the
forward backlog.

Items get a rough size (S/M/L) and a one-line "why".

For features borrowed from other vendors' SDKs, see
[sdk-coverage.md](sdk-coverage.md).

For external app/service integrations (Notion, Spotify, Slack, etc),
see [future-integrations.md](future-integrations.md).

## Next phase candidates

### Self-update + rollback (Phase 29 proposal)

Documented in [self-update.md](self-update.md). Headline: `evi update`
checks PyPI, snapshots prior state, upgrades, verifies, rolls back if
broken. **L** (~400 + 200 LOC + tests).

### Citations + reranker (Phase 30 proposal)

Two features that pair: (a) when the agent uses `read_file`,
`find_in_project`, or `web_fetch`, surface excerpts as inline
citations; (b) re-rank `find_in_project` candidates with a small
cross-encoder before returning. **M** each.

### `evi review` — git-aware code review (Phase 31 proposal)

`evi review HEAD~5..` / `evi review --staged` / `evi review --pr <url>`.
Combines `git_diff` + an LLM critique pass. Could leverage the
`coder` route from multi-model routing. **M**.

### Conversation grep (Phase 32 proposal)

`evi search "<query>"` across all transcripts. We already write
JSONL; this is fundamentally `ripgrep | format`. **S**.

### MCP-server-publish (Phase 33 proposal)

Expose Evi's tools (memory, index, calendar, git) as an MCP server
that Claude Desktop / Cline / Continue / Cursor can consume. Inverts
the integration story: instead of building one tool per app, the
editor's existing MCP client connects to Evi. **L**.

## Smaller items (S — could fit a single afternoon)

- ✅ **Output caching** — shipped Phase 36 (0.18.0): `read_file` caches by (path, mtime, size).
- ✅ **Permission batching** — shipped Phase 36 (0.18.0): one prompt per multi-tool turn.
- ✅ **Auto-titling** — shipped Phase 36 (0.18.0): `Agent.suggest_title()` + web tab rename + `evi sessions title`.
- ✅ **Hot reload of skills** — confirmed Phase 36 (0.18.0): stores rescan disk on every prompt compose; `/reload` reflects new skills + memory.
- ✅ **`evi doctor`** — shipped Phase 36 (0.18.0): paths / config / backend / deps / binaries / hardware diagnostic.
- **Long-context model awareness** — tag models in the registry with `context_size`; auto-pick a long-context one when usage gets high.
- **Recent-prompts history in REPL** — currently we have command history via prompt_toolkit; could add a per-project recent-prompts list surfaced via `/recent`.

## Medium items (M — a chunky afternoon to a day)

- **Background tool execution** — long-running tools (index large repo, scheduled task) post progress events. Currently they block the turn.
- **Memory tags** — `remember("Q3 plans", tags=["work","planning"])`; `recall_by_tag("work")`. Replaces flat filename namespace.
- **`evi recipe`** — multi-turn workflows. "Morning standup" = calendar + yesterday's commits + email. Recipes are stored under `~/.evi/recipes/` like skills.
- **Cross-machine sync** — sync `~/.evi/` via git or rclone. Memory, skills, profiles, routes, calendars all move with you. Conflicts via last-write-wins or `~/.evi/.attic/`.
- **Conversation autotitle via LLM** — see "auto-titling" above; LLM-powered version.
- **Web UI permission audit log** — list previously-approved tool calls so users can revoke. Reverse permission flow.
- **Plugin loader** — `~/.evi/plugins/<name>/` lazy-loaded Python modules that register tools. Already partly possible via skills + MCP, but a first-class `tools.py` plugin would be cleaner.

## Large items (L — a phase of work)

- **`evi update` self-update** — see above.
- **MCP-server-publish** — see above.
- **Responses API migration** — adopt OpenAI's new shape. Big migration but future-proofs the core.
- **Citations** — see above.
- **Local rerank tool** — see above.
- **Multi-user web mode** — auth per user (we have a single token); per-user `~/.evi/` paths; per-user permission policies. Useful for small-team installs.
- **Federation / inter-agent protocol** — Evi-to-Evi: one machine's agent can delegate to another's. Pairs with profiles + remote backend.

## Already considered, deferred

- **Computer use upgrade — agentic browser via Playwright** — agentic browsing rather than just pyautogui. Big surface area. Considered for Phase 12.5+ but deprioritised in favor of MCP integration with existing browser-MCP servers.
- **Fine-tune Evi from your own transcripts** — dream engine already does memory curation; the next step is "use the transcripts as a fine-tuning dataset for a local 3B model". Pairs with distillation. Niche.
- **Voice cloning for AutoSpeaker** — replace platform TTS with a local cloned voice (e.g. Bark, Tortoise, F5-TTS). Heavy deps + huge model downloads; defer.

## Notes on prioritisation

We've shipped 28 phases bringing Evi from scaffold to v0.11.0 in three
days. The remaining "must-have" list is getting short:

- **Distribution polish** — self-update is the last big gap.
- **Quality of life** — output caching, permission batching, auto-titling, hot reload, doctor.
- **Search + retrieval** — citations + rerank are the highest-leverage QoL upgrade for any non-trivial repo.
- **Federation / publish** — MCP server publish is the integration-story flip.

After those, we're in territory where individual features compete with
"is the user actually using the existing surface enough that this
matters?". Worth pausing to gather usage data via the `evi tail` /
transcripts + `evi dream` engine before adding more.
