# MCP (client + serve)

## Overview

[MCP (Model Context Protocol)](https://modelcontextprotocol.io) lets eVi connect to other tool-providing programs — and lets other AI apps connect to eVi. eVi plays **both roles**:

- **MCP client** — eVi launches one or more external MCP servers (a filesystem server, a git server, a Jira server, whatever you configure) and exposes *their* tools to eVi's agent. Your assistant can now call those tools mid-conversation as if they were built in.
- **MCP server** (`evi mcp serve`) — eVi runs *as* an MCP server so other agents (Claude Desktop, Cursor, Cline, Continue) can reach into eVi: call a curated subset of eVi's own tools, read your long-term memory entries as resources, and pull your saved slash-command templates as prompts.

Use the **client** side when you want eVi to do more than its built-in toolset allows, without writing a Python plugin — just point it at an existing MCP server. Use the **serve** side when you'd rather drive eVi's tools and memory from a desktop AI app you already use.

Everything is local-first: client servers run as child processes on your machine over stdio, and the serve side defaults to stdio (spawned by the client) — nothing touches the network unless you explicitly opt into HTTP.

## How it works

### Client (consuming external servers)

1. On startup, eVi reads `~/.evi/mcp.json` (plus any `mcp.json` shipped by installed plugins) into a list of `MCPServer` launch configs. Missing or malformed files yield an empty list — eVi keeps running.
2. The consume-side allowlist `[tools] mcp_allow` is applied (see Setup). Then each **enabled** server is spawned as a child process over **stdio** using the `mcp` Python SDK.
3. For each server, eVi calls `initialize` and `list_tools`, then wraps every discovered tool as a native eVi tool named **`<server>.<tool>`** and registers it in eVi's tool registry. (Plugin-supplied servers are namespaced `<plugin>:<server>`, so they can't collide with your own.)
4. When the agent calls one of those tools, the call is forwarded to the server through an async-to-sync **bridge** (one asyncio loop on a daemon thread), so eVi's synchronous tool layer can drive the all-async MCP SDK.
5. Tool results are flattened to text for the model: text content is concatenated; non-text items (images, etc.) become a `[<type> omitted]` placeholder; errors are prefixed `ERROR:`.

