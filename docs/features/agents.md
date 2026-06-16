# Agents & Orchestration

## Overview

eVi is single-user and local-first, but a single linear chat is not always the
best shape for a task. **Agents & Orchestration** is the family of features that
let one eVi run *other* scoped agents — and even borrow a second machine — to
break work into focused pieces:

- **Subagents** — hand a narrow task (e.g. "where is X defined", "draft an
  implementation plan") to a throwaway agent with its own fresh context and a
  restricted tool set. Its report comes back as a tool result, so your main
  conversation stays small and on-topic.
- **Parallel research** — fan several independent sub-questions out to read-only
  Explore subagents at once, then synthesize their findings.
- **Workflows** — author a multi-step, optionally-parallel pipeline in TOML
  where each step is its own headless agent and later steps interpolate earlier
  steps' output.
- **Federation** — delegate a self-contained task to a *trusted peer eVi* (e.g.
  your GPU box) over plain HTTP, so a small local model can offload heavy work to
  a bigger remote one without any shared cloud.

Why use it: keep the main context clean, parallelise tool-heavy investigation,
script repeatable multi-stage jobs, and scale across your own machines — all
while staying local and privacy-first.

## How it works

### Subagents

A subagent is the **same `Agent` class** as the main loop, constructed with a
focused system prompt and a restricted tool list. The runner
(`evi/llm/subagent.py`) reuses the parent's LLM client and config, builds the
agent, runs it to completion (default `max_turns=6`), and returns the
concatenated final assistant text. A short `[trace]` of tool calls (each tool
name + a 200-char preview) is appended so you can see what it did. If the
subagent errors, you get `ERROR: subagent failed: …` instead.

Tool scoping is by **category**, not per-tool toggle. A subagent profile lists
the tool *categories* it may use, and the runner pulls every registered tool in
those categories — so an Explore subagent gets read-only `fs` tools even if the
global `fs` toggle is otherwise managed elsewhere.

Built-in profiles (`SUBAGENT_PROFILES`):

| Profile | Tool categories | Purpose |
|---|---|---|
| `explore` | `fs` | Read-only investigation; ends with a bulleted summary. |
| `plan` | *(none)* | Produces a numbered implementation plan; calls no tools. |

These are exposed as tools (category `subagent`):

- `delegate(profile, task)` — run any named profile (built-in or plugin).
- `delegate_explore(task)` — shortcut for the `explore` profile.
- `delegate_plan(task)` — shortcut for the `plan` profile.
- `parallel_research(tasks)` — run up to **6** Explore subagents concurrently and
  return a combined "Parallel research findings" report (you synthesize it).

> Note on concurrency: parallel subagents overlap orchestration and tool calls,
> but a single local backend serialises the actual model inference (one model,
> one GPU). The big wins are on tool-heavy work or a remote / multi-GPU backend.

### Custom profiles

Define your own subagent profiles without editing JSON by hand:

```bash
evi agents new security --prompt "You are a security reviewer." --tools fs,code
evi agents                # lists built-in + user + plugin profiles
```

This writes `~/.evi/agents.toml` (same `[[agent]]` schema as below) and the
profile is then usable by bare name via `delegate(profile="security", task=…)`.
Re-run with `--force` to overwrite; built-in names (`explore`, `plan`) are
reserved.

### Plugin profiles

Installed plugins can add subagent profiles via a `<plugin>/agents.toml`. Each
profile is namespaced `<plugin>:<name>` so it can never shadow a built-in. The
same file format works for the user-level `~/.evi/agents.toml` (referenced by
bare name). Malformed files or entries are silently skipped (plugin scanning
never breaks core subagents):

```toml
[[agent]]
name = "security"
system_prompt = "You are a security reviewer…"
tools = ["fs"]            # tool *categories* the subagent may use
```

### Agent teams

Agent teams (`evi/teams.py`) are a **dynamic, claimable** task list — distinct
from workflows (a declarative DAG you author) and ultracode (a fixed pipeline). A
**lead** decomposes a goal into tasks with `blocked_by` dependencies, persisted to
a shared file (`~/.evi/team.json`); **teammates** (subagents) then claim ready
tasks, run them, and record results, draining the list in dependency order with
bounded parallelism. Claims are thread-safe (no double-claim) and the store is
persisted, so the team's progress is inspectable and survives a restart.

```bash
evi team new "add OpenAPI docs to the HTTP API"   # lead decomposes -> task list
evi team list                                      # see tasks + status + deps
evi team run --workers 3                            # teammates drain it in dep order
evi team add "write the integration test" --blocked-by t2   # hand-add a task
evi team clear
```

A task whose dependency **fails** is reported (its dependents stay pending rather
than hanging the run). The core is model-free — `TeamStore` + `drain_team(store,
run_one)` (injected runner, like ultracode) — and re-exported from `evi.sdk`.

**Distributed teammates (`evi team run --peers`).** Local teammates all share one
backend, so they serialise on a single GPU. With `--peers` (alias `--distribute`)
the team round-robins claimed tasks across the local backend **and reachable
[federation peers](#federation)** — each peer is separate hardware, so this is
where team parallelism becomes real wall-clock speedup (e.g. your main box + a
P40 box draining one task list together). A peer that goes unreachable mid-run
falls back to running its task locally, so it never stalls the team. Peers must be
running `evi web --host 0.0.0.0` and listed via `evi peer add`.

### Workflows

A workflow (`evi/workflows.py`) orchestrates **independent** steps, where a
*recipe* is by contrast a sequence of turns through one shared conversation.
Each step:

- runs its **own headless agent** (fresh context),
- runs **concurrently** with adjacent steps marked `parallel = true` (a
  contiguous run of parallel steps forms a fan-out block),
- can interpolate earlier steps' output and workflow vars into its prompt via
  `{step_id}` / `{var}`.

A parallel block sees a *frozen snapshot* of outputs produced **before** that
block, so a following sequential step is the natural fan-in point. Use `{{` and
`}}` for literal braces. Unknown references raise a clear `WorkflowError` listing
the known names.

Workflows live at `~/.evi/workflows/<name>.toml`. Each step may set a `mode`
(`chat` | `cowork` | `code`) to swap to that mode's tool preset; otherwise it
uses your enabled tools. Workflow runs are **unattended**, so every step's agent
auto-approves all tool calls.

### Federation

Federation (`evi/federation.py`) lets your eVi POST a task to a peer eVi's
`/api/federate` endpoint and return its answer. Peers live in
`~/.evi/peers.json` (kept separate from synced config so per-peer tokens don't
leak). Transport is plain HTTP with the peer's existing web bearer token — no new
trust model. Default delegation timeout is **180 s**.

On the **serving** side, `/api/federate` is **off unless** the peer sets
`[federation] serve = true` (otherwise HTTP 403). When enabled, the served task
runs **non-interactively**: the peer's agent denies any tool not already in its
auto-approve list (`permission_callback` returns `False`), so a remote task can't
trigger surprises like shell or network tools. A peer-side error comes back as a
JSON `error`, surfaced to the caller as `ERROR: peer <name>: …`.

## Setup

### Subagents (config: `~/.evi/config.toml`, section `[tools]`)

Subagent *tools* are gated by the `subagent` tool toggle, which is **off by
default** (it's part of the `code` mode preset). The `subagent` permission
category also deliberately does **not** auto-approve — spawning agents is
something you may want to see first.

```toml
[tools]
subagent = true               # enable delegate / delegate_explore / parallel_research

[auto]
# Optional: auto-approve subagent spawns so they run without a prompt.
auto_approve = ["fs", "code", "memory", "skills", "image", "subagent"]
```

No pip extras are required for subagents or workflows — they're core.

### Workflows (files: `~/.evi/workflows/*.toml`)

No config keys. Create the directory implicitly with `evi workflow new <name>`,
which writes a starter template. A workflow file supports:

- `name`, `description` — metadata.
- `[vars]` — default variables (override at run time with `--var k=v`).
- `[[steps]]` — each needs a non-empty `prompt`; optional `id` (defaults to
  `step<N>`, must be unique), `parallel = true`, `mode`, and `label`.

### Federation (config: `[federation]`; peers: `~/.evi/peers.json`)

To **delegate to** peers, enable the network capability and list your peers:

```toml
[tools]
federation = true             # enables the delegate_peer tool (off by default)

[federation]
serve = false                 # this instance answers federated tasks? default false
```

```json
[
  { "name": "gpu", "url": "http://gpu-box:8473", "token": "<peer web token>" }
]
```

In `peers.json`, `name` and `url` are required; `token` is the peer's web bearer
token (omit if the peer needs no auth). Missing or malformed files yield no
peers; bad entries are skipped.

To let this instance **answer** federated tasks you need **two** things:

1. **Serving on.** Toggle **Settings → Peers → "answer federation requests"**
   (writes `[federation] serve = true`; takes effect immediately, no restart —
   no need to hand-edit `config.toml`). The CLI/`peers.json` equivalent is still
   `[federation] serve = true`.
2. **Reachable on the network.** The server must listen beyond loopback. Run
   `evi web --host 0.0.0.0` (and allow the port — default **8473** — through the
   firewall). **Desktop caveat:** the desktop app currently binds the bundled
   server to `127.0.0.1` only, so a desktop instance isn't reachable as a peer
   yet — run the server directly on the peer box with
   `evi-server.exe --host 0.0.0.0 --port 8473` (or `evi web --host 0.0.0.0`).
   A "scan finds nothing" with the firewall open almost always means the peer is
   bound to loopback, not the firewall.

The two are independent: serving-on without LAN-binding answers only localhost;
LAN-binding without serving-on returns HTTP 403 to delegations. The full
two-node path (probe → reachable → delegate → serve-gating → distributed team
runner) is covered by `tests/e2e/test_federation_network.py`.

## Usage

### CLI — list subagent profiles

```text
evi agents
```

Lists every profile (built-in + plugin) with its tool categories, origin, and a
prompt preview. Built-in profiles are used via the `delegate(profile, task)`
tool from inside a chat.

### Inside a chat — delegate

When the `subagent` tools are enabled, the model can call them directly. You just
ask in natural language, e.g. "explore where the config is loaded and report
back" → the agent calls `delegate_explore`. For broad investigations it can call
`parallel_research` with several sub-questions.

### CLI — workflows

```text
evi workflow new <name>            # write a starter ~/.evi/workflows/<name>.toml
evi workflow list                  # list saved workflows (step + parallel counts)
evi workflow show <name>           # show steps, vars, and prompts
evi workflow run <name> [--var k=v ...] [--json]
```

`evi workflow run` executes each step as its own headless agent (tools
auto-approved). `--var k=v` overrides a workflow var (repeatable). `--json`
prints `{step_id: output}` as JSON instead of the live, step-by-step console
output.

### CLI — federation / peers

```text
evi peer list                      # configured peers, with live reachability + version
evi peer add <name> <url> [--token …] [--overwrite]
evi peer remove <name>
evi peer scan [--port N]           # sweep the local /24 for running eVi instances
evi peer run <name> "<task>"       # delegate a task to a peer and print its answer
```

The `delegate_peer(peer, task)` tool does the same from inside a chat when the
`federation` tool toggle is on.

### Web / Desktop — the Peers panel

**Settings → Peers** manages federation without the CLI:

- lists configured peers with a live status dot (green = reachable, with the
  peer's eVi version + active model from its `/api/health`; red = unreachable —
  machine off or `evi web` not running);
- shows whether **this** instance is serving federation requests
  (`[federation] serve`);
- an add-peer form (name / URL / optional token) and per-peer **Remove**;
- **Scan local network** — probes your /24 for eVi instances on port 8473
  (raw-socket connect, then an `/api/health` fingerprint so a random web server
  isn't mistaken for a peer; ~2 s). Hits show host, version, and model, with a
  one-click **Add**; already-configured peers are marked.

Backed by `GET /api/peers`, `POST /api/peers[/remove]`, and
`POST /api/peers/scan` (`{port?, hosts?}`). Note: discovery requires the peer
to listen beyond loopback — start it with `evi web --host 0.0.0.0` and allow
the port through its firewall.

### Web / desktop — Dispatch view

The web UI's Dispatch view (`GET /api/dispatch`) shows every live session with
its mode, message count, token usage/ceiling, and pending tool count, alongside
the workflows you can launch. You can run a workflow headless server-side via
`POST /api/dispatch/workflow/{name}` (body `{ "vars": { ... } }`), where each
step runs as its own auto-approved agent and the response is `{step_id: output}`.

**Live agent-watch.** While the Dispatch panel is open it subscribes to
`GET /api/dispatch/stream` (Server-Sent Events) and re-renders the session list
in real time — a pulsing green dot marks any session whose turn is mid-flight, so
you can watch agents work across tabs (eVi's analogue of the Claude Code Agent
view). The stream emits a snapshot every `interval` seconds (default 1.5,
clamped 0.25–10); pass `limit=N` to end it after N snapshots (one-off polls /
tests).

The web server also exposes the receiving end of federation,
`POST /api/federate` (body `{ "task": "...", "mode": "" }`), which only responds
when `[federation] serve = true`.

## Examples

### Example 1 — A parallel research-and-synthesize workflow

Create the workflow:

```text
evi workflow new research
```

Edit `~/.evi/workflows/research.toml` so the two angles run in parallel and the
last step fans them in:

```toml
name = "research"
description = "Plan, research two angles in parallel, then synthesize."

[vars]
topic = "local-first AI"

[[steps]]
id = "plan"
prompt = "Outline an approach to research {topic}."

[[steps]]
id = "pros"
parallel = true
prompt = "Given this plan, list the upsides of {topic}.\nPlan: {plan}"

[[steps]]
id = "cons"
parallel = true
prompt = "Given this plan, list the downsides of {topic}.\nPlan: {plan}"

[[steps]]
id = "synth"
prompt = "Synthesize a balanced take.\nUpsides: {pros}\nDownsides: {cons}"
```

Run it, overriding the topic and asking for machine-readable output:

```text
evi workflow run research --var topic="self-hosted vector search" --json
```

`pros` and `cons` execute concurrently (each its own headless agent off the same
`plan` snapshot); `synth` waits for both and merges them. Output:

```json
{
  "plan": "1. Define evaluation criteria…",
  "pros": "- Data never leaves the device…",
  "cons": "- Index maintenance overhead…",
  "synth": "On balance, self-hosted vector search…"
}
```

### Example 2 — Offload heavy work to a GPU peer

On the **GPU box**, opt in to serving and start the web server:

```toml
# ~/.evi/config.toml on the GPU box
[federation]
serve = true
```

On your **laptop**, register the peer and enable the federation tool:

```json
// ~/.evi/peers.json on the laptop
[
  { "name": "gpu", "url": "http://gpu-box:8473", "token": "PASTE_GPU_WEB_TOKEN" }
]
```

```toml
# ~/.evi/config.toml on the laptop
[tools]
federation = true
```

Delegate a self-contained task and print the peer's answer:

```text
evi peer list
# →   gpu  http://gpu-box:8473 (token set)

evi peer run gpu "Summarize the architecture of the repo at /srv/project and list its top 5 risks."
```

The big model on the GPU box runs the task non-interactively (only its
auto-approved tools are available) and returns the final text.

### Example 3 — A plugin-supplied "security" subagent

Drop an `agents.toml` into a plugin directory to add a namespaced profile:

```toml
# <plugin>/agents.toml
[[agent]]
name = "security"
system_prompt = "You are a security reviewer. Read code and report vulnerabilities. Do not modify anything."
tools = ["fs"]
```

Confirm it's registered, then use it from a chat via the generic delegate tool:

```text
evi agents
# →   <plugin>:security  (fs · plugin)
#       You are a security reviewer. Read code and report vulnerabilities…
```

Inside a chat (with `subagent` tools enabled), the model invokes it as
`delegate("<plugin>:security", "Audit the auth module for injection risks.")`.

## Notes / limits

- **Subagent tools are off by default.** Set `[tools] subagent = true` (it's also
  included in `code` mode). Spawns are not auto-approved unless you add
  `subagent` to `[auto] auto_approve`.
- **Category-level scoping only.** Profiles grant whole tool *categories*; there
  is no per-tool allow-list inside a profile. The `explore` profile is read-only
  (`fs`); the `plan` profile gets no tools at all.
- **Parallelism is bounded.** `parallel_research` caps at **6** concurrent
  Explore subagents; the parallel runner uses up to 4 workers by default. A
  single local model serialises inference regardless, so expect speedups mainly
  on tool-heavy work or remote/multi-GPU backends.
- **Subagent depth.** Each subagent runs at most `max_turns=6` and returns text
  plus a short tool trace; it's meant for bounded, one-shot delegation, not
  open-ended sessions.
- **Workflows are unattended.** Every step auto-approves all tool calls
  (`enable_auto_all`). Don't put steps that touch dangerous tools (shell,
  computer control, network writes) into a workflow you run unsupervised unless
  you trust the prompts. Malformed workflow files are skipped by `list`, and
  template errors (e.g. an unknown `{ref}` or unescaped brace) raise a clear
  `WorkflowError`.
- **Federation is opt-in on both ends.** The caller needs `[tools] federation =
  true` and a `peers.json` entry; the responder needs `[federation] serve =
  true`. Serving is **deny-by-default** for tools — a federated task can only use
  the responder's already-auto-approved tools, never trigger fresh prompts.
- **Security of tokens.** Federation reuses the peer's **web bearer token** over
  plain HTTP — there's no separate trust model and no TLS by default. Keep peers
  on a trusted network (LAN/VPN); `peers.json` lives outside synced config so
  per-peer tokens don't propagate.
- **Fail-open / fail-safe behaviors.** Plugin profile scanning never breaks core
  subagents (errors are swallowed); a missing/malformed `peers.json` simply
  yields no peers; an unreachable peer raises `FederationError` (surfaced as a
  red CLI message or an `ERROR: …` tool result), and a peer-side error is
  returned as `ERROR: peer <name>: …` rather than crashing the caller.
- **Exact paths.** Config: `~/.evi/config.toml` (`%USERPROFILE%\.evi\config.toml`
  on Windows; override the base dir with `EVI_HOME`). Peers:
  `~/.evi/peers.json`. Workflows: `~/.evi/workflows/<name>.toml`. Plugin
  profiles: `<plugin>/agents.toml`.
