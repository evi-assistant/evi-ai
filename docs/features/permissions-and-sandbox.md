# Permissions & Sandbox

## Overview

eVi runs language models locally and lets them call **tools** — read and write
files, run Python, search the web, control the mouse, talk to MCP servers, and
more. Permissions decide *which* of those calls happen automatically, which get
blocked, and which pause to ask you first. The **sandbox** is a separate,
defence-in-depth layer that confines the one tool that runs arbitrary code
(`run_python`) to a read-only filesystem with no network.

You'd use these features to:

- Let trusted, low-risk tools (reading files, recalling memory) run silently
  while risky ones (shell, deleting things, network calls) still prompt.
- Hard-block specific dangerous calls regardless of mode (e.g. `rm` in shell).
- Pre-approve only files under a project directory, or web fetches to a domain
  you trust, without opening up a whole tool category.
- Switch a whole session to "read-only planning" or "approve everything"
  on the fly.
- Run model-generated Python under OS isolation so a buggy or hostile snippet
  can't touch the rest of your disk or phone home.

Everything is local and single-user. There is no remote policy server; the
policy is the `[auto]` and `[tools]` sections of your `~/.evi/config.toml`.

## How it works

### The permission decision

Every time the model requests a tool call, eVi resolves it to exactly one of
**allow**, **deny**, or **ask**. The logic lives in `evi/permissions.py`
(`decide()`), called by the agent before any tool runs. It evaluates layers in
this order:

1. **Mode** (`auto.mode`). Checked first and can short-circuit everything:
   - `yolo` → **allow** every call, unconditionally.
   - `plan` → **deny** every call (read-only planning; the model can think but
     not act).
   - `accept_edits` and `ask` fall through to the lower layers.
2. **Rules** (`auto.rules`). A **first-match** allow/deny list. Each rule is a
   string `<allow|deny> <tool-glob> [arg-glob]`. The tool glob is matched
   against the tool name (`fnmatch`); the optional arg glob is matched against
   the call's string-valued arguments. The first rule that matches wins —
   **so an explicit `deny` here beats trusted dirs/domains below.**
3. **`accept_edits` mode shortcut.** If the mode is `accept_edits` and the
   tool's category is `fs` or `code`, the call is **allowed**.
4. **Auto-approve categories** (`auto.auto_approve`). If the tool's category is
   in this list, it's **allowed** without prompting.
5. **Trusted scopes.** For `fs`/`code` tools whose path arguments resolve to
   somewhere under a `trusted_dirs` entry → **allow**. For `web` tools whose URL
   host matches a `trusted_domains` entry (exact host or any subdomain) →
   **allow**.
6. **Otherwise → ask.** eVi prompts you (in the CLI/REPL or web UI). If there is
   no UI able to prompt — e.g. the headless scheduler, a federation request, or
   a workflow step — an unattended agent **default-denies** anything not
   pre-approved.

The REPL `/auto on` toggle is a session override that sits *above* all of this:
it forces every call to **allow** for the rest of the session, until you
`/auto off`.

### The sandbox

The sandbox (`evi/sandbox.py`) wraps the `run_python` subprocess so it runs with
the filesystem read-only except a throwaway temp work directory, and (by
default) no network:

- **Linux** — `bwrap` (bubblewrap): `--ro-bind / /` plus a writable bind for the
  work dir, plus `--unshare-net` to cut the network.
- **macOS** — `sandbox-exec` with a generated SBPL profile that denies
  `file-write*` outside `/tmp` and the work dir, and denies network.
- **Windows / no sandboxer on PATH** — **not available.** The wrapper returns
  the command unchanged, so the snippet runs *unsandboxed* and `run_python`
  prepends a note saying so.

The sandbox only confines `run_python`. It is not the same thing as the
permission policy — a call can be permitted and still be sandboxed, or permitted
and unsandboxed.

## Setup

All configuration lives in `~/.evi/config.toml` (Windows:
`%USERPROFILE%\.evi\config.toml`). First launch writes defaults; hand-edit the
file, then restart or run `/reload` in the REPL.

### `[auto]` — the permission policy

```toml
[auto]
mode            = "ask"                              # ask | accept_edits | plan | yolo
auto_approve    = ["fs", "code", "memory", "skills", "image"]
rules           = []                                 # first-match "allow|deny <tool> [arg]"
trusted_dirs    = []                                 # auto-approve fs/code under these paths
trusted_domains = []                                 # auto-approve web fetches to these hosts
```

