# Development notes

For people working on Evi itself.

## Setting up

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1            # Windows PowerShell
# source .venv/bin/activate            # Linux / macOS
pip install -e '.[dev,web,mcp,scheduler,downloads,web-tools,stt,computer]'
```

The `stt` and `computer` extras pull native deps (PortAudio for
sounddevice, screen capture libs for pyautogui). If you're not actively
touching those areas, skip them.

## Test suite

```bash
pytest -q                         # ~12 s, 183 tests
pytest -q tests/test_agent.py     # one file
pytest -q -k "permission"         # by keyword
pytest --timeout=15               # ceiling per test
```

The suite avoids network calls and external processes by:

- Stubbing the OpenAI client with hand-rolled `_FakeClient` chunks
  (see `tests/test_agent.py`).
- Patching `httpx` with `MockTransport` for backend HTTP boundaries
  (`tests/test_backends.py`, `tests/test_websearch.py`,
  `tests/test_image_comfy.py`).
- Spawning real `git` for worktree tests, but they're skipped when
  `git` isn't on PATH.
- Stubbing the MCP `ClientSession` for hook-running and bridge tests.

## Linting

```bash
ruff check evi apps tests
ruff format evi apps tests        # if you want formatter behavior too
```

`pyproject.toml` sets `line-length = 100, target-version = "py311"`.

## Project layout

```
evi/                core library — no IO at import time
  agent.py           Agent class + Event types
  backends/          LM Studio / Ollama / llama.cpp / OpenAI-compat
  commands.py        ~/.evi/commands/ loader
  config.py          Config dataclasses + load/save + dir constants
  dream.py           memory consolidation engine
  downloads.py       HF GGUF puller
  hardware.py        NVIDIA + RAM detection
  hooks.py           hook config loader + runner
  llm/
    agent.py         re-exports from evi.agent (or holds the loop)
    client.py        make_client(settings) → OpenAI SDK
    subagent.py      run_subagent() + SUBAGENT_PROFILES dict
  mcp/
    bridge.py        async-to-sync bridge thread
    manager.py       MCPManager — boots stdio servers, registers tools
    servers.py       MCPServer dataclass + JSON loader
  memory.py          MemoryStore with .attic/ soft-delete
  profiles.py        partial TOML overlay merge
  project.py         find_project_file → ProjectContext
  recommend.py       tiered model picks per VRAM
  scheduled.py       ScheduledTask + TaskStore
  scheduler.py       Scheduler — APScheduler wrapper
  skills.py          SkillStore + minimal frontmatter parser
  tools/
    base.py          @tool decorator + REGISTRY + Tool dataclass
    code.py          run_python tool (subprocess)
    computer.py      pyautogui wrappers — screenshot/click/type/key/scroll
    fs.py            read_file/write_file/list_dir
    image_comfy.py   ComfyUI text2img
    memory.py        remember/recall/forget/list_memories
    skills.py        list_skills/invoke_skill
    subagent.py      delegate_explore/delegate_plan
    voice.py         speak_text/transcribe_microphone
    websearch.py     web_search/web_fetch
  transcripts.py     JSONL session log store
  util/streaming.py  legacy
  voice.py           TTS (platform CLIs) + STT (faster-whisper)
  worktree.py        git worktree wrapper
  apps/              frontends (shipped in the wheel)
    cli/main.py      Typer CLI — every public command
    web/server.py    FastAPI + SSE + permission flow
    web/static/      vanilla JS chat UI
desktop/             Tauri 2 project (NOT a Python package)
tests/               pytest — one file per module
docs/                this folder
scripts/             install + dev helpers
```

## Adding a tool

1. Create `evi/tools/<thing>.py`:

   ```python
   from evi.tools.base import tool

   @tool(
       description="What this tool does for the model.",
       category="thing",  # also: the config toggle name
   )
   def the_tool(arg1: str, arg2: int = 5) -> str:
       ...
       return "result"
   ```

2. Add the category to `ToolToggles` in `evi/config.py`.

3. Import for side effect in:
   - `evi/apps/cli/main.py`
   - `evi/apps/web/server.py`

4. Decide whether the category belongs in `auto.auto_approve`. If it does
   anything irreversible or network-y, leave it out.

5. Test pattern:

   ```python
   from evi.tools.base import REGISTRY
   import evi.tools.thing  # noqa: F401  register

   def test_the_tool() -> None:
       out = REGISTRY["the_tool"].call(json.dumps({"arg1": "x"}))
       assert ...
   ```

## Adding a backend

`evi/backends/__init__.py` registers backends in `KNOWN_BACKENDS`. Each
subclass of `Backend`:

- `name: str` class attribute
- `__init__(base_url, api_key, request_timeout)` constructor
- `make_client() -> OpenAI` — required
- Override `list_models / model_info / pull_model / delete_model /
  supports_pull` as makes sense for the backend.

Add a default port to `_DEFAULT_URLS` in `factory.py`. Add tests in
`tests/test_backends.py` (use `httpx.MockTransport`).

## Adding a slash command

Built-in commands live in `evi/apps/cli/main.py` and `evi/apps/web/server.py`.
The two paths are intentionally separate:

- CLI: `_handle_<name>(agent, args, cmd_store) -> SlashResult`. Register
  in the `_BUILTINS` dict.
- Web: `_handle_slash` is one switch statement returning `_SlashOutcome`.
  Mirror your CLI handler there.

User-defined commands need no code — they're just markdown files in
`~/.evi/commands/`.

## Adding a subagent profile

Add an entry to `SUBAGENT_PROFILES` in `evi/llm/subagent.py`:

```python
SUBAGENT_PROFILES["reviewer"] = {
    "system_prompt": "You review pull requests for correctness…",
    "tool_categories": ("fs",),
}
```

Then add a delegate tool in `evi/tools/subagent.py` that wraps
`run_subagent(...)` with that profile.

## Common gotchas

- **Agent.history vs transcripts**: `history` is in-process, dies with
  the Agent. Transcripts are append-only JSONL on disk. Don't conflate
  them.
- **Tool registration is import-side-effect-driven**. Forgetting to
  import the module in the CLI / web entry points means the tool won't
  appear in REGISTRY. The same module imported via tests works because
  the test imports it.
- **`config.py` constants**: Lots of `~/.evi/<thing>` paths live there.
  Always import the constant, never hard-code `Path.home() / ".evi" /
  ...`.
- **Test isolation**: Several modules cache state at module level
  (`memory_tools._store`, `voice_mod._WHISPER_MODEL`). Tests that
  monkeypatch their roots must do so via the module attribute, not the
  globals.
- **MCP bridge ownership**: `MCPManager` constructs its own bridge by
  default. If you pass one in (tests do), `manager.stop()` doesn't stop
  the bridge — the caller is responsible.

## Bumping a phase

When you add a meaningful chunk of work:

1. Add tests covering the new code paths.
2. Update `README.md` "What's built" table.
3. Update `docs/architecture.md` if you added a new subsystem.
4. Add a memory entry in
   `~/.claude/projects/C--evi/memory/project_evi.md` so the next
   Claude Code session knows what changed.

## Releasing

Nothing automated yet. Planned: `scripts/release.sh` that runs
`pytest && ruff check && python -m build`, tags the commit, attaches
wheels.
