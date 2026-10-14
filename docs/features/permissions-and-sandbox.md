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

0. **Hard-deny** (`auto.hard_deny`). Evaluated **before everything — even
   `yolo` and any `allow` rule.** A list of `[deny] <tool-glob> [arg-glob]`
   strings; if one matches, the call is **denied**, full stop. Use for calls
   that must never run regardless of mode (e.g.
   `hard_deny = ["run_command rm -rf*"]`). For a shell command line it is also
   checked against each command in the line, so `ls && rm -rf x` is denied too
   (see [Shell command lines](#shell-command-lines)).
1. **Mode** (`auto.mode`). Checked next and can short-circuit the rest:
   - `yolo` → **allow** every call (but hard-deny above still blocks).
   - `plan` → **deny** every call (read-only planning; the model can think but
     not act).
   - `accept_edits` and `ask` fall through to the lower layers.
2. **Rules** (`auto.rules`). A **first-match** allow/deny list. Each rule is a
   string `<allow|deny> <tool-glob> [arg-glob]`. The tool glob is matched
   against the tool name (`fnmatch`), not its `[tools]` category; the optional
   arg glob is matched against the call's string-valued arguments. The first
   rule that matches wins — **so an explicit `deny` here beats trusted
   dirs/domains below.** Calls that run a shell command line (`run_command`,
   `monitor` with `kind=command`) are matched command by command instead —
   see [Shell command lines](#shell-command-lines).
   - **Protected paths** (`auto.protected_paths`). Before any *implicit* allow
     below, an `fs`/`code` write whose path matches a protected pattern
     (fnmatch on the path or its basename) is forced to **ask** — so
     `accept_edits` / auto-approve / trusted-dirs never silently write a
     secret or code-executing file. Defaults cover `.env`, `.npmrc`,
     `.gitconfig`, `.pypirc`, shell rc files, `*.pem`, SSH keys. An explicit
     `allow` rule (step 2) still wins, honouring your intent.
3. **`accept_edits` mode shortcut.** If the mode is `accept_edits` and the
   tool's category is `fs` or `code`, the call is **allowed**.
4. **Auto-approve categories** (`auto.auto_approve`). If the tool's category is
   in this list, it's **allowed** without prompting.
5. **Trusted scopes.** For `fs`/`code` tools whose path arguments resolve to
   somewhere under a `trusted_dirs` entry → **allow**. For `web` tools whose URL
   host matches a `trusted_domains` entry (exact host or any subdomain) →
   **allow**. A `trusted_domains` entry also lets `web_fetch` reach that host on
   a loopback or LAN address (see [What `web_fetch` will
   fetch](#what-web_fetch-will-fetch)).
6. **Otherwise → ask.** eVi prompts you (in the CLI/REPL or web UI). If there is
   no UI able to prompt — e.g. the headless scheduler, a federation request, or
   a workflow step — an unattended agent **default-denies** anything not
   pre-approved.

The REPL `/auto on` toggle is a session override that sits *above* all of this:
it forces every call to **allow** for the rest of the session, until you
`/auto off`.

### Shell command lines

`run_command`, `monitor` with `kind=command` and any other `shell`-category tool
run a whole command line, which can chain several commands. eVi splits the line
the way the shell that runs it would (`cmd.exe` on Windows, `/bin/sh`
elsewhere; `evi/shellsplit.py`): `;`, `&`, `&&`, `||`, `|` and newlines
separate commands, and `$( )`, backticks and `( )` subshells are commands of
their own. Rules then apply to each command (segment), not to the line as a
whole:

- **An allow rule with an arg-glob covers a line only if all of this holds:**
  every command's first matching rule is an `allow` (different rules may cover
  different commands), the line can be split, and every redirect (`>`, `>>`,
  `2>`, `<`) points inside the working folder or at a null device (`/dev/null`,
  `nul`, `$null`). So `allow run_command git*` approves
  `git status && git log`, but `git log; python x.py` and `git log; curl x | sh`
  ask, and so does `git log > /elsewhere/out.txt`. Add `allow run_command
  python*` and `git log; python x.py` is approved too. A redirect target is not
  part of the text the glob sees: `allow run_command git log` still covers
  `git log > log.txt`.
- **A line eVi can't split is never approved by a specific allow rule** (it
  asks; only a broad allow, below, covers it): unbalanced quotes or brackets; an inline script (`bash -c "…"`, `sh -c`, `eval`,
  `iex` / `Invoke-Expression`, `cmd /c`, `powershell -Command` or
  `-EncodedCommand`); a shell reading its script from a pipe or stdin
  (`curl x | sh`); nesting deeper than 32 levels; a line over 100,000
  characters. On Windows, `cmd.exe` expands and re-parses some things before
  it splits the line, so these ask too: any `%VAR%` expansion (even
  `echo %PATH%`), any `for /f` loop, a `^&` or `^|` in a line that also has a
  `|` pipe, and a `^` before a bare carriage return.
- **Some ordinary lines now ask under a specific allow rule**, because their
  parts become separate commands that the rule doesn't name: an environment
  prefix (`FOO=1 git log` is not `git*`), `if … then`, `for … do`, `{ … }` and
  `[[ a && b ]]`. `python -c` / `node -e` are not refused, but the command text
  (`python -c "…"`) must itself match a rule.
- **Relative redirect targets** resolve against `run_command`'s `cwd` argument
  (default: the working folder). `monitor` starts its command in the eVi
  process's own folder, not the session's, so its relative redirects resolve
  there; in a web session whose working folder is elsewhere they count as
  outside and the call asks. A variable in a target, or a `cd` anywhere in the
  line, also counts as outside.
- **An allow rule's arg-glob is matched against the commands only**, never the
  other arguments: `allow run_command C:\proj*` no longer approves every
  command run with `cwd` in that folder. A `deny` rule still matches any string
  argument, `cwd` included.
- **`deny` rules and `hard_deny` match any command in the line**:
  `deny run_command rm*` catches `ls && rm x`.
- **A broad allow** (`allow run_command`, `allow run_command *`) still covers
  any line, including one that can't be split and redirects anywhere — it is
  the same as auto-approving the tool.

The destructive-command guard uses the same split. A specific allow rule
(matching the tool by name or category, with a real arg-glob) clears a guard
hit only when it covers every command in the line, the line can be split and
its redirects stay inside the working folder; a broad allow never clears it.
`destructive_allow` globs are matched per command too: a hit is exempt only if
every command it touches matches a glob and it touches no redirect, and never
on a line that can't be split. See
[configuration.md](../configuration.md#primary-file--configtoml) for the guard's
settings.

### What `web_fetch` will fetch

The model picks `web_fetch`'s URL, and the page can redirect anywhere, so
`web_fetch` (`evi/netguard.py`) refuses addresses that aren't on the public
internet: unspecified, loopback, private (RFC 1918) and carrier-grade NAT
ranges, link-local, unique-local and site-local IPv6, multicast and reserved
ranges, cloud metadata endpoints (`169.254.169.254`, `fd00:ec2::254`,
`100.100.100.200`, `168.63.129.16`) and the IPv6 forms that embed any of these
(IPv4-mapped, IPv4-compatible, NAT64, 6to4). Every address a host resolves to
is checked, and the connection goes to the checked address, so a second DNS
lookup can't swap it.

- **To fetch a LAN or loopback host** (Home Assistant, a SearXNG page, a local
  dev server), add its name or IP to `[auto] trusted_domains`. A name covers
  its subdomains; an IP matches only that address; entries take no port.
  Trusting `localhost` or `127.0.0.1` lets the model fetch any port on this
  machine except eVi's own.
- **eVi's API port (8473) on a loopback address is never fetched**, even for a
  trusted host. Only the default port is known: if you run eVi's server on
  another port, trusting `localhost` exposes that port.
- **Redirects** are followed by hand, at most 5, and each hop is checked again.
  A hop your rules deny is never followed. A hop to the same host (ignoring a
  leading `www.`), or to one the policy allows, is followed. A hop to another
  host that would need approval is followed only if the first URL needed
  approval as well; if the first URL was let through without a prompt by
  `trusted_domains` or an allow rule, the redirect is refused and the model is
  told to call `web_fetch` on the new URL itself. A redirect from a public
  address is never followed to a non-public one, trusted host or not.
- **Limits:** the body is capped at 1 MB while it streams (a compressed body
  can't inflate past that, and at most 4 MB of raw data is read), each network
  step has a 30 s timeout, and the whole fetch has a 60 s deadline.
- **Proxies:** `HTTP(S)_PROXY` / `ALL_PROXY` are not used, because the check
  has to see the address eVi connects to. On a network where only a proxy
  reaches the internet, `web_fetch` fails; the error says a proxy was ignored.

A refusal comes back to the model as `ERROR: blocked: <reason>`. There is no
prompt for a private address yet: an untrusted host on one is refused in every
mode, `yolo` and `/auto on` included.

### Repositories you didn't write

eVi runs git itself for the git tools, `evi review`, worktrees, the status
line, the project anatomy map, `evi sync` and plugin / skill installs
(`evi/gitsafe.py`). A repository's own `.git/config` can name programs for git
to run, so these calls switch that off (`evi sync` works on eVi's own `~/.evi`
repository, so it keeps its hooks and filters):

- no hook files run (`core.hooksPath` points at an empty location), so
  `evi worktree create` doesn't run the repository's post-checkout hook — or
  your global one;
- `core.fsmonitor`, external diff and textconv drivers, the pager and
  signature checking (`gpg.program`) are off;
- filter drivers (`filter.<name>.clean/smudge/process`) and config-defined
  hooks (`hook.<name>.command`) from the repository's own config, or a
  submodule's, are switched off. Ones from your global or system config, such
  as Git LFS, still run. A filter set up only in a repo's `.git/config`
  (git-crypt, `nbstripout --install`, `git lfs install --local`) does not, so
  such files can show as modified; define it globally to keep it working;
- Git LFS can't run what the repository names (its transfer agents,
  `core.sshCommand`, a credential helper), and checkouts skip LFS downloads:
  after `evi worktree create` in an LFS repo, run `git lfs pull` there;
- inherited `GIT_DIR`, `GIT_INDEX_FILE`, `GIT_EXTERNAL_DIFF`, `GIT_PAGER` and
  similar variables are dropped (`evi review`, run from your own shell, keeps
  the ones that locate the repository);
- git runs from an absolute `PATH` entry, never a `git.exe` in the current
  folder, and with `GIT_TERMINAL_PROMPT=0`: a plugin or skill install from a
  private HTTPS repo needs a credential helper (`evi sync` still prompts).

The git tools also refuse refs that start with `-` (`git_diff` still takes
`--staged` / `--cached`), `git_diff` refuses paths outside the repository, and
`git_status`, `git_diff`, `git_blame` and `git_info` refuse a directory that is
not inside the repository's own work tree: a bare repository, a `.git` folder,
or a `core.worktree` that points somewhere else (an ancestor such as your home
folder included). This holds for `evi mcp serve` clients too.

The destructive-command guard also stops a shell line from silently setting
`GIT_*` variables (`GIT_EXTERNAL_DIFF=`, `GIT_CONFIG_COUNT=`, `GIT_DIR=`, …,
apart from identity and date variables, the prompt, lock and trace switches,
`GIT_EDITOR=true` and `GIT_PAGER=cat`) or `LD_PRELOAD` / `LD_LIBRARY_PATH` /
`DYLD_*`, in bash, cmd or PowerShell form: it asks, or denies when nothing can
ask.

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
| `rules` | `[]` | First-match allow/deny list, matched on the tool **name**; explicit `deny` overrides trusted dirs/domains. Shell lines are matched command by command. |
| `trusted_dirs` | `[]` | Paths whose `fs`/`code` calls are auto-approved (resolved, `~` expanded, sub-paths included). |
| `trusted_domains` | `[]` | Hosts whose `web` fetches are auto-approved (exact host or subdomain). Also the only way to let `web_fetch` reach a loopback or LAN address. |

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
(`~/.evi/profiles/<name>.toml`, via `--profile`/`EVI_PROFILE`) can override them
freely — it is your own file.

A per-project `.evi.toml` (the nearest one walking up from the working
directory) is different: it arrives with whatever repository you cloned, so
since 1.0.53 eVi treats it as **untrusted** and it can only **tighten** these
sections, judged against your config + active profile:

| Key | What a `.evi.toml` can do |
|---|---|
| `[tools] <category> = false` | Switch a tool category off. `= true` is ignored. |
| `[tools] sandbox = true` | Turn the `run_python` / `run_command` OS sandbox on. `= false` is ignored. |
| `[auto] mode` | Applies only if **stricter** than yours (`yolo` < `accept_edits` < `ask` < `plan`). |
| `[auto] rules` | Only `deny …` rules apply, and they are evaluated **before** your rules (so your broad `allow` can't shadow them). `allow` rules are dropped. Like all `rules`, they are **skipped in `yolo`** mode. |
| `[auto] hard_deny`, `protected_paths` | **Added** to your lists — never replace or shrink them. `hard_deny` applies in **every** mode, `yolo` included. |
| `[auto] block_destructive = true` | Honoured. `= false` is ignored. |

Everything else that loosens is ignored: `auto_approve`, `trusted_dirs`,
`trusted_domains`, `destructive_allow`, `destructive_disable_rules`, any other
`[tools]` key (`transcripts`, `mcp_allow`, `search_backend`, `searxng_url`,
`cleanup_period_days`, …). So a repo can pin a stricter `mode`, a `deny` rule or
a `hard_deny` entry for everyone who works in it, but it can never grant itself
more. A `deny` rule alone does nothing for someone on `yolo`: for a block that
must hold for everyone, use `hard_deny` (or also set `mode`). Rule and
`hard_deny` entries match the tool **name** (`run_command`, `write_file`, …),
not its `[tools]` category, e.g. `hard_deny = ["run_command rm -rf*"]`. Outside
`[tools]`/`[auto]` a `.evi.toml` may only set model/sampling preferences — see
[Per-project config](../configuration.md#per-project-config--evitoml) for the
full list. Ignored keys are named once per process in a warning (e.g. `eVi:
ignoring these settings from project config <path>: auto.mode, …`); a file that
can't be parsed is ignored as a whole, so it can never switch your own settings
off. `--safe-mode` skips the file entirely.

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
  "deny run_command rm*",    # block any command in the line that starts with "rm"
  "deny run_command *sudo*", # ...or mentions sudo
  "deny *_file *.env",       # never read/write/edit dotenv files, even under trusted_dirs
  "allow web_*",             # allow web_search / web_fetch without prompting
]
trusted_dirs    = ["~/projects/eVi"]
trusted_domains = ["docs.python.org"]
```

Because rules are first-match and sit **above** trusted scopes, the
`deny *_file *.env` rule still blocks a `write_file` to `~/projects/eVi/.env` even
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
  scheme. For `web_fetch` an entry also unlocks that host's loopback / LAN
  addresses (see [What `web_fetch` will fetch](#what-web_fetch-will-fetch)), so
  list only hosts you mean the model to reach.

### Relevant source files

- `C:\evi\evi\permissions.py` — the `decide()` policy and rule/trust matching.
- `C:\evi\evi\shellsplit.py` — splits a shell line into the commands rules are
  matched against.
- `C:\evi\evi\shell_guard.py` — the destructive-command guard.
- `C:\evi\evi\netguard.py` — `web_fetch`'s address, redirect and size checks.
- `C:\evi\evi\gitsafe.py` — the hardened git calls.
- `C:\evi\evi\sandbox.py` — `wrap()` / `available()` / `status()` per-OS.
- `C:\evi\evi\config.py` — `AutoSettings` (`[auto]`) and `ToolToggles` (`[tools]`).
- `C:\evi\evi\tools\code.py` — where `run_python` consults `tools.sandbox`.
- `C:\evi\evi\llm\agent.py` — wires `decide()`, `/auto`, and `/plan` into the run loop.
- `C:\evi\evi\apps\cli\main.py` — REPL slash commands and the CLI permission prompts.
