# Permissions & Sandbox

## Overview

eVi runs language models locally and lets them call **tools** — read and write
files, run Python, search the web, control the mouse, talk to MCP servers, and
more. Permissions decide *which* of those calls happen automatically, which get
blocked, and which pause to ask you first. The **sandbox** is a separate,
defence-in-depth layer that confines the tools that run code (`run_python`,
and the shell lines `run_command` and `monitor` run) to a read-only filesystem
with no network.

You'd use these features to:

- Let trusted, low-risk tools (reading files, reading code, recalling memory)
  run silently while risky ones (running code, shell, deleting things, network
  calls) still prompt.
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
   `hard_deny = ["shell rm -rf*"]`). The tool-glob matches the tool name or its
   category, as in `rules` below. For a shell command line it is also checked
   against each command in the line, so `ls && rm -rf x` is denied too (see
   [Shell command lines](#shell-command-lines)).
1. **Protected paths** (`auto.protected_paths`, plus eVi's built-in
   write-protected paths). Right after hard-deny and before the mode: a call
   that would **write** a protected path **asks**, in every mode — `yolo` and
   `/auto on` included — and is **denied** when nothing can ask. Only `plan`
   mode or a matching `deny` rule changes that, to deny; an `allow` rule does
   not clear it. See [Protected paths](#protected-paths).
2. **Mode** (`auto.mode`). Checked next and can short-circuit the rest:
   - `yolo` → **allow** every call (hard-deny, protected paths and the
     destructive-command guard still apply).
   - `plan` → **deny** every call (read-only planning; the model can think but
     not act).
   - `accept_edits` and `ask` fall through to the lower layers.
3. **Rules** (`auto.rules`). A **first-match** allow/deny list. Each rule is a
   string `<allow|deny> <tool-glob> [arg-glob]`. The tool glob is matched
   (`fnmatch`) against the tool name, its `[tools]` category **or** the call's
   own category ([What a call counts as](#what-a-call-counts-as)), so
   `deny shell rm*`, `deny fs *.env`, `allow web` and `deny code_read *.env`
   work as well as `deny run_command rm*`; `shell` also matches any call that
   runs a shell line (`monitor` with `kind=command` too). An **allow** rule
   never approves a shell line by its tool's category (`allow code` doesn't
   cover `monitor` with `kind=command`; `allow monitor` or `allow shell` does),
   nor running code by `code_read`. The optional arg glob is matched against
   the call's string-valued arguments. The first rule that matches wins — **so
   an explicit `deny` here beats trusted dirs/domains below.** Calls that run a
   shell command line (`run_command`, `monitor` with `kind=command`) are matched
   command by command instead — see [Shell command
   lines](#shell-command-lines).
4. **Secret reads.** An `fs`, `code` or `code_read` call whose path argument
   matches a protected pattern other than the built-in write-protected ones
   (`.env`, `*.pem`, `id_rsa`, …) **asks**, even with `fs` auto-approved, so a
   secret is never pulled into the model's context silently. That includes the
   file `monitor` tails. `yolo` or an explicit allow rule above (e.g.
   `allow read_file *.env.example`) lets it through. In an
   [unattended run](#unattended-runs) only the allow rule does: there the read
   is denied even under `yolo`, `/auto on` or `evi run -y`.
5. **Code execution in a remote-triggered run.** In a run a remote trigger
   drives (a federated or A2A task, a routine webhook without `yes`, a channel
   push with `run: true`), a call that runs code goes no further: call category
   `code` or `shell`, or any `run_python` call, sandboxed or not. It **asks**,
   and the run's `deny` policy refuses it. Only an explicit allow rule (step 3)
   lets one through. See [Unattended runs](#unattended-runs).
6. **`accept_edits` mode shortcut.** If the mode is `accept_edits` and the
   call's category is `fs` or `code_read` (file edits and reading code, never
   running code), the call is **allowed**.
7. **Auto-approve categories** (`auto.auto_approve`). If the call's category is
   in this list, it's **allowed** without prompting. `code` in the list also
   covers `code_read`.
8. **Trusted scopes.** An `fs` or `code_read` call is **allowed** when every
   one of its path arguments resolves (against the working folder, on its real
   path) under a `trusted_dirs` entry. Only path arguments count — `path`,
   `paths`, `file_path`, `cwd`, `dir`, the file `monitor` tails, and for an MCP
   tool the arguments named like paths — never a file's content, and a call
   that runs code (`run_python`, `check_file` on a `.js` file) is never
   approved this way. A `find_files` / `search_files` glob that is
   absolute or climbs out with `..` isn't either. For `web` tools whose URL host
   matches a `trusted_domains` entry (exact host or any subdomain) → **allow**.
   A `trusted_domains` entry also lets `web_fetch` reach that host on a loopback
   or LAN address (see [What `web_fetch` will
   fetch](#what-web_fetch-will-fetch)).
9. **Otherwise → ask.** eVi prompts you (in the CLI/REPL or web UI). If there is
   no UI able to prompt — a scheduled task, `evi run`, a workflow step, a
   federation request, an SDK agent built without a `permission_callback` — the
   call follows the unattended policy and is **denied** unless you opted in
   (see [Unattended runs](#unattended-runs)).

The REPL `/auto on` toggle (and `s` at a CLI prompt) is a session override: it
acts as `yolo` for the rest of the session, until you `/auto off`. Hard-deny,
protected paths and the destructive-command guard still apply under it.

### What a call counts as

Every tool sits in a `[tools]` category, the toggle that switches it on:
`run_python`, `python_symbols`, `check_file` and `monitor` are all in `code`.
The policy checks each call under what that call does, its **call category**:

| Call | Call category |
|---|---|
| Any call that runs a shell line: `run_command`, `monitor` with `kind=command` | `shell` |
| `python_symbols`; `monitor` tailing a file (`kind=file`); `check_file` on a `.py` file (ruff, pyflakes) or on a file type eVi has no linter for | `code_read` |
| `run_python` while the OS sandbox is active (`[tools] sandbox = true` and `bwrap` or `sandbox-exec` on `PATH`) | `code_read` |
| `run_python` otherwise; `check_file` on a `.js`, `.jsx`, `.ts`, `.tsx`, `.go` or `.rs` file | `code` |
| Any other call | its tool's category |

`check_file` on JavaScript, TypeScript, Go or Rust counts as running code
because its linter runs the project's own code: eslint loads
`eslint.config.js`, `cargo clippy` builds the crate (`build.rs`, proc macros)
and `go vet` may run cgo or fetch a toolchain. The file's extension decides,
not which linter happens to be installed.

`auto_approve`, `accept_edits` and trusted dirs go by the call category, so
the default list (`fs`, `code_read`, `memory`, `skills`, `image`) reads code
without asking and asks before running it. Add `code` to let running code
through unasked as well; `code` also covers `code_read`. The permission prompt
names the call category, e.g. `permission: monitor (shell) args=…`.

A sandboxed `run_python` can't write outside its temp folder or reach the
network, but it can still read every file you can, secrets included, and print
them into the chat. If that matters to you, take `code_read` out of
`auto_approve`: a sandboxed `run_python` then asks, and so do `python_symbols`,
`monitor` on a file and `check_file` on Python.

`monitor` with `kind=command` is held to everything `run_command` is. It is
checked as `shell`, so rules, `hard_deny` and the destructive-command guard
apply and it runs unasked only where `run_command` would (`shell` in
`auto_approve`, an `allow monitor …` / `allow shell …` rule, `yolo`). It
runs only in a session that offers `run_command` (`[tools] shell = true`, or
code mode, which turns the shell tools on), and elsewhere returns an error
without running anything. It runs under the OS sandbox when
`[tools] sandbox = true`.

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
  line, also counts as outside. A relative target counts as inside when it
  resolves there, which holds on a mapped drive or a `\\server\share` working
  folder too. A redirect target or `cwd` given as an absolute network path is
  never resolved (that would already connect to the host) and counts as
  outside.
- **An allow rule's arg-glob is matched against the commands only**, never the
  other arguments: `allow run_command C:\proj*` no longer approves every
  command run with `cwd` in that folder. A `deny` rule still matches any string
  argument, `cwd` included.
- **`deny` rules and `hard_deny` match any command in the line**:
  `deny run_command rm*` catches `ls && rm x`.
- **A broad allow** (`allow run_command`, `allow run_command *`) still covers
  any line, including one that can't be split and redirects anywhere — it is
  the same as auto-approving the tool.

The destructive-command guard uses the same split. Its rules come in two
severities. A **prompt**-severity hit (`git reset --hard`, `git stash drop`,
`find … -delete`, …) is cleared by a specific allow rule (naming the tool or
`shell`, never the toggle category `monitor` is in, with a real arg-glob) only
when the rule covers every command in the line, the line can be split and its
redirects stay inside the working folder; a broad allow never clears it. A
**block**-severity hit (`rm -rf ~`, a force-push, `curl … | sh`, a disk
format, …) is never cleared by an allow rule: only a `destructive_allow` glob
exempts one.
`destructive_allow` globs are matched per command too: a hit is exempt only if
every command it touches matches a glob and it touches no redirect, and never
on a line that can't be split. A glob whose first word (the command) holds a
wildcard (`*--force-with-lease*`, `git*`, `r*`) is ignored, because it doesn't
say which command it exempts; write the command out
(`git push --force-with-lease*`). See
[configuration.md](../configuration.md#primary-file--configtoml) for the guard's
settings.

### Protected paths

Protected paths keep a prompt-injected agent from silently rewriting the files
that steer it or run code later: its instructions, eVi's own config and hooks,
git hooks, CI workflows, and secrets. A call that would **write** one asks in
every mode (`yolo` and `/auto on` included) and is denied when nothing can
answer (the scheduler, a federation request, `evi run -y` in CI or a script
with no terminal). Plan mode or a matching `deny`
rule denies it; no `allow` rule clears it. The denial tells the model which
path was protected.

The patterns in force are:

- **`[auto] protected_paths`** — by default the secret and shell start-up
  files: `.env`, `*.env`, `.env.*`, `.npmrc`, `.gitconfig`, `.pypirc`,
  `.bashrc`, `.zshrc`, `.bash_profile`, `.profile`, `*.pem`, `id_rsa`,
  `id_ed25519`. Edit the list to taste. Reading one of these asks too (step 4
  above).
- **The built-in write-protected paths**, always added on top of your list:
  `.evi.toml`, `EVI.md`, `AGENTS.md`, `.evi/**`, `~/.evi/**` (and your
  `EVI_HOME` folder when it is set), `.git/hooks/**`, `.git/config`,
  `.git/modules/**/hooks/**`, `.git/modules/**/config`, `.github/workflows/**`
  and `.vscode/tasks.json`. Reading them is normal and never asks.
- **minus `[auto] protected_paths_remove`**, which un-protects an entry by its
  exact text: a built-in one, or one of your own `protected_paths` entries.
  For example, `protected_paths_remove = ["AGENTS.md"]` lets an unattended
  `evi run -y` rewrite AGENTS.md. Only `config.toml` or a profile can set it; a
  project's `.evi.toml` can't. `.evi/**` matches an `.evi` folder at any depth,
  so removing `~/.evi/**` alone does not un-protect your eVi home.

A pattern without a `/` matches a file's name; one with a `/` matches its path
(a relative one at any depth); `~` is your home folder. Matching ignores case,
and each path is checked as written, normalised and on its real path, so a
symlink or junction to a protected file is caught (on Windows, `EVI.md.`,
`EVI.md::$DATA`, `\\?\C:\…` and `\\localhost\C$\…` spellings count as the file
they open). Any tool counts as a writer except eVi's built-in read-only ones
(`read_file`, `list_dir`, `find_files`, `search_files`, the `git_*` readers, …)
and a call that only reads code (`monitor` tailing a file), so an MCP tool that
names `AGENTS.md` asks too.

For a shell line eVi inspects its `>` / `>>` targets (followed through `cd` /
`pushd` / `Set-Location`). It also inspects the files named by `tee`, `cp`,
`mv`, `install`, `ln`, `copy`, `move`, `xcopy`, `robocopy`, `ren`,
`Tee-Object`, `Out-File`, `Set-Content`, `Add-Content`, `New-Item`,
`Copy-Item`, `Move-Item` and `Rename-Item` (and their aliases), by `sed -i` /
`perl -i` and by `dd of=`, inline option values (`-FilePath:x`,
`--target-directory=x`) included. An argument of a copy, move or link command
may be the folder it writes into, so `cp pre-commit .git/hooks` counts as a
write to `.git/hooks/pre-commit`; copying *from* a protected file
(`cp EVI.md notes.md`) asks too. After a `cd` eVi can't follow, a relative
write target counts as protected: the call asks, and is denied when nothing can
ask, with the reason "after a cd eVi can't follow, so where it lands is
unknown". That covers a `cd` with an expansion such as `cd $DIR`, a wildcard, a
drive switch, or a `~name` that can't be expanded. Retry with an absolute path
or without the `cd`.

This is a guardrail, not a sandbox. Not inspected: command strings run through
`bash -c` / `sh -c` / `pwsh -Command`, `eval`, scripts, `run_python`,
`git config` and other git subcommands that write `.git/config`, a
`core.hooksPath` folder such as `.husky/`, the contents of a recursive copy
(`cp -r payload/. .`), hard links, and paths nested inside an MCP tool's
arguments (`{"edits": [{"path": …}]}`).

### Unattended runs

A run is **unattended** when nothing can show you a prompt: a scheduled task,
`evi run` and `evi batch`, `evi team run`, `evi dream`, an ultracode stage, a
workflow or recipe the web app runs, a subagent whose parent has no permission
UI, an agent built with the SDK without a `permission_callback`, and the
remote-triggered runs below. Pre-approved calls still run in it: the
`auto_approve` categories, `allow` rules, trusted dirs and domains. A call that
would **ask** follows `[auto] unattended_policy`:

- **`"deny"`** (the default, and what any value other than `"allow"` means):
  the call is not run, and the model is told why: `PERMISSION DENIED:
  <tool>(<args>) was not run. This run is unattended (nobody can approve a tool
  call), so only auto-approved tool categories run ([auto] auto_approve; [auto]
  unattended_policy = "deny").`
- **`"allow"`**: the call runs. This is the escape hatch for your own jobs and
  scripts, and it opens every tool such a run has. To let one kind of call
  through, add its category to `auto_approve` or write an `allow` rule
  instead.

Whatever the policy, and under `-y`, `/auto on` or `yolo` too, an unattended
run never writes a protected path, never reads a protected secret (`.env`, a
key) that no explicit `allow` rule covers, and never runs a command the
destructive-command guard flags. A repository's `.evi.toml` can't set
`unattended_policy`.

`[auto] mode` still applies to the unattended runs you start yourself: under
`yolo` every call runs (the checks just named aside), and under `accept_edits`
file edits and code reading run (never running code). The run's policy label
names this.

**Remote-triggered runs are pinned to `deny`.** A federated task
(`/api/federate`), an A2A task (`/a2a`), a routine webhook without `yes` and a
channel push with `run: true` (`POST /api/session/<id>/channel`) come from
outside, so nothing you turned on for yourself applies to them: not
`unattended_policy = "allow"`, not `[auto] mode` yolo or accept_edits, not
`/auto on` or a session's own approvals. Only `[auto] auto_approve`, your
`allow` rules and trusted dirs and domains let a call run, and a subagent such
a run starts gets the same limits. Running code is held back further: no
category, `accept_edits` or trusted dir approves `run_python` (sandboxed or
not), `run_command`, a `monitor` command or a `check_file` whose linter runs
project code there, even with `code` or `shell` in `auto_approve`. Only an
explicit allow rule (`allow run_python`, `allow shell npm test*`) lets one
run. The Telegram channel has a policy of its own and never uses `[auto]`; see
[channels.md](channels.md#the-channels-policy).

**`deny` only covers calls that would ask.** The default `auto_approve` list
(`fs`, `code_read`, `memory`, `skills`, `image`) runs in every unattended run,
remote-triggered ones included: file writes and reading code, but no code
execution. A list that adds `code` or `shell` (or one saved before
`code_read` existed, which holds `code`) runs that code unasked in the
unattended runs you start yourself, such as scheduled tasks, `evi run` and
`evi team run`, but never in a remote-triggered one. If your scheduled jobs,
peers or webhooks shouldn't write files, take `fs` out of `auto_approve`,
which makes your own sessions ask for it too, or block specific calls with
`deny` rules or `hard_deny`.

**Each run names its policy**: `deny`, `allow`, `allow-all` (`-y`, a routine's
`yes`) or `interactive`, plus the mode when it lets calls through
(`allow-all (mode=yolo)`, `deny (mode=accept_edits: file edits run)`). It is
the `# policy:` line of a scheduled task's log, `"policy"` in `evi run --format
json` and `evi batch` output and in the web app's workflow and recipe
responses, the `policy=` note `evi scheduler` and `evi team run` print, and the
`policy:` line of a dream report. A scheduled run in which a call was denied
records the status `denied: <tools> (policy <policy>)`, which `evi schedule
list` shows.

**Subagents** ask through the agent that started them. In a chat you are
watching, a subagent's prompt is that chat's prompt, one at a time however many
subagents run at once, and your `/auto on` (or an `s` answer) covers it. A
subagent with no parent UI (`evi team run`, `evi dream`, one a scheduled task
starts) runs unattended.

**Delegate CLI-agent backends are refused.** `codex`, `gemini`, `amp`, `qwen`
and `copilot` run their own tools outside these checks, so an unattended run
won't use one, as `[llm] backend` or as any `[models] *_backend`. The
scheduler records the refusal as the task's status, `evi run`, `evi batch` and
`evi team run` exit with it, the web app's runs answer `409`, and an A2A task
fails. Set `[llm] allow_cli_agent_unattended = true` to accept the risk; the
Telegram channel refuses them even then.

Still allow-all, as before: eval cases (`evi eval run`, the web eval runner
and scheduled eval suites, whose log says `# policy: allow-all`), so scores
measure what the agent can do, and `evi workflow run`, which approves every
tool of every step. The web app's workflow runs follow the policy.

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

With `[tools] sandbox = true`, the sandbox (`evi/sandbox.py`) wraps the
processes that run code so they run with the filesystem read-only except one
writable work directory, and with no network. The work directory is a
throwaway temp folder for `run_python`, the command's working folder for
`run_command`, and the eVi process's own folder for a `monitor` command.

- **Linux** — `bwrap` (bubblewrap): `--ro-bind / /` plus a writable bind for the
  work dir, plus `--unshare-net` to cut the network.
- **macOS** — `sandbox-exec` with a generated SBPL profile that denies
  `file-write*` outside `/tmp` and the work dir, and denies network.
- **Windows / no sandboxer on PATH** — **not available.** The wrapper returns
  the command unchanged, so it runs *unsandboxed*, with a note saying so in
  front of its output.

The sandbox confines only those three. It is not the same thing as the
permission policy — a call can be permitted and still be sandboxed, or permitted
and unsandboxed. The one link between them: while the sandbox is active (on,
and a sandboxer present), a `run_python` call counts as `code_read` rather
than `code`, so the default `auto_approve` list runs it without asking (see
[What a call counts as](#what-a-call-counts-as)). Without a sandboxer it stays
`code` and asks.

## Setup

All configuration lives in `~/.evi/config.toml` (Windows:
`%USERPROFILE%\.evi\config.toml`). First launch writes defaults; hand-edit the
file, then restart or run `/reload` in the REPL.

### `[auto]` — the permission policy

```toml
[auto]
mode            = "ask"                              # ask | accept_edits | plan | yolo
auto_approve    = ["fs", "code_read", "memory", "skills", "image"]
rules           = []                                 # first-match "allow|deny <tool-or-category> [arg]"
hard_deny       = []                                 # always deny, in every mode
protected_paths = [".env", "*.env", ".env.*", ".npmrc", ".gitconfig", ".pypirc",
                   ".bashrc", ".zshrc", ".bash_profile", ".profile",
                   "*.pem", "id_rsa", "id_ed25519"]
protected_paths_remove = []                          # protected entries to un-protect
trusted_dirs    = []                                 # auto-approve fs/code_read under these paths
trusted_domains = []                                 # auto-approve web fetches to these hosts
unattended_policy = "deny"                           # deny | allow: a call that would ask, with nobody to ask
```

| Key | Default | Meaning |
|-----|---------|---------|
| `mode` | `"ask"` | Top-level policy. `ask` prompts for anything not pre-approved; `accept_edits` auto-allows file edits and reading code (`fs`, `code_read`), never running code; `plan` denies all tools; `yolo` allows all tools (hard-deny, protected paths and the destructive-command guard still apply). |
| `auto_approve` | `["fs","code_read","memory","skills","image"]` | **Call categories** that run without prompting ([What a call counts as](#what-a-call-counts-as)). `code` (running code: `run_python`, linters that run project code) also covers `code_read`. Note `code`, `shell`, `subagent`, `computer`, `web` are deliberately **not** here. In a remote-triggered run no category approves running code. |
| `rules` | `[]` | First-match list of `allow` / `deny` `<tool-name-or-category-glob> [arg-glob]` entries, matched against the tool name, its category and the call's category; `shell` also matches `monitor` with `kind=command`. An allow rule never approves a shell line by its tool's category, nor running code by `code_read`. An explicit `deny` overrides trusted dirs/domains. Shell lines are matched command by command. |
| `hard_deny` | `[]` | `<tool-name-or-category-glob> [arg-glob]` entries denied before everything else, `yolo` and `/auto on` included. |
| `protected_paths` | the secret and shell start-up files above | Writing one asks in every mode; reading one asks even with `fs` auto-approved. The built-in write-protected paths always apply on top (see [Protected paths](#protected-paths)). |
| `protected_paths_remove` | `[]` | Protected entries to un-protect, by exact text: a built-in write-protected path or one of your `protected_paths`. Not settable from a `.evi.toml`. |
| `trusted_dirs` | `[]` | Paths whose `fs` and `code_read` calls are auto-approved when every path argument resolves inside one (`~` expanded, real path, sub-paths included). Never running code. |
| `trusted_domains` | `[]` | Hosts whose `web` fetches are auto-approved (exact host or subdomain). Also the only way to let `web_fetch` reach a loopback or LAN address. |
| `unattended_policy` | `"deny"` | What a call that would ask does in an [unattended run](#unattended-runs): `deny` refuses it, `allow` runs it; any other value means `deny`. Remote-triggered runs always deny. Not settable from a `.evi.toml`. |

Valid modes are exactly `ask`, `accept_edits`, `plan`, `yolo`.

**Lists saved before `code_read`.** The default list used to hold `code`, and
eVi writes the full defaults into `config.toml` on first run, so most existing
configs still list `code`, which now means running code without asking. eVi
leaves your list as it is. `evi lint`, `evi doctor` (its `code execution
approval` check) and Settings → Permissions point it out and offer the same
fix: your list with `code_read` in place of `code`, every other entry kept,
for example

```toml
[auto]
auto_approve = ["fs", "code_read", "memory", "skills", "image"]
```

In Settings it is a banner, **Running code is approved without asking**, whose
**Switch to code_read** button saves the list at once (it applies to new
chats). A list that names `code_read` beside `code` keeps running code unasked
on purpose, and nothing warns about it.

### Checking your policy

A rule or `hard_deny` entry that matches no tool does nothing, and nothing
used to say so. Two commands check the policy as written:

- **`evi lint`** also checks `config.toml`'s own `[auto]` section (it reads the
  file and writes nothing). It warns about an `auto_approve` list that still
  holds the old default's `code` (above), a `rules` entry that isn't
  `<allow|deny> <tool-glob> [arg-glob]`, and a `rules` or `hard_deny` entry
  whose tool-glob matches no tool name, tool or call category (`shell`,
  `code_read` and `mcp` included) or configured MCP server. It also warns
  about a `hard_deny` entry that starts with `allow`, a `destructive_allow`
  glob with a wildcard in its command word (the guard ignores it), a
  `destructive_disable_rules` id that names no guard rule, and a
  `protected_paths_remove` entry that removes nothing, is also listed in
  `protected_paths`, or removes `~/.evi/**` without `.evi/**`. An MCP rule
  such as `deny github.delete_*` is checked by its server part only; the tool
  part can't be checked without connecting.
- **`evi doctor`** has a `protected paths` check. It lists every protected
  pattern in force — `[auto] protected_paths`, plus the built-in
  write-protected ones, minus `protected_paths_remove` — and warns when a
  built-in one was removed. Its `code execution approval` check says whether
  `run_python` asks, and warns, with the fix, when the list in force still
  holds the old default's `code`.

### `[tools]` — the sandbox toggle (and tool categories)

Tools are grouped into categories that the permission policy reasons about.
The sandbox is one toggle here:

```toml
[tools]
fs       = true     # read_file / write_file / list_dir          (category: fs)
code     = true     # run_python, python_symbols, check_file, monitor (category: code)
shell    = false    # run_command (and monitor's commands); not auto-approved by default
web      = false    # web_search / web_fetch — network, opt in
computer = false    # mouse/keyboard control — never default-on
# ... other categories: memory, skills, image, subagent, mcp, voice,
#     transcripts, pdf, sqlite, index, git, federation, ocr, calendar ...

sandbox  = false    # run_python, run_command and monitor commands under an OS sandbox where available
```

These are toggle categories: the policy checks each call under its own call
category ([What a call counts as](#what-a-call-counts-as)), so the four `code`
tools are approved as `code_read`, `code` or `shell` depending on the call.

Set `sandbox = true` to run `run_python`, `run_command` and `monitor`'s
commands under `bwrap`/`sandbox-exec`. There are **no pip extras required**
for the sandbox itself, but the OS launcher must be present:

- Linux: install bubblewrap (e.g. `apt install bubblewrap`) so `bwrap` is on
  `PATH`.
- macOS: `sandbox-exec` ships with the OS.
- Windows: no sandboxer exists — `sandbox = true` is honored as "requested" but
  falls back to running unsandboxed with a printed note.

### Where it can't prompt

A run with no permission UI never blocks waiting on a human: anything not
pre-approved via `auto_approve` / `rules` / `mode` / trusted scopes follows
`[auto] unattended_policy`, **denied** by default. To let your own scheduled
jobs or scripts run calls that would ask, set:

```toml
[auto]
unattended_policy = "allow"   # the escape hatch; remote-triggered runs still deny
```

Which runs count, what still denies and how a run reports its policy:
[Unattended runs](#unattended-runs).

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
  approve? y/a (this call) / n / s (allow all this session):
```

Press `y` or `a` to allow this one call, or `n` (or just Enter) to deny it.
Press `s` to allow every tool call for the rest of the session, the same as
`/auto on`. `s` first asks `allow EVERY tool call for the rest of this session
(like /auto on)? y/N`; any answer but `y` turns nothing on and denies the call.
When the model batches several calls in one turn, eVi prompts once for the
whole batch: answer `a` (all of these calls), `n` (none), specific 1-based
indices like `1,3`, or `s` (allow all this session, after the same
confirmation). Even under `s` or `/auto on`, `hard_deny`, protected paths and
the destructive-command guard still apply.

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

**Settings → Permissions** lists the auto-approve categories as checkboxes,
with `code_read` just before `code`. `code_read`, `code` and `shell` each carry
a line saying what they let run unasked. When the saved list still holds the
old default's `code`, the banner described in
[Lists saved before `code_read`](#auto--the-permission-policy) appears above
them with its one-click switch. `GET /api/config` (and the reply to
`POST /api/config`) carries the switch as `auto_approve_migration`: the
migrated list, or `null` when there is nothing to change. It isn't a setting,
so posting it back changes nothing.

### Per-project and per-profile overrides

Because `[auto]` and `[tools]` are ordinary config sections, a profile
(`~/.evi/profiles/<name>.toml`, via `--profile`/`EVI_PROFILE`) can override them
freely — it is your own file.

A per-project `.evi.toml` (the nearest one walking up from the working
directory, stopping at the repository's top level) is different: it arrives
with whatever repository you cloned, so eVi treats it as **untrusted**. It
applies only in a folder you have trusted — `evi chat` asks the first time,
`evi trust [PATH]` trusts one up front — and only the version you trusted:
edit it and it is off again until you re-trust. Trust is per repository (its
worktrees share it); outside git it covers exactly the folder you trusted, not
its subfolders. `evi trust` asks you to confirm (`--yes` in scripts) and
refuses inside an `evi chat` session, so the agent there can't trust a folder
for you.
`evi run`, the web UI and the scheduler never ask; in an untrusted folder they
run with the project config off. See
[Trusting a folder](../configuration.md#trusting-a-folder).

Even in a trusted folder (since 1.0.53) it can only **tighten** these
sections, judged against your config + active profile:

| Key | What a `.evi.toml` can do |
|---|---|
| `[tools] <category> = false` | Switch a tool category off. `= true` is ignored. |
| `[tools] sandbox = true` | Turn the OS sandbox on (`run_python`, `run_command`, `monitor` commands). `= false` is ignored. |
| `[auto] mode` | Applies only if **stricter** than yours (`yolo` < `accept_edits` < `ask` < `plan`). |
| `[auto] rules` | Only `deny …` rules apply, and they are evaluated **before** your rules (so your broad `allow` can't shadow them). `allow` rules are dropped. Like all `rules`, they are **skipped in `yolo`** mode. |
| `[auto] hard_deny`, `protected_paths` | **Added** to your lists — never replace or shrink them. `hard_deny` applies in **every** mode, `yolo` included. |
| `[auto] block_destructive = true` | Honoured. `= false` is ignored. |

Everything else that loosens is ignored: `auto_approve`, `trusted_dirs`,
`trusted_domains`, `destructive_allow`, `destructive_disable_rules`,
`protected_paths_remove`, `unattended_policy`, any other
`[tools]` key (`transcripts`, `mcp_allow`, `search_backend`, `searxng_url`,
`cleanup_period_days`, …). So a repo can pin a stricter `mode`, a `deny` rule or
a `hard_deny` entry for everyone who works in it, but it can never grant itself
more. A `deny` rule alone does nothing for someone on `yolo`: for a block that
must hold for everyone, use `hard_deny` (or also set `mode`). Rule and
`hard_deny` entries match the tool name (`run_command`, `write_file`, …), its
`[tools]` category or the call's category, e.g.
`hard_deny = ["shell rm -rf*"]`. Outside
`[tools]`/`[auto]` a `.evi.toml` may only set model/sampling preferences — see
[Per-project config](../configuration.md#per-project-config--evitoml) for the
full list. Ignored keys are named once per process in a warning (e.g. `eVi:
ignoring these settings from project config <path>: auto.mode, …`); a file that
can't be parsed is ignored as a whole, so it can never switch your own settings
off. `--safe-mode` skips the file entirely. Changing a setting (`/model`,
`/effort`, the web Settings page) writes only your own `config.toml` values plus
that setting (for an inherited list, just the entries you added or removed),
so a repository's or a profile's `[auto]` / `[tools]` values never become your
global ones.

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
auto_approve = ["fs", "code_read", "memory", "skills", "image", "git"]
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
The call itself runs without a prompt: a sandboxed `run_python` counts as
`code_read`, which the default list approves. On Windows (or any machine with
no sandboxer on `PATH`), `run_python` asks first, as running code always does
unless you approved `code`, and if you approve it the snippet runs
**unsandboxed**, its output prefixed with:

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
  (always the case on Windows), `run_python`, `run_command` and `monitor` do
  **not** refuse — they run unsandboxed and tell you. `run_python` then counts
  as running code again, so it asks unless you approved `code`. Treat the
  sandbox as best-effort hardening, not a guarantee, and don't rely on it
  existing on Windows.
- **`run_python` is not otherwise a sandbox.** Even sandboxed, it's an OS-level
  confinement (read-only FS + no net), not a full container. The source itself
  notes it's "acceptable for personal-assistant use on a trusted machine."
- **Explicit `deny` always wins among the trust layers.** A `deny` rule beats
  `trusted_dirs`/`trusted_domains`. But note `yolo` mode short-circuits *before*
  rules are evaluated — in `yolo`, a `deny` rule is ignored (except that it
  still turns a protected write's prompt into a denial). Don't expect a
  deny-list to protect you while in `yolo`; use `hard_deny`.
- **`/auto on` overrides the mode, rules and trusted scopes.** It acts as `yolo`
  for the whole session; `hard_deny`, protected paths and the
  destructive-command guard still apply. It resets when you `/auto off` or
  exit; it is not persisted to config.
- **Categories, not individual tools, are auto-approved.** `auto_approve` works
  at the granularity of a call's category (`fs`, `code_read`, `code`, `web`,
  …). To approve or block a single tool, use a `rules` entry with the tool
  name. `code`, `shell`, `subagent`, `computer`, and `web` are intentionally
  left out of the defaults because they run code, act broadly or reach the
  network.
- **`monitor` commands and deferred tool search.** With `[tools] tool_search`
  on, `run_command` may sit in the deferred catalog until the model looks it up
  with `search_tools`. Until then `monitor` with `kind=command` says the session
  has no shell tools and runs nothing, even with `[tools] shell = true`.
- **Trusted-dir matching is path-resolved.** Only path arguments are checked,
  and each is `~`-expanded and resolved against the working folder to its real
  path before the sub-path check, so symlink/relative tricks resolve to their
  real target. A non-string argument can't be matched by an arg-glob; rules
  only see string-valued args.
- **Headless = strict.** In any run that can't prompt (scheduled tasks,
  `evi run`, subagents with no parent UI, the web app's workflow and recipe
  runs, federation, SDK agents without a `permission_callback`), anything not
  pre-approved is denied unless you set `[auto] unattended_policy = "allow"`,
  and remote-triggered runs deny it even then. Make scheduled tasks rely on
  `auto_approve` / `rules` / trusted scopes, not on interactive approval. See
  [Unattended runs](#unattended-runs).
- **`trusted_domains` is host-based.** It matches the URL host exactly or as a
  subdomain (`docs.python.org` also covers `x.docs.python.org`), not by path or
  scheme. For `web_fetch` an entry also unlocks that host's loopback / LAN
  addresses (see [What `web_fetch` will fetch](#what-web_fetch-will-fetch)), so
  list only hosts you mean the model to reach.

### Relevant source files

- `C:\evi\evi\permissions.py` — the `decide()` policy, rule/trust matching and
  protected paths (`effective_protected_paths`, `protected_write`).
- `C:\evi\evi\tools\base.py` — which arguments are paths (`Tool.path_args`),
  which tools only read (`READ_ONLY_TOOLS`) and a call's category
  (`Tool.category_for`, with `classify_call` in permissions.py).
- `C:\evi\evi\tools\monitor.py` — `monitor`'s shell-tools check, sandbox and
  tailed-file path argument.
- `C:\evi\evi\configlint.py` — the `[auto]` checks `evi lint` runs.
- `C:\evi\evi\doctor.py` — the `protected paths` and `code execution approval`
  checks.
- `C:\evi\evi\shellsplit.py` — splits a shell line into the commands rules are
  matched against.
- `C:\evi\evi\shell_guard.py` — the destructive-command guard.
- `C:\evi\evi\netguard.py` — `web_fetch`'s address, redirect and size checks.
- `C:\evi\evi\gitsafe.py` — the hardened git calls.
- `C:\evi\evi\sandbox.py` — `wrap()` / `available()` / `status()` per-OS.
- `C:\evi\evi\config.py` — `AutoSettings` (`[auto]`) and `ToolToggles` (`[tools]`).
- `C:\evi\evi\tools\code.py` — where `run_python` consults `tools.sandbox`
  (`sandbox.requested()` / `sandbox.active()`), for running and for its call
  category.
- `C:\evi\evi\llm\agent.py` — wires `decide()`, `/auto`, and `/plan` into the run loop,
  plus the unattended policy (`make_unattended`, `remote_unattended`, `policy_label`).
- `C:\evi\evi\llm\subagent.py` — subagent tool lists and how a subagent asks its parent.
- `C:\evi\evi\llm\cli_agent.py` — `unattended()`, the delegate CLI-agent refusal.
- `C:\evi\evi\apps\cli\main.py` — REPL slash commands and the CLI permission prompts.
