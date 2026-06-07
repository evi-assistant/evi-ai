"""Opt-in OpenAI **Responses API** path for the agent loop (Phase 55).

eVi's default — and the only shape its local backends (LM Studio / Ollama /
llama.cpp) speak — is **Chat Completions**. This module adds an *optional*
Responses API path, enabled with `[llm] api = "responses"` (or `EVI_LLM_API`),
for endpoints that implement it (e.g. OpenAI cloud). Local-first is unaffected:
nothing here runs unless you opt in.

The trick that keeps the (large, battle-tested) streaming loop in `agent.py`
unchanged: `adapt_responses_stream` re-emits Responses stream events as objects
shaped like Chat Completion chunks (`.choices[0].delta.content` / `.tool_calls`
/ `.finish_reason`, and a final `.usage`). We dispatch on each event's `.type`
discriminator (stable across SDK versions) rather than isinstance.

NOTE: the converters + adapter are unit-tested against the SDK's documented
event shapes, but NOT verified against a live Responses endpoint (no cloud
access in CI). Treat first real use as the integration test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterable, Iterator


# --- request conversion (chat shape -> responses shape) ------------------


def _as_text(content: Any) -> str:
    """Flatten chat `content` (str or multimodal parts list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text") and p.get("text"):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return "" if content is None else str(content)


def to_responses_tools(tools: list[dict] | None) -> list[dict]:
    """Chat tool schema -> Responses tool schema (the function fields are
    flattened up one level)."""
    out: list[dict] = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return out


def to_responses_input(messages: list[dict]) -> list[dict]:
    """Chat messages -> Responses `input` items.

    Text messages pass through (role/content); assistant `tool_calls` become
    `function_call` items; `tool`-role results become `function_call_output`
    items — so a full tool-using history round-trips.
    """
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": _as_text(m.get("content", "")),
            })
            continue
        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            if m.get("content"):
                items.append({"role": "assistant", "content": _as_text(m["content"])})
            for tc in tool_calls:
                fn = tc.get("function", {})
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                })
            continue
        items.append({"role": role, "content": _as_text(m.get("content", ""))})
    return items


# --- response stream adaptation (responses events -> chat chunks) --------


def _tc(index: int, *, id: str | None = None, name: str | None = None,
        args: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=args))


def _text_chunk(text: str) -> SimpleNamespace:
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None, logprobs=None)],
                           usage=None)


def _tool_chunk(tc: SimpleNamespace) -> SimpleNamespace:
    delta = SimpleNamespace(content=None, tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None, logprobs=None)],
                           usage=None)


def _finish_chunk(reason: str) -> SimpleNamespace:
    delta = SimpleNamespace(content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=reason, logprobs=None)],
                           usage=None)


def _usage_chunk(usage: SimpleNamespace) -> SimpleNamespace:
    # No choices — matches how Chat Completions delivers the usage tally.
    return SimpleNamespace(choices=[], usage=usage)


def _convert_usage(u: Any) -> SimpleNamespace | None:
    if u is None:
        return None
    inp = getattr(u, "input_tokens", None) or 0
    out = getattr(u, "output_tokens", None) or 0
    tot = getattr(u, "total_tokens", None) or (inp + out)
    return SimpleNamespace(prompt_tokens=inp, completion_tokens=out, total_tokens=tot)


def adapt_responses_stream(events: Iterable[Any]) -> Iterator[SimpleNamespace]:
    """Re-emit Responses stream events as Chat-Completion-shaped chunks so the
    existing agent loop consumes them unchanged."""
    fc_slot: dict[int, int] = {}   # responses output_index -> our tool_call index
    saw_function_call = False
    for ev in events:
        et = getattr(ev, "type", "")
        if et == "response.output_text.delta":
            d = getattr(ev, "delta", "") or ""
            if d:
                yield _text_chunk(d)
        elif et == "response.output_item.added":
            item = getattr(ev, "item", None)
            if item is not None and getattr(item, "type", "") == "function_call":
                oi = getattr(ev, "output_index", len(fc_slot))
                idx = fc_slot.setdefault(oi, len(fc_slot))
                saw_function_call = True
                call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
                yield _tool_chunk(_tc(idx, id=call_id, name=getattr(item, "name", None)))
        elif et == "response.function_call_arguments.delta":
            oi = getattr(ev, "output_index", 0)
            idx = fc_slot.setdefault(oi, len(fc_slot))
            d = getattr(ev, "delta", "") or ""
            if d:
                yield _tool_chunk(_tc(idx, args=d))
        elif et == "response.completed":
            yield _finish_chunk("tool_calls" if saw_function_call else "stop")
            usage = _convert_usage(getattr(getattr(ev, "response", None), "usage", None))
            if usage is not None:
                yield _usage_chunk(usage)
        elif et in ("response.failed", "error"):
            yield _finish_chunk("stop")


def stream_chat_via_responses(client: Any, *, model: str, messages: list[dict],
                              tools: list[dict] | None = None,
                              temperature: float | None = None,
                              max_tokens: int | None = None,
                              max_completion_tokens: int | None = None,
                              **_ignored: Any) -> Iterator[SimpleNamespace]:
    """Call `client.responses.create(stream=True)` with chat-shaped kwargs and
    yield Chat-Completion-shaped chunks. Chat-only kwargs (tool_choice,
    stream_options, logprobs, penalties, …) are accepted and ignored."""
    kwargs: dict[str, Any] = {
        "model": model,
        "input": to_responses_input(messages),
        "stream": True,
    }
    if tools:
        kwargs["tools"] = to_responses_tools(tools)
    if temperature is not None:
        kwargs["temperature"] = temperature
    budget = max_completion_tokens or max_tokens
    if budget:
        kwargs["max_output_tokens"] = int(budget)
    yield from adapt_responses_stream(client.responses.create(**kwargs))
