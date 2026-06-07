# Changelog

All notable user-visible changes to Evi. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.24.0] — 2026-06-07

### Added — Phase 53: Evi as an MCP server (`mcp-server-publish`)

Evi has always been an MCP *client*; now it can run as an MCP *server* too, so
other agents (Claude Desktop, Cursor, Cline, Continue) can reach into Evi's
tools. This flips the integration story — instead of bridging into Evi from
each app, the app's existing MCP client connects to Evi.

- **`evi mcp serve`** — runs Evi as an MCP server over stdio, exposing a
  curated set of tools (default categories: `memory, index, calendar, git`;
  widen with `--categories`). Each MCP tool is a thin wrapper over the existing
  `evi.tools.base.REGISTRY` entry — same name, description, and JSON-schema, so
  there's one source of truth. Shell/computer/code-write are **not** exposed by
  default.
- **`evi mcp serve-config`** — prints a ready-to-paste `mcpServers` config
  snippet for Claude Desktop / Cursor.
- **`python -m evi`** now works (new `evi/__main__.py`) — the portable way an
  MCP client spawns the server.
- New `evi/mcp/publish.py` (`build_server`, `selected_tools`, `dispatch`).
  Verified end-to-end: a real MCP client initializes the server, lists tools,
  and calls one (input-schema validation enforced).

### Added — Phase 54: MCP server follow-ups (resources, prompts, HTTP, auth)

Builds on Phase 53's `evi mcp serve`:

- **Resources** — your long-term memory entries are exposed as MCP resources
  (`evi://memory/<name>`); clients can list + read them.
- **Prompts** — your saved slash-command templates (`~/.evi/commands/*.md`)
  are exposed as MCP prompts (with an optional `args` argument substituted into
  `{args}`).
- **Streamable HTTP transport** — `evi mcp serve --http [--host --port]` for
  remote clients (default stays stdio). `--token` gates it with a bearer token
  (constant-time compare); serving non-localhost without one warns.
- **Per-tool allow-list** — `--tools a,b,c` narrows the exposed set to exact
  tool names within the chosen `--categories`.
- Verified end-to-end: stdio lists tools/resources/prompts; HTTP rejects
  missing/bad tokens (401) and admits the right one.

### Added — Phase 55: opt-in OpenAI Responses API path

The agent loop can now talk the newer **Responses API** as an opt-in, without
disturbing local-first defaults.

- New **`[llm] api`** setting (`"chat"` default | `"responses"`), env override
  `EVI_LLM_API`. `"chat"` (Chat Completions) stays the default and the only
  shape Evi's local backends (LM Studio/Ollama/llama.cpp) support; `"responses"`
  is for endpoints that implement it (e.g. OpenAI cloud).
- New `evi/llm/responses.py`: chat↔responses request conversion (messages →
  `input` incl. tool-call/tool-result round-trips; tools flattened) and a stream
  **adapter** that re-emits Responses events as Chat-Completion-shaped chunks —
  so the large streaming/tool loop in `agent.py` is reused unchanged (one
  branch at the API call).
- **Not a migration:** with the default `"chat"`, nothing changes for existing
  users. The converters + adapter are unit-tested against the SDK's event
  shapes but NOT verified against a live Responses endpoint (no cloud in CI) —
  treat first real use as the integration test.

### Changed — PyPI distribution renamed to `evi-assistant`

The intended `evi-ai` name is taken on PyPI by an unrelated project, so the
distribution is now **`evi-assistant`** (`pip install evi-assistant`). The
import package and CLI are unchanged (`import evi`, run `evi`) — only the
install name, the in-app install hints, and the self-updater's target moved.
Publishing still requires PyPI Trusted Publishing configured for the new name.

## [0.23.0] — 2026-06-07

Phases 49–52 — supply-chain hygiene, a frictionless first run, desktop
auto-update, and opt-in crash reporting.

### Added — Phase 52: opt-in crash reporting

Privacy-first error reporting, **OFF by default and inert until configured**.

- **`evi/reporting.py`** — a swappable `Reporter` seam (`NullReporter` default;
  `SentryReporter` via the optional `sentry-sdk`) plus a shared **scrubber**
  applied to every event: rewrites home dir → `<HOME>` + username → `<USER>`,
  redacts API-key/token patterns, and **drops stack-frame locals, env,
  request/headers/cookies** — critical for an AI app where those can carry
  prompt text or keys. Anonymises `server_name`, drops the user/IP block.
- **`[telemetry]` config** (`crash_reports` off, `dsn`, `backend`) with env
  overrides `EVI_CRASH_REPORTS` / `EVI_TELEMETRY_DSN`. Point `dsn` at a
  self-hosted GlitchTip or hosted Sentry whenever you want — no code change.
- **Hooks:** a chained `sys.excepthook` (CLI) and `init_reporting()` at web
  `create_app()` (so sentry-sdk's FastAPI integration captures server errors —
  and the frozen desktop sidecar, which runs the same app). New optional extra
  `evi-assistant[telemetry]` (`sentry-sdk`).
- A "log a GitHub issue" backend was evaluated and deferred — it needs a
  token-holding relay and re-implements dedup/scrub/rate-limit that the SDK
  gives free; the `Reporter` seam leaves room for it later.

### Added — Phase 50: one-click first-run setup

A brand-new user has no LLM backend, so the web/desktop UI showed a dead-end
"no backend" banner. That banner is now a **setup wizard**:

- **`evi/firstrun.py`** — per-OS, package-manager-first Ollama install planning
  (`winget` on Windows, Homebrew on macOS, the official `install.sh` on Linux),
  with a graceful manual-download fallback when no unattended path exists, plus
  an `install_ollama()` runner (with `dry_run`).
- **`recommend.first_run_model(hw)`** — picks a small, fast default to auto-pull
  (`qwen2.5:3b-instruct-q4_K_M`, ~1.9 GB), capped at 3B even on big GPUs so the
  *first* download is quick; `recommend()` still surfaces the bigger
  hardware-optimal model as an upgrade.
- **New endpoints:** `POST /api/backend/install` (unattended Ollama install) and
  SSE `GET /api/backend/pull` (streams model-pull progress). `GET
  /api/backend/status` now also reports `recommended_model` +
  `can_auto_install_ollama`.
- **Web UI wizard:** the banner's "⚡ Set up Evi automatically" button chains
  install → start → pull (with a live progress bar) → recheck, so a fresh user
  reaches first chat without manual backend setup. Manual fallbacks remain.
- **Decisions (from research):** we deliberately do **not** bundle a runtime
  (the multi-GB model download is the real cost; bundling balloons the installer
  5–10×), and **vLLM is excluded** for first-run (GPU/CUDA/Linux server-grade).

