# Troubleshooting

Common problems, in roughly the order new users hit them.

## Safe mode — is a customization breaking things?

If eVi behaves oddly and you suspect a project `EVI.md`, skill, plugin, hook,
MCP server, memory, guardrail, or custom command, run with **`--safe-mode`** (or
`EVI_SAFE_MODE=1`) to load **none** of them:

```bash
evi --safe-mode chat        # clean baseline — no customizations loaded
evi --safe-mode web         # same clean boot for the web / desktop UI
```

The REPL header shows `SAFE MODE (customizations off)`. If the problem disappears
in safe mode, re-enable customizations one at a time to find the culprit.

Safe mode is carried in the environment (`EVI_SAFE_MODE=1`), so it applies to the
whole process tree — including the desktop app's sidecar and any subagents — and
is never written to your config. It switches off:

| | |
|---|---|
| Project context | `EVI.md` / `AGENTS.md` — including on a later `/cd` |
| Config overlays | your profile **and** a repo's `.evi.toml` — safe mode is *stock config* |
| Memory | the prompt-side index **and** the `remember`/`recall` tools |
| Skills | the catalog **and** `invoke_skill` / `list_skills` |
| Hooks | `~/.evi/hooks.toml` + every plugin's hooks |
| Guardrails | `~/.evi/guardrails.toml` |
| Commands | user + plugin slash commands (and their tab-completion) |
| Plugins | `bin/` PATH wiring |
| MCP | external servers are never spawned |
| Output style | a user `~/.evi/styles/<name>.md` can override a builtin — skipped |
| Keybindings | `~/.evi/keybindings.toml` — stock keys |
| Transcripts | not written |

Inspection commands still work normally, so you can find and fix the culprit —
`evi plugin list`, `evi skill list` and `evi config show` are unaffected.

## "evi: command not found" after `pip install -e .`

The venv isn't on PATH. Either activate it
(`.venv\Scripts\Activate.ps1` on Windows,
`source .venv/bin/activate` on Linux/macOS) or invoke directly:

```bash
.venv\Scripts\evi.exe --help
```

## "LLM request failed: APIConnectionError"

eVi can't reach the backend. Check, in order:

1. Is the backend actually running?
   - LM Studio: open the app, Developer tab, **Start Server**.
   - Ollama: `ollama serve` or check `sudo systemctl status ollama`.
   - llama.cpp: `llama-server -m <path>` must be running.
2. Is `[llm] base_url` correct? Check with `evi config show`.
3. Can you hit it manually? `curl http://localhost:1234/v1/models` for LM Studio,
   `curl http://localhost:11434/api/tags` for Ollama.
4. Are you behind a proxy / VPN that's blocking localhost? Check
   `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`.

## "The model can't seem to call tools — it just describes them"

The model isn't tool-capable, or the temperature is too high.

- Use a tool-tuned model: Qwen2.5 series (especially **14B+**), Hermes-3
  on Llama 3.1, Command-R. See [docs/models.md] in the README quick
  reference.
- Avoid Gemma 2/3 and Phi-4 — their tool calling is hit-or-miss.
- Lower `[llm] temperature` to 0.3–0.4. The default 0.7 makes
  tool-call JSON noisy.
- Watch the model's chat template: some GGUF builds have the tool prompt
  baked wrong. Try a different build from a known-good repacker
  (bartowski / unsloth on HF).

## "Permission denied" for every tool call (CLI)

Auto-approval might be off and you're sitting at an `approve? y/a (this call)
/ n / s (allow all this session)` prompt that hasn't been answered. Type `y`
(or `a`) to allow that call, or `s` and then `y` to allow every call for the
rest of the session. Then revisit `auto.auto_approve` in `config.toml` for a
lasting fix.

## "PERMISSION DENIED: … This run is unattended …"

A call that would have asked was refused because nothing could ask you: a
scheduled task, `evi run` or `evi batch` without `-y`, `evi team run`,
`evi dream`, an ultracode stage, a subagent with no chat to ask through, a
workflow or recipe the web app runs, or an SDK agent built without a
`permission_callback`. Such a run denies every call that would ask unless you
opted in (`[auto] unattended_policy = "deny"` is the default). For a scheduled
task, `evi schedule list` shows the refusal as `denied: <tools> (policy deny)`
and the log's header says `# policy: deny`.

To let the call run, from narrowest to widest:

- **Pre-approve that kind of call**: an `allow` rule for the tool
  (`rules = ["allow web_fetch"]`), a `trusted_dirs` / `trusted_domains` entry,
  or its category in `[auto] auto_approve`. Your own sessions then stop asking
  for it too.
- **For one run**: `evi run -y` and `evi batch -y` approve every call of that
  run, and a routine added with `--yes` does for its webhook.
