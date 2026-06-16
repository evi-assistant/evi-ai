# eVi for VS Code

A fully-local AI **autocomplete (Tab/ghost-text)** + **chat** sidebar for VS Code,
backed entirely by your local eVi server. Nothing leaves your machine — the
extension is thin glue over two endpoints eVi already exposes:

- `POST /api/complete` — fill-in-the-middle (FIM) code completion (`evi/complete.py`)
- `POST /api/chat` — the full eVi agent (tools, routing, MCP), streamed (SSE)

## Prerequisites

1. An eVi server running locally: `evi web` (or the desktop app). Default URL
   `http://127.0.0.1:8473`.
2. A FIM-capable coder model available to eVi for autocomplete (e.g.
   `qwen2.5-coder`, `deepseek-coder`, `codestral`). eVi picks one automatically;
   override with the `evi.completionModel` setting.
3. Node 18+ and VS Code 1.90+.

## Run it (development)

```bash
cd editors/vscode
npm install
npm run compile        # or: npm run watch
```

Then press **F5** in VS Code (with this folder open) to launch an Extension
Development Host with eVi loaded. Start typing for ghost-text; open the **eVi**
icon in the Activity Bar for chat.

## Install for daily use

```bash
cd editors/vscode
npm install && npm run compile
npm run package        # produces evi-vscode-0.1.0.vsix
```

Then in VS Code: **Command Palette → "Extensions: Install from VSIX…"** and pick
the `.vsix`. (Publishing to a marketplace is optional and not required.)

## Settings

| Setting | Default | What |
|---|---|---|
| `evi.serverUrl` | `http://127.0.0.1:8473` | Your eVi server |
| `evi.authToken` | `""` | Bearer token if `[web] auth_token` is set |
| `evi.autocomplete.enabled` | `true` | Inline ghost-text completions |
| `evi.completionModel` | `""` | Override FIM model (empty = eVi picks) |
| `evi.maxTokens` | `128` | Tokens per completion |
| `evi.debounceMs` | `250` | Typing-pause delay before requesting |

Commands: **eVi: Toggle Autocomplete**, **eVi: Open Chat**, **eVi: Explain Selection**.

## Notes / roadmap

- Chat uses its **own** webview sidebar (no GitHub Copilot dependency) by design.
- Phase 4 (optional): factor the HTTP calls into a shared agent/LSP server so
  JetBrains / Neovim / Emacs can reuse the same logic (LSP gives dropdown, not
  ghost-text — VS Code keeps the native inline API).
