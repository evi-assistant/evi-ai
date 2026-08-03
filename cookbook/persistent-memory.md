# Give the agent persistent memory

Let eVi remember things between sessions — your preferences, project facts,
decisions — and pick up durable, per-project context automatically. No custom
code: the agent manages memory through its own tools, and project context is a
Markdown file you check into the repo.

> **Uses:** configuration (`~/.evi/config.toml`) + CLI · **Applies to:** the
> CLI, web, and desktop — they share one memory store.

## Two mechanisms

- **Memory** — durable notes stored as one Markdown file per topic under
  `~/.evi/memory/` (`%USERPROFILE%\.evi\memory\` on Windows). Survives restarts.
  The model decides what's worth keeping and writes it itself.
- **Project context** — an `EVI.md` (or `AGENTS.md`) checked into a repo, loaded
  into every system prompt while you work in that tree.

## Memory: how the agent remembers

Memory is attached whenever the `memory` tool toggle is on (the default). The
model drives it through five tools — you never call them, you just talk
("remember that I prefer tabs over spaces", "what do you know about project-x?"):

| Tool | What it does |
|------|--------------|
| `remember(name, content, tags="")` | Save/overwrite a note (same `name` overwrites). |
| `recall(name)` | Return a note's full body. |
| `forget(name)` | Soft-delete — moves it to `~/.evi/memory/.attic/`. |
| `list_memories()` | JSON list of `{name, summary, tags}`. |
| `recall_by_tag(tag)` | JSON list of notes carrying a tag. |

Each note is `<name>.md`; an auto-maintained `INDEX.md` is folded into the system
prompt as a `## Memory index` block, so the model always knows *what* is stored
and calls `recall(name)` to pull a full body on demand. Names are
`[A-Za-z0-9_\-]` (max 64 chars) and bodies cap at 64 KB. Because it's plain
Markdown you can add or edit files by hand — `/reload` re-reads them mid-session.

Memory is on by default. To confirm the toggle, and to keep writes from prompting
every turn:

```toml
# ~/.evi/config.toml
[tools]
memory = true   # attaches the memory store + tools to the agent

[auto]
auto_approve = ["fs", "code", "memory", "skills", "image"]   # memory = no prompt
```

## Project context: EVI.md / AGENTS.md

Scaffold a project-context file in the current directory:

```bash
evi init                 # creates AGENTS.md (the cross-tool standard)
evi init --name EVI.md   # or eVi's own name; EVI.md wins if both exist
```

eVi walks up from your cwd and loads the nearest file, appending it to every
system prompt — so you get per-project behavior just by `cd`-ing in. In a
monorepo a root file plus a package-level one are layered (outermost first).
Keep it to conventions, where things live, glossary, and hard "don'ts"; it's
capped at 64 KB. See [`examples/EVI.md`](../examples/EVI.md) for a starter.

## Curate memory automatically

After a stretch of work, have eVi review recent transcripts and prune or update
long-term memory for you:

```bash
evi dream --hours 24     # review the last 24h and curate memory
```

This needs transcripts enabled (`[tools] transcripts = true`, the default).
Deletions are soft — recoverable from `~/.evi/memory/.attic/`.

The full field reference (compaction, per-user isolation, size limits) is in
[`docs/features/memory-context.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/memory-context.md).

## Where to go next

- [Build an agent with the SDK](agent-sdk-quickstart.md) — `build_agent(...,
  enable_memory=True, enable_project=True)` wires both mechanisms into your own
  agent.
- [Guard an agent's inputs and outputs](guardrails.md) — add safety layers on top.
