"""The context budget is checked before every request (tool schemas counted,
the estimate calibrated on the backend's prompt_tokens), an overflow error is
recovered by one compact-and-retry, and a prompt that fills the server's
window is flagged (roadmap item models-ops-token-budget-recovery)."""

from __future__ import annotations

from types import SimpleNamespace

import jsonschema

import evi.tools.fs  # noqa: F401  registers read_file
from evi import events
from evi.config import Config
from evi.llm.agent import (
    Agent,
    ContextCompacted,
    ContextTruncated,
    Done,
    Error,
    TextDelta,
    ToolResult,
)
from evi.tools.base import REGISTRY
from tests.conftest import llm_call, llm_text


def _agent(client, tmp_path, *, size: int = 32768) -> Agent:
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url = "openai_compat", "https://api.example/v1"
    cfg.llm.context_size, cfg.llm.compact_when_pct = size, 80
    agent = Agent(client=client, config=cfg, transcripts=None, unattended_policy="allow",
                  tools=[REGISTRY["read_file"]])
    agent.cwd = str(tmp_path)
    return agent


def _stub_compaction(agent: Agent, collapsed: int, requests: list | None = None) -> list[int]:
    """Replace the summary call (a real LLM round trip) with a stub; returns
    the number of requests made before each compaction."""
    calls: list[int] = []

    def compact(*_a, **_k):
        calls.append(len(requests) if requests is not None else 0)
        for m in agent.history:  # shrink it, as a real summary would
            if m.get("role") == "tool":
                m["content"] = "(summarised)"
        return collapsed

    agent.compact_history = compact
    return calls


class _Failing:
    """A client whose create() raises each queued exception, then streams a reply."""

    def __init__(self, *errors: Exception, reply: str = "answered") -> None:
        self.errors, self.reply, self.calls = list(errors), reply, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_kw):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return iter(llm_text(self.reply))


def _overflow() -> Exception:
    return RuntimeError("This model's maximum context length is 4096 tokens. However, you "
                        "requested 5120 tokens. (code: context_length_exceeded)")


def test_a_request_that_would_not_fit_is_compacted_first_mid_tool_loop(
    fake_llm, tmp_path, monkeypatch
):
    # The per-result cap (evi.tool_output) would shrink this one result first; this
    # test is about the budget check, so the cap is out of the way here.
    monkeypatch.setattr("evi.tool_output.cap_chars", lambda _window: 10**9)
    (tmp_path / "big.txt").write_text("word " * 1600, encoding="utf-8")  # ~2000 tokens
    client = fake_llm([llm_call(("read_file", {"path": "big.txt"})), llm_text("done")])
    agent = _agent(client, tmp_path, size=2000)
    requests = client.chat.completions.requests
    before = _stub_compaction(agent, 4, requests)
    evs = list(agent.chat("read it", max_turns=4))
    # Not before the first request (it fit); before the second, after the big result.
    assert before == [1]
    kinds = [type(e).__name__ for e in evs if isinstance(e, (ToolResult, ContextCompacted, Done))]
    assert kinds == ["ToolResult", "ContextCompacted", "Done"]
    assert next(e for e in evs if isinstance(e, ContextCompacted)) == ContextCompacted("budget", 4)


def test_the_tool_schemas_count_toward_the_budget(fake_llm, tmp_path):
    agent = _agent(fake_llm([]), tmp_path)
    schemas = [t.openai_schema() for t in agent.tools.values()]
    assert agent._prompt_estimate(schemas) > agent._prompt_estimate(None) > 0


def test_the_estimate_follows_the_backends_own_count(fake_llm, tmp_path):
    agent = _agent(fake_llm([]), tmp_path)
    plain = agent._prompt_estimate(None)
    agent._calibrate(reported=200, estimated=100)  # the backend counted twice as many
    assert agent._prompt_estimate(None) == plain * 2
    agent._calibrate(reported=10_000, estimated=100)  # an odd report is capped
    assert agent._prompt_estimate(None) == plain * 3
    agent._calibrate(reported=0, estimated=100)  # no count: the ratio stays
    assert agent._prompt_estimate(None) == plain * 3


def test_an_overflow_error_is_recovered_by_one_compact_and_retry(tmp_path):
    client = _Failing(_overflow())
    agent = _agent(client, tmp_path)
    _stub_compaction(agent, 3)
    evs = list(agent.chat("go", max_turns=4))
    assert ContextCompacted("overflow", 3) in evs
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "answered"
    assert evs[-1] == Done("stop") and client.calls == 2


def test_a_second_overflow_is_an_error_not_another_retry(tmp_path):
    client = _Failing(_overflow(), _overflow())
    agent = _agent(client, tmp_path)
    compactions = _stub_compaction(agent, 3)
    evs = list(agent.chat("go", max_turns=6))
    assert len(compactions) == 1 and client.calls == 2
    assert isinstance(evs[-1], Error) and "maximum context length" in evs[-1].message


def test_an_overflow_with_nothing_to_compact_or_another_error_fails_at_once(tmp_path):
    client = _Failing(_overflow())
    agent = _agent(client, tmp_path)
    _stub_compaction(agent, 0)
    evs = list(agent.chat("go", max_turns=4))
    assert isinstance(evs[-1], Error) and client.calls == 1
    client = _Failing(RuntimeError("connection refused"))
    agent = _agent(client, tmp_path)
    compactions = _stub_compaction(agent, 3)
    evs = list(agent.chat("go", max_turns=4))
    assert isinstance(evs[-1], Error) and compactions == [] and client.calls == 1


def _usage_client(prompt_tokens: int):
    text = SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop",
        logprobs=None)])
    usage = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt_tokens,
                                                  completion_tokens=1, total_tokens=0),
                            choices=[])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kw: iter([text, usage]))))


def test_a_prompt_that_fills_the_servers_window_is_flagged(tmp_path, monkeypatch):
    agent = _agent(_usage_client(4096), tmp_path)
    monkeypatch.setattr(agent, "_known_window", lambda: 4096)
    evs = list(agent.chat("go", max_turns=2))
    assert ContextTruncated(prompt_tokens=4096, window=4096) in evs
    agent = _agent(_usage_client(1200), tmp_path)
    monkeypatch.setattr(agent, "_known_window", lambda: 4096)
    assert not any(isinstance(e, ContextTruncated) for e in agent.chat("go", max_turns=2))


def test_the_new_events_match_the_event_schema():
    for event in (ContextCompacted("budget", 4), ContextCompacted("overflow", 1),
                  ContextTruncated(prompt_tokens=4096, window=4096)):
        payload = {"schema_version": events.SCHEMA_VERSION, "seq": 1,
                   "ts": "2026-10-23T00:00:00Z", **events.event_payload(event)}
        jsonschema.Draft202012Validator(events.SCHEMA).validate(payload)


def test_the_request_path_never_asks_the_server(tmp_path, monkeypatch):
    # A probe of a slow or absent server mustn't hold up a request (or a cancel).
    order: list[str] = []
    client = _Failing()
    real_create = client.create

    def create(**kw):
        order.append("request")
        return real_create(**kw)

    client.chat.completions.create = create
    agent = _agent(client, tmp_path)
    monkeypatch.setattr(agent, "_server_window", lambda: order.append("probe") or None)
    list(agent.chat("go", max_turns=2))
    assert order[0] == "request"
