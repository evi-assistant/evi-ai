# Web & Desktop (settings, multi-user, deep links, updater)

## Overview

eVi ships the same assistant in three skins: a Typer **CLI**, a **web UI** (FastAPI + Server-Sent Events), and a **Tauri desktop app** that wraps that web UI in a native window with a menu bar, system tray, and a signed auto-updater. This page covers the cross-cutting "shell" concerns of the web/desktop surface:

- **Settings** — editing your whole `config.toml` from a browser panel, with most changes hot-applied to live chats.
- **Auth & multi-user** — an optional bearer token to gate `evi web`, plus opt-in per-user logins where each person gets an isolated workspace.
- **Deep links** — the `evi://` URL scheme that focuses the desktop app (or a browser tab) on a specific session or workflow.
- **Updater** — the desktop app's background self-update against signed GitHub releases.

You'd use the web UI when you want a richer chat surface than the terminal (tabs, settings screen, drag-drop uploads, dispatch dashboard) while keeping everything local. You'd use the desktop app for a one-click, always-warm assistant that survives window-close (it hides to the tray) and keeps itself up to date. Everything is local-first: the desktop app spawns the Python server on `127.0.0.1` and there is no cloud component.

## How it works

### The web server

`evi web` (or `python -m uvicorn evi.apps.web.server:app`) serves a single-page app from `evi/apps/web/static/index.html`. The browser POSTs to `/api/chat` with `{session_id, message}` and reads back an SSE stream of JSON event lines. Each `session_id` maps to one `WebSession` holding an `Agent`; sessions are revived from their on-disk transcript when first requested, so closing and reopening the app restores history.

Slash commands (`/help`, `/reset`, `/tools`, `/model`, `/goal`, `/plan`, `/auto`, `/compact`, `/reload`, plus user commands from `~/.evi/commands/`) are dispatched **server-side** — the browser just types and reads; CLI and web behave identically.

### The desktop app

`desktop/src-tauri/src/main.rs` is the native shell. On launch it:

1. Picks a **stable preferred port `8473`** (falling back to a random free port if taken). A stable origin means the webview's `localStorage` — your open tabs and session ids — survives a restart.
2. **Spawns the server.** In a packaged build it runs the bundled PyInstaller sidecar `evi-server`; in a source checkout it spawns `py -3.13 -m uvicorn evi.apps.web.server:app` (or `python3 -m uvicorn …` off-Windows) from the repo root.
3. Loads a **loading shim** that polls the port and redirects once `/api/health` is up, so you never see a connection-refused page during the sidecar's cold start.
4. Builds the **File / Edit / View / Help** menu bar and a **system tray**. Closing the window calls `prevent_close()` and *hides* it — the assistant and its warm sidecar keep running. Quit from the tray ("Quit eVi") or **File → Exit**.
5. Starts a background **update check** (unless in remote mode or opted out).

Menu/tray actions other than devtools and quit are routed into the page via `window.eviUI.handleMenu('<id>')` (e.g. `new_chat`, `settings`, `check_updates`, `open_logs`).

**Remote (thin-client) mode:** set `EVI_REMOTE_URL` to point the desktop window at an already-running eVi elsewhere (e.g. a GPU box). The app skips the Python spawn, waits up to 15s for that URL's `/api/health`, and navigates to it. The updater is disabled in this mode (there's no local bundle to replace).

### Settings (read/write config)

The settings panel reads `GET /api/config` and writes `POST /api/config` with a nested `{section: {key: value}}` patch. Key behaviours:

- **Secrets are masked.** `[llm] api_key`, `[web] auth_token`, and `[telemetry] dsn` come back as the sentinel `********`. POSTing the sentinel back means "leave unchanged" so re-saving the form never wipes a token you never saw. An empty string still clears the value.
- **Unknown sections/keys are ignored** (forward-compatible with newer configs).
- **Hot-apply.** Touched sections in `llm`, `auto`, `tools`, `telemetry`, `comfy`, `web`, `google`, `microsoft`, `obsidian` are pushed onto every live session so changes take effect on the next turn without a new chat. The LLM client is rebuilt when `[llm]` changes. `tools` and `auto` only fully bind at session creation, so the response flags them as `deferred` ("applies to new chats").

