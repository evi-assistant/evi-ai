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


class _BreaksMidStream:
    """A stream that raises once iterated, the way Ollama's native stream
    reports a malformed tool call after the model has thought."""

    def __init__(self, msg: str) -> None:
        self.msg = msg

    def __iter__(self):
        raise RuntimeError(self.msg)
        yield  # pragma: no cover


class _MidStreamMalformedThenOk:
    def __init__(self, bad: int, msg: str = "Ollama: error parsing tool call: raw='{\"patch\"'") -> None:
        self.bad, self.calls, self.msg = bad, 0, msg
        self.chat = type("_Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        return _BreaksMidStream(self.msg) if self.calls <= self.bad else _EmptyStream()


def test_a_malformed_call_failing_mid_stream_is_asked_for_again(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="m", fallbacks=[])
    client = _MidStreamMalformedThenOk(bad=2)
    events = list(Agent(client=client, config=cfg, tools=[]).chat("hi"))
    assert client.calls == 3 and not any(isinstance(e, Error) for e in events)


def test_mid_stream_retries_give_up_and_other_breaks_are_not_retried(tmp_path, monkeypatch):
    from evi.llm.agent import MALFORMED_CALL_RETRIES

    cfg = _make_config(tmp_path, monkeypatch, model="m", fallbacks=[])
    client = _MidStreamMalformedThenOk(bad=99)
    events = list(Agent(client=client, config=cfg, tools=[]).chat("hi"))
    assert client.calls == MALFORMED_CALL_RETRIES + 1
    assert any(isinstance(e, Error) for e in events)

    client = _MidStreamMalformedThenOk(bad=99, msg="connection reset")
    list(Agent(client=client, config=cfg, tools=[]).chat("hi"))
    assert client.calls == 1


class _TextThenBreak:
    """One text chunk, then the malformed-call error: gpt-oss wrote a line
    before the call Ollama couldn't parse."""

    def __init__(self) -> None:
        from types import SimpleNamespace as NS

        self.chunk = NS(choices=[NS(delta=NS(content="Let me patch it.", tool_calls=None,
                                             reasoning=None, reasoning_content=None),
                                    finish_reason=None, logprobs=None)], usage=None)

    def __iter__(self):
        yield self.chunk
        raise RuntimeError("Ollama: error parsing tool call: raw='{\"patch\"'")


def test_a_malformed_call_after_some_text_is_asked_for_again(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path, monkeypatch, model="m", fallbacks=[])
    client = _MidStreamMalformedThenOk(bad=1)
    client.create = lambda **kw: (setattr(client, "calls", client.calls + 1)
                                  or (_TextThenBreak() if client.calls == 1 else _EmptyStream()))
    agent = Agent(client=client, config=cfg, tools=[])
    events = list(agent.chat("hi"))
    assert client.calls == 2 and not any(isinstance(e, Error) for e in events)
    assert not any("Let me patch it." in str(m.get("content")) for m in agent.history)
