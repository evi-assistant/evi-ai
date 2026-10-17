"""Tests for the evals harness (evi eval)."""

from __future__ import annotations

import pytest

from evi import evals
from evi.evals import EvalCase


# ---- loading -------------------------------------------------------------


def test_load_suite_file(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text(
        'name = "smoke"\n'
        '[[case]]\nname = "m"\nprompt = "2+2?"\ncontains = ["4"]\n'
        '[[case]]\nprompt = "x"\nregex = "ok"\nignore_case = true\n',
        encoding="utf-8",
    )
    s = evals.load_suite_file(p)
    assert s.name == "smoke" and len(s.cases) == 2
    assert s.cases[0].contains == ["4"]
    assert s.cases[1].name == "case2" and s.cases[1].ignore_case is True


def test_load_rejects_no_cases(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('name = "x"\n', encoding="utf-8")
    with pytest.raises(evals.EvalError):
        evals.load_suite_file(p)


def test_create_and_reload(tmp_path):
    path = evals.create_suite("smoke", root=tmp_path)
    assert path.is_file()
    s = evals.load_suite("smoke", root=tmp_path)
    assert len(s.cases) >= 1
    with pytest.raises(evals.EvalError):
        evals.create_suite("smoke", root=tmp_path)


# ---- assertions ----------------------------------------------------------


def test_check_contains_and_not_contains():
    case = EvalCase(name="c", prompt="p", contains=["foo"], not_contains=["bar"])
    assert evals.check_case(case, "has foo only") == (True, [])
    ok, fails = evals.check_case(case, "has bar")
    assert not ok and any("foo" in f for f in fails) and any("bar" in f for f in fails)


def test_check_regex():
    case = EvalCase(name="c", prompt="p", regex=r"\d{3}")
    assert evals.check_case(case, "abc 123")[0] is True
    assert evals.check_case(case, "no digits")[0] is False


def test_check_equals_and_ignore_case():
    case = EvalCase(name="c", prompt="p", equals="Yes", ignore_case=True)
    assert evals.check_case(case, "  yes ")[0] is True
    assert evals.check_case(EvalCase(name="c", prompt="p", equals="Yes"), "no")[0] is False


def test_check_no_assertions_passes():
    assert evals.check_case(EvalCase(name="c", prompt="p"), "anything")[0] is True


# ---- LLM-as-judge (evaluate: the judge and the answer assertions) ----------


def _evaluate(tmp_path, output: str, grader=None, **case_kw) -> list[str]:
    """`evals.evaluate` (with `grader` as the judge) on a finished run that
    answered `output` and did nothing else (no tool call, file or command)."""
    from evi.headless import HeadlessResult

    case = EvalCase(name="j", prompt="p", **case_kw)
    res = HeadlessResult(text=output, stop_reason="done", files_changed=[])
    return evals.evaluate(case, res, tmp_path, None, judge=grader)


def test_judge_pass(tmp_path):
    assert _evaluate(tmp_path, "hi friend!", lambda c, o: (True, "friendly"),
                     judge="is friendly") == []


def test_judge_fail_reason(tmp_path):
    fails = _evaluate(tmp_path, "go away", lambda c, o: (False, "rude"), judge="is friendly")
    assert fails == ["judge: rude"]


def test_judge_without_grader_fails(tmp_path):
    assert _evaluate(tmp_path, "out", judge="x") == ["judge rubric set but no grader available"]


def test_judge_and_deterministic_both_required(tmp_path):
    # deterministic check fails even though judge passes -> case fails
    fails = _evaluate(tmp_path, "text", lambda c, o: (True, "ok"), contains=["MISSING"],
                      judge="ok")
    assert fails == ["missing 'MISSING'"]


# ---- make_judge (the `judge` grader `model_judge` builds) ------------------------


def test_make_judge_pass(monkeypatch):
    from evi.headless import HeadlessResult

    seen: list = []

    class FakeAgent:
        def __init__(self):
            self.tools = {"x": object()}

    def fake_run_headless(agent, prompt):
        seen.append((dict(agent.tools), prompt))
        return HeadlessResult(text="PASS looks good\nmore")

    monkeypatch.setattr("evi.headless.run_headless", fake_run_headless)
    ok, reason = evals.make_judge(FakeAgent)(EvalCase(name="j", prompt="x", judge="be nice"),
                                             "hi")
    assert ok is True and reason == "PASS looks good"
    # The grader answers from the text alone: no tools, the rubric and the answer.
    [(tools, prompt)] = seen
    assert tools == {} and "RUBRIC: be nice" in prompt and prompt.endswith("ANSWER:\nhi")


def test_make_judge_fail(monkeypatch):
    from evi.headless import HeadlessResult

    class FakeAgent:
        tools: dict = {}

    monkeypatch.setattr(
        "evi.headless.run_headless", lambda a, p: HeadlessResult(text="FAIL too terse")
    )
    ok, reason = evals.make_judge(FakeAgent)(EvalCase(name="j", prompt="x", judge="r"), "out")
    assert ok is False and "too terse" in reason
