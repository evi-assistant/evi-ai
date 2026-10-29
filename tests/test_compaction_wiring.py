"""compact_history's token-budgeted tail, chunked summaries and "compact up to
here" (evi.history.tail_within / pack_blocks, wired in Agent.compact_history)."""

from __future__ import annotations

from types import SimpleNamespace

from evi.config import Config
from evi.history import validate_history
from evi.llm.agent import Agent


class _Summarizer:
    """Answers each summary request with the next reply ("" once they run out)."""

    def __init__(self, *replies: str) -> None:
        self.replies, self.prompts = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.prompts.append(kw["messages"][1]["content"])
        text = self.replies.pop(0) if self.replies else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _agent(client, context_size: int = 32768, keep_pct: int = 30) -> Agent:
    cfg = Config()
    cfg.llm.context_size, cfg.llm.compact_keep_pct = context_size, keep_pct
    return Agent(client=client, config=cfg, tools=[], transcripts=None)


def _turns(agent: Agent, n: int, size: int = 20) -> None:
    for i in range(n):
        agent.history.append({"role": "user", "content": f"user {i} " + "u" * size})
        agent.history.append({"role": "assistant", "content": f"reply {i} " + "a" * size})


def _tool_turn(agent: Agent, output: str) -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    agent.history += [
        {"role": "user", "content": "read the log"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": output},
        {"role": "assistant", "content": "the log is long"},
    ]


def test_a_tail_over_the_budget_moves_past_a_big_tool_result():
    # 4000-token window, 30%: the 2500-token log can't stay verbatim
    agent = _agent(_Summarizer("S"), context_size=4000)
    _turns(agent, 6)
    _tool_turn(agent, "L" * 10_000)
    _turns(agent, 1)
    assert agent.compact_history(keep_recent=8) > 0
    assert not any(m.get("role") == "tool" for m in agent.history)
    assert agent.history[-1]["content"].startswith("reply 0 ")
    assert validate_history(agent.history) == []


def test_without_a_tail_budget_the_message_count_alone_decides():
    agent = _agent(_Summarizer("S"), context_size=4000, keep_pct=0)
    _turns(agent, 6)
    _tool_turn(agent, "L" * 10_000)
    _turns(agent, 1)
    agent.compact_history(keep_recent=8)
    assert any(m.get("role") == "tool" for m in agent.history)


def test_a_stretch_longer_than_the_window_is_summarised_a_chunk_at_a_time():
    # 3000-token window: ~5.8K characters of transcript per request
    client = _Summarizer("S1", "S2", "S3", "S4", "S5", "S6")
    agent = _agent(client, context_size=3000, keep_pct=0)
    _turns(agent, 20, size=600)
    assert agent.compact_history(keep_recent=2) > 0
    n = len(client.prompts)
    assert n > 1
    # each chunk updates the summary so far, and the last one is kept
    assert "Earlier summary:\nS1" in client.prompts[1]
    assert agent.history[1]["content"].endswith(f"S{n}")
    # the chunks cover the whole stretch, in order
    assert "user 0 " in client.prompts[0] and "reply 18 " in client.prompts[-1]


def test_a_failed_chunk_leaves_the_history_as_it_was():
    agent = _agent(_Summarizer("S1"), context_size=3000, keep_pct=0)
    _turns(agent, 20, size=600)
    before = [dict(m) for m in agent.history]
    assert agent.compact_history(keep_recent=2) == 0
    assert agent.history == before


def test_an_unknown_window_sends_one_request():
    client = _Summarizer("S")
    agent = _agent(client, context_size=0, keep_pct=0)
    _turns(agent, 20, size=600)
    assert agent.compact_history(keep_recent=2) > 0
    assert len(client.prompts) == 1


def test_compact_up_to_here_keeps_the_chosen_message_on():
    agent = _agent(_Summarizer("S"))
    _turns(agent, 5)
    chosen = agent.history[7]  # user 3
    assert agent.compact_history(upto=7) == 6
    assert agent.history[-4] == chosen
    assert validate_history(agent.history) == []


def test_compact_up_to_a_tool_result_keeps_from_the_next_step():
    agent = _agent(_Summarizer("S"))
    _turns(agent, 2)
    _tool_turn(agent, "short log")  # indices 5..8: user, call, result, reply
    _turns(agent, 1)
    assert agent.compact_history(upto=7) == 7  # the result's step ends at 8
    assert agent.history[-3]["content"] == "the log is long"


def test_compact_up_to_the_first_message_has_nothing_to_do():
    client = _Summarizer("S")
    agent = _agent(client)
    _turns(agent, 3)
    assert agent.compact_history(upto=1) == 0 and client.prompts == []


def test_the_web_route_compacts_up_to_the_chosen_message(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from tests._live_web import web_app

    agent = _agent(_Summarizer("S"))
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    c.get("/api/session/s1/history")  # opens the session
    _turns(agent, 5)
    res = c.post("/api/session/s1/compact", json={"upto": 7}).json()
    assert res["collapsed"] == 6
    msgs = c.get("/api/session/s1/history").json()["messages"]
    assert [m.get("kind") for m in msgs[1:3]] == ["compaction", "compaction_ack"]
    assert msgs[3]["content"].startswith("user 3 ")
    assert c.post("/api/session/nope/compact", json={"upto": 3}).status_code == 404
