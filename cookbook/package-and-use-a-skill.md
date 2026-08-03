# Package and use a skill

A skill is a named markdown instruction packet — a review rubric, a house style,
a checklist — that the agent loads *on demand* when a turn calls for it. You write
the "how" of a task once; the context window only pays for it when the model
actually pulls it in.

> **Uses:** CLI (`evi skill`) · config (`[tools] skills`) · **Ships:** three
> example skills under [`examples/skills/`](../examples/skills/).

## What a `SKILL.md` looks like

A skill is a folder `~/.evi/skills/<name>/` holding a `SKILL.md`: optional
frontmatter, then a markdown body of instructions the model follows once loaded.

```markdown
---
name: code-review
description: Review a diff for correctness, style, and security issues.
---

# Code review skill
When asked to review code, follow these steps in order:
1. Read the diff in full before commenting.
2. Correctness pass (highest priority): off-by-one, resource leaks, injection …
```

- **`name`** (optional) — the skill's id; overrides the folder name. Must match
  `[A-Za-z0-9_-]+`.
- **`description`** — the one-liner shown in the model's skill index, so it can
  decide whether the skill is relevant. Keep it on **one line** (frontmatter is
  single-line `key: value`, not full YAML).
- **Body** — everything after the frontmatter. Direct instructions work best:
  ordered steps, a priority list, an explicit output format.

## Install one

```bash
evi skill add <name|git-url|zip-url|dir>   # one line: download (if needed) + import
evi skill import <dir-or-SKILL.md>         # copy a LOCAL folder into ~/.evi/skills/
evi skill list                             # what's installed (yours + plugin skills)
evi skill show <name>                      # print its description, body, bundled files
evi skill remove <name>                    # delete a user skill
```

`evi skill add` is the one-liner: a bare **name** resolves against the skills
index (`[plugins] index_urls` + the local marketplace), while a **git/zip URL or
local dir** is installed directly. `evi skill import` is the local-only copy —
add `--rewrite-paths` to fix relative refs in `SKILL.md` to absolute installed
paths (useful when porting a Claude Agent Skill that leans on companion files).
Both take `--name` and `--force`.

## Try the bundled ones

The repo ships three ready skills — `code-review`, `sql-explain`, and
`summarize-paper`:

```bash
cp -r examples/skills/* ~/.evi/skills/          # all three
# or bring in just one:
evi skill import examples/skills/summarize-paper
```

See [`examples/skills/`](../examples/skills/) for the full `SKILL.md` of each and
[`examples/README.md`](../examples/README.md) for the rundown.

## How the agent invokes it

Skills are **model-driven** — you don't type them (that's a slash command). Every
skill's one-line index (name + description) is injected into the system prompt;
when a turn matches, the model calls the `invoke_skill(<name>)` tool to pull the
full body, and you'll see that call in the transcript. To nudge it, just ask:
*"review the staged diff"* with `code-review` installed, or *"use the
summarize-paper skill on this PDF"*. The `SkillStore` rescans on every read, so a
freshly added skill shows up without a restart (use `/reload-skills` in a live
`evi chat`).

The index and the `invoke_skill` / `list_skills` tools only appear when the
toggle is on (it defaults on):

```toml
# ~/.evi/config.toml
[tools]
skills = true      # default; false drops the skill index + invoke_skill tool
```

Skills work identically in the CLI, web, and desktop — they're a property of the
agent, not the UI. Full reference:
[`docs/features/skills.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/skills.md).

## Where to go next

- [Build an agent with the SDK](agent-sdk-quickstart.md) — `build_agent(...,
  enable_skills=True)` gives a programmatic agent the same skill library.
- [Review a pull request in CI](headless-ci-review.md) — point a `code-review`
  skill at a diff in an unattended run.
