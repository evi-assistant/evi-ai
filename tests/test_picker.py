"""Tests for the model picker — API endpoints + agent plumbing for
reasoning_effort and fast_mode."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
import evi.config as config_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Agent, Done, Event, TextDelta  # noqa: E402


# ---- Agent plumbing -----------------------------------------------------


class _Delta:
    content = "ok"
    tool_calls = None


class _Choice:
    delta = _Delta()
    finish_reason = "stop"


class _Chunk:
    choices = [_Choice()]


class _CapturingCompletions:
    """Records each create() call's kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        return iter([_Chunk()])


def _make_agent(cfg: Config) -> tuple[Agent, _CapturingCompletions]:
    cc = _CapturingCompletions()
    client = type("C", (), {"chat": type("X", (), {"completions": cc})()})()
    return Agent(client=client, config=cfg, tools=[]), cc


def test_effort_default_medium_not_passed_through() -> None:
    """Medium = the implicit default; we don't pollute extra_body with it."""
    cfg = Config()
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert "extra_body" not in cc.calls[0]


def test_effort_high_lands_in_extra_body() -> None:
    # Effort is only forwarded to reasoning-capable models (see test below).
    cfg = Config()
    cfg.llm.model = "deepseek-r1:14b"
    cfg.llm.reasoning_effort = "high"
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0].get("extra_body") == {"reasoning_effort": "high"}


def test_effort_max() -> None:
    cfg = Config()
    cfg.llm.model = "deepseek-r1:14b"
    cfg.llm.reasoning_effort = "max"
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["extra_body"] == {"reasoning_effort": "max"}


def test_effort_not_sent_to_non_reasoning_model() -> None:
    # Regression: Ollama 400s a non-reasoning model on a thinking request, so we
    # must NOT forward reasoning_effort to e.g. qwen2.5.
    cfg = Config()
    cfg.llm.model = "qwen2.5:14b-instruct-q4_K_M"
    cfg.llm.reasoning_effort = "max"
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert "extra_body" not in cc.calls[0] or "reasoning_effort" not in cc.calls[0].get("extra_body", {})


def test_fast_mode_swaps_model_when_set() -> None:
    cfg = Config()
    cfg.llm.model = "big-model"
    cfg.llm.fast_model = "small-model"
    cfg.llm.fast_mode = True
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["model"] == "small-model"


def test_fast_mode_off_uses_primary_model() -> None:
    cfg = Config()
    cfg.llm.model = "big-model"
    cfg.llm.fast_model = "small-model"
    cfg.llm.fast_mode = False
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["model"] == "big-model"


def test_fast_mode_with_empty_fast_model_is_noop() -> None:
    """Defensive: fast_mode=True but no fast_model set must not blank out the model."""
    cfg = Config()
    cfg.llm.model = "primary"
    cfg.llm.fast_model = ""
    cfg.llm.fast_mode = True
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi"))
    assert cc.calls[0]["model"] == "primary"


# ---- API endpoints ------------------------------------------------------


class _FakeBackend:
    name = "fake"
    base_url = "http://x"
    def list_models(self):
        class _M:
            def __init__(self, mid): self.id = mid
        return [_M("alpha"), _M("beta"), _M("gamma")]


class _FakeAgent:
    """Minimal stand-in; the picker mutates config in place."""
    def __init__(self, *_, **__) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.auto_all = False
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None
    def chat(self, *_args, **_kwargs) -> Iterator[Event]:
        yield TextDelta("ok")
        yield Done(reason="stop")

    def reset(self): pass
    def set_goal(self, g): self.goal = g
    def clear_goal(self): self.goal = None
    def enable_plan_mode(self): self.plan_mode_once = True
    def enable_auto_all(self): self.auto_all = True
    def disable_auto_all(self): self.auto_all = False
    def token_usage(self): return (0, 0)
    def refresh_prompt(self): pass


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> TestClient:
    # Redirect ~/.evi to tmp so config.save() doesn't touch the real one.
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    import evi.sdk.builder as builder_mod
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "get_backend", lambda *_a, **_k: _FakeBackend())
    # The picker now aggregates models via the backend registry. Isolate its
    # file to tmp (empty → falls back to the single [llm] backend) and route its
    # per-backend model listing through the test-mocked get_backend.
    from evi.backends import registry as _reg
    monkeypatch.setattr(_reg, "BACKENDS_PATH", tmp_path / "backends.json")

    def _list_models_for(entry):
        try:
            return [m.id for m in server_mod.get_backend(entry).list_models()]
        except Exception:
            return []

    monkeypatch.setattr(_reg, "list_models_for", _list_models_for)
    app = server_mod.create_app()
    return TestClient(app)


