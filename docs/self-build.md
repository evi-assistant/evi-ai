# Self-build — developing and building eVi *with* eVi

eVi is meant to maintain itself. It already has everything needed to work on its
own source: a coding agent with file/shell/git tools, an isolated permission
model, the full test suite, and a one-command desktop build. This guide is the
"bootstrap" — how to drive changes, tests, and builds **through eVi** instead of
an external assistant.

The two enabling artifacts:

- **[`EVI.md`](../EVI.md)** at the repo root — auto-loaded as project context
  (`evi/project.py`) whenever you run eVi from `C:\evi`. It tells eVi's agent the
  repo map, the two-venv rule, the build/test commands, and the conventions.
- **`scripts/build-desktop.{ps1,sh}`** — the single "build the whole app"
  entrypoint (freeze the sidecar → bundle the Tauri app), which eVi can run with
  its shell tool.

## The development loop

Run eVi from inside the repo so it picks up `EVI.md`:

```bash
cd C:\evi
evi chat --mode code          # or: evi run --mode code "…"  for one-shot
```

A typical change, driven by eVi:

1. **Describe the task** ("add an `evi skill rename` command", "fix X in
   `server.py`"). eVi has the repo map + conventions from `EVI.md`.
2. eVi **reads** the relevant `docs/features/<area>.md` and module(s), then
   **edits** with its file tools.
3. eVi **tests**: `pytest -q` plus the matching e2e suite (`tests/cli_e2e` for CLI
   changes, `tests/e2e` for UI). Approve the shell calls (or use `--mode code`
   which pre-approves the dev toolset; or `/auto on`).
4. eVi **lints**: `ruff check evi tests scripts`.
5. eVi updates the **feature doc + `examples/`** if behavior changed, and
   **commits** with a Conventional Commit message.

Tips:
- A **subagent / workflow** (`evi workflow`) can split a big change into
  plan → implement → test stages.
- An **eval suite** (`evi eval`) makes a good regression gate for behavior you
  care about; a **routine**/**scheduler** job can re-run it.
- Give eVi a strong local coding model (e.g. a `qwen2.5-coder` via Ollama/LM
  Studio); route coding turns to it with `evi route` (see
  [examples/routes.json](../examples/routes.json)).

## Building the app

```bash
# unit + lint
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check evi tests scripts

# desktop app (sidecar + Tauri) — produces the installers
powershell -File scripts\build-desktop.ps1     # Windows
bash scripts/build-desktop.sh                  # POSIX
```

`build-desktop` runs `build-sidecar` (PyInstaller `--onedir` freeze of the web
server, using `.venv-build`) then `npm run tauri build` with the standalone
config. Installers land in
`desktop/src-tauri/target/release/bundle/{msi,nsis}/`.

> **Note:** the final Tauri step may exit non-zero on the optional
> *updater-signing* step (it needs the CI-only `TAURI_SIGNING_PRIVATE_KEY`). The
> installers are still complete and runnable — the scripts report this rather
> than treating it as a failure. See [desktop-bundling.md](desktop-bundling.md).

## Shipping a change

1. Tests green (`pytest -q` + the relevant e2e), `ruff` clean.
2. Conventional Commit on a branch (don't commit on `main` without intent).
3. Docs touched? Mirror the changed `docs/*.md` to the public wiki
   (`dmang-dev/evi-ai-releases.wiki`) — see [releasing.md](releasing.md).
4. **Releases are currently paused** on a GitHub Actions billing block (a
   human-only fix). Until that clears, keep changes local on `main`; don't tag or
   trigger the release workflows.

## Why this works (and its limits)

eVi's agent is model-driven: how well it can self-develop scales with the local
model you give it. `EVI.md` + the test suite + the build script remove the
*tooling* gap — eVi can read, edit, test, and build itself unattended. What
remains is model capability and your review. Treat eVi's changes like any PR:
read the diff, check the tests, then commit.

See also: [TESTING.md](../TESTING.md) (the four test layers),
[development.md](development.md), [desktop-bundling.md](desktop-bundling.md),
[releasing.md](releasing.md).
