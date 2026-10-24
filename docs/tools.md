# Tool reference

Every tool eVi ships with, grouped by category. Each tool is enabled via
`tools.<category> = true` in `~/.evi/config.toml`. Whether the agent can
invoke it *without prompting* depends on `auto.auto_approve` (see
[configuration.md](configuration.md#tools)), matched against what each call
does rather than the tool's category: a `code` tool's call can count as
`code_read`, `code` or `shell` (see [Permissions & sandbox → What a call
counts as](features/permissions-and-sandbox.md#what-a-call-counts-as)).

Add your own tool by decorating a function with `@tool(...)` and importing
the module from `evi/apps/cli/main.py` + `evi/apps/web/server.py` — see
[development.md](development.md#adding-a-tool). A tool that needs to know
which session called it (its id, working folder, turn, or a multi-user
login's data folder) declares a `ctx` parameter and receives a `ToolContext`;
`ctx` never appears in the schema the model sees. See
[sdk.md](sdk.md#the-calling-session-ctx).

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

`find_files`, `search_files`, `list_dir` and the project index also skip what
the repository's `.gitignore` and `.eviignore` ignore (`.eviignore` uses the
same syntax and can re-include with `!`); `list_dir` ends with a line saying
how many entries it hid. Pointing a tool at an ignored file or folder still
reads it: an explicit request wins. `edit_file`, `apply_patch` and slice reads
keep a file's own line endings (CRLF stays CRLF), and parallel writes to one
file wait for each other. Writes are atomic (a temp file, then `os.replace`).
When `edit_file`'s `old_string` isn't found exactly, it tries the same text
ignoring each line's surrounding whitespace and applies it on a unique match,
re-indented to the file; a re-sent edit that is already applied succeeds; a
miss shows the closest lines and an ambiguous match lists each line number.
In a chat or run, `write_file` overwrites an existing file only after the
model has read it with `read_file` and while it is unchanged since (same size
and modification time); otherwise it refuses, shows the file's first lines and
says to read it or use `edit_file`. Files the model created, wrote or edited
itself count as read; outside an agent nothing is checked.

Before a call runs, eVi fits its arguments to the tool once (`Tool.prepare`,
`evi/tools/repair.py`), and the permission check, the policy, hooks and the
tool all see the result: lenient JSON (single quotes, `True`/`None`, raw
newlines in strings), values converted to the schema's type where that is
unambiguous (`"5"` to 5, `"true"` to true, a lone value to a list),
`file_path`, `filepath`, `filename` and `file` read as `path` and `cmd` as
`command`, and arguments the tool doesn't take dropped. The tool's result
then ends with `(eVi repaired this call: ...)`. Arguments cut off
mid-object are closed only for a read-only tool. A call that can't be used
is answered with the tool's arguments and an example; an unknown tool with
the close names (or `search_tools`, or the list of tools). A reply that
starts with a call written as `<tool_call>` or `<function=...>` text runs it.

A tool result may fill about 15% of the context window (between 2,000 and
60,000 characters; 16,000 when the window is unknown). A longer one enters the
conversation as its head and its tail (more tail for command and test output,
where the error is) with a line saying how much was cut, and the whole of it
goes to a spill file, `<eVi home>/tool-output/<session>/<call>.txt` (secrets
redacted, readable by you only), which the model can page with `read_file`'s
`offset` and `limit`; under the implementer policy the session may read that
folder. Spill files are deleted at startup after `[tools]
cleanup_period_days`, or after 14 days when that is 0 (`evi/tool_output.py`).

The output of a tool marked untrusted (`web_search`, `web_fetch`, the
calendar tools and every MCP tool: text someone outside the conversation
wrote) reaches the model inside `<untrusted source="<tool>">` …
`</untrusted>`, with any look-alike tag in the text defused so it can't close
the fence early, and the system prompt carries one line saying fenced text is
information, never instructions (`evi/untrusted.py`). A plugin or SDK tool
opts in with `@tool(..., trust="untrusted")`.

## `code` — code execution and reading (default on)

| Tool             | What it does                                                | Checked as |
|------------------|-------------------------------------------------------------|------------|
| `run_python`     | Run a Python 3 snippet in a subprocess with a 10s timeout. Combined stdout+stderr, capped at 16 KB. Fresh temp cwd. | `code`; `code_read` while the OS sandbox is active |
| `python_symbols` | Outline a Python file from its AST: functions (with args), classes (with methods) and imports. | `code_read` |
| `check_file`     | Run the installed linter for a file's type (ruff or pyflakes, eslint, `go vet`, `cargo clippy`) and return its diagnostics. | `code_read` for `.py` and file types with no linter; `code` for `.js`/`.jsx`/`.ts`/`.tsx`/`.go`/`.rs`, whose linters run project code |
| `monitor`        | Watch for a bounded window (≤300s): `kind=file` tails NEW lines of a log; `kind=command` runs a shell command and collects output. Returns early when an `until` regex matches — for babysitting a build/CI run or tailing a log. | `code_read` for a file; `shell` for a command |

The default `auto_approve` list holds `code_read`, not `code`: reading code
runs unasked, running it asks. `monitor` with `kind=command` is approved
exactly like `run_command`, runs only in a session that offers the shell
tools (`[tools] shell = true`, or code mode), and runs under the OS sandbox
when `[tools] sandbox = true`. The file `monitor` tails counts as its path
argument, so tailing a protected secret (`.env`, a key) asks.

**Not a sandbox** unless `[tools] sandbox = true` and a sandboxer is present
(see [Permissions & sandbox → The sandbox](features/permissions-and-sandbox.md#the-sandbox)).
Acceptable for trusted local use. For untrusted code, wrap with a Docker hook
or disable the category.

## `memory` — long-term memory (default on)

| Tool              | What it does                                                |
|-------------------|-------------------------------------------------------------|
| `remember`        | Save markdown as `<name>.md` in the session's memory store, with optional `tags`. Overwrites. |
| `recall`          | Return the full body of a stored memory.                    |
| `forget`          | **Soft delete** — move the file to `.attic/`. Recoverable.  |
| `list_memories`   | JSON of `[{name, summary, tags}, …]` — same data agents see in the system prompt. |
| `recall_by_tag`   | JSON of the memories carrying a tag.                        |

The session's memory store is the one its system prompt's index lists:
`~/.evi/memory/` by default, the agent's own `memory_root` for an SDK agent
built with one, and `~/.evi/users/<name>/memory/` for a multi-user web
login (and the subagents its chats start). A call made outside an agent
(`evi mcp serve`) uses `~/.evi/memory/`.

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
| `web_search` | DuckDuckGo search via `ddgs`. Returns JSON `[{title, url, snippet}, …]`. |
| `web_fetch`  | Download a URL (http/https only) and return extracted text. Public addresses only unless the host is in `[auto] trusted_domains`; at most 5 redirects, each re-checked; 1 MB body cap while streaming, 60 s per fetch, 16 KB output cap. |

Requires `pip install 'evi-assistant[web-tools]'` (gets `ddgs` and
`beautifulsoup4`).

`web_fetch` takes its URL from the model, so it guards where it connects:

- **No private addresses** unless the host is listed in `[auto]
  trusted_domains`: loopback, LAN (RFC 1918), carrier-grade NAT, link-local,
  IPv6 unique-local / site-local, multicast, reserved ranges and cloud metadata
  endpoints are refused, including their IPv6-embedded forms. Every address the
  host resolves to is checked, and eVi connects to that checked address. A
  Home Assistant or SearXNG page on your LAN needs its host (or IP) in
  `trusted_domains`.
- **Never eVi's own API** on a loopback address (port 8473), trusted or not.
- **Redirects**: at most 5, each re-checked against your permission settings.
  A redirect to another host is not followed when the first URL was
  pre-approved by `trusted_domains` or an allow rule (the model is told to
  fetch the new URL itself, which goes through the normal check), and a
  redirect from a public address never leads to a private one.
- **Size and time**: the body is capped at 1 MB as it streams (a compressed
  body can't inflate past it; at most 4 MB of raw data is read), 30 s per
  network step and 60 s for the whole fetch. An HTTP error status returns
  `ERROR: fetch failed: HTTP <status> for <url>` without reading the body.
- **No proxy**: `HTTP(S)_PROXY` is not used, because the guard has to see the
  address it connects to; on a proxy-only network `web_fetch` fails, and the
  error says so.

A refusal returns `ERROR: blocked: <reason>`. Details: [Permissions &
sandbox](features/permissions-and-sandbox.md#what-web_fetch-will-fetch).

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
| `code`      | on      | reading code only (`code_read`) | Running code asks unless sandboxed; subprocess timeout 10s |
| `shell`     | off     | ❌                       | `run_command`; `monitor` commands count as `shell` too |
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
