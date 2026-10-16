# Development notes

For people working on eVi itself.

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

`scripts/dev-bootstrap.sh` / `scripts\dev-bootstrap.ps1` do the same with a
venv outside the checkout, and `evi doctor --dev` checks the result (venv, git
`safe.directory`, test and lint tools, line endings) and prints fix commands.
Both are described in [self-build.md](self-build.md).

## Test suite

```bash
pytest -q                         # the unit suite (pytest --collect-only -q for the count)
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
ruff check evi tests scripts .github/actions   # what CI runs
ruff format evi tests             # if you want formatter behavior too
```

`pyproject.toml` sets `line-length = 100, target-version = "py312"`.

A change that adds a CLI command or a web route must also change something
under `docs/`, or CI's docs-freshness check fails it; see
[TESTING.md → CI gates](../TESTING.md#ci-gates) for that and the other checks.

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
    web/static/      the UI: index.html (markup), app.js (code), vendor/ (pinned libraries)
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

## The web UI and its libraries

The web UI, which the desktop app shows too, is plain files in
`evi/apps/web/static/`:

```
index.html     markup and styles, plus the Content-Security-Policy <meta>
app.js         all of the UI's code
manifest.json  web app manifest
vendor/        marked, DOMPurify, highlight.js, mermaid (pinned; never edit)
```

- `index.html` loads `app.js` last, as a classic script just before
  `</body>` (no `defer`, `async` or `type="module"`). It runs after the
  vendored libraries in `<head>` and once the markup above it is parsed, and
  its top-level functions and constants are globals: the desktop menu bridge
  (`window.eviUI.handleMenu`) and the tests call them by name.
- `index.html` holds no code. The page's CSP is `script-src 'self'` with no
  hashes, so the browser refuses an inline `<script>` or an `on*` attribute,
  and `python scripts/vendor_ui_libs.py --check` fails on one. Put code in
  `app.js` and attach listeners there.
- Editing `app.js` never touches the CSP. The server sends the `<meta>`
  policy as the page's header, with `frame-ancestors 'none'` added (see
  [security.md](security.md#response-headers)).
- Model output is rendered only through `renderMarkdownInto`, which runs
  marked and then DOMPurify (`sanitizeMarkdown`); never put model text into
  `innerHTML`. What the sanitizer keeps is in
  [security.md](security.md#rendering-model-output).
- A UI change ships a Playwright test in `tests/e2e/`;
  `tests/e2e/test_ui_xss.py` holds the rendering-safety corpus.

`scripts/vendor_ui_libs.py` maintains `vendor/` and the pins that load it:

```bash
python scripts/vendor_ui_libs.py --check      # offline: vendored files, SRI pins, page CSP
python scripts/vendor_ui_libs.py --sync-html  # rewrite the SRI pins in index.html and app.js
python scripts/vendor_ui_libs.py              # fetch the pinned releases, verify, write vendor/
python scripts/vendor_ui_libs.py --repin      # after a version bump: fetch, write, print new pins
```

- `--check` verifies, offline, that:
  - every file in `vendor/` matches its sha384 pin, and nothing unpinned is
    there;
  - the SRI pins match: the `integrity` of the `<script>` and `<link>` tags
    in `index.html`, and the `src: '/static/vendor/…', integrity: '…'` pairs
    in `app.js` (mermaid, which `loadMermaid()` loads on demand);
  - each vendored script and stylesheet, and `app.js`, is loaded exactly
    once;
  - `index.html` has no inline code;
  - the CSP `<meta>` is in the form the server reads and comes before every
    `<script>` and `<link>`;
  - its `script-src` is `'self'` alone, with no `frame-ancestors`.

  `tests/test_ui_render_safety.py` runs the same check.
- `--sync-html` only rewrites the SRI pins in `index.html` and `app.js`, and
  drops any leftover hash from `script-src`. When they are in step it changes
  nothing.
- With no flag, the script downloads each library's exact npm tarball. It
  checks the tarball against the pinned `dist.integrity` (sha512) before
  reading anything, reads only the listed members (by exact name, regular
  files only), and refuses to write a file that differs from its pin. Nothing
  downloaded is executed or extracted by `tarfile`.
- To move a library to a new release, set its `version` and `integrity` in
  `PACKAGES`, using the registry's `dist.integrity` from
  `https://registry.npmjs.org/<name>/<version>`. Then run `--repin`, paste
  the printed `FILES` block over the one in the script, run `--sync-html`,
  and run the UI tests.
- `highlight/highlight.min.js` is derived: highlight.js's prebuilt common
  bundle followed by every other prebuilt language file.
- `vendor/.gitattributes` marks the files `binary`, so git never rewrites
  their line endings (the pins are hashes of the bytes). `pyproject.toml`
  ships them as package data (`static/vendor/*/*`).
- Exit codes: 0 ok, 1 a check failed or a pin does not match, 2 usage or
  network error.

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

## Bumping llama.cpp or the model catalog

The managed runtime downloads a `llama-server` build and catalog GGUFs, then
runs and loads them, so every download is checked against a sha256 pinned in
the repo: the generated `_ASSET_SHA256` block in
`evi/runtime/llamacpp_runtime.py` (one pin per release asset of
`LLAMACPP_VERSION`), and a `revision` (HuggingFace commit) plus `sha256` on
each entry of `evi/data/runtime_models.json`. A download without a pin is
never made.

After bumping `LLAMACPP_VERSION`, adding an asset table, or adding a catalog
model, re-pin and commit the result:

```bash
python scripts/pin_runtime_hashes.py                  # pin new assets and new models
python scripts/pin_runtime_hashes.py --check          # offline: every download is pinned
python scripts/pin_runtime_hashes.py --verify         # online: the pins still match upstream
```

- The script reads only metadata (GitHub's published asset digests, the
  HuggingFace revision API); it never downloads the files. Set `GITHUB_TOKEN`
  to avoid GitHub's anonymous rate limit.
- Models keep their pinned commit; `--models-latest` moves every model to its
  repo's current `main`. `--only runtime|models` writes one half.
- An asset that is already pinned is never silently re-pinned: a different
  upstream digest under the same name means the release asset was replaced,
  so the run changes nothing and exits 1. Pass `--repin-runtime` only once
  you have confirmed the replacement is genuine.
- After editing `runtime_models.json` by hand, run `--format` (offline) to put
  it back in the layout the script writes; a unit test checks that layout.
- Exit codes: 0 ok, 1 missing, stale or changed pins, 2 usage or network
  error. The unit suite fails while any asset or catalog model is unpinned
  (`tests/test_runtime_integrity.py`), so CI catches a forgotten re-pin.

What users see when a check fails:

- `<file> failed verification (sha256 …, expected …); the download was
  discarded.` — the bytes didn't match the pin. Nothing is extracted or run;
  retrying usually fixes a transfer error, and a repeat means the file changed
  upstream or in transit.
- `No pinned sha256 for <file>, so eVi won't download it unverified.` — the
  build is missing a pin (a packaging bug: re-pin and release).
- `Archive member … escapes …; refusing to extract.` — an archive tried to
  write outside its folder.

A runtime install is all-or-nothing: archives are verified and extracted in a
private staging folder and moved into place only when complete. A catalog GGUF
already on disk counts as installed only if its sha256 matches the pin (hashed
once, cached beside it in `<file>.sha256`), so the first start after this
change hashes each installed catalog model once.

## Releasing

See [releasing.md](releasing.md): a `v*` tag publishes to PyPI, the desktop
release and GHCR, gated on the browser e2e suite and a CHANGELOG entry for the
version.
