# Review checklist (eVi)

Tracked, loaded by `evi review` and the `evi task verify` review gate
(`evi/review.py`). A finding is blocking when it breaks one of these.

## Scope and contract

- The diff stays inside the task's `files_in_scope`; anything else is named
  in the hand-off, not silently changed.
- No edits to `.github/workflows/`, the version (`pyproject.toml`,
  `evi/__init__.py`), a released CHANGELOG entry, `EVI.md` or `.evi/**`
  unless the task names the file.
- Nothing touches the real `~/.evi` or `$EVI_HOME`: tests use `tmp_path`,
  `monkeypatch` and the throwaway EVI_HOME from `tests/conftest.py`.

## Tests

- Every behaviour change ships a test that fails without it. Agent-loop
  changes: an `Agent.chat`-level test on the shared fake LLM. UI or `/api`
  changes: a Playwright test in `tests/e2e/`. Security fixes: an exploit
  regression test.
- Tests exercise the change, not a mock of it; no `sleep`-based timing.
- Existing tests are not weakened or deleted to make a change pass.

## Security posture

- Anything unattended fails closed: a missing callback, an unknown tool or an
  unparsable command means deny.
- Repository content (`.evi.toml`, hooks, commands, MCP, skills, checks) is
  used only behind the folder-trust gate; project config only tightens.
- Subprocesses go through `evi.proc` (killable tree, `EVI_AGENT=1`, scrubbed
  environment for unattended runs); no `shell=True` on text a model or a
  repository supplies.
- Paths are checked on their real path (symlinks, junctions, Windows case
  and device names); redirects and arguments are confined like paths.
- Text that reaches a terminal, HTML or a log from a model, a repository or a
  task file is escaped or redacted (`evi.redact`, `textContent`).
- Nothing lets an agent mark work verified, read the director key or widen
  its own permissions.

## Correctness and style

- Windows and POSIX both work (paths, line endings, process trees, encodings).
- Errors are reported with the reason; no bare `except` that hides a failure.
- New files use LF; the change matches the surrounding naming, comment
  density and idiom; no drive-by refactors.
- Docs and the CHANGELOG `[Unreleased]` section change with behaviour.
