"""Tests for the model fallback chain (llm.fallback_models)."""

from __future__ import annotations

from evi.llm.agent import Agent, Error, RouteInfo


class _EmptyStream:
    def __iter__(self):
        return iter(())  # no chunks -> turn ends immediately


class _ScriptedCompletions:
    """create() raises for models in `fail`, else returns an empty stream."""

    def __init__(self, fail: set[str]) -> None:
        self.fail = fail
        self.models_tried: list[str] = []

    def create(self, **kwargs):
        model = kwargs["model"]
        self.models_tried.append(model)
        if model in self.fail:
            raise ConnectionError(f"{model} is down")
        return _EmptyStream()


class _ScriptedClient:
    def __init__(self, fail: set[str]) -> None:
        self.chat = type("_Chat", (), {"completions": _ScriptedCompletions(fail)})()


def _make_config(tmp_path, monkeypatch, *, model, fallbacks):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    from evi.config import Config

    cfg = Config.load()
    cfg.llm.model = model
    cfg.llm.fallback_models = fallbacks
    return cfg


def test_falls_back_to_next_model(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="primary", fallbacks=["backup"])
    client = _ScriptedClient(fail={"primary"})
    agent = Agent(client=client, config=cfg, tools=[])

    events = list(agent.chat("hi"))
    # The primary failed, then backup succeeded.
    assert client.chat.completions.models_tried == ["primary", "backup"]
    # A RouteInfo(route="fallback") announced the switch; no Error surfaced.
    routes = [e for e in events if isinstance(e, RouteInfo) and e.route == "fallback"]
    assert routes and routes[0].model == "backup"
    assert not any(isinstance(e, Error) for e in events)


def test_no_fallback_when_primary_works(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="primary", fallbacks=["backup"])
    client = _ScriptedClient(fail=set())
    agent = Agent(client=client, config=cfg, tools=[])

    events = list(agent.chat("hi"))
    assert client.chat.completions.models_tried == ["primary"]
    assert not any(isinstance(e, RouteInfo) and e.route == "fallback" for e in events)


def test_all_models_fail_yields_error(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="primary", fallbacks=["backup"])
    client = _ScriptedClient(fail={"primary", "backup"})
    agent = Agent(client=client, config=cfg, tools=[])

    events = list(agent.chat("hi"))
    assert client.chat.completions.models_tried == ["primary", "backup"]
    errors = [e for e in events if isinstance(e, Error)]
    assert errors and "failed" in errors[0].message.lower()
