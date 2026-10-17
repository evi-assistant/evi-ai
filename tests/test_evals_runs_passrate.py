"""Runs, pass rates, expected failures, baselines and reports (loop-eval-gates).

A case runs `runs` times and passes when passes / runs >= `pass_rate`; the
report has one row per case (runs, passes, pass_rate, threshold, passed,
skipped, failures per failing run, mean turns and duration, stop_reasons)
under a header (suite, model, backend, n, evi version, start and finish,
hardware), validated against evi.evals.REPORT_SCHEMA and kept as JSON plus a
static HTML page. `expected_fail` reports without failing the suite;
`apply_baseline` fails a case below the baseline minus a tolerance; `compare`
gives per-case deltas.

Acceptance criterion 3 — runs=5 with pass_rate 0.8 reports correctly:
test_runs_5_pass_rate_0_8_passes_on_4_of_5_and_fails_on_3_of_5 (an Agent.chat
run per attempt on the shared fake_llm fixture).

eVi's home lives under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evi import evals
from evi.config import Config
from evi.evals import EvalCase, EvalSuite
from evi.llm.agent import Agent
from tests.conftest import llm_text

HW = {"gpu": "none", "vram_gb": None, "cpu": "test", "ram_gb": None, "os": "test"}


@pytest.fixture
def home(monkeypatch, tmp_path):
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    return evi_home


def _scripted(fake_llm, answers: list[str]):
    """make_agent whose n-th run answers answers[n] (Agent.chat on fake_llm)."""
    calls = iter(answers)

    def make(case, workspace):
        return Agent(client=fake_llm([llm_text(next(calls))]), config=Config.load(), tools=[])

    return make


def _ctx(make, tmp_path: Path) -> evals.EvalContext:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return evals.EvalContext(make_agent=make, backend="test", model="m", tmp_root=work)


def _suite(*cases: EvalCase) -> EvalSuite:
    return EvalSuite(name="s", cases=list(cases))


# ---- runs and pass_rate (acceptance criterion 3) ------------------------------------


@pytest.mark.parametrize("answers, passes, passed", [
    (["ok", "ok", "nope", "ok", "ok"], 4, True),
    (["ok", "nope", "ok", "nope", "ok"], 3, False),
])
def test_runs_5_pass_rate_0_8_passes_on_4_of_5_and_fails_on_3_of_5(
        home, tmp_path, fake_llm, answers, passes, passed):
    case = EvalCase(name="flaky", prompt="say ok", contains=["ok"], runs=5, pass_rate=0.8)
    report = evals.run_suite(_suite(case), _ctx(_scripted(fake_llm, answers), tmp_path),
                             hardware=HW)
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []
    row = report["cases"][0]
    assert row["runs"] == 5 and row["passes"] == passes
    assert row["pass_rate"] == passes / 5 and row["threshold"] == 0.8
    assert row["passed"] is passed and not row["skipped"]
    failing = [i for i, a in enumerate(answers, 1) if a != "ok"]
    assert row["failures"] == [f"run {i}: missing 'ok'" for i in failing]
    assert row["stop_reasons"] == {"done": 5} and row["mean_turns"] == 1.0
    assert row["mean_duration_s"] >= 0
    assert report["passed"] == int(passed) and report["ran"] == 1
    assert report["failed"] == int(not passed)
    assert evals.report_ok(report) is passed
    assert report["model"] == "m" and report["backend"] == "test" and report["n"] is None
    assert report["hardware"] == HW


def test_runs_overrides_each_cases_own(home, tmp_path, fake_llm):
    case = EvalCase(name="c", prompt="say ok", contains=["ok"], runs=5)
    report = evals.run_suite(_suite(case), _ctx(_scripted(fake_llm, ["ok"] * 2), tmp_path),
                             runs=2, hardware=HW)
    assert report["n"] == 2 and report["cases"][0]["runs"] == 2
    assert report["cases"][0]["passed"]


# ---- expected_fail and skipping ------------------------------------------------------


def test_expected_fail_reports_but_never_fails_the_suite(home, tmp_path, fake_llm):
    suite = _suite(
        EvalCase(name="known-gap", prompt="p", contains=["fixed"], expected_fail=True),
        EvalCase(name="xpass", prompt="p", contains=["ok"], expected_fail=True),
        EvalCase(name="fine", prompt="p", contains=["ok"]),
    )
    report = evals.run_suite(suite, _ctx(_scripted(fake_llm, ["broken", "ok", "ok"]), tmp_path),
                             hardware=HW)
    by = {r["name"]: r for r in report["cases"]}
    assert not by["known-gap"]["passed"] and by["known-gap"]["expected_fail"]
    assert by["xpass"]["passed"] and by["xpass"]["expected_fail"]
    assert report["expected_failures"] == 1 and report["failed"] == 0
    assert evals.report_ok(report)


def test_a_case_without_a_fake_script_is_skipped_under_the_fake_backend(home, tmp_path):
    suite = _suite(EvalCase(name="needs-a-model", prompt="p", contains=["x"]),
                   EvalCase(name="judged", prompt="p", judge="nice", fake=[{"text": "hi"}]))
    ctx = evals.EvalContext(make_agent=evals.fake_agent_factory(), backend="fake", model="fake",
                            tmp_root=tmp_path)
    report = evals.run_suite(suite, ctx, hardware=HW)
    by = {r["name"]: r for r in report["cases"]}
    assert by["needs-a-model"]["skipped"] and "no fake script" in by["needs-a-model"]["skip_reason"]
    assert by["judged"]["skipped"] and "judge" in by["judged"]["skip_reason"]
    assert report["skipped"] == 2 and report["ran"] == 0 and evals.report_ok(report)
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []


# ---- baseline gate and compare --------------------------------------------------------


def _report(rates: dict[str, float | None], model: str = "m") -> dict:
    rows = []
    for name, rate in rates.items():
        row = evals.case_row(EvalCase(name=name, prompt="p"), [], skipped="x" if rate is None
                             else None)
        if rate is not None:
            row.update(runs=5, passes=round(rate * 5), pass_rate=rate, passed=rate >= 1.0)
        rows.append(row)
    report = {"schema_version": 1, "kind": evals.REPORT_KIND, "suite": "s", "model": model,
              "backend": "test", "n": 5, "evi_version": "0", "started_at": "2026-10-17T00:00:00Z",
              "finished_at": "2026-10-17T00:01:00Z", "duration_s": 60.0, "hardware": HW,
              "baseline": None, "cases": rows}
    return evals.summarize(report)


def test_apply_baseline_fails_a_case_below_baseline_minus_tolerance():
    report = _report({"a": 0.6, "b": 0.8, "c": 1.0})
    for row in report["cases"]:
        row["passed"] = True  # every case met its own threshold
    baseline = {"cases": {"a": 0.8, "b": 0.8, "c": 0.8}}
    evals.apply_baseline(report, baseline, tolerance=0.1, path="baseline.json")
    by = {r["name"]: r for r in report["cases"]}
    assert not by["a"]["passed"] and by["a"]["baseline"]["regressed"]
    assert "below the baseline 0.80 minus 0.10" in by["a"]["failures"][-1]
    assert by["b"]["passed"] and by["c"]["passed"]
    assert by["b"]["baseline"] == {"pass_rate": 0.8, "tolerance": 0.1, "regressed": False}
    assert report["failed"] == 1 and not evals.report_ok(report)
    assert report["baseline"] == {
        "path": "baseline.json", "tolerance": 0.1, "model": None, "backend": None,
        "regressed": ["a"], "missing": [], "excluded": [], "mismatch": None}
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []


def test_a_baseline_case_that_did_not_run_fails_the_gate():
    """Deleting, renaming or skipping a case the baseline holds must not get
    past the gate (compare() already counts it as a regression)."""
    baseline = {"cases": {"kept": 1.0, "skipped": 1.0, "gone": 1.0}}
    report = _report({"kept": 1.0, "skipped": None, "renamed": 1.0})
    for row in report["cases"]:
        row["passed"] = not row["skipped"]
    assert evals.report_ok(report)  # on its own the run is green
    evals.apply_baseline(report, baseline, tolerance=0.1)
    by = {r["name"]: r for r in report["cases"]}
    assert by["skipped"]["baseline"]["regressed"] and by["skipped"]["skipped"]
    assert "skipped (x), but the baseline holds it at 1.00" in by["skipped"]["failures"][-1]
    assert by["renamed"]["baseline"] is None  # new: nothing to hold it to
    assert report["baseline"]["regressed"] == ["skipped"]
    assert report["baseline"]["missing"] == ["gone"]
    assert not evals.report_ok(report)
    assert evals.compare(baseline, report)["regressed"] == ["skipped", "gone"]
    assert evals.schema_errors(report, evals.REPORT_SCHEMA) == []


def test_only_a_case_left_out_on_purpose_is_excused_from_the_baseline():
    baseline = {"cases": {"kept": 1.0, "tagged-out": 1.0, "gone": 1.0}}
    report = _report({"kept": 1.0})
    report["cases"][0]["passed"] = True
    evals.apply_baseline(report, baseline, excluded={"tagged-out", "not-in-baseline"})
    assert report["baseline"]["excluded"] == ["tagged-out"]
    assert report["baseline"]["missing"] == ["gone"] and not evals.report_ok(report)
    report = _report({"kept": 1.0})
    report["cases"][0]["passed"] = True
    evals.apply_baseline(report, baseline, excluded={"tagged-out", "gone"})
    assert evals.report_ok(report)


def test_an_expected_failure_that_fell_below_the_baseline_fails_the_gate():
    report = _report({"crlf": 0.0, "ok": 1.0})
    report["cases"][0]["expected_fail"] = True
    report["cases"][1]["passed"] = True
    evals.summarize(report)
    assert evals.report_ok(report) and report["expected_failures"] == 1
    evals.apply_baseline(report, {"cases": {"crlf": 1.0, "ok": 1.0}})
    assert report["cases"][0]["baseline"]["regressed"]
    assert not evals.report_ok(report)
    assert report["failed"] == 1 and report["expected_failures"] == 0
    # Held to what it reached (0.0), it is still only an expected failure.
    report = _report({"crlf": 0.0})
    report["cases"][0]["expected_fail"] = True
    evals.apply_baseline(report, {"cases": {"crlf": 0.0}})
    assert evals.report_ok(report) and report["expected_failures"] == 1


def test_apply_baseline_names_how_the_run_differs_from_the_baseline():
    report = _report({"a": 1.0}, model="other-model")
    report["cases"][0]["passed"] = True
    evals.apply_baseline(report, {"model": "ref", "backend": "test", "cases": {"a": 1.0}})
    assert report["baseline"]["mismatch"] == "model other-model (the baseline's is ref)"
    assert evals.report_ok(report)  # a warning, not a failure
    html = evals.render_html(report)
    assert "differs: model other-model" in html


def test_compare_gives_per_case_deltas_and_regressions():
    a = _report({"same": 0.8, "worse": 1.0, "better": 0.2, "dropped": 1.0, "was-skipped": None})
    b = _report({"same": 0.8, "worse": 0.6, "better": 0.6, "was-skipped": 1.0, "new": 1.0})
    result = evals.compare(a, b)
    status = {r["name"]: (r["status"], r["delta"]) for r in result["rows"]}
    assert status == {
        "same": ("same", 0.0), "worse": ("regressed", -0.4), "better": ("improved", 0.4),
        "dropped": ("missing", None), "was-skipped": ("skipped", None), "new": ("new", None),
    }
    assert result["regressed"] == ["worse", "dropped"]
    # Within the tolerance a drop is no regression; a baseline compares too.
    assert evals.compare(a, b, tolerance=0.5)["regressed"] == ["dropped"]
    assert evals.compare({"cases": {"same": 0.8}}, b)["regressed"] == []


# ---- reports on disk ------------------------------------------------------------------


def test_write_report_keeps_json_and_a_static_html_page(home, tmp_path):
    report = _report({"<script>alert(1)</script>": 0.4, "fine": 1.0}, model="qwen/7b:q4")
    report["cases"][0]["failures"] = ["run 1: <img src=x onerror=alert(1)>"]
    path, page = evals.write_report(report)
    assert path.parent == home / "evals" / "reports" / "s"
    assert path.name == "20261017T000100Z-qwen-7b-q4.json" and page.suffix == ".html"
    assert json.loads(path.read_text(encoding="utf-8")) == report
    html = page.read_text(encoding="utf-8")
    assert "<script" not in html.lower() and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "default-src 'none'" in html
    # The same second again: a second file, not an overwrite.
    second, _ = evals.write_report(report)
    assert second != path and second.exists()
    latest = evals.latest_reports()
    assert [(r["suite"], r["model"]) for r in latest] == [("s", "qwen/7b:q4")]


def test_write_report_refuses_a_report_off_its_schema(home):
    report = _report({"a": 1.0})
    del report["hardware"]
    with pytest.raises(evals.EvalError, match="schema"):
        evals.write_report(report)
    assert not (home / "evals" / "reports").exists()
