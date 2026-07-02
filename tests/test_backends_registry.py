"""Tests for the multi-backend registry + its API endpoints."""

from __future__ import annotations


import pytest

from evi.backends import registry as R
from evi.config import Config


# ---- registry unit ------------------------------------------------------


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "backends.json"
    R.save_backends(
        [
            R.BackendEntry(name="openai", kind="openai_compat",
                           base_url="https://api.openai.com/v1", api_key="env:OPENAI_API_KEY"),
            R.BackendEntry(name="local", kind="ollama", base_url="http://localhost:11434/v1"),
        ],
        p,
    )
    got = R.load_backends(p)
    assert [e.name for e in got] == ["openai", "local"]
    assert got[0].backend == "openai_compat"  # duck-types LLMSettings.backend
    assert got[1].kind == "ollama"


def test_load_missing_and_malformed(tmp_path):
    assert R.load_backends(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert R.load_backends(bad) == []


def test_add_remove_get(tmp_path):
    p = tmp_path / "backends.json"
    assert R.add_backend(R.BackendEntry(name="a", kind="ollama"), p)
    assert not R.add_backend(R.BackendEntry(name="a", kind="ollama"), p)  # dup, no overwrite
    assert R.add_backend(R.BackendEntry(name="a", kind="lmstudio"), p, overwrite=True)
    assert R.get_entry("A", R.load_backends(p)).kind == "lmstudio"
    assert R.remove_backend("a", p)
    assert not R.remove_backend("ghost", p)


def test_from_preset_defaults_to_env_ref():
    e = R.from_preset("openai")
    assert e.kind == "openai_compat"
    assert e.base_url == "https://api.openai.com/v1"
    assert e.api_key == "env:OPENAI_API_KEY"
    # inline key overrides the env ref
    assert R.from_preset("xai", api_key="sk-abc").api_key == "sk-abc"
    assert R.from_preset("nonsuch") is None


def test_effective_backends_falls_back_to_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "BACKENDS_PATH", tmp_path / "backends.json")  # empty registry
    cfg = Config()
    cfg.llm.backend = "ollama"
    ents = R.effective_backends(cfg)
    assert len(ents) == 1 and ents[0].kind == "ollama"


def test_active_backend_name_matches_by_kind_and_url(tmp_path):
    cfg = Config()
    cfg.llm.backend = "openai_compat"
    cfg.llm.base_url = "https://api.x.ai/v1"
    entries = [
        R.BackendEntry(name="grok", kind="openai_compat", base_url="https://api.x.ai/v1"),
        R.BackendEntry(name="oai", kind="openai_compat", base_url="https://api.openai.com/v1"),
    ]
    assert R.active_backend_name(cfg, entries) == "grok"


def test_all_models_aggregates_and_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "BACKENDS_PATH", tmp_path / "backends.json")
    R.save_backends(
        [
            R.BackendEntry(name="oai", kind="openai_compat", base_url="https://api.openai.com/v1"),
            R.BackendEntry(name="local", kind="ollama", base_url="http://localhost:11434/v1"),
            R.BackendEntry(name="down", kind="openai_compat", base_url="http://127.0.0.1:1/v1"),
        ],
        R.BACKENDS_PATH,
    )
    canned = {"oai": ["gpt-4o", "gpt-4o-mini"], "local": ["qwen2.5:14b"], "down": []}
    monkeypatch.setattr(R, "list_models_for", lambda e: canned[e.name])
    groups = R.all_models(Config())
    by = {g["backend"]: g for g in groups}
    assert by["oai"]["models"] == ["gpt-4o", "gpt-4o-mini"] and by["oai"]["reachable"]
    assert by["local"]["models"] == ["qwen2.5:14b"]
    assert by["down"]["reachable"] is False and by["down"]["models"] == []


# ---- API endpoints ------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as config_mod

    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(R, "BACKENDS_PATH", tmp_path / "backends.json")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    # Avoid real network in the picker aggregation.
    monkeypatch.setattr(
        R, "list_models_for",
        lambda e: {"oai": ["gpt-4o"], "local": ["qwen2.5:14b"]}.get(e.name, []),
    )
    return TestClient(server_mod.create_app())


def test_backends_add_list_remove(client):
    # add from preset (key omitted → env ref)
    r = client.post("/api/backends", json={"preset": "openai", "name": "oai"})
    assert r.status_code == 200 and r.json()["name"] == "oai"
    # add a custom local one
    client.post("/api/backends", json={"name": "local", "kind": "ollama"})
    data = client.get("/api/backends").json()
    names = {b["name"]: b for b in data["backends"]}
    assert names["oai"]["key_is_env"] is True and names["oai"]["has_key"] is True
    assert names["local"]["kind"] == "ollama"
    assert any(p["name"] == "anthropic" for p in data["presets"])
    # remove
    assert client.request("DELETE", "/api/backends/oai").json()["ok"] is True
    assert "oai" not in {b["name"] for b in client.get("/api/backends").json()["backends"]}


def test_backends_add_custom_requires_name(client):
    assert client.post("/api/backends", json={"kind": "ollama"}).status_code == 400


def test_backends_add_bad_preset(client):
    assert client.post("/api/backends", json={"preset": "nonsuch"}).status_code == 400


def test_picker_get_aggregates_across_backends(client):
    client.post("/api/backends", json={"preset": "openai", "name": "oai"})
    client.post("/api/backends", json={"name": "local", "kind": "ollama"})
    data = client.get("/api/model-picker").json()
    assert "gpt-4o" in data["models"] and "qwen2.5:14b" in data["models"]
    # each model tagged with its source backend
    assert data["sources"]["gpt-4o"] == ["oai"]
    assert data["sources"]["qwen2.5:14b"] == ["local"]
    assert {g["backend"] for g in data["backends"]} == {"oai", "local"}


def test_picker_post_switches_backend(client):
    client.post("/api/backends", json={"preset": "openai", "name": "oai"})
    r = client.post("/api/model-picker", json={"backend": "oai", "model": "gpt-4o"})
    assert r.status_code == 200
    assert r.json()["active"] == "gpt-4o"
    # the active backend materialized into [llm]
    cfg = Config.load()
    assert cfg.llm.base_url == "https://api.openai.com/v1"
    assert cfg.llm.model == "gpt-4o"


def test_picker_post_unknown_backend_is_400(client):
    assert client.post("/api/model-picker", json={"backend": "ghost"}).status_code == 400