### Added — Phase 51: desktop in-app auto-update

The desktop app now updates itself from its signed GitHub releases (the CLI/pip
path already self-updates via `evi update`; this is the desktop analog).

- **Tauri updater plugin** wired into the Rust shell: on launch it checks
  `releases/latest/download/latest.json` and, if a newer **signed** build
  exists, downloads + installs + restarts. Background — never blocks launch.
  Opt out with `EVI_AUTO_UPDATE=0`; skipped in remote mode.
- **Signing:** updater minisign keypair generated; public key in
  `tauri.conf.json`, private key + password as repo secrets consumed by
  `desktop-release.yml` (`createUpdaterArtifacts: true` → `.sig` + `latest.json`
  attached to releases). Only bundles signed with our key install.
- Desktop app version bumped **0.1.0 → 0.2.0** (the updater compares this to
  the latest release). OS code-signing (Authenticode/Apple) is still separate +
  TODO — minisign signing ≠ SmartScreen/Gatekeeper trust. Key handling + the
  release flow are documented in `docs/releasing.md`.

### Added — Phase 49: dependency vulnerability scanning

- CI **`security.yml`**: `pip-audit` (OSV) for Python + `cargo-audit` (RustSec)
  for the desktop crate, weekly + on push/PR. cargo-audit gates on real
  vulnerabilities only (Tauri's transitive GTK3 tree trips "unmaintained"
  notices with no upgrade path — those stay warnings).
- **`.github/dependabot.yml`** for pip + cargo + github-actions (weekly,
  grouped, Conventional-Commit prefixes); **`deny.toml`** for local
  `cargo deny` / future license+ban gating.
