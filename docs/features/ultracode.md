# Ultracode

**Ultracode** runs one hard task through an exhaustive multi-agent pipeline
instead of a single pass: it decomposes the task, fans out several solver agents
that attack it from different angles, has an adversarial critic try to break each
candidate, then synthesizes the survivors into one answer. It's eVi's analogue of
Claude Code's `ultracode` — more thorough and more trustworthy, at the cost of
more model calls.

## Overview

| | |
|---|---|
| **What** | A fixed pipeline: `decompose → fan-out N solvers (diverse angles) → adversarial verify → synthesize`. |
| **Why** | A single model pass misses edge cases and commits to the first approach. Multiple angles + an adversarial critic + a synthesis step catch more and produce a stronger answer. |
| **When** | One genuinely hard task — a tricky refactor, a design with real trade-offs, a bug you want cross-checked. Overkill for quick questions. |
| **Surfaces** | `evi ultracode "<task>"`, the `/ultra` REPL command, `/effort ultracode`, and **Settings → Ultracode** in the web/desktop app. |

### Why a fixed pipeline (not a model-authored script)

Claude Code's ultracode can lean on a strong model to *write* a bespoke
orchestration script per task. eVi targets **local models** (qwen2.5-coder:14b
and down) that can't reliably do that — so eVi's orchestration is **fixed Python**
(`evi/ultracode.py`). The model is only ever asked to answer one concrete,
role-scoped sub-prompt per stage (decompose / solve-one-angle / critique-one /
synthesize) — the floor even a small model can clear. Each stage is a fresh
headless agent, so per-stage context stays small no matter how long the pipeline.

## How it works

1. **Decompose** — one agent maps the task into sub-goals + key risks (context for the solvers; no tools).
2. **Solve (fan-out)** — `breadth` solver agents run in parallel, each told to take a different **angle** (direct, first-principles, edge-cases, simplicity, performance, alt). Solvers get the `mode` toolset (default `code`).
3. **Verify (adversarial)** — a critic reviews each candidate for its single strongest flaw (or says `APPROVE`). Runs with **no tools** — a critic can't write files. With `rounds > 1`, each critique is fed back to its solver for a refine pass, then re-critiqued.
4. **Synthesize (fan-in)** — one agent merges the strengths and fixes the critiqued flaws into the final answer (keeping the best candidate verbatim if it can't improve it; ignoring any `ERROR:` candidate).

The fan-out reuses `workflows.fan_out` (the same concurrency primitive behind
`evi workflow`'s parallel blocks). The core is **model-free** — `run_ultracode`
takes an injected `run_one` callable, exactly like `evals.make_runners` — so the
CLI, REPL, and web each supply their own agent factory and the pipeline is fully
unit-testable.

> **Local-model note:** with a single local backend, inference serialises, so
> breadth buys a **quality** win (diverse angles + adversarial cross-check) more
> than wall-clock speed. Real parallel speedup needs a multi-GPU box or a
> [federation peer](agents.md#federation).

## Setup

Defaults live under `[ultracode]` in `~/.evi/config.toml`:

```toml
[ultracode]
breadth = 3        # parallel solver angles (1 disables fan-out)
rounds = 1         # verify->refine cycles (0 skips critique — weakest-model escape hatch)
mode = "code"      # tool preset for solvers: chat | cowork | code
angles = []        # optional explicit angle names (empty = first `breadth`)
max_workers = 4    # cap on concurrent stage agents
auto_tune = true   # downshift breadth/rounds for tiny / short-context models
```

`auto_tune` downshifts to `breadth=2, rounds=0` for tiny models (names with
`1b/3b/mini/phi/small`) or short context (< 16k) so ultracode stays usable on
weak backends.

## Usage

### CLI

```bash
evi ultracode "refactor the auth module to remove duplicated token parsing, add tests"
evi ultracode "<task>" --breadth 4 --rounds 2 --mode code   # override config
evi ultracode "<task>" --json                               # full result incl. every stage
```

It prints each stage as it runs (`> solve direct`, `> verify edge_cases`, …) then
the final answer.

### REPL

```text
/ultra <task>     run THIS turn through the pipeline
/ultra            toggle session-wide auto-ultracode on/off
/effort ultracode max reasoning + auto-pipeline every substantive turn (Claude-parity);
                  /effort high|medium|low|max clears it
```

With auto-ultracode on, substantive turns (non-trivial, non-greeting) run through
the pipeline automatically; short/greeting turns fall through to a normal turn.
The prompt shows an `[ultracode]` chip while it's active.

### Web / Desktop

**Settings → Ultracode**: a task box with breadth/rounds/mode controls and a
**Run** button. It shows each stage (collapsible) and the final answer. Backed by
`POST /api/dispatch/ultracode` (`{task, breadth?, rounds?, mode?}` →
`{ok, answer, stages, config}`). Like the eval/recipe runners it blocks until the
whole pipeline finishes.

## Examples

```bash
# A weakest-model run: one solver, no critique (~3 calls, barely more than `evi run`)
evi ultracode "summarise what changed in this file" --breadth 1 --rounds 0

# A thorough run: 4 angles, a refine round
evi ultracode "design a retry policy for the HTTP client" --breadth 4 --rounds 2
```

## Notes / limits

- **Cost**: a default run is ~8 model calls (1 decompose + 3 solve + 3 verify + 1
  synthesize); `rounds=2` adds a refine + re-critique pass. Use `--breadth 1
  --rounds 0` to gauge cost cheaply first.
- **Synthesis regression**: a weak synthesizer can occasionally produce a worse
  answer than the best solver; the synthesis prompt mitigates this ("return the
  best candidate verbatim if you can't improve it"). Inspect stages with `--json`
  or the web panel if a result looks off.
- **Conversation coherence**: the `/ultra` and auto-ultracode paths store only the
  final answer in history (not the plan/angles/critiques), so `/context`
  under-reports the turn's real token cost.
- Stages that error return `ERROR: …` and are passed to synthesis (told to ignore
  them) — a failed stage never crashes the run.
