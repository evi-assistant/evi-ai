# eVi Desktop

Tauri 2 shell. On launch it spawns the web server
(`evi.apps.web.server`) as a child process on a free local port, waits for
`/api/health` to respond, then loads the chat UI in a native webview. On window
close the child server is killed.

## Prerequisites

- Rust toolchain (`rustup`, stable, 1.77+)
- Node 18+ (only used to invoke the Tauri CLI)
- Python 3.13+ with the eVi package installed in editable mode:
  `pip install -e '.[web]'` from the repo root
- Platform deps for Tauri 2: see <https://tauri.app/start/prerequisites/>

## Configuration

| Env var | Purpose |
|---|---|
| `EVI_REMOTE_URL` | **Remote mode.** Skip the local Python spawn and point the webview at this URL (e.g. `http://ai-server:8000`). Use when this machine is a thin client of your AI server. |
| `EVI_PYTHON` | Local mode only. Override the Python interpreter (default: `py -3.13` on Windows, `python3` elsewhere). |
| `EVI_REPO_ROOT` | Local mode only. Override repo-root discovery (default: walks up from the binary looking for `pyproject.toml`). |

## Dev

From `desktop/`:

```
npm install
npm run dev
```

Tauri opens a window pointing at the embedded server. Reload with F5 to pick up
changes to `evi/apps/web/static/index.html`.

## Build

```
npm run build
```

Produces a platform-native installer under `src-tauri/target/release/bundle/`.

By default this bundle does **not** ship Python — the target machine needs
Python 3.13+ with `pip install evi-assistant[web]` (local mode falls back to system
Python). For a **standalone** installer that embeds a frozen server (no
Python prerequisite), freeze the sidecar first and build with the standalone
config — see [docs/desktop-bundling.md](../docs/desktop-bundling.md):

```
pip install -e '.[web]' pyinstaller
../scripts/build-sidecar.sh                       # or build-sidecar.ps1
npm run tauri build -- --config src-tauri/tauri.standalone.conf.json
```

## Roadmap (not yet wired)

- System-tray icon + global hotkey
- Toast notifications for long-running tool calls
- Per-window settings (model selector, tool toggles)
