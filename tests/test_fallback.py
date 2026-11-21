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


class _MalformedThenOk:
    """create() fails `bad` times the way Ollama answers gpt-oss's malformed
    tool call, then returns an empty stream."""

    def __init__(self, bad: int) -> None:
        self.bad, self.calls = bad, 0
        self.chat = type("_Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.bad:
            raise RuntimeError("Ollama 500: error parsing tool call: raw='The patch...'")
        return _EmptyStream()


def test_a_malformed_tool_call_is_asked_for_again(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="m", fallbacks=[])
    client = _MalformedThenOk(bad=2)
    events = list(Agent(client=client, config=cfg, tools=[]).chat("hi"))
    assert client.calls == 3 and not any(isinstance(e, Error) for e in events)


def test_malformed_tool_calls_give_up_after_the_retries(tmp_path, monkeypatch):
    from evi.llm.agent import MALFORMED_CALL_RETRIES

    cfg = _make_config(tmp_path, monkeypatch, model="m", fallbacks=[])
    client = _MalformedThenOk(bad=99)
    events = list(Agent(client=client, config=cfg, tools=[]).chat("hi"))
    assert client.calls == MALFORMED_CALL_RETRIES + 1
    assert any(isinstance(e, Error) and "error parsing tool call" in e.message for e in events)


def test_other_failures_are_not_retried(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="primary", fallbacks=[])
    client = _ScriptedClient(fail={"primary"})
    list(Agent(client=client, config=cfg, tools=[]).chat("hi"))
    assert client.chat.completions.models_tried == ["primary"]
