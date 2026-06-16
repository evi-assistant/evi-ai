"""Web API for the ultracode panel — POST /api/dispatch/ultracode."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.ultracode import UltraResult, UltraStage  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_run_returns_answer_and_stages(client, monkeypatch):
    captured = {}

    def fake_run(task, *, run_one, cfg=None, on_stage=None):
        captured["task"] = task
        captured["cfg"] = cfg
        return UltraResult(
            task=task, answer="the final answer",
            stages=[UltraStage("decompose", "plan", "p"),
                    UltraStage("solve", "direct", "s"),
                    UltraStage("synthesize", "final", "the final answer")],
            config=cfg,
        )

    # avoid the model: stub the pipeline (the endpoint still builds the factory)
    monkeypatch.setattr("evi.ultracode.run_ultracode", fake_run)
    r = client.post("/api/dispatch/ultracode", json={"task": "do a thing", "breadth": 2, "rounds": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["answer"] == "the final answer"
    assert [s["name"] for s in body["stages"]] == ["decompose", "solve", "synthesize"]
    # overrides reached the config the endpoint passed to run_ultracode
    assert captured["cfg"].breadth == 2 and captured["cfg"].rounds == 0
    assert captured["task"] == "do a thing"


def test_requires_task(client):
    assert client.post("/api/dispatch/ultracode", json={}).status_code == 400
    assert client.post("/api/dispatch/ultracode", json={"task": "  "}).status_code == 400
