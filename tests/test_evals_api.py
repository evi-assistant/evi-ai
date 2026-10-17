"""Web API for the evals panel — GET /api/evals (browse) + POST /api/evals/run."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


def _seed_suite(evals_dir, name, body):
    evals_dir.mkdir(parents=True, exist_ok=True)
    (evals_dir / f"{name}.toml").write_text(body, encoding="utf-8")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    return tmp_path


@pytest.fixture
def client(monkeypatch, home, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_list_empty(client):
    assert client.get("/api/evals").json() == {"suites": [], "reports": []}


def test_list_suites(client, home):
    _seed_suite(home / "evals", "math",
                'name = "math"\ndescription = "numbers"\n'
                '[[case]]\nname = "add"\nprompt = "2+2?"\ncontains = ["4"]\n'
                '[[case]]\nname = "tone"\nprompt = "hi"\njudge = "be friendly"\n')
    d = client.get("/api/evals").json()
    assert len(d["suites"]) == 1
    s = d["suites"][0]
    assert s["name"] == "math" and s["description"] == "numbers"
    assert [c["name"] for c in s["cases"]] == ["add", "tone"]
    # assertion summaries are surfaced for the browser
    assert any("contains 4" in c for c in s["cases"][0]["checks"])
    assert any("judge" in c for c in s["cases"][1]["checks"])


def test_run_unknown_404(client):
    assert client.post("/api/evals/run", json={"name": "nope"}).status_code == 404


def test_run_requires_name(client):
    assert client.post("/api/evals/run", json={}).status_code == 400


def test_run_returns_report(client, home, monkeypatch):
    _seed_suite(home / "evals", "s",
                'name = "s"\n[[case]]\nname = "a"\nprompt = "p"\ncontains = ["x"]\n')
    canned = {"name": "s", "total": 1, "passed": 1, "pass_rate": 1.0,
              "cases": [{"name": "a", "passed": True, "failures": [], "output": "x"}]}
    # Avoid invoking the model: stub the orchestrator the endpoint calls.
    monkeypatch.setattr("evi.evals.run_eval", lambda *a, **k: canned)
    r = client.post("/api/evals/run", json={"name": "s"})
    assert r.status_code == 200 and r.json() == canned


def test_list_shows_the_latest_report_per_suite_and_model(client, home):
    """GET /api/evals carries the newest report of each (suite, model): what
    the Evals panel's per-model pass rates show. A file that is no report is
    left out."""
    import json

    folder = home / "evals" / "reports" / "qualification"
    folder.mkdir(parents=True)

    def report(model: str, stamp: str, rate: float) -> dict:
        ok = rate == 1.0
        return {"schema_version": 1, "kind": "eval-report", "suite": "qualification",
                "model": model, "backend": "fake", "n": 1, "finished_at": stamp,
                "total": 1, "ran": 1, "passed": int(ok), "skipped": 0, "failed": int(not ok),
                "pass_rate": rate,
                "cases": [{"name": "c", "runs": 1, "passes": int(ok), "pass_rate": rate,
                           "passed": ok, "skipped": False, "expected_fail": False}]}

    for name, doc in (
        ("20261001T000000Z-model-a.json", report("model-a", "2026-10-01T00:00:00Z", 0.0)),
        ("20261002T000000Z-model-a.json", report("model-a", "2026-10-02T00:00:00Z", 1.0)),
        ("20261001T000000Z-model-b.json", report("model-b", "2026-10-01T00:00:00Z", 0.0)),
    ):
        (folder / name).write_text(json.dumps(doc), encoding="utf-8")
    (folder / "20261003T000000Z-junk.json").write_text("{not json", encoding="utf-8")
    reports = client.get("/api/evals").json()["reports"]
    assert [(r["suite"], r["model"], r["pass_rate"]) for r in reports] == [
        ("qualification", "model-a", 1.0), ("qualification", "model-b", 0.0)]
    assert reports[0]["cases"][0]["name"] == "c"
    assert reports[0]["file"] == "20261002T000000Z-model-a.json"


def test_a_fake_run_never_replaces_a_models_real_result(client, home):
    """`--backend fake --model M` (and `models recommend --verify --backend
    fake`) label a scripted run with a real model's id: the latest report is
    kept per backend too, so the real result stays visible beside it."""
    import json

    folder = home / "evals" / "reports" / "self"
    folder.mkdir(parents=True)
    for name, backend, rate, stamp in (
        ("20261001T000000Z-qwen.json", "ollama", 0.0, "2026-10-01T00:00:00Z"),
        ("20261002T000000Z-qwen.json", "fake", 1.0, "2026-10-02T00:00:00Z"),
    ):
        (folder / name).write_text(json.dumps({
            "kind": "eval-report", "suite": "self", "model": "qwen2.5:7b", "backend": backend,
            "finished_at": stamp, "total": 1, "ran": 1, "passed": int(rate), "pass_rate": rate,
            "cases": []}), encoding="utf-8")
    reports = client.get("/api/evals").json()["reports"]
    assert [(r["model"], r["backend"], r["pass_rate"]) for r in reports] == [
        ("qwen2.5:7b", "fake", 1.0), ("qwen2.5:7b", "ollama", 0.0)]
