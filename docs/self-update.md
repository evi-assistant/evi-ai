# Self-update design (Phase 29 proposal)

`evi update` lets a user pull a newer eVi version, verify it works, and
roll back if it doesn't. This doc is the design — none of it is built
yet. Once approved, it becomes Phase 29.

## Goals

1. **One-command upgrade.** `evi update` is the only command a user
   should need.
2. **Always rollback-able.** Every upgrade snapshots the prior `pip
   freeze` so we can restore an exact known-good state.
3. **Refuse to clobber dev work.** Editable installs (`pip install -e
   .`) are never touched — we detect and refuse.
4. **Channels later.** Stable PyPI is the default. Pre-release + custom
   index URL come in a follow-up.
5. **Don't surprise locked envs.** Poetry / uv / pipenv projects get a
   clear "use your tool's upgrade command" message instead of a
   silently-broken venv.

## Non-goals (for now)

- Auto-updating the Tauri desktop binary — Tauri has its own updater;
  that's a separate phase.
- Updating dependencies independently of eVi (only pinned-via-extras
  matter for our use cases).
- Background / daemon checks. Update is always user-initiated.

## Command surface

| Command | Purpose |
|---|---|
| `evi update` | Check, show diff, prompt, upgrade. |
| `evi update check` | Check only — no install. |
| `evi update --yes` | Non-interactive upgrade. |
| `evi update --to <version>` | Pin an explicit version (downgrade allowed). |
| `evi update rollback` | Restore the most recent snapshot. |
| `evi update rollback <n>` | Restore the n-th most recent (1-indexed). |
| `evi update rollback <id>` | Restore a named snapshot dir. |
| `evi update history` | List snapshots. |
| `evi update prune --keep <n>` | Manual GC of old snapshots. |
| `evi update from-wheel <path>` | Offline install from a local wheel/sdist. |
| `evi update settings` | Print channel + snapshot retention. |

## Storage layout

```
~/.evi/snapshots/
    2026-05-27T14-03-00_0.10.0_to_0.11.0/
        requirements.txt        # pip freeze BEFORE the upgrade
        version.txt             # "0.10.0"
        metadata.json           # {"timestamp", "from", "to", "command"}
    2026-06-02T09-12-44_0.11.0_to_0.11.1/
        …
```

Snapshots are append-only. Default retention: **last 5**. A `prune`
runs at the end of each successful upgrade to drop older ones.

## State machine

```
                              ┌──────────────────┐
                              │  evi update      │
                              │  (or --check)    │
                              └─────────┬────────┘
                                        │
                                        ▼
                              ┌──────────────────┐
                              │  Probe PyPI      │   /pypi/evi-assistant/json
                              └─────────┬────────┘
                                        │
                          current ≥ latest? ──── yes ──► "up to date"
                                        │
                                        no
                                        │
                                        ▼
                              ┌──────────────────┐
                              │  Detect install  │   editable? pipx? venv? locked-env?
                              │      kind        │
                              └─────────┬────────┘
                                        │
                       editable / locked-env / unknown
                                        │
                                        ▼
                              ┌──────────────────┐
                              │  Refuse + hint   │   message tailored to install kind
                              └──────────────────┘

                       venv (the happy path)
                                        │
                                        ▼
                              ┌──────────────────┐
                              │  Take snapshot   │   pip freeze + metadata
                              └─────────┬────────┘
                                        │
                                        ▼
                              ┌──────────────────┐
                              │  pip install -U  │   --upgrade evi[==<version>]
                              └─────────┬────────┘
                                        │
                                  exit 0?
                                        │
                              ┌─────────┴────────┐
                              │                  │
                            no                  yes
                              │                  │
                              ▼                  ▼
                  ┌──────────────────┐  ┌──────────────────┐
                  │  Surface stderr  │  │  Verify import   │   subprocess: python -c "import evi"
                  │  + snapshot kept │  └─────────┬────────┘
                  │  + hint rollback │            │
                  └──────────────────┘    version matches?
                                                  │
                                          ┌───────┴────────┐
                                          │                │
                                         no              yes
                                          │                │
                                          ▼                ▼
                              ┌──────────────────┐  ┌──────────────────┐
                              │  Auto-rollback?  │  │  GC old snapshots│
                              │  Or warn + hint  │  │  + done          │
                              └──────────────────┘  └──────────────────┘
```

## Install-kind detection

We check, in order:

1. **Editable** — parse the output of `pip show evi-assistant`. If it has an
   `Editable project location:` line, refuse with the editable
   location printed and a hint to `git pull` instead.
