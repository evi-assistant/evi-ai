"""Tests for Phase 34: speculative-decoding `prediction` parameter.

Covers the agent-level wiring (extra_body shape, first-round-only
semantics, coexistence with `reasoning_effort`) plus the CLI surfaces
(`/predict` slash command + `evi edit` command).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evi.config import Config
from evi.llm.agent import Agent


# ---- shared fake client -------------------------------------------------


class _Delta:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content="", tool_calls=None, finish="stop"):
        self.delta = _Delta(content=content, tool_calls=tool_calls)
        self.finish_reason = finish


class _Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _ToolCallDelta:
    """OpenAI tool-call delta shape used inside delta.tool_calls."""

    def __init__(self, idx, call_id, name, args):
        self.index = idx
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": args})()


class _CapturingCompletions:
    """Records each create() call's kwargs. Replies with a single text chunk."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._scripted: list[list[_Chunk]] = []

    def script(self, *responses: list[_Chunk]) -> None:
        """Queue scripted responses for successive create() calls."""
        self._scripted = list(responses)

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self._scripted:
            return iter(self._scripted.pop(0))
        return iter([_Chunk(choices=[_Choice(content="ok", finish="stop")])])


def _make_agent(cfg: Config | None = None, tools=None) -> tuple[Agent, _CapturingCompletions]:
    cc = _CapturingCompletions()
    client = type("C", (), {"chat": type("X", (), {"completions": cc})()})()
    return Agent(client=client, config=cfg or Config(), tools=tools or []), cc


# ---- agent: extra_body shape -------------------------------------------


def test_prediction_default_omitted_from_extra_body() -> None:
    """No prediction = no extra_body key sneaks through."""
    agent, cc = _make_agent()
    list(agent.chat("hi"))
    assert "extra_body" not in cc.calls[0]


def test_prediction_text_lands_in_extra_body() -> None:
    """The string is wrapped in {"type":"content","content":...} per OpenAI."""
    agent, cc = _make_agent()
    list(agent.chat("hi", prediction="def foo():\n    pass"))
    extra = cc.calls[0].get("extra_body")
    assert extra is not None
    assert extra["prediction"] == {
        "type": "content",
        "content": "def foo():\n    pass",
    }


def test_prediction_coexists_with_reasoning_effort() -> None:
    """When both effort and prediction are set, both ride in extra_body."""
    cfg = Config()
    cfg.llm.reasoning_effort = "high"
    agent, cc = _make_agent(cfg)
    list(agent.chat("hi", prediction="hello world"))
    extra = cc.calls[0]["extra_body"]
    assert extra["reasoning_effort"] == "high"
    assert extra["prediction"]["content"] == "hello world"


def test_prediction_empty_string_treated_as_unset() -> None:
    """An empty string isn't a useful hint — don't bloat extra_body with it."""
    agent, cc = _make_agent()
    list(agent.chat("hi", prediction=""))
    assert "extra_body" not in cc.calls[0]


# ---- first-round-only semantics ----------------------------------------


class _ToolThatRecordsArgs:
    """Minimal Tool stand-in: records each call() and returns "done"."""

    name = "noop"
    description = "no-op tool used in prediction tests"
    category = "fs"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def call(self, args_json: str) -> str:
        self.calls.append(args_json)
        return "done"


def test_prediction_only_on_first_round_when_tool_runs() -> None:
    """After a tool round-trip the prediction is stale — drop it from the
    second LLM call so the model writes freely from the tool output."""
    tool = _ToolThatRecordsArgs()
    agent, cc = _make_agent(tools=[tool])
    cc.script(
        # Round 1: the model invokes the tool — no visible text.
        [
            _Chunk(choices=[_Choice(
                content="",
                tool_calls=[_ToolCallDelta(0, "call_1", "noop", "{}")],
                finish="tool_calls",
            )]),
        ],
        # Round 2: tool result is fed back; model emits final text.
        [
            _Chunk(choices=[_Choice(content="final answer", finish="stop")]),
        ],
    )
    list(agent.chat("edit foo.py to use Path", prediction="ORIG_FILE_CONTENT"))

    assert len(cc.calls) == 2
    # First LLM call carries the prediction…
    extra_1 = cc.calls[0].get("extra_body") or {}
    assert extra_1.get("prediction", {}).get("content") == "ORIG_FILE_CONTENT"
    # …second one does NOT.
    extra_2 = cc.calls[1].get("extra_body") or {}
    assert "prediction" not in extra_2


# ---- CLI slash command --------------------------------------------------


def test_slash_predict_sets_pending_text() -> None:
    from evi.apps.cli.main import _handle_predict
    from evi.commands import CommandStore

    agent, _cc = _make_agent()
    store = CommandStore()
    _handle_predict(agent, "the predicted output", store)
    assert agent._pending_prediction == "the predicted output"


def test_slash_predict_clear() -> None:
    from evi.apps.cli.main import _handle_predict
    from evi.commands import CommandStore

    agent, _cc = _make_agent()
    agent._pending_prediction = "preexisting"
    _handle_predict(agent, "clear", CommandStore())
    assert agent._pending_prediction is None


def test_slash_predict_file_reads_file(tmp_path: Path) -> None:
    from evi.apps.cli.main import _handle_predict
    from evi.commands import CommandStore

    target = tmp_path / "source.py"
    target.write_text("def x():\n    return 42\n", encoding="utf-8")
    agent, _cc = _make_agent()
    _handle_predict(agent, f"file {target}", CommandStore())
    assert agent._pending_prediction == "def x():\n    return 42\n"


def test_slash_predict_file_missing_path(tmp_path: Path) -> None:
    from evi.apps.cli.main import _handle_predict
    from evi.commands import CommandStore

    agent, _cc = _make_agent()
    _handle_predict(agent, f"file {tmp_path / 'nope.txt'}", CommandStore())
    # Errors are printed; pending stays unset.
    assert getattr(agent, "_pending_prediction", None) is None


# ---- web: ChatRequest.prediction ---------------------------------------


def test_chat_request_accepts_prediction() -> None:
    """The Pydantic model takes a prediction field without coercion drama."""
    import pytest

    pytest.importorskip("fastapi")
    from evi.apps.web.server import ChatRequest

    req = ChatRequest(
        session_id="abc",
        message="hi",
        prediction="ORIGINAL_FILE",
    )
    assert req.prediction == "ORIGINAL_FILE"


def test_chat_request_prediction_optional() -> None:
    import pytest

    pytest.importorskip("fastapi")
    from evi.apps.web.server import ChatRequest

    req = ChatRequest(session_id="abc", message="hi")
    assert req.prediction is None


def test_chat_request_serializes_round_trip() -> None:
    """The field survives JSON round-trips so SSE clients can send it."""
    import pytest

    pytest.importorskip("fastapi")
    from evi.apps.web.server import ChatRequest

    req = ChatRequest.model_validate_json(
        json.dumps({"session_id": "abc", "message": "hi", "prediction": "X"})
    )
    assert req.prediction == "X"
