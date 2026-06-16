# Tool reference

Every tool eVi ships with, grouped by category. Each tool is enabled via
`tools.<category> = true` in `~/.evi/config.toml`. Whether the agent can
invoke it *without prompting* depends on `auto.auto_approve` (see
[configuration.md](configuration.md#tools)).

Add your own tool by decorating a function with `@tool(...)` and importing
the module from `evi/apps/cli/main.py` + `evi/apps/web/server.py` — see
[development.md](development.md#adding-a-tool).

## `fs` — filesystem (default on)

| Tool          | What it does                                                |
|---------------|-------------------------------------------------------------|
| `read_file`   | Read a UTF-8 text file. 256 KB cap for whole-file reads; pass `offset` (1-based start line) + `limit` (max lines) to stream a slice of a larger file. Errors string-returned. |
| `write_file`  | Overwrite or create a file with UTF-8 text. Creates parents. |
| `edit_file`   | Surgical edit: replace an exact `old_string` with `new_string` (once, or `replace_all`). Preferred over `write_file` for small changes — cheaper and safer than a full rewrite. |
| `list_dir`    | List entries in a directory, marked `D`/`F` per kind.        |
| `find_files`  | Glob by name (e.g. `**/*.py`). Skips noise dirs (`.git`, `node_modules`, `.venv`, …). One path per line. |
| `search_files`| Literal **regex grep** over file contents (`path:line: text`). Narrow with `glob` (e.g. `*.py`) + `ignore_case`. For meaning-based search use `find_in_project` (the `index` category). |

Safety: 256 KB read cap prevents loading huge blobs into context. There's
no implicit traversal check — combine with a hook for that.

## `code` — code execution (default on)

| Tool         | What it does                                                |
|--------------|-------------------------------------------------------------|
| `run_python` | Run a Python 3 snippet in a subprocess with a 10s timeout. Combined stdout+stderr, capped at 16 KB. Fresh temp cwd. |

**Not a sandbox.** Acceptable for trusted local use. For untrusted code,
wrap with a Docker hook or disable the category.

## `memory` — long-term memory (default on)

| Tool              | What it does                                                |
|-------------------|-------------------------------------------------------------|
| `remember`        | Save markdown under `~/.evi/memory/<name>.md`. Overwrites.  |
| `recall`          | Return the full body of a stored memory.                    |
| `forget`          | **Soft delete** — move the file to `.attic/`. Recoverable.  |
| `list_memories`   | JSON of `[{name, summary}, …]` — same data agents see in the system prompt. |

The memory index is regenerated on every write/delete and appears in the
agent's system prompt automatically. The dreaming engine relies on
soft-delete to avoid permanently losing data.

## `skills` — saved instruction packets (default on)

| Tool          | What it does                                                |
|---------------|-------------------------------------------------------------|
| `list_skills` | JSON of `[{name, description}, …]`.                         |
| `invoke_skill`| Return the full SKILL.md body so the agent can follow it.   |

Skills live in `~/.evi/skills/<name>/SKILL.md` with optional YAML
frontmatter (`name`, `description`). Example skill in `examples/skills/`.

## `image` — ComfyUI image generation (default OFF)

| Tool             | What it does                                                |
|------------------|-------------------------------------------------------------|
| `generate_image` | text2img via ComfyUI. Submits workflow → polls → fetches → saves to `~/.evi/images/`. Returns JSON `{prompt_id, seed, paths}`. |

Requires ComfyUI running at `comfy.base_url` (default `http://localhost:8188`).
Workflow params: `prompt`, `negative_prompt`, `width`, `height`, `steps`,
`seed`, `cfg`, `sampler`, `scheduler`.

## `subagent` — delegated sub-conversations (default OFF)

| Tool                | What it does                                                |
|---------------------|-------------------------------------------------------------|
| `delegate_explore`  | Spawn a read-only investigation subagent (fs only).         |
| `delegate_plan`     | Spawn a planning subagent (no tools; thinks then returns).  |

Each spawns a fresh `Agent` with the dream-style scoped tool list, runs
to completion, returns the final text + a short trace. Useful for keeping
the main conversation focused.

## `web` — web search + fetch (default OFF)

| Tool         | What it does                                                |
|--------------|-------------------------------------------------------------|
| `web_search` | DuckDuckGo search via `duckduckgo_search`. Returns JSON `[{title, url, snippet}, …]`. |
| `web_fetch`  | Download a URL (http/https only) and return extracted text. 1 MB raw cap, 16 KB output cap. |

Requires `pip install 'evi-assistant[web-tools]'` (gets `duckduckgo_search` and
`beautifulsoup4`).

## `voice` — TTS + STT (default OFF)

| Tool                     | What it does                                                |
|--------------------------|-------------------------------------------------------------|
| `speak_text`             | Speak text via local TTS (PowerShell SAPI / `say` / `espeak`). Returns immediately. |
| `transcribe_microphone`  | Record `duration` seconds and transcribe via faster-whisper. |

`speak` needs no Python deps — uses platform CLIs.
`transcribe_microphone` needs `pip install 'evi-assistant[stt]'` (faster-whisper +
sounddevice + numpy).

## `computer` — mouse / keyboard / screen (default OFF, NEVER auto-approved)

| Tool          | What it does                                                |
|---------------|-------------------------------------------------------------|
| `screenshot`  | Capture primary display, save under `~/.evi/screenshots/`.  |
| `click`       | Move mouse to (x, y) and click. `button` ∈ `{left, right, middle}`. |
| `move`        | Move mouse to (x, y) without clicking.                      |
| `type_text`   | Type text at the focused element (per-keystroke pause).     |
| `key`         | Press a named key (`enter`, `tab`, `f5`, …).                |
| `scroll`      | Scroll the wheel. Positive = up.                            |
| `screen_size` | Return primary display dimensions as JSON.                  |

**Safety:**
- Category never lives in `auto.auto_approve` — every action prompts the
  human.
- `pyautogui.FAILSAFE = True` — slam the mouse into a corner to abort.
- Requires `pip install 'evi-assistant[computer]'` (pyautogui + pillow).

## `sqlite` — SQLite queries (default OFF)

| Tool             | What it does                                                |
|------------------|-------------------------------------------------------------|
| `sqlite_schema`  | Return tables + columns for a sqlite file as JSON.          |
| `sqlite_query`   | Run a read-only SELECT (rejects DDL/DML). Returns rows JSON. |

## `pdf` — PDF reading (default OFF)

| Tool       | What it does                                                  |
|------------|---------------------------------------------------------------|
| `read_pdf` | Extract text from a PDF. Page-range optional. Caps total output. |

Requires `pip install 'evi-assistant[pdf]'` (PyMuPDF).

## `mcp` — Model Context Protocol (default OFF)

When enabled, `~/.evi/mcp.json` is loaded and each MCP server's tools
appear in the registry as `<server>.<tool>`. The catalog is
*per-installation* — see `evi mcp list-tools` for whatever you've wired up.

## `transcripts` — session logs (default on, no LLM-facing tool)

Background concern, not a callable tool. When `tools.transcripts = true`,
`Agent` writes every message to `~/.evi/transcripts/<day>/<session>.jsonl`.
This feeds `evi dream` and `evi sessions list/resume`.

---

## Quick safety table

| Category    | Default | In default auto_approve | Notes |
|-------------|---------|-------------------------|-------|
| `fs`        | on      | ✅                       | Read/write to anywhere user can access |
| `code`      | on      | ✅                       | Not sandboxed; subprocess timeout 10s |
| `memory`    | on      | ✅                       | Soft-delete keeps data recoverable |
| `skills`    | on      | ✅                       | Read-only on shipped skill files |
| `image`     | off     | ✅                       | Network: ComfyUI on localhost only |
| `subagent`  | off     | ❌                       | Spawns more LLM calls |
| `web`       | off     | ❌                       | Real network |
| `voice`     | off     | ❌                       | Records mic / speaks audio |
| `computer`  | off     | ❌ (never)               | Drives the desktop |
| `sqlite`    | off     | ❌                       | DB content may be sensitive |
| `pdf`       | off     | ❌                       | File reads |
| `mcp`       | off     | ❌                       | Whatever the MCP server does |

---

## Tool search at scale

With many tools enabled (especially several MCP servers), sending every tool
schema on every turn bloats the context. Turn on **deferred tool search**:

```toml
[tools]
tool_search = true            # off by default
tool_search_threshold = 30    # only defers once the toolset exceeds this
```

When active, the `fs` and `memory` tools stay loaded and the long tail moves
behind a single `search_tools` meta-tool. The model calls
`search_tools("git commit")` to surface matching tools; they're added to the
live toolset and become callable on the next step — still subject to the normal
per-category permission gating (`search_tools` only *exposes* tools, never runs
them). Below the threshold it's a no-op, so it's safe to leave on.