2. **pipx** — if `PIPX_HOME` is in env OR `which evi` resolves under
   `~/.local/pipx/venvs/evi-assistant/`, suggest `pipx upgrade evi-assistant` rather than
   running pip ourselves.
3. **Locked env** — if cwd (or any parent up to `$HOME`) has a
   `poetry.lock` / `uv.lock` / `Pipfile.lock`, suggest the right tool's
   upgrade command. The user can pass `--force` to override.
4. **Plain venv / system pip** — the happy path. Run `pip install --upgrade`.

## The pip call

```python
subprocess.run(
    [sys.executable, "-m", "pip", "install", "--upgrade", spec],
    check=False, capture_output=True, text=True, timeout=600,
)
```

`spec` is either `evi-assistant` (latest) or `evi-assistant==<version>`. `sys.executable`
guarantees we install into the SAME interpreter that's running `evi`,
which is critical — using bare `pip` could pick up a different venv.

If `--from-wheel <path>` is used, `spec` becomes the path; pip handles
wheels and sdists transparently.

## Verification

After pip exits 0, we spawn:

```bash
python -c "import evi; print(evi.__version__)"
```

If that fails or returns the OLD version, we either:

- **Interactive**: ask the user "broken — roll back? [Y/n]"
- **`--yes`**: roll back automatically and exit non-zero with a clear
  error.

This catches the case where pip happily "succeeded" but the installed
package can't import (e.g. a dep version conflict that pip didn't catch
because eVi doesn't pin it strictly).

## Rollback

```python
subprocess.run(
    [sys.executable, "-m", "pip", "install", "-r", snapshot / "requirements.txt"],
    check=True, timeout=600,
)
```

Then re-verify the import.

Note: we restore ALL frozen packages, not just eVi. That's intentional
— if the failure was caused by a transitive bump, just reinstalling
eVi at the old version won't help. Restoring the full `pip freeze` is
the only way to get a deterministic rollback.

Risk: if the user installed something else in the venv between the
snapshot and now, the rollback wipes that. Mitigation: print the
`requirements.txt` diff before running and prompt to confirm. With
`--yes`, skip the prompt — the user explicitly opted in.

## Implementation skeleton

```
evi/
    update.py
        ─ check_pypi() → LatestInfo {version, release_url, changelog_url}
        ─ detect_install_kind() → "editable" | "pipx" | "locked" | "venv"
        ─ Snapshot dataclass {dir, timestamp, from_version, to_version}
        ─ create_snapshot(from_version, to_version) → Snapshot
        ─ list_snapshots() → list[Snapshot]
        ─ apply_upgrade(spec, *, dry_run=False) → UpgradeResult
        ─ apply_rollback(snapshot) → RollbackResult
        ─ verify_install() → (ok, installed_version, err)
        ─ gc_snapshots(keep=5) → list[deleted]
evi/apps/cli/main.py
    ─ update_app = typer.Typer()
    ─ @update_app.command("check")  → check_pypi() + render
    ─ @update_app.command()         → check + prompt + apply_upgrade
    ─ @update_app.command("rollback")
    ─ @update_app.command("history")
    ─ @update_app.command("prune")
    ─ @update_app.command("from-wheel")
    ─ @update_app.command("settings")
```

## Tests

- Mock `httpx.get("https://pypi.org/pypi/evi-assistant/json")` to return canned
  payloads (newer / same / older / 404).
- Mock `subprocess.run` for pip calls — verify command shape, capture
  exit codes, simulate success + failure.
- Real `tmp_path`-rooted snapshot dirs to exercise create / list /
  gc / rollback dispatch.
- `detect_install_kind` against synthetic `pip show` output.

## Open questions

1. **Should rollback be one-step or stepped?** Right now I propose
   "restore the full pip freeze". Alternative: only downgrade eVi
   itself, leave deps alone. Cleaner but doesn't recover from
   transitive bumps. I lean toward "full freeze, with a confirmation".
2. **Where do PyPI release notes come from?** The PyPI JSON API exposes
   `description` (the README of that release). We could surface that
   verbatim, or link to GitHub's release page. Probably both.
3. **Auto-update via the scheduler?** Could plug into the existing
   `evi/scheduler.py`. Defer — feels too aggressive without a "stable
   for N days" policy first.
4. **Versioning of snapshots themselves.** Right now I propose a
   directory-name convention; a small `snapshots.json` index would
   make `list` faster and more reliable. Worth doing from day 1.

## Estimated scope

- Implementation: ~400 LOC in `evi/update.py`, ~200 LOC of CLI wiring.
- Tests: ~300 LOC.
- Total: a short phase, comparable to Phase 22 (distribution tooling).
- No new deps — uses stdlib `subprocess`, `httpx` (already in core), and
  the user's installed `pip`.
