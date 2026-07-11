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
```

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
and `forget(name)` tools to manage it. **`forget` is soft-delete** — it
moves to `.attic/`. The dreaming engine relies on this to avoid losing
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

- `EVI_HOOK_EVENT` — `before_tool_call` or `after_tool_call`
- `EVI_HOOK_TOOL` — fully-qualified tool name
- `EVI_HOOK_ARGS_JSON` — JSON of the call arguments
- `EVI_HOOK_RESULT` — tool output (after-hooks only, capped at 4 KB)

A hook uses either `command` (argv, spawned) **or** `url` (HTTP POST of
`{event, tool, args_json, result}`). For a url hook a 2xx response means
success; any other status becomes the exit code, so `veto_on_nonzero` still
gates the call.

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
- Each step is an unattended (auto-approved) headless agent; set `mode` on a
  step for a tool preset (`chat`/`cowork`/`code`).

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
(tools not auto-approved are denied).

## Multi-user web — `~/.evi/users.json`

Opt-in (`[web] multi_user = true`): each person logs in with their own revocable
token instead of sharing `auth_token`.

```json
[ { "name": "alice", "token": "…" }, { "name": "bob", "token": "…" } ]
```

Manage with `evi web-config users add/list/remove`. Each user gets an **isolated
workspace** — their web sessions, transcripts, and memory live under
`~/.evi/users/<name>/` and aren't visible to other users. Drop a user from the
file to revoke access. (Skills/plugins/config stay shared — they're capabilities,
not personal data.)

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

## Where things write logs

| What                 | Where                            |
|----------------------|----------------------------------|
| Dream audit          | `~/.evi/logs/dreams/<stamp>.log` |
| Scheduled task runs  | `~/.evi/logs/scheduled/<id>_<stamp>.log` |
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
