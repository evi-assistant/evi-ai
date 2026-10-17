"""E2E: the Evals panel shows per-model pass rates per suite (loop-eval-gates).

`evi eval run` keeps each report under <eVi home>/evals/reports/<suite>/;
GET /api/evals returns the newest one per (suite, model) and Settings → Evals
draws them as a table per suite: a column per model, the suite's pass rate
on top, each case's below. Two reports of two models are seeded in the
server's EVI_HOME (the session's evi_home, a pytest tmp dir); the panel must
show both models' pass rates. Report text goes in through textContent only:
a case named like markup stays text. A column is a model on one backend,
named under the model: a fake run labelled with a real model's id gets its
own column and never replaces that model's real result.

The Run button (polish-evals-engine): POST /api/evals/run runs the suite on
the engine `evi eval run` uses, each case in a throwaway workspace on the
configured model (here the harness's fake OpenAI-compatible backend); a
spinner shows until the run is done, then each case is marked (pass, fail,
skipped with the reason) with the report's text as text, the totals name
the model, and the new report joins the per-model results. The button sends
the suite's file name (GET /api/evals `file`), which its TOML `name` need
not match; a user self.toml is shown shadowed and can't be run; a run that
errors leaves no verdict of the run before.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

SUITE = "qualification-e2e"
HW = {"gpu": "none", "vram_gb": None, "cpu": "test", "ram_gb": None, "os": "test"}


def _report(model: str, stamp: str, outcomes: dict[str, tuple[int, int]],
            backend: str = "fake") -> dict:
    rows = []
    for name, (passes, runs) in outcomes.items():
        rate = passes / runs
        rows.append({
            "name": name, "runs": runs, "passes": passes, "pass_rate": rate, "threshold": 0.8,
            "passed": rate >= 0.8, "skipped": False, "skip_reason": None,
            "expected_fail": False, "failures": [], "mean_turns": 2.0,
            "mean_duration_s": 1.0, "stop_reasons": {"done": runs}, "tags": [],
            "baseline": None, "verify_skipped": 0,
        })
    passed = sum(1 for r in rows if r["passed"])
    return {
        "schema_version": 1, "kind": "eval-report", "suite": SUITE, "model": model,
        "backend": backend, "n": 5, "evi_version": "test", "started_at": stamp,
        "finished_at": stamp, "duration_s": 10.0, "hardware": HW, "baseline": None,
        "total": len(rows), "ran": len(rows), "passed": passed, "failed": len(rows) - passed,
        "expected_failures": 0, "skipped": 0, "pass_rate": passed / len(rows), "cases": rows,
    }


def test_the_evals_panel_shows_each_models_pass_rates(page: Page, evi_base_url: str,
                                                      evi_home):
    folder = evi_home / "evals" / "reports" / SUITE
    folder.mkdir(parents=True, exist_ok=True)
    reports = {
        "20261017T100000Z-model-alpha.json": _report(
            "model-alpha", "2026-10-17T10:00:00Z",
            {"read-then-edit": (5, 5), "search": (4, 5), "<b>bold</b>": (5, 5),
             "apply-patch": (5, 5), "ask": (1, 5)}, backend="ollama"),
        "20261017T110000Z-model-beta.json": _report(
            "model-beta", "2026-10-17T11:00:00Z",
            {"read-then-edit": (2, 5), "search": (5, 5), "<b>bold</b>": (0, 5),
             "apply-patch": (1, 5), "ask": (5, 5)}),
        # model-alpha again, later, on the fake backend (scripts, every case
        # passing): a column of its own, beside the real one.
        "20261017T120000Z-model-alpha.json": _report(
            "model-alpha", "2026-10-17T12:00:00Z",
            {"read-then-edit": (1, 1), "search": (1, 1), "<b>bold</b>": (1, 1),
             "apply-patch": (1, 1), "ask": (1, 1)}, backend="fake"),
    }
    for name, doc in reports.items():
        (folder / name).write_text(json.dumps(doc), encoding="utf-8")

    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('evals')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    table = page.locator(f'.eval-results[data-suite="{SUITE}"]')
    expect(table).to_be_visible(timeout=10_000)
    alpha = '[data-model="model-alpha"][data-backend="ollama"]'
    alpha_fake = '[data-model="model-alpha"][data-backend="fake"]'
    beta = '[data-model="model-beta"]'
    expect(table.locator(f"th{alpha} .eval-results-model")).to_have_text("model-alpha")
    expect(table.locator(f"th{alpha} .eval-results-backend")).to_have_text("ollama")
    expect(table.locator(f"th{alpha_fake} .eval-results-backend")).to_have_text("fake")
    expect(table.locator(f"th{beta} .eval-results-model")).to_have_text("model-beta")
    expect(table.locator("th[data-model]")).to_have_count(3)
    total = table.locator("tr.eval-results-total")
    # model-alpha: 4 of 5 cases passed (ask is 20%); model-beta: 2 of 5; the
    # fake run of model-alpha doesn't replace its real 80%.
    expect(total.locator(f"td{alpha}")).to_have_text("80% (4/5)")
    expect(total.locator(f"td{alpha_fake}")).to_have_text("100% (5/5)")
    expect(total.locator(f"td{beta}")).to_have_text("40% (2/5)")
    search = table.locator('tr[data-case="search"]')
    expect(search.locator(f"td{alpha}")).to_have_text("80%")
    expect(search.locator(f"td{beta}")).to_have_text("100%")
    expect(table.locator(f'tr[data-case="ask"] td{alpha}')).to_have_class("bad")
    # A case name that looks like markup is drawn as text, not as an element.
    expect(table.locator('tr[data-case="<b>bold</b>"] td').first).to_have_text("<b>bold</b>")
    expect(table.locator("b")).to_have_count(0)


RUN_SUITE = "run-e2e"


def test_the_run_button_runs_the_suite_and_renders_the_report(page: Page, evi_base_url: str,
                                                               evi_home, fake_llm):
    # The fake backend answers every case with its canned greeting; the first
    # case holds its model call until the test releases it, so the spinner
    # can be seen. A repo case is skipped: the server's repository is not
    # trusted in this eVi home.
    suite = (
        f'name = "{RUN_SUITE}"\n'
        f'[[case]]\nname = "greets"\nprompt = "{fake_llm.HOLD} hi"\ncontains = ["Hello"]\n'
        '[[case]]\nname = "markup"\nprompt = "hi"\n'
        'contains = ["<img src=x onerror=alert(1)>"]\n'
        '[[case]]\nname = "untouched"\nprompt = "hi there"\nfiles = { "a.txt" = "x" }\n'
        'diff_touches_only = []\ncontains = ["Hello"]\n'
        '[[case]]\nname = "repo"\nprompt = "look"\nworkspace = "repo"\ncontains = ["Hello"]\n'
    )
    (evi_home / "evals" / f"{RUN_SUITE}.toml").write_text(suite, encoding="utf-8")

    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('evals')")
    run = page.locator(f'button[data-run="{RUN_SUITE}"]')
    expect(run).to_be_visible(timeout=10_000)
    card = page.locator(".eval-suite", has=run)
    run.click()
    expect(card.locator(".eval-status .eval-spin")).to_be_visible()
    expect(run).to_be_disabled()
    assert fake_llm.held.wait(60), "the first case never reached the model"
    fake_llm.release.set()

    status = card.locator(".eval-status")
    expect(status).to_have_text("2/3 passed (67%), 1 skipped — fake (openai_compat)",
                                timeout=60_000)
    expect(card.locator(".eval-spin")).to_have_count(0)
    expect(run).to_be_enabled()

    def case(name: str):
        return card.locator(f'.eval-case[data-case="{name}"]')

    expect(case("greets")).to_have_attribute("data-verdict", "pass")
    expect(case("greets").locator(".eval-mark")).to_have_text("✓")
    expect(case("untouched")).to_have_attribute("data-verdict", "pass")
    expect(case("markup")).to_have_attribute("data-verdict", "fail")
    expect(case("markup").locator(".eval-mark")).to_have_text("✗")
    # The failure quotes the assertion: drawn as text, never as an element.
    expect(case("markup").locator(".eval-fail")).to_contain_text(
        "missing '<img src=x onerror=alert(1)>'")
    expect(card.locator("img")).to_have_count(0)
    expect(case("repo")).to_have_attribute("data-verdict", "skip")
    expect(case("repo").locator(".eval-fail")).to_contain_text("not a trusted folder")

    # Kept as `evi eval run` keeps it, and shown with the per-model results.
    reports = sorted((evi_home / "evals" / "reports" / RUN_SUITE).glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert (report["kind"], report["backend"], report["model"]) == (
        "eval-report", "openai_compat", "fake")
    table = page.locator(f'.eval-results[data-suite="{RUN_SUITE}"]')
    expect(table.locator('tr.eval-results-total td[data-model="fake"]')).to_have_text(
        "67% (2/3)")


def test_the_run_button_runs_a_suite_by_its_file_and_clears_old_verdicts(page: Page,
                                                                          evi_base_url: str,
                                                                          evi_home):
    """A suite's TOML `name` need not match its file name: the card shows the
    name and its Run button sends the file name (as `evi eval run` takes
    it), so it runs that suite. A user self.toml is shown shadowed, its Run
    button disabled. A second run that errors leaves no verdict of the
    first run behind."""
    folder = evi_home / "evals"
    by_file = folder / "run-by-file.toml"
    shadowed = folder / "self.toml"
    by_file.write_text('name = "Named otherwise"\n[[case]]\nname = "greets"\nprompt = "hi"\n'
                       'contains = ["Hello"]\n', encoding="utf-8")
    shadowed.write_text('[[case]]\nname = "mine"\nprompt = "hi"\ncontains = ["Hello"]\n',
                        encoding="utf-8")
    try:
        page.goto(evi_base_url)
        page.evaluate("window.eviUI.openSettings('evals')")
        run = page.locator('button[data-run="run-by-file"]')
        expect(run).to_be_visible(timeout=10_000)
        card = page.locator(".eval-suite", has=run)
        expect(card.locator("b").first).to_have_text("Named otherwise")
        mine = page.locator('button[data-run="self"]')
        expect(mine).to_be_disabled()
        expect(page.locator(".eval-suite", has=mine).locator(".eval-shadowed")).to_contain_text(
            "Shadowed")

        greets = card.locator('.eval-case[data-case="greets"]')
        run.click()
        expect(card.locator(".eval-status")).to_have_text(
            "1/1 passed (100%) — fake (openai_compat)", timeout=60_000)
        expect(greets).to_have_attribute("data-verdict", "pass")

        page.route("**/api/evals/run", lambda route: route.fulfill(
            status=409, json={"detail": "the backend went away"}))
        run.click()
        expect(card.locator(".eval-status")).to_have_text("Error: the backend went away")
        expect(run).to_be_enabled()
        expect(greets.locator(".eval-mark")).to_have_text("○")
        assert greets.get_attribute("data-verdict") is None
    finally:
        by_file.unlink(missing_ok=True)
        shadowed.unlink(missing_ok=True)
