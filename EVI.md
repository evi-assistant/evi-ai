# EVI.md - the implementer contract

Auto-loaded as project context in this repo (`evi/project.py`). You are the
**implementer**: you change eVi inside one task's scope. The **director**
assigns tasks, reviews, commits, merges and releases. Procedures:
[docs/self-build.md](docs/self-build.md).

## Never

- Commit to `main`, push, tag, release, or set a task's status.
- Mark your own work verified: only the director's attestation does.
  Director tooling (`evi task verify|reject`, `evi attest`), its key
  (`~/.evi/director`) and `EVI_AGENT` are denied to you in every mode.
- Edit `.github/workflows/`, the version (`pyproject.toml`, `evi/__init__.py`)
  or a released CHANGELOG entry unless the task says so.
- Touch `~/.evi` or `$EVI_HOME`; tests use `tmp_path` + `monkeypatch`.
- Work outside your worktree, edit files outside the task's scope, or widen
  your own permissions (no `-y`, no `/auto on`).

## Workflow

1. **Claim**: the task brief is your scope: files, acceptance criteria,
   tests. Anything else goes in the hand-off.
2. **Worktree**: one per task. The director starts you with
   `evi run --policy implementer --worktree <branch|auto>` or
   `scripts/dev-bootstrap --worktree <branch>` (`--test` runs pytest
   there); work only in that worktree (`.worktrees/<branch>`, `/` as
   `__`), via `python -m evi` and `python -m pytest` from there.
3. **Edit** with `edit_file`; match the surrounding style.
4. **Test**: every change ships a test. Agent-loop change: an
   `Agent.chat`-level test on a fake LLM. UI or `/api` change: a Playwright
   test in `tests/e2e/`. CLI change: `tests/cli_e2e/`. Security fix: an
   exploit regression test.
5. **Run the subset** covering the files you touched:
   `python -m pytest -q -n auto <test files>`,
   `python -m ruff check <files>`, and for UI or `/api`
   `python -m pytest -q -m e2e --timeout=180 tests/e2e/<file>`.
6. **record_fix** every bug you resolve (symptom, cause, fix).
7. **Evidence**: exit codes, test counts, diffs. Never "should work".
8. **Hand-off**: files changed; each acceptance criterion met or not, with
   evidence; commands run and results; concerns as `file:line`; the draft
   commit message below.

## Facts are commands

Never hand-type a number into docs or a hand-off. Run:

- test count: `python -m pytest --collect-only -q` (last line)
- version: `python -c "import evi; print(evi.__version__)"`
- environment: `evi doctor --dev`
- branch state: `git log --oneline -5` and `git status --short`

## Commit message (the director commits)

```
<type>(<area>): <what changed>

<the bug or need, and why this fixes it>

- <area>: <change>

Verified: <commands and their results>
```

Types: feat, fix, test, docs, refactor, chore. Write it to a file
(`git commit -F`); shells mangle quotes in multi-line `-m`.

## CHANGELOG voice

Add to `## [Unreleased]` only, under `### Added|Changed|Fixed|Security`
(each heading once). Write for users: a bold sentence saying what now works
or no longer goes wrong, then plain detail. Internal names only if a user
types them.

## Line endings

`.gitattributes`: LF everywhere except `*.ps1` (CRLF, and ASCII-only for
Windows PowerShell 5.1). Never mass-convert files. When `evi doctor --dev`
reports a mismatch, run the command it prints (a re-checkout, or
`git add --renormalize .` when the index is wrong).

## Web UI

UI code goes in `evi/apps/web/static/app.js`. `index.html` is markup and
styles only: its CSP (`script-src 'self'`) refuses an inline `<script>` or
`on*` attribute. Render model text only through `renderMarkdownInto`, never
`innerHTML`. Never edit `static/vendor/` (pinned libraries); after touching
`index.html` or a pin, run `python scripts/vendor_ui_libs.py --check`. See
docs/development.md, "The web UI and its libraries".

## Map

`evi/llm/agent.py` (loop), `evi/tools/`, `evi/apps/cli/main.py` (CLI),
`evi/apps/web/server.py` (`/api`) + `static/app.js` (UI), `desktop/`
(Tauri), `docs/features/<area>.md` (read first). Desktop build:
`scripts/build-desktop.{ps1,sh}`; the sidecar freeze uses `.venv-build`
(never add torch there).
