"""Web API for the Routes & Recipes panel — routes CRUD + recipe browse/run."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.routing.ROUTES_PATH", tmp_path / "routes.json")
    return tmp_path


@pytest.fixture
def client(monkeypatch, home, tmp_path):
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


# ---- routes --------------------------------------------------------------


def test_routes_empty(client):
    assert client.get("/api/routes").json() == {"routes": []}


def test_routes_add_list_remove(client):
    r = client.post("/api/routes", json={
        "name": "code", "model": "qwen-coder",
        "keywords": "debug, refactor", "description": "coding tasks"})
    assert r.status_code == 200
    routes = client.get("/api/routes").json()["routes"]
    assert len(routes) == 1
    rt = routes[0]
    assert rt["name"] == "code" and rt["model"] == "qwen-coder"
    assert rt["match_keywords"] == ["debug", "refactor"]
    assert rt["description"] == "coding tasks"
    # remove
    assert client.post("/api/routes/remove", json={"name": "code"}).status_code == 200
    assert client.get("/api/routes").json()["routes"] == []


def test_routes_add_keywords_as_list(client):
    client.post("/api/routes", json={"name": "r", "model": "m", "keywords": ["a", "b"]})
    assert client.get("/api/routes").json()["routes"][0]["match_keywords"] == ["a", "b"]


def test_routes_overwrite_in_place(client):
    client.post("/api/routes", json={"name": "r", "model": "m1"})
    client.post("/api/routes", json={"name": "r", "model": "m2"})
    routes = client.get("/api/routes").json()["routes"]
    assert len(routes) == 1 and routes[0]["model"] == "m2"


def test_routes_add_requires_name_and_model(client):
    assert client.post("/api/routes", json={"name": "x"}).status_code == 400
    assert client.post("/api/routes", json={"model": "y"}).status_code == 400


def test_routes_remove_unknown_404(client):
    assert client.post("/api/routes/remove", json={"name": "ghost"}).status_code == 404


def test_routes_remove_requires_name(client):
    assert client.post("/api/routes/remove", json={}).status_code == 400


# ---- recipes -------------------------------------------------------------


def _seed_recipe(home, name, body):
    d = home / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body, encoding="utf-8")


def test_recipes_empty(client):
    assert client.get("/api/recipes").json() == {"recipes": []}


def test_recipes_list(client, home):
    _seed_recipe(home, "standup",
                 'name = "standup"\ndescription = "daily"\n'
                 '[[steps]]\nlabel = "summary"\nprompt = "Summarize my day"\n'
                 '[[steps]]\nprompt = "List blockers"\n')
    d = client.get("/api/recipes").json()["recipes"]
    assert len(d) == 1
    rc = d[0]
    assert rc["name"] == "standup" and rc["description"] == "daily"
    assert [s["label"] for s in rc["steps"]] == ["summary", ""]
    assert rc["steps"][0]["prompt"] == "Summarize my day"


def test_recipes_run_unknown_404(client):
    assert client.post("/api/recipes/run", json={"name": "nope"}).status_code == 404


def test_recipes_run_requires_name(client):
    assert client.post("/api/recipes/run", json={}).status_code == 400


def test_recipes_run_returns_steps(client, home, monkeypatch):
    _seed_recipe(home, "r",
                 'name = "r"\n[[steps]]\nlabel = "one"\nprompt = "p1"\n')
    canned = [{"label": "one", "prompt": "p1", "text": "done", "error": None}]
    # Avoid the model: stub the headless recipe runner.
    monkeypatch.setattr("evi.recipes.run_recipe_headless", lambda agent, recipe: canned)
    r = client.post("/api/recipes/run", json={"name": "r"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "r" and body["steps"] == canned


def test_routes_json_on_disk(client, home):
    client.post("/api/routes", json={"name": "r", "model": "m"})
    saved = json.loads((home / "routes.json").read_text(encoding="utf-8"))
    assert saved["routes"][0]["name"] == "r"
