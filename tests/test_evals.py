"""Tests for the evals harness (evi eval)."""

from __future__ import annotations

import pytest

from evi import evals
from evi.evals import EvalCase, EvalSuite


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


# ---- run -----------------------------------------------------------------


def test_run_eval_report():
    suite = EvalSuite(name="s", cases=[
        EvalCase(name="a", prompt="p", contains=["4"]),
        EvalCase(name="b", prompt="p", contains=["nope"]),
    ])
    report = evals.run_eval(suite, run_one=lambda case: "the answer is 4")
    assert report["total"] == 2 and report["passed"] == 1
    assert report["pass_rate"] == 0.5
    by = {c["name"]: c for c in report["cases"]}
    assert by["a"]["passed"] and not by["b"]["passed"]
    assert by["b"]["failures"]