**Fail-open by design:** a server that fails to start is logged once and skipped — the rest of eVi keeps working. On shutdown eVi unregisters the added tools first (so an in-flight agent loop can't reach a closed session), then closes each server session.

### Server (`evi mcp serve`)

eVi builds a standard MCP `Server` named `evi` exposing three things, all drawn from eVi's own single sources of truth:

- **Tools** — entries from eVi's tool registry, filtered by **category** (and optionally an exact-name allow-list). The same name / description / JSON-schema the agent already uses is reused verbatim. The default surface is the categories `memory`, `index`, `calendar`, `git`. **shell, computer, and code-write tools are deliberately NOT exposed by default.**
- **Resources** — each of your long-term memory entries as `evi://memory/<name>` (mimeType `text/markdown`).
- **Prompts** — each saved slash-command template (`~/.evi/commands/*.md`); each accepts an optional free-text `args` argument substituted for `{args}` in the template body.

Transports: **stdio** (the default; what a desktop MCP client spawns) or **streamable HTTP** (`--http`) served at the `/mcp` path, with an optional bearer-token gate (`--token`) for remote/untrusted clients.

## Setup

### Client side

**1. Define your servers** in `~/.evi/mcp.json` (on Windows: `C:\Users\<you>\.evi\mcp.json`; the directory honors the `EVI_HOME` environment variable if set). The file is a **JSON array** of server objects:

| Field | Type | Required | Default | Meaning |
|-------|------|----------|---------|---------|
| `name` | string | yes | — | Server label; tools register as `<name>.<tool>` |
| `command` | string | yes | — | Executable to launch (e.g. `npx`, `uvx`, `python`) |
| `args` | string[] | no | `[]` | Arguments passed to the command |
| `env` | object | no | `{}` | Extra environment variables for the child process |
| `enabled` | bool | no | `true` | Set `false` to keep a server in the file but skip it |

Entries missing `name` or `command` are skipped silently. Print the exact path any time with `evi mcp path`.

**2. Enable MCP** in `~/.evi/config.toml` (it is **off by default**):

```toml
[tools]
mcp = true
```

**3. (Optional) Gate which servers load per machine.** When `[tools] mcp_allow` is non-empty, only servers whose `name` is in the list are loaded — handy if you sync one `mcp.json` across machines but want different servers active on each:

```toml
[tools]
mcp = true
mcp_allow = ["filesystem", "git"]
```

An empty/unset `mcp_allow` loads every server in the file (each still honoring its own `enabled` flag).

**4. Install the MCP extra** (the `mcp` SDK is an optional dependency):

```bash
pip install 'evi-assistant[mcp]'
```

If MCP is enabled but the package isn't installed, eVi prints a hint and continues without MCP tools.

### Server side (`evi mcp serve`)

No config keys are required — `evi mcp serve` works out of the box once the `mcp` extra is installed. You control the exposed surface with command flags:

- `--categories` / `-c` — comma-separated tool categories to expose (default `memory,index,calendar,git`).
- `--tools` — optional comma-separated allow-list of exact tool names *within* those categories.
- `--http` — serve streamable HTTP instead of stdio.
- `--host` (default `127.0.0.1`) and `--port` (default `8765`) — HTTP bind address.
- `--token` — require `Authorization: Bearer <token>` on every HTTP request.

## Usage

### CLI commands (client side)

| Command | What it does |
|---------|--------------|
| `evi mcp path` | Print the path to your `mcp.json` |
| `evi mcp list-servers` | List configured servers from `mcp.json` with on/off state |
| `evi mcp list-tools` | Start the servers and enumerate every tool they expose |

Once `tools.mcp = true` and servers are configured, MCP tools are available automatically in normal `evi` usage (CLI chat, the REPL, and the web UI) — they appear in the registry as `<server>.<tool>` and the agent calls them like any other tool. `evi mcp list-tools` warns if `tools.mcp` is still `false`.

### CLI commands (server side)

| Command | What it does |
|---------|--------------|
| `evi mcp serve` | Run eVi as an MCP server over **stdio** (what a desktop client spawns) |
| `evi mcp serve --http --token <secret>` | Run as a streamable-HTTP server, bearer-token gated |
| `evi mcp serve-config` | Print a ready-to-paste client config snippet (`mcpServers` block) |

### Web UI

The MCP server allowlist is editable from the web settings panel as **"MCP server allowlist"** (a lines field bound to `[tools] mcp_allow`). When `tools.mcp` is enabled and servers are configured, the web server starts the same `MCPManager` on launch, so MCP tools are available in browser chat too.

## Examples

### Example 1 — Client: give eVi a filesystem server and a git server

Create `~/.evi/mcp.json`:

```json
[
  {
    "name": "filesystem",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users"],
    "env": {},
    "enabled": true
  },
  {
    "name": "git",
    "command": "uvx",
    "args": ["mcp-server-git", "--repository", "C:/evi"]
  }
]
```

Enable MCP in `~/.evi/config.toml`:

```toml
[tools]
mcp = true
```

Install the extra, then verify what's wired up:

```bash
pip install 'evi-assistant[mcp]'

evi mcp list-servers
# on  filesystem — npx -y @modelcontextprotocol/server-filesystem C:/Users
# on  git — uvx mcp-server-git --repository C:/evi

evi mcp list-tools
# filesystem.read_file — Read the complete contents of a file...
# filesystem.list_directory — Get a detailed listing...
# git.git_log — Shows the commit logs...
# ...
```

From here the agent can call e.g. `git.git_log` or `filesystem.read_file` during any chat.

### Example 2 — Serve: expose eVi to Claude Desktop / Cursor

Generate a client config snippet:

```bash
evi mcp serve-config --categories memory,index,calendar
```

It prints a block you paste into your MCP client's `mcpServers` config (the `command` is your current Python interpreter):

```json
{
  "mcpServers": {
    "evi": {
      "command": "/path/to/python",
      "args": ["-m", "evi", "mcp", "serve", "--categories", "memory,index,calendar"]
    }
  }
}
```

The client spawns `evi mcp serve` over stdio on demand; eVi's memory, index, and calendar tools — plus your memory entries (as `evi://memory/<name>` resources) and saved commands (as prompts) — show up inside that app.

### Example 3 — Serve over HTTP for a remote client

Run a token-gated streamable-HTTP server, narrowing to two exact tools within the `memory` category:

```bash
evi mcp serve --http \
  --host 0.0.0.0 \
  --port 8765 \
  --token "$(openssl rand -hex 32)" \
  --categories memory \
  --tools memory.search,memory.read
```

The MCP endpoint is served at `/mcp` (e.g. `http://host:8765/mcp`). Every request must send `Authorization: Bearer <token>` or it gets a `401`.

## Notes / limits

- **stdio only on the client side.** eVi launches external servers over stdio (child processes); there is no built-in support for connecting to a remote HTTP MCP server as a *client*. The HTTP transport exists only on the *serve* side.
- **Fail-open.** A server that won't start (bad command, missing binary) is logged once and skipped — it never blocks eVi. Malformed `mcp.json` entries are dropped silently; a connection that actually fails is what gets logged.
- **Off by default.** Client MCP requires `tools.mcp = true`; without it, `mcp.json` is ignored.
- **Trust the servers you launch.** An MCP server is just a program eVi runs with the `command`/`args`/`env` you give it — it executes with your privileges. Only configure servers you trust, and scope filesystem/git servers to the directories you intend.
- **Curated serve surface.** `evi mcp serve` defaults to `memory,index,calendar,git` and deliberately excludes shell, computer-control, and code-write tools. Widening `--categories` can hand powerful tools to whatever connects — do so consciously.
- **HTTP security.** Running `--http` without `--token` is unauthenticated; eVi prints a warning and you should bind to localhost only in that case. For any non-localhost exposure, set `--token` (compared in constant time) and prefer running behind TLS.
- **Result flattening.** When eVi consumes a server's tool, non-text content (images, binary) is replaced with a `[<type> omitted]` placeholder so the model knows something was elided rather than silently dropped.
- **Call timeout.** Forwarded client tool calls default to a 120-second timeout; a server that hangs will surface a timeout rather than block forever.
- **Lazy SDK import.** The `mcp` package is imported only when needed, so the rest of eVi runs fine without the `[mcp]` extra installed — you just won't get MCP tools (client) or be able to `serve`.

Source of record: `C:\evi\evi\mcp\__init__.py`, `C:\evi\evi\mcp\manager.py`, `C:\evi\evi\mcp\bridge.py`, `C:\evi\evi\mcp\servers.py`, `C:\evi\evi\mcp\publish.py`, and the `mcp_app` commands in `C:\evi\evi\apps\cli\main.py` (lines 4488-4600). Config keys: `C:\evi\evi\config.py` (`ToolToggles.mcp`, `ToolToggles.mcp_allow`; `MCP_CONFIG_PATH`).
