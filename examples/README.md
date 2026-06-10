# Examples

Drop-in samples you can copy into `~/.evi/`.

## `EVI.md`

Project-context file. Copy to a project root; eVi auto-loads it when you
`evi chat` from that tree.

## `skills/`

Three example skills:

- **`code-review`** — Review a diff for correctness, style, security.
- **`summarize-paper`** — Boil an academic paper down to 5 bullets.
- **`sql-explain`** — Explain a SQL query and flag slow patterns.

Install one:

```bash
mkdir -p ~/.evi/skills/code-review
cp examples/skills/code-review/SKILL.md ~/.evi/skills/code-review/

# Or all of them:
cp -r examples/skills/* ~/.evi/skills/
```

The agent loads a skill on demand via `invoke_skill` when a turn matches its
one-line description (skills aren't typed like commands). Full guide:
[../docs/features/skills.md](../docs/features/skills.md).

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

## `mcp.json` — external MCP servers

Copy `mcp.json` to `~/.evi/mcp.json` (and flip `tools.mcp = true` in
`config.toml`) to give eVi a filesystem, git, and sqlite server:

```bash
cp examples/mcp.json ~/.evi/mcp.json
# edit the paths, then:
evi mcp list-tools          # shows what each server exposes
```

Full guide (client + `evi mcp serve`): [../docs/features/mcp.md](../docs/features/mcp.md).

## `peers.json` — eVi-to-eVi federation

Federation lets one eVi delegate a task to another (e.g. a laptop offloading to a
GPU box) via the `delegate_peer` tool and `evi peer run`.

**On the box that does the work** (the GPU box), opt in to serving and note its
web token:

```toml
# ~/.evi/config.toml
[web]
auth_token = "some-long-secret"     # the token peers authenticate with

[federation]
serve = true                        # answer delegated tasks
```

Run its web server: `evi web --host 0.0.0.0 --port 8473`.

**On the box that delegates** (the laptop), copy `peers.json` to
`~/.evi/peers.json` and fill in the URL + that token:

```bash
cp examples/peers.json ~/.evi/peers.json   # then edit url + token
evi peer list                              # gpu  http://gpu-box:8473 (token set)
evi peer run gpu "summarise this repo"     # delegate one task
```

Enable the in-chat tool with `tools.federation = true` to let the model call
`delegate_peer` itself. Per-peer tokens live in `peers.json` (not `config.toml`)
so they stay out of synced config. Full guide:
[../docs/features/agents.md](../docs/features/agents.md) → *Federation*.
