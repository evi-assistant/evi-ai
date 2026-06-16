"""Ultracode — a fixed, exhaustive multi-agent pipeline for one hard task.

eVi's analogue of Claude Code's `ultracode`: instead of answering a substantial
task in a single pass, fan it out across several solver agents that attack it
from different angles, have an adversarial critic try to break each candidate,
then synthesize the survivors into one answer. More trustworthy than a single
shot, at the cost of more model calls.

Why a *fixed* Python pipeline (not a model-authored script like Claude's):
eVi targets local models (qwen2.5-coder:14b and down) that can't reliably write
orchestration. So the orchestration is hard-coded here; the model is only ever
asked to answer one concrete, role-scoped sub-prompt per stage — the floor a
small model can clear. Knobs collapse it for the weakest models
(``breadth=1, rounds=0`` ≈ ``evi run`` plus a synthesis pass).

The core is **model-free**: :func:`run_ultracode` takes an injected
``run_one(system_prompt, task, mode) -> text`` callable, exactly like
:func:`evi.evals.make_runners` / :func:`evi.workflows.run_workflow`. The CLI,
REPL, and web each build their own via :func:`make_runner`, so the pipeline is
unit-testable without a backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from evi.workflows import fan_out

# A stage run with this mode gets NO tools — pure reasoning over the text it's
# handed. Used for decompose / critique / synthesize so only the *solver* stage
# can touch the filesystem or shell. Keeps an adversarial critic from writing.
NO_TOOLS = "none"


# --- diversity angles (baked into the solver task text, like review lenses) --

SOLVER_ANGLES: dict[str, str] = {
    "direct": "Solve it the most direct, conventional way.",
    "first_principles": "Ignore convention; reason from first principles.",
    "edge_cases": "Optimise for correctness on edge cases and failure modes.",
    "simplicity": "Optimise for the simplest solution a reviewer would accept.",
    "performance": "Optimise for efficiency and resource use.",
    "alt": "Deliberately take a DIFFERENT approach than the obvious one.",
}

# --- role system prompts (constants, like review.MULTI_REVIEW_SYSTEM_PROMPT) -

DECOMPOSE_SYSTEM_PROMPT = (
    "You are a planning assistant. Break the user's task into a short, concrete "
    "list of sub-goals and the key constraints/risks to keep in mind. Be brief "
    "(a handful of bullets). Do not solve the task — just map it."
)
SOLVER_SYSTEM_PROMPT = (
    "You are an expert problem-solver. Produce a complete, concrete solution to "
    "the task, following the ANGLE you are given. If it's code, give the actual "
    "code; if it's analysis, give the actual answer. Be self-contained — your "
    "answer will be judged on its own."
)
CRITIC_SYSTEM_PROMPT = (
    "You are a rigorous adversarial reviewer. Find the single strongest flaw, "
    "bug, gap, or risk in the candidate answer to the task. Be specific and "
    "concrete. If you genuinely cannot find a real problem, reply with exactly "
    "APPROVE on the first line. Do not rewrite the answer — only critique it."
)
SYNTH_SYSTEM_PROMPT = (
    "You are a synthesis assistant. You are given several candidate answers to a "
    "task and an adversarial critique of each. Produce the single best final "
    "answer: merge the strengths, fix the flaws the critiques raised, and drop "
    "weak ideas. If one candidate is already best and you cannot improve it, "
    "return it verbatim. Ignore any candidate whose text begins with 'ERROR:'. "
    "Output only the final answer — no commentary about the candidates."
)


# --- data ------------------------------------------------------------------


@dataclass
class UltraConfig:
    breadth: int = 3          # parallel solver angles (1 disables fan-out)
    rounds: int = 1           # verify passes (0 skips critique entirely)
    mode: str = "code"        # tool preset for the SOLVER stage (chat|cowork|code)
    angles: list[str] = field(default_factory=list)  # [] => first `breadth`
    max_workers: int = 4
    # Optional per-stage model override {stage -> model id}, stages:
    # decompose | solve | verify | synthesize. Empty = every stage on the main
    # model. make_runner reads this to route a stage to a cheaper model.
    stage_models: dict[str, str] = field(default_factory=dict)


@dataclass
class UltraStage:
    name: str    # decompose | solve | verify | synthesize
    label: str   # angle name / round marker / "plan" / "final"
    output: str


@dataclass
class UltraResult:
    task: str
    answer: str
    stages: list[UltraStage]
    config: UltraConfig


# --- pure prompt builders (no model, no I/O) -------------------------------


def decompose_prompt(task: str) -> str:
    return f"Task:\n{task}\n\nMap this task: sub-goals + key constraints/risks."


def solver_prompt(task: str, angle_instruction: str, plan: str, prior_critique: str = "") -> str:
    parts = [f"Task:\n{task}"]
    if plan.strip():
        parts.append(f"Plan / context:\n{plan}")
    parts.append(f"Your angle: {angle_instruction}")
    if prior_critique.strip():
        parts.append(
            "A reviewer critiqued your previous attempt — address it in this "
            f"revision:\n{prior_critique}"
        )
    parts.append("Give your complete solution.")
    return "\n\n".join(parts)


def critic_prompt(task: str, candidate: str) -> str:
    return (
        f"Task:\n{task}\n\nCandidate answer:\n{candidate}\n\n"
        "Find the single strongest flaw, or reply APPROVE."
    )


def synthesize_prompt(task: str, candidates: list[str], critiques: list[str]) -> str:
    blocks = [f"Task:\n{task}\n"]
    for i, cand in enumerate(candidates):
        blocks.append(f"--- Candidate {i + 1} ---\n{cand}")
        if i < len(critiques) and critiques[i].strip():
            blocks.append(f"Critique of candidate {i + 1}:\n{critiques[i]}")
    blocks.append("\nProduce the single best final answer.")
    return "\n\n".join(blocks)


def select_angles(cfg: UltraConfig) -> list[str]:
    """The angle names to run: explicit ``cfg.angles`` (validated) or the first
    ``cfg.breadth`` built-in angles. ``breadth`` is clamped to >= 1."""
    if cfg.angles:
        unknown = [a for a in cfg.angles if a not in SOLVER_ANGLES]
        if unknown:
            raise ValueError(
                f"unknown ultracode angle(s): {', '.join(unknown)} "
                f"(known: {', '.join(SOLVER_ANGLES)})"
            )
        return list(cfg.angles)
    n = max(1, cfg.breadth)
    return list(SOLVER_ANGLES)[:n]


# --- the model-free core ----------------------------------------------------


def run_ultracode(
    task: str,
    *,
    run_one: Callable[[str, str, str, str], str],
    cfg: UltraConfig | None = None,
    on_stage: Callable[[UltraStage], None] | None = None,
) -> UltraResult:
    """Run the fixed pipeline. ``run_one(system_prompt, task, mode, stage) -> text``
    where ``stage`` is one of decompose | solve | verify | synthesize (so the
    runner can route a stage to a different model — see :func:`make_runner`).

    decompose → fan-out(N solvers, diverse angles) → [verify → refine] × rounds
    → synthesize. Each stage is one ``run_one`` call; a stage that fails returns
    an ``ERROR: …`` string (never raises) and is passed to synthesis, which is
    told to ignore errored candidates.
    """
    cfg = cfg or UltraConfig()
    stages: list[UltraStage] = []

    def emit(name: str, label: str, output: str) -> str:
        st = UltraStage(name=name, label=label, output=output)
        stages.append(st)
        if on_stage is not None:
            on_stage(st)
        return output

    # 1. decompose (sequential, no tools)
    plan = emit("decompose", "plan",
                run_one(DECOMPOSE_SYSTEM_PROMPT, decompose_prompt(task), NO_TOOLS, "decompose"))

    angles = select_angles(cfg)

    def _solve(pair: tuple[str, str]) -> str:
        angle, critique = pair
        return run_one(
            SOLVER_SYSTEM_PROMPT,
            solver_prompt(task, SOLVER_ANGLES[angle], plan, critique),
            cfg.mode,
            "solve",
        )

    # 2. solve (fan-out)
    candidates = fan_out(_solve, [(a, "") for a in angles], cfg.max_workers)
    for a, c in zip(angles, candidates):
        emit("solve", a, c)

    # 3. verify (+ optional refine rounds)
    critiques: list[str] = []
    for r in range(max(0, cfg.rounds)):
        critiques = fan_out(
            lambda cand: run_one(CRITIC_SYSTEM_PROMPT, critic_prompt(task, cand), NO_TOOLS, "verify"),
            candidates,
            cfg.max_workers,
        )
        rmark = f" (round {r + 1})" if cfg.rounds > 1 else ""
        for a, cr in zip(angles, critiques):
            emit("verify", a + rmark, cr)
        if r < cfg.rounds - 1:  # refine for the next pass
            candidates = fan_out(_solve, list(zip(angles, critiques)), cfg.max_workers)
            for a, c in zip(angles, candidates):
                emit("solve", f"{a} (refine {r + 1})", c)

    # 4. synthesize (sequential fan-in, no tools)
    answer = emit("synthesize", "final",
                  run_one(SYNTH_SYSTEM_PROMPT, synthesize_prompt(task, candidates, critiques),
                          NO_TOOLS, "synthesize"))
    return UltraResult(task=task, answer=answer, stages=stages, config=cfg)


# --- the headless wiring (one home, mirrors evals.make_runners) -------------


def make_runner(
    agent_factory: Callable[..., Any],
    stage_models: dict[str, str] | None = None,
) -> Callable[[str, str, str, str], str]:
    """Build the ``run_one`` :func:`run_ultracode` needs from an agent factory.

    ``agent_factory(system_prompt, model)`` must return a FRESH Agent constructed
    WITH that system prompt (threaded through construction — never mutated after)
    and, when ``model`` is given, that model id. Each stage gets its own agent,
    so per-stage context stays small regardless of pipeline length.
    ``mode == NO_TOOLS`` strips tools (decompose/critic/synth); a real mode name
    scopes the solver's toolset.

    ``stage_models`` maps a stage name (decompose | solve | verify | synthesize)
    to a model id; a stage not in the map runs on the factory's default model.
    This is how "cheaper fan-out" works — pass ``{"solve": fast_model}``.
    """
    from evi.headless import run_headless
    from evi.modes import mode_tools

    sm = stage_models or {}

    def run_one(system_prompt: str, task: str, mode: str, stage: str = "") -> str:
        # Never raise: a flaky stage (e.g. a mid-stream backend drop) must
        # degrade to an ignorable ERROR candidate, not tear down the whole
        # fan-out via a re-raised future. synthesis is told to ignore these.
        try:
            agent = agent_factory(system_prompt, sm.get(stage))
            if mode == NO_TOOLS:
                agent.tools = {}
            elif mode:
                agent.tools = {t.name: t for t in mode_tools(mode)}
            agent.enable_auto_all()
            res = run_headless(agent, task)
            return res.text or (f"ERROR: {res.error}" if res.error else "")
        except Exception as exc:  # noqa: BLE001 — a stage failure can't crash the run
            return f"ERROR: {type(exc).__name__}: {exc}"

    return run_one


# --- config + small-model tuning -------------------------------------------


def load_ultra_config(cfg=None) -> UltraConfig:
    """Build an :class:`UltraConfig` from the ``[ultracode]`` config section.

    Resolves ``cheap_fanout`` → ``stage_models={"solve": fast_model}`` here (it
    needs both the ``[ultracode]`` and ``[llm]`` sections), so every caller
    inherits the cheaper fan-out without re-implementing it. A no-op when
    ``fast_model`` is unset.
    """
    from evi.config import Config

    full = cfg or Config.load()
    u = full.ultracode
    stage_models: dict[str, str] = {}
    if getattr(u, "cheap_fanout", False) and full.llm.fast_model:
        stage_models["solve"] = full.llm.fast_model
    return UltraConfig(
        breadth=u.breadth, rounds=u.rounds, mode=u.mode,
        angles=list(u.angles), max_workers=u.max_workers,
        stage_models=stage_models,
    )


# Match a SMALL parameter-size token (0.5b/1b/1.5b/3b) on number boundaries, so
# "14b"/"21b"/"31b"/"71b"/"13b"/"33b" are NOT misread as small. `mini`/`small`
# name tokens cover phi-3.5-mini etc. ("phi" alone is excluded — phi-4 is ~14B).
_SMALL_SIZE_RE = re.compile(r"(?<![\d.])(?:0\.5|1\.5|1|3)b(?![\d])")


def _looks_small(model: str) -> bool:
    m = (model or "").lower()
    return bool(_SMALL_SIZE_RE.search(m)) or "mini" in m or "small" in m


def default_tuning(model: str, context_size: int, base: UltraConfig) -> UltraConfig:
    """Downshift breadth/rounds for tiny or short-context models so ultracode
    stays usable (and finishes) on weak local backends. Returns ``base``
    unchanged for capable models."""
    if _looks_small(model) or (context_size and context_size < 16000):
        return replace(base, breadth=min(base.breadth, 2), rounds=0)
    return base
