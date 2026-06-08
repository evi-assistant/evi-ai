# Configuration

All eVi state lives under `~/.evi/` (Windows: `%USERPROFILE%\.evi\`). The
primary file is `config.toml`. First launch writes a default. Hand-edit it
when you need to change something; nothing else holds machine-wide state.

## Primary file — `config.toml`

```toml
[llm]
backend         = "lmstudio"                  # lmstudio | ollama | llamacpp | openai_compat
base_url        = "http://localhost:1234/v1"  # default per-backend; auto-bumped by `evi models backend`
api_key         = "lm-studio"                 # ignored by local servers; OpenAI SDK requires a value
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
| `EVI_PYTHON`       | Tauri desktop: Python interpreter to spawn (default `py -3.11`) |
| `EVI_REPO_ROOT`    | Tauri desktop: pin the repo root rather than auto-detecting    |
| `EVI_REMOTE_URL`   | Tauri desktop: thin-client mode — skip spawn, navigate to URL  |
