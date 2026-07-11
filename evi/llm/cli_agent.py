"""Shared shim core for CLI-agent backends.

eVi backends must expose an OpenAI ``chat.completions.create(...)`` surface, but
several AI coding CLIs (Claude Code, OpenAI Codex, …) are agent loops reached over
a LOCAL CLI/SDK using a subscription login (no API key) — not OpenAI-compatible.
To present one as an eVi backend we run it and adapt its streamed output into the
chat-completions chunk shape eVi's agent loop consumes. This module holds the
parts that are IDENTICAL across every such CLI:

* OpenAI-shaped duck-typed chunks/responses — only the fields eVi's loop reads
  (``chunk.choices[0].delta.content`` / ``.tool_calls`` / ``.finish_reason`` and a
  final ``chunk.usage``; non-stream ``resp.choices[i].message.content``).
* The async/subprocess → SYNC bridge: run one turn on a background thread that
  pushes chunks onto a queue; a sync generator drains it (eVi's loop is sync).
* The ``.chat.completions.create(...)`` client shell (stream + non-stream + ``n``),
  parameterised by a per-CLI DRIVER.

A per-CLI backend supplies a driver with ``run_turn(*, model, messages, tools,
out)`` that runs ONE turn (spawning its subprocess / SDK query however it likes)
and puts chunk objects on ``out``, finishing with a finish chunk + a usage chunk
(or ``cli_agent.error(exc)``). See ``evi/llm/claude_agent.py`` (claude-agent-sdk
driver) and ``evi/llm/codex_agent.py`` (``codex exec --json`` subprocess driver).
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Callable

_SENTINEL = object()


class CliUnavailable(RuntimeError):
    """A driver's CLI/SDK isn't installed or isn't logged in. Raised lazily at
    call time so eVi runs fine until the backend is actually selected."""


def flatten_content(content) -> str:
    """OpenAI message content (str, or a list of ``{type:text,text}`` parts) → str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    out.append(part["text"])
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return str(content)


# --- OpenAI-shaped duck types (only the fields eVi's agent loop reads) --------


def delta_chunk(content: str | None = None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, logprobs=None)
    return SimpleNamespace(choices=[choice], usage=None)


def usage_chunk(prompt_tokens: int = 0, completion_tokens: int = 0):
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    payload = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    return SimpleNamespace(choices=[], usage=payload)


def tool_call_delta(idx: int, call_id: str, name: str, arguments: str):
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=idx, id=call_id, function=fn, type="function")
    return delta_chunk(tool_calls=[tc])


def error(exc: BaseException):
    """A driver puts this on its queue to surface a fatal turn error to eVi."""
    return ("__error__", exc)


# --- async / subprocess -> sync bridge ---------------------------------------


def spawn_turn(run_turn: Callable[[queue.Queue], None]) -> queue.Queue:
    """Run ``run_turn(out)`` on a daemon thread; return the queue it fills. The
    sentinel + error tuple are handled by :func:`drain`."""
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            run_turn(q)
        except Exception as exc:  # noqa: BLE001 — a turn failure can't crash eVi
            q.put(("__error__", exc))
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=worker, name="cli-agent-turn", daemon=True).start()
    return q


def drain(q: queue.Queue):
    """Yield chunk objects off the queue until the sentinel; re-raise a driver
    error tuple so it propagates like an OpenAI SDK exception."""
    while True:
        item = q.get()
        if item is _SENTINEL:
            return
        if isinstance(item, tuple) and item and item[0] == "__error__":
            raise item[1]
        yield item


# --- the OpenAI-client shell --------------------------------------------------


class _Completions:
    def __init__(self, client: "CliAgentClient"):
        self._client = client

    def create(self, *, model=None, messages=None, tools=None, stream=False,
               n=1, **_ignored):
        """OpenAI-compatible entry point. Extra kwargs (temperature, top_p,
        tool_choice, stream_options, …) are accepted and ignored — a CLI agent
        doesn't take them."""
        driver = self._client._driver
        model = model or self._client.model
        msgs = messages or []

        def run(out):
            driver.run_turn(model=model, messages=msgs, tools=tools, out=out)

        if stream:
            return drain(spawn_turn(run))

        # Non-streaming: run n times, collect text + tool_calls into n choices.
        choices = []
        usage_payload = None
        for i in range(max(1, int(n or 1))):
            text_parts: list[str] = []
            tool_calls = []
            for chunk in drain(spawn_turn(run)):
                if getattr(chunk, "usage", None) is not None:
                    usage_payload = chunk.usage
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                if d and d.content:
                    text_parts.append(d.content)
                if d and d.tool_calls:
                    for tc in d.tool_calls:
                        tool_calls.append(SimpleNamespace(
                            id=tc.id, type="function",
                            function=SimpleNamespace(name=tc.function.name,
                                                     arguments=tc.function.arguments),
                        ))
            message = SimpleNamespace(
                role="assistant",
                content="".join(text_parts) or None,
                tool_calls=tool_calls or None,
            )
            choices.append(SimpleNamespace(index=i, message=message, finish_reason="stop"))
        return SimpleNamespace(choices=choices, usage=usage_payload)


class _Chat:
    def __init__(self, client: "CliAgentClient"):
        self.completions = _Completions(client)


class CliAgentClient:
    """OpenAI-``client``-shaped object backed by a per-CLI ``driver``. Only the
    surface eVi's agent loop touches is implemented: ``.chat.completions.create``.
    ``model`` is the default model id when a call omits one."""

    def __init__(self, driver, default_model: str = ""):
        self._driver = driver
        self.model = default_model or ""
        self.chat = _Chat(self)
