# Testing

How eVi is tested, and the process for keeping it working as phases land.

## Why this exists

The 0.24.2 bug — **chat rendered nothing** for two minor releases — slipped
through because we had 600+ unit tests but **zero tests that drive the actual
UI**. The Python tests asserted the server *emits* SSE events; nothing checked
that the browser *renders* them. The fix is a real end-to-end (e2e) layer plus
a standing rule: **every UI-affecting change ships with an e2e test.**

## The three layers

| Layer | Tool | Speed | Covers | Runs |
|---|---|---|---|---|
| **Unit** | `pytest` (`tests/*.py`) | fast (~30 s, 631 tests) | core logic, config, tools, backends, server endpoints (via `TestClient`), converters | every push (CI `ci.yml`) + locally |
| **E2E (UI)** | Playwright (`tests/e2e/`) | medium (~10 s + browser install) | the real web UI in a real browser against the real server | PRs + weekly + dispatch (CI `e2e.yml`) |
| **Manual** | a human + the desktop app | slow | things no harness reaches: the Tauri window itself, the auto-updater, OS install/SmartScreen, voice/mic, computer-use | per desktop release (see checklist below) |

The web UI **is** the desktop UI (the Tauri shell wraps the same server +
`static/index.html`), so e2e against the web server covers the desktop UI's
HTML/JS. Only the native shell (window, updater, sidecar spawn) needs manual.

## Running the tests

```bash
# Unit (fast; e2e auto-excluded)
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check evi tests scripts

# E2E (needs the extra + a browser, once)
pip install -e ".[e2e]"
python -m playwright install chromium
.venv/Scripts/python -m pytest tests/e2e -m e2e --timeout=120
```

E2E is opt-in: it's marked `e2e` and excluded from the default run via
`addopts = -m "not e2e"`, and `tests/conftest.py` skips the whole `tests/e2e/`
dir when Playwright isn't installed (so the default CI job never trips on it).

### How the e2e harness works (`tests/e2e/conftest.py`)

- A **fake OpenAI-compatible backend** (Starlette, in-thread) streams a canned
  chat-completion — so e2e needs **no Ollama/LM Studio** and runs in CI.
- The **real eVi web server** runs as a subprocess with an isolated `EVI_HOME`
  whose `config.toml` points at the fake backend.
- **Playwright** (headless chromium) drives the page like a user.

This exercises the full path — agent → `sse-starlette` → browser fetch/parse →
DOM render — which is exactly where the 0.24.2 bug lived.

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
| Markdown + code highlighting | ⬜ |
| Citations rendering (P30) | ⬜ |
| Token/usage indicator updates | ⬜ |

### Backends / first-run (Phase 48, 50, 0.24.1)
| Feature | Status |
|---|---|
| Configured backend reachable → no banner | ✅ `test_backend_configured_hides_banner` |
| No backend → setup wizard banner | ⬜ (fake a dead backend) |
| "Use <backend>" switch action (`/api/backend/use`) | 🧪 unit · ⬜ e2e |
| Install/start/pull wizard flow | 👤 (real Ollama) |
| Model picker / switcher (`/api/model-picker`) | 🧪 unit · ⬜ e2e |

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

### Other surfaces
| Feature | Status |
|---|---|
| Auth token login overlay (P9) | ⬜ |
| Permission prompt dialog | ⬜ |
| MCP tools available (P7) | 🧪 |
| Memory / index / calendar / git tools | 🧪 |

### Native desktop (manual per release)
| Feature | Status |
|---|---|
| Installer runs; app window opens | 👤 |
| Sidecar spawns; chat works in the window | 👤 |
| In-app auto-update (check → install → relaunch) | 👤 |
| SmartScreen/Gatekeeper behaviour (unsigned) | 👤 |

## Desktop release checklist (manual)

Before announcing a `desktop-v*` release, on a clean machine:

1. Install from the public channel installer; the app window opens.
2. First-run wizard: install/start Ollama, pull the model, reach a working chat.
3. Send a message → get a reply (the thing that broke).
4. Trigger an update (bump version, publish, relaunch) → app self-updates.

Automating 1–4 needs a windowed CI runner + a real backend; until then it's a
human gate. Items 2–3's *logic* is covered by e2e against the web server.
