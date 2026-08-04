# Connect eVi to MCP servers — and expose eVi over MCP

Give the agent tools it doesn't ship with by pointing it at an existing
[Model Context Protocol](https://modelcontextprotocol.io) server — a filesystem
server, a git server, a Jira server — no plugin code required. Then flip it
around: run eVi *as* an MCP server so Claude Desktop, Cursor, or Cline can reach
into your memory and tools.

> **Uses:** CLI (`evi mcp …`) · config (`[tools] mcp`, `~/.evi/mcp.json`). The
> `mcp` extra: `pip install "evi-assistant[mcp]"`.

eVi plays **both roles**, and this recipe does both in turn. Everything is
local-first: client servers run as child processes over stdio, and the serve
side defaults to stdio too — nothing touches the network unless you opt into
`--http`.

## Direction 1 — eVi as a client (consume other servers' tools)

Turn the client on, then register a server. Two servers to start with — a
filesystem server (scoped to one directory) and a git server for this repo:

```toml
# ~/.evi/config.toml
[tools]
mcp = true            # off by default — enables the whole MCP client layer
```

```bash
evi mcp add filesystem npx -- -y @modelcontextprotocol/server-filesystem C:/Users/me/projects
evi mcp add git uvx -- mcp-server-git --repository C:/evi
```

Use `--` before any argument that starts with a dash, or Typer will read it as an
option to `evi` itself. Prefer a file? Copy
[`examples/mcp.json`](../examples/mcp.json) to `~/.evi/mcp.json` and edit the
`command`/`args` for each server.

Confirm the tools actually came across before you rely on them:

```bash
evi mcp list-tools
```

Each discovered tool is registered under its server's name — `filesystem.read_file`,
`git.git_log` — so two servers can't collide, and the agent calls them mid-chat
like built-ins. A server that fails to launch is logged once and skipped; the rest
of eVi keeps working (**fail-open by design**).

For a **remote** server (streamable HTTP or SSE) instead of a spawned process:

```bash
evi mcp add-http linear https://mcp.linear.app/mcp -H "Authorization=Bearer <token>"
```

Managing servers: `evi mcp list-servers`, `evi mcp enable <name>` / `disable <name>`,
`evi mcp remove <name>`, and `evi mcp path` to print where `mcp.json` lives.

### Keep a synced mcp.json scoped per machine

If you sync `mcp.json` across machines, gate which servers are allowed to load
here without editing the shared file:

```toml
[tools]
mcp_allow = ["filesystem", "git"]   # empty = allow all; otherwise a strict allowlist
mcp_max_output_chars = 20000        # clip a chatty server's result (0 = unlimited)
```

## Direction 2 — eVi as a server (expose eVi to a desktop AI app)

Run eVi as an MCP server and other agents can call a **curated** subset of eVi's
tools, read your long-term memory entries as resources, and pull your saved
slash-command templates as prompts:

```bash
evi mcp serve                                  # stdio — what a desktop client spawns
evi mcp serve --categories memory,index,git    # widen or narrow the exposed surface
```

The default surface is the `memory`, `index`, `calendar`, and `git` categories.
**shell, computer, and code-write tools are deliberately not exposed** — and
because MCP has no interactive approval prompt, any destructive shell command that
*did* slip into the surface is refused by the [destructive-command
guard](guardrails.md) rather than run headless.

Rather than run it by hand, let the desktop client spawn it. Generate a paste-ready
config snippet:

```bash
evi mcp serve-config --categories memory,index,git
```

That prints an `mcpServers` block (pointing at your current Python) to drop into
Claude Desktop's or Cursor's MCP config. Restart the client and eVi's tools,
memory, and prompts appear.

### Serve over HTTP for a remote client

Only cross the network deliberately — and gate it with a bearer token:

```bash
evi mcp serve --http --host 0.0.0.0 --port 8765 --token "$(openssl rand -hex 16)"
```

The server mounts at `/mcp`; clients must send `Authorization: Bearer <token>`.
`--http` without `--token` prints a warning and leaves the endpoint
unauthenticated — fine bound to `127.0.0.1`, never on `0.0.0.0`.

## Where to go next

- [Package and use a skill](package-and-use-a-skill.md) — the in-process way to
  extend the agent when you'd rather write instructions than wire up a server.
- [Give the agent persistent memory](persistent-memory.md) — the same entries
  the serve side hands out as MCP resources.
- [Guard an agent's inputs and outputs](guardrails.md) — the guard that keeps a
  headless MCP surface from running something destructive.

The full field reference (transports, the MCP panel in web/desktop, plugin-supplied
servers, security notes) is in
[`docs/features/mcp.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/mcp.md).
