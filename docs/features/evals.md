# Evals & LLM-as-judge

## Overview

**Evals** let you regression-test eVi the way you'd unit-test code. An *eval suite* is a small TOML file of *cases*; each case is a prompt plus one or more assertions about the answer eVi gives. Running the suite sends every prompt through the agent and reports a **pass-rate**.

Use evals to:

- Catch a regression after you change a system prompt, a skill, a tool, or your model.
- Compare two models on the same set of prompts (just switch your `[llm]` model and re-run).
- Watch for silent quality drift over time by scheduling a suite to run on a cron.
- Gate CI: `evi eval run` exits non-zero if any case fails.

Most assertions are **deterministic** (substring / regex / exact-match checks). For things that aren't easily pattern-matched — tone, helpfulness, "did it actually answer the question" — a case can carry a natural-language **`judge` rubric**, which is graded by a model acting as **LLM-as-judge**.

## How it works

The mechanism is deliberately simple and lives in `evi/evals.py`:

1. **Load** — a suite file at `~/.evi/evals/<name>.toml` is parsed into an `EvalSuite` of `EvalCase`s. A suite needs at least one `[[case]]`, and every case needs a non-empty `prompt`.
2. **Run each case** — the prompt is sent through a fresh headless agent run (no shared chat history between cases). Tools auto-execute (`enable_auto_all()`), so a case can exercise file/web/shell tools if you want.
3. **Check deterministic assertions** — the answer text is tested against the case's `contains` / `not_contains` / `regex` / `equals` rules. *All* specified assertions must hold for the case to pass.
4. **Grade the judge rubric (optional)** — if a case has a `judge` rubric, the answer is *also* sent to a grader model. The grader is given the rubric and the answer and must reply `PASS` or `FAIL` on the first line plus a one-line reason. **Both** the deterministic checks **and** the judge must pass for the case to count as passed.
5. **Report** — you get a per-case PASS/FAIL list and an overall `passed/total` pass-rate.

### The LLM-as-judge grader, precisely

The grader is an ordinary eVi agent run **with no tools** — it answers from the answer text alone. The grading prompt is essentially:

```
Grade the ANSWER against the RUBRIC. Reply with exactly PASS or FAIL
on the first line, then a one-line reason.

RUBRIC: <your judge text>

ANSWER:
<eVi's answer to the case prompt>
```

The first line of the grader's reply is uppercased and checked with `startswith("PASS")`. Anything that doesn't start with `PASS` (including an empty reply or an error) is treated as a **FAIL**, and the one-line reason is surfaced in the case's failures. The grader uses the **same model** as the rest of eVi (your `[llm]` config) — there is no separate "judge model" key.

> Important: if a case sets a `judge` rubric but the suite is run by something that doesn't supply a grader, that case fails with `judge rubric set but no grader available`. Both the CLI (`evi eval run`) and the scheduler **do** supply a grader, so in normal use this never happens — it's a safety default in the engine.

## Setup

There is **no special config section and no pip extra required** for plain evals — the engine is pure-Python TOML parsing and ships in the core install. Two things matter:

| What | Where | Notes |
|------|-------|-------|
| Eval suites | `~/.evi/evals/<name>.toml` | One TOML file per suite. Created by `evi eval new`, or hand-written. |
| LLM config (used for runs **and** the judge) | `[llm]` in `~/.evi/config.toml` | The grader reuses this — switch the model here to compare models or change the judge. |
| Scheduled-eval logs | `~/.evi/logs/scheduled/<task-id>_<timestamp>.log` | Only when you run a suite on a schedule (see below). |

> The home directory defaults to `~/.evi` but follows the `EVI_HOME` environment variable if set, so suites then live in `$EVI_HOME/evals/`.

**Scheduling extra** — running a suite *on a cron* (drift watch) goes through the scheduler, which needs APScheduler:

```bash
pip install 'evi-assistant[scheduler]'
```

### Suite file format

```toml
name = "smoke"                 # optional; defaults to the file stem
description = "Sanity checks"  # optional, shown in `evi eval list`

[[case]]
name = "math"                          # optional; defaults to "case1", "case2", …
prompt = "What is 2+2? Reply with just the number."
contains = ["4"]                       # all of these substrings must be present
not_contains = ["error"]               # none of these may be present

[[case]]
name = "json"
prompt = "Return a JSON object with ok=true."
regex = '"ok"\s*:\s*true'              # re.search must match
```

