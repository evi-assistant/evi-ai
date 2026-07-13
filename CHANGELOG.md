# Changelog

All notable user-visible changes to eVi. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.11] — 2026-07-13

### Fixed
- **Settings / Model picker no longer stalls ~5s when a local backend is down.**
  Opening Settings or the model picker enumerates every enabled backend's models
  over HTTP; that path used the 120s chat timeout against `localhost` URLs and
  skipped the fast socket pre-check, so a configured-but-not-running local backend
  (e.g. LM Studio on :1234) stalled the panel for seconds on Windows' dual-stack
  loopback (`::1` SYN-filtered → full connect timeout). Model listing now
  fast-fails via a socket probe + `localhost`→127.0.0.1 normalisation — a down
  backend returns empty in **~0.4s instead of ~5s**.

### Added
- **More settings surfaced in the Settings panel.** Config that was previously
  `config.toml`/CLI-only is now editable in the web/desktop UI: `reasoning_effort:
  off`, **fallback models**, **format-on-edit** / **check-on-edit**, **transcript
  retention**, the **web-search backend** (ddg/searxng/ollama) + SearXNG URL,
  **completion notifications** (sound / desktop toast / ntfy-webhook), the
  `[ultracode]` **fan-out master toggles** (the per-backend fan-out flag no longer
  does nothing without them), and the `[federation] a2a` interop-endpoint toggle.

## [1.0.10] — 2026-07-11

