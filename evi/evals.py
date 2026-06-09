"""Evals — regression-test prompts, skills, and models against assertions.

A suite is a TOML file of cases; each case is a prompt plus deterministic
assertions. Run it against the agent to get a pass-rate — catch a prompt/skill
regression, or compare two models on the same suite.

    ~/.evi/evals/<name>.toml:

        name = "smoke"
        description = "Sanity checks"

        [[case]]
        name = "math"
        prompt = "What is 2+2? Reply with just the number."
        contains = ["4"]
        not_contains = ["error"]

        [[case]]
        name = "json"
        prompt = "Return a JSON object with ok=true."
        regex = '"ok"\\s*:\\s*true'

Assertions (all listed must hold): `contains` (all substrings present),
`not_contains` (none present), `regex` (search matches), `equals` (exact, after
strip). `ignore_case` makes them case-insensitive; `mode` sets a tool preset.

The engine takes a `run_one` callable, so it's fully testable without a model.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import evi.config as config

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


class EvalError(Exception):
    """A suite file is missing or malformed."""


@dataclass
class EvalCase:
    name: str
    prompt: str
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    regex: str = ""
    equals: str | None = None
    ignore_case: bool = False
    mode: str = ""
    judge: str = ""   # LLM-as-judge rubric; graded by a model (needs judge_fn)


@dataclass
class EvalSuite:
    name: str
    description: str = ""
    cases: list[EvalCase] = field(default_factory=list)
    path: Path | None = None


def evals_dir(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "evals"


def _slug(name: str) -> str:
    return Path(name).name.removesuffix(".toml")


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if v in (None, ""):
        return []
    return [str(v)]


def load_suite_file(path: Path) -> EvalSuite:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EvalError(f"could not read {path.name}: {exc}") from exc

    raw = data.get("case", [])
    if not isinstance(raw, list) or not raw:
        raise EvalError(f"{path.name}: a suite needs at least one [[case]]")
    cases: list[EvalCase] = []
    for i, c in enumerate(raw, 1):
        if not isinstance(c, dict) or not str(c.get("prompt", "")).strip():
            raise EvalError(f"{path.name}: case {i} is missing a non-empty `prompt`")
        cases.append(
            EvalCase(
                name=str(c.get("name") or f"case{i}").strip(),
                prompt=str(c["prompt"]).strip(),
                contains=_as_list(c.get("contains")),
                not_contains=_as_list(c.get("not_contains")),
                regex=str(c.get("regex", "")),
                equals=(str(c["equals"]) if "equals" in c else None),
                ignore_case=bool(c.get("ignore_case", False)),
                mode=str(c.get("mode", "")).strip(),
                judge=str(c.get("judge", "")).strip(),
            )
        )
    return EvalSuite(
        name=str(data.get("name") or path.stem).strip(),
        description=str(data.get("description", "")).strip(),
        cases=cases,
        path=path,
    )


def load_suite(name: str, root: Path | None = None) -> EvalSuite:
    path = evals_dir(root) / f"{_slug(name)}.toml"
    if not path.is_file():
        raise EvalError(f"no eval suite named {_slug(name)!r} (looked in {evals_dir(root)})")
    return load_suite_file(path)


def list_suites(root: Path | None = None) -> list[EvalSuite]:
    d = evals_dir(root)
    out: list[EvalSuite] = []
    if d.is_dir():
        for p in sorted(d.glob("*.toml")):
            try:
                out.append(load_suite_file(p))
            except EvalError:
                continue
    return out


def check_case(case: EvalCase, output: str) -> tuple[bool, list[str]]:
    """Return (passed, failure_reasons) for one case against its output."""
    fails: list[str] = []
    hay = output.lower() if case.ignore_case else output

    def norm(s: str) -> str:
        return s.lower() if case.ignore_case else s

    for sub in case.contains:
        if norm(sub) not in hay:
            fails.append(f"missing {sub!r}")
    for sub in case.not_contains:
        if norm(sub) in hay:
            fails.append(f"unexpected {sub!r}")
    if case.regex:
        if not re.search(case.regex, output, re.I if case.ignore_case else 0):
            fails.append(f"no match for /{case.regex}/")
    if case.equals is not None:
        a, b = output.strip(), case.equals.strip()
        if case.ignore_case:
            a, b = a.lower(), b.lower()
        if a != b:
            fails.append("output != equals")
    return (not fails, fails)


def run_eval(
    suite: EvalSuite,
    run_one: Callable[[EvalCase], str],
    judge_fn: Callable[[EvalCase, str], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Run every case through `run_one(case) -> output text`. Returns a report:
    {name, total, passed, pass_rate, cases: [{name, passed, failures, output}]}.

    A case with a `judge` rubric is additionally graded by `judge_fn(case,
    output) -> (passed, reason)`; both the deterministic checks and the judge
    must pass. If a case needs a judge but none is provided, it fails.
    """
    results = []
    passed = 0
    for case in suite.cases:
        output = run_one(case)
        ok, failures = check_case(case, output)
        if case.judge:
            if judge_fn is None:
                ok = False
                failures.append("judge rubric set but no grader available")
            else:
                jok, reason = judge_fn(case, output)
                if not jok:
                    ok = False
                    failures.append(f"judge: {reason}")
        if ok:
            passed += 1
        results.append(
            {"name": case.name, "passed": ok, "failures": failures, "output": output}
        )
    total = len(suite.cases)
    return {
        "name": suite.name,
        "total": total,
        "passed": passed,
        "pass_rate": (passed / total) if total else 0.0,
        "cases": results,
    }


def make_runners(
    agent_factory: Callable[[], Any],
    *,
    default_mode: str = "",
) -> tuple[Callable[[EvalCase], str], Callable[[EvalCase, str], tuple[bool, str]]]:
    """Build the ``(run_one, judge_fn)`` pair that :func:`run_eval` expects.

    ``agent_factory()`` must return a fresh Agent (one per call — cases must not
    share conversation state). This is the single home for the headless-run and
    LLM-as-judge grading logic, shared by the CLI ``evi eval run`` and the web
    ``POST /api/evals/run`` so the two surfaces can never drift.
    """
    from evi.headless import run_headless
    from evi.modes import mode_tools

    def run_one(case: EvalCase) -> str:
        agent = agent_factory()
        m = case.mode or default_mode
        if m:
            agent.tools = {t.name: t for t in mode_tools(m)}
        agent.enable_auto_all()
        res = run_headless(agent, case.prompt)
        return res.text or (f"ERROR: {res.error}" if res.error else "")

    def judge_fn(case: EvalCase, output: str) -> tuple[bool, str]:
        agent = agent_factory()
        agent.tools = {}  # the grader answers from text alone, no tools
        prompt = (
            "Grade the ANSWER against the RUBRIC. Reply with exactly PASS or FAIL "
            "on the first line, then a one-line reason.\n\n"
            f"RUBRIC: {case.judge}\n\nANSWER:\n{output}"
        )
        res = run_headless(agent, prompt)
        text = (res.text or "").strip()
        first = text.splitlines()[0] if text else ""
        return first.strip().upper().startswith("PASS"), (first[:200] or "no judge output")

    return run_one, judge_fn


_TEMPLATE = '''\
name = "{name}"
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
'''


def create_suite(name: str, root: Path | None = None, overwrite: bool = False) -> Path:
    """Write a starter eval suite and return its path."""
    d = evals_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)
    path = d / f"{slug}.toml"
    if path.exists() and not overwrite:
        raise EvalError(f"eval suite {slug!r} already exists (pass --overwrite to replace)")
    path.write_text(_TEMPLATE.format(name=slug), encoding="utf-8")
    return path
