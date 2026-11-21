"""Ollama's native thinking goes through the native reasoning channel
(`reasoning` on the delta / message), never wrapped in <think> tags."""

from __future__ import annotations

import json

import httpx

import evi.tools.fs  # noqa: F401  registers read_file
from evi.backends.factory import get_backend
from evi.config import Config
from evi.llm.agent import Agent, TextDelta, ThinkingDelta
from evi.llm.ollama_native import OllamaNativeClient
from evi.reasoning import reasoning_text
from evi.tools.base import REGISTRY


def _ndjson(*lines: dict) -> bytes:
    return b"".join(json.dumps(x).encode() + b"\n" for x in lines)


class _Fake:
    """A fake Ollama: records each /api/chat body, answers from a script."""

    def __init__(self, *replies) -> None:
        self.replies, self.bodies = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        status, content = self.replies.pop(0)
        return httpx.Response(status, content=content)


def _client(fake: _Fake) -> OllamaNativeClient:
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url = "ollama", "http://127.0.0.1:9/v1"
    client = get_backend(cfg.llm).make_client()
    assert isinstance(client, OllamaNativeClient)
    client.http = httpx.Client(transport=httpx.MockTransport(fake))
    return client


def _done() -> dict:
    return {"message": {"role": "assistant", "content": ""}, "done": True,
            "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 2}


def _deltas(fake: _Fake) -> list:
    chunks = list(_client(fake).chat.completions.create(model="m", stream=True, messages=[]))
    return [c.choices[0].delta for c in chunks if c.choices]


def test_a_streamed_answer_without_thinking_has_no_reasoning():
    deltas = _deltas(_Fake((200, _ndjson({"message": {"content": "hi"}, "done": False},
                                         _done()))))
    assert deltas[0].content == "hi" and deltas[0].reasoning is None
    assert reasoning_text(deltas[0]) == ""


def test_streamed_thinking_is_the_deltas_reasoning_not_tagged_content():
    deltas = _deltas(_Fake((200, _ndjson(
        {"message": {"thinking": "let me "}, "done": False},
        {"message": {"thinking": "see", "content": "4"}, "done": False},
        {"message": {"content": "2"}, "done": False}, _done()))))
    # getattr: the finish chunk (the last delta) need not carry the field.
    assert [(getattr(d, "reasoning", None), d.content) for d in deltas] == [
        ("let me ", None), ("see", "4"), (None, "2"), (None, None)]
    assert not any("think>" in (d.content or "") for d in deltas)


def test_the_agent_shows_ollama_thinking_with_a_literal_tag_and_keeps_it_out():
    # Thinking that mentions </think> stays thinking: before, the tag wrapped
    # around it ended the block early and leaked the rest into the answer.
    fake = _Fake((200, _ndjson({"message": {"thinking": "the parser ends at </think> so"},
                                "done": False},
                               {"message": {"content": "42"}, "done": False}, _done())))
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.model = "ollama", "http://127.0.0.1:9/v1", "qwen3"
    agent = Agent(client=_client(fake), config=cfg, transcripts=None,
                  unattended_policy="allow", tools=[REGISTRY["read_file"]])
    evs = list(agent.chat("q", max_turns=2))
    assert "".join(e.text for e in evs if isinstance(e, ThinkingDelta)) == \
        "the parser ends at </think> so"
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "42"
    assert agent.history[-1] == {"role": "assistant", "content": "42"}


def test_a_non_streamed_reply_keeps_thinking_out_of_its_content():
    # A summary, a title or a guardrail verdict reads message.content alone.
    fake = _Fake((200, json.dumps({"message": {"role": "assistant", "content": "ALLOW",
                                               "thinking": "pondering"},
                                   "done": True, "done_reason": "stop"}).encode()))
    msg = _client(fake).chat.completions.create(model="m", stream=False,
                                                messages=[]).choices[0].message
    assert msg.content == "ALLOW"
    assert msg.reasoning == "pondering" and reasoning_text(msg) == "pondering"


def test_a_non_streamed_reply_without_thinking_has_no_reasoning():
    fake = _Fake((200, json.dumps({"message": {"role": "assistant", "content": "t"},
                                   "done": True}).encode()))
    msg = _client(fake).chat.completions.create(model="m", stream=False,
                                                messages=[]).choices[0].message
    assert msg.content == "t" and msg.reasoning is None
