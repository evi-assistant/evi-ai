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
   ([section 3](#3-worktrees-until-the-phase-1-task-loop));
5. finishes with `python -m evi doctor --dev --strict`.

`--dry-run` prints every step and changes nothing. An option that takes a value
refuses one that starts with `-`, so `--extras --dry-run` is an error, not a real
run. The PowerShell script takes the same options in PowerShell style: `-Venv`,
`-Recreate`, `-Worktree`, `-DryRun` and so on.

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
`/auto on`.

A change, driven by eVi:

1. Describe the task, or give eVi the task brief.
2. eVi reads `docs/features/<area>.md` and the modules it names, then edits with
   `edit_file`.
3. eVi runs the tests and lint for the files it touched ([section 4](#4-test-and-lint)).
4. eVi records each bug it fixed with `record_fix`, updates the feature doc and
   `examples/` when behaviour changed, and writes the hand-off that `EVI.md`
   describes. The director reviews and commits.

Tips: `evi workflow` splits a large change into plan, implement and test stages.
An eval suite (`evi eval`) is a regression gate for behaviour you care about.
`evi route` sends coding turns to a strong local coder model
([examples/routes.json](../examples/routes.json)).

## 3. Worktrees (until the Phase 1 task loop)

The director gives each task its own git worktree, and the implementer works
only there:

```bash
bash scripts/dev-bootstrap.sh --worktree fix/doctor-eol
```

This creates the branch `fix/doctor-eol` (from `HEAD`, or from `--base REF`) at
`.worktrees/fix__doctor-eol` (a `/` in the branch name becomes `__`). It adds
`.worktrees/` to `.git/info/exclude`, so the main checkout never stages a
worktree, and runs `doctor --dev` in the new worktree. Run again, it reuses the
worktree only while it is still on that branch: `fix/x` and `fix__x` would share
a directory, so the second one is refused. The worktree shares the venv, whose editable install points at the
main checkout, so run eVi and the tests from the worktree as modules:

```bash
python -m evi chat --mode code
python -m pytest -q -n auto tests/test_doctor_dev.py
```

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
on the way. Docs under `docs/` are published to the wiki automatically from
`main` by `.github/workflows/docs-publish.yml`; nobody edits the wiki directly.
See [releasing.md](releasing.md).

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