### Auth & multi-user

A FastAPI middleware guards every `/api/*` route. Auth fires only when `[web] auth_token` is set **or** `[web] multi_user = true` with users present; otherwise access is open (fine for localhost-only). When active, a request must carry `Authorization: Bearer <token>` **or** `?token=<token>` (the query form covers `<img>` src and SSE streams that can't set headers). A few paths self-bootstrap and stay public: `/`, `/static/*`, `/images/*`, `/api/health`, `/api/auth/check`, `/api/backend/status`, and routine webhooks (`/api/routine/*`, which authenticate via their own unguessable path token).

In **multi-user** mode, `~/.evi/users.json` holds `{name, token}` entries. Any user token authenticates and the request is scoped to that user; the owner's `auth_token` still works and resolves to the user `"owner"`. Each user gets an **isolated workspace** — sessions, transcripts (`~/.evi/users/<name>/transcripts`), and memory (`~/.evi/users/<name>/memory`) — that other users can't see. Skills, plugins, and config stay shared (capabilities, not personal data). Drop a user from the file to revoke access.

### Deep links

`evi/deeplinks.py` defines three routes:

| Link | Effect |
|------|--------|
| `evi://session/<id>` | open / resume that session |
| `evi://new` | start a new chat |
| `evi://workflow/<name>` | open the dispatch panel, ready to run a workflow |

The desktop app registers the `evi://` scheme (via `tauri.conf.json`, plus a best-effort runtime `register_all()` for dev/portable runs). An incoming link is mapped to an in-app web path by `to_web_path()` / the Rust mirror `deep_link_to_path()`: `evi://session/<id>` → `/?session=<id>`, `evi://workflow/<name>` → `/?workflow=<name>`, everything else (including `evi://new`) → `/`. **Unknown routes fall back to `/`** so a stray link never errors the shell. Because the web UI understands `/?session=` and `/?workflow=` natively, the same links also work pasted into a browser.

### Updater

`spawn_update_check()` queries eVi's signed GitHub releases (`latest.json` published by `desktop-release.yml`). If a newer version exists it downloads, installs, and restarts — on the async runtime, so it never blocks the window. The updater **only accepts bundles signed with eVi's key** (pubkey baked into `tauri.conf.json`), so a tampered release can't be installed. Before installing, the sidecar is killed first (Windows keeps onedir DLLs locked while `evi-server` runs). Progress surfaces as an in-app toast polled via the `update_status_cmd` Tauri command; **Help → Check for Updates** triggers it on demand via `check_for_update_cmd`.

## Setup

All state lives under `~/.evi/` (Windows: `%USERPROFILE%\.evi\`). The relevant config sections in `~/.evi/config.toml`:

```toml
[web]
auth_token = ""          # empty = open access. Set to require a bearer token.
multi_user = false       # opt-in per-user logins from ~/.evi/users.json
```

Multi-user logins file, `~/.evi/users.json` (managed by the CLI — see below):

```json
[ { "name": "alice", "token": "…" }, { "name": "bob", "token": "…" } ]
```

**Optional pip extra:** the web UI needs uvicorn/FastAPI, shipped in the `web` extra:

```bash
pip install 'evi-assistant[web]'
```

**Defaults:** `evi web` binds `127.0.0.1:8000`. The desktop app prefers port **8473**. Auth is **off** by default. Multi-user is **off** by default. Auto-update is **on** in packaged desktop builds.

**Environment variables (desktop):**

| Var | Purpose |
|-----|---------|
| `EVI_HOME` | Override the `~/.evi/` location entirely |
| `EVI_REMOTE_URL` | Thin-client mode: skip the local spawn and navigate to this URL |
| `EVI_PYTHON` | Interpreter to spawn in a source checkout (default `py -3.13`) |
| `EVI_REPO_ROOT` | Pin the repo root instead of auto-detecting it |
| `EVI_AUTO_UPDATE` | Set to `0` / `false` to disable the background self-update |
| `EVI_TESSERACT_CMD` | Set automatically when a bundled `tesseract` ships next to the sidecar |

## Usage

### CLI

```text
evi web [--host 127.0.0.1] [--port 8000]   # launch the web UI
evi web-config token show                   # print the current token (or "(unset)")
evi web-config token rotate [--length 32]   # generate + persist a new token (prints once)
evi web-config token clear                  # unset the token → open access again
evi web-config users list                   # list multi-user logins
evi web-config users add <name>             # add/re-issue a user (prints token once)
evi web-config users remove <name>          # revoke a user
evi link [<id>|new]                         # print an evi:// deep link (default: most recent session)
evi link --open <evi://…>                   # resolve a link and show where it routes
```

Note the asymmetry: you *launch* the server with `evi web`, but its auth/helpers live under `evi web-config` (the token group is `evi web-config token …`, not `evi web token …`).

### Web UI

- **Sign in:** if auth is on, a login overlay asks for the token. It's stored in `localStorage` and attached to every same-origin request; a 401 clears it and re-prompts.
- **Settings:** click the ⚙ button or press **Ctrl+,**. Edit any config section; secret fields show `********` and are left untouched on save unless you type a new value.
- **Open a session/workflow:** visit `/?session=<id>` to open or resume a tab, or `/?workflow=<name>` to open the dispatch panel.
- **Keyboard:** Enter sends, Shift+Enter newline, Ctrl+N new chat, Ctrl+I switch model, Ctrl+F find, Esc closes a dialog.

### Desktop app

- **Menu bar:** File (New Chat, Open File, Export Chat, Undo File Change, Settings, Exit), Edit, View (zoom, toggle theme, devtools), Help (Documentation, Keyboard Shortcuts, Check for Updates, Run Diagnostics, Open Logs Folder, Get Support, About).
- **Tray:** left-click shows the window; menu has Show, New Chat, Check for Updates, Quit.
- **Check for Updates:** Help → Check for Updates (or the tray item) checks immediately and, if an update exists, downloads + installs in the background and restarts.

## Examples

### Example 1 — turn on web auth and connect

```bash
# 1. Generate and persist a bearer token (printed once).
evi web-config token rotate
# rotated
# 3f2a9c…<64 hex chars>

# 2. Launch the server — it confirms auth is enabled.
evi web --host 127.0.0.1 --port 8000
# web auth: enabled (browser prompts for the token on first load)
# eVi web → http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000`, paste the token into the sign-in form (it's cached in `localStorage`). Scripts/curl can authenticate either way:

```bash
# Header form (preferred):
curl -H "Authorization: Bearer 3f2a9c…" http://127.0.0.1:8000/api/health

# Query form (for streaming/img endpoints that can't set headers):
curl "http://127.0.0.1:8000/api/health?token=3f2a9c…"
```

To go back to open access: `evi web-config token clear`.

### Example 2 — multi-user team with isolated workspaces

```bash
# Add two users; each token is printed exactly once.
evi web-config users add alice
# added alice
#   token: a1b2c3…  (shown once)
evi web-config users add bob

# Enable multi-user mode in ~/.evi/config.toml:
```

```toml
[web]
multi_user = true
```

```bash
evi web
```

Alice signs in with her token and sees only her sessions, transcripts (`~/.evi/users/alice/transcripts`), and memory (`~/.evi/users/alice/memory`); Bob sees only his. Revoke Bob instantly with `evi web-config users remove bob`.

### Example 3 — deep links

```bash
# Print a link to your most recent session:
evi link
# evi://session/9f3c2a7b1e4d6058

# Link to a fresh chat:
evi link new
# evi://new

# See where a workflow link routes (no app needed):
evi link --open "evi://workflow/research"
# workflow research -> /?workflow=research
```

Clicking `evi://session/9f3c2a7b1e4d6058` focuses the desktop app on that tab; pasting `http://127.0.0.1:8473/?session=9f3c2a7b1e4d6058` into a browser does the same thing against a running server.

### Example 4 — desktop as a thin client, no local model

```bash
# Point the desktop window at an eVi already running on a GPU box.
# (PowerShell)
$env:EVI_REMOTE_URL = "http://gpu-box:8473"
# (bash)
export EVI_REMOTE_URL="http://gpu-box:8473"
```

Launch the desktop app: it skips the Python spawn, waits for `http://gpu-box:8473/api/health`, and loads it. Disable the background updater in this or any build with `EVI_AUTO_UPDATE=0`.

## Notes / limits

- **Open by default, on purpose.** With no `auth_token` and no users, `/api/*` is unauthenticated — safe for a localhost-only bind, risky if you expose the port. Set a token (or multi-user) before binding to a non-loopback host.
- **Tokens are compared with `secrets.compare_digest`** (constant-time). The token is printed only once on rotate/add; lost tokens must be rotated/re-issued, not recovered.
- **Auth re-reads config per request**, so `evi web-config token rotate` takes effect without restarting the server (though the in-app message still suggests a restart out of caution).
- **The `?token=` query form leaks the token into logs/history** more readily than a header. It exists only for `<img>` and SSE URLs that can't carry headers — prefer the header everywhere else.
- **Multi-user isolation covers personal data only** (sessions/transcripts/memory). Skills, plugins, and the single shared `config.toml` are common to everyone — don't treat a user token as a sandbox boundary for capabilities.
- **Routine webhooks bypass web auth** by design: `/api/routine/<token>` authenticates with its own unguessable path token, so external callers don't need the web token. Federation (`/api/federate`) is a separate opt-in (`[federation] serve = true`) and runs non-interactively (non-auto-approved tools are denied).
- **Settings: deferred sections.** Changing `[tools]` or `[auto]` won't affect existing chats — those bind at session creation, so they apply to new chats only (the UI hints this).
- **Updater is desktop-only and signature-pinned.** It will not install an unsigned or wrongly-signed bundle. The CLI/web package updates the ordinary way: `pip install -U evi-assistant`. Auto-update is skipped entirely in `EVI_REMOTE_URL` mode and when `EVI_AUTO_UPDATE=0`.
- **Update install kills the sidecar first.** On Windows the installer would otherwise fail with "Error opening file for writing" because the running `evi-server` locks its onedir DLLs. This is automatic; just let it restart.
- **Deep links fail safe.** Any malformed or unknown `evi://` route resolves to `/` rather than erroring — so a bad link opens the home view instead of breaking the shell.
- **Stable-port caveat.** A second desktop instance can't take port 8473 and falls back to a random port, which means that second launch starts with a fresh webview origin (empty `localStorage` — no remembered tabs).

## Relevant source files

- `C:\evi\evi\apps\web\server.py` — FastAPI app, auth middleware, `/api/config`, multi-user scoping, session/SSE endpoints.
- `C:\evi\evi\apps\web\static\index.html` — single-page UI: login overlay, settings panel, `?session=`/`?workflow=` handling, desktop menu bridge, update toast.
- `C:\evi\evi\deeplinks.py` — `build_link`, `parse_link`, `to_web_path` for the `evi://` scheme.
- `C:\evi\desktop\src-tauri\src\main.rs` — Tauri shell: port pick, server/sidecar spawn, menu/tray, deep-link routing, signed auto-updater.
- `C:\evi\evi\apps\cli\main.py` — `evi web`, `evi web-config token/users`, `evi link` commands.
- `C:\evi\evi\config.py` — `WebSettings` (`auth_token`, `multi_user`) and other config sections.
- `C:\evi\docs\configuration.md` — concise companion reference (multi-user, deep links, env vars).
