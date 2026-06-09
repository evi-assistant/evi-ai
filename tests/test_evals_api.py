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
    assert client.get("/api/evals").json() == {"suites": []}


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
