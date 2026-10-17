# Self-build — developing eVi with eVi

eVi maintains itself. It has what that takes: a coding agent with file, shell
and git tools, a permission model, the test suites and a one-command desktop
build. Two files drive it:

- **[`EVI.md`](../EVI.md)**, auto-loaded as project context whenever eVi runs
  inside this repo (`evi/project.py`). It is the implementer contract: what never
  to do, the workflow, and the hand-off. It stays under 4 KB, and
  `tests/test_bootstrap.py` fails if it grows past that or picks up facts that
  go stale: a release status, a typed-in test count, an old checkout path.
- **This guide**: the procedures behind the contract.

`tests/test_self_build_commands.py` runs every command in this guide's code
blocks, against a fake LLM backend in a temporary `EVI_HOME`. A command that
stops working fails the suite.

## 1. Set up a development environment

The documented setup is the one the maintainers' Windows machine runs. Linux and
macOS work the same way.

- **Python 3.13 in a uv-managed venv outside the checkout**: `~/.venvs/evi-313`.
  It is outside because the checkout lives on a network share that more than one
  machine uses. A venv records its machine's interpreter in `pyvenv.cfg`
  (`home = ...`), so an in-repo `.venv` made on one machine is *foreign*, and
  broken, on every other.
- **git `safe.directory`** for the share. git refuses a checkout owned by another
  account ("dubious ownership"). `evi doctor --dev` prints the exact one-time
  `git config --global --add safe.directory ...` command.