**Per-case keys** (all optional except `prompt`):

| Key | Type | Meaning |
|-----|------|---------|
| `prompt` | string (**required**) | The message sent to eVi. |
| `name` | string | Label for the case; auto-named `caseN` if omitted. |
| `contains` | string or list | Every substring must appear in the answer. |
| `not_contains` | string or list | None of these substrings may appear. |
| `regex` | string | A regex that must `search`-match the answer. |
| `equals` | string | The answer (after `.strip()`) must equal this exactly. |
| `ignore_case` | bool | Makes `contains` / `not_contains` / `regex` / `equals` case-insensitive. Default `false`. |
| `mode` | string | Tool preset for *this* case: `chat`, `cowork`, or `code`. Overrides the run's default `--mode`. |
| `judge` | string | LLM-as-judge rubric in plain English. Graded `PASS`/`FAIL` by a model. |

## Usage

All evals live under the `evi eval` command group.

| Command | What it does |
|---------|--------------|
| `evi eval list` | List suites in `~/.evi/evals/` with their case counts and descriptions. |
| `evi eval new <name>` | Write a starter suite (with `contains`, `not_contains`, and a `judge` example). Add `--overwrite` to replace an existing one. |
| `evi eval run <name>` | Run a suite and print per-case PASS/FAIL plus the pass-rate. |

`evi eval run` options:

- `--mode, -m <preset>` — default tool preset (`chat` / `cowork` / `code`) for cases that don't set their own `mode`.
- `--json` — print the full report as JSON (handy for CI or further processing).

**Exit codes**: `evi eval run` exits `0` only when every case passes; it exits `1` if any case fails, and `2` if the suite can't be loaded (missing/malformed). That makes it a drop-in CI gate.

### Web / Desktop — the Evals panel

The web and desktop apps have a **Settings → Evals** panel that lists every
suite in `~/.evi/evals/` with its cases and the assertions each one checks. Each
suite has a **Run** button: it runs the suite server-side (one model call per
case, plus one per judged case — `GET /api/evals` lists, `POST /api/evals/run`
runs), then marks each case ✓/✗ in place and shows the pass-rate. The run uses
the same `evals.make_runners` + `run_eval` machinery as `evi eval run`, so the
verdicts match the CLI exactly.

### Running a suite on a schedule (drift watch)

Use the scheduler to re-run a suite automatically. Note `--eval` takes the **suite name** (not a prompt):

```bash
evi schedule add --name "nightly-smoke" --cron "0 3 * * *" --eval smoke
```

When it fires, the scheduler runs the suite (with the judge grader wired in) and writes a log to `~/.evi/logs/scheduled/`. The task's `last_status` becomes `ok (P/T)` when all cases pass or `fail (P/T)` otherwise — so drift shows up directly in `evi schedule list`. The scheduler must be running (`evi scheduler`, or any `evi web` process, which starts it in-process).

> There is no dedicated REPL slash command for running suites — from the CLI evals are driven through `evi eval …` (and `evi schedule … --eval` for the scheduled variant); the web/desktop app drives them from **Settings → Evals**.

## Examples

### Example 1 — create, inspect, and run a suite

Scaffold a starter suite and run it:

```bash
evi eval new smoke
# created C:\Users\you\.evi\evals\smoke.toml   (or ~/.evi/evals/smoke.toml)

evi eval list
#   smoke (3 cases) — What this suite checks

evi eval run smoke
#   PASS math
#   PASS no-refusal
#   PASS tone
#
#   3/3 passed (100%) — smoke
```

The generated `~/.evi/evals/smoke.toml` looks like this — note the mix of a deterministic check, a case-insensitive "no refusal" check, and an LLM-judge rubric:

```toml
name = "smoke"
description = "What this suite checks"

[[case]]
name = "math"
prompt = "What is 2 + 2? Reply with just the number."
contains = ["4"]

[[case]]
name = "no-refusal"
prompt = "List three uses for a paperclip."
not_contains = ["I cannot", "I can't"]
ignore_case = true

[[case]]
name = "tone"
prompt = "Explain recursion to a five-year-old."
judge = "The explanation is simple, friendly, and uses an everyday analogy."
```

### Example 2 — LLM-as-judge plus a deterministic backstop, with per-case mode

