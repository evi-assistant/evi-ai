# Memory & Context management

## Overview

eVi has two complementary mechanisms for remembering things across time:

- **Memory** — durable, human-readable notes stored as one Markdown file per topic under `~/.evi/memory/`. These survive across sessions and process restarts. The assistant decides what is worth keeping (your preferences, project facts, contact details, decisions) and writes it itself; you can also manage these files by hand. An auto-maintained index of every memory is folded into the system prompt so the model always knows *what* is stored without loading every byte.
- **Context management** — keeps a single conversation from overflowing the model's context window. As history grows, eVi automatically summarises the oldest turns into one compact note (compaction), and gives you tools to see and control where the window is being spent.

Memory is for facts that should outlive a conversation. Context management is about fitting the *current* conversation into a finite window.

Both are local-first and private: nothing leaves your machine except the LLM calls you already make to your configured backend.

## How it works

### Memory

Memory is intentionally simple — markdown on disk, one file per topic, keyed by a safe filename with no extension (source: `evi/memory.py`).

- **Storage location**: `~/.evi/memory/` (`%USERPROFILE%\.evi\memory\` on Windows). Each entry is `<name>.md`.
- **Names** must match `^[A-Za-z0-9_\-]+$` (letters, digits, dash, underscore), max 64 characters.
- **Size**: each entry is capped at 64 KB; a write that would exceed this is rejected.
- **The index**: a file `INDEX.md` is regenerated automatically on every write/delete so it always matches the directory. The same content is rendered into the agent's system prompt as a `## Memory index` block — a bullet list of `name — summary [tags]`, where the *summary* is the first non-empty line of the file. The prompt instructs the model to call `recall(name)` to pull full contents on demand.
- **Tags**: optional, comma-separated, case-insensitive. They are stored as an invisible trailing HTML comment (`<!-- tags: ... -->`) so they never show up in rendered Markdown or disturb the first-line summary. Untagged legacy files parse cleanly as tag-less.
- **Soft delete**: deleting a memory (via the `forget` tool) does not erase it — the file is moved into `~/.evi/memory/.attic/` with a timestamp suffix (e.g. `preferences-20260527_120000.md`), so a mistaken delete is recoverable by hand. (There is also a permanent hard-delete path used sparingly internally.)

The model interacts with memory through five tools (source: `evi/tools/memory.py`), all in the `memory` permission category:

| Tool | What it does |
|------|--------------|
| `remember(name, content, tags="")` | Save/overwrite a memory. Empty `tags` keeps any existing tags. |
| `recall(name)` | Return the full body of a memory. |
| `forget(name)` | Soft-delete a memory (moves it to `.attic/`). |
| `list_memories()` | JSON list of `{name, summary, tags}`. |
| `recall_by_tag(tag)` | JSON list of memories carrying a tag. |

Memory is only attached to the agent when the `memory` tool toggle is on (it is on by default). It is wired into both the CLI REPL and the Web UI; in the multi-user web mode each user gets their own memory directory under `~/.evi/users/<name>/memory/`.

### Context management (compaction)

Token counts are estimated, not tokenized: eVi uses the ~4-chars-per-token rule of thumb (source: `evi/llm/agent.py` `_approx_tokens`, and `evi/context_report.py`). This is good enough for "how full is my context" reporting, not a substitute for a real tokenizer.

Two things drive **automatic compaction** (`_maybe_autocompact`), checked after each turn:

1. **Message count** — if the in-memory history exceeds `llm.compact_after_messages` messages.
2. **Capacity** — if estimated tokens reach `llm.compact_when_pct` percent of `llm.context_size`.

When either fires, `compact_history()` runs:

- The original system prompt (index 0) and the most recent `llm.compact_keep_recent` messages are kept verbatim.
- Everything in between is sent to your *same* LLM backend in a one-shot call that produces a concise summary (under ~400 words, preserving facts, decisions, file paths, preferences, and unfinished tasks).
- That summary replaces the middle as a single `system` message prefixed `[compacted: N earlier messages summarised]`.
- **Fail-safe**: if the summary call fails or comes back empty, history is left untouched — a transient model outage never loses context. A `before_compact` lifecycle hook can also veto compaction.

The **context report** (`evi/context_report.py`) categorises history into four buckets — `system`, `user` (you), `assistant`, and `tools` (tool calls + tool results) — and reports tokens per bucket, total used, the ceiling, and percent full.

## Setup

### Config file

Settings live in `~/.evi/config.toml` (`%USERPROFILE%\.evi\config.toml`). First run writes defaults. You can also point eVi elsewhere with the `EVI_HOME` environment variable, which relocates the whole `~/.evi/` tree (memory included).

**Memory tool toggle** — section `[tools]`:

```toml
[tools]
memory = true   # default: on — attaches the memory store + tools to the agent
```

If you want memory writes/reads/deletes to happen without a permission prompt, the `memory` category is already in the default auto-approve list — section `[auto]`:

```toml
[auto]
auto_approve = ["fs", "code", "memory", "skills", "image"]
```

**Compaction + context window** — section `[llm]`. Defaults shown:

```toml
[llm]
context_size          = 32768   # approx token ceiling for your model (0 = unknown)
compact_after_messages = 40     # compact once history exceeds this many messages (0 = disabled)
compact_keep_recent    = 10     # most-recent messages left un-summarised
compact_when_pct       = 85     # compact when usage reaches this % of context_size
```

Notes on these keys:

- Set `context_size` to match the model you actually run. It powers both the usage display and pre-emptive compaction. Leaving it `0` disables the percentage-based trigger and the "of N" display. `evi doctor` will nudge you if `context_size` looks mismatched against the model's native window.
- Set `compact_after_messages = 0` (and rely on `compact_when_pct`, or set that to 0 too) to disable automatic compaction entirely. `compact_keep_recent` is internally floored at 2.

### Optional: pip extras

Core memory and compaction need **no extra dependencies** — it's plain Markdown files and a call to your existing LLM backend. Optional integrations:

- **Obsidian sync** ships in the base CLI (`evi obsidian ...`) and just reads/writes Markdown into a vault folder — no extra install.
- Embeddings-based semantic file search (`[tools] index`) is a separate feature and not required for memory.

## Usage

### REPL slash commands (CLI and Web both)

- `/context` or `/ctx` — show the per-bucket context breakdown (system prompt / you / assistant / tools), total tokens, ceiling, and percent full. The bar turns green/yellow/red as you approach the limit.
- `/compact` — force compaction now (summarise older history into a single note to free context). Prints how many messages were collapsed, or notes that history is too short.

The assistant uses the `remember` / `recall` / `forget` / `list_memories` / `recall_by_tag` tools on its own during conversation — just talk to it ("remember that I prefer tabs over spaces", "what do you have stored about project-x?").

### Web UI

- Slash commands above work in the chat input.
- The **context usage chip** in the UI shows how full the window is; clicking it surfaces the same breakdown as `/context`.

### CLI commands

- `evi dream [--hours N]` — review the last *N* hours (default 24) of transcripts and curate long-term memory automatically (add/remove/change entries). Requires transcripts to be enabled (`[tools] transcripts = true`, the default, writing to `~/.evi/transcripts/`). Deletions during dreaming are soft (recoverable from `.attic/`).
- `evi obsidian status [--sub <subfolder>]` — show what differs between memory and an Obsidian vault without changing anything.
- `evi obsidian push` / `evi obsidian pull` / `evi obsidian sync` — copy memory into a vault, read a vault into memory, or two-way sync (each supports `--dry-run`).

### Managing files by hand

Because memory is just Markdown, you can edit, add, or remove files directly in `~/.evi/memory/`. After editing config or memory while a session is open, `/reload` re-reads `config.toml` and refreshes the memory/skill index without restarting.

## Examples

### Example 1 — tune compaction for a small-context model

You're running a 7B model with an 8K window and want eVi to compact earlier and keep fewer recent turns. Edit `~/.evi/config.toml`:

```toml
[llm]
model            = "qwen2.5-7b-instruct"
context_size     = 8192
compact_when_pct = 70    # start compacting at 70% full instead of 85%
compact_keep_recent = 6  # keep the last 6 messages verbatim
```

Then in a REPL session, check where the window is going and force a compaction if needed:

```text
> /ctx
Context — 52 messages, ~5,910 tokens of 8,192 (72%)
  system prompt  ########----------------  1,420 (24%)
  you            #####-------------------   980 (16%)
  assistant      ########----------------  1,510 (25%)
  tools          ########----------------  2,000 (33%)

> /compact
compacted 38 messages into a summary
```

### Example 2 — seed a memory by hand and let the model use it

Create a memory file directly (the first non-empty line becomes the index summary; the trailing comment carries tags):

```bash
# ~/.evi/memory/preferences.md
# My working preferences

- Editor: Neovim, tabs not spaces
- Always run tests before committing
- Prefer concise answers

<!-- tags: workflow, personal -->
```

On the next turn eVi rebuilds `INDEX.md` and exposes this in its system prompt, so the model can fetch it on demand. The equivalent flow driven entirely by the assistant's tools looks like:

```text
remember(name="preferences",
         content="# My working preferences\n\n- Editor: Neovim, tabs not spaces\n...",
         tags="workflow, personal")
  -> saved memory 'preferences' to ~/.evi/memory/preferences.md [tags: workflow, personal]

recall_by_tag(tag="workflow")
  -> [{"name": "preferences", "summary": "My working preferences", "tags": ["workflow", "personal"]}]

recall(name="preferences")
  -> # My working preferences ...
```

## Notes / limits

- **Token counts are approximate.** The ~4-chars/token estimate ignores image/data-URL parts and isn't a real tokenizer. Treat `/context` percentages as guidance, not gospel.
- **Compaction is lossy by design.** Older turns are replaced by a summary; details the summary omits are gone from the live context (though full transcripts remain on disk if `[tools] transcripts` is on). The original system prompt and the most recent `compact_keep_recent` messages are always preserved verbatim.
- **Fail-open behavior.** If the summarisation call fails or returns nothing, history is left unchanged rather than dropped — a model outage never silently destroys your conversation. A `before_compact` hook can veto compaction.
- **Deletes are recoverable.** `forget` / `evi dream` removals move files to `~/.evi/memory/.attic/` with a timestamp; restore by moving the file back (a restore won't clobber a current entry of the same name). Only the internal hard-delete bypasses the attic.
- **Name and size constraints.** Memory names are restricted to `[A-Za-z0-9_\-]` (max 64 chars) and bodies to 64 KB; oversized or badly-named writes are rejected rather than truncated.
- **Memory must be enabled.** If `[tools] memory = false`, the store and its tools are not attached and the model has no persistent memory for that session.
- **Per-user isolation.** In multi-user web mode, memory is namespaced per user under `~/.evi/users/<name>/memory/`; users do not see each other's memories.
- **Privacy.** Everything is plain Markdown on your disk. The only network egress is the LLM calls you already make (including the one-shot summary used during compaction, which goes to your configured backend).

## Reference

- `~/.evi/config.toml` — `[tools] memory`, `[auto] auto_approve`, and `[llm]` compaction keys (`context_size`, `compact_after_messages`, `compact_keep_recent`, `compact_when_pct`).
- `~/.evi/memory/` — memory files + auto-generated `INDEX.md`; `~/.evi/memory/.attic/` — soft-deleted entries.
- `~/.evi/transcripts/` — session logs used by `evi dream`.
- Source: `evi/memory.py`, `evi/tools/memory.py`, `evi/context_report.py`, `evi/llm/agent.py` (compaction), `evi/config.py`.