| Key | Default | Meaning |
|-----|---------|---------|
| `mode` | `"ask"` | Top-level policy. `ask` prompts for anything not pre-approved; `accept_edits` auto-allows `fs`/`code`; `plan` denies all tools; `yolo` allows all tools. |
| `auto_approve` | `["fs","code","memory","skills","image"]` | Tool **categories** that run without prompting. Note `shell`, `subagent`, `computer`, `web` are deliberately **not** here. |
| `rules` | `[]` | First-match allow/deny list; explicit `deny` overrides trusted dirs/domains. |
| `trusted_dirs` | `[]` | Paths whose `fs`/`code` calls are auto-approved (resolved, `~` expanded, sub-paths included). |
| `trusted_domains` | `[]` | Hosts whose `web` fetches are auto-approved (exact host or subdomain). |

Valid modes are exactly `ask`, `accept_edits`, `plan`, `yolo`.

### `[tools]` — the sandbox toggle (and tool categories)

Tools are grouped into categories that the permission policy reasons about.
The sandbox is one toggle here:

```toml
[tools]
fs       = true     # read_file / write_file / list_dir          (category: fs)
code     = true     # run_python                                  (category: code)
shell    = false    # not auto-approved by default
web      = false    # web_search / web_fetch — network, opt in
computer = false    # mouse/keyboard control — never default-on
# ... other categories: memory, skills, image, subagent, mcp, voice,
#     transcripts, pdf, sqlite, index, git, federation, ocr, calendar ...

sandbox  = false    # run_python under an OS sandbox where available
```

Set `sandbox = true` to run `run_python` under `bwrap`/`sandbox-exec`. There are
**no pip extras required** for the sandbox itself, but the OS launcher must be
present:

- Linux: install bubblewrap (e.g. `apt install bubblewrap`) so `bwrap` is on
  `PATH`.
- macOS: `sandbox-exec` ships with the OS.
- Windows: no sandboxer exists — `sandbox = true` is honored as "requested" but
  falls back to running unsandboxed with a printed note.

### Where it can't prompt

Headless contexts (the scheduler, `evi web` background runs, federation
`/api/federate`, and workflow steps) attach a deny-only permission callback:
anything not pre-approved via `auto_approve`/`rules`/`mode`/trusted scopes is
**denied**, never blocked waiting on a human.

## Usage

### REPL slash commands (`evi chat`)

