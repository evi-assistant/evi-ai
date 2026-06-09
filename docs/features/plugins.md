# Plugins & Marketplace

## Overview

A **plugin** is an installable bundle that extends eVi without touching your own configuration directories. One plugin can ship any mix of:

- **slash commands** — exposed as `/<plugin>:<command>`
- **skills** — markdown instruction packets the model loads on demand
- **hooks** — before/after-tool-call hooks
- **MCP servers** — namespaced tool providers
- **subagent profiles** — used through the `delegate` tool

Use plugins when you want to share or reuse a coherent set of capabilities (e.g. a "git-helpers" bundle of git slash commands) instead of hand-copying individual command files, skills, and config snippets. The **marketplace** is a thin, optional layer on top: a searchable `name → source` index so you can `evi plugin search` and `evi plugin install <name>` instead of pasting directory paths or git URLs.

Everything is local-first and single-user. Installed plugins live under your eVi home (`~/.evi/`), and nothing is fetched at runtime except the optional remote index files you explicitly configure.

## How it works

Each plugin is just a directory containing a `plugin.toml` manifest. Installed plugins live at:

```
~/.evi/plugins/<name>/
```

Component types are **auto-discovered from well-known sub-paths** inside the plugin directory:

| Path inside the plugin | Component | Surfaced as |
|------------------------|-----------|-------------|
| `commands/**/*.md`     | slash commands | `/<plugin>:<command>` |
| `skills/<name>/SKILL.md` | skills | `<plugin>:<skill>` in the skill index |
| `hooks.toml`           | before/after-tool hooks | merged after your own `~/.evi/hooks.toml` |
| `mcp.json`             | MCP servers | namespaced `<plugin>:<server>` |
| `agents.toml`          | subagent profiles | namespaced `<plugin>:<name>` (via `delegate`) |

The manifest itself is minimal:

```toml
# ~/.evi/plugins/git-helpers/plugin.toml
name = "git-helpers"
description = "Handy git slash commands"
version = "0.1.0"
```

**Install is just directory management.** `evi plugin add` reads the manifest, validates the name (must match `[A-Za-z0-9_-]+`), and copies the source into `~/.evi/plugins/<name>/` (the `.git` directory is stripped). If a plugin with that name already exists, it is replaced. `evi plugin remove` deletes the directory. There is no copying into your own `commands/`, `skills/`, etc. — so there's no clobbering of your personal files.

**Loaders scan plugin directories live.** The command, skill, hook, MCP, and subagent loaders each rescan `~/.evi/plugins/` on use:

- The skill loader (`evi/skills.py`) and command loader (`evi/commands.py`) prefix every plugin component with `<plugin>:`.
- The hook loader (`evi/hooks.py`) parses each plugin's `hooks.toml` **after** your own hooks, appending them to the registry.
- The MCP loader (`evi/mcp/servers.py`) parses each plugin's `mcp.json`, prefixing each server name with `<plugin>:`.
- The subagent loader (`evi/llm/subagent.py`) parses each plugin's `agents.toml`, namespacing each profile `<plugin>:<name>`.

Because loaders rescan on every call, freshly added plugins appear without restarting long-lived processes (the web/desktop server).

**The marketplace index** (`evi/marketplace.py`) is plain JSON mapping a plugin name to where it can be installed from. The local index lives at `~/.evi/marketplace.json`; you may also configure extra **remote** index URLs that are fetched and merged in. On a name clash, the **local** entry wins. Remote fetches are best-effort with a 10-second timeout — a flaky or bad URL is silently ignored and never breaks search. `evi plugin install <name>` resolves the name through the merged index to its `source`, then hands that to the same installer used by `evi plugin add`.

## Setup

### Files and paths

| Path | What it is |
|------|------------|
| `~/.evi/plugins/<name>/` | An installed plugin (directory with a `plugin.toml`) |
| `~/.evi/marketplace.json` | The local plugin index (optional; created by `evi plugin index init`) |
| `~/.evi/config.toml` | Main config; holds the `[plugins]` section |

