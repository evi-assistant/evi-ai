# Troubleshooting

Common problems, in roughly the order new users hit them.

## Safe mode — is a customization breaking things?

If eVi behaves oddly and you suspect a project `EVI.md`, skill, plugin, hook,
MCP server, memory, guardrail, or custom command, run with **`--safe-mode`** (or
`EVI_SAFE_MODE=1`) to load **none** of them:

```bash
evi --safe-mode chat        # clean baseline — no customizations loaded
```

The REPL header shows `SAFE MODE (customizations off)`. If the problem disappears
in safe mode, re-enable customizations one at a time to find the culprit.

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
2. Is `[llm] base_url` correct?
   ```bash
   evi config show
   ```
3. Can you hit it manually?
   ```bash
   curl http://localhost:1234/v1/models   # LM Studio
   curl http://localhost:11434/api/tags   # Ollama
   ```
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

Auto-approval might be off and you're sitting at a `(approve y/n/a)`
prompt that hasn't been answered. Type `a` to allow everything for the
session, then revisit `auto.auto_approve` in `config.toml` for a
persistent fix.

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
`~/.evi/logs/`:

- `npx` is missing → install Node.js.
- The MCP server package isn't installable → try
  `npx -y <package>` manually to see the error.
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
  - alongside web: `evi web` (lifespan starts the scheduler)
- The cron expression is invalid: check
  `~/.evi/logs/` — failed jobs get logged with the parse error.
- The task is disabled: `evi schedule list` shows status; flip with
  `evi schedule enable <id>`.

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
