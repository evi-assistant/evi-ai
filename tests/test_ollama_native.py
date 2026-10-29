"""Chat through Ollama's native /api/chat (evi.llm.ollama_native): requests
carry the context window and the agent loop gets the chunks it reads."""

from __future__ import annotations

import json

import httpx
import pytest

import evi.tools.fs  # noqa: F401  registers read_file
from evi.backends.factory import get_backend
from evi.config import Config
from evi.llm.agent import Agent, Done, TextDelta, ThinkingDelta, ToolResult, UsageStats
from evi.llm.ollama_native import OllamaError, OllamaNativeClient, native_body
from evi.tools.base import REGISTRY


def _ndjson(*lines: dict) -> bytes:
    return b"".join(json.dumps(x).encode() + b"\n" for x in lines)


class _Fake:
    """A fake Ollama: records each /api/chat body, answers from a script."""

    def __init__(self, *replies) -> None:
        self.replies, self.bodies = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
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


def _agent(fake: _Fake, tmp_path, size: int = 8192) -> Agent:
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.model = "ollama", "http://127.0.0.1:9/v1", "qwen"
    cfg.llm.context_size = size
    agent = Agent(client=_client(fake), config=cfg, transcripts=None, unattended_policy="allow",
                  tools=[REGISTRY["read_file"]])
    agent.cwd = str(tmp_path)
    return agent


def _done(prompt: int = 50, reply: int = 5, reason: str = "stop") -> dict:
    return {"message": {"role": "assistant", "content": ""}, "done": True,
            "done_reason": reason, "prompt_eval_count": prompt, "eval_count": reply}


def test_a_turn_sends_the_window_and_streams_text_and_usage(tmp_path):
    fake = _Fake((200, _ndjson({"message": {"content": "Hel"}, "done": False},
                               {"message": {"content": "lo"}, "done": False},
                               _done(42, 2))))
    evs = list(_agent(fake, tmp_path, size=8192).chat("hi", max_turns=2))
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "Hello"
    assert UsageStats(prompt_tokens=42, completion_tokens=2, total_tokens=44) in evs
    assert evs[-1] == Done("stop")
    body = fake.bodies[0]
    assert body["options"]["num_ctx"] == 8192 and body["stream"] is True
    assert body["tools"][0]["function"]["name"] == "read_file"


def test_a_tool_call_round_trips_with_object_arguments(tmp_path):
    (tmp_path / "n.txt").write_text("PELICAN\n", encoding="utf-8")
    call = {"function": {"name": "read_file", "arguments": {"path": "n.txt"}}}
    fake = _Fake((200, _ndjson({"message": {"content": "", "tool_calls": [call]}, "done": False},
                               _done())),
                 (200, _ndjson({"message": {"content": "It says PELICAN."}, "done": False},
                               _done())))
    evs = list(_agent(fake, tmp_path).chat("read it", max_turns=3))
    assert [e.output for e in evs if isinstance(e, ToolResult)][0].strip().endswith("PELICAN")
    second = fake.bodies[1]["messages"]
    assistant = [m for m in second if m.get("tool_calls")][0]
    assert assistant["tool_calls"][0]["function"]["arguments"] == {"path": "n.txt"}
    tool = [m for m in second if m["role"] == "tool"][0]
    assert tool["tool_name"] == "read_file" and "PELICAN" in tool["content"]


def test_thinking_arrives_as_thinking_not_as_text(tmp_path):
    fake = _Fake((200, _ndjson({"message": {"thinking": "let me "}, "done": False},
                               {"message": {"thinking": "see"}, "done": False},
                               {"message": {"content": "42"}, "done": False}, _done())))
    evs = list(_agent(fake, tmp_path).chat("q", max_turns=2))
    assert "".join(e.text for e in evs if isinstance(e, ThinkingDelta)) == "let me see"
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "42"


def test_an_error_from_ollama_is_its_own_message(tmp_path):
    fake = _Fake((404, json.dumps({"error": "model 'qwen' not found"}).encode()))
    client = _client(fake)
    with pytest.raises(OllamaError, match="model 'qwen' not found"):
        client.chat.completions.create(model="qwen", stream=True, messages=[])


def test_a_non_streamed_call_returns_one_message():
    fake = _Fake((200, json.dumps({"message": {"role": "assistant", "content": "summary"},
                                   "done": True, "done_reason": "stop",
                                   "prompt_eval_count": 9, "eval_count": 3}).encode()))
    resp = _client(fake).chat.completions.create(model="m", stream=False, max_tokens=50,
                                                 messages=[{"role": "user", "content": "x"}])
    assert resp.choices[0].message.content == "summary" and resp.usage.total_tokens == 12
    assert fake.bodies[0]["options"]["num_predict"] == 50


def test_a_side_call_asks_for_the_configured_window_too():
    # A summary or title call names no window; without one Ollama would
    # reload the model with its default and the next turn reload it again.
    reply = json.dumps({"message": {"content": "t"}, "done": True}).encode()
    fake = _Fake((200, reply), (200, reply))
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.context_size = "ollama", "http://127.0.0.1:9/v1", 16384
    client = get_backend(cfg.llm).make_client()
    client.http = httpx.Client(transport=httpx.MockTransport(fake))
    client.chat.completions.create(model="m", stream=False, messages=[])
    client.chat.completions.create(model="m", stream=False, messages=[],
                                   extra_body={"options": {"num_ctx": 2048}})
    assert [b["options"]["num_ctx"] for b in fake.bodies] == [16384, 2048]


def test_the_request_body_maps_the_openai_knobs():
    body = native_body({
        "model": "m", "stream": True, "temperature": 0.2, "top_p": 0.9, "seed": 7,
        "max_completion_tokens": 100, "stop": "END",
        "response_format": {"type": "json_object"},
        "extra_body": {"options": {"num_ctx": 4096}, "reasoning_effort": "high",
                       "cache_prompt": True},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}],
    })
    assert body["options"] == {"num_ctx": 4096, "temperature": 0.2, "top_p": 0.9, "seed": 7,
                               "num_predict": 100, "stop": ["END"]}
    assert body["format"] == "json" and body["think"] == "high"
    assert body["messages"] == [{"role": "user", "content": "look", "images": ["QUJD"]}]
    assert native_body({"model": "m", "extra_body": {"think": False}})["think"] is False


def test_the_openai_endpoint_stays_available_by_opting_out():
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.ollama_native = "ollama", "http://h:11434/v1", False
    assert not isinstance(get_backend(cfg.llm).make_client(), OllamaNativeClient)