def test_picker_get_returns_snapshot(client: TestClient) -> None:
    r = client.get("/api/model-picker")
    assert r.status_code == 200
    data = r.json()
    assert data["active"]  # comes from the default Config
    assert "alpha" in data["models"] and "beta" in data["models"]
    assert data["effort_levels"] == ["off", "low", "medium", "high", "max"]
    assert data["fast_mode"] is False
    # Capability flags per model + for the active model (UI chips).
    assert "capabilities" in data and "active_capabilities" in data
    assert set(data["active_capabilities"]) >= {"vision", "reasoning", "infill", "audio"}


def test_picker_capabilities_detect_per_model(client: TestClient, monkeypatch) -> None:
    # A VLM + a coder among the models → vision/infill flags light up.
    import evi.apps.web.server as server_mod

    class _B:
        name = "fake"
        base_url = "http://x"

        def list_models(self):
            return [type("M", (), {"id": x})() for x in
                    ("qwen2.5-vl:7b", "qwen2.5-coder:14b", "qwen2.5:7b")]

    monkeypatch.setattr(server_mod, "get_backend", lambda *_a, **_k: _B())
    caps = client.get("/api/model-picker").json()["capabilities"]
    assert caps["qwen2.5-vl:7b"]["vision"] is True
    assert caps["qwen2.5-coder:14b"]["infill"] is True
    assert caps["qwen2.5:7b"]["vision"] is False


def test_picker_get_includes_active_model_even_if_backend_missing(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Active should always appear in the model list so the UI never shows empty."""

    class _Empty:
        name = "fake"
        base_url = "http://x"
        def list_models(self):
            return []

    monkeypatch.setattr(server_mod, "get_backend", lambda *_a, **_k: _Empty())
    r = client.get("/api/model-picker")
    data = r.json()
    assert data["active"] in data["models"]


def test_picker_post_sets_model(client: TestClient) -> None:
    r = client.post("/api/model-picker", json={"model": "alpha"})
    assert r.status_code == 200
    assert r.json()["active"] == "alpha"
    r2 = client.get("/api/model-picker")
    assert r2.json()["active"] == "alpha"


def test_picker_post_sets_effort(client: TestClient) -> None:
    r = client.post("/api/model-picker", json={"effort": "high"})
    assert r.status_code == 200
    assert r.json()["effort"] == "high"


def test_picker_post_sets_effort_off(client: TestClient) -> None:
    r = client.post("/api/model-picker", json={"effort": "off"})
    assert r.status_code == 200
    assert r.json()["effort"] == "off"


def test_picker_post_rejects_bad_effort(client: TestClient) -> None:
    r = client.post("/api/model-picker", json={"effort": "extreme"})
    assert r.status_code == 400


def test_picker_post_toggles_fast_mode(client: TestClient) -> None:
    r = client.post(
        "/api/model-picker",
        json={"fast_mode": True, "fast_model": "small"},
    )
    data = r.json()
    assert data["fast_mode"] is True
    assert data["fast_model"] == "small"
    r2 = client.post("/api/model-picker", json={"fast_mode": False})
    assert r2.json()["fast_mode"] is False


def test_picker_propagates_to_live_sessions(client: TestClient) -> None:
    """Changes via the picker should land on existing in-memory agents too."""
    # Open a session first so the server has one in its `sessions` dict.
    client.post("/api/chat", json={"session_id": "s1", "message": "/help"})
    client.post("/api/model-picker", json={"model": "gamma", "effort": "low"})
    # Snapshot via the picker reflects the change.
    snap = client.get("/api/model-picker").json()
    assert snap["active"] == "gamma"
    assert snap["effort"] == "low"