- **For every unattended run you start**: set `unattended_policy = "allow"`
  under `[auto]` in `config.toml`.
- **In your own Python code**: pass `permission_callback=` or
  `unattended_policy="allow"` to `build_agent` (see "Permissions" in
  [sdk.md](sdk.md)).

None of these opens a remote-triggered run beyond what you pre-approved: a
federated or A2A task, a routine webhook without `yes` and a channel push with
`run: true` deny the call whatever `unattended_policy` or `[auto] mode` says.
A Telegram turn follows `[channels] telegram_auto_approve` (the denial names
`[auto] auto_approve` because the bot's own policy fills that list). Protected
writes, secret reads and commands the destructive-command guard flags stay
denied in every unattended run; see the next section. Background: [Unattended
runs](features/permissions-and-sandbox.md#unattended-runs).

## "PERMISSION DENIED: … writes the protected path …"

The call would write a protected path: an instruction, config, hook, task or CI
file (`EVI.md`, `AGENTS.md`, `.evi.toml`, `.evi/**`, `~/.evi/**`,
`.git/hooks/**`, `.github/workflows/**`, …) or a secret file. That asks in
every mode, `yolo` and `/auto on` included, and is denied when nothing can
answer (the scheduler, `evi run -y` in CI or a script with no terminal). If an
unattended run really should write one, add its exact entry to
`[auto] protected_paths_remove`, e.g. `protected_paths_remove = ["AGENTS.md"]`;
`evi doctor` lists the protected paths in force. A reason ending in `(after a cd eVi can't follow, so where it
lands is unknown)` means the shell line `cd`s somewhere eVi can't work out
(`cd $DIR`, a wildcard, a drive switch) before writing a relative path: retry
with an absolute path or without the `cd`. See [Permissions & sandbox →
Protected paths](features/permissions-and-sandbox.md#protected-paths).

`… reads the protected path …` is the same check for a secret (`.env`,
`*.pem`, an SSH key). Unattended, the read is denied even under `-y`, `yolo`
or `[auto] unattended_policy = "allow"`; only an explicit allow rule that names
it, such as `allow read_file *.env.example`, lets it through.

## "The … backend is a CLI agent that runs its own tools … won't use it for an unattended … run"

`[llm] backend` (or a `[models] *_backend`) is `codex`, `gemini`, `amp`,
`qwen` or `copilot`. Those CLIs run their own shell, file and web tools outside
eVi's permission checks, so a run nobody is watching (a scheduled task,
`evi run`, `evi batch`, `evi team run`, a subagent with no chat to ask, the web
app's workflow, recipe, routine, channel-push, ultracode and federation runs)
refuses them; the web app answers `409`. Point the setting the message names at
a local or API backend, or set `allow_cli_agent_unattended = true` under
`[llm]` to accept the risk. The Telegram channel refuses them even then.

## A repository's `.evi.toml` settings aren't applied

eVi applies a `.evi.toml` only in a folder you have trusted, and only the
version you trusted. The log says which case it is: `eVi: not applying project
config <path>: this folder is not trusted` or `… it is new or changed since you
trusted this folder`. Answer `y` at the prompt `evi chat` shows, or run
`evi trust` in that folder from your own terminal; `evi trust --status` shows
the state. `evi run`, `evi batch`, the web UI and the scheduler never ask. In
the web UI the settings come from the folder the server was started in, not a
session's working folder. The file is looked for only up to the repository's
top level, so a `.evi.toml`, `EVI.md` or `AGENTS.md` in a folder above the
repository doesn't apply inside it. See [Trusting a
folder](configuration.md#trusting-a-folder).

## "eVi: unknown settings in … config.toml"

A key in `config.toml` (or the active profile) isn't one this eVi knows: a
typo, or a setting from a newer version. It is ignored but kept in the file,
and eVi starts normally. Fix the spelling, or leave it if a newer eVi on
another machine shares the file.

## Chats compact early (or overflow) after switching models

eVi compacts a conversation at `compact_when_pct` of `[llm] context_size`.
The managed llama.cpp runtime sets that to its server's window (32K, 16K or
8K), and gives your own value back when you switch to another backend from the
web or desktop app. After a switch made with the CLI (`evi backend use`, …),
the old window stays until the next `evi web` or desktop start; start one, or
set `context_size` yourself. See [`context_size` and the managed llama.cpp
runtime](configuration.md#context_size-and-the-managed-llamacpp-runtime).

## Gemini backend: "Login with Google" no longer works

Since 2026-06-18 Google doesn't serve Gemini CLI to free, Google AI Pro or
Google AI Ultra "Login with Google" accounts. Set `GEMINI_API_KEY` (a key from
<https://aistudio.google.com/apikey>) or Vertex AI, then run `evi doctor`; see
the Gemini section of [configuration.md](configuration.md). A Gemini Code
Assist Standard / Enterprise login still works.

## ComfyUI image generation hangs or times out

The default 5-minute poll ceiling is generous, but a stuck workflow won't
get unstuck. Check:

1. ComfyUI itself is responsive: `curl http://localhost:8188/system_stats`.
2. The checkpoint named in `[comfy] default_checkpoint` exists in
   ComfyUI's models directory. If not, the workflow fails immediately —
   tail ComfyUI's terminal for the actual error.
3. The sampler/scheduler combo is valid (`euler` + `normal` is safe).

## MCP server says "spawned but no tools registered"

The server probably failed to start. Tail eVi's stderr or check
`~/.evi/logs/` for `MCP server <name> failed to start: …`. The reason names
the actual error, such as `HTTP 401 Unauthorized` (a remote server rejected
the `headers` you gave it) or `ConnectError: All connection attempts failed`
(nothing is listening at the `url`):

- `npx` is missing → install Node.js.
- The MCP server package isn't installable → try
  `npx -y <package>` manually to see the error.
- A remote (`transport: http`) server fails on every connect → upgrade eVi:
  older versions passed its headers in a way current mcp SDKs reject. The
  `[mcp]` extra now needs mcp 1.30 or newer.
- Server start needs a long handshake → bump
  `MCPBridge.run(timeout=…)` if you're hacking on the code.

Confirm what *did* register: `evi mcp list-tools`.

## Tauri desktop window stays on the loading shim

The bundled Python server didn't come up in 20 seconds. Common reasons:

- `py -3.13` not on PATH. Override with `EVI_PYTHON=python3.13`.
- `EVI_REPO_ROOT` couldn't be detected (binary not next to the repo).
  Set it explicitly.
- The web extras aren't installed: `pip install -e '.[web]'`.
- For thin-client mode, set `EVI_REMOTE_URL=http://server:8000` to skip
  the spawn entirely.

Tail the Tauri stderr to see what `py -3.13 -m uvicorn …` actually did.

## Scheduler / cron tasks aren't firing

- `apscheduler` not installed: `pip install 'evi-assistant[scheduler]'`.
- The scheduler isn't running anywhere. Pick one:
  - foreground daemon: `evi scheduler`
  - alongside web: `evi web` (lifespan starts the scheduler; not in safe mode)
- The cron expression is invalid: check
  `~/.evi/logs/` — failed jobs get logged with the parse error.
- The task is disabled: `evi schedule list` shows status; flip with
  `evi schedule enable <id>`.
- It fires but its status is `denied: …`: a tool it needed would have asked,
  and a scheduled run denies that by default; see ["PERMISSION DENIED: … This
  run is unattended …"](#permission-denied--this-run-is-unattended-).

## `evi dream` reports zero transcripts to review

You ran chat sessions before `tools.transcripts` was on, or the day-dir
under `~/.evi/transcripts/` doesn't exist yet. Confirm:

```bash
evi config show | grep transcripts
ls ~/.evi/transcripts/
```

Run a quick `evi chat`, exit, then `ls ~/.evi/transcripts/<today>/`.

## STT: "no input device" / "PortAudioError"

`sounddevice` couldn't open the default mic.

- Linux: install PortAudio (`sudo apt install libportaudio2`).
- Windows: confirm the mic isn't disabled in System → Sound. Privacy
  settings → Microphone → "Allow desktop apps" on.
- macOS: grant terminal Microphone permission in System Settings → Privacy.

## Computer-use clicks miss the target

Multi-monitor or HiDPI scaling. Get a known-good baseline:

```bash
# In eVi chat:
> take a screenshot and read the file path back to me
> use screen_size to tell me the resolution
```

Coordinates are pixels on the **primary** display. For non-primary
monitors or fractional scaling, you may need to compute offsets manually.

## Tests hang or fail with "git: unknown switch -b"

Your git is <2.28. Upgrade:

```powershell
winget install --id Git.Git -e
```

The worktree tests use `git -c init.defaultBranch=main init` as a
broader-compat fallback, so they should pass on git ≥2.5.

## "ImportError: cannot import name 'X' from 'evi.…'" after pulling new code

You're running the editable install but Python is caching old bytecode.
Either delete `__pycache__` dirs:

```bash
find . -name __pycache__ -exec rm -rf {} +
```

…or just `pip install -e .` again to refresh metadata.

## Where to dig deeper

- The session transcripts: `~/.evi/transcripts/<today>/<session>.jsonl`
- Tool/hook stdout via `~/.evi/logs/`
- Dream audit logs: `~/.evi/logs/dreams/`
- Scheduled task logs: `~/.evi/logs/scheduled/`
- LM Studio / Ollama have their own terminal output — keep them visible
  during dev.
