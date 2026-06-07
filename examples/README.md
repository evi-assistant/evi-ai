# Examples

Drop-in samples you can copy into `~/.evi/`.

## `EVI.md`

Project-context file. Copy to a project root; eVi auto-loads it when you
`evi chat` from that tree.

## `skills/`

Two example skills:

- **`code-review`** — Review a diff for correctness, style, security.
- **`summarize-paper`** — Boil an academic paper down to 5 bullets.

Install one:

```bash
mkdir -p ~/.evi/skills/code-review
cp examples/skills/code-review/SKILL.md ~/.evi/skills/code-review/

# Or all of them:
cp -r examples/skills/* ~/.evi/skills/
```

Then in the REPL: `/help` shows them, and the agent loads them on demand
via `invoke_skill`.

## `commands/`

User-defined slash commands. One example:

- **`commit.md`** — typing `/commit` runs git diff and proposes a
  conventional-commits message.

Install:

```bash
mkdir -p ~/.evi/commands
cp examples/commands/commit.md ~/.evi/commands/
```

## Hook examples (in-line)

A few patterns to drop into `~/.evi/hooks.toml`:

```toml
# Audit everything to a log file
[[before_tool_call]]
name = "audit"
match = "*"
command = ["bash", "-c", "echo $(date -u +%FT%TZ) $EVI_HOOK_TOOL $EVI_HOOK_ARGS_JSON >> ~/.evi/logs/tools.log"]

# Block writes outside the home directory
[[before_tool_call]]
name = "no-system-writes"
match = "write_file"
command = ["python3", "-c", "import os, sys, json; a=json.loads(os.environ['EVI_HOOK_ARGS_JSON']); p=os.path.realpath(a.get('path','')); sys.exit(0 if p.startswith(os.path.expanduser('~')) else 1)"]
veto_on_nonzero = true

# Desktop notification when an image is generated
[[after_tool_call]]
name = "image-done"
match = "generate_image"
command = ["notify-send", "eVi", "Image generated"]
```

## MCP server examples (in-line)

Drop into `~/.evi/mcp.json` (flip `tools.mcp = true` in config.toml):

```json
[
  {
    "name": "filesystem",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/projects"]
  },
  {
    "name": "git",
    "command": "uvx",
    "args": ["mcp-server-git", "--repository", "."]
  },
  {
    "name": "sqlite",
    "command": "uvx",
    "args": ["mcp-server-sqlite", "--db-path", "/home/me/notes.db"]
  }
]
```

After editing: `evi mcp list-tools` should show what each server exposes.
