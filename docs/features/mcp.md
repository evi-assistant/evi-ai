# MCP (client + serve)

## Overview

[MCP (Model Context Protocol)](https://modelcontextprotocol.io) lets eVi connect to other tool-providing programs — and lets other AI apps connect to eVi. eVi plays **both roles**:

- **MCP client** — eVi launches one or more external MCP servers (a filesystem server, a git server, a Jira server, whatever you configure) and exposes *their* tools to eVi's agent. Your assistant can now call those tools mid-conversation as if they were built in.
- **MCP server** (`evi mcp serve`) — eVi runs *as* an MCP server so other agents (Claude Desktop, Cursor, Cline, Continue) can reach into eVi: call a curated subset of eVi's own tools, read your long-term memory entries as resources, and pull your saved slash-command templates as prompts. With the opt-in `agent` category, the client can also file tasks in a repository's [task queue](tasks.md), start them, follow and steer the runs and collect their reports ([below](#the-agent-category-the-directors-task-api)).

Use the **client** side when you want eVi to do more than its built-in toolset allows, without writing a Python plugin — just point it at an existing MCP server. Use the **serve** side when you'd rather drive eVi's tools and memory from a desktop AI app you already use.

Everything is local-first: client servers run as child processes on your machine over stdio, and the serve side defaults to stdio (spawned by the client) — nothing touches the network unless you explicitly opt into HTTP.

## How it works

### Client (consuming external servers)

1. On startup, eVi reads `~/.evi/mcp.json` (plus any `mcp.json` shipped by installed plugins) into a list of `MCPServer` launch configs. Missing or malformed files yield an empty list — eVi keeps running.
2. The consume-side allowlist `[tools] mcp_allow` is applied (see Setup). Then each **enabled** server is spawned as a child process over **stdio** using the `mcp` Python SDK.
3. For each server, eVi calls `initialize` and `list_tools`, then wraps every discovered tool as a native eVi tool named **`<server>.<tool>`** and registers it in eVi's tool registry. (Plugin-supplied servers are namespaced `<plugin>:<server>`, so they can't collide with your own.)
4. When the agent calls one of those tools, the call is forwarded to the server through an async-to-sync **bridge** (one asyncio loop on a daemon thread), so eVi's synchronous tool layer can drive the all-async MCP SDK.
5. Tool results are flattened to text for the model: text content is concatenated; non-text items (images, etc.) become a `[<type> omitted]` placeholder; a result the server flags as an error is prefixed `ERROR:` (once — text that already starts with `ERROR:` isn't prefixed again).
6. A call that runs past its timeout is cancelled, and the server is told so (`notifications/cancelled`), so it can stop the work too.

**Fail-open by design:** a server that fails to start is logged once (`MCP server <name> failed to start: <reason>`, where the reason names the real error, e.g. `HTTP 401 Unauthorized` or `ConnectError: …`) and skipped — the rest of eVi keeps working. On shutdown eVi unregisters the added tools first (so an in-flight agent loop can't reach a closed session), then closes each server session.

### Server (`evi mcp serve`)

eVi builds a standard MCP `Server` named `evi` exposing three things, all drawn from eVi's own single sources of truth:

- **Tools** — entries from eVi's tool registry, filtered by **category** (and optionally an exact-name allow-list). The same name / description / JSON-schema the agent already uses is reused verbatim. The default surface is the categories `memory`, `index`, `calendar`, `git`. **shell, computer, and code-write tools are deliberately NOT exposed by default.** The `agent` category is not from the registry: it is the director's task API, served only when you name it ([below](#the-agent-category-the-directors-task-api)).
- **Resources** — each of your long-term memory entries as `evi://memory/<name>` (mimeType `text/markdown`).
- **Prompts** — each saved slash-command template (`~/.evi/commands/*.md`); each accepts an optional free-text `args` argument substituted for `{args}` in the template body.

Transports: **stdio** (the default; what a desktop MCP client spawns) or **streamable HTTP** (`--http`) served at the `/mcp` path. Over HTTP, `--token` requires a bearer token on every request; it is **required** for any non-loopback `--host`, and for the `agent` category on loopback too. A server without a token answers only to loopback `Host` / `Origin` headers, so a web page that rebinds its own hostname to 127.0.0.1 (DNS rebinding) can't call the tools.

A tool failure served this way (an unknown tool, a call the destructive-command guard refuses, a tool's own `ERROR: …`) is returned with MCP's error flag set, on both the 1.x and 2.x SDK lines.

### The agent category: the director's task API

`evi mcp serve --categories agent` lets the **director**, the MCP client that
spawned the server (Claude Desktop, Claude Code, another agent) or holds its
token, hand work to eVi through a stable API instead of a chat. It works on
the [task queue](tasks.md) of one repository: the tasks `evi task` manages,
in `.evi/tasks/`.

The category is never a default: only `--categories …,agent` serves it. It
is never in eVi's tool registry either, so no eVi agent (a chat, the web
app, a task's own run) has these tools: an agent can't hand tasks to
itself.

| Tool | What it does |
|---|---|
| `task_create(title, why?, hints?, acceptance?, files_in_scope?, required_checks?, implementer?, id?)` | Files a task, `open`, in `.evi/tasks/<id>.toml`, as `evi task new` does, and returns it. `hints` (a string) is your guidance on how to go about it, shown after `why` in the implementer's prompt. `files_in_scope` are the only files the run may write ([What a task may change](tasks.md#what-a-task-may-change)); a task without them is filed, but its run ends in `error` without starting. |
| `task_list(status?)` | `{tasks, problems}`: each task's `id`, `title`, `status`, `label`, `implementer`, `branch` and `attempts`, and the task files that can't be read. |
| `task_show(id)` | `{task, label, path}`: every field, the status as it reads, and the file. |
| `task_start(id, backend?, model?, max_turns?, base?)` | Queues a run of the task and answers at once with `{run_id, task_id, state, position}` (`position`: how many runs are ahead of it). `max_turns` is 1 to 200 (default 30); `backend` names a saved backend; `base` is where a new task branch starts. |
| `run_status(run_id)` | `state` (`queued`, `running`, `done`, `cancelled`, `error`), when it was queued, started and ended, `elapsed_s`, `turns_used` of `max_turns`, the steering notes sent, applied and unapplied, the last 10 events in one redacted line each, and once it ended its `stop_reason`, `exit_code`, `task_status`, `implemented`, any `error` and `note`. |
| `run_steer(run_id, text)` | Sends the running agent a note, framed as a note from the director. It joins the turn at its next step, without interrupting the work in flight; a note sent before the run's agent exists waits for it. |
| `run_cancel(run_id, reason?)` | A queued run is dropped and its task left as it was. A running one stops at its next step and ends `interrupted` ([below](#limits-cancelling-and-shutting-down)). |
| `run_result(run_id)` | A finished run's v1 [run report](structured-and-batch.md#the-run-report) with its `task` section (whose `out_of_scope` lists the files the run changed outside `files_in_scope`), its path, `stop_reason`, `exit_code`, `implemented`, and the task's status and label after it. An `ERROR` while the run is still going. |

Every result is JSON text; a refusal is an `ERROR: …` result with MCP's error
flag. Each tool takes only the arguments listed: anything else is refused.

**No tool gives a verdict.** None verifies, rejects, signs, submits or
reopens a task, and no argument sets a status, an attestation, a lease or
evidence. A run ends `implemented` at best, and an implemented task can't be
started again. `evi mcp serve` runs as an agent's process (`EVI_AGENT=1`):
the director key is never read there and director tooling refuses to run, so
a signed verdict reads `unverified (no director key here …)` in `task_list`
and `task_show`. You verify from your own shell, with `evi task submit` and
`evi task verify` ([Verify and reject](tasks.md#verify-and-reject-director-only)).

#### Runs, one at a time

One worker per server runs the starts one at a time (one GPU), first in
first out, through the same code `evi task start` runs: the claim, the
worktree (branch `task/<id>` in `.worktrees/task__<id>`), the implementer
policy with the verify gate on, eVi's own run of the required checks, the
run report, the [evidence bundle](evidence.md) and the task's bookkeeping.
Its agent is built the way `evi task start` builds it, and a delegate
CLI-agent backend is refused. At most 16 runs wait behind the running one.

`task_start` refuses at once, queueing nothing, when the task isn't `open`
(or claimed with a lease that ran out, or by this server), someone else
holds a live claim on it, the repository isn't trusted, the backend name
isn't a saved backend, the task is already queued or running, the queue is
full, or the server is shutting down. What a run records is in [Task queue
→ Over MCP](tasks.md#over-mcp-evi-mcp-serve).

**Leases.** A run claims its task with a 10-minute lease, held by
`<user>@<host> (evi mcp serve, pid N)`, and renews it every 2 minutes once
its agent is built, so a long run keeps its task. If the server dies, the
claim runs out within 10 minutes and the task can be started again, by
`evi task start` or another server. Each renewal rewrites the lease in the
tracked task file.

#### Limits, cancelling and shutting down

- **`[tasks] max_run_minutes`** (default 60, counted from when the run
  starts, not while it waits) cancels a run that takes longer, as
  `run_cancel` does. See
  [configuration](../configuration.md#task-runs-over-mcp--tasks-in-configtoml).
- **`run_cancel`** stops a running run at its next step: in its turn, or in
  the required check eVi runs after it, whose process tree is killed. It
  ends with stop reason `interrupted`, the task back to `open` with the
  attempt counted, and a bundle with its diff and transcript but no checks
  run. A cancel that arrives once the outcome is being recorded leaves the
  outcome as it is (the run's `note` says so) and only stops the bundle's
  own run of the checks.
- **Closing the server** (the client disconnects, or Ctrl+C) cancels the
  running run, waits up to 15 seconds for it to record its outcome, and drops
  the queued runs, whose tasks stay as they were. A run that can't record in
  time keeps its claim until the lease runs out.
- **Finished runs** stay readable with `run_status` and `run_result` for 24
  hours, the newest 32 of them; after that, `task_show`'s `last_result` names
  the run report.

#### What may be served beside it

While a run goes on, the server process works in the task's worktree, as
`evi task start` does (its `monitor` commands run there). A tool of another
category that resolves a path, runs a command or calls git in the process's
folder would act on the run's work as if it were the repository. So beside
`agent` only **`memory`**, **`calendar`** and **`web`** may be served: their
tools never use the process's folder, and the fix ledger (`record_fix`,
`search_fixes`) is held to the served repository. Any other category beside
`agent` (`git`, `index`, `fs`, `code`, `shell` …) is refused with exit 2;
serve it from a second `evi mcp serve`.

#### Transport, `--repo` and the client snippet

- **stdio**, spawned by the client, is the usual way.
- **HTTP** needs `--token`, even on a loopback `--host`: without one, any
  program on this machine could start and steer task runs.
  `evi mcp serve --http --categories agent` without `--token` exits 2.
- **`--repo DIR`** names the repository whose queue the server serves
  (its main checkout, also from a worktree or a subfolder). Without it, the
  repository the server starts in. Outside a git repository the server
  refuses to start (exit 2).
- **`evi mcp serve-config --categories …,agent`** adds `--repo <the
  repository you run it in>` to the snippet, because a desktop client may
  start the server in any folder, and refuses the categories the server
  would refuse.
- **The web and desktop app don't serve it.** Settings → MCP → Serve
  (`POST /api/mcp/serve`) answers `400` (`could not build MCP server: …`) for
  a category list with `agent` in it, and its client snippet
  (`GET /api/mcp/serve/config`) answers `400` and points to
  `evi mcp serve-config` in the repository.

## Setup

### Client side

**1. Define your servers** in `~/.evi/mcp.json` (on Windows: `C:\Users\<you>\.evi\mcp.json`; the directory honors the `EVI_HOME` environment variable if set). The file is a **JSON array** of server objects:

| Field | Type | Required | Default | Meaning |
|-------|------|----------|---------|---------|
| `name` | string | yes | — | Server label; tools register as `<name>.<tool>` |
| `command` | string | stdio only | — | Executable to launch (e.g. `npx`, `uvx`, `python`) |
| `args` | string[] | no | `[]` | Arguments passed to the command |
| `env` | object | no | `{}` | Extra environment variables for the child process |
| `transport` | string | no | `stdio` | `stdio` (spawn `command`) or a remote transport: `http` (streamable-http) / `sse` |
| `url` | string | http/sse only | — | The remote server's endpoint URL |
| `headers` | object | no | `{}` | Request headers for the remote transport (e.g. `Authorization`) |
| `enabled` | bool | no | `true` | Set `false` to keep a server in the file but skip it |

A stdio entry needs `command`; a remote entry needs `url` (transport defaults to `http` when a `url` is present). Entries missing the required field are skipped silently. Print the exact path any time with `evi mcp path`.

**Remote (HTTP) servers.** To connect to a hosted MCP server instead of spawning a local process:

```json
[
  { "name": "linear", "transport": "http", "url": "https://mcp.linear.app/mcp",
    "headers": { "Authorization": "Bearer <token>" } }
]
```

or add it from the CLI (header values are never echoed back by `list-servers`):

```bash
evi mcp add-http linear https://mcp.linear.app/mcp -H "Authorization=Bearer <token>"
evi mcp add-http legacy https://host/sse --sse        # legacy SSE transport
```

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

If MCP is enabled but the package isn't installed, eVi prints a hint and continues without MCP tools. The extra admits `mcp>=1.30,<2.4`; CI runs the MCP tests on the 1.30 line, the 2.0 line and the newest release the range allows, so installing or upgrading the extra may upgrade an older `mcp`.

### Server side (`evi mcp serve`)

No config keys are required — `evi mcp serve` works out of the box once the `mcp` extra is installed. You control the exposed surface with command flags:

- `--categories` / `-c` — comma-separated tool categories to expose (default `memory,index,calendar,git`). `agent` adds the [director's task API](#the-agent-category-the-directors-task-api); beside it only `memory`, `calendar` and `web` may go, and any other category with it exits 2 (`error: refusing to serve the agent category beside git: …`).
- `--tools` — optional comma-separated allow-list of exact tool names *within* those categories (the agent category's tools included).
- `--repo DIR` — with the agent category: the repository whose task queue it serves (default: the one the server starts in).
- `--http` — serve streamable HTTP instead of stdio.
- `--host` (default `127.0.0.1`) and `--port` (default `8765`) — HTTP bind address.
- `--token` — require `Authorization: Bearer <token>` on every HTTP request. **Required for a non-loopback `--host`, and for the agent category on any host** (`error: refusing to serve the agent category over HTTP without --token …`, exit 2). Without one, `evi mcp serve --http --host 0.0.0.0` exits with code 2 and prints `error: refusing to serve MCP over HTTP on 0.0.0.0 without --token. Pass --token, or bind to 127.0.0.1.` A tokenless server on a loopback address (127.0.0.1 or another 127.x address, `localhost`, `::1`) starts with the warning `⚠ --http without --token: any program on this machine can call these tools. Pass --token to require one.` and answers only requests whose `Host` is a loopback name — `127.0.0.1`, `localhost`, `[::1]` or the address it is bound to — (anything else gets `421`) and whose browser `Origin`, if any, is a loopback page (anything else gets `403`).

## Usage

### CLI commands (client side)

| Command | What it does |
|---------|--------------|
| `evi mcp path` | Print the path to your `mcp.json` |
| `evi mcp list-servers` | List configured servers from `mcp.json` with on/off state |
| `evi mcp add <name> <command> [args…]` | Add a server. Use `--` before args that start with a dash; `--env KEY=VALUE` (repeatable) for secrets; `--disabled`; `--overwrite` |
| `evi mcp remove <name>` | Remove a user server (plugin-supplied `plugin:name` servers go with `evi plugin remove`) |
| `evi mcp enable <name>` / `disable <name>` | Flip a server's `enabled` flag without deleting it |
| `evi mcp list-tools` | Start the servers and enumerate every tool they expose |

```bash
# example: the filesystem server, scoped to your projects dir
evi mcp add filesystem npx -- -y @modelcontextprotocol/server-filesystem ~/projects
evi mcp list-tools          # verify it connects + see what it exposes
```

Once `tools.mcp = true` and servers are configured, MCP tools are available automatically in normal `evi` usage (CLI chat, the REPL, and the web UI) — they appear in the registry as `<server>.<tool>` and the agent calls them like any other tool. `evi mcp list-tools` warns if `tools.mcp` is still `false`.

### CLI commands (server side)

| Command | What it does |
|---------|--------------|
| `evi mcp serve` | Run eVi as an MCP server over **stdio** (what a desktop client spawns) |
| `evi mcp serve --http --token <secret>` | Run as a streamable-HTTP server, bearer-token gated (a token is required for a non-loopback `--host`) |
| `evi mcp serve-config` | Print a ready-to-paste client config snippet (`mcpServers` block) |
| `evi mcp serve --categories memory,agent` | Also serve the director's task API for the repository here (`--repo DIR` for another) |
| `evi mcp serve-config --categories memory,agent` | The snippet for it, with `--repo` set to the repository you run it in |

### Web / Desktop — the MCP panel

**Settings → MCP** manages servers without touching the file:

- shows whether the `mcp` tool toggle is on, plus the active allowlist (servers
  outside it are marked *blocked by allowlist*);
- one row per server — enabled dot, command + args, env **key names** (values
  are never shown: they may hold API keys), plugin-supplied servers marked and
  read-only;
- per-server **Enable/Disable** and **Remove**, and an add form (name, command,
  args as one shell-style string). Env vars aren't set in the UI — use
  `evi mcp add --env KEY=VALUE` or edit `mcp.json` for those.

Backed by `GET /api/mcp` and `POST /api/mcp[/remove|/toggle]`. The allowlist
itself stays editable as **"MCP server allowlist"** under Tools (bound to
`[tools] mcp_allow`). When `tools.mcp` is enabled and servers are configured,
the web server starts the same `MCPManager` on launch, so MCP tools are
available in browser chat too.

The panel's **Serve** section (`POST /api/mcp/serve`, and the client snippet
from `GET /api/mcp/serve/config`) serves eVi's tools on 127.0.0.1, but never
the `agent` category: both answer `400` when it is named. Run
`evi mcp serve-config --categories agent` in the repository instead.

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

The MCP endpoint is served at `/mcp` (e.g. `http://host:8765/mcp`). Every request must send `Authorization: Bearer <token>` or it gets a `401`. Leave out `--token` here and eVi refuses to start (exit code 2): `0.0.0.0` is not a loopback address.

### Example 4 — Hand a repository's task queue to Claude Desktop

In the repository, from your own terminal (the queue runs tasks only in a
trusted repository):

```bash
evi trust
evi mcp serve-config --categories memory,agent
```

```json
{
  "mcpServers": {
    "evi": {
      "command": "/path/to/python",
      "args": ["-m", "evi", "mcp", "serve", "--categories", "memory,agent", "--repo", "/home/you/project"]
    }
  }
}
```

Paste it into the client's config. The client can now call `task_create`
with a title, acceptance criteria, `hints`, `files_in_scope` and
`required_checks`,
then `task_start` with the new id, poll `run_status` with the `run_id` it
got, steer with `run_steer`, and read `run_result` once the run is `done`.
The work waits uncommitted in `.worktrees/task__<id>`; review and commit it
there, then `evi task submit <id>` and `evi task verify <id>` from your own
shell.

## Notes / limits

- **Local or remote servers.** eVi launches local servers over stdio (child processes) and connects to remote ones over streamable HTTP (`transport: http`, the default when a `url` is set) or legacy SSE, sending the entry's `headers` on every request (see [Remote (HTTP) servers](#client-side)).
- **Fail-open.** A server that won't start (bad command, missing binary) is logged once and skipped — it never blocks eVi. Malformed `mcp.json` entries are dropped silently; a connection that actually fails is what gets logged.
- **Off by default.** Client MCP requires `tools.mcp = true`; without it, `mcp.json` is ignored.
- **Trust the servers you launch.** An MCP server is just a program eVi runs with the `command`/`args`/`env` you give it — it executes with your privileges. Only configure servers you trust, and scope filesystem/git servers to the directories you intend.
- **Curated serve surface.** `evi mcp serve` defaults to `memory,index,calendar,git` and deliberately excludes shell, computer-control, and code-write tools. Widening `--categories` can hand powerful tools to whatever connects — do so consciously.
- **The agent category runs code.** A task's `required_checks` are commands eVi runs in the task's worktree, and the run itself tests the repository's code, so whoever can call `task_create` and `task_start` can have commands of their choosing run in a trusted repository. Serve it only to a client you would let run `evi task start` there. No tool marks a task verified.
- **HTTP security.** `--http` without `--token` is unauthenticated, so eVi allows it only on a loopback `--host` and refuses any other bind (exit code 2). Even then any program on this machine can call the tools, and only loopback `Host` / `Origin` headers are answered (`421` / `403` otherwise), which keeps a DNS-rebinding web page out. For anything beyond this machine, set `--token` (compared in constant time) and prefer running behind TLS. The agent category needs `--token` over HTTP even on loopback. **Settings → MCP → Serve** in the web / desktop app serves on 127.0.0.1 and gets the same loopback-only guard when you leave its token empty; it never serves the agent category.
- **Result flattening.** When eVi consumes a server's tool, non-text content (images, binary) is replaced with a `[<type> omitted]` placeholder so the model knows something was elided rather than silently dropped.
- **Call timeout.** Forwarded client tool calls default to a 120-second timeout; a server that hangs will surface a timeout rather than block forever, and the call is cancelled on the server too.
- **Lazy SDK import.** The `mcp` package is imported only when needed, so the rest of eVi runs fine without the `[mcp]` extra installed — you just won't get MCP tools (client) or be able to `serve`.

Source of record: `evi/mcp/__init__.py`, `evi/mcp/manager.py`, `evi/mcp/bridge.py`, `evi/mcp/servers.py`, `evi/mcp/publish.py` (`AGENT_COMPANIONS`), `evi/mcp/agent_tools.py` (the agent category), and the `mcp_app` commands in `evi/apps/cli/main.py`. Config keys: `evi/config.py` (`ToolToggles.mcp`, `ToolToggles.mcp_allow`, `TaskSettings.max_run_minutes`; `MCP_CONFIG_PATH`).
