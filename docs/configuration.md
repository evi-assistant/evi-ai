# Configuration

All eVi state lives under `~/.evi/` (Windows: `%USERPROFILE%\.evi\`). The
primary file is `config.toml`. First launch writes a default. Hand-edit it
when you need to change something; nothing else holds machine-wide state.

## Primary file — `config.toml`

```toml
[llm]
backend         = "lmstudio"                  # lmstudio | ollama | llamacpp | openai_compat
base_url        = "http://localhost:1234/v1"  # default per-backend; auto-bumped by `evi models backend`
api_key         = "lm-studio"                 # local servers ignore it; `env:VARNAME` reads the env var
model           = "qwen2.5-7b-instruct"       # current active model
temperature     = 0.7                         # lower (0.3) is friendlier to tool calling
max_tokens      = 4096
request_timeout = 120.0
context_size    = 32768                       # token window; the managed llama.cpp runtime sets it itself
allow_cli_agent_unattended = false            # let unattended runs use a delegate CLI-agent backend (below)

[comfy]
base_url           = "http://localhost:8188"
default_checkpoint = "sd_xl_base_1.0.safetensors"
default_steps      = 25
default_width      = 1024
default_height     = 1024

[google]                                       # Phase 2 — deferred
client_secrets_path = ""
scopes              = ["https://www.googleapis.com/auth/gmail.readonly"]

[microsoft]                                    # Phase 2 — deferred
client_id = ""
tenant_id = "common"
scopes    = ["Mail.Read", "User.Read"]

[tools]
fs          = true     # read_file / write_file / list_dir
code        = true     # run_python in a subprocess
shell       = false    # not implemented; never default-on
gmail       = false    # Phase 2
outlook     = false    # Phase 2
image       = false    # ComfyUI text2img
memory      = true     # remember / recall / forget / list_memories
subagent    = false    # delegate_explore / delegate_plan
mcp         = false    # MCP-server-provided tools
skills      = true     # list_skills / invoke_skill
web         = false    # web_search / web_fetch (network!)
voice       = false    # speak_text / transcribe_microphone
computer    = false    # never default-on; mouse/keyboard control
transcripts = true     # write session JSONL for dreaming

[auto]
# Categories listed here run without prompting. `computer` is never here.
auto_approve = ["fs", "code", "memory", "skills", "image"]
# First-match "allow|deny <tool-name-or-category-glob> [arg-glob]"; "shell"
# also matches monitor kind=command. e.g. "deny shell rm*", "allow web".
rules = []
# Denied before everything else, yolo and /auto on included. Same syntax.
hard_deny = []
# Secret and shell start-up files: writing one asks in every mode, reading one
# asks even with fs auto-approved. eVi's built-in write-protected paths (EVI.md,
# AGENTS.md, .evi.toml, .evi/**, ~/.evi/**, .git/hooks/**, .github/workflows/**,
# …) always apply on top.
protected_paths = [".env", "*.env", ".env.*", ".npmrc", ".gitconfig", ".pypirc",
                   ".bashrc", ".zshrc", ".bash_profile", ".profile",
                   "*.pem", "id_rsa", "id_ed25519"]
# Protected-path entries to un-protect, by exact text (built-in or listed above).
# config.toml or a profile only; a .evi.toml can't set it.
protected_paths_remove = []

# Curated destructive-command guard (on by default). Commands like `rm -rf ~`,
# `git reset --hard`, `git push --force`, disk formats, `curl | sh`, secret
# exfil, or `terraform destroy` can never run SILENTLY: a match forces a
# confirmation prompt, and is DENIED when there's no UI to confirm with
# (headless / scheduler / MCP). Overrides yolo, accept_edits and auto_approve.
block_destructive = true
# fnmatch globs that exempt a command from the guard. Each is matched against
# every command in the line (`a && b` is two), never the whole line: a hit is
# exempt only if every command it touches matches, and never on a line eVi
# can't split. A glob with a wildcard in its first word (`*--force*`, `git*`)
# is ignored: name the command.
destructive_allow = ["git push --force-with-lease*"]
# Builtin rule ids to silence without disabling the guard (see evi/shell_guard.py).
destructive_disable_rules = ["git-commit-amend"]
# What a call that would ask does when nobody can be asked (a scheduled task,
# `evi run`, a subagent or workflow step with no UI, an SDK agent without a
# permission_callback): "deny" (default; any other value means deny) or
# "allow". Remote-triggered runs (federation, A2A, routine webhooks, channel
# push) always deny. config.toml or a profile only; a .evi.toml can't set it.
unattended_policy = "deny"
```

Everyday commands are deliberately *not* flagged — `rm -rf ./build`, `rm -rf
node_modules`, `git reset --soft`, `git branch -d`, `chmod 644`, `terraform
plan`, `apt remove <app>` all run as normal. Only genuinely destructive,
hard-to-reverse forms match. An explicit, *specific* allow rule (one carrying an
arg-glob, e.g. `allow run_command git reset --hard*`) is honoured as intent and
clears a **prompt**-severity hit, but only when it covers every command in the
line, the line can be split and its redirects stay inside the working folder —
so `allow run_command git*` does not clear `git log; git reset --hard`. A broad
`allow run_command` / `allow run_command *` never clears it, and no allow rule
clears a **block**-severity hit (`rm -rf ~`, a force-push, `curl … | sh`): only a
`destructive_allow` glob exempts one. Rules, here and in the permission policy,
may name the tool (`run_command`) or its category (`shell`). Details in
[Permissions & sandbox → Shell command
lines](features/permissions-and-sandbox.md#shell-command-lines).

Writes to protected paths ask in every mode and are denied when nothing can ask;
see [Permissions & sandbox → Protected
paths](features/permissions-and-sandbox.md#protected-paths). `evi lint` warns
about `[auto]` entries that can never apply, and `evi doctor` lists the
protected paths in force.

A run nobody is watching denies every call that would ask unless you set
`unattended_policy = "allow"`; the auto-approved categories, `allow` rules and
trusted dirs and domains still run. Protected writes, secret reads and the
guard deny there whatever the policy, `-y` and `yolo` included. See
[Permissions & sandbox → Unattended
runs](features/permissions-and-sandbox.md#unattended-runs).

### Unknown keys and how eVi saves `config.toml`

A key eVi doesn't know — a typo, or a setting from a newer eVi — no longer
stops eVi from starting. It is ignored with one warning per process
(`eVi: unknown settings in <path> (or the active profile): … They are ignored
but kept in the file`) and kept when eVi saves the file.

eVi saves `config.toml` when you change a setting (`/model`, `/effort`, the web
Settings page, a backend switch, …). It writes back your own file with only
that setting changed: values from an active profile or a trusted `.evi.toml`
are never copied in (see [Trusting a folder](#trusting-a-folder) for the
details), and keys eVi doesn't use — top-level keys, nested or inline tables,
dates — are kept. Comments are not kept. A value with a newline in it (a stop
sequence, say) is written escaped, so the file still loads.

### `context_size` and the managed llama.cpp runtime

With eVi's managed llama.cpp runtime (first-run setup in the web or desktop
app, or `evi runtime setup`), eVi owns `[llm] context_size`: it starts the
server with a window sized to your machine — 32768, 16384 or 8192 tokens,
whatever fits next to the model in VRAM (when the model runs on the GPU) or in
about 70% of RAM — and writes the same number into `config.toml`. That happens on setup, on a model switch, on the
GPU switch and on every relaunch. Web and desktop chats that are already open
use the new window from their next message, with no reload. `GET
/api/runtime/status` and `GET /api/runtime/catalog` report it as `ctx` (`null`
when no server is running).

Switching to another backend from the web or desktop UI (the backend switch or
the model picker) gives `config.toml` back the `context_size` it had before the
runtime took over, at once and in open chats too; if it had none, the key is
removed and the default (32768) applies. A value you set yourself after the
runtime took over is kept. A switch made outside the web app, such as with
`evi backend use`, is given back at the next `evi web` or desktop start. A
`context_size` from a profile or a trusted repository's `.evi.toml` is never
written into `config.toml` and never what is given back; such a value still
applies to sessions that read the config, but a web or desktop chat talking to
the managed server always uses that server's window.

### `evi config show` prints the resolved config (with profile overlay).
### `evi config path` prints the file path.

### Online providers — `evi models preset`

eVi is local-first, but any OpenAI-compatible cloud gateway works via
`backend = "openai_compat"`. Named presets make the common ones one command:

```bash
evi models preset                         # list providers + whether each key env var is set
evi models preset openrouter -m anthropic/claude-3.5-sonnet
evi models preset openai                  # uses $OPENAI_API_KEY (api=chat; set api=responses for the Responses API)
```

Presets (`openrouter`, `openai`, `xai`, `anthropic`, `groq`, `together`) all set
`backend=openai_compat` + the provider `base_url` and, by default, an
`api_key = "env:<PROVIDER>_API_KEY"` reference so your key stays in the
environment, not in `config.toml`. (Pass `--api-key` to store it inline instead.)
The **anthropic** preset targets Anthropic's OpenAI-compatible endpoint, not the
native Messages API.

### Claude via your Max/Pro plan — `backend = "claude_agent"` (no API key)

The `claude_agent` backend talks to Claude through the local **`claude` CLI**
(the Claude Agent SDK), authenticating with your Claude **subscription login**
instead of an `ANTHROPIC_API_KEY` — the sanctioned, no-key way to use your
Max/Pro plan from your own tools.

Setup (one time):

```bash
npm i -g @anthropic-ai/claude-code   # the `claude` CLI, if not already installed
claude                                # log in on your Max/Pro plan
pip install 'evi-assistant[claude-agent]'
```

Then add + select it (no URL, no key):

```bash
evi backend add claude --kind claude_agent   # or: Settings → Model & Backend → add, kind claude_agent
evi backend use claude --model opus           # models are aliases: opus | sonnet | haiku
```

The `opus` / `sonnet` / `haiku` aliases resolve to whatever your plan currently
serves. **Tools work fully** — eVi still runs its own tools (with its
permissions, checkpoints, and mode-scoped toolsets); the Agent SDK is used only
to decide *which* tool to call. Note this routes through the `claude` CLI (not an
HTTP endpoint), so it's local-machine-only and needs the CLI logged in; if the
SDK/CLI is missing, eVi says so the moment you select the backend.

### OpenAI Codex via your ChatGPT plan — `backend = "codex"` (no API key)

The `codex` backend is the OpenAI counterpart: it talks to the local **`codex`
CLI** authenticated by `codex login` (ChatGPT **Plus/Pro/Business**), no
`OPENAI_API_KEY`.

```bash
npm i -g @openai/codex   # the `codex` CLI (or: brew install --cask codex)
codex login              # browser OAuth against your ChatGPT plan
evi backend add codex --kind codex     # or Settings → Model & Backend → kind codex
evi backend use codex --model gpt-5-codex   # models: gpt-5-codex | gpt-5
```

Under the hood eVi runs `codex exec --json -s read-only --ephemeral -` with the
conversation on stdin and streams the answer.
**Difference from `claude_agent`:** Codex is an *autonomous* agent that runs its
**own** tools in its sandbox, so it's a **chat / delegate** provider — eVi's tools
don't route through it, and it can't edit files during a chat turn. Both backends
are built on the same shared CLI-agent shim (`evi/llm/cli_agent.py`), so they
behave consistently and adding further subscription-login CLIs is cheap.

### Google Gemini via an API key or Vertex AI — `backend = "gemini"`

The `gemini` backend uses the local **`gemini` CLI** with a **Gemini API key** or
**Vertex AI**. Since **2026-06-18** Google no longer serves Gemini CLI to free,
Google AI Pro and Google AI Ultra "Login with Google" accounts: those plans moved
to Google's closed **Antigravity** CLI, which eVi doesn't drive. (A Gemini Code
Assist Standard / Enterprise login still works.)

```bash
npm i -g @google/gemini-cli   # the `gemini` CLI
export GEMINI_API_KEY=…       # a key from https://aistudio.google.com/apikey
# or Vertex AI: GOOGLE_GENAI_USE_VERTEXAI=true plus GOOGLE_CLOUD_PROJECT and
# GOOGLE_CLOUD_LOCATION (or GOOGLE_API_KEY); pick the same auth in gemini's settings
evi backend add gemini --kind gemini        # or Settings → Model & Backend → kind gemini
evi backend use gemini --model gemini-2.5-flash   # models: gemini-2.5-pro | gemini-2.5-flash
```

An unpaid key gets a small daily quota; a paid key or Vertex AI lifts it. eVi runs
`gemini -o json --approval-mode default -p "<fixed instruction>"` with the
conversation on **stdin**, never on the command line, and reads the reply. The
approval mode is `default`, never `yolo`: in a headless turn Gemini treats a tool
that needs approval (shell, file writes) as denied, so a chat turn stays
read-only like `codex`. Like `codex` it's a **chat / delegate** provider (Gemini
drives its own tools; eVi's tools don't route through it), built on the same
shared shim. When gemini reports the "Login with Google" cut-off, a rejected key
or a used-up quota, eVi's error says what to do, and `evi doctor` checks for the
key or Vertex AI setup.

### Amp via your subscription — `backend = "amp"` (no API key)

The `amp` backend uses the local **`amp` CLI**, authenticated by `amp login` (an
Amp subscription / credit balance) or an `AMP_API_KEY` access token — not a
per-token model API key.

```bash
npm i -g @ampcode/cli   # the `amp` CLI (it replaced @sourcegraph/amp; or Amp's installer)
amp login               # Amp subscription (or: set AMP_API_KEY)
evi backend add amp --kind amp             # or Settings → Model & Backend → kind amp
evi backend use amp --model medium         # modes: medium | low | high | ultra
```

eVi runs `amp -x --stream-json --mode <mode>` with the conversation on stdin and
streams the reply. Amp selects its model by **agent mode** (`low`/`medium`/`high`,
or `ultra` for the deepest reasoning — slowest, most credits), not a model id.
Like `codex`/`gemini` it's a **chat / delegate** provider, but note Amp is
autonomous and *can* use tools (including file edits) per your configured `amp
permissions` — restrict it there if you want a read-only chat. eVi **won't start
Amp unauthenticated** (its login flow would block); `evi doctor` checks for the
login too.

### Qwen Code via the free Qwen login — `backend = "qwen"` (no API key)

The `qwen` backend uses **Qwen Code** (Alibaba's gemini-cli fork): sign in free
with a qwen.ai account (a generous free tier, ~2000 requests/day) — no API key.

```bash
npm i -g @qwen-code/qwen-code   # the `qwen` CLI
qwen                            # run once and pick 'Qwen' to sign in (free OAuth)
evi backend add qwen --kind qwen            # or Settings → Model & Backend → kind qwen
evi backend use qwen --model qwen3-coder-plus   # models: qwen3-coder-plus | qwen3-coder-flash
```

eVi runs `qwen -o json --approval-mode default -p "<fixed instruction>"` with the
conversation on **stdin** (Claude-Code-style events) and reads the reply — a
**chat / delegate** provider like `gemini`, on the same shared shim. The approval
mode is pinned to `default`, so qwen's own settings can't switch a chat turn to
`yolo`.

### GitHub Copilot via your subscription — `backend = "copilot"` (no API key)

The `copilot` backend uses the local **`copilot` CLI** (`@github/copilot`),
authenticated by your GitHub **Copilot subscription** (`copilot login`) — no
separate model API key.

```bash
npm i -g @github/copilot   # the `copilot` CLI
copilot login              # GitHub Copilot subscription
evi backend add copilot --kind copilot          # or Settings → Model & Backend → kind copilot
evi backend use copilot --model auto            # models: auto | claude-sonnet-4.5 | gpt-5
```

eVi pipes the conversation to `copilot --output-format text -s` on **stdin** (no
`-p`: copilot ignores piped input when `-p` is given) and reads the reply, adding
`--no-ask-user` when the installed CLI has it. `--model auto` lets Copilot pick;
the exact set depends on your plan. A **chat / delegate** provider; unapproved
tool calls are auto-denied in non-interactive mode, so a chat turn stays
answer-only.

### The CLI-agent backends at a glance

| kind | CLI | Install | Auth | Tools |
|---|---|---|---|---|
| `claude_agent` | `claude` | `pip install 'evi-assistant[claude-agent]'` (+ the `claude` CLI) | Claude **Max/Pro** login | **eVi drives tools** (full parity) |
| `codex` | `codex` | `npm i -g @openai/codex` | ChatGPT **Plus/Pro** login | delegate (Codex's own, read-only) |
| `gemini` | `gemini` | `npm i -g @google/gemini-cli` | Gemini **API key** or **Vertex AI** | delegate (Gemini's own; approval-gated tools denied) |
| `amp` | `amp` | `npm i -g @ampcode/cli` | Amp **subscription** (`amp login` / `AMP_API_KEY`) | delegate (Amp's own, per `amp permissions`) |
| `qwen` | `qwen` | `npm i -g @qwen-code/qwen-code` | Qwen **free** OAuth login | delegate (Qwen's own; approval-gated tools denied) |
| `copilot` | `copilot` | `npm i -g @github/copilot` | GitHub **Copilot** login | delegate (Copilot's own, auto-denied) |

All six are local-machine-only (they shell out to a CLI, so they don't work in
the packaged desktop sidecar) and report clearly if their CLI isn't installed;
`evi doctor` names the install command. For `gemini`, `qwen` and `copilot` the
flags eVi passes are checked against the installed CLI's `--help`, so a CLI too
old for them gets an "update it" error instead of failing mid-chat.

**The conversation goes on stdin.** `codex`, `gemini`, `amp`, `qwen` and
`copilot` all get the rendered conversation on stdin; the command line carries
only fixed flags and a model id or mode, and a model id with anything but
letters, digits and `. _ : / @ + = -` is refused. On Windows the npm-installed
CLIs are `.cmd` shims that run their command line through `cmd.exe`, which would
mangle quotes, `&`, `%`, `|` and newlines and stop at 8191 characters.

**Every turn is time-bounded**: 180 s for `gemini`, `qwen`, `copilot` and `amp`,
600 s for `codex` and `claude_agent`. A turn that runs over ends with an error
saying so. For the CLIs eVi starts itself (all but `claude_agent`), the CLI's
whole process tree is killed: on Windows each run gets its own Job Object (the
CLI only starts running once it is in it, so everything it starts is in it too,
unless a process explicitly asks to leave it); on macOS/Linux the CLI leads its
own session (process group). If a CLI answers and exits but something it started
still holds its output 2 s later, that leftover is killed and the answer is
kept. On macOS/Linux, Ctrl-C in the terminal therefore doesn't reach the CLI
directly; the turn is still bounded, and a CLI still running when eVi exits
normally is killed.

**Delegate backends run their own tools.** `codex`, `gemini`, `amp`, `qwen` and
`copilot` run their tools under their own approval rules, outside eVi's
permission checks, `hard_deny` list and protected paths. So a run nobody is
watching refuses one, as `[llm] backend` or as any `[models] *_backend`: a
scheduled task, `evi run` / `evi batch` / `evi team run`, a subagent with no
parent UI (and so `evi dream`), an ultracode stage, and the web app's
workflow, recipe, routine, ultracode, channel-push, federation and A2A runs.
The refusal names the setting; the web app answers `409` (an A2A task fails
instead). Point `[llm] backend` and any `[models] *_backend` at a local or API
backend on a machine that serves those, or set `[llm]
allow_cli_agent_unattended = true` to accept the risk. The Telegram channel
refuses a delegate backend even then. A chat you are watching is not affected.

## Profiles — `~/.evi/profiles/<name>.toml`

Partial overlay merged on top of `config.toml`. Activated by env var
`EVI_PROFILE=<name>` or `--profile <name>` / `-p <name>` on any CLI
invocation.

```toml
# ~/.evi/profiles/home.toml
[llm]
backend  = "openai_compat"
base_url = "http://ai-server.local:8000/v1"
model    = "qwen2.5:32b"
```

```bash
evi profile add away --backend lmstudio --model llama3.2:1b-instruct-q4_K_M
evi profile list
evi --profile home chat
```

Profile merge is **deep** for tables (dicts) and **replacing** for lists.
So a profile that overrides `[microsoft] scopes` replaces it wholesale
rather than appending.

## Per-project config — `.evi.toml`

A `.evi.toml` in a repository (the nearest one walking up from the working
directory, stopping at the repository's top level) is layered on top of
`config.toml` + the active profile. Because it comes with the repo — including
repos you merely cloned — it is **untrusted**: it applies only in a folder
you have **trusted**, and even then it is filtered through an allowlist
(since 1.0.53) and everything not listed below is ignored.

### Trusting a folder

Until you trust the folder, eVi ignores its `.evi.toml` entirely — even the
allowlisted keys — and says so once per process (`eVi: not applying project
config <path>: this folder is not trusted. It sets: …`; key names only, never
values).

- **`evi chat`** asks the first time it starts in a folder with a
  `.evi.toml`: it lists the keys the file sets, which of them would apply,
  and which a `.evi.toml` can never set, then asks `Trust this folder …?
  y/N`. The default is no. `evi sessions resume`, `continue` and `fork` ask
  the same way. Without a terminal (piped input or output, or a command the
  agent runs) it only shows the summary.
- **`evi trust [PATH]`** trusts a folder up front (default: the current one):
  it prints the same summary and asks you to confirm. In a script, pass
  `--yes` to skip the question; without a terminal and without `--yes` it
  refuses. It also refuses, even with `--yes`, inside an `evi chat` session —
  a command the agent runs through its shell tool, or a `!` command in the
  REPL — so a repository's instructions can't get the agent to trust its own
  folder; run it in your own terminal. (That check relies on the
  `EVI_INTERACTIVE=1` marker the REPL sets; the web and desktop apps don't set
  it, so don't auto-approve shell commands for an agent working in a
  repository you haven't reviewed.) `evi trust --status [PATH]` shows the state without changing
  it, `evi trust --list` lists trusted folders and `evi trust --revoke [PATH]`
  removes one.
- **`evi run`, `evi batch`, the web UI and the scheduler never ask**: in an
  untrusted folder they run with the project config off (fail closed) and
  print the warning above. Trust the folder first with `evi trust` if a
  scripted run should use it. The web API exposes the same state for a
  session's working folder at `GET /api/project/trust?session_id=…`;
  `POST /api/project/trust` with `{session_id, trust: true|false,
  config_sha256}` records the answer (pass the `config_sha256` the GET
  returned and a file that changed in between is refused with 409; in
  multi-user mode only the owner may change it). For now the settings
  themselves come from the folder eVi was started in — the web server's (and
  `evi chat --cwd`'s) process folder — not a session's working folder.

Trust is recorded **per repository**: inside git it is keyed by the
repository's git common directory, so every worktree of a trusted repository
is trusted too. (A `.git` *file* counts as a worktree of a repository only
when that repository's own git directory holds a worktree record pointing
back at it, so a folder can't borrow another repository's trust; a submodule
is trusted on its own.) Outside git, trust covers exactly the folder you
trusted, not its subfolders (an archive unpacked into a trusted folder later
is not trusted), so starting eVi in a subfolder asks again. The list lives in
`~/.evi/trusted_folders.json`.

Trusting approves the `.evi.toml` **as it is now**. If the file changes
afterwards, it stops applying until you trust the folder again (`evi chat`
asks again; the warning says `it is new or changed since you trusted this
folder` — also the case for a second `.evi.toml` deeper in a trusted
repository, until you approve it too). A repository remembers the last few
versions you approved, so worktrees on branches with different `.evi.toml`
files don't keep asking. Editing EVI.md or AGENTS.md doesn't affect trust:
those are instructions for the model, and they are loaded whether or not the
folder is trusted — they can't run anything or change a setting.

Settings commands (`/model`, `/effort`, the web Settings page, …) write only
your own `config.toml` values plus the setting you changed: values that came
from a `.evi.toml` or an active profile are never copied into `config.toml`.
For a list you inherit entries of (a profile's `auto_approve`, a trusted
repository's `hard_deny` or `protected_paths` entries), only the entries you
add or remove are written to your own list. Undoing a change (setting it back
to what it was) restores your own value. Setting a value you only inherit —
for example choosing the profile's model while that profile is active — is
not a change, so it isn't copied into `config.toml`; put it there yourself to
keep it without the profile.

### What a trusted `.evi.toml` can set

eVi names the keys it ignores once per process in a warning (`eVi: ignoring
these settings from project config <path>: …`; odd key names are shown
escaped). A file that can't be used at all — not valid UTF-8 TOML, nested too
deeply, or larger than 64 KB — is ignored as a whole and reported the same
way; it never stops eVi from loading your own config. `--safe-mode` skips the
file entirely.

**Applied as-is** (model / sampling preferences only):

- `[llm]` `model`, `embed_model`, `embed_dimensions`, `temperature`, `top_p`,
  `presence_penalty`, `frequency_penalty`, `seed`, `stop_sequences`,
  `logit_bias`, `max_tokens`, `max_completion_tokens`, `context_size`,
  `compact_after_messages`, `compact_keep_recent`, `compact_when_pct`,
  `reasoning_effort`, `parallel_tool_calls`, `cache_prompt`, `logprobs`,
  `top_logprobs`, `output_style`
- `[worktree]` `base_ref`

A `context_size` from a `.evi.toml` applies to sessions that read the config,
but a web or desktop chat talking to eVi's managed llama.cpp server always uses
that server's window (see [`context_size` and the managed llama.cpp
runtime](#context_size-and-the-managed-llamacpp-runtime)).

Values must have the field's type and contain no control characters, and
anything else is ignored (the warning marks it `(invalid value)`):

- names (`model`, `embed_model`, `output_style`) must be plain names —
  letters, digits and `_ . : / @ + -`, with single spaces allowed inside
  (`"Qwen2.5 7B Instruct"`), not starting with `-` or with a word that starts
  with `-`; `base_ref` may also use `~` and `^` (e.g. `HEAD~1`) but no spaces;
- numbers must be finite and in range: `temperature` 0–2, `top_p` 0–1,
  `presence_penalty` / `frequency_penalty` −2–2, `max_tokens` ≥ 1,
  `compact_when_pct` 1–100, `embed_dimensions` 1–65536, `top_logprobs` 0–20,
  `seed` a 64-bit integer, and the other counts 0 to 2³¹−1;
- `reasoning_effort` must be one of `off`, `none`, `low`, `medium`, `high`,
  `max`;
- `logit_bias` must be a JSON object of token id → number from −100 to 100;
- `stop_sequences` may hold at most 16 entries of up to 256 characters
  (newlines and tabs are fine).

`fast_mode`, `fast_model`, `fallback_models`, `router_enabled`,
`router_model` and `request_timeout` are **not** honoured from a `.evi.toml`:
each can make a turn run on a different model (or deadline) from the one the
`[[judge]]` guardrails use, and a judge that errors lets the message through.

**Tighten-only** (`[tools]` and `[auto]`): a project can switch tool categories
off (and the `run_python` / `run_command` OS sandbox on), pick a *stricter*
`mode`, add `deny` rules, add `hard_deny` / `protected_paths` entries, and
force `block_destructive = true` — never the reverse. `[tools] transcripts` is
not a tool category and can't be changed. Details in
[Permissions & sandbox](features/permissions-and-sandbox.md#per-project-and-per-profile-overrides).

**Always ignored**: anything that is an endpoint, credential, executable,
shell command, listener or file path — `[llm]` `backend`, `base_url`,
`api_key`, `api`, `responses_tools` — and the whole `[statusline]`,
`[runtime]`, `[web]`, `[federation]`, `[channels]`, `[notify]`,
`[telemetry]`, `[plugins]`, `[desktop]`, `[google]`, `[microsoft]`,
`[obsidian]`, `[comfy]`, `[voice]`, `[models]` and `[ultracode]` sections, plus
any unknown key. Put those in `config.toml` or a profile.

```toml
# <repo>/.evi.toml
[llm]
model       = "qwen2.5-coder:14b"
temperature = 0.2

[tools]
computer = false            # no desktop control in this repo

[auto]
mode      = "ask"           # applies only if you're on yolo / accept_edits
rules     = ["deny run_command git push*"]  # skipped in yolo (the mode above rules that out)
hard_deny = ["run_command rm -rf*"]         # applies in every mode, even yolo
```

`rules` and `hard_deny` entries match the tool name (`run_command`,
`run_python`, `write_file`, `edit_file`, …) or its `[tools]` category, so
`"deny shell rm*"` and `"deny fs *.env"` work too. A `.evi.toml` can add
`protected_paths` entries but can't set `protected_paths_remove`.

## Memory — `~/.evi/memory/`

One markdown file per fact:

```
~/.evi/memory/
  INDEX.md             auto-regenerated; you can read but don't edit
  preferences.md       arbitrary name; first non-empty line becomes the summary
  project_paths.md
  .attic/              soft-deleted entries; safe to remove if you want
```

The `Agent.memory.format_for_prompt()` block shows up in every system
prompt. The model uses the `remember(name, content)`, `recall(name)`,
and `forget(name)` tools to manage it, in the same store the prompt lists:
in multi-user web mode that is the login's `~/.evi/users/<name>/memory/`
(see [Multi-user web](#multi-user-web--eviusersjson)). **`forget` is
soft-delete** — it moves to `.attic/`. The dreaming engine relies on this to avoid losing
data on bad consolidations.

Caps: 64 KB per entry, names must match `[A-Za-z0-9_-]{1,64}`.

## Skills — `~/.evi/skills/<name>/SKILL.md`

```
~/.evi/skills/
  summarize/
    SKILL.md          required — instructions + optional YAML frontmatter
    example-input.txt  optional assets (loadable by the model with read_file)
```

```markdown
---
name: summarize
description: Boil long text down to 3 bullets.
---

# Steps
1. Identify the topic.
2. Pull the three highest-impact points.
3. Format as Markdown bullets ≤ 12 words each.
```

The frontmatter `description` shows up in the system prompt's
`## Available skills` block. The model calls `invoke_skill(name)` to
load the full body when it decides the skill applies.

## Slash commands — `~/.evi/commands/<name>.md`

User-defined prompt templates. Type `/<name> args` in the chat REPL or
web UI and the file's body is sent as the user turn with `{args}`
substituted.

```markdown
# ~/.evi/commands/commit.md
Run `git diff` to see what changed, then propose a conventional-commits
style message under 70 chars. Args: {args}
```

An `@path` written in the template inlines that file, if it is inside the
working folder or an `[auto] trusted_dirs` entry and is neither a protected
path nor a key / credential file. An `@path` in the arguments is never read,
and MCP-served prompts never inline. See
[File references](features/slash-commands.md#file-references-path).

Built-in slash commands (handled in code, not files): `/help /reset
/exit /tools /model /goal /plan /auto`.

## Hooks — `~/.evi/hooks.toml`

```toml
[[before_tool_call]]
name             = "audit"
match            = "*"                  # glob over tool names (fs.*, write_file, etc.)
command          = ["bash", "-c", "echo $EVI_HOOK_TOOL >> ~/.evi/logs/tools.log"]
timeout          = 5

[[before_tool_call]]
name             = "no-system-writes"
match            = "write_file"
command          = ["bash", "-c", 'echo "$EVI_HOOK_ARGS_JSON" | grep -qv "/etc/"']
veto_on_nonzero  = true                 # non-zero exit blocks the tool

[[after_tool_call]]
name    = "notify"
match   = "generate_image"
command = ["notify-send", "Image ready"]

[[after_tool_call]]
name    = "webhook"
match   = "*"
url     = "https://example.com/evi-hook"   # POST instead of spawning a command
```

Env vars set in the child process (command hooks):

- `EVI_HOOK_EVENT` — the event (`before_tool_call`, `user_prompt_submit`, …)
- `EVI_HOOK_TOOL` — fully-qualified tool name (the event name for lifecycle
  events)
- `EVI_HOOK_ARGS_JSON` — JSON of the call arguments (the prompt for
  `user_prompt_submit`, the finish reason for `stop`), whole up to the OS limit
  for one environment variable (about 32,000 characters on Windows, 128 KiB on
  Linux)
- `EVI_HOOK_ARGS_FILE` — path of a temp file holding the whole payload, set
  when `EVI_HOOK_ARGS_JSON` was cut and always for a hook with
  `args_via_file = true`; deleted when the hook exits
- `EVI_HOOK_ARGS_TRUNCATED` — `1` when `EVI_HOOK_ARGS_JSON` was cut
- `EVI_HOOK_RESULT` — tool output (after-hooks only, capped at 4 KB)
- `EVI_EFFORT` — the active reasoning effort
- `EVI_CWD` — the session working folder; the hook also runs in it

A guard (`veto_on_nonzero = true`) is never run on a cut payload: the action
is blocked instead, unless the hook sets `args_via_file = true` and reads
`EVI_HOOK_ARGS_FILE`. See [Hooks → Big
payloads](features/hooks.md#big-payloads).

A hook uses either `command` (argv, spawned) **or** `url` (HTTP POST of
`{event, tool, args_json, effort, cwd, result}`). For a url hook a 2xx
response means success; any other status becomes the exit code, so
`veto_on_nonzero` still gates the call.

Besides the tool events, hooks fire on **lifecycle events** (use `match = "*"`):

```toml
[[user_prompt_submit]]   # before each turn — veto blocks the prompt
name = "no-secrets"
command = ["python3", "/path/check_prompt.py"]   # prompt is in EVI_HOOK_ARGS_JSON
veto_on_nonzero = true

[[before_compact]]       # before history compaction — veto keeps it intact
[[stop]]                 # after a turn completes (notification; never blocks)
```

## Keybindings — `~/.evi/keybindings.toml`

Map a key to a slash command in the interactive chat REPL — pressing it
replaces the line with that command and submits it.

```toml
[keybindings]
"c-t"      = "/tools"      # Ctrl-T
"f2"       = "/model"      # F2
"escape g" = "/goal"       # Esc then g (a two-key sequence)
```

Keys use prompt_toolkit names (`c-t`, `f2`, `escape`, …); a space-separated
value is a multi-key sequence. Terminal essentials (`c-c`, `c-d`, `tab`,
`enter`) are reserved and silently ignored, and an unknown key name is skipped
without breaking the others.

## Voice / TTS — `[voice]` in `config.toml`

```toml
[voice]
engine       = "system"   # system | coqui | f5 | piper
model        = ""         # engine-specific: Coqui XTTS id, or a Piper voice .onnx path
clone_sample = ""         # reference WAV for the cloning engines (coqui / f5)
language     = "en"
```

- **system** — zero-dep platform voice (Windows SAPI / macOS `say` / espeak).
- **coqui** — Coqui XTTS v2; multilingual, clones a voice from `clone_sample`.
- **f5** — F5-TTS; fast zero-shot cloning (uses its `f5-tts_infer-cli`).
- **piper** — lightweight local neural voices (set `model` to a `.onnx`); no cloning.

The neural engines are optional heavyweight installs; eVi lazy-imports them and
falls back to a clear error if the deps/binaries aren't present. `evi voice
engines` shows which are installed and which is active; switch the engine in the
desktop **Settings → Voice** screen or by editing `[voice]`.

## Workflows — `~/.evi/workflows/<name>.toml`

Where a recipe is a sequence of turns through one conversation, a **workflow**
orchestrates independent steps — each its own headless agent — with parallel
fan-out and variable interpolation.

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
prompt = "List the upsides of {topic} given this plan:\n{plan}"

[[steps]]
id = "cons"
parallel = true
prompt = "List the downsides of {topic} given this plan:\n{plan}"

[[steps]]
id = "synth"
prompt = "Synthesize a balanced take.\nUpsides:\n{pros}\nDownsides:\n{cons}"
```

- Steps run in file order; a contiguous run of `parallel = true` steps runs
  concurrently. A following sequential step is the natural fan-in point.
- Prompts interpolate workflow `[vars]` and earlier step outputs by id —
  `{topic}`, `{plan}`, … (escape literal braces as `{{` / `}}`).
- Each step is an unattended headless agent; set `mode` on a step for a tool
  preset (`chat`/`cowork`/`code`). `evi workflow run` auto-approves every tool
  call; a run from the web app's Dispatch panel runs only the auto-approved
  categories and denies anything else unless `[auto] unattended_policy =
  "allow"`.

`evi workflow new <name>` scaffolds one; `evi workflow run <name> --var topic=…`
runs it (`--json` for machine output). The desktop **🗂 Dispatch** panel lists
and launches workflows and shows every live session.

## Plugins — `~/.evi/plugins/<name>/` + marketplace

A plugin bundles any of: `commands/`, `skills/`, `hooks.toml`, `mcp.json`, and
`agents.toml` (subagent profiles, used via the `delegate` tool — see
`evi agents`). Install from a directory or git URL with `evi plugin add`, or by
name from the marketplace index:

```json
// ~/.evi/marketplace.json
{
  "plugins": [
    { "name": "git-helpers",
      "source": "https://github.com/you/evi-git-helpers.git",
      "description": "Handy git slash commands",
      "tags": ["git"] }
  ]
}
```

```toml
# config.toml — extra remote index files merged with the local one
[plugins]
index_urls = ["https://example.com/evi-plugins.json"]
```

`evi plugin search [query]` lists matches; `evi plugin install <name>` resolves
the name through the index and installs its `source`. `evi plugin index init` /
`evi plugin index add <name> <source>` manage the local index.

## Federation — `~/.evi/peers.json`

Delegate a task to a trusted peer eVi (e.g. a GPU box):

```json
[ { "name": "gpu", "url": "http://gpu-box:8473", "token": "<peer web token>" } ]
```

`evi peer run gpu "summarise this repo"` (or the `delegate_peer` tool, category
`federation`, off by default) POSTs to the peer's `/api/federate`. The peer must
opt in with `[federation] serve = true`; it runs the task non-interactively
(tools not auto-approved are denied, whatever the peer's `[auto] mode` or
`unattended_policy` says).

### A2A (Agent2Agent) — interop with any agent

Federation is eVi's **private fast path** to your *own* eVis. **A2A**
([a2a-protocol.org](https://a2a-protocol.org), a Linux Foundation standard) is the
**interop path** — it lets eVi talk to *any* vendor's agent that speaks A2A, and
lets such agents call eVi. No extra dependency (hand-rolled against the v0.3/v1.0
wire shapes).

**Discovery — always on, public.** eVi serves a spec-compliant **Agent Card** at
`GET /.well-known/agent-card.json` (auth-exempt). It carries the standard fields
(`protocolVersion`, `capabilities`, `skills`, `securitySchemes`) plus eVi's model
capability flags under an `x-evi` extension, so capability-aware clients can route
by modality.

**Serve — opt in.** Set `a2a = true` under `[federation]` to expose the JSON-RPC
endpoint `POST /a2a`:

```toml
[federation]
a2a = true      # expose POST /a2a (off by default)
```

It implements `message/send`, `tasks/get`, and `tasks/cancel`, is **bearer-token
gated** (same web token as the rest of the API), and runs each delegated task
**non-interactively** — tools not already auto-approved are denied, exactly like
`/api/federate`. Streaming (`message/stream`) and push notifications aren't
implemented yet, so the card advertises `capabilities.streaming = false` and a
compliant client falls back to blocking `message/send`.

**Call out — the `delegate_a2a` tool.** eVi can delegate to any external A2A agent
by its JSON-RPC URL:

```
delegate_a2a(url="https://some-agent.example/a2a", task="research X", token="…")
```

(category `federation`, off by default — enable the tool to let the model use it).

## Web access — `[web]` in `config.toml`

Who the web server (`evi web`, the desktop app, the Docker image) answers, and
how long it waits on a permission prompt:

```toml
[web]
auth_token         = ""      # access token; empty = only this machine can connect
multi_user         = false   # per-person logins from ~/.evi/users.json (below)
decision_timeout_s = 300.0   # an unanswered permission prompt is denied after this
```

- **No token** (`auth_token` empty, no `EVI_WEB_AUTH_TOKEN`, no users): the
  server answers only loopback clients on this machine, and
  `evi web --host 0.0.0.0` refuses to start.
- **`auth_token`** set: every client needs it, this machine's too.
  `evi web-config token rotate` sets a new one (in effect at once, no restart),
  `evi web-config token show` prints the token clients need, and
  `evi web-config token clear` removes it.
- **`EVI_WEB_AUTH_TOKEN`** (the launch token): an environment variable of the
  server process that gates other machines only. The desktop app sets it when
  LAN access is on, and so does the Docker image when no token is configured.
- **`decision_timeout_s`**: `EVI_DECISION_TIMEOUT_S` overrides it; anything
  that isn't a positive number means 300.

The full request rules (browsers and `X-Evi-Client`, proxies, the desktop
app's LAN token, containers, owner-only routes) are in
[security.md](security.md).

## Multi-user web — `~/.evi/users.json`

Opt-in (`[web] multi_user = true`): each person logs in with their own revocable
token instead of sharing `auth_token`.

```json
[ { "name": "alice", "token": "…" }, { "name": "bob", "token": "…" } ]
```

Manage with `evi web-config users add/list/remove`. Each user gets an **isolated
workspace** — their web sessions, transcripts, and memory live under
`~/.evi/users/<name>/` and aren't visible to other users. Memory there means
both the index in the system prompt and the memory tools (`remember`,
`recall`, `forget`, `list_memories`, `recall_by_tag`), including in the
subagents a user's chat starts; notes those tools saved to the shared
`~/.evi/memory/` before this release stay there. Drop a user from the
file to revoke access. Skills and installed plugins stay shared, but the routes
that act on the host (settings, hooks, plugins, channels, peers, the runtime,
MCP, backends, checkpoints and rewind, and host-level runs) are owner-only:
they need `[web] auth_token` (or the launch token), and a login gets 403. A
login named `owner` can't sign in, since that name is the owner's workspace.

`<name>` is the login's name with every character outside `A-Z a-z 0-9 _ . -`
replaced by `_`, and leading and trailing `.`, `_` and `-` stripped (`user` if
nothing is left). Logins whose folders would match, ignoring case
(`bob smith` and `bob_smith`; `Alice` and `alice`, one folder on Windows and
macOS), can't sign in at all until all but one are renamed or removed: each
gets a 403 that says so, and the server log names the whole group. Details in
[security.md](security.md#multi-user-mode).

## Guardrails — `~/.evi/guardrails.toml`

A local content filter over the model. Off by default; `enabled = true` to turn
it on. Two rule types layer together:

```toml
enabled = true

[[rule]]                      # regex — fast, deterministic
name = "block-secrets"
pattern = "(?i)(api[_-]?key|secret)\\s*[:=]"
action = "block"             # block | redact
applies_to = "input"         # input | output | both

[[judge]]                     # semantic — graded by the LLM
name = "no-self-harm"
policy = "Requests for, or content encouraging, self-harm or suicide."
applies_to = "both"

[[classifier]]                # offline ML moderation model
name = "toxicity"
model = "unitary/toxic-bert"  # any HF text-classification model ("" = default)
labels = ["toxic", "threat", "insult"]   # labels that block ([] = any)
threshold = 0.7
applies_to = "both"
```

Three layers run in order, stopping at the first block:

- **`[[rule]]` regex** — fast, deterministic: `block` refuses the turn (input) or
  scrubs the stored reply (output); `redact` replaces spans with `[REDACTED]`.
- **`[[judge]]`** — eVi's own model classifies the text against `policy` and
  blocks on a match. The local counterpart to a hosted moderation API; one model
  round-trip per turn.
- **`[[classifier]]`** — a local HuggingFace text-classification model scores the
  text and blocks when a `labels` score crosses `threshold`. Fully offline; needs
  `pip install 'evi-assistant[moderation]'` (transformers + torch). Block-only.

Both semantic layers **fail open** (a missing/flaky model skips the rule, not the
turn). Inspect with `evi guardrails list`; `evi guardrails test "<text>"` dry-runs
the regex layer.

Inspect with `evi guardrails list`; dry-run the regex layer with
`evi guardrails test "<text>"`.

## Deep links — `evi://`

The desktop app registers the `evi://` URL scheme. `evi://session/<id>` focuses
a session, `evi://workflow/<name>` opens the dispatch panel, `evi://new` starts a
chat. `evi link [id|new]` prints a link; `evi link --open <url>` shows where it
routes. The same targets work in a browser via `/?session=` and `/?workflow=`.

## MCP — `~/.evi/mcp.json`

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

Flip `tools.mcp = true` in `config.toml`. `evi mcp list-tools` enumerates
what each server exposes; tools land in the registry as
`<server-name>.<tool-name>`.

## Scheduled tasks — `~/.evi/scheduled/<id>.json`

Managed via `evi schedule add/list/remove/enable/disable/run-now`. One
file per task. Don't hand-edit unless you know the schema; the IDs are
opaque and the scheduler caches state.

```bash
evi schedule add --name "morning brief" --cron "0 8 * * *" \
                 --prompt "Summarize my overnight email."
evi scheduler          # foreground daemon, or just run `evi web`
```

A scheduled run is unattended: the auto-approved categories run and anything
that would ask follows `[auto] unattended_policy` (denied by default). Its log
names the policy (`# policy: deny`), and a run in which a call was denied gets
the status `denied: <tools> (policy deny)` in `evi schedule list`. A delegate
CLI-agent backend is refused unless `[llm] allow_cli_agent_unattended = true`.

## Where things write logs

| What                 | Where                            |
|----------------------|----------------------------------|
| Dream audit          | `~/.evi/logs/dreams/<stamp>.log` |
| Scheduled task runs  | `~/.evi/logs/scheduled/<id>_<stamp>.log` |
| Telegram channel turns (policy only) | `~/.evi/logs/channels/telegram.log` |
| User-defined hooks   | Wherever your hook commands write |
| Session transcripts  | `~/.evi/transcripts/<YYYY-MM-DD>/<session>.jsonl` |
| ComfyUI images       | `~/.evi/images/`                 |
| Screenshots          | `~/.evi/screenshots/`            |
| HF model downloads   | `~/.evi/models/<repo-flat>/`     |

## Environment variables

| Var                | Purpose                                                        |
|--------------------|----------------------------------------------------------------|
| `EVI_HOME`         | Override `~/.evi/` location entirely                           |
| `EVI_PROFILE`      | Active profile name (same as `--profile`)                      |
| `EVI_PYTHON`       | Tauri desktop: Python interpreter to spawn (default `py -3.13`) |
| `EVI_REPO_ROOT`    | Tauri desktop: pin the repo root rather than auto-detecting    |
| `EVI_REMOTE_URL`   | Tauri desktop: thin-client mode — skip spawn, navigate to URL  |
| `EVI_REMOTE_TOKEN` | Tauri desktop: the access token for the `EVI_REMOTE_URL` server |
| `EVI_WEB_AUTH_TOKEN` | Web server: launch token that gates other machines ([Web access](#web-access--web-in-configtoml)) |
| `EVI_DECISION_TIMEOUT_S` | Web server: permission-prompt deadline in seconds; overrides `[web] decision_timeout_s` |

The web server's other variables (`EVI_ALLOW_ANY_HOST`,
`EVI_ALLOW_CROSS_ORIGIN`, `EVI_TRUSTED_PROXY`) are listed in
[security.md](security.md#environment-variables).