| Command | Effect |
|---------|--------|
| `/plan` | The **next** turn runs plan-only (no tools). Type your task after it, or pass it inline: `/plan outline a refactor`. |
| `/auto on` | Approve every tool call for the rest of this session. |
| `/auto off` | Return to config defaults (`mode` + `auto_approve` + `rules`). |
| `/auto` | Show whether auto-all is ON/OFF and list the always-allowed categories. |
| `/tools` | List the currently active tools (so you can see what's callable). |
| `/reload` | Re-read `config.toml` (pick up edits to `[auto]`/`[tools]`) without restarting. |

When a call needs a decision, the CLI prints a prompt like:

```
permission: write_file (fs) args={"path": "/etc/hosts", ...}
  approve? y/n/a (allow all this session):
```

Press `y` to allow once, `n` (or just Enter) to deny, `a` to flip on allow-all
for the session. When the model batches several calls in one turn, eVi prompts
once for the whole batch and you can answer `a` (all), `n` (none), specific
1-based indices like `1,3`, or `s` (allow all this session).

### Setting the policy

There is no dedicated `evi config set` for these keys — edit `config.toml`
directly. `evi config show` prints the resolved config (including any profile or
per-project overlay), and `evi config path` prints the file location. After
editing, use `/reload` in the REPL or restart.

### Web UI

The web frontend prompts for each non-pre-approved tool call inline in the chat
stream (approve/deny per call), honoring the same `[auto]` policy. The
diagnostics endpoint reports the live sandbox state — `{enabled, platform,
launcher, available}` — which the desktop/web settings surface so you can see
whether a sandboxer is actually present on this machine.

### Per-project and per-profile overrides

Because `[auto]` and `[tools]` are ordinary config sections, a profile
(`~/.evi/profiles/<name>.toml`, via `--profile`/`EVI_PROFILE`) or a per-project
`.evi.toml` (walked up from the working directory) can override them. A repo can
thus pin a stricter `mode` or a `deny` rule for everyone who works in it.

## Examples

### Example 1 — Lock down shell, trust a project, trust a docs domain

A policy that: keeps the safe defaults, allows the `web` and `git` categories,
hard-blocks destructive shell commands, and auto-approves file edits anywhere
under one project plus web fetches to a docs site.

```toml
# ~/.evi/config.toml

[tools]
fs    = true
code  = true
shell = true      # enable shell tools, but gate them with rules below
web   = true
git   = true

[auto]
mode         = "ask"
auto_approve = ["fs", "code", "memory", "skills", "image", "git"]
rules = [
  "deny shell rm*",          # block any shell call whose arg starts with "rm"
  "deny shell *sudo*",       # ...or mentions sudo
  "deny fs *.env",           # never touch dotenv files, even under trusted_dirs
  "allow web",               # allow the whole web category without prompting
]
trusted_dirs    = ["~/projects/eVi"]
trusted_domains = ["docs.python.org"]
```

Because rules are first-match and sit **above** trusted scopes, the
`deny fs *.env` rule still blocks a `write_file` to `~/projects/eVi/.env` even
though that path is under a trusted dir.

### Example 2 — Run model-generated Python in a sandbox (Linux)

Turn on the sandbox and confirm a snippet really is confined:

```toml
# ~/.evi/config.toml
[tools]
code    = true
sandbox = true
```

```bash
# Make sure the Linux sandboxer is installed
sudo apt install bubblewrap     # provides `bwrap`
which bwrap                      # -> /usr/bin/bwrap

evi chat
```

In the REPL, ask the model to run code that tries to write outside its work dir
and to reach the network — both should fail under the sandbox:

```text
you> run this python: open('/etc/evi-probe','w').write('x'); import urllib.request as u; u.urlopen('https://example.com')
```

With `bwrap` present, the filesystem is read-only outside the temp work dir and
`--unshare-net` removes networking, so the write and the fetch both error out.
On Windows (or any machine with no sandboxer on `PATH`), the same call runs
**unsandboxed** and `run_python` prefixes its output with:

```text
(sandbox requested but no sandboxer on PATH — ran unsandboxed)
```

### Example 3 — Read-only planning for one turn

Use `/plan` to let the model reason about a change without it touching anything:

```text
you> /plan
plan-only mode enabled for the next turn. Type your task.
you> review my repo layout and propose where a new auth module should live
```

That single turn runs with all tools denied (`mode = "plan"` semantics for the
turn), so the model produces a plan rather than editing files. The following
turn returns to your normal policy.

## Notes / limits

- **Fail-open sandbox.** If `sandbox = true` but no sandboxer is on `PATH`
  (always the case on Windows), `run_python` does **not** refuse — it runs
  unsandboxed and tells you. Treat the sandbox as best-effort hardening, not a
  guarantee, and don't rely on it existing on Windows.
- **`run_python` is not otherwise a sandbox.** Even sandboxed, it's an OS-level
  confinement (read-only FS + no net), not a full container. The source itself
  notes it's "acceptable for personal-assistant use on a trusted machine."
- **Explicit `deny` always wins among the trust layers.** A `deny` rule beats
  `trusted_dirs`/`trusted_domains`. But note `yolo` mode short-circuits *before*
  rules are evaluated — in `yolo`, even a `deny` rule is ignored. Don't expect a
  deny-list to protect you while in `yolo`.
- **`/auto on` overrides everything.** It forces allow for the whole session,
  ignoring mode, rules, and trusted scopes. It resets when you `/auto off` or
  exit; it is not persisted to config.
- **Categories, not individual tools, are auto-approved.** `auto_approve` works
  at the category granularity (`fs`, `code`, `web`, …). To approve or block a
  single tool, use a `rules` entry with the tool name. `shell`, `subagent`,
  `computer`, and `web` are intentionally left out of the defaults because they
  can act broadly or reach the network.
- **Trusted-dir matching is path-resolved.** Arguments are `~`-expanded and
  resolved to absolute paths before the sub-path check, so symlink/relative
  tricks resolve to their real target. A non-string argument can't be matched by
  an arg-glob; rules only see string-valued args.
- **Headless = strict.** In any context that can't prompt (scheduler, web
  background runs, federation, workflow steps), unattended agents deny anything
  not pre-approved — so make sure scheduled tasks rely only on `auto_approve` /
  `rules` / trusted scopes, not on interactive approval.
- **`trusted_domains` is host-based.** It matches the URL host exactly or as a
  subdomain (`docs.python.org` also covers `x.docs.python.org`), not by path or
  scheme.

### Relevant source files

- `C:\evi\evi\permissions.py` — the `decide()` policy and rule/trust matching.
- `C:\evi\evi\sandbox.py` — `wrap()` / `available()` / `status()` per-OS.
- `C:\evi\evi\config.py` — `AutoSettings` (`[auto]`) and `ToolToggles` (`[tools]`).
- `C:\evi\evi\tools\code.py` — where `run_python` consults `tools.sandbox`.
- `C:\evi\evi\llm\agent.py` — wires `decide()`, `/auto`, and `/plan` into the run loop.
- `C:\evi\evi\apps\cli\main.py` — REPL slash commands and the CLI permission prompts.