- GitHub-native **Dependabot alerts + security updates** enabled. (CodeQL /
  secret-scanning aren't free on private repos → deferred.)

### Added — desktop-release CI

- **`.github/workflows/desktop-release.yml`** builds the standalone Tauri
  installers (Windows/macOS/Linux) on `desktop-v*` tags or manual dispatch and
  attaches them to a draft release. Verified end-to-end on all three OSes.

### Fixed

- **CI was red since the initial commit** — `ci.yml` ran `ruff check evi apps
  tests`, but the top-level `apps/` had moved under `evi/apps/`, so the lint
  step errored before tests ran. Now `evi tests scripts`. CI is green.

### License

- The project is now explicitly **MIT** (LICENSE present; was already drafted).

## [0.22.0] — 2026-06-06

Phase 48 — desktop standalone launch is now fast, and the web/desktop UI
tells you (and helps you fix it) when no local LLM backend is running.

### Desktop — standalone launch ~2.7 s (was ~16 s)

- **`--onedir` sidecar, not `--onefile`.** `scripts/build-sidecar.{ps1,sh}`
  now freeze the server as a folder (`evi-server[.exe]` + `_internal/`)
  instead of a single self-extracting exe. Onefile unpacked ~70 MB to a
  temp dir on every launch (~13–16 s cold start); onedir runs in place, so
  the app window appears in ~2–3 s.
- **`tauri.standalone.conf.json` ships the folder via `bundle.resources`**
  (was `externalBin`, which expects a single binary). `main.rs` resolves
  the sidecar from Tauri's `resource_dir()` (`<resources>/evi-server/`),
  with adjacent-exe fallbacks for dev/staged layouts.
- The non-blocking loading shim (`desktop/dist-shim/index.html`, "Starting
  Evi…" spinner polling `/api/health`) added in 0.21.2 still covers any
  residual startup time and now almost always flashes by.

### Added — "No local LLM backend" UX (web + desktop)

- **Banner in the web UI** (`evi/apps/web/static/index.html`): when no
  reachable OpenAI-compatible backend is found, a "⚠ No local LLM backend"
  banner offers **Start** (auto-start Ollama), **Install** (open the
  backend's download page), and **Recheck**, and gates message-send until a
  backend answers.
- **`GET /api/backend/status`** probes the configured backend plus known
  local candidates **concurrently**, caches the result for 3 s, validates
  the OpenAI `/v1/models` shape (so a random service on a port isn't
  mistaken for an LLM), and reports llama.cpp's resolved port. New
  **`POST /api/backend/start`** (Ollama auto-start) and
  **`POST /api/backend/open-download`** back the banner's actions.

### Added — `evi/portprobe.py` + llama.cpp port discovery

- New dependency-light **`evi.portprobe`** module: a raw-socket
  `port_open` check, `is_openai_server` (200 + JSON `data` list), and
  `discover_llamacpp_url` that scans `8080..8090` for a real server. Host
  is normalised `localhost`/`::1` → `127.0.0.1`.
- **`evi/backends/llamacpp.py`** auto-discovers a live llama.cpp across
  8080–8090 when the configured port isn't the one serving (cached;
  `discover_ports=True`).

### Fixed

- **`:8080` false positives** — backend detection counted *any* service
  answering on a known port as an LLM. It now requires an OpenAI-shaped
  `/v1/models` response.
- **Slow backend status (~13.8 s → ~1.5 s)** — concurrent probing + a 3 s
  cache replace the old serial, uncached checks.
- **Windows `localhost` IPv6 stall** — a connect to a closed `::1`
  loopback port is *dropped* (SYN filtered), not refused, so it blocked for
  the full timeout. Probes and connections now pin to `127.0.0.1`.
- **Time-bomb transcript test** — `tests/test_transcripts.py` used a fixed
  date that would eventually fail; made it relative.
- **`python-multipart` missing from the `web` extra** — the `/api/transcribe`
  and `/api/upload` endpoints use `Form`/`UploadFile`, which FastAPI requires
  `python-multipart` for *at route-registration time*, so `create_app()`
  raised and the server wouldn't start. It was only ever present transitively
  via the `mcp` extra, so `pip install evi-assistant[web]` (and the standalone
  sidecar) shipped a server that crashed on boot. Now a declared `web` dep.

### Build — standalone sidecar

- **Isolated build venv.** `build-sidecar.{ps1,sh}` now prefer a `.venv-build`
  if present. `--collect-submodules evi` pulls every `evi.tools.*` module, so
  building from a dev `.venv` that has the `stt`/`computer`/`rerank` extras
  installed dragged torch + faster-whisper + sounddevice + av into the
  "practical tier" sidecar (~75 MB → >1 GB). Build from a venv with only
  `web,pdf,index,build-desktop` for the lean ~128 MB onedir.
- Added `--hidden-import python_multipart` (FastAPI imports it lazily, so
  PyInstaller's static analysis misses it) and a `python_multipart` line to
  `evi-server --check`.
- **Verified end-to-end on Windows (2026-06-06):** rebuilt the onedir sidecar
  (127.9 MB, `--check` OK), built both installers (`Evi_0.1.0_x64_en-US.msi`
  59.5 MB, `Evi_0.1.0_x64-setup.exe` 46.0 MB), and confirmed the built
  `evi-desktop.exe` resolves + spawns the sidecar, which serves
  `/api/health` 200 and the no-backend banner.

### Tests

- New `tests/test_portprobe.py`; rewrote `tests/test_backend_status.py`;
  +5 cases in `tests/test_backends.py` for the llama.cpp port fallback.

## [0.21.2] — 2026-05-29

### Desktop — fixed: app launched the sidecar but no window appeared

Running the built app spawned `evi-server.exe` (with a stray console
window) but showed no Evi window. Two `main.rs`/config bugs:

- **Duplicate window label.** `tauri.conf.json` declared a window
  (auto-labeled `main`) *and* `main.rs` creates a `main` window at runtime
  (so it can set the URL to the local server port / `EVI_REMOTE_URL` /
  shim). The runtime `build()` collided on the label and errored, killing
  the app right after it spawned the sidecar. Fixed by emptying
  `app.windows` in the config — `main.rs` is the sole window creator.
- **Stray console window + no logs.** The console-subsystem sidecar popped
  a console window, and its output went to `Stdio::null` (no debug trail).
  New `configure_child()` routes the sidecar's stdout/stderr to
  `~/.evi/logs/desktop-server.log` and spawns it with `CREATE_NO_WINDOW`
  on Windows (applied to both the bundled-sidecar and dev-python spawns).

- **Connection-refused / startup race.** With the window finally showing,
  it loaded `http://127.0.0.1:<port>` but the onefile sidecar's cold start
  (unpack 70 MB + import the server) takes ~13–16 s and varies, so a
  synchronous 20 s health-wait sometimes lost the race → the webview hit
  the port before the server bound. Reworked startup: `main.rs` no longer
  blocks — it shows a loading page **immediately** and injects the chosen
  port (`window.__EVI_PORT__`); the shim (`dist-shim/index.html`) polls
  `/api/health` and redirects to the server once it's up. Robust to any
  cold-start time, with a "Starting Evi…" spinner and a log-path hint.

Verified headlessly: the app stays alive (no crash), the sidecar binds,
and the root Evi UI serves HTTP 200 — the shim redirects into it. No
console window.

## [0.21.1] — 2026-05-29

### Desktop — standalone build verified end-to-end (Windows)

The self-contained desktop app was built for real on Windows; the
build scripts are no longer "verified-by-construction" only.

- **Verified:** PyInstaller froze a 72.7 MB `evi-server.exe` sidecar
  (`--check` self-test passes; the frozen server boots + answers
  `/api/health`), and `tauri build --config tauri.standalone.conf.json`
  produced `Evi_0.1.0_x64_en-US.msi` (~79 MB) and `Evi_0.1.0_x64-setup.exe`
  (~78 MB) with the sidecar embedded.
- **Fixed `desktop/src-tauri/Cargo.toml`:** removed a `[lib]
  evi_desktop_lib` target with no `src/lib.rs` (create-tauri-app mobile
  template leftover) that broke `cargo` outright — it's a plain binary
  crate.
- **Fixed `desktop/src-tauri/tauri.conf.json`:** `bundle.icon` listed only
  a PNG; the Windows resource + installers need an `.ico`. Added a
  generated icon set (`icons/`, via `tauri icon`) and the standard
  32/128/128@2x/icns/ico `bundle.icon` list.
- **`scripts/sidecar_entry.py`** gained a `--check` mode that imports the
  bundled deps (app + `fitz`/`numpy` + uvicorn protocols) and reports —
  lets a frozen build self-verify without a chat flow.

The `evi-assistant` wheel is functionally unchanged (these live in `desktop/` +
`scripts/`, neither of which ships in the wheel). See
`docs/desktop-bundling.md` for the full, now-verified flow.

## [0.21.0] — 2026-05-29

### Changed — self-contained desktop bundle (practical tier)

- The desktop sidecar now freezes **web + pdf + index** (was core + web).
  A standalone Evi Desktop build covers chat, tools, image-gen, the web UI,
  PDF reading, and semantic search with no Python on the machine. STT
  (`faster-whisper`/PortAudio) and computer-use stay opt-in via a system
  Python — they'd bloat the binary and drag in fiddly native deps.
  `build-sidecar.{ps1,sh}` install `.[web,pdf,index]` and add
  `--collect-all pymupdf --collect-all numpy --hidden-import fitz`.
- **OCR tesseract resolution** — `evi/tools/ocr.py` now resolves the
  binary in order: `$EVI_TESSERACT_CMD` → `~/.evi/tools/bin/tesseract`
  (what `evi-tools install` drops) → PATH. The desktop shell sets
  `EVI_TESSERACT_CMD` (+ `TESSDATA_PREFIX`) when a `tesseract` binary is
  bundled next to the sidecar, so OCR works offline in the standalone app.
- New **`evi-assistant[build-desktop]`** extra (`pyinstaller`) — a build-time-only
  dependency for freezing the sidecar.
- `docs/desktop-bundling.md` updated for the practical tier + the optional
  Tesseract-bundling step. (The Tauri/PyInstaller build itself is still
  per-OS and unverified in CI.)

### Distribution note

The desktop app is **not** a pip extra — pip extras only install Python
deps, they can't build a native installer. It's a separate downloadable
artifact (native installers via GitHub Releases), built from `desktop/` in
this monorepo, embedding a frozen server. `pip install evi-assistant` remains the
path for CLI / web / server / library use.

### Bumped to 0.21.0.

## [0.20.0] — 2026-05-29

### Added — external-binary provisioner + standalone desktop scaffold

- **`scripts/evi_tools.py`** (+ `evi-tools.ps1` / `.sh` wrappers) — a
  bootstrap helper *outside* the package for the programs Evi shells out
  to. **Package-manager-first** (winget/choco/scoop → brew →
  apt/dnf/pacman/zypper), falling back to a direct download into
  `~/.evi/tools/bin/` only where a clean prebuilt exists (ffmpeg).
  Commands: `list`, `install <tesseract|ffmpeg|ollama>` (`--dry-run`,
  `--force`), `remove`, `path`. Pairs with `evi doctor`, which reports
  what's missing.
- **Standalone desktop bundle (scaffold, unverified build).** The Tauri
  shell (`desktop/src-tauri/src/main.rs`) now prefers a frozen
  `evi-server[.exe]` **sidecar** sitting next to the app binary, falling
  back to system Python only when absent. `scripts/sidecar_entry.py`
  (imports the FastAPI `app` object + runs uvicorn) is frozen by
  `scripts/build-sidecar.{ps1,sh}` via PyInstaller and staged for Tauri's
  `externalBin`. A separate `tauri.standalone.conf.json` overlay enables
  the sidecar so the default (no-Python-bundled) build still works.
  Full flow + caveats in **`docs/desktop-bundling.md`**. The Python pieces
  are tested; the Rust+PyInstaller+Tauri build itself must be run per-OS.

### Changed

- Bumped to 0.20.0.

## [0.19.0] — 2026-05-28

### Changed — packaging: single `evi` namespace (no top-level `apps`)

- The CLI + web frontends moved from a top-level `apps/` package into
  **`evi.apps.*`** (`evi/apps/cli`, `evi/apps/web`). This removes the
  generic `apps` top-level package from the wheel — it would have risked a
  PyPI namespace collision with any other project shipping `apps`. The
  wheel's `top_level.txt` is now just `evi`.
- The Tauri desktop project moved out of the Python tree to a top-level
  **`desktop/`** (it was never a Python package).
- Entry point is now `evi = evi.apps.cli.main:app`; `evi web` and the Tauri
  shell launch `uvicorn evi.apps.web.server:app`. The `evi` command and
  `import evi` are unchanged for users.

### Changed — packaging: distribution renamed to `evi-assistant`

- The PyPI distribution is now **`evi-assistant`** (the bare `evi` name is taken).
  The **import package stays `evi`** (`import evi`) and the **CLI command
  stays `evi`** — only the install name changes: `pip install evi-assistant`.
- All in-app install hints now read `pip install 'evi-assistant[<extra>]'`.
- The self-updater (`evi update`) targets `evi-assistant` on PyPI: the version
  probe hits `/pypi/evi-assistant/json`, `pip show evi-assistant` detects editable
  installs, and pipx/poetry/uv/pipenv hints all reference `evi-assistant`.
  A new `evi.update.DIST_NAME` constant centralises the name.
- Verified `python -m build` produces `evi_assistant-0.19.0-py3-none-any.whl`
  carrying the `evi`/`apps` packages, the `evi` console-script entry
  point, `py.typed`, and the web static assets.

### Added — Phase 37: medium-value SDK items

Knocks out the four M-sized rows from `docs/sdk-coverage.md`, leaving only
MCP-server-publish and the Responses API migration.

- **Prompt caching (`cache_prompt`)** — new `[llm] cache_prompt` (default
  off). When on, forwards `extra_body.cache_prompt=true`; llama.cpp's
  server reuses the KV cache for the stable system+memory+project prefix
  across turns. vLLM does the equivalent server-side. Other backends
  ignore the key.
- **Logprobs + confidence** — new `[llm] logprobs` + `top_logprobs`. When
  enabled, the backend's per-token log-probabilities are collected into a
  new `LogProbs` event (avg/min logprob, low-confidence token count). The
  CLI prints a `confidence: N% avg · M low-confidence token(s)` line; the
  web UI shows a per-bubble confidence badge. Requested on the first round
  only.
- **Audio input** — `evi/audio_input.py` builds OpenAI `input_audio`
  parts for omni models (`model_supports_audio`: Qwen2.5-Omni, MiniCPM-o,
  gpt-4o-audio). `Agent.chat(audio=[...])`, web `ChatRequest.audio`, and
  the CLI `/audioraw <path> [prompt]` slash. **Graceful degrade:** non-omni
  models transcribe the clip via local Whisper and fold the transcript
  into the text, so "talk about this clip" works on any model.
- **Guardrails** — `evi/guardrails.py` + `~/.evi/guardrails.toml`: a local,
  regex-first content filter (off by default). Rules have `action`
  (block | redact) and `applies_to` (input | output | both). Input block
  rules refuse the turn before any LLM call; redact rules rewrite the text;
  output rules clean the stored reply and flag it. New `Guardrail` event,
  CLI `evi guardrails list / test / path`. Borrowed in spirit from Bedrock
  Guardrails / Gemini safety settings, but fully local.

### Bumped to 0.19.0.

## [0.18.0] — 2026-05-28

### Added — Phase 36: quality-of-life bundle

- **`evi doctor`** — one-shot environment diagnostic. Checks `~/.evi/`
  writability, config.toml parse, LLM backend reachability, hardware
  (RAM/GPU), external binaries (git, tesseract, TTS engine), and every
  optional Python extra. Renders a ✓/⚠/✗ checklist with an
  ok/warn/fail tally. `--strict` exits non-zero on any failure.
- **`read_file` output caching** — results are cached in-process keyed by
  `(resolved path, mtime_ns, size)`. Re-reading an unchanged file returns
  the cached `ToolOutput` (and citation) without touching disk; any edit
  invalidates the entry. Bounded at 128 files. `clear_read_cache()` for
  tests / future explicit invalidation.
- **Permission batching** — when one assistant turn proposes multiple tool
  calls that need approval, Evi now prompts **once** instead of N times.
  New optional `Agent(permission_batch_callback=...)`. The CLI batch
  prompt lists every call and accepts `a` (all) / `n` (none) /
  comma-separated indices (e.g. `1,3`) / `s` (allow all this session).
  Pre-approved categories still never prompt; single-call turns fall back
  to the per-call prompt.
- **Session auto-titling** — `Agent.suggest_title()` produces a terse
  (≤6-word) title from the opening exchange. The web UI calls
  `POST /api/session/{id}/title` after the first turn and renames the tab
  from the raw first message to the LLM-written title. New
  `evi sessions title <id>` prints a title for a saved session.
- **Hot-reload of skills & memory** — confirmed `/reload`
  (`Agent.refresh_config()`) now reflects freshly added skills and memory
  files without a restart, since both stores rescan disk on every prompt
  composition. Locked in with tests.

### Bumped to 0.18.0.

## [0.17.0] — 2026-05-28

### Added — Phase 35: remaining OpenAI SDK params

Closes out the cheap pass-through parameters from `docs/sdk-coverage.md`.

- **`parallel_tool_calls`** — new `[llm] parallel_tool_calls` (default
  `true`). When `false`, the model may only request one tool per
  assistant turn. Per-turn override via `Agent.chat(parallel_tool_calls=…)`
  and web `ChatRequest.parallel_tool_calls`. Only forwarded when `false`
  AND tools are present, so backends that don't speak the flag never see
  it.
- **`max_completion_tokens`** — new `[llm] max_completion_tokens`
  (default `0` = unset). When `> 0` it's sent *instead of* `max_tokens` —
  reasoning models (o-series, some local R1 builds) reject `max_tokens`
  and want the completion-token budget that counts hidden reasoning plus
  visible output.
- **`logit_bias`** — new `[llm] logit_bias`, a JSON string
  (`'{"123": -100, "456": 5}'`) since our flat TOML writer can't nest a
  dict. Values clamp to ±100. Per-turn override via
  `Agent.chat(logit_bias={…})` and web `ChatRequest.logit_bias`. Invalid
  JSON is dropped (logged in `--debug`), never fatal. You need the
  model's tokenizer to find ids, so this is mostly for power use.
- **`n`-best-of variants** — new `Agent.complete_variants(prompt, n)`:
  a stateless, non-streaming, no-tools helper that returns a list of
  independent completions (default `temperature=0.9` for variety). New
  **`evi variants "<prompt>" -n 3 [-t 0.9]`** command. Backends that
  ignore OpenAI's `n` return a single variant; the CLI notes when it got
  fewer than requested.

### Bumped to 0.17.0.

## [0.16.0] — 2026-05-28

### Added — Phase 34: speculative decoding (`prediction`)

- **`Agent.chat(prediction="...")`** forwards OpenAI's predicted-outputs
  hint (https://platform.openai.com/docs/guides/predicted-outputs) via
  `extra_body`. Backends that support it (OpenAI, vLLM, llama.cpp with
  speculative decoding) verify the prediction token-by-token instead of
  regenerating, which is 3-5× faster when the prediction is mostly right.
  Backends that don't recognise the field drop it silently — same risk
  shape as `reasoning_effort`.
- Only applied to the FIRST LLM round-trip in a turn. Once any tool runs,
  the prediction is stale and we let the model write freely.
- **`evi edit <file> "<instruction>"`** — new top-level command. Reads
  the file, sends its contents as the prediction, asks the model to apply
  the instruction, and returns the edited file. Flags:
  - `--diff` — show a coloured unified diff instead of the raw file
  - `--write` / `-w` — overwrite the file (with a confirmation prompt)
  - `--yes` / `-y` — skip the confirmation
  - Default behaviour prints the new content to stdout so you can pipe.
- **`/predict` slash command** for the REPL. Three forms:
  - `/predict <text>` — set a literal prediction string
  - `/predict file <path>` — read a file as the prediction
  - `/predict clear` — drop a pending prediction
  - Tab-completes file paths after `/predict file`.
- **`ChatRequest.prediction`** on the web API — same field, same shape.
  Forwarded through to `agent.chat(prediction=...)`.

### Bumped to 0.16.0.

## [0.15.0] — 2026-05-27

### Added — Phase 32: conversation grep

- **`evi search "<query>"`** greps across every saved transcript under
  `~/.evi/transcripts/`. Substring matching by default, regex via
  `--regex` (case-insensitive). Each hit prints session id, role,
  one-line snippet with surrounding context, and line number into the
  source JSONL.
- Filters: `--days N` window (default 90), `--role user|assistant|tool|system`,
  `--session <id>`, `--limit N`.
- Results stream newest-first. Jump back into a session with
  `evi sessions show <id>` (already shipped in Phase 14.B).
- No new deps — pure stdlib over the existing JSONL transcript store.

### Bumped to 0.15.0.

## [0.14.0] — 2026-05-27

### Added — Phase 31: git-aware code review

- **`evi review`** runs a focused code review with `git diff` as input
  and the LLM as the reviewer. Defaults to `git diff HEAD` (working
  tree vs last commit). Other modes:
  - `evi review HEAD~3..HEAD` — explicit range
  - `evi review --staged` — what's queued for commit
  - `evi review --branch main` — the current branch's PR diff
  - `evi review --file path/to/x.py` — one file
  - `evi review --diff-file change.patch` — saved patch
- The reviewer prompt focuses on bugs, security issues, API breakage,
  performance gotchas, and missing tests; nudges away from style nits;
  ends with a one-line verdict (APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION).
- The review agent runs scoped: only `fs` / `git` / `index` read-only
  tools are enabled by default so it can pull surrounding context
  without touching anything. `--no-tools` disables tools entirely.
- Pairs with multi-model routing: if you have a `coder` route configured
  with `evi route preset common`, code-heavy review prompts auto-route
  there. The system prompt is review-focused so the model knows its
  job.
- Diffs are capped at 64 KB to fit the context window; longer diffs get
  a `(diff truncated)` marker.

### Bumped to 0.14.0.

## [0.13.0] — 2026-05-27

### Added — Phase 30: citations + rerank

- **Citations.** Tools can now return either a plain `str` (no citations,
  works as before) or a `ToolOutput(text, citations)`. New `Citation`
  dataclass carries `{id, source_type, source_id, excerpt, start, end}`.
  The `ToolResult` event grows a `citations: list[Citation]` field, the
  web SSE serializes it, and the web UI renders chips
  (`[1] path/foo.py:10-25`) underneath each tool bubble.
  - `read_file` emits one citation per call covering the full file.
  - `find_in_project` emits one citation per hit, with line numbers.
  - `web_fetch` emits one citation pointing at the URL.
  - `Tool.call_rich(args)` is the new contract — old `Tool.call(args)`
    is now a thin shim returning just the text for back-compat. All 53
    existing tool-call-site tests still pass unchanged.
- **Local rerank** (`evi/tools/rerank.py`, category `index`, default
  off). After `find_in_project` returns top-K by cosine similarity, the
  `rerank(query, candidates, top_k)` tool re-scores with a local
  cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80 MB,
  downloaded on first use). Accepts either bare-string candidates or
  `find_in_project`-shaped `{text, path, lines}` dicts; results carry
  scores + citations. New extra: `pip install 'evi[rerank]'` →
  `sentence-transformers>=3.0`.

### Bumped to 0.13.0.

## [0.12.0] — 2026-05-27

### Added — Phase 29: self-update + rollback

- **`evi update`** checks PyPI for a newer Evi, snapshots the current
  `pip freeze` state, runs `pip install --upgrade evi==<version>` via
  `sys.executable -m pip` (so the upgrade lands in the same venv we're
  running from), and verifies the result by spawning a fresh python and
  importing.
- **Refusals.** Editable installs are detected via `pip show` and
  refused. pipx installs are detected via `PIPX_HOME` or path markers
  and the user is steered to `pipx upgrade`. Locked envs (poetry.lock /
  uv.lock / Pipfile.lock anywhere from cwd up to `$HOME`) are refused
  unless `--force` is passed.
- **Snapshots** live under `~/.evi/snapshots/<ts>_<from>_to_<to>/`
  with `requirements.txt` (the full pre-upgrade pip freeze),
  `version.txt`, and `metadata.json`. Retention: last 5 newest; the
  GC fires automatically after a successful upgrade.
- **`evi update rollback [N|<dir>]`** restores a snapshot by running
  `pip install -r requirements.txt`. Defaults to the newest. We
  restore the FULL freeze, not just Evi, because a transitive bump
  can break the import just as easily as a direct one.
- **`evi update history`** lists snapshots newest-first.
  **`evi update prune --keep N`** is the manual GC.
  **`evi update from-wheel <path>`** does an offline install from a
  local `.whl` or sdist, with the same snapshot + verify lifecycle.
  **`evi update settings`** prints install kind + snapshot dir +
  retention.
- **`evi update check`** is the safe probe — hits PyPI only, never
  installs anything.

No new runtime deps (uses stdlib `subprocess` + httpx, both already in core).

### Bumped to 0.12.0.

## [0.11.0] — 2026-05-27

### Added — Phase 28: calendar reading

- **iCal URL + CalDAV calendar support.** New `evi/calendar.py` reads
  public iCal endpoints (Google Calendar Secret iCal address, iCloud
  public, Outlook publish, raw .ics files) AND authenticated CalDAV
  servers (Nextcloud, Fastmail, Posteo, iCloud private, mailbox.org,
  Synology, Radicale). Sources live in `~/.evi/calendars.json`;
  passwords are NEVER stored on disk — CalDAV sources name an env var
  (`password_env="EVI_CAL_WORK_PASSWORD"`) that the reader resolves at
  call time. Both reader paths normalise to the same `Event` dataclass
  so tools don't see the source-shape difference.
  - **Tools** (`evi/tools/calendar.py`, category `calendar`, default off):
    `calendar_today`, `calendar_week`, `calendar_search(query, days)`,
    `calendar_next`. All accept an optional `source` to scope to one
    feed. Per-source errors are surfaced inline so one broken feed
    doesn't black out the rest.
  - **CLI**: `evi calendar list / add / remove / peek`. `add` takes
    `--url`, `--kind ical|caldav`, `--username`, `--password-env`,
    `--calendar` (to scope to a named CalDAV calendar).
  - **Extras**: new `pip install 'evi[calendar]'` →
    `icalendar`, `recurring-ical-events`, `caldav`.
  - **Output**: events grouped by day, `HH:MM-HH:MM Summary @ location`
    per line, all-day events sort to the top of their day.
- **Deferred integrations tracked.** New `docs/future-integrations.md`
  captures the explicitly-deferred apps (Home Assistant, Notion,
  Spotify, Plex, Slack, VS Code, JetBrains) plus a long candidate
  table (Discord, Telegram, GitHub native, Linear, IMAP/SMTP, RSS, etc).
  Easy to extend as ideas come up.

### Bumped to 0.11.0.

## [0.10.0] — 2026-05-27

### Added — Phases 24-27: voice loop, routing, web auth, REPL completion

- **Always-on voice mode** (`evi voice loop`). New `evi.voice.AutoListener`
  runs an energy-based VAD over a continuous `sounddevice.InputStream` at
  16 kHz / 30 ms frames. Speech is detected on ~180 ms of voice; an
  utterance ends after ~750 ms of silence (or 30 s max), gets transcribed
  via Whisper, optionally gated on a wake phrase, and handed to a
  callback. The new `voice loop` CLI orchestrates listener + Agent +
  AutoSpeaker so you can keep talking without typing. `pause()`/`resume()`
  on the listener prevents Evi from transcribing its own TTS echo.
  - Flags: `--wake "<phrase>"`, `--model tiny.en`, `--device cpu`,
    `--no-speak`, `--rms-threshold`, `--debug`.
  - Needs `pip install 'evi[stt]'` for sounddevice + faster-whisper.
- **Multi-model routing.** New `evi/routing.py` picks a model per turn
  based on cheap keyword rules first, then optionally a tiny LLM
  classifier. Routes live in `~/.evi/routes.json`; keyword matches use
  word-boundary anchoring so `hi` doesn't trip on `this`. Precedence:
  routing > fast_mode > configured default. Re-rolls (`continue_chat`)
  stay on the model the original turn picked.
  - New config: `[llm] router_enabled`, `[llm] router_model`.
  - New CLI: `evi route list/add/remove/test/enable/disable/classifier/preset`.
  - A `common` preset ships with `coder` + `fast` routes out of the box.
- **Web UI auth.** New `[web] auth_token` config. When set, every
  `/api/*` endpoint requires `Authorization: Bearer <token>` or a
  `?token=<token>` query param; `/`, `/static/*`, `/images/*`,
  `/api/health`, `/api/auth/check` stay public so the login form can
  bootstrap. The browser stores the token in `localStorage` and a global
  `fetch` wrapper attaches it automatically. 401 responses pop the
  login overlay back up.
  - New CLI: `evi web-config token show / rotate / clear`.
  - Constant-time compare via `secrets.compare_digest`.
- **REPL tab completion.** The chat REPL now runs through `prompt_toolkit`
  with a custom completer + persistent history at `~/.evi/repl_history`.
  Tab completes slash commands, then their arguments — model ids
  (lazy-fetched from the backend), effort levels, on/off toggles, tool
  names for `/forcetool`, filesystem paths for `/image` and `/audio`.
  Falls back to `rich.Console().input` if `prompt_toolkit` is missing.
  - New core dep: `prompt_toolkit>=3.0`.

### Bumped to 0.10.0.

## [0.9.0] — 2026-05-27

### Added — Phase 23: OCR + streaming TTS

- **OCR tool** (`evi/tools/ocr.py`). `ocr_image(path, language='eng')`
  shells out to the Tesseract binary; clean error pointing at the
  install command for each OS if the binary isn't on PATH. Bonus
  `ocr_screen()` takes a fresh screenshot and OCRs it in one call (needs
  `[computer]` + tesseract). New `tools.ocr` toggle, default off.
- **Streaming TTS alongside chat.** New `evi.voice.AutoSpeaker` buffers
  TextDelta strings and emits sentence-by-sentence speech on a worker
  thread (no overlap). Strips code fences, inline code, and URLs before
  speaking.
  - **CLI**: `/speak on|off` slash command. The REPL feeds every
    TextDelta into an `AutoSpeaker` when on, flushes the partial on
    `Done`.
  - **Web**: 🔇/🔊 toggle button in the header (persists in
    `localStorage`). Uses the browser's built-in `speechSynthesis` API
    — no extra deps, works in any modern browser. Speech is cancelled
    on the next user turn so we never talk over you.

### Bumped to 0.9.0.

## [0.8.0] — 2026-05-27

### Added — Phase 21 (polish) + Phase 22 (distribution)

- **Real markdown rendering in chat.** Assistant + system messages now go
  through marked.js with GFM tables, breaks, lists, and code blocks —
  the latter syntax-highlighted via highlight.js (github-dark theme).
  Falls back to escaped text + minimal substitutions if a CDN fails.
- **Long tool-output collapse.** Tool result bubbles longer than 400
  chars get a "show full (N chars)" toggle.
- **`evi tail`** — `tail -f` for today's transcripts. Optional
  session-id filter, configurable poll interval.
- **`pyproject.toml`** finalised for PyPI: `[project.urls]` block,
  expanded classifiers, fixed misplaced `dependencies` key. `python -m
  build` produces a clean sdist + wheel including the static
  frontend files.
- **`Dockerfile` + `docker-compose.yml`** for the headless-server case.
  Two-stage build (no compiler in the runtime image); compose stack
  wires Evi to a sibling Ollama container with named volumes.
- **CI + release workflows.** `.github/workflows/ci.yml` runs pytest +
  ruff across macOS / Linux / Windows × Py 3.11 / 3.12 on every PR.
  `release.yml` is tag-triggered, sanity-checks the version, publishes
  to PyPI via Trusted Publishing, and creates a GitHub Release with
  artifacts.
- **`docs/releasing.md`** — how to cut a version.

### Bumped to 0.8.0.

## [0.7.0] — 2026-05-27

### Added — Phase 20

- **Obsidian sync.** New `evi/obsidian.py` syncs `~/.evi/memory/` with
  a sub-directory of an Obsidian vault. CLI: `evi obsidian
  status / push / pull / sync`. Push writes each memory entry with YAML
  frontmatter (`source: evi-memory`, `name`, `created`, `updated`) so
  Dataview / Bases queries can find them. Pull strips frontmatter
  before storage; safe-name validation rejects invalid filenames. Sync
  is bidirectional with last-modified-time conflict resolution. New
  config block `[obsidian] vault_path = "..."`, `subdir = "Evi"`.
- **Multi-conversation tabs in the web UI.** Tab bar above the chat
  log; each tab is a separate session id. Click to switch (rebuilds
  history from `/api/session/{id}/history`), `+` to start a new one,
  `×` to close (always keeps at least one tab). Auto-labels from the
  first user message. State persists in `localStorage` so reloads
  restore the workspace. Branch-from-bubble now opens a new tab
  inheriting the parent's label with a "· branch" suffix.

### Bumped to 0.7.0.

## [0.6.0] — 2026-05-27

### Added — Phase 19: editable messages, export, Mermaid, live reload, audio in, PWA

- **Editable messages / re-roll / branch.** Hover any chat bubble in the
  web UI to get an action toolbar: ✏️ edit, 🔄 re-roll (assistant only),
  🌿 branch into a new session, 🗑 delete from here on. Editing a user
  message truncates everything after it and auto-rerolls. Backend:
  `Agent.truncate_history / edit_message / rewind_to_last_user /
  continue_chat` + four new endpoints under `/api/session/{id}/{...}`.
- **Conversation export.** `evi sessions export <id> --format md|html|json`
  with optional `--out`. Markdown rendering preserves tool calls + raw
  tool output; HTML wraps in a styled standalone doc.
- **Mermaid rendering in the web UI.** ` ```mermaid ` blocks in any
  assistant response get rendered as inline SVG via the Mermaid 11 ESM
  CDN module.
- **Live config reload.** New `Agent.refresh_config()` re-reads
  `config.toml` and re-composes the system prompt (so memory / skill
  edits land too). CLI `/reload` and web `/reload` slash commands wired up.
- **Audio file input.** Drop a `.wav / .mp3 / .m4a / .ogg / .flac / .webm`
  on the chat → backend transcribes it via faster-whisper → the text
  lands in the input box for you to review and send. New endpoint:
  `POST /api/transcribe`. CLI: `/audio <path>`.
- **PWA + responsive web UI.** `manifest.json` makes it installable on
  iOS / Android / desktop browsers. New mobile media queries collapse
  decorative header chips, bump touch targets, set 16px input to avoid
  iOS zoom, and turn the hover-only action toolbar into a low-opacity
  always-visible band on small screens.

### Bumped to 0.6.0.

## [0.5.0] — 2026-05-27

### Added — Phase 18: OpenAI SDK feature completion

- **`response_format`**. `Agent.chat(response_format=...)` per-turn override.
  Use `{"type": "json_object"}` for plain JSON mode, or `{"type":
  "json_schema", ...}` for guaranteed-shape outputs on backends that
  support it. New CLI command: `/json <prompt>`.
- **`tool_choice`**. Per-turn override with the OpenAI vocabulary:
  `"auto"` (default), `"none"` (drops the tools list entirely),
  `"required"` (force any tool), or `{"type":"function","function":
  {"name":"x"}}` (force a specific one). New CLI: `/notools <prompt>`
  and `/forcetool <name> <prompt>`.
- **Sampling knobs**: `top_p`, `presence_penalty`, `frequency_penalty`
  in `[llm]` config. Only forwarded when non-default so picky local
  backends don't choke.
- **`seed`**: `[llm] seed = N` for reproducible outputs.
- **`stop` sequences**: `[llm] stop_sequences = [...]` for hard
  generation cutoffs.
- **Real token usage**. Every turn requests
  `stream_options.include_usage`; the final chunk's `usage` becomes a
  new `UsageStats(prompt, completion, total)` event yielded between the
  last `TextDelta` and `Done`. CLI prints a dim line; web UI overwrites
  the approximate usage chip with the real number. Backends that don't
  emit usage simply never emit the event.

### Bumped to 0.5.0.

## [0.4.0] — 2026-05-27

### Added — Phase 17: model picker

- **Web UI model picker.** Footer button shows `model · effort · fast` and
  opens a popup matching the inspiration screenshot: Models section
  (number-key shortcuts 1–9), Effort section (Low / Medium / High / Max),
  Fast mode toggle. **Ctrl+I** opens the picker; **Shift+Ctrl+E** cycles
  effort levels without opening it.
- **`GET / POST /api/model-picker`.** GET returns `{active, models,
  effort, effort_levels, fast_mode, fast_model, backend}`. POST accepts
  any subset of those fields, persists to `config.toml`, and pushes the
  change into every live session's agent so the next turn picks it up
  without a restart.
- **Reasoning effort.** New `[llm] reasoning_effort` (default `medium`,
  one of `low|medium|high|max`). Passed via `extra_body` so backends
  that ignore it just drop it. Surfaces as a `[low|high|max]` chip in
  the CLI prompt when non-default.
- **Fast mode.** New `[llm] fast_mode` + `[llm] fast_model`. When on,
  `Agent.chat` swaps to `fast_model` for the current turn — pattern is
  a 14B for daily work, a 3B–7B as the fast alternate. CLI prompt grows
  a `[fast]` chip when active.
- **CLI slash commands.** `/effort [low|medium|high|max]` and
  `/fast [on|off|<model-id>]`. Calling `/fast <model-id>` sets the
  fast_model AND flips fast_mode on in one go.

### Bumped to 0.4.0.

## [0.3.0] — 2026-05-27

### Added — Phase 16: daily-driver UX bundle

- **`<think>…</think>` block surfacing.** The agent loop now parses
  reasoning-model output as it streams and routes inner thoughts to a new
  `ThinkingDelta` event. CLI renders dim+italic; web UI shows a
  collapsible `<details>` "thinking…" bubble that auto-collapses once
  visible output starts.
- **Parallel tool calls.** When the model emits multiple `tool_calls` in
  one assistant turn, the bodies now run in a `ThreadPoolExecutor`
  (max 4 workers). Permission and before-hook gating still runs serially
  so the human answers one prompt at a time; result order is preserved
  for `tool_call_id` consistency.
- **Context-window awareness.** New `[llm] context_size` (default 32768)
  + `[llm] compact_when_pct` (default 85%). `Agent.token_usage()`
  returns `(used, ceiling)`; the CLI prompt shows `12k/32k`,
  red-tinted past 85%. Web UI gets a usage chip refreshed on every
  turn via the new `GET /api/session/{id}/usage` endpoint. Auto-compact
  fires on the percentage threshold in addition to message-count.
- **Semantic file search.** `evi/index.py` builds a numpy-backed
  embedding index of any directory; `evi/tools/index.py` exposes
  `index_project`, `find_in_project`, `project_index_stats`. Embeddings
  go through `[llm] embed_model` (default `nomic-embed-text` for
  Ollama). New `evi[index]` extra (`numpy>=1.26`).
- **Git intelligence tools.** `git_status`, `git_diff`, `git_log`,
  `git_show`, `git_blame`, `git_info` — read-only subprocess wrappers.
  Category `git`, default off.
- **`evi --debug` / `-d`.** Top-level flag (also `EVI_DEBUG=1`). Prints
  every LLM request, tool call, and tool result to stderr. New
  `evi/debug.py` module with `dlog(tag, payload)`.

### Bumped to 0.3.0.

## [0.2.0] — 2026-05-27

### Added — Phase 14: setup wizard, sessions, PDF/SQLite, file upload, compaction, backup

- **`evi setup`** — interactive first-run wizard. Detects which backends
  are reachable, runs the hardware recommender, optionally pulls a model
  via Ollama, writes `config.toml`.
- **`evi sessions`** — list / show / resume past sessions backed by the
  transcript log. Resuming hydrates a fresh `Agent`'s history.
- **`read_pdf` tool** (`pdf` category) — extract text from a PDF via
  PyMuPDF. New `evi[pdf]` optional dep.
- **`sqlite_schema` + `sqlite_query` tools** (`sqlite` category) —
  read-only schema + SELECT against any SQLite file. DDL/DML rejected.
- **Web file upload** — `POST /api/upload` saves to per-session temp dir;
  drag-and-drop in the browser; the path lands in the next user message.
- **Conversation auto-compaction** — when chat history grows past a
  threshold the oldest turns are summarised into one system note via a
  subagent. Manual trigger: `/compact`. Threshold via
  `[llm] compact_after_messages`.
- **`evi backup create / restore`** — portable archive of `~/.evi/`, with
  configurable excludes (`--no-transcripts`, `--no-models`). Re-installs
  config + memory + skills + scheduled tasks on a new machine.

### Added — Phase 15: vision

- Vision-capable backends can now receive images. `Agent.chat()` takes
  optional `images=` list; CLI exposes `/image <path>` and the web UI
  shows a preview when you drop in an image.

### Added — polish

- `LICENSE` (MIT) shipped.
- `docs/tools.md` — full tool reference with safety table.
- `docs/troubleshooting.md` — common failure modes.
- `examples/` — sample `EVI.md`, two skills (`code-review`,
  `summarize-paper`), and a `commit` slash command.
- `--version` / `-V` flag.
- `py.typed` marker for downstream type checkers.

### Fixed

- (Tracked in 0.1.0; restated for visibility.)

## [0.1.0] — 2026-05-27

Initial coherent release across thirteen phases:

### Foundation (phases 1, 3–6)
- Core agent loop, tool framework, fs/code tools, CLI REPL.
- ComfyUI image-generation tool.
- FastAPI + SSE web UI.
- Tauri 2 desktop shell (local-spawn and remote modes).
- Persistent memory with soft-delete + agent system-prompt injection.
- Scoped subagent runner (`delegate_explore`, `delegate_plan`).

### Integrations (phase 7)
- MCP (Model Context Protocol) client + bridge + manager. Any stdio MCP
  server's tools appear as `<server>.<tool>` in the registry.

### Workflows (phases 8, 10, 11)
- Skills (markdown instruction packets) with on-demand loading.
- Scheduled tasks via APScheduler.
- EVI.md auto-loaded project context.
- Slash command dispatcher: `/help`, `/reset`, `/tools`, `/model`,
  `/goal`, `/plan`, `/auto`, plus user-defined templates under
  `~/.evi/commands/`.
- Persistent `/goal` injection + one-shot plan mode.
- Hook system over `~/.evi/hooks.toml` (`before_tool_call` /
  `after_tool_call`, glob match, veto).
- Permission flow with auto-approval categories and `/auto on|off`.
- `evi worktree` for parallel git work.

### Models + multi-machine (phase 9)
- Backend abstraction: LM Studio, Ollama, llama.cpp, OpenAI-compatible.
- `evi models list / use / info / recommend / pull / backend`.
- Hardware recommender (nvidia-smi parse + psutil) with curated tiers.
- HuggingFace direct downloads (`hf:<repo>:<file>`).
- Profiles (`~/.evi/profiles/<name>.toml`) overlay base config.
- Tauri remote mode via `EVI_REMOTE_URL`.

### Transcripts, dreaming, vision-of-the-world (phase 12)
- Session transcripts (JSONL) feeding the dream engine.
- `evi dream` — scheduled memory consolidation with audit logging.
- Web search + fetch tools.
- TTS via platform CLIs (no Python deps).
- Computer use (`screenshot`, `click`, `type_text`, `key`, `scroll`).

### Phase 13 — listen + parity + polish
- STT via faster-whisper.
- Web UI parity: server-side slash dispatcher; permission flow as SSE
  `PermissionRequest` events; browser dialog; `/api/decide` endpoint.
- Top-level `README.md` rewrite; `docs/architecture.md`,
  `docs/configuration.md`, `docs/multi-machine.md`,
  `docs/development.md`; install + test scripts.

[0.2.0]: #020--2026-05-27
[0.1.0]: #010--2026-05-27
