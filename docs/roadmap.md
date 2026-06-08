# Roadmap

Forward plan for eVi. **Shipped** work lives in `CHANGELOG.md` + project
memory; this file is what's *next* and why. Items carry a rough size
(S = an afternoon, M = a day, L = a phase) and a one-line rationale.

- Vendor-SDK feature parity ideas → [sdk-coverage.md](sdk-coverage.md)
- Third-party app/service integrations → [future-integrations.md](future-integrations.md)
- Release mechanics → [releasing.md](releasing.md)

_Last rewritten 2026-06-06 (post-Phase-48). Supersedes the old v0.11.0-era
backlog — most of those "next phase candidates" have since shipped._

## Reality check — shipped through Phase 48 (v0.22.0)

The old roadmap's headline candidates are **done**: self-update (`evi update`,
P29), citations + local rerank (P30), `evi review` (P31), conversation grep
(P32), plus memory/MCP/skills/scheduler/multi-backend/hooks/worktrees/dream/
voice(STT+TTS)/vision/OCR/PDF/SQLite/calendar/routing/auth, the FastAPI+SSE web
UI, and the standalone Tauri desktop bundle (onedir sidecar + "no LLM backend"
UX, P48). CI is green; the desktop-release pipeline exists.

So the backlog below is genuinely forward-looking.

## Planned phases (proposed sequence)

This is a *proposed* order, weighted toward finishing the distribution story
(so new users have a smooth install → first-chat → stay-updated path) before
expanding surface area.

### Phase 49 — Supply-chain hygiene / vuln checking — **✅ shipped (0.23.0)**

Make dependency vulnerabilities visible and gated in CI.

- `pip-audit` (OSV-backed) CI job for the Python deps.
- `cargo-deny check advisories` (RustSec) for the desktop crate.
- `.github/dependabot.yml` for pip + cargo + github-actions (weekly, grouped,
  Conventional-Commit prefixes).
- Enable GitHub-native **Dependabot alerts + security updates** (free on
  private repos). NOTE: CodeQL code-scanning and secret-scanning are **not**
  free on private repos (need paid GitHub Advanced Security) — deferred; a
  self-hosted `gitleaks`/CodeQL-CLI step is a possible later add.

### Phase 50 — Frictionless first run (time-to-first-chat) — **✅ shipped (0.23.0)**

A fresh user has no LLM backend → today they hit the "no backend" banner.
**Decision (from research): do NOT bundle a runtime** — the multi-GB *model*
download is the real blocker, bundling balloons the installer 5–10×, and eVi
is already built around Ollama. Instead:

- True **one-click Ollama install** in `/api/backend/start` (winget /
  silent installer on Windows, cask/dmg on macOS, official `install.sh` on
  Linux) → existing auto-`serve` → existing `pull_model` SSE progress.
- **First-run wizard**: detect hardware via `recommend(hw)`, auto-pull a small
  sensible default (`qwen2.5:3b-instruct-q4_K_M`, ~1.9 GB — best local
  tool-calling at that size), "running on CPU (slow)" hint, "upgrade to 7B
  later" as a secondary action. Chat unblocks the moment the pull finishes.
- **vLLM: excluded** for first-run (GPU/CUDA/Linux server-grade; not a desktop
  fit). It already works as a *remote* OpenAI endpoint via the generic
  `openai_compat` backend — no work needed.
