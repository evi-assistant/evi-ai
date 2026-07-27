# Review a pull request in CI

Run eVi headless in a pipeline and fail the build on real findings — either with
the built-in reviewer or your own agent.

> **Uses:** CLI + Python SDK · **You'll need:** eVi installed in the CI job and a
> model backend it can reach (a local server on the runner, or an
> OpenAI-compatible endpoint via `evi backend add`).

## Option A — the built-in reviewer

`evi review` diffs your changes, has a model review them, and can turn the
verdict into an exit code. That is all you need for a gate:

```bash
# Fail the job if the review verdict is FAIL. --multi runs several lenses.
evi review --multi --exit-code
```

Useful flags:

- `--staged` — review `git diff --cached` (pre-commit).
- `--branch main` — review this branch against `main`.
- `--json` — emit `{verdict, exit_code, review}` for a later step to parse.
- `--diff-file patch.diff` — review a saved patch instead of the working tree.

A drop-in GitHub Actions workflow is in
[`examples/github/pr-review.yml`](../examples/github/pr-review.yml).

## Option B — your own agent, headless

When you want custom logic — post the summary as a PR comment, gate on a
specific check — drive an agent yourself. `run_headless` returns a structured
result and `to_json` serialises it; set the exit code from `result.error`:

```python
from evi.sdk import build_agent, run_headless, to_json

# Scope the agent to read-only fs + code tools for an unattended run.
agent = build_agent(tool_categories=["fs", "code"])
result = run_headless(agent, "Does this diff introduce any obvious bug? Answer with findings.", max_turns=8)

print(to_json(result))          # {"text", "tools", "usage", "error"}
raise SystemExit(1 if result.error else 0)
```

Runnable version: [`examples/python/headless_ci.py`](../examples/python/headless_ci.py)

```bash
python examples/python/headless_ci.py "Does this repo have a README? Answer yes/no."
```

## Keeping CI runs safe and cheap

- **Scope the tools.** `tool_categories=["fs", "code"]` gives the agent
  read-only file and search tools — no shell, no writes — which is what an
  unattended run should have.
- **Bound the work.** `max_turns=` caps how many tool round-trips a single run
  may take, so a confused model can't loop forever.
- **Pick a small model on the runner.** See
  [Run a model on your own hardware](run-a-local-model.md) — a 7B coder model is
  plenty for diff review and keeps the job fast.

## Where to go next

- [Guard an agent's inputs and outputs](guardrails.md) — add input/output safety
  on top of a headless run.
