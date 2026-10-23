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
| `/image <path>` | Attach an image to the next turn (vision/VLM models). With Pillow installed it is turned upright, scaled to `[vision] max_px` on its longer side (1568; 0 = never) and sent without its EXIF data. |
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

Write a conventional-commit message for the staged changes
(run `git diff --cached` to see them).
Scope: $1
Full request: $ARGUMENTS
Follow the conventions in @CONTRIBUTING.md
```

- **Frontmatter** (optional `---` block):
  - `description` — shown in `/help`. If omitted, the first body line is used.
  - `argument-hint` — documents expected args (e.g. `[scope]`), shown to you.
  - `model` — a per-command model override surfaced to callers that honour it.
- **Arguments** — always substituted as plain text (never re-scanned, never read
  as file refs):
  - `$ARGUMENTS` — everything after the command name.
  - `$1` … `$9` — positional args. The argument string is `shlex`-split; if it
    doesn't parse (an unbalanced quote or a trailing backslash, as in
    `/fix don't panic`), it falls back to a plain whitespace split instead of
    failing. A missing positional becomes an empty string.
  - `{args}` — legacy alias for `$ARGUMENTS`.
- **File references** — an `@path` written in the command file inlines that
  file, within the limits below.
- **Namespacing** — subdirectories become `:` names:
  `~/.evi/commands/git/commit.md` → `/git:commit`.

### File references (`@path`)

An `@` at the start of a line or after whitespace starts a file ref (so emails
like `a@b.com` and `x@y` are not refs). A ref is replaced by the file's
contents, fenced, when **all** of these hold:

- **It is written in the template itself.** Only the command file's own refs
  are read. An `@path` in your arguments stays text: with a template of
  `Review $ARGUMENTS`, `/review @src/app.py` sends the literal `@src/app.py`
  (the model can still open the file with its own tools, under the normal
  permission prompt). A ref built from arguments (`@$1`, `@$ARGUMENTS`,
  `@{args}`) is spliced in as text and never read either. Inlined file text is
  pasted verbatim: placeholders and `@` refs inside it are not expanded.
- **It is inside the working folder or a trusted dir.** Relative refs resolve
  against the session's working folder (`evi chat --cwd`, `/cd`, or the web
  session's folder). Absolute and `~/` refs are allowed too, but after symlinks
  are followed the file must sit under the working folder or a directory in
  `[auto] trusted_dirs` (which `/add-dir` extends for the session). A UNC or
  device path (`\\host\share\...`) on a share that neither the working folder
  nor a trusted dir is on is refused before anything touches the network.
- **It is not a protected or secret path.** Files matching
  `[auto] protected_paths` are skipped, and so are key and credential files on
  a built-in list that applies even if you empty `protected_paths`: `.env*`,
  SSH keys (`id_rsa*`, `id_ed25519*`, …), anything under `.ssh/` or `.gnupg/`,
  `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx`, `.netrc`, `.git-credentials`,
  `.npmrc`, `.yarnrc.yml`, `.pypirc`, `.pgpass`, `.my.cnf`, `.vault-token`,
  Cargo and RubyGems credentials, `.docker/config.json`, `.kube/config`,
  `gh`'s `hosts.yml`, the AWS (`.aws/credentials`, SSO cache), Google Cloud
  (`.config/gcloud/`, `application_default_credentials.json`,
  `*service-account*.json`) and Azure (`.azure/`) CLI stores, Terraform
  `*.tfvars` / `*.tfstate`, and `~/.evi/config.toml`. Matching ignores case and
  checks the path however it is spelled (`./x`, `a/../x`, absolute, or relative
  to each allowed folder).
- **It is a regular UTF-8 text file.** Only the first 16,000 characters are
  read. Directories, FIFOs and devices are never opened.

Anything else is left in the message as the literal `@path` token. When a ref
names a file that exists but is refused (outside the folders above, protected,
secret, or a network path), eVi logs a warning saying why, so check the log if
a template's file stops appearing.

To inline a shared file that lives outside the project (a style guide in your
notes, say), trust its folder. This also auto-approves `fs` and `code` tools
under it (see [permissions-and-sandbox.md](permissions-and-sandbox.md)):

```toml
[auto]
trusted_dirs = ["~/notes/style"]
```

The REPL and the web chat apply the same rules. When eVi serves your commands
as MCP prompts (`evi mcp serve`), `prompts/get` never inlines anything: template
refs and the client's `args` both come back as text, so an MCP client can't
read local files through a prompt.

> **Changed after 1.0.53.** Earlier versions inlined every `@path` after the
> arguments were substituted, including refs typed in the arguments, refs
> outside the working folder (such as `@~/notes/style.md`), and key files.
> Commands that relied on those now send the `@path` text instead. Move the
> ref into the template and the file under the working folder or a trusted
> dir, or let the model read the file with its tools.

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
Run `git diff main...HEAD` to see the change, then summarise it, call out
risks, and list manual test steps. Follow the layout in
@.github/pull_request_template.md
EOF
```

Fire it with `/pr:describe ABC-123`.

## Notes / limits

- Command and skill names must match `[A-Za-z0-9_-]+` per path segment (this also
  blocks path traversal).
- The command store **rescans on every call**, so newly added files show up
  without restarting `evi web`.
- A command file that isn't readable UTF-8 is listed in `/help` as
  `(unreadable: …)`. Running it replies `can't run /<name>: …`; your other
  commands keep working.
- Templates never run shell: there is no `@<(command)` or `!command`
  substitution. Ask the model to run the command instead (it goes through the
  usual permission prompt).
- `/json` and `/schema` are per-turn structured-output controls — see
  [structured-and-batch.md](structured-and-batch.md). The web/API equivalent is
  the `output_schema` field on the chat request.
- Built-ins always win over a user command of the same name, so you can't shadow
  `/model` with `~/.evi/commands/model.md`.
