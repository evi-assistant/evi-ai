# Testing

How eVi is tested, and the process for keeping it working as phases land.

## Why this exists

The 0.24.2 bug — **chat rendered nothing** for two minor releases — slipped
through because we had a large unit suite but **zero tests that drive the actual
UI**. The Python tests asserted the server *emits* SSE events; nothing checked
that the browser *renders* them. The fix is a real end-to-end (e2e) layer plus
a standing rule: **every UI-affecting change ships with an e2e test.**

## The layers

| Layer | Tool | Speed | Covers | Runs |
|---|---|---|---|---|
| **Unit** | `pytest` (`tests/*.py`, plus the composite actions' tests in `.github/actions/`) | fast (run `pytest --collect-only -q` for the live count) | core logic, config, tools, backends, server endpoints (via `TestClient`), converters | every push and PR (CI `ci.yml`), every release, + locally |
| **E2E (CLI)** | subprocess (`tests/cli_e2e/`) | medium (~100 s, no browser) | the real `evi` CLI per subsystem — arg parsing, config wiring, file I/O, output (the offline commands) | opt-in (`-m e2e`) |
| **E2E (UI)** | Playwright (`tests/e2e/`) | medium (~25 s + browser install) | the real web UI in a real browser against the real server | PRs, pushes to main, weekly, dispatch, and every release before it publishes (CI `e2e.yml`) |
| **E2E (desktop)** | tauri-driver + msedgedriver (`tests/desktop_e2e/`) | slow (needs a built `evi-desktop.exe`, Windows) | the native shell: the built app launches and renders the UI, the page reaches the shell only through its listed commands (no `window.__TAURI__`) and the window stays on the eVi server, the native menu opens Settings, LAN access starts with a generated token, thin-client mode signs in with its token | opt-in (`-m desktop`); in `desktop-release.yml` behind a flag (see [CI gates](#ci-gates)) |
| **Manual** | a human + the desktop app | slow | things no harness reaches: the auto-updater, OS install/SmartScreen, voice/mic, computer-use | per desktop release (see checklist below) |

The web UI **is** the desktop UI (the Tauri shell wraps the same server +
`static/index.html` and `static/app.js`), so e2e against the web server covers
the desktop UI's HTML/JS. Only the native shell (window, updater, sidecar spawn) needs the
desktop e2e or a human.

## Running the tests

```bash
# Unit (fast; e2e auto-excluded). The composite actions' tests sit next to
# them under .github/actions, outside `testpaths`, so name that dir too.
.venv/Scripts/python -m pytest -q tests .github/actions
.venv/Scripts/python -m ruff check evi tests scripts .github/actions

# E2E (CLI) — drives the real `evi` CLI as a subprocess; no browser needed
.venv/Scripts/python -m pytest tests/cli_e2e -m e2e --timeout=120

# E2E (UI) — needs the extra + a browser, once
pip install -e ".[e2e]"
python -m playwright install chromium
.venv/Scripts/python -m pytest tests/e2e -m e2e --timeout=120
```

`tests/test_web_hardening.py` runs the Docker image's entrypoint
(`scripts/docker-entrypoint.sh`) under a POSIX `sh` (dash when present, as in
the image) and skips those tests when there is none on `PATH` (on Windows, put
Git for Windows' `usr\bin` on `PATH`).

E2E is opt-in: both suites are marked `e2e` and excluded from the default run via
`addopts = -m "not e2e"`. `tests/e2e/conftest.py` (UI) skips its dir when
Playwright isn't installed; `tests/cli_e2e/` (CLI) has no browser dependency, so
it runs anywhere Python + the package are installed.

### How the e2e harness works (`tests/e2e/conftest.py`)

- A **fake OpenAI-compatible backend** (Starlette, in-thread) streams a canned
  chat-completion — so e2e needs **no Ollama/LM Studio** and runs in CI. It
  serves two models, `fake` and `fake-o1`, with different capability chips.
- The **real eVi web server** runs as a subprocess with an isolated `EVI_HOME`
  whose `config.toml` points at the fake backend (`evi_base_url`, one per
  session and per xdist worker).
- **Playwright** (headless chromium) drives the page like a user.

This exercises the full path — agent → `sse-starlette` → browser fetch/parse →
DOM render — which is exactly where the 0.24.2 bug lived.

Fixtures and markers a UI test can use:

- **`evi_home`**: that server's `EVI_HOME` (a pytest tmp dir). Seed state the
  server reads on every request there, such as the Telegram pairing queue
  (`channels/telegram.json`), a plugin, or the managed runtime's log.
- **`fake_llm`**: the test's handle on the fake backend, reset before the test
  and released after it, so a failing test can't leave a turn held. Put a
  marker in the prompt to shape the turn:
  - `[e2e-hold]` (`fake_llm.HOLD`): the backend answers nothing until the test
    sets `fake_llm.release`; `fake_llm.held` is set once the request arrives,
    so the turn is really in flight server-side (`test_ui_steering.py`).
  - `[e2e-tool]` (`fake_llm.TOOL`): the backend answers with one call to
    `list_memories` (`fake_llm.TOOL_NAME`), which is read-only and
    auto-approved, so the turn gets a next step without a permission prompt.

  The markers count only in a streamed request whose last message is the
  user's, so the step after the tool call and the title request get the plain
  canned reply. Take them from the fixture: importing `conftest` loads a
  second copy of the control object.
- **`@pytest.mark.quarantine`** is registered in `tests/e2e/conftest.py` (see
  [CI gates](#ci-gates)); `tests/e2e/test_e2e_harness.py` checks that it is.

### How the CLI e2e harness works (`tests/cli_e2e/conftest.py`)

- The `evi_cli` fixture runs `python -m evi <args>` as a **subprocess** against
  an isolated `EVI_HOME` (so tests never touch your real `~/.evi`).
- It returns a `CliResult` (`code`, `stdout`, `stderr`, `.out`, `.json()`) and
  exposes `.home` (the temp `EVI_HOME`) and `.workdir` (scratch space for
  building source plugins/skills to import).
- Tests cover the **offline** commands per subsystem (config, doctor, skill,
  plugin, guardrails, route, recipe, workflow, eval, schedule, sessions, style,
  peer, web-config token, mcp, stats, backup, finetune, routine, agents, tools).
  Model-driven commands (`chat`, `run`, `eval run`, `recipe run`, `batch`) need a
  backend and are covered by the UI e2e (fake backend) + unit suites instead.

This catches CLI-wiring regressions — a renamed option, a broken `add_typer`
mount, a config path that doesn't resolve — that function-level unit tests miss.

## CI gates

What runs on GitHub, and what each check wants from a change:

| Check | Workflow / job | Runs on | Fails when |
|---|---|---|---|
| pytest + ruff | `ci.yml` `test` | push to main, PRs (full OS/Python matrix on PRs, weekly, dispatch) | a unit test or lint error, in `tests` or `.github/actions` |
| cargo check + test | `ci.yml` `rust` (Linux), `rust-windows` | `rust`: push to main and PRs; `rust-windows`: PRs, weekly, dispatch (releases run the same `cargo test` in `desktop-release.yml`'s Windows leg) | the desktop shell doesn't build or its tests fail |
| actionlint | `ci.yml` `actionlint` | push to main, PRs | a workflow has a syntax, expression or type error, or a shellcheck error in a `run:` |
| workflow injection | `ci.yml` `workflow-injection` | push to main, PRs | any `${{ … }}` inside a `run:`, a github-script `script:`, a retry `command:` or a `docker://` step's `args:` / `entrypoint:` (pass the value through `env:` and read `$VAR`), or a zizmor template-injection finding; zizmor's other findings are printed only |
| docs freshness | `docs-freshness.yml` | PRs and pushes to main | the change adds a CLI command (Typer `@….command`, `add_typer`) or a web route (FastAPI `@app.get/post/…`, `add_api_route`) under `evi/` and changes nothing under `docs/` |
| UI e2e | `e2e.yml` | PRs, pushes to main, weekly, dispatch; called by `release.yml` | a Playwright test fails twice in a row |
| desktop e2e | `desktop-release.yml` (Windows leg) | releases, only with the `desktop_e2e` input or the repo variable `EVI_DESKTOP_E2E=true` | a smoke test fails or skips, or no test runs |

- **Docs freshness escape hatch.** For an internal route, a rename, or docs
  landing in a follow-up: label the PR `docs-skip`, or, for a direct push to
  main, put a `Docs-skip: <reason>` trailer line in one of the commit
  messages. Run it locally with
  `python scripts/check_docs_freshness.py --base origin/main`. A registration
  the same diff also removes counts as moved, not new.
- **Flaky UI tests.** e2e gives a failing test one retry (`--reruns 1`,
  pytest-rerunfailures, which `e2e.yml` installs). A test known to be flaky can
  be marked `@pytest.mark.quarantine`: it then runs in a separate step that
  never fails the run, so it stays visible without blocking merges or
  releases. Fix or delete it promptly. The gate runs
  `pytest tests/e2e -m "e2e and not quarantine" --reruns 1 --timeout=120`.
- **Workflow injection locally:**
  `python .github/actions/workflow-guard/workflow_guard.py` (the strict check;
  zizmor runs only in CI, pinned to 1.30.1).
- **Releases** are gated on top of this; see
  [docs/releasing.md](docs/releasing.md).

## The rule for new phases

When a phase touches the web UI (anything in `evi/apps/web/static/` or a
`/api/*` endpoint the UI calls):

1. Add/extend an e2e test in `tests/e2e/` that asserts the **user-visible
   result** (a bubble appears, a banner shows/hides, a tab opens, …).
2. Run `pytest tests/e2e -m e2e` locally before opening the PR.
3. CI runs e2e on the PR; merge only when green.

A feature without an e2e test is assumed broken until proven otherwise.

## Feature inventory + e2e coverage

Status: ✅ e2e now · 🧪 unit-only · ⬜ TODO e2e · 👤 manual-only. This is the
backlog — fill the ⬜ rows as you touch each area.

### Chat core (Phase 1, 4)
| Feature | Status |
|---|---|
| Send message → streamed reply renders | ✅ `test_chat_renders_reply` |
| No console errors on load / during chat | ✅ |
| Tool call + tool result bubbles render | ⬜ |
| Thinking blocks (`<think>`) render + collapse | ⬜ |
| Markdown + code highlighting | ✅ `tests/e2e/test_ui_xss.py::test_markdown_keeps_what_chat_needs`, `::test_ui_renders_with_only_the_evi_server_reachable` (offline, vendored libraries) |
| Model output renders inert (scripts, handlers, foreign images and links, diagrams) | ✅ `tests/e2e/test_ui_xss.py` (`test_streamed_reply_and_system_message_render_inert`, `test_history_rebuild_and_inbox_render_inert`, `test_images_and_links_cannot_reach_evis_own_endpoints`, `test_diagrams_cannot_make_the_page_fetch_anything`, `test_csp_stops_script_that_bypasses_the_sanitizer`, …) · 🧪 `tests/test_ui_render_safety.py` |
| Citations rendering (P30) | ⬜ |
| Token/usage indicator updates | ⬜ |

### Backends / first-run (Phase 48, 50, 0.24.1, 1.0.49, 1.0.50)
| Feature | Status |
|---|---|
| Configured backend reachable → no banner | ✅ `test_backend_configured_hides_banner` |
| No backend → full-screen setup ranks what the machine has | ✅ `test_ui_onboarding.py::test_setup_screen_ranks_what_the_machine_already_has` |
| "Use <backend>" switch action (`/api/backend/use`), verified | 🧪 unit · ✅ `test_ui_onboarding.py::test_verified_switch_reports_chat_and_tools` |
| Failed switch rolls back; Run diagnostics works | ✅ `test_ui_onboarding.py::test_failed_switch_rolls_back_and_changes_nothing`, `::test_failed_switch_run_diagnostics_button_works` |
| "Set up local AI" card: what it installs, its steps, log + diagnostics on failure | ✅ `test_ui_runtime_setup.py` (`test_first_run_download_option_opens_the_card`, `test_card_lists_what_it_will_put_on_the_machine`, `test_setup_names_its_steps`, `test_failure_offers_the_log_and_diagnostics`) |
| Install/start/pull wizard flow | 👤 (real Ollama) |
| Model download progress (first-run step; Settings → Model & Backend Pull) | ✅ `tests/e2e/test_ui_model_pull.py` (`test_first_run_download_shows_progress`, `test_first_run_download_reaches_the_server_and_shows_its_error`, `test_settings_pull_streams_progress_to_installed`, `test_settings_pull_reaches_the_server_and_shows_its_error`) |
| Model picker / switcher (`/api/model-picker`) | 🧪 unit · ✅ `test_ui_model_switch.py` |
| Starter prompts in an empty chat | ✅ `test_ui_starter_prompts.py` (`test_empty_chat_offers_three_starters`, `test_starter_sends_its_prompt_and_gives_way`, `test_starters_survive_when_nothing_answers`, `test_new_chat_gets_starters_a_restored_one_does_not`) |

### Sessions & navigation (Phase 4, 13)
| Feature | Status |
|---|---|
| New tab (+), close tab, switch tabs | ⬜ |
| Reset / new chat | ⬜ |
| Auto-title after first exchange | 🧪 · ⬜ |
| Resume past session | ⬜ |

### Slash commands & modes (Phase 10, 11)
| Feature | Status |
|---|---|
| `/help` renders | ⬜ |
| `/goal`, plan mode, auto mode chips | 🧪 · ⬜ |
| User-defined slash commands | 🧪 · ⬜ |

### Files / media (Phase 3, 12, 13)
| Feature | Status |
|---|---|
| File upload / drop | ⬜ |
| Image generation result render (P3) | 👤 |
| Voice TTS / STT controls | 👤 (audio device) |

### Steering a running turn (1.0.52)
| Feature | Status |
|---|---|
| Send becomes Queue while a turn runs | ✅ `test_ui_steering.py::test_send_becomes_queue_while_a_turn_runs` |
| Follow-up shows "queued", then "applied" at the next step | ✅ `test_ui_steering.py::test_follow_up_is_queued_then_applied_at_the_next_step[chromium-prompt-steer]` / `[chromium-late-steer]` |
| Turn ends first → the follow-up is sent as a normal message | ✅ `test_ui_steering.py::test_follow_up_is_resent_when_the_turn_ends_first[…]` |
| …when the server's leftover-steering frame arrives after `Done` | ⚠ strict xfail `test_follow_up_is_resent_when_its_frame_comes_after_done[…]` (known bug: remove the xfail once fixed) |
| A message to a busy session (409) becomes a follow-up | ✅ `tests/e2e/test_web_hardening.py::test_a_busy_session_turns_the_message_into_a_follow_up` |

### Other surfaces
| Feature | Status |
|---|---|
| Auth token login overlay (P9) | ✅ `tests/e2e/test_web_hardening.py::test_a_stale_injected_token_never_replaces_a_working_one` (signs in through the overlay), `::test_injected_token_signs_the_ui_in` |
| UI sends `X-Evi-Client`; a browser POST without it is refused | ✅ `tests/e2e/test_web_hardening.py::test_ui_requests_carry_the_client_header`, `::test_browser_post_without_the_header_is_refused` |
| Launch token (desktop LAN / container) spares this machine | ✅ `tests/e2e/test_web_hardening.py::test_launch_token_spares_this_machine` |
| Cross-site / cross-alias requests refused | ✅ `test_ui_security.py` |
| A model pull needs `X-Evi-Client`; the UI streams its own | ✅ `tests/e2e/test_ui_security.py::test_evis_own_page_cannot_start_a_pull_without_the_header[img, eventsource]`, `::test_ui_streams_its_own_model_pull` |
| UI runs under its CSP header; another site can't frame it | ✅ `tests/e2e/test_ui_xss.py::test_the_ui_runs_under_its_csp_header`, `::test_another_site_cannot_frame_the_ui`, `::test_a_framed_ui_without_its_headers_stays_blank_and_inert` |
| A login whose data folder collides is told why | ✅ `tests/e2e/test_web_hardening.py::test_a_login_sharing_a_data_folder_is_told_why` |
| Desktop: links go to the system browser through one listed command | ✅ `tests/e2e/test_ui_xss.py::test_desktop_links_go_to_the_system_browser_through_one_listed_command` (emulated) |
| Permission prompt dialog | ⬜ |
| Settings → Channels: approve / revoke Telegram senders | ✅ `test_ui_channels.py` (`test_approve_then_revoke_a_sender`, `test_channel_is_off_and_unknown_codes_pair_nobody`) |
| MCP tools available (P7) | 🧪 |
| Memory / index / calendar / git tools | 🧪 |

### Settings & help (Phase 56, eVi 0.2.5)
| Feature | Status |
|---|---|
| Settings screen opens (⚙ / Ctrl+, / File→Settings) | ✅ `test_settings_opens_and_renders` |
| Setting change saves via `/api/config` (round-trip) | ✅ `test_settings_persists_change` · 🧪 unit |
| Secret fields masked (api_key/auth_token/dsn) | 🧪 unit |
| Sections grouped (Assistant / This computer / Extend / App, "More"); active one scrolled into view | ✅ `test_ui_settings_groups.py` (`test_sections_are_grouped`, `test_ungrouped_section_lands_under_more`, `test_active_section_is_scrolled_into_view`) |
| "How this works" explainers + live health banners (backend, integrations, voice, guardrails, plugins) | ✅ `test_ui_settings_health.py` |
| Unsaved-changes cue; a banner moves only after Save | ✅ `test_ui_settings_unsaved.py` (`test_unsaved_cue_then_green_saved_and_red_failure`, `test_banner_moves_only_after_save`) |
| In-app docs render offline (`/api/docs`, mdlite) | ✅ `test_docs_dialog_renders` |
| Diagnostics panel (`/api/doctor`) | ✅ `test_diagnostics_dialog_renders` |
| Theme toggle (system/dark/light) | 👤 (verified via preview) |

### Native desktop (manual per release)
| Feature | Status |
|---|---|
| Installer runs; app window opens | 👤 |
| Sidecar spawns; chat works in the window | 👤 |
| No `window.__TAURI__`; only the listed commands reach the shell; the window stays on the eVi server | ✅ desktop e2e, opt-in: `tests/desktop_e2e/test_desktop_smoke.py::test_tauri_invoke_bridge_is_present`, `::test_window_stays_on_the_evi_server` · 🧪 Rust `ipc_guard_tests` in `main.rs` |
| A reply with a mermaid diagram renders in the window | 👤 |
| In-app auto-update (check → install → relaunch) | 👤 |
| File/Edit/View/Help menus + accelerators | 👤 |
| Help → Check for Updates (force-update) | 👤 |
| Tray icon: show/hide, new chat, quit | 👤 |
| Close → minimize to tray (app keeps running) | 👤 |
| SmartScreen/Gatekeeper behaviour (unsigned) | 👤 |

## Desktop release checklist (manual)

Before announcing a `desktop-v*` release, on a clean machine:

1. Install from the public channel installer; the app window opens.
2. First-run wizard: install/start Ollama, pull the model, reach a working chat.
3. Send a message → get a reply (the thing that broke).
4. Trigger an update (bump version, publish, relaunch) → app self-updates.

Automating 1–4 needs a windowed CI runner + a real backend; until then it's a
human gate. Items 2–3's *logic* is covered by e2e against the web server, and
the opt-in desktop e2e (`EVI_DESKTOP_E2E=true`) checks that the built app
launches, renders the UI and opens Settings from its native menu, and that
LAN access and thin-client mode sign in with their tokens.
