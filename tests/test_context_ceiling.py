"""The session's context ceiling is [llm] context_size, or the smaller window
the server reports for the model (Agent.context_ceiling ->
Backend.context_window), asked at most once a minute."""

from __future__ import annotations

import httpx
import pytest

import evi.llm.agent as agent_mod
from evi.config import Config
from evi.llm.agent import Agent
from tests.test_backends import _patch_httpx


def _agent(backend: str = "llamacpp", url: str = "http://h:8080/v1", size: int = 32768) -> Agent:
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.context_size = backend, url, size
    cfg.llm.model = "m"
    return Agent(client=None, config=cfg, tools=[], transcripts=None)


def _props(n_ctx: int, seen: list[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": n_ctx}})
    return handler


def test_a_server_running_a_smaller_window_sets_the_ceiling(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []
    _patch_httpx(monkeypatch, _props(8192, seen))
    agent = _agent(size=32768)
    assert agent.context_ceiling() == 8192
    assert agent.token_usage()[1] == 8192
    assert seen == ["/props"]  # asked once: the second call used the cached answer


def test_the_configured_size_stands_when_it_is_smaller_or_the_server_is_silent(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[str] = []
    _patch_httpx(monkeypatch, _props(65536, seen))
    assert _agent(size=16384).context_ceiling() == 16384
    # An online backend doesn't report one: no request, the config decides.
    assert _agent(backend="openai_compat", url="https://api.example/v1").context_ceiling() == 32768
    _patch_httpx(monkeypatch, lambda req: httpx.Response(500))
    assert _agent(size=16384).context_ceiling() == 16384
    # With no context_size set, the server's window is the ceiling.
    _patch_httpx(monkeypatch, _props(4096, seen))
    assert _agent(size=0).context_ceiling() == 4096


def test_the_probe_is_asked_again_after_a_minute_or_for_another_model(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[str] = []
    _patch_httpx(monkeypatch, _props(8192, seen))
    clock = [1000.0]
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock[0])
    agent = _agent()
    agent.context_ceiling()
    clock[0] += 30
    agent.context_ceiling()
    assert len(seen) == 1
    clock[0] += 31
    agent.context_ceiling()
    assert len(seen) == 2
    agent.config.llm.model = "other"
    agent.context_ceiling()
    assert len(seen) == 3


def test_a_probe_that_raises_leaves_the_configured_size(monkeypatch: pytest.MonkeyPatch):
    from evi.backends.llamacpp import LlamaCppBackend

    def boom(self, model_id):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(LlamaCppBackend, "context_window", boom)
    assert _agent(size=16384).context_ceiling() == 16384


def test_the_probe_never_scans_ports(monkeypatch: pytest.MonkeyPatch):
    import evi.portprobe as pp

    def scan(*a, **k):
        raise AssertionError("the probe scanned ports")

    monkeypatch.setattr(pp, "discover_llamacpp_url", scan)
    monkeypatch.setattr(pp, "is_openai_server", scan)
    seen: list[str] = []
    _patch_httpx(monkeypatch, _props(8192, seen))
    assert _agent().context_ceiling() == 8192


def test_autocompaction_uses_the_reported_window(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []
    _patch_httpx(monkeypatch, _props(1000, seen))
    agent = _agent(size=32768)
    agent.config.llm.compact_after_messages = 0
    agent.config.llm.compact_when_pct = 80
    agent.history.append({"role": "user", "content": "x" * 4000})  # ~1000 tokens
    compacted: list[bool] = []
    monkeypatch.setattr(agent, "compact_history", lambda *a, **k: compacted.append(True))
    agent._maybe_autocompact()
    assert compacted == [True]  # 1000 of a 1000-token window, not of the configured 32768
