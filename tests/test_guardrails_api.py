"""Web API for the guardrails editor — GET summary + POST validate/save."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", tmp_path / "guardrails.toml")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_get_empty(client):
    g = client.get("/api/guardrails").json()
    assert g["raw"] == "" and g["enabled"] is False
    assert g["summary"] == {"regex": 0, "judge": 0, "classifier": 0}


def test_post_saves_and_get_reflects(client):
    toml = (
        'enabled = true\n'
        '[[rule]]\nname = "k"\npattern = "secret"\naction = "block"\n'
        '[[judge]]\nname = "j"\npolicy = "no self-harm"\n'
    )
    r = client.post("/api/guardrails", json={"raw": toml})
    assert r.status_code == 200
    assert r.json()["summary"] == {"regex": 1, "judge": 1, "classifier": 0}
    # GET now reflects the saved file
    g = client.get("/api/guardrails").json()
    assert g["enabled"] is True and "secret" in g["raw"]
    assert [x["name"] for x in g["rules"]] == ["k"]


def test_post_invalid_is_400(client):
    r = client.post("/api/guardrails", json={"raw": '[[rule]]\nname="x"\npattern="([bad"\n'})
    assert r.status_code == 400


def test_post_requires_raw(client):
    assert client.post("/api/guardrails", json={}).status_code == 400
