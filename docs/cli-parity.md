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
| `eval` | suites that check the answer and what a run did: list/new/run/compare, `baseline record` (director only) | ◐ settings → Evals | ◐ | the panel shows the latest pass rate per suite, model and backend from `evi eval run`'s reports, and browses and runs your suites on the same engine as `evi eval run` (throwaway workspaces, the implementer policy, a report per run) with the configured backend; `--runs`, `--backend fake`, `compare` and the `--baseline` gate are CLI-only ([guide](features/evals.md)) |
| `ultracode` | exhaustive multi-agent pass | ✅ settings → Ultracode | ✅ | `/ultra` + `/effort ultracode` in REPL; stages shown in the panel |
| `agents` | list subagent profiles | ◐ used by `delegate` | ◐ | a "profiles" list could live in Dispatch |
| `workflow` | run/list/new/show | ◐ run+list via 🗂 Dispatch | ◐ | authoring stays CLI |
| `peer` | federation peers (list/add/remove/scan/run) | ✅ settings → Peers | ✅ | live status + LAN scan; `/api/federate` serves |
| `link` | make `evi://` deep links | n/a | ✅ scheme handler | desktop opens the links |
| `stats` | local usage analytics | ✅ settings → Usage | ✅ | sessions/messages/tokens, roles, top tools, busy days; `--provenance` (attested eVi share of a repository's history) is CLI-only |
| `sessions` | list/resume/fork/handoff | ✅ tabs/history; handoff API | ✅ | resume via `/?session=` |
| `recipe` | saved multi-turn flows | ✅ settings → Routes & Recipes | ✅ | browse steps + run; also via `routine` webhook |
| `routine` | webhook → recipe | ◐ `/api/routine/{token}` | ◐ | inbound trigger |
| `style` | output styles | ✅ settings (llm.output_style) | ✅ | |
| `voice` | TTS engine/speak/listen | ✅ settings → Voice; speak toggle | ✅ | |
| `guardrails` | content filter rules | ✅ settings → Guardrails | ✅ | validated `guardrails.toml` editor + rule summary |
| `hooks` | path/list/test (match resolution) | ✅ settings → Hooks | ✅ | validated `hooks.toml` editor; catches event typos |
| `command` | list/new (slash-command scaffold) | ◐ `/help` lists; typed in chat | ◐ | authoring stays markdown files |
| `keybindings` | path/list (effective bindings) | n/a | n/a | REPL-only feature |
| `plugin` | add/list/remove/search/install | ✅ settings → Plugins | ✅ | list installed, search marketplace, install/remove |
| `skill` | list/show/import/remove (incl. Claude skills) | ◐ used in chat via `invoke_skill` | ◐ | skills load on demand; no manager UI (file/CLI managed) |
| `mcp` | servers add/remove/enable/list + serve | ✅ settings → MCP | ✅ | manage servers in-app; env vars via CLI/file; the `agent` category (the director's task API) is served only by `evi mcp serve`, never from the app |
| `models` | list/use/info/pull/recommend | ✅ Model picker + settings | ✅ | recommended-pull flow; `evi models recommend --verify` (rank the recommended models on an eval suite) is CLI-only, and its reports show in settings → Evals |
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
| `task` | the repository's task queue: new/list/show/claim/release/submit/reopen/start, and the director's verify/reject | ⌨️ | ⌨️ | dev workflow; an MCP client can file, start, steer and cancel tasks through `evi mcp serve --categories agent`; verify and reject run only from the director's own shell ([guide](features/tasks.md)) |
| `attest` | the director's attestation key (init/status) | ⌨️ | ⌨️ | director only, never from inside eVi |
| `evidence` | evidence bundles: `evi evidence collect`, `evi evidence commit-body`, `evi evidence prune` | ⌨️ | ⌨️ | dev workflow; `evi run --evidence DIR` for a plain run ([guide](features/evidence.md)) |
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
files (or via `evi eval new` / `evi recipe new`), but browsing them, and
running recipes and answer-only eval suites, no longer requires the CLI. An
eval suite that checks what a run did (tools, files, commands) runs with
`evi eval run`, whose results the Evals panel then shows.
