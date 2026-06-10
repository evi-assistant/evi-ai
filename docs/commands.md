# Slash commands

> Quick reference. For the **full guide** — every built-in with its arguments,
> the web/desktop subset, aliases, and worked examples — see
> [features/slash-commands.md](features/slash-commands.md).

Type `/` at the start of a message to run a command (in the REPL or the web
chat). Built-ins cover the session controls — `/help`, `/model`, `/effort`,
`/fast`, `/plan`, `/auto`, `/goal`, `/compact`, `/reset`, `/tools`, `/json`,
`/notools`, `/forcetool`, `/image`, `/audio`, `/reload`. Run `/help` for the
full list.

## Custom commands

Drop a markdown file at `~/.evi/commands/<name>.md` and it becomes `/<name>`.
The file's contents are sent as your next message, after substitution. Modelled
on [Claude Code's custom commands](https://code.claude.com/docs/en/commands).

### Frontmatter (optional)

```markdown
---
description: Draft a conventional-commit message
argument-hint: [scope]
model: qwen2.5-coder:14b-instruct-q4_K_M
---
Look at the staged diff and write a commit message for $ARGUMENTS.
```

- `description` — shown in `/help`.
- `argument-hint` — documents the expected arguments.
- `model` — surfaced to the UI as the command's preferred model.

### Arguments

- `$ARGUMENTS` — everything typed after the command name.
- `$1`, `$2`, … `$9` — positional args (shell-split; quote to group:
  `/review "two words" tail` → `$1` = `two words`).
- `{args}` — legacy alias for `$ARGUMENTS` (still works).

`/commit auth` with the file above sends:
*"Look at the staged diff and write a commit message for auth."*

### File references

`@path/to/file` inlines that file's contents (fenced) if it exists — handy for
templates that should always include a checklist or style guide. Tokens that
aren't readable files (emails, `@handles`) are left untouched.

### Namespacing

Subdirectories become `:`-separated names:
`~/.evi/commands/git/commit.md` → `/git:commit`.

### Not supported

`!bash` execution blocks are intentionally **not** run — auto-executing shell on
command expansion is too sharp an edge for eVi's permission model. Use a tool
(with its permission prompt) or a [Skill](tools.md) for anything that needs to
run code.