### Added
- **Desktop: sidecar update channel.** The desktop app can now update its frozen
  Python core (`evi-server`) independently of the Tauri shell. On launch it
  background-fetches a **minisign-signed** manifest and, if a newer,
  ABI-compatible sidecar exists, downloads + verifies (minisign + sha256) + stages
  it — applied on the **next launch**, with last-known-good rollback if it fails
  `--check`. So core updates can arrive at PyPI cadence without a full 3-OS app
  rebuild. Opt out with `EVI_SIDECAR_UPDATE=0`. (This release establishes the
  channel; it's a no-op until a newer sidecar is published to it.)

### Documentation
- **A2A (Agent2Agent) documented.** Added a *Federation → A2A* section to
  `docs/configuration.md` (the adapter itself shipped in 1.0.3): the public
  agent-card, the `[federation] a2a = true` `POST /a2a` JSON-RPC endpoint, and the
  `delegate_a2a` tool. Also recorded the OS code-signing research (Windows free via
  SignPath Foundation; macOS notarization needs a paid Apple Developer ID).

## [1.0.9] — 2026-07-11

### Fixed
- **Linux desktop build restored + a 250 MB sidecar bloat removed.** 1.0.7's
  desktop `claude-agent` bundling used PyInstaller `--collect-all claude_agent_sdk`,
  which pulled in the SDK's vendored `_bundled/claude` CLI (~250 MB) — that broke the
  Linux AppImage bundler (`failed to run linuxdeploy`) and bloated every platform's
  sidecar. The freeze now collects only the SDK's Python modules; the SDK falls back
  to the system `claude` CLI (already required for `claude_agent`), so nothing is
  lost. (No PyPI-visible change; this is a desktop-build fix.)

## [1.0.8] — 2026-07-11

### Fixed
- **Honest model identity for non-local backends.** eVi's system prompt always
  described itself as "running the local open-weight model `X`" and denied being
  Claude/GPT/Gemini — false for the CLI-agent and online backends, where the model
  genuinely is proprietary (e.g. `claude_agent` → Claude Opus, which reported "I'm
  eVi, running the local open-weight model `opus`"). The identity is now
  backend-aware: the anti-hallucination wording applies only to local open-weight
  backends (`ollama`/`lmstudio`/`llamacpp`); for `openai_compat` and the CLI-agent
  backends eVi names the model honestly and no longer claims to be open-weight or
  purely local.
- **Linux desktop AppImage build hardened.** Set `APPIMAGE_EXTRACT_AND_RUN=1` /
  `NO_STRIP` so AppImage bundling doesn't intermittently fail with "failed to run
  linuxdeploy" on GitHub's FUSE-less ubuntu runners.

## [1.0.7] — 2026-07-11

### Fixed
- **CLI-agent backends no longer show a false "isn't running" warning.** The
  backend health check probed for a reachable HTTP port — which the CLI-agent
  backends (`claude_agent`/`codex`/`gemini`/`amp`/`qwen`/`copilot`) don't have — so
  selecting one flagged it as down and offered to switch you back to Ollama. It now
  checks whether that backend's CLI is on PATH instead, and the banner (only when
  the CLI is genuinely missing) tells you to install and log in to that CLI rather
  than pointing you at a local server.
- **`claude_agent` now works in the packaged desktop app.** The Claude Agent SDK
  (`claude-agent-sdk`) is bundled into the frozen sidecar, so the `claude_agent`
  backend works in the desktop build too (it still needs the `claude` CLI installed
  and logged in on the machine, as the other CLI-agent backends need their CLI).

## [1.0.6] — 2026-07-11

### Fixed
- **Add CLI-agent backends from Settings.** The web/desktop **Settings → Model &
  Backend** "custom…" kind dropdown now lists the six CLI-agent kinds
  (`claude_agent`, `codex`, `gemini`, `amp`, `qwen`, `copilot`) alongside the
  local server kinds — previously only `openai_compat`/`ollama`/`lmstudio`/
  `llamacpp` were selectable, so these subscription/login backends could only be
  added from the `evi backend add --kind` CLI. Selecting a CLI-agent kind hides
  the URL + API-key fields (they need neither) and shows a reminder to install and
  log in to that CLI first.

### Changed
- **Desktop now auto-follows the core.** Every PyPI `v*` release also cuts the
  matching `desktop-v*` build (`release.yml` calls `desktop-release.yml`), with the
  desktop version derived from the tag, so the desktop app no longer lags the
  package. (1.0.6 is the first release built this way.)

## [1.0.5] — 2026-07-11

### Added
- **Sourcegraph Amp CLI backend — your Amp subscription (no model API key).** Adds
  an `amp` backend over the local **`amp` CLI**, authenticated by `amp login` (an
  Amp subscription / credit balance) or an `AMP_API_KEY` access token. eVi runs
  `amp -x --stream-json` and streams the reply; the model is chosen by **agent
  mode** (`medium`/`low`/`high`). A chat/delegate provider — Amp is autonomous and
  can use tools per your `amp permissions`. Because an unauthenticated `amp` opens
  an interactive browser login that would block, eVi **refuses to start Amp without
  evidence of auth** (`AMP_API_KEY` or a saved login) and bounds each turn with a
  timeout. Requires `npm i -g @sourcegraph/amp`.
- **Qwen Code CLI backend — free Qwen OAuth (no API key).** Adds a `qwen` backend
  over **Qwen Code** (Alibaba's gemini-cli fork): sign in free with a qwen.ai
  account (~2000 req/day). eVi runs `qwen -p … -o json` (Claude-Code-style events)
  and streams the reply — chat/delegate, like `gemini`; models `qwen3-coder-plus` /
  `qwen3-coder-flash`. Requires `npm i -g @qwen-code/qwen-code` + a one-time login.
- **GitHub Copilot CLI backend — your Copilot subscription (no model API key).**
  Adds a `copilot` backend over the local **`copilot` CLI** (`@github/copilot`),
  authenticated by `copilot login`. eVi runs `copilot -p … --output-format text -s`
  and streams the reply; `--model auto` lets Copilot pick (or `claude-sonnet-4.5` /
  `gpt-5`, plan-dependent). Chat/delegate — unapproved tool calls are auto-denied
  in non-interactive mode, so a chat turn stays answer-only. Requires
  `npm i -g @github/copilot`.

### Changed
- **Shared Claude-Code stream-json parser in the CLI-agent shim.** `amp` and `qwen`
  both speak Claude Code's `stream-json` event schema (`assistant` text blocks + a
  terminal `result` with usage/error), so the parser now lives once in
  `evi/llm/cli_agent.py` (`emit_claude_events` + `cc_usage`/`cc_error_message`) and
  is shared by both. Six subscription/free-login CLI backends now ride the shim:
  `claude_agent`, `codex`, `gemini`, `amp`, `qwen`, `copilot`.

## [1.0.4] — 2026-07-10

### Added
- **Codex CLI backend — OpenAI Codex via your ChatGPT plan (no API key).** Adds a
  `codex` backend that talks to Claude-Code's counterpart, the local **`codex`
  CLI**, authenticated by `codex login` (ChatGPT Plus/Pro/Business) — no
  `OPENAI_API_KEY`. Pick it in **Settings → Model & Backend** (kind `codex`, no
  URL/key) or `evi backend add codex --kind codex`; models are `gpt-5-codex` /
  `gpt-5`. Codex is an autonomous agent that runs its **own** tools, so it's a
  chat/delegate provider (eVi's tools don't route through it; it runs read-only so
  a chat turn can't edit files). Requires `npm i -g @openai/codex` + `codex login`.
- **Gemini CLI backend — Google Gemini via the free login (no API key).** Adds a
  `gemini` backend over the local **`gemini` CLI**: a Google-account login gives a
  generous free tier (~1000 req/day) with no `GEMINI_API_KEY`. Pick it in
  **Settings → Model & Backend** (kind `gemini`, no URL/key) or `evi backend add
  gemini --kind gemini`; models `gemini-2.5-pro` / `gemini-2.5-flash`. Runs
  `gemini -p … -o json` (chat/delegate provider, like `codex`). Requires
  `npm i -g @google/gemini-cli` + a one-time `gemini` login.

### Changed
- **Shared CLI-agent shim (`evi/llm/cli_agent.py`).** Factored the reusable core
  out of the `claude_agent` backend — the OpenAI-shaped chunk builders, the
  async/subprocess→sync bridge, the `chat.completions` client shell, and the
  `render_transcript` helper are now generic and driven by a small per-CLI
  "driver". `claude_agent` (SDK driver), `codex` (`codex exec --json` subprocess),
  and `gemini` (`gemini -o json` subprocess) are thin consumers, so further
  subscription/free-login CLIs are cheap to add. No behavior change to
  `claude_agent` (re-verified end-to-end on a Max plan).

## [1.0.3] — 2026-07-10

### Added
- **Claude via your Max/Pro plan — no API key (`claude_agent` backend).** Adds a
  new backend that talks to Claude through the local **`claude` CLI** (the Claude
  Agent SDK), authenticating with your Claude subscription login instead of an
  `ANTHROPIC_API_KEY`. Pick it in **Settings → Model & Backend** (kind
  `claude_agent`, no URL/key) or `evi backend add claude --kind claude_agent`;
  models are the `opus`/`sonnet`/`haiku` aliases the CLI resolves to your plan's
  current models. **Full tool-calling parity:** eVi keeps driving its own tools
  (permissions, checkpoints, mode-scoped toolsets, tool-activity UI all work) —
  the SDK is coerced into proposing one tool call at a time, which eVi executes.
  Requires `pip install 'evi-assistant[claude-agent]'` + the `claude` CLI logged
  in. Under the hood: a stateless per-turn shim (`evi/llm/claude_agent.py`) that
  adapts the SDK's async agent loop into the OpenAI `chat.completions` streaming
  surface the agent loop already speaks, so CLI / web / headless / ultracode /
  subagents all use it unchanged.

### Added
- **Multiple model backends at once.** eVi now keeps a registry of backends
  (`~/.evi/backends.json`) — local (ollama / lmstudio / llamacpp) and online via
  presets (openai / xai / anthropic / openrouter / groq / together, base URL
  pre-filled, just supply a key — inline or an `env:VARNAME` reference). The
  **model picker aggregates models across all of them**, each tagged with its
  **source backend** (hover shows the provider); selecting a model also switches
  the active backend. Manage in **Settings → Model & Backend** (add from a preset
  or a custom endpoint, per-backend **enabled** + **fan-out** toggles, remove) or
  via `evi backend list/add/remove/use` / the `/api/backends` endpoints. A
  per-backend **"allow for subagent fan-out"** flag marks which providers' models
  may serve delegated subagents (the eligible pool for multi-model fan-out).
  `[llm]` stays the materialized active backend, so nothing else changed underneath.
- **Multi-backend fan-out for ultracode.** With `[ultracode] fanout = true`, the
  parallel SOLVER angles are spread — round-robin, interleaved by provider —
  across every model on backends flagged **fan-out** in the registry, so one run
  can use several providers at once (a big cloud model for some angles, locals for
  others). Opt in per backend via the Settings → Model & Backend "fan-out"
  checkbox (or `evi backend add --fanout`); no-op if none are flagged. Built on a
  new `build_agent(backend=…)` that binds an agent to a specific registry backend.
- **A2A (Agent2Agent) adapter — interop with any standards-compliant agent.**
  Complements eVi's own federation (the zero-dep *private fast path* for your own
  eVis) with the *interop path*:
  - **Agent Card** at `/.well-known/agent-card.json` — a spec-compliant A2A
    `AgentCard` (protocolVersion, capabilities, skills, securitySchemes) that also
    carries eVi's model capability flags under an `x-evi` extension. Served
    always, auth-exempt (discovery is public).
  - **`POST /a2a`** JSON-RPC endpoint — `message/send`, `tasks/get`,
    `tasks/cancel`. Off unless `[federation] a2a = true`; bearer-token-gated and
    run non-interactively (tools not auto-approved are denied), same as
    `/api/federate`. Streaming (`message/stream`) + push are not implemented yet
    (the card advertises `streaming: false`).
  - **`delegate_a2a` tool** — eVi can call *any* external A2A agent by its
    JSON-RPC URL.
- **Federation capability discovery (the A2A Agent-Card idea, applied locally).**
  `/api/health` now carries model capability flags (vision / tools / reasoning /
  audio) so a LAN scan / `check_peer` learns what a peer can do in one request,
  and a new **`list_peers`** tool lets the model pick the right peer *before*
  `delegate_peer` (e.g. route a vision task to the peer whose model has vision).

### Fixed
- **Switching the model mid-session now actually registers.** The system prompt
  (which carries the model identity) was composed once at session start and
  frozen, so after a model switch the assistant kept reporting the *old* model.
  Every switch path (`/api/model-picker`, `/api/backend/use`, the `/model`
  command) now re-stitches the prompt via a new `Agent.refresh_prompt()`. The
  web header (`model=…`) also refreshes on switch instead of showing the model
  from page load.

## [1.0.1] — 2026-07-01

### Fixed
- **Model identity.** The system prompt now names the running local model and
  explicitly disclaims being GPT-4 / ChatGPT / Claude, so local models stop
  answering "I'm based on OpenAI's GPT-4" when asked what they are. It also nudges
  the model against firing tools for greetings / small talk.
- **No more raw tool-call JSON in the chat.** When a local model emits a tool call
  as a text JSON blob (common with qwen via Ollama), eVi recovers it as a real
  tool call and no longer flashes the raw `{"name": …}` JSON as a chat message.
  Genuine JSON replies still render (held, then flushed if not a tool call).

## [1.0.0] — 2026-07-01

**eVi 1.0 — first stable public release.** The project is now developed in the
open at [`evi-assistant/evi-ai`](https://github.com/evi-assistant/evi-ai) (MIT).
No breaking API changes from 0.40.0 — 1.0.0 marks stability, a public repo, and a
coordinated launch across the PyPI package, the desktop app, and the
[`evi-skills`](https://github.com/evi-assistant/evi-skills) catalog.

Everything from the 0.34–0.40 line is in 1.0: specialty SLMs + capability chips
(vision/thinking/infill/audio/tools/guard/embeddings), the guard-model guardrail
layer, the models.dev catalog, the config linter (`evi lint`), completion
notifications, pluggable web search, `evi skill add`, the project-intelligence
pack (anatomy map, bug ledger, session reflection), the VS Code extension, local
FIM completion, federation, ultracode, and the full CLI/web/desktop parity set.

### Changed
- `Development Status` classifier → Production/Stable.
- Desktop releases build the full Windows/macOS/Linux matrix and serve the
  in-app updater directly from the public repo (the private release-mirror
  channel is retired).

### Fixed
- Flaky `test_url_hook_non_2xx_vetoes` (HTTP hook test now drains the request
  body before responding, avoiding an intermittent connection-reset race).

## [0.40.0] — 2026-06-17

### Added — project-intelligence pack (feature-scan Batch C)
- **Project anatomy map** — `evi anatomy [--write]` builds a token-estimated file
  map (git-aware) so the agent budgets reads instead of blindly opening files.
  Written to `.evi/anatomy.md` and auto-injected into project context when present
  (`evi/anatomy.py`).
- **Bug-fix ledger** — per-project `.evi/bug-ledger.jsonl` with `record_fix` /
  `search_fixes` tools so the agent checks past fixes before retrying a repair
  (`evi/bugledger.py`).
- **Session reflection** — `evi reflect` distills durable preferences/corrections
  from recent sessions into long-term memory (`evi/reflect.py`); model call is
  injected so it's testable.

### Fixes (from an adversarial review of this batch)
- reflect: robust JSON extraction (handles brackets in surrounding prose), name
  slugification (non-slug names normalised, not dropped), and no clobbering of a
  hand-authored memory on a name collision (+ in-batch dedupe).
- anatomy: git mode now honours the same ignore filter as the walk (drops `.evi/`
  & binaries), keeps non-ASCII filenames (`core.quotepath=false`), no longer
  drops files merely *named* like an ignored dir, and flags char-cap truncation.

## [0.39.0] — 2026-06-17

### Added — models.dev catalog + config linter (feature-scan Batch B)
- **models.dev catalog** — `evi/modelsdev.py` consults a model-metadata catalog
  for ground-truth capability flags + context window + (hosted) pricing. A baked
  snapshot (`evi/data/models-catalog.json`) ships for offline use; `evi models
  refresh` downloads the full live catalog to `~/.evi/models-catalog.json`.
  `capabilities()` and `recommend.context_window_for()` now prefer the catalog
  and fall back to the existing heuristics for any model it doesn't list (exact +
  canonical id match only — no loose substring matching that could mis-resolve).
- **Config linter** — `evi lint` validates authored resources (skills, hooks,
  commands, guardrails, agents): missing SKILL.md `description`, oversized
  bodies, broken file refs, typo'd hook events, unparseable agents.toml. Reuses
  the existing per-resource validators. `evi lint --path ./skills` is the CI gate
  for an evi-skills repo (`evi/configlint.py`).

## [0.38.0] — 2026-06-17

### Added — Tier-1 fold-ins (from the AI-dev-tool feature scan)
- **Completion notifications** — `evi/notify.py` + `[notify]` config (off by
  default). The CLI pings on turn-done (sound + native toast on macOS/Linux +
  optional ntfy/webhook URL); the web/desktop UI fires a browser Notification
  via a 🔔 toggle when the tab is backgrounded. Walk away from long local turns.
- **check-on-edit** — `[tools] check_on_edit` runs the linter after a write and
  folds diagnostics into the tool result (cheap LSP-lite feedback), alongside
  `format_on_edit`. `codeintel.diagnose` now keys off the linter exit code so
  clean banners ("All checks passed!") aren't mistaken for findings.
- **Pluggable web search** — `[tools] search_backend = ddg | searxng | ollama`;
  SearXNG is the fully self-hosted, keyless option. DuckDuckGo stays the default.
- **`evi skill add <name|git|zip|dir>`** — one-line skill installer; the
  marketplace index now carries a `skills` section (`load_skill_index`).

### Security / fixes (from an adversarial review of this batch)
- Skill `.zip` install no longer requires `plugin.toml` (extract + find SKILL.md);
  download/unzip is now a shared `plugins.download_and_unzip` helper.
- `git clone` for skill/plugin sources now uses `--` and rejects `-`-leading
  sources (closes an option-injection vector via a malicious/MITM'd index).
- Multi-skill repos require `--name` to disambiguate instead of silently
  installing whichever sorts first; plugin download errors surface as `SkillError`.

## [0.37.0] — 2026-06-17

### Added — specialty SLMs + capability chips
- **Safety-guard model layer** 🛡 — a new `[[guard]]` guardrail kind backed by a
  dedicated generative guard model (Llama Guard 3 / ShieldGemma, set via
  `[models] guard`). It's the 4th guardrail layer (regex → judge → classifier →
  guard), classifies each turn against the model's built-in safety taxonomy, and
  fails *open* like the others. New `evi/guardmodel.py`; a 🛡 capability chip
  marks guard models in the picker so they're not mistaken for chat models.
- **Embeddings / reranker chip** ◆ — the picker now flags embedding and
  cross-encoder/reranker model families (`evi/embedcap.py`), so a
  nomic-embed / bge-reranker id reads as a model *class*, not a chat model.
- **Speaker diarization** — `evi/diarize.py` + `evi voice diarize <audio>`
  ("who spoke when", pyannote.audio). Optional `[diarize]` extra; degrades
  gracefully when the deps/HF-token are missing.
- **Document layout / OCR** — `evi/doclayout.py` + `ocr_image(engine="doc")`
  (Docling: layout-aware PDF/scanned-doc → Markdown). Optional `[doc]` extra.
- `evi models specialty` now manages `guard` / `diarize` / `doc_layout` too.

## [0.36.1] — 2026-06-17

### Added
- **Tool-calling capability chip** 🔧 — the model picker (web + status bar) now
  shows whether the selected model's family is known to do OpenAI-style
  function calling. eVi is an agent: a model that can't tool-call silently
  produces prose instead of acting, and there was no signal for it. Detection
  lives in `evi/toolcalling.py` (known-good families, with anti-hints so
  base/FIM/embedding/guard models don't false-positive), wired through
  `capabilities()` like the vision/thinking/infill/audio chips.

## [0.36.0] — 2026-06-17

### Added — last parity gaps closed
- **Skill tool-scoping** — a SKILL.md may declare `allowed-tools` /
  `disallowed-tools`; while that skill is active the agent's toolset is scoped
  accordingly (`evi/skillscope.py`), and a stray out-of-scope call is refused.
  This was the last buildable Claude Code parity gap.
- **Channels → live session** — `POST /api/session/{id}/channel` with
  `run: true` drives an immediate agent turn on the session (webhook can act
  now), not just a note for the next turn.
- **Packaged CI action** — ready-to-copy `examples/github/pr-review.yml` using
  the existing `evi-run` composite action (PR review → comment).

### Validated
- The 0.35.0 **Python 3.13** floor is now verified locally too (fresh 3.13
  venv: full suite green, **fast-walk active** for `python_symbols`), in
  addition to CI on 3.13.

## [0.35.0] — 2026-06-16

### Changed (breaking)
- **Minimum Python is now 3.13** (was 3.11). Dropped the `tomli` fallback;
  updated CI, Dockerfile (`3.13-slim`), install scripts, the Tauri launcher,
  and docs. **0.34.x is the last 3.11/3.12-compatible release.** Recreate a
  local dev venv with 3.13 (`py -3.13 -m venv .venv`).

### Added
- **`python_symbols` tool** — AST outline of a Python file (functions/classes/
  methods/imports) for fast code navigation (`evi/pyanalyze.py`).
- **Optional `[ast]` extra** — Reflex **fast-walk** (Rust `ast.walk`, 3.13+
  wheels) accelerates `python_symbols`; `evi/pyanalyze.py` falls back to stdlib
  `ast.walk` when it isn't installed. `pip install 'evi-assistant[ast]'`.
- **eVi VS Code extension — Phase 3 polish** (`editors/vscode/`): status-bar
  item shows reachability + autocomplete on/off + active model (click to
  toggle), 30 s re-check, and a graceful "server not reachable — run evi web"
  prompt.

## [0.34.1] — 2026-06-16

### Fixed
- **run_python in the desktop app** — in the frozen sidecar `sys.executable` is
  `evi-server.exe`, so `run_python` ran the server binary
  (`evi-server: error: unrecognized arguments: …snippet.py`) instead of Python.
  It now finds a real interpreter on PATH when frozen, with a clear error if
  none. (run_python is for quick scripts, not GUI/long-running apps — write the
  file or use the shell tool for those.)

### Added
- **Model capability indicators** — the web model-picker shows capability chips
  (👁 vision · 🧠 thinking · ⌨ infill · 🎤 audio · ☁ Responses-API) on the footer
  chip and per model in the popover; new `evi/capabilities.py` + `capabilities`
  in `/api/model-picker`.
- **Federation receiver indicator** — `/api/federate` records inbound peer
  activity; the footer shows a ⇄ "serving a peer request (host)" pill; the
  dispatch snapshot exposes `federation: {active, recent}`; `delegate()` sends
  the sender's hostname.
- **eVi for VS Code** (`editors/vscode/`) — a local Tab (inline FIM) + chat
  extension over `/api/complete` and `/api/chat`. Run with F5 or package to VSIX.

## [0.34.0] — 2026-06-16

### Added — specialty SLMs, working folder, opencode + Cursor gleanings

- **Specialty-model framework** — `[models]` registry (ocr/vision/stt/tts) +
  `SpecialtyRegistry` so a small dedicated model handles a task without
  swapping the main model. `describe_image` tool + OCR-VLM routing in
  `ocr_image` (falls back to tesseract); `evi models specialty list/set/clear`.
- **Voice models** — Kokoro-82M TTS engine (CPU real-time, Apache); STT default
  reads `[models] stt` (e.g. `large-v3-turbo`).
- **Working folder** — per-session cwd (`evi.workdir`): `/cd`, `evi chat --cwd`,
  web `📁` chip + `/api/session/cwd`. File tools resolve relative paths against it.
- **Shell tool** — `run_command` (opt-in `[tools] shell`, permission-gated),
  wiring the previously-dangling `shell` category.
- **Editing** — `apply_patch` (multi-hunk SEARCH/REPLACE in one call);
  `[tools] format_on_edit` (ruff/black/prettier/gofmt/rustfmt); `check_file`
  diagnostics (ruff/eslint/go vet/clippy) — LSP-lite, no server.
- **Local FIM completion** — `evi/complete.py` + `evi complete` + `/api/complete`:
  eVi as a fully-local Tab/Copilot backend for an editor extension.
- **Plan/build toggle** — `/plan on|off` (persistent read-only mode).
- **`evi init`** — scaffold AGENTS.md/EVI.md (discovery already merges up the tree).
- **Bugbot-style review** — `evi review` loads `.evi/BUGBOT.md` + learned rules
  (`evi review-remember`), tags findings by severity; composes with --json/--exit-code.
- **Federation guard** — `evi doctor` / `evi peer scan` warn when serving but
  loopback-bound (the desktop-0.2.14 trap).

### Notes

- Desktop **0.2.16** shipped separately (bundles the 0.33.0 batch). A 0.2.17
  with this batch follows. SLM models are user-pulled (e.g. `ollama pull
  moondream` / `glm-ocr`); eVi wires the integration, config, and tools.

## [0.33.0] — 2026-06-16

### Added — Claude Code parity, S/M batch

Closes the buildable small/medium gaps from the
[comparison](docs/claude-code-comparison.md):

- **Model fallback chain** — `[llm] fallback_models` retries a turn against the
  next model when the primary fails at setup (timeout / 5xx / not loaded).
- **Extended thinking off** — `reasoning_effort = "off"` (and `/effort off`)
  disables thinking; logic centralized in `reasoning.py`.
- **Transcript retention** — `tools.cleanup_period_days` auto-prunes old
  transcripts on startup; `evi sessions purge [--older-than N]` does it manually.
- **Transcript search** — `evi sessions search <query>` with snippets.
- **MCP output cap** — `tools.mcp_max_output_chars` truncates a chatty server's
  result before it reaches the model.
- **Conditional hooks** — `arg_match` gates a hook on tool arguments (e.g.
  `arg_match = { path = "*.env" }`), not just the tool name.
- **Session lifecycle hooks** — `session_start` / `session_end`.
- **CI-gating review** — `evi review --exit-code` / `--json` (+ `/ultrareview`)
  for gating a build on a clean verdict.
- **Plugins** — `evi plugin init` scaffold; install from `.zip` (file or URL);
  enabled plugins' `bin/` on PATH; nested (subfolder) skill discovery;
  `/reload-skills`.
- **`/add-dir`** — trust an extra directory for the session.
- **`!cmd`** — run a shell command from the REPL; output folded into context.
- **`ask_user` tool** — clarifying questions (AskUserQuestion parity);
  interactive-only, a graceful no-op in web/headless.
- **`worktree.base_ref`** — default fork point for `evi worktree create`.
- **Usage by category** — `evi stats` attributes tool calls per category.

### Notes

- Desktop **0.2.15** shipped separately (updater ACL fix + federation LAN-bind);
  install it manually once — 0.2.14's updater can't self-update.

## [0.31.0] — 2026-06-08

### Added — Claude Code parity, round 2 (phases 75–78)

From the [eVi vs Claude Code comparison](docs/claude-code-comparison.md):

- **Skills in plugins** (Phase 75) — plugins now bundle skills as well as
  commands (scanned as `<plugin>:<skill>`); `evi plugin list` shows both counts.
- **Nested project context** (Phase 76) — `EVI.md`/`AGENTS.md` are merged from
  every ancestor directory (root → cwd), so monorepos get layered context.
- **Trusted directories + domains** (Phase 77) — `auto.trusted_dirs` /
  `auto.trusted_domains` auto-approve file tools under a path or web fetches to a
  host, without opening a whole tool category (explicit deny rules still win).
- **MCP server allowlist** (Phase 78) — `[tools] mcp_allow` restricts which
  mcp.json servers load, so a shared/synced config can be gated per machine.

Desktop → 0.2.12.

## [0.30.0] — 2026-06-08

### Added

- **Customizable status line** (Phase 72) — `[statusline] enabled` prints a dim
  status line above each REPL prompt; customize via a `format` template
  ({model}/{pct}/{branch}/{goal}/…) or a `command` that gets the state as JSON
  on stdin. Off by default.
- **Routines / webhook triggers** (Phase 73) — `evi routine add <name>
  --recipe <r>` mints a token; `POST /api/routine/<token>` runs that recipe
  headless (auth = the path token; restricted tool perms unless `--yes`). Lets
  any external service trigger an eVi workflow over HTTP.
- **Project-level config** (Phase 74) — a repo-local `.evi.toml` (walked up from
  cwd) overlays the user config + active profile (project wins), so a repo can
  pin its own model/tools/permissions. Project context now also recognises
  **AGENTS.md** (EVI.md still wins).

Desktop → 0.2.11.

## [desktop 0.2.10] — 2026-06-08

### Fixed — "Command … not allowed by ACL" on desktop IPC

The desktop window loads the local server over `http://127.0.0.1:<port>`, which
Tauri treats as a *remote* URL — and a capability doesn't apply to remote pages
unless they're explicitly allowed. So every frontend `invoke()` (Check for
Updates, Open Logs, Get Support's external open) failed with
`Command <name> not allowed by ACL`. The default capability now allows
`http://localhost:*/*` + `http://127.0.0.1:*/*`. (Menus were unaffected — they
dispatch via Rust `eval`, not `invoke`.) Desktop → 0.2.10; no Python change.

## [0.29.0] — 2026-06-08

### Added — Claude Code parity (phases 64–71)

- **File checkpointing + rewind** — every `write_file` is journalled with the
  file's prior state; `evi rewind` / `/rewind` / File→Undo File Change restore
  modified files and delete newly-created ones.
- **Headless mode** — `evi run "<prompt>" [--format json] [--mode …] [--yes]`
  for scripts/CI/cron; prints text or a JSON envelope.
- **Permission modes + rules** — `auto.mode` (ask/accept_edits/plan/yolo) and a
  first-match allow/deny rule list (`deny shell rm*`, `deny write_file *.env`…),
  surfaced in Settings → Permissions.
- **Sandboxed code execution** — `run_python` can run under bwrap/sandbox-exec
  (read-only FS, no network) when `[tools] sandbox` is on.
- **Plugins** — `evi plugin add/list/remove` install command bundles from a
  local dir or git; the command loader scans them as `/<plugin>:<command>`.
- **Output styles** — switchable response personas (concise/explanatory/teacher
  or `~/.evi/styles/*.md`) via `[llm] output_style`, `evi style`, and Settings.
- **Multi-agent code review** — `evi review --multi` fans out parallel reviewers
  (correctness/security/performance/tests) and combines them.
- **Session continue/fork** — `evi sessions continue` (resume latest) and
  `evi sessions fork <id>` (diverge into a new session).

Desktop → 0.2.9.

## [0.28.0] — 2026-06-08

### Added

- **Session modes — Chat / Cowork / Code** — a segmented control in the header
  (like Claude Desktop) that gates which tools a session can use: Chat (memory +
  skills), Cowork (+ files, web, calendar, images, PDF), Code (+ code, shell,
  git, subagents). Switching hot-swaps the live agent's tools; the choice
  persists and follows tab switches. New `/api/modes` + `/api/session/{id}/mode`.

### CI

- **desktop-release** now warms the Tauri Windows bundler cache with retries
  before the release build, so transient `http status: 504` failures on the
  bundler's toolchain download self-heal (the Rust compile is cached, so the
  retries are cheap). Desktop 0.2.6/0.2.7 builds were lost to that outage; this
  release supersedes them.

Desktop → 0.2.8.

## [0.27.0] — 2026-06-08

### Added

- **Parallel multi-agent research** — `parallel_research(tasks)` runs up to 6
  read-only Explore subagents at once (one per sub-question) and combines their
  findings for the main model to synthesize.
- **Claude-Code-style custom slash commands** — `~/.evi/commands/*.md` gain
  frontmatter (`description`/`argument-hint`/`model`), `$ARGUMENTS` + positional
  `$1..$9`, `@file` inlining, and subdirectory namespacing (`/git:commit`). The
  legacy `{args}` still works. See [docs/commands.md](docs/commands.md).
- **System stats in Settings → Model & Backend** — OS, GPU, VRAM (total/free),
  RAM, driver + CUDA compute capability, and inference mode (new `/api/system`),
  plus the hardware-recommended model with a one-click Ollama **Pull** (progress
  bar) or **Use**.

### Fixed

- **File → Settings (and other native menu items) did nothing** in the desktop
  app — only the gear worked. The menu used `emit`, but withGlobalTauri's event
  module isn't reliably injected on the remote (localhost) page, so no listener
  was attached. The Rust bridge now `eval`s the JS handler directly.
- **Help → Check for Updates** now shows clear states: checking / up to date /
  downloading.

Desktop → 0.2.7 (0.2.6 was skipped — its build hit a sustained transient CDN
`504` on the Windows Tauri bundler; this release supersedes it).

## [0.26.0] — 2026-06-07

### Added — sync, recipes, memory tags, tool progress

Four roadmap phases (57–60).

- **Cross-machine sync** (`evi sync init/push/pull/status`) — git-backed sync of
  the portable `~/.evi` state (memory, skills, profiles, commands, routes,
  mcp.json, hooks). A managed `.gitignore` keeps per-machine config, secrets
  (`tokens/`), and large/rebuildable data (`models/`, `indices/`) local. First
  pull on a new machine adopts the remote state.
- **Recipes** (`evi recipe new/list/show/run`) — saved multi-turn workflows in
  `~/.evi/recipes/*.toml`, run through one shared conversation so later steps
  build on earlier answers. `run --yes` for unattended runs.
- **Memory tags** — `remember(..., tags="a, b")` + `recall_by_tag`; tags stored
  as an invisible marker, fully backward-compatible with untagged memories.
- **Tool progress heartbeats** — slow tools (web fetch/search, index build) now
  stream `ToolProgress` status to the CLI + web instead of an apparent hang;
  tools flagged `long` announce immediately.

Desktop → 0.2.6.

## [0.25.0] — 2026-06-07

### Added — settings screen, native menus, system tray, in-app docs

A Claude-Desktop-style control surface across the web + desktop frontends.

- **Settings screen** (⚙ / Ctrl+, / File → Settings): General, Model & Backend,
  Tools, Permissions, Context, Integrations, Server, About. Backed by a new
  `GET/POST /api/config` — full snapshot with secrets masked, plus a
  section-patch that hot-reloads live sessions and rebuilds the LLM client on
  backend/model changes.
- **Native menus** (desktop): File / Edit / View / Help with accelerators. Edit
  uses native undo/redo/cut/copy/paste/select-all; View has reload, zoom, theme
  toggle, and **Toggle Developer Tools**.
- **System tray + minimize-to-tray**: closing the window keeps eVi (and its warm
  sidecar) running in the tray — quit via the tray or File → Exit.
- **Force-update**: Help → Check for Updates triggers the signed updater on
  demand (the launch-time auto-check still runs).
- **In-app documentation** (Help → Documentation): renders `docs/*.md` offline
  via a new dependency-free Markdown renderer (`evi/apps/web/mdlite.py`) — no CDN
  needed; docs are bundled into the frozen sidecar.
- **Diagnostics** (Help → Run Diagnostics): `evi doctor` checks in-app, via
  `/api/doctor`.
- **Light theme** + a system/dark/light toggle.

### Testing

New Playwright e2e tests cover the settings screen, the `/api/config`
round-trip, in-app docs, and diagnostics. Desktop → 0.2.5.

## [0.24.3] — 2026-06-07

### Changed — rebranded to **eVi**

The product is now stylized **eVi** (lowercase-e, capital-V, lowercase-i)
everywhere it's shown: the app/installer name (`productName`), window title,
web/PWA title + header, CLI help/output, and the docs. The lowercase `evi`
import package, the `evi` CLI command, `EVI_*` env vars, the `evi-assistant`
PyPI name, and the `evi-assistant/evi-ai` repo are unchanged (code/identifier
surfaces).

> **Desktop note:** because the app's `productName` changed, the eVi installer
> installs to a new location (`…\eVi\`) — it does **not** upgrade an existing
> **Evi** install in place. Do a one-time clean switch: finish/skip the Evi
> update, uninstall **Evi**, then install **eVi** 0.2.4. From eVi onward,
> auto-update works in place again.

### Fixed — desktop updater failed with "Error opening file for writing"

The auto-updater's NSIS installer closed the main app but not the spawned
`evi-server` sidecar, which kept `_internal\*.dll` (e.g. `VCRUNTIME140.dll`)
locked → the install aborted. The updater now **kills the sidecar before
installing**, releasing the locks. Desktop → 0.2.4.

## [0.24.2] — 2026-06-07

### Fixed — chat showed no response (SSE frames split on the wrong separator)

Chat sent your message but never rendered a reply — no assistant bubble, no
error, Send just re-enabled. The frontend split the streamed SSE event frames
on `\n\n`, but `sse-starlette` (3.4.x, pulled into the fresh build) separates
them with `\r\n\r\n`. Since `\r\n\r\n` contains no `\n\n`, the parser found
**zero** frame boundaries and dispatched **zero** events. Both SSE readers (the
chat stream and session re-roll) now split on `/\r?\n\r?\n/` and tolerate CRLF
within frames. Affected all chat (web + desktop) in 0.23.0–0.24.1. Desktop →
0.2.3.

> Slipped through because there's no browser/e2e test of the chat stream — the
> Python tests assert the server *emits* events (parsed by code that handles
> both separators), never that the JS *renders* them.

## [0.24.1] — 2026-06-07

### Fixed — first-run wizard now actually activates the backend it sets up

A fresh Ollama user could finish the setup wizard, watch the "no backend"
banner clear, and still get **no reply** — because the wizard installed Ollama
and pulled a model but never pointed eVi's config at it, so chats kept hitting
the shipped default (LM Studio / a 7B that wasn't installed).

- **New `POST /api/backend/use`** persists `{backend, base_url, model}` to
  config and rebuilds live sessions' clients (works without a restart). When no
  model is given it picks one **actually installed** on the backend (preferring
  the hardware-recommended one), so the config never points at a missing model.
- **The wizard now activates the backend** after the pull: install → start →
  pull → **use** → ready.
- **Banner logic corrected:** it now keys off whether the **configured** backend
  (the one chat uses) is reachable — not "any backend is reachable," which was
  the bug that cleared the warning while chats hit a dead backend. When a
  *different* backend is running than the one configured, the banner offers a
  **"Use <backend>"** button. Message-send is gated on the configured backend.
- **Install feedback:** the silent Ollama install now shows a spinner.
- Desktop app → **0.2.2**.

## [0.24.0] — 2026-06-07

### Added — Phase 53: eVi as an MCP server (`mcp-server-publish`)

eVi has always been an MCP *client*; now it can run as an MCP *server* too, so
other agents (Claude Desktop, Cursor, Cline, Continue) can reach into eVi's
tools. This flips the integration story — instead of bridging into eVi from
each app, the app's existing MCP client connects to eVi.

- **`evi mcp serve`** — runs eVi as an MCP server over stdio, exposing a
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
  shape eVi's local backends (LM Studio/Ollama/llama.cpp) support; `"responses"`
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
- **Web UI wizard:** the banner's "⚡ Set up eVi automatically" button chains
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
  eVi…" spinner polling `/api/health`) added in 0.21.2 still covers any
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
  (127.9 MB, `--check` OK), built both installers (`eVi_0.1.0_x64_en-US.msi`
  59.5 MB, `eVi_0.1.0_x64-setup.exe` 46.0 MB), and confirmed the built
  `evi-desktop.exe` resolves + spawns the sidecar, which serves
  `/api/health` 200 and the no-backend banner.

### Tests

- New `tests/test_portprobe.py`; rewrote `tests/test_backend_status.py`;
  +5 cases in `tests/test_backends.py` for the llama.cpp port fallback.

## [0.21.2] — 2026-05-29

### Desktop — fixed: app launched the sidecar but no window appeared

Running the built app spawned `evi-server.exe` (with a stray console
window) but showed no eVi window. Two `main.rs`/config bugs:

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
  cold-start time, with a "Starting eVi…" spinner and a log-path hint.

Verified headlessly: the app stays alive (no crash), the sidecar binds,
and the root eVi UI serves HTTP 200 — the shim redirects into it. No
console window.

## [0.21.1] — 2026-05-29

### Desktop — standalone build verified end-to-end (Windows)

The self-contained desktop app was built for real on Windows; the
build scripts are no longer "verified-by-construction" only.

- **Verified:** PyInstaller froze a 72.7 MB `evi-server.exe` sidecar
  (`--check` self-test passes; the frozen server boots + answers
  `/api/health`), and `tauri build --config tauri.standalone.conf.json`
  produced `eVi_0.1.0_x64_en-US.msi` (~79 MB) and `eVi_0.1.0_x64-setup.exe`
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
  A standalone eVi Desktop build covers chat, tools, image-gen, the web UI,
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
  bootstrap helper *outside* the package for the programs eVi shells out
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
  calls that need approval, eVi now prompts **once** instead of N times.
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

- **`evi update`** checks PyPI for a newer eVi, snapshots the current
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
  restore the FULL freeze, not just eVi, because a transitive bump
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
  on the listener prevents eVi from transcribing its own TTS echo.
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
  wires eVi to a sibling Ollama container with named volumes.
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
  config block `[obsidian] vault_path = "..."`, `subdir = "eVi"`.
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
