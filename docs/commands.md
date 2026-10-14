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
  `/review "two words" tail` → `$1` = `two words`). Input that doesn't
  shell-split, such as an unbalanced quote in `/fix don't panic`, falls back to
  a plain whitespace split.
- `{args}` — legacy alias for `$ARGUMENTS` (still works).

Arguments are pasted in as plain text. `/commit auth` with the file above sends:
*"Look at the staged diff and write a commit message for auth."*

### File references

An `@path/to/file` written **in the command file** inlines that file's contents
(fenced, first 16,000 characters) — handy for templates that should always
include a checklist or style guide. The rules:

- Only the template's own refs are read. An `@path` in your arguments
  (`/review @src/app.py`), or a ref built from them (`@$1`, `@{args}`), stays
  text.
- The file must be inside the session's working folder or a folder in
  `[auto] trusted_dirs` (`/add-dir` adds one for the session), after symlinks
  are followed. Put a shared style guide in a trusted dir to keep inlining it.
- Files matching `[auto] protected_paths` never inline, and neither do key and
  credential files (`.env*`, SSH keys, `.ssh/`, `*.pem`, `*.key`, `.netrc`,
  `.git-credentials`, `.npmrc`, `.pypirc`, Docker, Kubernetes, `gh` and cloud
  CLI credential files, Terraform state, `~/.evi/config.toml`), even if you
  empty `protected_paths`.
- Commands served over MCP (`evi mcp serve` prompts) never inline files.

Anything else (emails, `@handles`, a refused or missing file) is left as typed;
a refused file that exists is logged with the reason. The full list and
details are in [features/slash-commands.md](features/slash-commands.md#file-references-path).

### Namespacing

Subdirectories become `:`-separated names:
`~/.evi/commands/git/commit.md` → `/git:commit`.

### Not supported

`!bash` execution blocks are intentionally **not** run — auto-executing shell on
command expansion is too sharp an edge for eVi's permission model. Use a tool
(with its permission prompt) or a [Skill](tools.md) for anything that needs to
run code.
