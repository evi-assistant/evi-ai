"""Tests for routines / webhook triggers (Phase 73)."""

from __future__ import annotations

import pytest

from evi import recipes, routines


def test_add_list_get_remove(tmp_path):
    r = routines.add("standup", "morning", root=tmp_path)
    assert r.token and r.recipe == "morning" and r.enabled
    assert [x.name for x in routines.load(root=tmp_path)] == ["standup"]
    assert routines.get("standup", root=tmp_path).recipe == "morning"
    assert routines.get_by_token(r.token, root=tmp_path).name == "standup"
    assert routines.get_by_token("wrong", root=tmp_path) is None
    assert routines.remove("standup", root=tmp_path) is True
    assert routines.load(root=tmp_path) == []


def test_unique_tokens(tmp_path):
    a = routines.add("a", "r", root=tmp_path)
    b = routines.add("b", "r", root=tmp_path)
    assert a.token != b.token


def test_add_no_overwrite(tmp_path):
    routines.add("dupe", "r", root=tmp_path)
    with pytest.raises(routines.RoutineError):
        routines.add("dupe", "r", root=tmp_path)
    routines.add("dupe", "r2", root=tmp_path, overwrite=True)
    assert routines.get("dupe", root=tmp_path).recipe == "r2"


def test_run_recipe_headless_with_fake_agent(tmp_path):
    recipes.create_recipe("demo", root=tmp_path)  # template: 2 steps
    recipe = recipes.load_recipe("demo", root=tmp_path)

    class _FakeAgent:
        def chat(self, prompt, max_turns=12):
            from evi.llm.agent import Done, TextDelta
            yield TextDelta(f"answer to: {prompt[:10]}")
            yield Done("stop")

    results = recipes.run_recipe_headless(_FakeAgent(), recipe)
    assert len(results) == len(recipe.steps)
    assert all(r["text"].startswith("answer to:") for r in results)
    assert all(r["error"] is None for r in results)


def test_webhook_endpoint_runs_and_guards_token(monkeypatch, tmp_path):
    # Point routines + recipes at a temp home; stub the heavy recipe runner so
    # the test exercises the endpoint wiring (token auth, lookup, response)
    # without a real LLM call.
    import evi.config as cfg
    import evi.recipes as recipes_mod

    monkeypatch.setattr(cfg, "HOME", tmp_path)
    recipes.create_recipe("demo")  # root defaults to cfg.HOME (= tmp_path)
    r = routines.add("hook", "demo")
    monkeypatch.setattr(
        recipes_mod, "run_recipe_headless",
        lambda agent, recipe: [{"label": "s", "prompt": "p", "text": "ok", "error": None}],
    )

    from fastapi.testclient import TestClient

    from evi.apps.web.server import create_app

    client = TestClient(create_app())
    # bad token → 404, and reachable without an auth token (capability URL)
    assert client.post("/api/routine/nope").status_code == 404
    resp = client.post(f"/api/routine/{r.token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["routine"] == "hook"
    assert body["results"][0]["text"] == "ok"
