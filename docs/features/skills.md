# Skills

Skills are **named markdown instruction packets the model loads on demand**. You
write the "how" of a task once (a review rubric, a house style, a checklist), and
the agent pulls it into context only when it decides the task calls for it — so
the playbook is reusable and the context window stays cheap.

## Overview

| | |
|---|---|
| **What** | A folder `~/.evi/skills/<name>/` containing a `SKILL.md` file with optional frontmatter + a markdown body of instructions. |
| **How the model uses it** | Every skill's *one-line index* (name + description) is injected into the system prompt. The model calls the `invoke_skill(name)` tool to pull the full body when it judges the skill relevant. |
| **Where** | `~/.evi/skills/` (yours) and `~/.evi/plugins/<plugin>/skills/` (plugin-supplied, exposed as `<plugin>:<skill>`). |
| **Toggle** | `[tools] skills = true` (the default). |

Skills vs the neighbours:

- **Skill** — *behavioural* instructions the model loads itself when relevant. No
  arguments, not triggered by you typing anything.
- **Slash command** ([slash-commands.md](slash-commands.md)) — a prompt template
  *you* fire by typing `/name`, with `$ARGUMENTS`. Deterministic, user-initiated.
- **Recipe** ([automation.md](automation.md)) — a fixed *sequence* of prompts run
  start-to-finish through one conversation.

Reach for a skill when you want the model to *consistently* apply a method
("review code like this", "summarise papers like this") without restating it
every time, and without forcing it on turns where it doesn't apply.

## How it works

`evi/skills.py` defines a read-only `SkillStore`:

- **Discovery** — on every call it rescans `~/.evi/skills/` plus each installed
  plugin's `skills/` directory, so a freshly added skill shows up without
  restarting the long-lived web/desktop process. Each skill is a *directory*
  containing `SKILL.md` (the directory layout leaves room for skill-local assets
  — helper scripts, sample data — without colliding with the instructions file).
- **The index** — `format_for_prompt()` renders an `## Available skills` block
  (one `- **name** — description` line each) and appends
  `Call invoke_skill(name) to load the full instructions.` This block is added to
  the agent's system prompt when `[tools] skills` is on and at least one skill
  exists.
- **Loading** — the model calls the `invoke_skill(name)` tool (`evi/tools/skills.py`),
  which returns the `SKILL.md` body with frontmatter stripped. A companion
  `list_skills` tool returns the index as JSON. Both are in the `skills` tool
  category.
- **No auto-firing.** eVi deliberately does **not** keyword-match skills into the
  prompt. The model sees the menu and chooses — which keeps token use predictable
  and the model's behaviour debuggable. (If you want metadata-triggered or
  argument-driven behaviour, use a slash command or recipe instead.)

### `SKILL.md` format

```markdown
---
name: code-review
description: Review a diff for correctness, style, and security issues.
---

# Body — the actual instructions the model follows once loaded
Step 1 …
Step 2 …
```

- **Frontmatter** is an optional `---`-delimited block at the very top.
  - `name` — overrides the folder name as the skill's id. If omitted, the folder
    name is used. Must match `[A-Za-z0-9_-]+`.
  - `description` — the one-liner shown in the index (so the model can decide
    whether to load it). Falls back to `(no description)`.
- **Body** — everything after the frontmatter. This is what `invoke_skill`
  returns. Write it as direct instructions to the model: ordered steps, a
  priority list, and an explicit output format work best.

## Setup

### Files and paths

| Path | What |
|------|------|
| `~/.evi/skills/<name>/SKILL.md` | One skill. `<name>` is the folder; frontmatter `name` overrides the id. |
| `~/.evi/skills/<name>/…` | Optional skill-local assets (scripts, data) — eVi only reads `SKILL.md`. |
| `~/.evi/plugins/<plugin>/skills/<skill>/SKILL.md` | A plugin-supplied skill, surfaced as `<plugin>:<skill>`. |

### Config

```toml
# ~/.evi/config.toml
[tools]
skills = true      # default; set false to drop the skill index + invoke_skill tool
```

### Installing the bundled examples

The repo ships two ready-to-use skills under `examples/skills/`:

```bash
# one skill
mkdir -p ~/.evi/skills/code-review
cp examples/skills/code-review/SKILL.md ~/.evi/skills/code-review/

# or all of them
cp -r examples/skills/* ~/.evi/skills/
```

## Usage

Skills are model-driven — there is **no `evi skill` CLI command** and nothing to
type. Once installed:

1. Start a chat (`evi chat`, or the web/desktop app).
2. The agent's system prompt now lists your skills.
3. When a turn matches a skill, the model calls `invoke_skill(<name>)` and then
   follows the loaded instructions. You'll see the `invoke_skill` tool call in the
   transcript / tool activity.

To nudge it explicitly, just ask: *"review this diff"* with a `code-review` skill
installed, or *"use the summarize-paper skill on this PDF"*. To confirm what's
available, ask the model to call `list_skills`, or look in `~/.evi/skills/`.

Skills work identically in the CLI, web, and desktop frontends — they're a
property of the agent, not the UI.

## Examples

### Example 1 — a house code-review rubric

`~/.evi/skills/code-review/SKILL.md` (the bundled example, abridged):

```markdown
---
name: code-review
description: Review a diff for correctness, style, and security issues.
---

# Code review skill
When asked to review code, follow these steps in order:

## 1. Understand the change
- Read the diff in full before commenting.

## 2. Correctness pass (highest priority)
- Off-by-one errors, resource leaks, concurrency bugs, error handling,
  security smells (string-built SQL/shell, secrets in code).

## 3. Style pass (lower priority)
- Follow the project's existing conventions, not your favourites.

## Output format
**Summary:** <one sentence>
### Correctness
- <issue> (`path/to/file.ext:42`)
```

Then: `evi chat` → *"review the staged diff"* → the model loads the skill and
produces output in your fixed format, every time.

### Example 2 — a minimal skill from scratch

```bash
mkdir -p ~/.evi/skills/sql-explain
cat > ~/.evi/skills/sql-explain/SKILL.md <<'EOF'
---
name: sql-explain
description: Explain a SQL query in plain English and flag slow patterns.
---

# SQL explainer
1. Restate what the query returns in one sentence.
2. Walk the joins in execution order.
3. Flag full-table scans, N+1 patterns, and missing-index smells.
4. End with a one-line "Bottom line:" verdict.
EOF
```

(That's just an illustration — `examples/skills/sql-explain/` ships it too.)

### Example 3 — a plugin-supplied skill

A plugin that ships `skills/threat-model/SKILL.md` exposes it as
`plugin-name:threat-model` in the index once installed
(`evi plugin add <dir-or-git-url>`). See [plugins.md](plugins.md).

## Notes / limits

- **Only `SKILL.md` is read.** Other files in the skill folder are for your own
  helper assets; eVi won't load them automatically.
- **Names** must match `[A-Za-z0-9_-]+`; skills that fail validation are skipped
  silently rather than erroring the whole list.
- **No arguments / no triggers.** A skill is static instructions. For
  argument-substituted templates you fire by hand, use a
  [slash command](slash-commands.md); for a fixed multi-step run, a
  [recipe](automation.md).
- **The model chooses.** If a skill isn't being picked up, sharpen its
  `description` so the relevance is obvious from the one-line index, or ask for it
  by name.
- **Context cost** is just the one-line index per skill until `invoke_skill` is
  called — so a big library of skills stays cheap.
