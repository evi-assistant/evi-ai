# Surface parity — CLI ↔ Web ↔ Desktop

eVi has three front-ends over one engine. The **CLI** is the most complete (it's
the admin + scripting surface); the **Web** UI is chat-centric with a full
settings screen; the **Desktop** app is the Web UI in a native shell (menus,
tray, updater, deep links). This table maps every CLI command group to its
Web/Desktop equivalent and is honest about what is intentionally CLI-only.

Legend: ✅ first-class · ◐ partial / via a related surface · ⌨️ CLI-only by design
(admin/scripting) · ➕ gap worth filling.

| CLI | What it does | Web | Desktop | Notes |
|-----|--------------|-----|---------|-------|
| `chat` | interactive chat | ✅ main view | ✅ | the core loop |
| `run` | headless one-shot | ◐ `/api/chat` | ◐ | scripting; no "run once" button (not needed) |
| `batch` | many prompts → JSONL | ⌨️ | ⌨️ | batch/scripting |
| `eval` | prompt→assertion suites | ✅ settings → Evals | ✅ | browse suites/cases + run with per-case PASS/FAIL |
| `agents` | list subagent profiles | ◐ used by `delegate` | ◐ | a "profiles" list could live in Dispatch |
| `workflow` | run/list/new/show | ◐ run+list via 🗂 Dispatch | ◐ | authoring stays CLI |
| `peer` | federation peers | ⌨️ | ⌨️ | `~/.evi/peers.json`; `/api/federate` serves |
| `link` | make `evi://` deep links | n/a | ✅ scheme handler | desktop opens the links |
| `stats` | local usage analytics | ✅ settings → Usage | ✅ | sessions/messages/tokens, roles, top tools, busy days |
| `sessions` | list/resume/fork/handoff | ✅ tabs/history; handoff API | ✅ | resume via `/?session=` |
| `recipe` | saved multi-turn flows | ✅ settings → Routes & Recipes | ✅ | browse steps + run; also via `routine` webhook |
| `routine` | webhook → recipe | ◐ `/api/routine/{token}` | ◐ | inbound trigger |
| `style` | output styles | ✅ settings (llm.output_style) | ✅ | |
| `voice` | TTS engine/speak/listen | ✅ settings → Voice; speak toggle | ✅ | |
| `guardrails` | content filter rules | ✅ settings → Guardrails | ✅ | validated `guardrails.toml` editor + rule summary |
| `plugin` | add/list/remove/search/install | ✅ settings → Plugins | ✅ | list installed, search marketplace, install/remove |
| `mcp` | MCP servers/serve | ◐ settings (mcp toggle + allowlist) | ◐ | |
| `models` | list/use/info/pull | ✅ Model picker + settings | ✅ | recommended-pull flow |
| `config` | show/path | ✅ full settings screen | ✅ | the settings UI *is* config |
| `route` | multi-model routing | ✅ settings → Routes & Recipes | ✅ | add/list/remove routing rules |
| `sync` | git sync of ~/.evi | ⌨️ | ⌨️ | machine admin |
| `backup` | backup/restore state | ⌨️ | ⌨️ | machine admin |
| `calendar`/`obsidian` | integrations | ◐ settings → Integrations | ◐ | |
| `doctor` | diagnostics | ✅ Help → Run Diagnostics | ✅ | |
| `update` | self-update | n/a | ✅ Help → Check for Updates | desktop updater |
| `rewind` | undo file writes | ✅ rewind dialog | ✅ | checkpoints |
| `setup` | first-run wizard | ◐ | ✅ first-run | desktop onboarding |
| `review` | multi-agent code review | ⌨️ | ⌨️ | dev workflow |
| `finetune` | export training data | ⌨️ | ⌨️ | scripting |
| `worktree`/`profile`/`scheduler` | dev/daemon | ⌨️ | ⌨️ | admin |

## In-chat parity (REPL slash commands ↔ web)

Many CLI REPL builtins have a web equivalent or are inherently REPL-only:

| REPL `/cmd` | Web equivalent |
|-------------|----------------|
| `/help` | (slash commands work in the web chat too) |
| `/model` | model picker chip |
| `/context`, `/ctx` | click the usage chip → breakdown popover |
| `/recent` | session tabs |
| `/tools`, `/notools`, `/forcetool` | settings → Tools; per-turn via API |
| `/effort`, `/fast`, `/json`, `/schema` | per-turn API params (`output_schema` for /schema) |
| `/auto`, `/plan` | auto + plan chips |
| `/compact` | automatic (config) |
| `/image`, `/audio` | attach button (📎) |
| `/speak` | speak toggle (🔇) |
| `/goal` | goal chip |

## Honest take on "full parity"

Several commands are **CLI-only by design** — they're machine admin (`sync`,
`backup`, `worktree`, `profile`, `scheduler`), batch/scripting (`run`, `batch`,
`finetune`, `review`), or developer tooling. Forcing a web button for those adds
surface without value.

The **gaps worth filling** (➕) — a web/desktop face for things a non-CLI user
might want — are now all shipped as settings sections: the **guardrails editor**
(Guardrails), the **plugin browser/installer** (Plugins), the **usage/stats
view** (Usage), the **evals results panel** (Evals), and **routes + recipes
management** (Routes & Recipes). Suite/recipe *authoring* still happens in TOML
files (or via `evi eval new` / `evi recipe new`), but browsing and running them
no longer requires the CLI.