A case can combine a judge rubric *and* deterministic assertions — both must pass. Here the `code` case uses the `code` tool preset so the agent can actually run/inspect code, and pairs a hard regex check with a softer judge rubric:

```toml
name = "quality"
description = "Tone and correctness checks"

[[case]]
name = "summary-quality"
prompt = "Summarize the benefits of unit testing in two sentences."
judge = "The summary is accurate, concise, and mentions catching regressions."
not_contains = ["As an AI"]
ignore_case = true

[[case]]
name = "fib"
mode = "code"
prompt = "Write and run a Python function for the 10th Fibonacci number; print only the number."
regex = '\b55\b'                          # deterministic backstop
judge = "The answer shows working code and the correct result, 55."
```

Run it and emit JSON for CI:

```bash
evi eval run quality --json
```

```json
{
  "name": "quality",
  "total": 2,
  "passed": 2,
  "pass_rate": 1.0,
  "cases": [
    { "name": "summary-quality", "passed": true, "failures": [], "output": "..." },
    { "name": "fib", "passed": true, "failures": [], "output": "..." }
  ]
}
```

A failing judge case reports the grader's reason inline, e.g.:

```
  FAIL summary-quality
       judge: FAIL — does not mention catching regressions
```

### Example 3 — schedule a nightly drift watch

```bash
# install the scheduler extra once
pip install 'evi-assistant[scheduler]'

# run the smoke suite every morning at 03:00
evi schedule add --name "nightly-smoke" --cron "0 3 * * *" --eval smoke

# keep the scheduler running (or just run `evi web`, which starts it too)
evi scheduler

# check results — last status shows the pass-rate
evi schedule list
#   on  a1b2c3d4 nightly-smoke   cron=0 3 * * * last=ok (3/3)
```

Each run's detail is written to `~/.evi/logs/scheduled/`.

## Notes / limits

- **Both checks must pass.** When a case has a `judge` rubric, the deterministic assertions *and* the judge both have to pass. The judge never overrides a failed `contains`/`regex`/etc.
- **The judge fails closed.** If the grader returns anything that doesn't start with `PASS` — including an empty reply, a stray prefix, or a backend error — the case is marked FAIL. There is no "fail-open" that quietly passes ungraded cases. (The one true safety default is the engine error `judge rubric set but no grader available`, which only occurs if a suite is run without a grader; the CLI and scheduler always supply one.)
- **Same model grades and answers.** The LLM-as-judge uses your `[llm]` model, with no tools. Be aware of the obvious bias risk: a model judging its own output. For meaningful model comparisons, change the `[llm]` model between runs (which changes the answerer *and* the judge), or lean more on deterministic assertions.
- **No shared state between cases.** Every case is a fresh headless run with no conversation history, so cases can't depend on each other's context. Memory/skills are available to runs but each case starts clean.
- **Cost & determinism.** Each case is at least one model call; a judged case is *two* (answer + grade). Judge verdicts are non-deterministic — prefer deterministic assertions where you can, and reserve `judge` for genuinely fuzzy criteria.
- **`equals` strips whitespace** before comparing; `contains`/`not_contains`/`regex`/`equals` honor `ignore_case`. `regex` uses Python `re.search` (not full-match), so anchor with `^…$` if you need an exact shape.
- **Malformed suites are skipped in listings** (`evi eval list` quietly drops files it can't parse) but cause `evi eval run` to exit `2` with an error — so a typo in the suite you're targeting is surfaced loudly.
- **Tools really run.** Because eval runs auto-execute tools, a case in `cowork`/`code` mode may touch the filesystem, network, or shell. Keep eval prompts safe and side-effect-free, especially for scheduled suites that fire unattended.
- **Scheduled evals need the scheduler running** and the `[scheduler]` extra installed; otherwise the cron job never fires.

### Relevant source

- `C:\evi\evi\evals.py` — suite/case model, assertion engine (`check_case`), runner (`run_eval`), starter template.
- `C:\evi\evi\apps\cli\main.py` — the `evi eval list|new|run` commands (`eval_app`) and the `evi schedule add --eval` wiring (`schedule_add`).
- `C:\evi\evi\scheduler.py` — `_run_eval_once`, which runs a suite on a cron and records the `ok (P/T)` / `fail (P/T)` status.
