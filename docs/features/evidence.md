# Evidence bundles and provenance (`evi evidence`)

## Overview

A run that only says "the tests pass" proves nothing. eVi ends each task run
with an **evidence bundle**: a folder holding the work as a diff, eVi's own
run of the task's required checks (with pytest's junit report), a redacted
excerpt of the conversation and a `report.json` that describes it all. A
review reads that, not the model's word for it.

- **Every task run gets one.** `evi task start`, and a run the MCP server's
  [agent category](mcp.md#the-agent-category-the-directors-task-api) starts,
  collect the attempt's bundle whatever the run's stop reason, and
  `evi task submit` collects it again from the work you committed. The task
  file records the bundle's path and a summary with its hash.
- **The director signs the bundle's hash.** `evi task verify` refuses a
  bundle that changed since it was collected, and the attestation signs the
  hash ([Verify and reject](tasks.md#verify-and-reject-director-only)).
- **Plain runs can have one too.** `evi run --evidence DIR` writes a bundle
  after the run, and `evi evidence collect --worktree PATH --base REF`
  collects one for work already done.
- **Commit messages and statistics come from it.** `evi evidence commit-body`
  prints the commit message for a task, with provenance trailers, and
  `evi stats --provenance` counts how much of a repository's history eVi
  implemented, counting only what the director signed.

Bundles live under eVi's home, not in the repository (unless you name a
folder for one), and `evi evidence prune` removes old ones, never one a task
names.

## How it works

### What a bundle holds

A bundle is a folder:

| File | What it holds |
|---|---|
| `report.json` | What the bundle holds, and the environment it was collected in ([below](#reportjson-and-its-schema)). |
| `diff.patch` | The work against its base: every commit since the base plus what is uncommitted in the work tree, and each untracked file git doesn't ignore as a new file. At most 8,000,000 characters; the rest is cut. An untracked file over 1 MB is named but not shown, a binary one is marked as binary, and a link is not followed. |
| `diffstat.txt` | One line per changed file (`+added -deleted`), then the totals. |
| `checks/NN-<name>.txt` | Each check's output, its last 256,000 characters. |
| `checks/NN-<name>.junit.xml` | For a pytest check, the junit report eVi's own run asked pytest for ([below](#the-checks-and-their-junit-reports)). |
| `transcript.md` | The end of the run's conversation ([below](#the-transcript-excerpt-and-redaction)), when there is a run report that names its transcript. |

git runs hardened, as everywhere in eVi (no program the repository
configures runs, no external diff or text conversion), and a work tree whose
git configuration points git at files somewhere else is not diffed: the
bundle says why instead.

A bundle is built in a hidden folder next to its target and then renamed
into place, so a reader sees a whole bundle or none. An older bundle at the
target is replaced whole; a folder that holds anything else, a file or a link
is refused, and nothing is written.

### report.json and its schema

`report.json` validates against `evi.evidence.SCHEMA` (JSON Schema 2020-12,
`$id` `urn:evi:evidence:v1`). When the optional `jsonschema` package is
installed, eVi checks a report against it before it writes the bundle, and
refuses to write one that doesn't match.

| Key | What it holds |
|---|---|
| `evidence_version`, `kind` | `1` and `"evi-evidence"`. |
| `created` | When the bundle was collected (UTC, `YYYY-MM-DDTHH:MM:SSZ`). |
| `environment` | eVi and Python versions, the Python implementation, the platform, backend and model, the repository, the work tree, its branch, the base and head commits, the run's policy and whether the folder is trusted. |
| `task` | `{id, title, attempt, branch, status}` for a task's bundle, `null` for a plain run. |
| `run` | The v1 [run report](structured-and-batch.md#the-run-report) (a task's has its `task` section), redacted; `null` when there is none. |
| `run_note` | Why a run report that was given isn't in `run` (not a JSON object, or it doesn't match the v1 schema). |
| `diff` | `base`, `head`, `files`, `insertions`, `deletions`, `untracked`, `patch` (the file name), `patch_chars`, `truncated`, `note` (what git couldn't tell), `paths` (`{path, added, deleted}`, at most 1,000; `null` counts for a binary or unshown file) and `paths_listed`. |
| `checks` | One entry per check: `name`, `cmd`, `exit` (`null` when it timed out, was cancelled or couldn't start), `ok`, `duration_ms`, `note`, `source` (`required`: eVi's own run of a required check; `run`: a check the run ran itself, its output's end only), `output` (its file), `junit_file`, `junit` (that report's totals) and `reported` (the counts pytest printed). |
| `checks_skipped` | Why the checks didn't run, or stopped; `null` otherwise. |
| `junit` | The junit totals summed over the checks, or `null`. |
| `transcript` | `{file, source, session_id, messages, shown, truncated}`, or `null`. |
| `files` | Every file in the bundle. |
| `redactions` | What was redacted, by kind, with counts. |
| `timestamps` | `{started, finished}`. |

To check a bundle yourself (`pip install jsonschema`):

```python
import json
import jsonschema
from evi import evidence

with open("report.json", encoding="utf-8") as f:
    jsonschema.Draft202012Validator(evidence.SCHEMA).validate(json.load(f))
```

### The checks and their junit reports

For a task, eVi runs each of the task's required checks again in the
worktree, after the run, so the bundle has their full output. A check counts
as pytest when it runs `pytest`, `python -m pytest` or a runner's
`… run pytest` (`uv run pytest`); eVi then adds `--junitxml=` pointing into
the bundle (last, or just before a `--` that follows the pytest word, after
which pytest reads every word as a path). The check's `junit` totals
(`tests`, `failures`, `errors`, `skipped`, `passed`) come from that report,
and `reported` holds the counts pytest printed in its closing summary line,
so the two can be compared.

The checks run:

- **only in a trusted folder** (`evi trust`): each runs the folder's own
  code. Elsewhere none runs, and `checks_skipped` says so;
- **through eVi's process runner**, with no `*_KEY`, `*_TOKEN` or `*_SECRET`
  variables in their environment, no Python bytecode written, `[checks]
  timeout` per check, and the whole process tree killed when it runs out;
- **not for a run that stopped before its checks**: when the run's stop
  reason is anything but `done` or `checks_failed` (an error, an interrupt),
  no check runs and `checks_skipped` reads `the run stopped (<reason>)
  before its checks, so eVi ran none`;
- **until they are stopped**: in `evi task start`, a Ctrl+C during the
  bundle's checks stops the running one and starts no other, and a second
  Ctrl+C stops the collection (the run is still recorded, with a note). Over
  MCP, `run_cancel`, `[tasks] max_run_minutes` and closing the server do the
  same. `checks_skipped` then says after how many of them eVi stopped.

`evi run --evidence` reruns nothing: it records the checks the run's verify
gate ran (`source` `run`, the end of each output, no junit).

### The transcript excerpt and redaction

`transcript.md` holds the last 60 messages of the run's session transcript:
user and assistant messages, tool results and tool calls with their
arguments. Each message is cut to 4,000 characters (a tool call's arguments
to 1,000), and the whole excerpt to its last 64,000. The transcript is read
only when the run report names a `.jsonl` file under eVi's home that is not
a link, and only its last 4 MB.

Every text that goes into a bundle (the patch, the diffstat, each check's
output, the excerpt, the run report) passes through one
[redactor](secret-redaction.md) before it is cut short, so neither a secret
nor a prefix of one is written, and `redactions` tallies what was replaced. A
junit report is redacted as the XML it is: each element's text and each
attribute value as it reads unescaped, so a value pytest wrote XML-escaped
is still found. A junit report too big to read (over 32 MB), unreadable or
not valid XML is removed rather than kept unredacted.

Redaction finds what eVi can recognise: tokens with a distinctive shape, and
the secrets this machine has (environment variables ending `_KEY`, `_TOKEN`,
`_SECRET` or `_PASSWORD`, the secret fields of eVi's config files, the
director key). It can't find:

- a secret with no recognisable shape that this machine doesn't hold, such
  as a password pasted into the chat;
- a known value shorter than 12 characters;
- a known value a program printed changed: cut short (pytest shortens long
  values in its assertion messages, so a secret compared in an assert can
  leave its start and end in a check's output), encoded or split.

Read a bundle before you share it.

### Where bundles live

| Bundle | Folder |
|---|---|
| A task's attempt | `<eVi home>/evidence/<repo key>/<task id>/<attempt>/` |
| A plain run (`evi evidence collect --worktree` without `--out`) | `<eVi home>/evidence/<repo key>/.runs/<UTC time>-<hex>/` |
| A folder you name (`evi run --evidence DIR`, `evi evidence collect --out DIR`) | That folder |

The repo key is the one task run reports use: a hash of the main checkout's
path. A folder you name must be outside `<eVi home>/evidence/`, new, empty
or an older bundle, and not the bundle a task of the repository names as its
evidence (a task may collect its own again). eVi records it in
`<eVi home>/evidence/registry.json` (the newest 500), which is what lets a
task name it.

### Which folders a task may name

A task file is tracked, so its `evidence` path may come from anyone who can
push to the repository. eVi reads (and hashes) it only when it is a bundle,
a folder holding a regular `report.json`, and either

- lies under the repository's own folder of `<eVi home>/evidence/`, reached
  through no link or junction, or
- is exactly a folder `registry.json` lists, with no link or junction on the
  way to it.

Anything else is never hashed: `.`, the repository, its `.git`, a UNC or
device path, another repository's bundle, any other folder. `evi task list`
and `evi task show` read it as evidence that changed (a verdict reads
`unverified (invalid signature: the evidence changed after it was signed)`),
`evi task verify` and `evi task reject` refuse with exit 2, and
`evi evidence commit-body` leaves the `Evidence:` trailer out and says why.

### What a task records

A task's bundle goes into two fields of its file:

- `evidence`: the bundle's path;
- `evidence_summary`: `hash` (`sha256:…`, what an attestation signs),
  `files`, `insertions`, `deletions`, `checks_passed`, `checks_failed`,
  `junit` (`{tests, failures, errors, skipped}`, when a pytest check ran) and
  `checks_skipped` (when none ran).

`evi task show` prints both. When a start's bundle can't be collected, the
run is still recorded, a note says why, and the fields are emptied: an
earlier attempt's bundle is not this attempt's evidence.

### Commit messages

`evi evidence commit-body T1` prints the commit message for a task:

1. the subject: the task's title;
2. its `why`;
3. `Acceptance criteria:` and the list;
4. `Checks (eVi's own run, in the evidence bundle):`, one line per check
   (`passed`, `failed (exit N)` or `did not finish`, with its junit totals),
   or `Checks: none ran (…)`;
5. `Files changed: N (+added -deleted)` and up to 30 paths;
6. the trailers: `Implemented-by: eVi (<backend>/<model>)` (left out for a
   task never started), `Task: <id>` and `Evidence: <hash>` (when the task
   names a bundle eVi reads), then `Verified-by`, `Review-rounds` and
   `Attestation`, only from a valid `verified` attestation at the task
   branch's current head.

The director trailers come from the attestation alone: a task that isn't
signed, was edited after signing, whose evidence changed, or whose branch
moved on gets none. They check only where the director key is, so run
commit-body from your own shell (in a process an eVi agent may have started,
the key isn't read and there are none). A line of the task's own text that
reads like one of these six trailers is quoted (`> `), so it can't pass for
one. What the message lacks, and why, goes to stderr as `note:` lines.
`--format json` prints `{message, verified, trailers}`.

Use the message for the commit that lands the work on your main branch: a
merge commit (`git merge --no-ff -F msg.txt task/T1`) or a squash commit.
Don't add a commit to `task/T1` after verifying: the attestation names the
branch's head, and a new head drops the director trailers.

### Provenance counts

`evi stats --provenance` reads the git history of the repository you run it
in (`--since DATE` narrows it, as `git log --since` does) and reports:

| Field | What it counts |
|---|---|
| `commits` | Every commit reachable from `HEAD`. |
| `implemented_by_evi` | Commits whose trailers say `Implemented-by: eVi …`. Trailers are the `Key: value` paragraphs at the end of the message, so an attribution paragraph after eVi's trailers (a `Co-Authored-By:` line) doesn't hide them. |
| `attested` | Commits whose `Verified-by`, `Review-rounds` and `Attestation` trailers check under this machine's director key. Each attestation (its task, head and key) counts once, at the oldest commit git lists with it. |
| `attested_evi` | Attested commits that also say `Implemented-by: eVi`. |
| `evi_share` | `attested_evi` over `commits`. |
| `review_rounds` | `total` and `mean` of the signed review rounds, over attested commits. |
| `attested_tasks` | Distinct tasks among the attested commits. |
| `attested_head_in_history` | Attested commits whose signed head is the commit or one of its ancestors. A squash merge's isn't, and still counts as attested. |
| `not_counted` | Listed apart, never in the share: `unattested_evi_claims` (`Implemented-by: eVi` without a valid attestation), `self_reported` (`Verified-by` or `Review-rounds` lines with no signed `Attestation`), `invalid` (an attestation that doesn't check: edited, forged, another key), `replayed` (a later commit carrying an attestation an older one already carries) and `unverifiable` (no key here to check with). |
| `key_note` | Why attestations can't be checked here, when they can't. |

The attestation is an HMAC under the director key, so it checks only where
that key is. In CI, on another machine, or in a process an eVi agent may
have started, nothing is attested: every signed commit is `unverifiable`
and `key_note` says why. Rotating the key (`evi attest init --rotate`) voids
every trailer signed before.

The `Implemented-by` line is not part of what the director signs: the
attestation covers the task, the head, the review rounds, the signer and the
key, not the commit message it sits in. An attested commit counts as eVi's
when its trailers say so, so the eVi share is only as trustworthy as the
people who write commits in the repository.

## Setup

Nothing to install: bundles need git, and checks run only in a trusted
folder. `jsonschema` (optional) lets eVi check each `report.json` before it
writes it. One setting, in `~/.evi/config.toml`:

```toml
[evidence]
keep_days = 30   # evi evidence prune removes bundles collected more than this many days ago
```

See [Evidence bundles](../configuration.md#evidence-bundles--evidence-in-configtoml)
in the configuration reference.

## Usage

### Commands

**`evi run --evidence DIR`** writes a bundle to `DIR` once the run is over,
whatever its stop reason: the run's folder (its worktree with `--worktree`)
against where it started (the worktree's base, else the commit checked out
when the run began), the checks the run ran, the transcript excerpt and the
run report. Its path and hash go to stderr (`evidence: <path> (sha256:…)`).
`DIR` (relative to where you run `evi run`) is checked before anything runs:
one that holds something other than a bundle, a file, a link, a folder under
`<eVi home>/evidence/` or a bundle a task names is a usage error (exit 2). A
bundle that can't be written is a note on stderr, and the run's exit code
stands. Without `--worktree`, changes that were uncommitted before the run
show in the diff too.

**`evi evidence collect ID`** collects a task's bundle now: the work in the
worktree git has `task/<id>` checked out in (never the path the task file
names) against the task's base, eVi's own run of its required checks, the
latest start's run report and its transcript excerpt. It records `evidence`
and `evidence_summary` in the task. It refuses a claimed task (a start may be
running), a verified or rejected one (its attestation signed the bundle it
has: `evi task reopen` it first), one never started or with no worktree on
its branch, and one whose status or attempts changed while it collected. `--out DIR` writes to a folder of your
own instead.

**`evi evidence collect --worktree PATH --base REF`** collects a plain run's
bundle (under the repository's `.runs/` folder, or `--out DIR`). `--report
FILE` adds a run report as `evi run --format json` printed it (and its
transcript excerpt), and `--check COMMAND` (repeatable, split the POSIX way)
a check to run there, in a trusted folder only. A task id and `--worktree`
don't go together, and `--base`, `--report` and `--check` are for
`--worktree` only.

Both forms print `evidence: <path> (<summary>)`, or with `--format json`
`{path, hash, summary}`.

**`evi evidence commit-body ID`** prints the task's [commit
message](#commit-messages). `--format text|json`.

**`evi evidence prune`** removes the current repository's old bundles: those
under `<eVi home>/evidence/<repo key>/` collected more than `[evidence]
keep_days` days ago (by `report.json`'s `created`, else the file's time),
superseded attempts and plain runs included, and half-written folders an
interrupted write left more than a day ago. It never removes:

- a bundle a task file of the repository names as its evidence (a verified
  task's attestation re-checks its hash);
- any bundle of a task whose file can't be read;
- a folder you named with `--evidence` or `--out`;
- another repository's bundles.

`--keep-days N` overrides the setting for this run (`0`: only the bundles a
task names, or whose task file can't be read, stay), and `--dry-run` lists
what would go. It prints each path, then
`N bundle(s) removed; M kept (K named by a task), keep_days D`. Nothing
prunes on its own.

**`evi stats --provenance`** prints the [provenance
counts](#provenance-counts). `--since DATE` (git's dates: `2026-10-01`,
`'2 weeks ago'`), `--format text|json` (`--json` too). Outside a git
repository it exits 2, and so does a `--since` that starts with `-`, or
`--since` without `--provenance`.

### Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `evi evidence collect` | collected and recorded | the bundle couldn't be written | refused (the task's status, no worktree, a bad `--out`, a task that changed meanwhile) |
| `evi evidence commit-body` | printed | | no such task, outside a repository |
| `evi evidence prune` | done | | outside a repository |
| `evi stats --provenance` | printed | | outside a repository, a `--since` eVi refuses |

## Examples

### The commit message for a verified task

```bash
evi task submit T1
evi task verify T1
evi evidence commit-body T1 > msg.txt
git merge --no-ff -F msg.txt task/T1
```

`msg.txt` ends with the trailers:

```text
Implemented-by: eVi (local/qwen2.5-coder-14b)
Task: T1
Evidence: sha256:9f2c…
Verified-by: director (director key 3fa1c2d4e5b6a7f8)
Review-rounds: 1
Attestation: v1 task=T1 head=4be1… key=3fa1c2d4e5b6a7f8 sig=…
```

### Evidence for a plain run

```bash
evi run --policy implementer --worktree auto -m code --evidence ../evidence/fix-1 "fix the off-by-one in evi/parser.py"
```

stderr ends with `evidence: /home/you/evidence/fix-1 (sha256:…)`.

### How much of the history did eVi implement?

```bash
evi stats --provenance --since "4 weeks ago"
```

```text
42 commits in /home/you/project since 4 weeks ago
  attested (director key checks): 9, for 9 task(s); review rounds 12 (mean 1.33)
  eVi share: 7 attested eVi commit(s) of 42 (16.7%); 8 say Implemented-by: eVi
  not counted: 1 eVi claim(s) without a valid attestation, 0 self-reported Verified-by, 0 invalid attestation(s), 0 replayed (an earlier commit's attestation, copied), 0 unverifiable here
```

## Notes / limits

- **Required checks run more than once.** `evi task start` runs them to
  decide the outcome, then again for the bundle (to get the whole output and
  the junit report), and `evi task submit` runs them a third time. A flaky
  check can pass in one run and fail in another: the task's outcome comes
  from the first, the bundle records its own run.
- **A bundle is evidence, not a sandbox.** Collecting runs the repository's
  checks, its own code, in a trusted folder.
- **The registry keeps the newest 500 folders you named.** A task that names
  an older one reads it as changed evidence. Bundles under
  `<eVi home>/evidence/` don't need the registry.
- **Provenance checks only where the key is**, and the `Implemented-by` line
  is unsigned ([Provenance counts](#provenance-counts)). See
  [The director's attestation key](../security.md#the-directors-attestation-key).

## Relevant source files

- `evi/evidence.py`: collecting, the schema, where bundles go, which paths
  a task may name, pruning, `commit_body`.
- `evi/stats.py`: `provenance`.
- `evi/attest.py`: `evidence_hash`, `trailers`, `verify_trailers`.
- `evi/tasks.py`: `evidence_path`, the evidence hook of `run_task`.
- `evi/apps/cli/main.py`: `evi evidence …`, `evi run --evidence`,
  `evi stats --provenance`.