On Windows, `~/.evi/` resolves to `%USERPROFILE%\.evi\`.

### Config keys — `[plugins]` in `~/.evi/config.toml`

The only configurable key for this feature area is `index_urls` — a list of extra remote plugin-index JSON files merged with the local `~/.evi/marketplace.json`:

```toml
# ~/.evi/config.toml
[plugins]
index_urls = ["https://example.com/evi-plugins.json"]
```

**Default:** `index_urls = []` (empty — only the local `~/.evi/marketplace.json` is consulted). No remote calls happen unless you add URLs here.

### Marketplace index format — `~/.evi/marketplace.json`

```json
{
  "plugins": [
    {
      "name": "git-helpers",
      "source": "https://github.com/you/evi-git-helpers.git",
      "description": "Handy git slash commands",
      "author": "you",
      "tags": ["git", "vcs"]
    }
  ]
}
```

Only `name` and `source` are required per entry; `description`, `author`, and `tags` are optional. A remote index file uses the exact same shape.

### Pip extras

None. Plugins and the marketplace use only the standard library (`tomllib`/`tomli`, `json`, `urllib`, `subprocess` for `git clone`). Installing a plugin from a git URL requires `git` to be available on your `PATH`.

## Usage

All commands are part of the `evi` CLI, under the `evi plugin` group.

### Managing installed plugins

```text
evi plugin add <dir|git-url> [--name NAME]   Install from a local dir or git URL
evi plugin list                              List installed plugins + component counts
evi plugin remove <name>                     Remove an installed plugin
```

- `evi plugin add` takes a local directory **or** a git URL (URLs starting with `http://`, `https://`, `git@`, `ssh://`, or ending in `.git` are treated as git and shallow-cloned). `--name` overrides the name from the manifest.
- `evi plugin list` prints each plugin with its version and a count summary, e.g. `(3 cmds, 1 skills, 2 hooks)`.

### Using the marketplace

```text
evi plugin search [query]                    Search the merged index by name/desc/tag
evi plugin install <name>                     Install by name via the index
```

- `evi plugin search` with no query lists every entry; with a query it does a case-insensitive substring match over name, description, and tags.
- `evi plugin install` resolves the name through the local index plus any configured `index_urls`, then installs the resolved `source`.

### Managing the local index

```text
evi plugin index init [--overwrite]                 Write a starter ~/.evi/marketplace.json
evi plugin index add <name> <source> [--desc D]     Add/replace a local index entry
                                       [--author A]
                                       [--tags t1,t2]
```

- `evi plugin index init` writes a starter `marketplace.json` (refuses to overwrite an existing file unless you pass `--overwrite`).
- `evi plugin index add` adds or replaces an entry by name. `--tags` takes a comma-separated list.

### Web / Desktop — the Plugins panel

The web and desktop apps have a **Settings → Plugins** panel (no CLI needed):

- lists installed plugins with their component counts (commands / skills / hooks / MCP / agents) and a **Remove** button each;
- a **Marketplace** list with a filter box and per-entry **Install** button (entries you already have are marked *installed*);
- an **Install from a directory or git URL** field for one-off installs.

It is backed by `GET /api/plugins` and `POST /api/plugins/{install,remove}` —
the same `evi.plugins` / `evi.marketplace` functions the CLI uses.

### Where installed components show up

- **Slash commands** appear as `/<plugin>:<command>` in the REPL and web UI.
- **Skills** appear in the skill index as `<plugin>:<skill>`; the model loads one via `invoke_skill(name)`.
- **Subagent profiles** appear in `evi agents` as `<plugin>:<name>` and are invoked through the `delegate` tool.
- **Hooks** and **MCP servers** are loaded automatically and need no extra command.

## Examples

### Example 1 — Build, install, and use a local plugin

Create a small plugin that ships one git slash command:

```bash
mkdir -p my-plugin/commands
cat > my-plugin/plugin.toml <<'EOF'
name = "git-helpers"
description = "Handy git slash commands"
version = "0.1.0"
EOF
cat > my-plugin/commands/changelog.md <<'EOF'
Summarize the git commits since the last tag as a changelog.
EOF
```

Install it and confirm:

```bash
evi plugin add ./my-plugin
# installed git-helpers
# its commands are now /git-helpers:<command> (see `evi plugin list`)

evi plugin list
#   git-helpers v0.1.0 (1 cmds) — Handy git slash commands
```

Now `/git-helpers:changelog` is available as a slash command in the REPL and web UI. To uninstall:

```bash
evi plugin remove git-helpers
# removed git-helpers
```

### Example 2 — Seed the marketplace and install by name

Create a local index, add an entry, search it, and install:

```bash
evi plugin index init
# created C:\Users\you\.evi\marketplace.json

evi plugin index add git-helpers \
  https://github.com/you/evi-git-helpers.git \
  --desc "Handy git slash commands" \
  --author you \
  --tags git,vcs
# indexed git-helpers -> https://github.com/you/evi-git-helpers.git

evi plugin search git
#   git-helpers · you #git #vcs
#     Handy git slash commands
#     https://github.com/you/evi-git-helpers.git

evi plugin install git-helpers
# installed git-helpers (from https://github.com/you/evi-git-helpers.git)
```

### Example 3 — Merge in a shared remote index

Point eVi at a hosted index (e.g. a team-shared list) so its plugins show up in search and install-by-name alongside your local entries:

```toml
# ~/.evi/config.toml
[plugins]
index_urls = ["https://example.com/evi-plugins.json"]
```

```bash
evi plugin search          # lists local + remote entries, sorted by name
evi plugin install <name>  # resolves through the merged index
```

If the URL is unreachable, search and install still work using just your local index.

## Notes / limits

- **Single config key.** The only `[plugins]` config option is `index_urls`. There is no enable/disable flag, no allow-list — installed plugins (anything under `~/.evi/plugins/<name>/` with a valid `plugin.toml`) are always active.
- **No version pinning or update command.** Installing a plugin that already exists **replaces** it (the old directory is removed first). To "update," just `evi plugin add` / `evi plugin install` again. There is no `evi plugin update`.
- **Local entries win on name clash.** When merging remote `index_urls` with `~/.evi/marketplace.json`, an entry already present locally takes precedence.
- **Remote fetches are fail-open.** Remote index fetches use a 10-second timeout and swallow all errors — a bad or slow URL returns nothing rather than raising, so search/install never break. Malformed local index JSON is likewise treated as empty.
- **Security — install runs `git clone`.** Installing from a git URL shells out to `git clone --depth 1`. Installing a plugin means trusting its contents: plugins can register **hooks** that run around tool calls and **MCP servers** that are real processes/endpoints. Only install plugins from sources you trust, and review `hooks.toml`, `mcp.json`, and `agents.toml` before relying on a third-party bundle. `index_urls` point at JSON you control or trust; the JSON only lists install sources, but those sources are what gets cloned.
- **Name validation.** Plugin names must match `[A-Za-z0-9_-]+`. A git URL like `…/evi-git-helpers.git` is slugged to `evi-git-helpers` (the `.git` suffix and path are stripped) unless you override with `--name`.
- **Malformed components are skipped, not fatal.** When counting/loading components, an absent or malformed `hooks.toml`, `mcp.json`, or `agents.toml` simply contributes zero — a broken optional file in one plugin won't stop others from loading. A plugin directory with no valid `plugin.toml` is ignored by `evi plugin list`.
- **No clobbering of personal files.** Plugins are never merged into your own `~/.evi/commands/`, `~/.evi/skills/`, etc. Their components are discovered in place under `~/.evi/plugins/<name>/` and namespaced with the `<plugin>:` prefix, so they can't shadow your own commands or skills.
