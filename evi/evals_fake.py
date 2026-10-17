"""The scripted model behind ``--backend fake`` (`evi eval run`, `evi models
recommend --verify`, `evi eval baseline record`): never offered for chat.

An eval case may carry a ``fake`` script, a list of turns, each one model
reply::

    fake = [
      { tool = "read_file", args = { path = "calc.py" } },
      { tool = "edit_file", args = { path = "calc.py", old_string = "a - b", new_string = "a + b" } },
      { text = "Fixed: add() now adds." },
    ]

`ScriptedClient` replays it as an OpenAI-compatible streaming client, one
turn per ``chat.completions.create(stream=True)`` call, so the case runs
through the real Agent loop: real tools, the real policy, the real run
report. It checks the harness and the suite's own consistency (CI runs every
case once with it), never a model. A request past the end of the script gets
a short final text and sets ``exhausted``, which the eval harness reports as
a failure (the script and the case disagree). One-shot requests
(``stream=False``: a compaction summary, a title) get an empty reply and use
no turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

# The final text a request past the end of the script gets.
EXHAUSTED_TEXT = "(the fake script has no more turns)"


@dataclass
class _FnDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ToolCallDelta:
    index: int
    id: str | None = None
    function: _FnDelta | None = None
    type: str = "function"


@dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[_ToolCallDelta] | None = None
    role: str = "assistant"


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class _Chunk:
    choices: list[_Choice]
    usage: Any = None


@dataclass
class _Message:
    content: str = ""
    role: str = "assistant"
    tool_calls: Any = None


@dataclass
class _FullChoice:
    message: _Message
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class _Response:
    choices: list[_FullChoice]
    usage: Any = None


def text_turn(text: str) -> list[_Chunk]:
    """One streamed reply that says `text` and stops."""
    return [_Chunk([_Choice(_Delta(content=str(text)), finish_reason="stop")])]


def call_turn(*calls: tuple[str, dict | str]) -> list[_Chunk]:
    """One streamed reply that calls the tools given as (name, args), where
    args is a dict or a JSON string."""
    deltas = [
        _ToolCallDelta(index=i, id=f"call_{i}", function=_FnDelta(
            name=name, arguments=args if isinstance(args, str) else json.dumps(args)))
        for i, (name, args) in enumerate(calls)
    ]
    return [_Chunk([_Choice(_Delta(tool_calls=deltas), finish_reason="tool_calls")])]


def turn_chunks(turn: dict[str, Any]) -> list[_Chunk]:
    """A case's script turn ({text} or {tool, args}) as stream chunks."""
    if "tool" in turn:
        return call_turn((str(turn["tool"]), dict(turn.get("args") or {})))
    return text_turn(str(turn.get("text", "")))


class _Completions:
    def __init__(self, owner: "ScriptedClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Iterator[_Chunk] | _Response:
        return self._owner._create(kwargs)


class _Chat:
    def __init__(self, owner: "ScriptedClient") -> None:
        self.completions = _Completions(owner)


class ScriptedClient:
    """An OpenAI-compatible client that replays `turns` (a case's ``fake``
    script: dicts, see the module doc, or ready-made chunk lists)."""

    def __init__(self, turns: Sequence[dict[str, Any] | list[_Chunk]]) -> None:
        self._turns = [t if isinstance(t, list) else turn_chunks(t) for t in turns or ()]
        self.calls = 0
        self.exhausted = False
        self.requests: list[dict[str, Any]] = []
        self.chat = _Chat(self)

    @property
    def scripted(self) -> int:
        return len(self._turns)

    def _create(self, kwargs: dict[str, Any]) -> Iterator[_Chunk] | _Response:
        self.requests.append(kwargs)
        if not kwargs.get("stream"):
            return _Response([_FullChoice(_Message(content=""))])
        if self.calls >= len(self._turns):
            self.exhausted = True
            return iter(text_turn(EXHAUSTED_TEXT))
        turn = self._turns[self.calls]
        self.calls += 1
        return iter(turn)
