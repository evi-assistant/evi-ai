"""Dynamic workflows — scriptable multi-agent orchestration (Phase 86).

Where a *recipe* (Phase 58) is a sequence of turns through one shared
conversation, a *workflow* orchestrates independent steps with structure:

- each step runs its own headless agent (fresh context),
- contiguous steps marked ``parallel = true`` run concurrently (fan-out),
- a step's prompt can interpolate earlier steps' outputs and workflow vars via
  ``{step_id}`` / ``{var}`` — so a later step fans the parallel results back in.

Workflows live at ``~/.evi/workflows/<name>.toml``:

    name = "research"
    description = "Plan, research two angles in parallel, then synthesize."

    [vars]
    topic = "local-first AI"

    [[steps]]
    id = "plan"
    prompt = "Outline an approach to research {topic}."

    [[steps]]
    id = "pros"
    parallel = true
    prompt = "Given this plan, list the upsides of {topic}.\\nPlan: {plan}"

    [[steps]]
    id = "cons"
    parallel = true
    prompt = "Given this plan, list the downsides of {topic}.\\nPlan: {plan}"

    [[steps]]
    id = "synth"
    prompt = "Synthesize a balanced take.\\nUpsides: {pros}\\nDownsides: {cons}"

The engine is decoupled from the LLM: ``run_workflow`` takes a ``run_step``
callable, so it's fully testable without a model. Use ``{{`` / ``}}`` for
literal braces in a prompt.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import evi.config as config

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


class WorkflowError(Exception):
    """A workflow file is missing or malformed."""


@dataclass
class WStep:
    id: str
    prompt: str
    parallel: bool = False
    mode: str = ""          # optional tool preset: chat | cowork | code
    label: str = ""


@dataclass
class Workflow:
    name: str
    description: str = ""
    steps: list[WStep] = field(default_factory=list)
    vars: dict[str, str] = field(default_factory=dict)
    path: Path | None = None


def workflows_dir(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "workflows"


def _slug(name: str) -> str:
    return Path(name).name.removesuffix(".toml")


def load_workflow_file(path: Path) -> Workflow:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowError(f"could not read {path.name}: {exc}") from exc

    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError(f"{path.name}: a workflow needs at least one [[steps]] entry")

    steps: list[WStep] = []
    seen: set[str] = set()
    for i, s in enumerate(raw_steps, 1):
        if not isinstance(s, dict) or not str(s.get("prompt", "")).strip():
            raise WorkflowError(f"{path.name}: step {i} is missing a non-empty `prompt`")
        sid = str(s.get("id") or f"step{i}").strip()
        if sid in seen:
            raise WorkflowError(f"{path.name}: duplicate step id {sid!r}")
        seen.add(sid)
        steps.append(
            WStep(
                id=sid,
                prompt=str(s["prompt"]).strip(),
                parallel=bool(s.get("parallel", False)),
                mode=str(s.get("mode", "")).strip(),
                label=str(s.get("label", "")).strip(),
            )
        )

    raw_vars = data.get("vars", {}) or {}
    variables = {str(k): str(v) for k, v in raw_vars.items()} if isinstance(raw_vars, dict) else {}

    return Workflow(
        name=str(data.get("name") or path.stem).strip(),
        description=str(data.get("description", "")).strip(),
        steps=steps,
        vars=variables,
        path=path,
    )


def load_workflow(name: str, root: Path | None = None) -> Workflow:
    path = workflows_dir(root) / f"{_slug(name)}.toml"
    if not path.is_file():
        raise WorkflowError(f"no workflow named {_slug(name)!r} (looked in {workflows_dir(root)})")
    return load_workflow_file(path)


def list_workflows(root: Path | None = None) -> list[Workflow]:
    d = workflows_dir(root)
    out: list[Workflow] = []
    if d.is_dir():
        for p in sorted(d.glob("*.toml")):
            try:
                out.append(load_workflow_file(p))
            except WorkflowError:
                continue  # skip malformed files rather than abort the listing
    return out


def _interp(prompt: str, mapping: dict[str, Any], step_id: str) -> str:
    try:
        return prompt.format_map(mapping)
    except KeyError as exc:
        key = exc.args[0] if exc.args else "?"
        raise WorkflowError(
            f"step {step_id!r}: unknown reference {{{key}}} "
            f"(known: {', '.join(sorted(mapping)) or 'none'})"
        ) from exc
    except (IndexError, ValueError) as exc:
        raise WorkflowError(
            f"step {step_id!r}: bad prompt template ({exc}); "
            f"escape literal braces as {{{{ and }}}}"
        ) from exc


def run_workflow(
    wf: Workflow,
    *,
    run_step: Callable[[str, WStep], str],
    variables: dict[str, str] | None = None,
    max_workers: int = 4,
) -> dict[str, str]:
    """Execute `wf`, returning {step_id: output}.

    `run_step(prompt, step)` runs one step and returns its text output. Steps
    run in file order; a contiguous run of `parallel = true` steps executes
    concurrently and each sees only outputs produced BEFORE that block (so a
    following sequential step is the natural fan-in point).
    """
    outputs: dict[str, str] = {}
    ctx: dict[str, str] = dict(wf.vars)
    ctx.update(variables or {})

    steps = wf.steps
    i = 0
    while i < len(steps):
        if steps[i].parallel:
            block: list[WStep] = []
            while i < len(steps) and steps[i].parallel:
                block.append(steps[i])
                i += 1
            base = {**ctx, **outputs}  # frozen snapshot for the whole block
            prompts = {s.id: _interp(s.prompt, base, s.id) for s in block}
            with ThreadPoolExecutor(max_workers=min(max_workers, len(block))) as ex:
                fut_to_id = {
                    ex.submit(run_step, prompts[s.id], s): s.id for s in block
                }
                for fut in as_completed(fut_to_id):
                    outputs[fut_to_id[fut]] = fut.result()
        else:
            s = steps[i]
            i += 1
            outputs[s.id] = run_step(_interp(s.prompt, {**ctx, **outputs}, s.id), s)
    return outputs


_TEMPLATE = '''\
name = "{name}"
description = "What this workflow does"

# Optional defaults; override at run time with `evi workflow run {name} --var k=v`.
[vars]
topic = "local-first AI"

# Steps run in order. A run of `parallel = true` steps runs concurrently; a
# later step fans them back in by referencing their ids — {{plan}}, {{pros}}, …
[[steps]]
id = "plan"
prompt = "Outline an approach to research {{topic}}."

[[steps]]
id = "pros"
parallel = true
prompt = "List the upsides of {{topic}} given this plan:\\n{{plan}}"

[[steps]]
id = "cons"
parallel = true
prompt = "List the downsides of {{topic}} given this plan:\\n{{plan}}"

[[steps]]
id = "synth"
prompt = "Synthesize a balanced take.\\nUpsides:\\n{{pros}}\\nDownsides:\\n{{cons}}"
'''


def create_workflow(name: str, root: Path | None = None, overwrite: bool = False) -> Path:
    """Write a starter workflow template and return its path."""
    d = workflows_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)
    path = d / f"{slug}.toml"
    if path.exists() and not overwrite:
        raise WorkflowError(f"workflow {slug!r} already exists (pass --overwrite to replace)")
    path.write_text(_TEMPLATE.format(name=slug), encoding="utf-8")
    return path
