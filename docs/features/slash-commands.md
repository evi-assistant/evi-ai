# Slash commands

Inside a chat you can type `/<name>` to run a command instead of sending a
message. There are two kinds:

1. **Built-ins** — REPL/chat controls baked into eVi (`/help`, `/model`,
   `/context`, …).
2. **User & plugin commands** — markdown prompt templates you drop in
   `~/.evi/commands/` (or that a plugin ships), fired as `/<name>` with
   arguments.

Type `/help` (or `/?`) at any time to list everything available in the current
session; press **Tab** in the CLI REPL to complete command names.

## Built-in commands (CLI REPL)

The full set, from `evi chat` / `evi sessions resume`:

| Command | What it does |
|---------|--------------|
| `/help`, `/?` | Show the command list (built-ins + your user/plugin commands). |
| `/reset` | Clear the conversation history (start fresh, same session). |
| `/exit`, `/quit` | Leave the REPL. |
| `/tools` | List the currently active tools. |
| `/model [id]` | Show the active model, or switch to `id` (persisted to config). |
| `/goal [text\|clear]` | Set / clear / show the ongoing goal (prepended to each turn). |
| `/plan [task]` | Run the next turn in plan-only mode (no tools); optional inline task. |
| `/auto [on\|off]` | Auto-approve every tool call for this session (no `on/off` = show state). |
| `/compact` | Summarise older history into one note to free context. |
| `/context`, `/ctx` | Show where the context window is being spent (per-bucket breakdown). |
| `/recent [n]` | List recent sessions (resume via `evi sessions resume`). |
| `/image <path>` | Attach an image to the next turn (vision/VLM models). |
| `/effort [low\|medium\|high\|max]` | Set reasoning effort for subsequent turns. |
| `/fast [on\|off\|<model-id>]` | Toggle fast mode (swap to a smaller model). |
| `/json <prompt>` | Force JSON-object output for the next turn. |
| `/schema <file> [prompt]` | Constrain the next turn to a JSON Schema (file path). |
| `/notools <prompt>` | Answer the next turn without using any tools. |
| `/forcetool <name> <prompt>` | Force the model to call a specific tool. |
| `/reload` | Re-read `config.toml` without restarting. |
| `/audio <path>` | Transcribe an audio file and send the text as the next turn. |
| `/audioraw <path> [prompt]` | Attach raw audio (omni models) / auto-transcribe otherwise. |
| `/speak [on\|off]` | Auto-speak assistant replies sentence-by-sentence (TTS). |
| `/predict <text\|file <p>\|clear>` | Set a speculative-decoding hint for the next turn. |

Aliases: `/?` = `/help`, `/ctx` = `/context`, `/img` = `/image`, `/quit` = `/exit`.

> Many of these have a GUI equivalent in the web/desktop app — the model picker
> chip (`/model`), the usage-chip popover (`/context`), the auto/plan chips,
> the 📎 attach button (`/image`, `/audio`), the speak toggle (`/speak`). See
> [cli-parity.md](../cli-parity.md) for the full mapping.

### Built-ins in the web / desktop chat

The web chat understands a **subset** of the built-ins (the ones that make sense
without a terminal), plus all your user/plugin commands:

`/help` · `/reset` · `/tools` · `/model [id]` · `/goal [text|clear]` ·
`/plan [task]` · `/auto [on|off]` · `/compact` · `/reload`

The remaining built-ins (`/context`, `/recent`, `/image`, `/effort`, `/fast`,
`/json`, `/schema`, `/notools`, `/forcetool`, `/audio*`, `/speak`, `/predict`,
`/exit`) are CLI-only — in the web/desktop app their job is done by GUI controls
or per-turn API parameters instead.

## User-defined commands

Drop a markdown file at `~/.evi/commands/<name>.md`; typing `/<name>` in the REPL
**or** the web chat sends its expanded content as the next user message. This is
modelled on Claude Code's custom commands (`evi/commands.py`).

```text
evi command list           # your + plugin commands (what /help shows in chat)
evi command new <name>     # scaffold ~/.evi/commands/<name>.md (ns:name → subdir)
```

(REPL keybindings have the same treatment: `evi keybindings list` shows the
effective bindings from `~/.evi/keybindings.toml`.)

### File format

```markdown
---
description: Draft a conventional-commit message
argument-hint: [scope]
model: qwen2.5-coder:14b-instruct-q4_K_M
---

Write a conventional-commit message for the staged changes.
Scope: $1
Full request: $ARGUMENTS
Here is the diff:
@<(git diff --cached)
```

- **Frontmatter** (optional `---` block):
  - `description` — shown in `/help`. If omitted, the first body line is used.
  - `argument-hint` — documents expected args (e.g. `[scope]`), shown to you.
  - `model` — a per-command model override surfaced to callers that honour it.
- **Arguments**:
  - `$ARGUMENTS` — everything after the command name.
  - `$1` … `$9` — positional args (the argument string is `shlex`-split).
  - `{args}` — legacy alias for `$ARGUMENTS`.
- **File references** — `@path/to/file` inlines that file's contents (fenced), up
  to 16 KB, if the file exists; otherwise the token is left as-is. (Emails like
  `a@b.com` are *not* treated as file refs.)
- **Namespacing** — subdirectories become `:` names:
  `~/.evi/commands/git/commit.md` → `/git:commit`.

> **No shell execution.** Unlike some tools, eVi does **not** run `!bash` blocks
> when expanding a command — auto-running shell on expansion is too sharp an edge
> for eVi's permission model. If you need triggering metadata or tool gating,
> write a [skill](skills.md) instead.

### Plugin commands

Installed plugins can ship a `commands/` directory; those appear as
`/<plugin>:<command>` alongside your own. See [plugins.md](plugins.md).

## Usage

```text
/help                     # list built-ins + user/plugin commands
/model qwen2.5:7b         # switch model (persisted)
/context                  # see the context-window breakdown
/auto on                  # auto-approve tools for this session
/commit fix(web)          # run ~/.evi/commands/commit.md with $1="fix(web)"
/git:commit               # a namespaced command from commands/git/commit.md
```

Anything that isn't a built-in and isn't a known user/plugin command prints
`unknown command: /<name> (try /help)`.

## Examples

### Example 1 — install the bundled `/commit` command

The repo ships one example command:

```bash
mkdir -p ~/.evi/commands
cp examples/commands/commit.md ~/.evi/commands/
```

Now `/commit` in any chat runs `git diff` and proposes a conventional-commit
message. Pass a scope: `/commit web`.

### Example 2 — a namespaced review command

```bash
mkdir -p ~/.evi/commands/pr
cat > ~/.evi/commands/pr/describe.md <<'EOF'
---
description: Draft a PR description from the branch diff
argument-hint: [ticket]
---
Write a concise PR description for ticket $1.
Summarise the change, call out risks, and list manual test steps.
Diff:
@<(git diff main...HEAD)
EOF
```

Fire it with `/pr:describe ABC-123`.

## Notes / limits

- Command and skill names must match `[A-Za-z0-9_-]+` per path segment (this also
  blocks path traversal).
- The command store **rescans on every call**, so newly added files show up
  without restarting `evi web`.
- `/json` and `/schema` are per-turn structured-output controls — see
  [structured-and-batch.md](structured-and-batch.md). The web/API equivalent is
  the `output_schema` field on the chat request.
- Built-ins always win over a user command of the same name, so you can't shadow
  `/model` with `~/.evi/commands/model.md`.