- **pytest-xdist**, so the suite runs with `-n auto`.
- **Rust, for desktop work only**: the GNU toolchain plus WinLibs MinGW-w64 when
  the MSVC Build Tools are not installed (see
  [section 5](#5-build-the-desktop-app)).

One command creates or repairs all of it:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev-bootstrap.ps1
```

```bash
bash scripts/dev-bootstrap.sh
```

The bootstrap:

1. checks that git trusts the checkout. If it doesn't, it prints git's own
   `safe.directory` command and trusts the path for that run only;
2. picks the venv: `--venv DIR`, else `$EVI_DEV_VENV`, else `~/.venvs/evi-313`
   (`--python 3.12` gives `evi-312`). It reuses a healthy venv as is. It
   recreates a *foreign* one, whose base interpreter does not exist on this
   machine, after moving the old one aside to `<venv>.foreign-<time>` (never
   deleting it: a venv that looks foreign from one account may still work from
   another). Any other broken or too-old venv, and any venv inside a git
   checkout, is replaced only with `--recreate`, which deletes it;
3. creates venvs with `uv venv --python 3.13`, or `py -3.13 -m venv` /
   `python3.13 -m venv` without uv, and installs eVi editable with the extras
   `dev,web,mcp,scheduler,downloads,web-tools,index,e2e` plus `pytest-xdist`.
   `--reinstall` repeats the install into an existing venv, `--extras` changes
   the list, and `--playwright` also downloads Chromium;
4. with `--worktree BRANCH`, creates a worktree for the branch
   ([section 3](#3-worktrees-one-per-task));
5. finishes with `python -m evi doctor --dev --strict`, in the worktree when
   there is one;
6. with `--test [ARGS]` (put it last: every argument after it goes to
   pytest), then runs `python -m pytest -q -p no:cacheprovider ARGS` where the
   check ran, once it has passed, and exits with pytest's exit code.

`--dry-run` prints every step and changes nothing. An option that takes a value
refuses one that starts with `-`, so `--extras --dry-run` is an error, not a real
run. The PowerShell script takes the same options in PowerShell style: `-Venv`,
`-Recreate`, `-Worktree`, `-DryRun`, `-Test` and so on. After `-Test`, the
arguments no option takes go to pytest; put a pytest option PowerShell would
read as one of its own (`-v`, `--verbose`, `-p`) in `$env:PYTEST_ADDOPTS`.

### `evi doctor --dev`

```bash
evi doctor --dev
```

It checks, and never changes anything:

| Check | Fails or warns when |
|---|---|
| python, evi import | Python is older than 3.12, is not a venv, or imports `evi` from another checkout |
| venv | the venv in use is foreign or broken (fail); a `.venv` or `.venv-build` the checkout carries is foreign (warn, since it may be another machine's) |
| git safe.directory | git refuses the checkout (prints the fix) |
| dev tools, pytest-xdist | pytest, pytest-timeout, ruff or pytest-xdist is missing |
| playwright | the e2e extra or its Chromium is missing |
| pytest --collect-only | collection fails |
| line endings | tracked files disagree with `.gitattributes` |

`--repo DIR` checks another checkout, `--venv DIR` another venv, and `--strict`
exits 1 when any check fails. The fix commands it prints name the venv being
checked (`<venv python> -m pip install ...`, or `uv pip install --python ...`
for a uv venv without pip).

Pointed at any checkout, doctor runs none of its code. Its git calls run no
program the repository configures (`core.fsmonitor`, hooks, diff drivers,
clean filters), and the interpreter probe runs isolated (`python -I`). `pytest --collect-only` imports the
checkout's `conftest.py`, so by default it runs only for the eVi checkout
doctor itself runs from, or another worktree of it. `--collect` runs it on any
checkout, and `--no-collect` skips it.

**Line endings.** `.gitattributes` asks for LF everywhere except `*.ps1`, which
is CRLF. An editor on Windows, or `core.autocrlf=true`, can leave a working file
with the other ending. The index is still right, so `git status` stays clean,
but every tool that edits the file sees the wrong endings. `doctor --dev` lists
those files and prints the re-checkout command,
`git --literal-pathspecs rm --cached -q -- <files>; git --literal-pathspecs checkout HEAD -- <files>`,
to run from the checkout root in bash or PowerShell. A plain `git checkout`
skips files git believes are unchanged. Literal pathspecs keep a name such as
`[ab].md` from also matching `a.md`. Files with local edits are listed
separately, because a re-checkout would discard the edits, and so is a name
with a quote in it, which no quoting carries safely in both shells. When the
index itself holds CRLF, it prints `git add --renormalize .` instead. Nothing is
ever converted automatically.

## 2. The development loop

Run eVi from the checkout, so it loads `EVI.md`:

```bash
evi chat --mode code
evi run --mode code "list the TODO comments in evi/doctor.py"
```

`--mode code` gives the session the Code tool preset: file, code, shell, git,
semantic-search and subagent tools, plus memory and skills. It is the same set
as the web UI's Code mode; `chat` and `cowork` are the other presets. Without
`--mode`, the session uses the tools enabled in your config. Under
`--safe-mode`, `chat --mode` still leaves out the memory, skills and MCP tools.
Approve shell and git calls as they come. Implementer runs never use `-y` or
`/auto on`: a headless implementer run uses `--policy implementer` instead
([section 3](#3-worktrees-one-per-task)).

A change, driven by eVi:

1. Describe the task, or give eVi the task brief.
2. eVi reads `docs/features/<area>.md` and the modules it names, then edits with
   `edit_file`.
3. eVi runs the tests and lint for the files it touched ([section 4](#4-test-and-lint)).
4. eVi records each bug it fixed with `record_fix`, updates the feature doc and
   `examples/` when behaviour changed, and writes the hand-off that `EVI.md`
   describes. The director reviews, commits and verifies; eVi can't mark its
   own work verified ([section 6](#6-shipping-director-only)).

Tips: `evi workflow` splits a large change into plan, implement and test stages.
An eval suite (`evi eval`) is a regression gate for behaviour you care about;
eVi's own is `evals/self` ([section 4](#4-test-and-lint)).
`evi route` sends coding turns to a strong local coder model
([examples/routes.json](../examples/routes.json)).

## 3. Worktrees: one per task

Every task gets a git worktree and branch of its own, and the implementer works
only there: a change in progress can't break the checkout eVi runs from, and
several fixes can go on side by side. Nothing is merged by the implementer; the
director reviews each branch and merges it.

**A task from the queue.** This is how the director delegates work. File the
task with `evi task new --title "…" --acceptance "…" --scope <file> --hint
"…" --check "<command>"` (it goes to `.evi/tasks/T<n>.toml`), then run it
with `evi task start T<n>`. `--scope` (repeatable) names the only files the
run may write, so give every file the work needs, its tests included; a task
without one is refused at start. `--hint` (repeatable) tells the implementer
how to go about it: where to look, what to reuse, what to avoid. eVi claims
the task, opens branch `task/T<n>` at `.worktrees/task__T<n>`, runs the
implementer under the implementer policy, confined to the task's scope (a
write to any other file is denied, and the model reports it as a concern
instead), with the verify gate on, and then runs the task's required checks
itself. The task ends `implemented` (exit 0) or back to
`open` with the attempt counted, the run report goes under `~/.evi/tasks/`
and its evidence bundle (the diff, eVi's own run of the required checks, a
redacted transcript excerpt) under `~/.evi/evidence/`; `evi task show T<n>`
lists anything the branch changed outside the scope anyway. The implementer
never commits: review its work in the worktree, commit it on `task/T<n>`
there, `evi task submit T<n>` (which collects the bundle again from your
commit), and sign it with `evi task verify T<n>`, which refuses while the
branch changes files outside the scope (unless `--allow-out-of-scope
"<why>"`) and runs the multi-lens review gate first ([section
6](#6-shipping-director-only)). `evi task reopen T<n>`
sends it back for another attempt in the same worktree, and `evi task reject
T<n> --reason "…"` turns it down. `evi task list` shows where every task
stands. The repository must be trusted, and a delegate CLI-agent backend is
refused. A director working from an MCP client can file and start tasks
through `evi mcp serve --categories agent` instead; verifying still happens
in the director's own shell. See [Task queue](features/tasks.md) and
[Evidence bundles](features/evidence.md).

**A headless run.** Without a task file, `evi run --policy implementer
--worktree auto --mode code "<task brief>"` makes a fresh branch `fix-1`
(then `fix-2`, …) at `.worktrees/fix-1` and runs there; `--worktree <branch>`
names the branch instead, and a later run with the same name carries on in
its worktree.
`--policy implementer` takes the place of `-y`, which it refuses: the run may
read and edit files in the worktree, run pytest, ruff and read-only git there,
and nothing else (no network, no `run_python`, no subagents), and every other
call is denied with a reason instead of asked. Protected files (`EVI.md`,
`.github/workflows/…`) stay denied unless the task names them with
`--scope <file>`; `.evi/tasks/**` never opens. Add `--confine` to hold every
write to the `--scope` entries, as a task run is. Before the run may finish,
eVi runs ruff on the `.py` files it changed and the matching
`tests/test_<module>.py` files, and hands failures back to the model (at most
`[checks] max_rounds` failing rounds, then the run exits 5,
`checks_failed`). This needs the repository to be trusted (`evi trust`, from
your own terminal): in an untrusted folder the policy runs no commands and no
checks. See [Implementer
policy](features/permissions-and-sandbox.md#implementer-policy) and [Verify
before done](configuration.md#verify-before-done--checks-in-configtoml). `--base REF` picks where a new branch starts
(default `[worktree] base_ref`, else `HEAD`). The worktree is the run's folder:
`EVI.md` and the anatomy come from it alone, its `.evi.toml` applies when the
repository is trusted (a worktree shares its repository's folder trust), and
the main checkout is never touched. The worktree stays locked while the run goes on, and with
`--format json` the report's `worktree` field gives the branch, its commits,
what is left uncommitted and the diffstat. A branch checked out in the main
checkout is refused. See [the run
report](features/structured-and-batch.md#the-run-report). `evi team run
--worktree` and workflow steps with `isolate = "worktree"` give each task a
worktree the same way ([Isolated runs](features/agents.md#isolated-runs-git-worktrees)).

**A worktree to work in.** The bootstrap makes one for a session you drive
yourself, and runs the tests in it:

```bash
bash scripts/dev-bootstrap.sh --worktree fix/doctor-eol
bash scripts/dev-bootstrap.sh --worktree fix/doctor-eol --test tests/test_doctor_dev.py
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev-bootstrap.ps1 -Worktree fix/doctor-eol -Test tests/test_doctor_dev.py
```

This creates the branch `fix/doctor-eol` (from `HEAD`, or from `--base REF`) at
`.worktrees/fix__doctor-eol` (a `/` in the branch name becomes `__`). It adds
`.worktrees/` to `.git/info/exclude`, so the main checkout never stages a
worktree, and runs `doctor --dev` in the new worktree; `--test` then runs
pytest there with the arguments after it. Run again, it reuses the
worktree only while it is still on that branch: `fix/x` and `fix__x` would share
a directory, so the second one is refused. The worktree shares the venv, whose editable install points at the
main checkout, so run eVi and the tests from the worktree as modules
(`python -m` puts the worktree first on the import path, which is also how
`--test` runs them):

```bash
python -m evi chat --mode code
python -m pytest -q -n auto tests/test_doctor_dev.py
```

Once a branch is merged, `evi worktree prune` (run from the main checkout)
removes its worktree if nothing is left uncommitted or untracked there; it
keeps the others and says why, and leaves the branches alone.

## 4. Test and lint

```bash
python -m pytest -q -n auto
python -m ruff check evi tests scripts
python -m pytest -q -m e2e --timeout=180 tests/cli_e2e
python -m pytest -q -m e2e --timeout=180 tests/e2e
python -m playwright install chromium
```

The first command runs the unit suite (e2e tests are excluded by default).
`tests/cli_e2e` drives the real `evi` CLI as a subprocess, and `tests/e2e`
drives the web UI in Chromium, which the last command installs once. While
working, run the subset that covers your change; run the full unit suite before
the hand-off. The live test count is the last line of
`python -m pytest --collect-only -q`. [TESTING.md](../TESTING.md) describes the
layers.

**The eval gate.** The unit tests can't see a change in how the model
behaves. A change to eVi's prompts, its tool-call repair or the agent loop
(the Phase 2 work) is held to the qualification suite in `evals/self` and the
reference model's recorded pass rates: `evi eval run self --baseline
evals/self/baseline.json --backend <saved backend> --model <reference model>
--runs 5`, on the model and backend the baseline names, must exit 0. It fails
a case whose pass rate fell more than `--tolerance` (default 0.1) below the
baseline's, and a baseline case that didn't run (skipped, deleted, renamed,
or left out by `--tag` / `--skip-tag`: only `--partial` excuses those, and a
partial run is no gate for a release); a case added after the baseline was
recorded is held only to its own `pass_rate` until the baseline is recorded
again. `evi eval run self
--backend fake` checks the harness and the suite with no model, and the unit
suite runs it too. The `self` suite loads only in a trusted checkout (`evi
trust`). The director records the baseline from their own shell with `evi
eval baseline record --suite self --runs 5 --backend <saved backend> --model
<reference model> --quantization <quantization>`. The implementer can do
neither: that command refuses in any process an eVi agent started, and
`evals/self/baseline.json` is never granted under the implementer policy. See
[Evals](features/evals.md#the-baseline-gate).

## 5. Build the desktop app

```powershell
powershell -File scripts\build-desktop.ps1
```

```bash
bash scripts/build-desktop.sh
```

`build-desktop` runs `build-sidecar` (a PyInstaller `--onedir` freeze of the
web server, from `.venv-build`) and then `npm run tauri build`. The installers
land in `desktop/src-tauri/target/release/bundle/{msi,nsis}/`. The final Tauri
step may exit non-zero on the optional updater-signing step, which needs the
CI-only `TAURI_SIGNING_PRIVATE_KEY`; the installers are still complete. See
[desktop-bundling.md](desktop-bundling.md).

To check the Rust shell without MSVC, install the GNU toolchain
(`rustup toolchain install stable-x86_64-pc-windows-gnu`, then
`rustup default stable-x86_64-pc-windows-gnu`) and unpack a WinLibs MinGW-w64
build, for example into `~/tools/winlibs`. Then:

```bash
export PATH="$HOME/.cargo/bin:$HOME/tools/winlibs/mingw64/bin:$PATH"
cd desktop/src-tauri
cargo check --locked
cargo test --locked
```

## 6. Shipping (director only)

The implementer stops at the hand-off. The director reviews it, commits with the
template in `EVI.md`, merges and releases; CI runs the unit, lint and e2e suites
on the way. For a task from the queue, the message that lands it comes from
the task itself: once `evi task verify T<n>` has signed it,
`evi evidence commit-body T<n> > msg.txt` writes the subject, the why, the
acceptance criteria, the checks from the evidence bundle and the files
changed, then the trailers `Implemented-by`, `Task`, `Evidence` and, from
the signature alone, `Verified-by`, `Review-rounds` and `Attestation`. Merge
with it, `git merge --no-ff -F msg.txt task/T<n>`, and add no commit to
`task/T<n>` after verifying: the signature names that branch's head.
`evi stats --provenance` later counts the work eVi implemented from those
trailers, counting only what the director key signed. See [Commit
messages](features/evidence.md#commit-messages). Docs under `docs/` are published to the wiki automatically from
`main` by `.github/workflows/docs-publish.yml`; nobody edits the wiki directly.
See [releasing.md](releasing.md).

**Only the director marks work verified.** A task's `verified` or `rejected`
status counts only when it carries an attestation signed with the director's
key (`evi/attest.py`), which lives in the OS keyring or in
`~/.evi/director/`. `evi attest init` makes the key once per machine, and
`evi attest status` shows its id and where it is kept. A status the
implementer writes into a task file itself, a task edited after it was
signed, or one whose branch has moved on since, reads as unverified in
`evi task list` and `evi task show`. The implementer can't reach the key or
the tooling that signs with it: in an eVi session a tool call that touches the
key folder, sets or clears `EVI_AGENT` / `EVI_DIRECTOR`, or runs
`evi task verify`, `evi task reject` or `evi attest` is denied in every mode,
`yolo` and `/auto on` included. Every process eVi's agent-hosting commands
start (`evi chat`, `evi run`, `evi task start`, `evi web` and the desktop
app's server …) carries `EVI_AGENT=1`, where director tooling refuses to run
and never reads the key, and where `evi trust` refuses too. So the director
signs from their own shell, never from inside `evi chat` or a command eVi
runs. `evi task verify` also wants the task's worktree at the branch head
with nothing uncommitted, so commit the implementer's work on `task/<id>`
before you verify it: the signature then covers exactly the commit you
reviewed. Details and limits: [security.md](security.md#the-directors-attestation-key)
and [Task queue](features/tasks.md#verify-and-reject-director-only).

## Local-model notes (from dogfooding)

Running eVi against its own repo with a local coder model surfaced two things,
both fixed, that matter for any local-model use:

- **Surgical edits.** eVi has an `edit_file` tool (exact `old_string` →
  `new_string`). A small local model can't reliably regenerate a 300-line file
  with `write_file`, but it *can* produce a one-spot edit, so prefer `edit_file`
  for changes. It is also far cheaper in tokens.
- **Text-emitted tool calls.** Some local models (e.g. qwen2.5 via Ollama) print
  a tool call as JSON *content* instead of using the structured `tool_calls`
  field, sometimes as a Python/JSON hybrid (single-quoted strings, lowercase
  `false`). eVi recovers these (`recover_text_tool_calls` in
  `evi/llm/agent.py`), so the call still runs. Frontier models over real APIs
  always return structured calls.

Worked dogfood: with `qwen2.5-coder:14b` as the model, eVi fixed a real bug in
its own `evi/recommend.py` (a string-vs-numeric compute-capability comparison)
with a single `edit_file` call.

## Why this works (and its limits)

eVi's agent is model-driven: how well it develops itself scales with the model
you give it. `EVI.md`, the test suites and the bootstrap remove the *tooling*
gap. What remains is model capability and review: treat eVi's changes like any
PR, and read the diff and the test results before anything is committed.

See also: [TESTING.md](../TESTING.md), [development.md](development.md),
[desktop-bundling.md](desktop-bundling.md), [releasing.md](releasing.md).
