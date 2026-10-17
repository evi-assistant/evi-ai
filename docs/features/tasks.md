# Task queue (`evi task`)

## Overview

The task queue is how eVi works on a repository, itself included, without
marking its own homework. The **director** (you) files a task with acceptance
criteria and the checks that prove them. `evi task start` runs eVi on it in a
git worktree of its own under the [implementer
policy](permissions-and-sandbox.md#implementer-policy). eVi runs the checks
itself and stops at `implemented`. Only the director's signed `evi task
verify` or `evi task reject` gives the task a verdict.

- **A task is a file in the repository.** Each task is one
  `.evi/tasks/<id>.toml`, tracked in git, so the queue is shared like any
  other file.
- **eVi stops at `implemented`.** A start never commits, merges or sets a
  verdict. It leaves the work uncommitted in the worktree of branch
  `task/<id>`, writes a run report, and sends the task back to `open` when
  the run or a required check failed.
- **Verdicts are signed.** `verified` and `rejected` count only with an
  attestation made with the director's key ([The director's attestation
  key](../security.md#the-directors-attestation-key)). A task file someone
  edited to say `verified` reads `unverified (unsigned)`.
- **Director tooling refuses where an agent may be.** `evi task verify`,
  `evi task reject` and `evi attest` refuse, and write nothing, in any process
  an eVi agent may have started.

The queue is CLI-only for now: the web app and `evi mcp serve` don't offer it.

## How it works

### What a task is

A task is a TOML table. `evi task new` writes the fields the director files;
`evi task start` and the verdict commands fill in the rest.

| Field | What it holds |
|---|---|
| `id` | `T<n>`, the next free number, unless you give one with `--id`: 1 to 64 letters, digits, `_` or `-`, not a Windows device name (`con`, `nul`, `com1` …), unique whatever its case. It names the file and the branch, `task/<id>`. |
| `title` | What the task is, in one line. Required. |
| `status` | `open`, `claimed`, `implemented`, `needs_review`, `verified` or `rejected` (see [States and leases](#states-and-leases)). |
| `why` | The bug or the need. |
| `acceptance` | The acceptance criteria, a list of strings. |
| `files_in_scope` | Globs relative to the repository. The implementer is told to change only these, and a protected file in the worktree (`EVI.md`, a CI workflow) becomes writable only when an entry names it, as `--scope` does for `evi run --policy implementer`. An empty, absolute or `..` entry is refused. |
| `required_checks` | The commands that prove the task: each a command line, split the POSIX way (use `/` in paths), or an argv array. A plain string instead of a list is refused. |
| `implementer` | Who should implement it, as a hint: `evi-local`, `evi-frontier` or `director`. Nothing enforces it. |
| `created` | When the task was filed (UTC). |
| `attempts` | How many starts ran, successful or not. |
| `backend`, `model` | What the latest start ran on. |
| `worktree`, `branch`, `base` | The task's worktree, its branch and the commit the branch started from. A verify reviews the diff from `base`. |
| `evidence` | The evidence bundle's path, `""` until there is one. |
| `reviewer_notes` | `{at, verdict, note}` entries, from a reopen, a verify or a reject. |
| `lease` | `{holder, expires_at}` while the task is claimed, empty otherwise. |
| `last_result` | The latest start's `{stop_reason, exit_code, report, summary}`. The summary is the first line of the answer, with control characters removed. |
| `attestation` | Only on a signed task: the director's signature ([Verify and reject](#verify-and-reject-director-only)). |

A new task looks like this:

```toml
id = "T1"
title = "Fix the parser's off-by-one"
status = "open"
why = "tokens at the end of a line are dropped"
acceptance = ["a trailing token is kept"]
files_in_scope = ["evi/parser.py", "tests/test_parser.py"]
required_checks = ["python -m pytest -q tests/test_parser.py"]
implementer = "evi-local"
created = "2026-10-17T10:42:07Z"
attempts = 0
backend = ""
model = ""
worktree = ""
branch = ""
base = ""
evidence = ""
reviewer_notes = []

[lease]
```

Unknown fields are kept, so you can add your own. Every value has to read
back from TOML exactly as it was written (TOML has no null), because the
attestation signs the values as parsed.

`files_in_scope` is not a hard edit boundary. The policy holds the run to its
worktree and to the protected-file rule above, but it doesn't stop an edit to
another unprotected file in the worktree. Checking the scope is part of your
review.

### Where the files live

The queue is `<repo>/.evi/tasks/`, where `<repo>` is the repository's main
checkout. `evi task` finds the same queue from any folder of the repository,
a task's own worktree included. The queue is whatever `.evi/tasks/` holds in
the main checkout's working tree, so commit a task file on the branch you
share the queue on.

Task files are tracked so that the queue travels with the repository: a
teammate who pulls sees the same tasks, and a task's record, its attestation
included, is in the history. eVi's own `.gitignore` ignores the rest of
`.evi/`:

```gitignore
.evi/*
!.evi/tasks/
!.evi/bug-ledger.jsonl
!.evi/REVIEW.md
!.evi/review-rules.md
*/**/.evi/
```

The rule is `.evi/*`, not `.evi/`, because git can't re-include a file inside
an ignored folder. Both rules are anchored to the repository's top level, so
`*/**/.evi/` keeps an `.evi/` folder deeper down ignored (a session started in
a subfolder leaves one there). The bug-fix ledger (`record_fix`,
`search_fixes`) and the review guidance (`evi review`) are shared the same
way. Another repository whose task queue you want in git needs the same
lines. Without them, `.evi/tasks.lock` shows up in `git status`; with a plain
`.evi/` rule, git ignores the task files too.

Every change to a task holds a lock on `.evi/tasks.lock`, so of two
concurrent claims one wins and the other sees the claim. A write goes to a
temporary file under `.evi/` and then replaces the task's file, so a crash
leaves either the old file or the new one. A write that can't be made, or a
lock still held after 15 seconds (`another eVi is changing the task queue`),
is an error and leaves the old file as it was.

An agent's write to a task file goes through the protected-path check:
`.evi/**` is protected, so the write asks in every mode, `yolo` included, and
is denied when nothing can answer. The implementer policy never opens
`.evi/tasks/**`, whatever the task's scope says. And a written verdict counts
for nothing without the director's signature.

Run reports are not in the repository. They go under eVi's home (see
[What `evi task start` does](#what-evi-task-start-does)).

### States and leases

| From | Step | To |
|---|---|---|
| `open` | `evi task claim`, or `evi task start` | `claimed` |
| `claimed` | `evi task release` | `open` |
| `claimed` | a start whose run finishes `done` with every required check passing | `implemented` |
| `claimed` | a start that fails in any other way | `open` |
| `implemented` | `evi task submit` | `needs_review` |
| `needs_review` | `evi task verify` (director) | `verified` |
| `needs_review` | `evi task reject` (director) | `rejected` |
| `implemented`, `needs_review`, `verified`, `rejected` | `evi task reopen` | `open` |

- `claim` makes a task `claimed`, with a lease: a holder (default
  `<user>@<host>`) and an expiry (`--ttl` minutes, default 240). The same
  holder claiming again renews the lease.
- Another holder's live lease refuses a claim, a release and a start, unless
  you pass `--force`. An expired lease, or one whose expiry can't be read,
  can be taken by anyone.
- `evi task start` claims the task itself and clears the lease when it
  finishes. The task ends `implemented` when the run finished and every
  required check passed, otherwise `open`. Either way the attempt is counted.
- `submit` hands an implemented task to review (`needs_review`). Only a
  `needs_review` task can be verified or rejected.
- `reopen` sends an implemented, needs_review, verified or rejected task back
  to `open` and drops its attestation, which no longer applies. To run an
  implemented task again, reopen it first.
- A step the status doesn't allow is refused (exit 2), and so is any write
  that sets a verdict without the director's attestation, or changes a
  verified or rejected task other than by reopening it.

### What `evi task start` does

`evi task start <id>`:

1. Marks its own process as an agent host (`EVI_AGENT=1`), as every
   agent-hosting entry point does, so everything the run starts carries the
   mark ([The director's attestation
   key](../security.md#the-directors-attestation-key)).
2. Refuses an untrusted repository. Run `evi trust` there from your own
   terminal first: the implementer policy runs no commands and no checks in
   an untrusted folder, so nothing could prove the task.
3. Reads the task (its required checks must parse and its scope must stay in
   the repository) and claims it with a lease of `--ttl` minutes. Someone
   else's live lease refuses the start unless you pass `--force`.
4. Opens the task's worktree: branch `task/<id>` in
   `<repo>/.worktrees/task__<id>`, created the first time from `--base`
   (default `[worktree] base_ref`, else `HEAD`), reused on later attempts,
   and locked while the run goes on. A branch checked out anywhere else, your
   main checkout above all, is refused. A lock left by a start on this
   machine whose process is gone is taken over; any other lock refuses the
   start. The worktree, branch and base go into the task.
5. Builds the agent in the worktree, on `--backend` and `--model` or your
   configured backend. The worktree's `EVI.md` and `.evi.toml` apply (the
   repository is trusted). The agent is held to the implementer policy, with
   the task's `files_in_scope` and its required checks allowed as exact
   commands, and nothing asks: every call is allowed or denied. The [verify
   gate](../configuration.md#verify-before-done--checks-in-configtoml) is on,
   even with `[checks] verify = "off"`, and the required checks join
   `[checks] commands` for the run, so the model sees a failing check before
   it can finish. A delegate CLI-agent backend (`codex`, `gemini`, `amp`,
   `qwen`, `copilot`) is refused as the `[llm] backend`, as a
   `[models] *_backend` or as `--backend`, whatever
   `[llm] allow_cli_agent_unattended` says: it runs its own tools, outside
   the policy.
6. Runs the prompt it builds from the task: the title, why, acceptance
   criteria, files in scope, required checks and the implementer contract in
   `EVI.md`. The implementer is told never to commit, push or change the
   task's status. The run takes at most `--max-turns` model round trips
   (default 30).
7. If the run ended `done`, runs each required check itself in the worktree
   (with `[checks] timeout`). That run is the ground truth: the model saying
   the tests pass doesn't count.
8. Writes the run report to
   `<eVi home>/tasks/<repo key>/<id>/<attempt>/report.json`, where the repo
   key is a hash of the main checkout's path. It is `evi run`'s [run
   report](structured-and-batch.md#the-run-report) plus a `task` section:
   the id, attempt, repository, branch, base, outcome, stop reason, exit code
   and the required checks' results.
9. Records the attempt, backend, model and `last_result`, and leaves the task
   `implemented` or `open`. When eVi's own run of a required check fails, the
   task's stop reason is `checks_failed` (exit 5) even if the run itself
   ended `done`. The worktree is unlocked.

The hand-off, the model's answer, goes to stdout. The verify gate's check
lines, the worktree summary, each required check, the outcome, any note and
the report's path go to stderr. Nothing is merged or committed, and the
checkout you work in is never touched.

A refusal before the run (exit 2) means nothing ran and the task is exactly
as it was before. A failure while building the agent counts as the run's
error: exit 1, the attempt counted, the task back to `open`. If the outcome
can't be written to the task file, the exit code is at least 1 and a note
says so; when the task is still claimed by you, `evi task release <id>` puts
it back to `open`.

The prompt doesn't include the reviewer notes. When you send a task back,
put what has to change in its `why` or `acceptance`. An open task's file is
plain TOML with no signature, so you can edit it.

### Verify and reject (director only)

eVi never marks its own work verified. A verdict is an **attestation**: an
HMAC-SHA256 under the director's key over the task record (every field but
the attestation), the branch head, the evidence bundle's hash, the verdict,
the review rounds and the review gate's result. Only director tooling run
from the director's own shell makes one, and the queue refuses to write a
verdict without one.

**Refused where an agent may have started the process.** `evi task verify`
and `evi task reject` exit 2, writing nothing and running no review, when
`EVI_AGENT` is set, inside an `evi chat` session (`EVI_INTERACTIVE=1`), when
an eVi process runs above them (`evi chat`, `evi serve`, the desktop app's
server), or when the process's own command line isn't
`evi task verify|reject` or `evi attest` (code calling eVi's signing
functions some other way). In an eVi session, a tool call that runs them is
denied in every mode as well ([The director's
ground](permissions-and-sandbox.md#the-directors-ground)).

**What `evi task verify` checks.** Before anything is signed:

1. The task is `needs_review` (`evi task submit` it first).
2. It has a branch and a base, so it has been started.
3. A worktree has `task/<id>` checked out (git's worktree list decides, not
   the path in the task file), it is at the branch's head, and it has no
   uncommitted or untracked files. The implementer never commits (the policy
   allows only read-only git), so **review the agent's work and commit it on
   `task/<id>` in its worktree first**. Otherwise the attestation would sign
   a head without the work, and the review would see an empty diff.
4. The evidence path, if the task names one, lies in the repository or in
   the task's folder under eVi's home, through no link.
5. The review gate runs: `evi review --multi --exit-code` on the diff from
   `base` to the branch head, in the worktree. It needs a working backend. No
   changes is a clean result. Blocking findings print the review on stderr
   and exit 1, unless `--reason` says why to sign anyway; that reason is
   recorded in the attestation and in the reviewer notes.
6. You confirm on the terminal: the prompt names the head, the evidence, the
   rounds, the review result and any override. `--yes` skips it from your own
   shell. With no terminal and no `--yes`, verify refuses (exit 1).
7. The director key signs (no key: exit 1, `evi attest init` makes one), and
   the signed task is written back only if the file hasn't changed since it
   was read (otherwise exit 2: run the command again).

**`evi task reject`** needs a `needs_review` task that has a branch, and
`--reason`, which goes into the reviewer notes before signing, so it is
signed too. There's no worktree or review check. `evi task reopen` then sends
the task back for rework.

`--rounds N` (default 1) records how many review rounds the task took.

### How a task's status reads

`evi task list` and `evi task show` show the status as the attestation has
it. A verdict counts only with a valid attestation from this machine's
director key, at the branch's current head, over the record and the evidence
as they are now.

| Shown | Why |
|---|---|
| `open`, `claimed`, `implemented`, `needs_review` | The status as written. |
| `verified`, `rejected` | A valid attestation. |
| `unverified (unsigned)` | The file claims a verdict (in any case, with look-alike letters or invisible characters too) and carries no attestation: it was edited by hand or written by something other than verify or reject. |
| `unverified (invalid signature: the task record changed after it was signed)` | The task file was edited after it was signed. |
| `unverified (invalid signature: the evidence changed after it was signed)` | The evidence bundle changed, or its path is one eVi won't read. |
| `unverified (invalid signature: …)`, other reasons | A forged or malformed signature, or one made with another director key. |
| `unverified (stale: signed at <commit>, the branch is now at <commit>)` | The branch moved on after it was signed, or an older verified file was restored from history. |
| `unverified (no director key here to check the signature)` | This machine has no director key, or the command runs where an agent may have started it, where the key isn't read. |
| `unverified (unrecognised status …)` | A status that isn't plain ASCII. |
| `<status> (an earlier attestation no longer applies)` | A status other than a verdict that still carries an attestation (eVi drops it when it reopens a task). |

When the branch no longer exists (a merge deleted it, say) or can't be read,
its head can't be compared, and only the record and the evidence are
checked. In `--format json`, `status` is the word in the file and `label` is
what counts.

### The director key (`evi attest`)

`evi attest init` creates the director key: 32 random bytes in the OS keyring
(service `evi-director`) when the optional `keyring` package works, else in
`<eVi home>/director/director.key`, readable only by you. It asks you to
confirm on the terminal, or takes `--yes` from your own shell. An existing
key is an error (exit 1) unless you pass `--rotate`, which replaces it and
makes every attestation signed so far invalid.

`evi attest status` prints the key's id and where it is kept, never the key
itself.

The key is an HMAC key, so an attestation can be checked only where the key
is. On another machine, or in CI, a verified task reads
`unverified (no director key here …)`. See [The director's attestation
key](../security.md#the-directors-attestation-key) for where the key lives,
what agents can't do with it, and the limits.

## Usage

### The director loop

From your own terminal, in the repository:

```bash
evi attest init        # once per machine
evi trust              # once per repository
evi task new --title "Fix the parser's off-by-one" --acceptance "a trailing token is kept" --scope evi/parser.py --scope tests/test_parser.py --check "python -m pytest -q tests/test_parser.py"
evi task start T1 --backend local
git -C .worktrees/task__T1 status
git -C .worktrees/task__T1 add -A
git -C .worktrees/task__T1 commit -m "fix(parser): keep a trailing token"
evi task submit T1
evi task verify T1
```

`--backend local` names a saved backend (`evi backend list`); leave it out to
use your configured one. Review the work in `.worktrees/task__T1` (and the
hand-off and the report) before you commit it. If it isn't right,
`evi task reopen T1 --reason "…"`, adjust the task's `why` or `acceptance`,
and `evi task start T1` again: the next attempt carries on in the same
branch and worktree. To turn it down for
good, `evi task reject T1 --reason "…"`. Once verified, merge `task/T1` as
usual; `evi worktree prune` then removes the worktree.

### Commands

Every `evi task` command finds the queue from the current folder. A refused
request (a bad id or flag, a step the status doesn't allow, someone else's
lease, a write that can't be made) prints `error: …` on stderr and exits 2.

**`evi task new`** files a task, `open`, in `.evi/tasks/<id>.toml`.
`--title TEXT` (required, unless `--from` gives one), `--why TEXT`,
`--acceptance TEXT` (repeatable), `--scope GLOB` (repeatable,
`files_in_scope`), `--check COMMAND` (repeatable, `required_checks`),
`--implementer evi-local|evi-frontier|director`, `--id ID` (default: the
next `T<n>`) and `--from FILE`, a TOML file with the same fields, which the
flags override (run bookkeeping and any attestation in it are dropped).

**`evi task list`** lists the tasks with their status as it reads. `--format
text|json` (`-f`); JSON gives `id`, `title`, `status`, `label`,
`implementer`, `branch` and `attempts` per task. A task file that can't be
read is a warning on stderr.

**`evi task show ID`** shows one task: its fields, the status as it reads,
the lease, the last result and report, the reviewer notes, the attestation
and the file. `--format json` gives `{task, label, path}`.

**`evi task claim ID`** claims an open task, or one whose lease ran out.
`--holder NAME` (default `<user>@<host>`), `--ttl MINUTES` (default 240),
`--force` to take over someone else's live lease.

**`evi task release ID`** gives a claimed task back: it is `open` again.
`--holder NAME`, `--force`.

**`evi task submit ID`** hands an implemented task to review
(`needs_review`).

**`evi task reopen ID`** sends an implemented, needs_review, verified or
rejected task back to `open`, dropping any attestation. `--reason TEXT` is
kept in the reviewer notes.

**`evi task start ID`** runs the task ([What `evi task start`
does](#what-evi-task-start-does)). `--backend NAME` (a saved backend,
`evi backend list`), `--model ID`, `--base REF` (where a new task branch
starts), `--max-turns N` (default 30), `--ttl MINUTES` (default 240),
`--force` to take over someone else's live lease.

**`evi task verify ID`** (director only) signs a needs_review task as
verified ([Verify and reject](#verify-and-reject-director-only)).
`--reason TEXT` (sign although the review gate reports blocking findings),
`--rounds N` (default 1), `--yes`.

**`evi task reject ID`** (director only) signs a needs_review task as
rejected. `--reason TEXT` (required), `--rounds N`, `--yes`.

**`evi attest init`** (director only) creates the director key. `--rotate`,
`--yes`.

**`evi attest status`** shows whether this machine has a director key.

### Exit codes

| Command | 0 | 1 | 2 | Other |
|---|---|---|---|---|
| `new`, `list`, `show`, `claim`, `release`, `submit`, `reopen` | done | | refused | |
| `start` | implemented | the run's error, or its outcome not recorded | refused before anything ran | as `evi run`: 3 `max_turns`, 4 `guardrail`, 5 `checks_failed`, 6 `stop_hook_exhausted`, 130 `interrupted` |
| `verify` | signed | no key, not confirmed, the review gate couldn't run or reported blocking findings | refused (an agent's process, the status, the worktree, the evidence path, a file that changed meanwhile) | |
| `reject` | signed | no key, not confirmed | refused (an agent's process, the status, no branch, no reason) | |
| `attest init` | key created | a key exists (no `--rotate`), not confirmed | refused in an agent's process | |
| `attest status` | key found | no key, or refused in an agent's process | | |

A start that ends anywhere but `implemented` leaves the task `open`; its
exit code says why, as [`evi
run`'s](../troubleshooting.md#evi-run-exited-non-zero-what-the-exit-code-means)
does. See [Troubleshooting](../troubleshooting.md#evi-task-start-exits-2) for
the refusals.

## Notes / limits

- **Only verify, reject and `evi attest` are director tooling.** The other
  `evi task` commands change unsigned state. None of them can make a verdict,
  and a reopen drops one rather than forging one.
- **A verdict checks only where the key is.** See [The director
  key](#the-director-key-evi-attest).
- **The review gate reads the branch's own guidance.** It loads
  `.evi/REVIEW.md` and `.evi/review-rules.md` from the task's worktree, so
  when the diff changes those files, read that part yourself.
- **Deleting the highest-numbered task file frees its number.** The next
  `evi task new` gets the same `T<n>`, and its start carries on in the old
  `task/T<n>` branch and worktree. Remove that worktree and branch first.
- **The same OS user.** The agent runs as you, so these checks keep it from
  verifying its own work by accident or unnoticed, not from a determined
  attempt. See the limits in [The director's attestation
  key](../security.md#the-directors-attestation-key).
- **A guardrail, not a sandbox.** The required checks run the repository's
  code in the worktree; see [Implementer
  policy](permissions-and-sandbox.md#implementer-policy).

## Relevant source files

- `evi/tasks.py`: the queue, the states and leases, `run_task`.
- `evi/attest.py`: the director key, signing, `status_label`, who may sign.
- `evi/apps/cli/main.py`: `evi task …` and `evi attest …`.
- `evi/worktree.py`: `open_isolated`, the worktree lock and a dead run's
  lock.
- `evi/policy.py`: the implementer policy.
- `evi/proc.py`: `EVI_AGENT` (`mark_agent_process`, `agent_env`).
