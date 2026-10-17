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
