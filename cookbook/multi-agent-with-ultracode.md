# Fan work out across subagents

Instead of answering a hard task in one pass — or grinding through a batch of
independent tasks one at a time — spread the work across several scoped agents
that run concurrently, then collect the results.

> **Uses:** CLI (`evi ultracode`, `/ultra`) · Python SDK (`evi.sdk`) · config
> (`[ultracode]` in `~/.evi/config.toml`)

## Two shapes of fan-out

eVi has two, and they solve different problems:

- **Many tasks, one agent each** — `run_subagents_parallel` runs a *list of
  different tasks*, each in its own one-shot agent with a scoped toolset, and
  returns the results in input order. This is map/reduce over prompts.
- **One hard task, many angles** — **ultracode** takes a *single* task and runs
  it through a fixed pipeline: `decompose → fan out N solvers (diverse angles) →
  adversarial verify → synthesize`. This is depth on one problem.

## Ultracode from the CLI

Point it at one genuinely hard task and it prints each stage as it runs, then the
final answer:

```bash
evi ultracode "refactor the auth module to remove duplicated token parsing, add tests"
evi ultracode "design a retry policy for the HTTP client" --breadth 4 --rounds 2
evi ultracode "<task>" --json      # full result incl. every stage's output
```

`--breadth/-b` sets the number of parallel solver angles, `--rounds/-r` the
verify→refine cycles (`0` skips the critic), and `--mode/-m` the solver toolset
(`chat | cowork | code`). A default run is ~8 model calls; start cheap with
`--breadth 1 --rounds 0` to gauge cost.

In the REPL, `/ultra <task>` runs one turn through the pipeline, bare `/ultra`
toggles it session-wide, and `/effort ultracode` auto-runs every substantive
turn through it (Claude-parity).

## Routing the fan-out to a cheaper model

The N parallel solvers are the expensive part; the critic and synthesizer are
where quality is won. Keep those sharp and send just the solvers to a small
model:

```bash
evi ultracode "<task>" --cheap-fanout                # solvers → [llm] fast_model
evi ultracode "<task>" --solver-model qwen2.5:3b     # solvers → an explicit model
evi ultracode "<task>" --synth-model qwen2.5-coder:14b
```

## Tuning the defaults

Set them once under `[ultracode]` so every surface inherits them:

```toml
[ultracode]
breadth = 3          # parallel solver angles (1 disables fan-out)
rounds = 1           # verify->refine cycles (0 skips critique)
mode = "code"        # solver toolset: chat | cowork | code
max_workers = 4      # cap on concurrent stage agents
auto_tune = true     # downshift breadth/rounds for tiny / short-context models
cheap_fanout = false # run solvers on [llm] fast_model (critic/synth stay on main)
```

The full knob list is in
[`docs/features/ultracode.md`](https://github.com/evi-assistant/evi-ai/blob/main/docs/features/ultracode.md).

## From the SDK: independent tasks

For the map/reduce shape, hand a list of tasks to `run_subagents_parallel`. Each
gets its own agent, and you choose which tool *categories* it may touch (`()` =
none). See
[`examples/python/subagents.py`](https://github.com/evi-assistant/evi-ai/blob/main/examples/python/subagents.py):

```python
from evi.sdk import run_subagents_parallel

results = run_subagents_parallel(
    ["Summarise a JWT.", "Summarise a UUID.", "Summarise a bloom filter."],
    system_prompt="You are a terse technical explainer.",
    tool_categories=(),      # no tools: pure explanation
    max_workers=3,
)
for task, answer in results:   # returned in input order
    print(task, "->", answer)
```

## From the SDK: the full pipeline

To drive the ultracode pipeline yourself, wrap `build_agent` in a factory,
build a `run_one` with `make_runner`, and pass it to `run_ultracode`:

```python
from evi.sdk import build_agent, make_runner, run_ultracode, UltraConfig

def factory(system_prompt, model=None):
    return build_agent(system_prompt=system_prompt, model=model)

run_one = make_runner(factory)
res = run_ultracode(
    "design a retry policy for the HTTP client",
    run_one=run_one,
    cfg=UltraConfig(breadth=4, rounds=2),
)
print(res.answer)
for stage in res.stages:       # decompose / solve / verify / synthesize
    print(stage.name, stage.label)
```

## Where to go next

- [Build an agent with the SDK](agent-sdk-quickstart.md) — the `build_agent` /
  `run_headless` basics the pipeline factory builds on.
- [Guard an agent's inputs and outputs](guardrails.md) — safety checks that wrap
  every stage the fan-out spawns.