- (If we ever do bundle: ship llama.cpp's `llama-server` **Vulkan** build —
  small, clean MIT, one binary for cross-vendor GPU + CPU; avoid the 373 MB
  CUDA pack and Ollama's redistribution notice debt.)

### Phase 51 — Desktop auto-update against GitHub releases — **✅ shipped (desktop 0.2.0)**

The CLI/pip path already self-updates (`evi update` → PyPI). The **desktop**
app (not pip-installed) needs its own updater pointed at the
`desktop-release.yml` GitHub releases we now build.

- Adopt Tauri 2's **`@tauri-apps/plugin-updater`** + `tauri-plugin-process`:
  checks a release endpoint, downloads, verifies, relaunches.
- Requires an **updater signing keypair** (Tauri minisign — free, separate
  from OS code-signing). `desktop-release.yml` gains the signing step + a
  `latest.json` manifest attached to releases.
- Pairs with — but does not require — **OS code-signing** (Authenticode /
  Apple Developer ID) to silence SmartScreen/Gatekeeper. Code-signing needs
  paid certs → track separately; the updater works unsigned-by-OS today.

### Phase 52 — Crash / error reporting — **✅ shipped (0.23.0)**

Opt-in, privacy-first telemetry so we learn about crashes.

- `sentry-sdk` behind a thin **`Reporter`** abstraction (swappable backend via
  one DSN/config value), pointed at **self-hosted GlitchTip** (OSS,
  Sentry-API-compatible) — or hosted Sentry free tier for zero-infra start.
- Hooks: CLI `sys.excepthook`, FastAPI exception handler, Tauri Rust panic
  handler (+ community `tauri-plugin-sentry`); the frozen sidecar reuses the
  CLI path.
- **Opt-in (default OFF)** config flag + env override; a **shared scrubber**
  (`before_send`) that strips home/user paths, env (allowlist only),
  API keys, hostnames, and — critically for an AI app — **prompt/exception
  content + frame locals**.
- "Open a GitHub issue on crash" stays a documented **Plan B** behind the same
  interface — only viable via a token-holding serverless relay (no shippable
  token), and re-implements dedup/rate-limit/scrub that Sentry gives free.

### Phase 56 — Desktop UX: settings, menus, tray, in-app docs — **✅ shipped (0.25.0 / desktop 0.2.5)**

A Claude-Desktop-style control surface (the "UI enhancements" detour).

- **Settings screen** (⚙ / Ctrl+, / File→Settings) backed by `GET/POST /api/config`
  (masked secrets; section-patch that hot-reloads live sessions).
- **Native menus** (File/Edit/View/Help + accelerators, dev tools) and **system
  tray** with **minimize-to-tray**; **force-update** via Help→Check for Updates.
- **In-app docs** (`/api/docs` + dependency-free `mdlite.py`, bundled offline) and
  **diagnostics** (`/api/doctor`); **light theme** toggle.
- Public docs **wiki** mirror on `evi-ai-releases`; Playwright e2e for the lot.

## Larger backlog (unsequenced — L unless noted)

- ✅ **MCP-server-publish** — **shipped (0.24.0, Phases 53–54)**: `evi mcp serve`
  exposes eVi's tools (memory/index/calendar/git by default) **+ memory
  resources + command prompts** as an MCP server for Claude Desktop / Cursor /
  Cline / Continue. Transports: stdio + streamable **HTTP** (`--http`, bearer
  `--token`); per-tool allow-list (`--tools`). Remaining nice-to-haves: an
  OAuth flow for HTTP (vs static token), and exposing index/calendar data as
  resources too.
- ✅ **Responses API** — **shipped as opt-in (0.24.0, Phase 55)**, NOT a
  migration: `[llm] api = "responses"` (default `"chat"`) routes the agent loop
  through OpenAI's Responses API for endpoints that support it, via a stream
  adapter that keeps the loop unchanged. Local backends stay on Chat
  Completions. Remaining: verify against a live Responses endpoint; extend the
  compaction/variant helpers (still chat-only — fine for OpenAI cloud).
- ✅ **Cross-machine sync** of `~/.evi/` — **shipped (0.26.0, Phase 57)**:
  `evi sync init/push/pull/status` over a git remote. Syncs
  memory/skills/profiles/commands/routes/mcp/hooks; a managed `.gitignore` keeps
  per-machine config, secrets, and large/rebuildable data local.
- ✅ **`evi recipe`** — **shipped (0.26.0, Phase 58)**: saved multi-turn
  workflows under `~/.evi/recipes/*.toml`, run through one shared conversation
  (`evi recipe new/list/show/run`, `--yes` for unattended).
- ✅ **Memory tags** — **shipped (0.26.0, Phase 59)**: tags + `recall_by_tag`
  (invisible marker, backward-compatible with untagged memories).
- ✅ **Background tool execution** — **shipped (0.26.0, Phase 60)**: ToolProgress
  heartbeats for slow tools (CLI + web) instead of an apparent hang; `long=True`
  tools announce immediately.
- ✅ **Parallel multi-agent research** — **shipped (0.27.0, Phase 61)**:
  `parallel_research(tasks)` fans out up to 6 read-only Explore subagents
  concurrently and combines their findings (`run_subagents_parallel`).
- ✅ **Claude-Code-style slash commands** — **shipped (0.27.0, Phase 62)**:
  `~/.evi/commands/*.md` now support frontmatter (description/argument-hint/
  model), `$ARGUMENTS` + positional `$1..$9`, `@file` refs, and subdir
  namespacing (`/git:commit`). See [commands.md](commands.md).
- **Plugin loader** (`~/.evi/plugins/`) — **M**; drop-in user tools.

### Phase 56b — desktop/settings polish — **✅ shipped (0.27.0 / desktop 0.2.7)**

- Fixed File→Settings (and all native-menu→webview actions): the Rust bridge now
  `eval`s `window.eviUI.handleMenu(id)` directly instead of emitting an event
  the remote page wasn't listening for.
- Settings → Model & Backend: a **System** panel (OS, GPU, VRAM, RAM,
  driver/CUDA cc, inference mode) via `/api/system`, plus the
  hardware-recommended model with an Ollama **Pull** button + progress bar.
- Help → Check for Updates shows clear states (checking / up to date /
  downloading).

### Phase 63 — session modes (Chat / Cowork / Code) — **✅ shipped (0.28.0 / desktop 0.2.8)**

A header segmented control (à la Claude Desktop) that gates a session's tool
set: **Chat** (memory + skills), **Cowork** (+ files/web/calendar/images/pdf),
**Code** (+ code/shell/git/subagents). Hot-swaps the live agent's tools; the
choice persists and follows tab switches. `evi/modes.py` + `/api/modes` +
`/api/session/{id}/mode`.
- **Multi-user web mode** — per-user auth/paths/permissions for small teams.
- **Federation / inter-agent protocol** — eVi-to-eVi delegation across machines
  (pairs with profiles + remote backend).
- Smaller: long-context model awareness in the registry; `/recent` prompt
  history in the REPL.

## Claude Code parity phases — ✅ all shipped (0.29.0 / desktop 0.2.9)

A pass over the [Claude Code docs](https://code.claude.com/docs/en/overview)
surfaced these gaps; **all eight shipped in 0.29.0** (see CHANGELOG for details).
Each was a genuine *net-new* capability (eVi already had the agentic loop,
subagents + parallel research, MCP client/publish, hooks, scheduler, skills,
memory + tags, worktrees, routing, guardrails, vuln scanning, voice,
computer-use, custom commands, recipes, and session modes).

- **Phase 64 — File checkpointing + rewind** — **M**. Snapshot files before each
  tool edit; `/rewind` (CLI + web) restores files and/or the conversation to a
  prior point. eVi has conversation edit/branch/reroll but **no file-state undo**
  — the biggest safety gap vs Claude Code's checkpointing.
- **Phase 65 — Headless / print mode** — **M**. `evi -p "prompt"
  [--output-format json|text] [--mode code]` for one-shot scripted/CI/cron use,
  reusing the agent loop. JSON envelope = final text + tool trace + usage. The
  foundation for any eVi automation story (and a thin SDK later).
- **Phase 66 — Granular permissions + permission modes** — **M**. Beyond
  category auto-approve: modes (ask / accept-edits / plan / yolo) and rule-based
  allow/deny (per tool, per path glob, per shell-command prefix, per domain),
  with a `/permissions` view in Settings.
- **Phase 67 — Sandboxed shell** — **M/L**. Run the shell/code-exec tools in a
  sandbox (read-only FS outside the project, no network) by default, opt-out per
  call. OS-specific (bubblewrap/seccomp · sandbox-exec · restricted job object).
  Hardens the riskiest tool.
- **Phase 68 — Plugins + local marketplace** — **L** *(supersedes the pending
  "plugin loader")*. A plugin bundles commands + skills + hooks + subagent
  profiles + optional MCP servers; `evi plugin add <git-url|dir>` + a manifest +
  a curated index. Subsumes much of the integrations backlog.
- **Phase 69 — Output styles** — **S/M**. Switchable response personas (concise /
  explanatory / teacher / reviewer) layered on the system prompt, independent of
  the Chat/Cowork/Code *tool* modes. `~/.evi/styles/*.md` + a picker.
- **Phase 70 — Multi-agent code review** — **M**. Upgrade `evi review` to fan out
  parallel reviewers (correctness / security / perf / tests) via the new
  `run_subagents_parallel`, then synthesize — a local take on Claude Code's
  multi-agent review.
- **Phase 71 — Session resume / fork (CLI)** — **S/M**. `evi --continue`,
  `evi --resume <id>`, `evi --fork <id>` off the transcript store; list saved
  sessions in the web tab bar. Completes session management.

✅ **Shipped in 0.30.0:** customizable **status line** (Phase 72 — format
template or custom command in the CLI REPL); **routines/triggers** (Phase 73 —
`POST /api/routine/<token>` runs a recipe headless); **project-level config**
(Phase 74 — repo-local `.evi.toml` overlay + AGENTS.md recognition).

Still open: **plugin component types beyond commands** (skills/hooks/MCP/subagent
profiles in a plugin) and the integrations backlog below.

**Explicitly not adopting** (philosophy mismatch): cloud/enterprise backends
(Bedrock/Vertex/Foundry), org admin / managed settings, cloud Ultrareview /
Ultraplan, S3/Redis session storage, usage analytics dashboards, and the agentic
browser (already deferred in favour of MCP browser servers). A full public Agent
SDK is deferred too — headless mode (Phase 65) covers the automation need without
committing to a stable library surface.

## Next planned phases (79+)

Releases are paused on a GitHub Actions billing block; these are building
locally and ship once that clears.

- **Phase 79 — in-app update progress toast** — **✅ shipped (local)**: the
  silent auto-updater now shows a progress toast (downloading % → installing).
- **Phase 80 — full plugin components** — **✅ shipped (local)**: plugins now
  bundle hooks (`hooks.toml`) and MCP servers (`mcp.json`, namespaced
  `<plugin>:<name>`) on top of commands + skills. Subagent profiles in plugins
  remain the one planned component type (code-defined dict — a larger change).
- **Phase 81 — HTTP hooks** — **✅ shipped (local)**: a hook can POST its event
  to a `url` instead of spawning a command; non-2xx vetoes a before-hook.
- **Phase 82 — keybindings** — **✅ shipped (local)**: `~/.evi/keybindings.toml`
  maps a key to a slash command in the REPL (press → run).
- **Phase 83 — channels** — push an external alert/notification into a running
  session (routines cover inbound webhook→recipe; this is push-into-live). **M.**
- **Phase 84 — packaged CI action** — a reusable GitHub Action / workflow that
  wraps `evi run` headless. **S/M.**
- **Phase 85 — agent dispatch view** — a dashboard to manage many concurrent
  sessions / subagents. **L.**
- **Phase 86 — dynamic workflows** — a small scriptable multi-agent
  orchestration format (beyond recipes + parallel research). **L.**
- **Phase 87 — cross-device session handoff** — continue a live session from
  another device. **M.**
- **Phase 88 — context-window visualization** — **✅ shipped (local)**: `/context`
  (`/ctx`) in the REPL and a click-the-chip popover in the web UI break tokens
  down by system / you / assistant / tools (`/api/session/{id}/context`).
- **Phase 89 — OpenTelemetry / metrics** — opt-in traces/metrics export. **M.**
- Smaller: long-context awareness in the model registry · `/recent` REPL history
  · deep links (`evi://`).

### Previously-deferred, now planned

- **Phase 90 — fine-tune from transcripts** — curate eVi transcripts into a
  fine-tune dataset (export format + a guided pipeline; training stays
  off-device/optional). **L.**
- **Phase 91 — voice cloning for AutoSpeaker** — optional local TTS voice
  cloning (e.g. F5-TTS / XTTS) behind the existing voice seam. **L.**
- **Phase 92 — CodeQL / secret-scanning** — security scanning on the private
  repo (self-hosted gitleaks + CodeQL CLI, since GHAS isn't free). **M.**
- **Phase 93 — Docker image push** — publish a container image in `release.yml`
  (CLI/web server in a box). **M.**
- **Phase 94 — sigstore wheel signing** — keyless signing of the PyPI wheels for
  supply-chain provenance. **S/M.**

## Integrations backlog

A large, separately-tracked list (Home Assistant, Notion, Spotify, Slack,
native GitHub tool, generic IMAP/SMTP email, RSS, weather, Wikipedia,
YouTube transcripts, Todoist, …) lives in
[future-integrations.md](future-integrations.md). Many become trivial once
**MCP-server-publish** lands (consume existing MCP servers instead of building
each tool by hand).

## Explicitly deferred

- **Agentic browser via Playwright** — deprioritised in favour of MCP browser
  servers (big surface area).
- **Fine-tune eVi from transcripts** — niche; dream engine already curates.
- **Voice cloning for AutoSpeaker** (Bark/Tortoise/F5-TTS) — heavy deps + huge
  models.
- **CodeQL / secret-scanning on the private repo** — not free; revisit if the
  repo goes public or GHAS is purchased (or self-host gitleaks).
- **Docker image push** in `release.yml`; **sigstore** wheel signing (post-1.0).

## Prioritisation note

Distribution polish (49→51) is the current focus: a new user should install,
get to first chat without a manual backend setup, and stay updated — all
without us hand-holding. After that, **MCP-server-publish** is the single
highest-leverage feature (it subsumes much of the integrations backlog).
Beyond that, gather real usage via transcripts + `evi dream` before piling on
more surface area.
