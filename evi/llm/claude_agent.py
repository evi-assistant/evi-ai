"""Claude Agent SDK shim — present the ``claude-agent-sdk`` (Claude Code over the
local ``claude`` CLI) as an OpenAI-Chat-Completions-shaped client so eVi's agent
loop can drive it unchanged.

Why a shim: eVi's inference is hard-wired to ``client.chat.completions.create``
(streaming chunks with ``delta.content`` / ``delta.tool_calls``, a final usage
chunk, ``n`` non-streaming choices). The Agent SDK is a *different* protocol — an
async agent loop that talks to the local ``claude`` CLI using the Max/Pro
subscription login (no ``ANTHROPIC_API_KEY``). This module bridges the two:

* **Translate** eVi's OpenAI messages -> the SDK's streaming-input message dicts
  (assistant ``tool_calls`` -> ``tool_use`` blocks, ``role:"tool"`` -> a
  ``tool_result`` block), and system messages -> ``system_prompt``.
* **eVi keeps driving the tools.** eVi's tools are registered with the SDK as
  schema-only in-process MCP tools purely so Claude knows they exist; a
  ``can_use_tool`` interceptor captures the *first* requested call, denies +
  interrupts (so the SDK never executes it), and the shim surfaces it back to
  eVi as an OpenAI ``delta.tool_calls`` chunk with ``finish_reason="tool_calls"``.
  eVi then runs the tool with its own permissions/checkpoints/events and calls
  ``create`` again — exactly as it does for any OpenAI model. Because eVi resends
  full history every turn, each ``create`` maps to one fresh (stateless) SDK turn.
* **Async -> sync.** The SDK is async; eVi's loop is sync. Each ``create`` runs
  the query on a background thread's event loop, pushing OpenAI-shaped chunks
  onto a queue that the returned sync generator drains.

Auth is entirely the local ``claude`` CLI login — nothing here reads or needs an
API key. See ``evi/backends/claude_agent.py`` for the backend that returns this
shim, and ``docs`` for setup (install the ``claude`` CLI + ``pip install
'evi-assistant[claude-agent]'``, then ``claude`` login on your Max/Pro plan).
"""

from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace
from typing import Any

# The in-process MCP server prefixes tool names ``mcp__<server>__<tool>``. We use
# a fixed server name so wrap/unwrap round-trips eVi tool names cleanly.
_MCP_SERVER = "evi"
_TOOL_PREFIX = f"mcp__{_MCP_SERVER}__"

_SENTINEL = object()


class ClaudeAgentUnavailable(RuntimeError):
    """Raised (lazily, at call time) when the SDK or the ``claude`` CLI is missing."""


def _import_sdk():
    try:
        import claude_agent_sdk as sdk  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise ClaudeAgentUnavailable(
            "The claude_agent backend needs the Claude Agent SDK. Install it with "
            "`pip install 'evi-assistant[claude-agent]'` and make sure the `claude` "
            "CLI is on PATH and logged in (your Max/Pro plan)."
        ) from exc
    return sdk


def _unwrap_tool_name(name: str) -> str:
    return name[len(_TOOL_PREFIX):] if name.startswith(_TOOL_PREFIX) else name


def _as_text(content: Any) -> str:
    """Flatten OpenAI message content (str or a list of parts) to plain text."""
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


def translate_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI messages -> (system_prompt, [SDK streaming-input message dicts]).

    System messages become ``system_prompt``. Prior tool interactions are
    rendered as PLAIN TEXT rather than replayed as structured
    ``tool_use``/``tool_result`` blocks: replaying a structured tool_use that
    names an in-process MCP tool trips the CLI's "tool use concurrency" guard.
    eVi still *drives* tools (a live call is intercepted on the current turn) —
    only the on-the-wire representation of past calls changes. An assistant that
    called ``add({"a":2})`` and got ``5`` back reads to Claude as
    ``[called add({"a":2})]`` then ``[tool add returned: 5]``, which carries the
    same information robustly across CLI versions.
    """
    system_parts: list[str] = []
    sdk_msgs: list[dict] = []
    id_to_name: dict[str, str] = {}

    def _push(role: str, text: str) -> None:
        if not text:
            return
        # Content MUST be a list of blocks: the CLI scans assistant content with
        # `content.some(r => r.type === "tool_use")`, which throws on a bare
        # string. Coalesce consecutive same-role turns into extra text blocks.
        block = {"type": "text", "text": text}
        if sdk_msgs and sdk_msgs[-1]["message"]["role"] == role:
            sdk_msgs[-1]["message"]["content"].append(block)
        else:
            sdk_msgs.append({"type": role, "message": {"role": role, "content": [block]}})

    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            txt = _as_text(content)
            if txt:
                system_parts.append(txt)
        elif role == "tool":
            name = id_to_name.get(m.get("tool_call_id") or "", "tool")
            _push("user", f"[tool {name} returned: {_as_text(content)}]")
        elif role == "assistant":
            parts: list[str] = []
            txt = _as_text(content)
            if txt:
                parts.append(txt)
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                nm = fn.get("name") or "tool"
                id_to_name[tc.get("id") or ""] = nm
                parts.append(f"[called {nm}({fn.get('arguments') or '{}'})]")
            _push("assistant", "\n".join(parts))
        else:  # user (default)
            _push("user", _as_text(content))
    return "\n\n".join(system_parts), sdk_msgs


def _build_tool_server(sdk, tools: list[dict] | None):
    """Register eVi's OpenAI tool schemas as schema-only SDK MCP tools so Claude
    knows they exist. The handlers never run — ``can_use_tool`` intercepts every
    call first. Returns ``(mcp_server_or_None, present)``."""
    if not tools:
        return None, False

    async def _never(_args):  # pragma: no cover - interception prevents execution
        return {"content": [{"type": "text", "text": ""}]}

    sdk_tools = []
    for t in tools:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        desc = fn.get("description") or name
        sdk_tools.append(sdk.tool(name, desc, schema)(_never))
    if not sdk_tools:
        return None, False
    return sdk.create_sdk_mcp_server(_MCP_SERVER, "1.0.0", sdk_tools), True


# --- OpenAI-shaped duck types (only the fields eVi's loop reads) --------------


def _delta_chunk(content: str | None = None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, logprobs=None)
    return SimpleNamespace(choices=[choice], usage=None)


def _usage_chunk(usage: dict | None):
    u = usage or {}
    prompt = int(u.get("input_tokens", 0) or 0)
    completion = int(u.get("output_tokens", 0) or 0)
    payload = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    return SimpleNamespace(choices=[], usage=payload)


def _tool_call_delta(idx: int, call_id: str, name: str, arguments: str):
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=idx, id=call_id, function=fn, type="function")
    return _delta_chunk(tool_calls=[tc])


# --- the async worker + sync bridge -------------------------------------------


async def _run_turn(sdk, *, model, system_prompt, sdk_msgs, tools, out: queue.Queue):
    """One SDK turn. Streams text chunks onto `out`; if Claude requests a tool,
    captures the first call (deny+interrupt) and emits it as a tool_calls chunk."""
    server, has_tools = _build_tool_server(sdk, tools)
    captured: dict[str, Any] = {}

    if has_tools:
        # Past tool calls are replayed as `[tool NAME returned: VALUE]` text (a
        # structured tool_use replay trips the CLI's tool-concurrency guard).
        # Tell the model those are authoritative so it uses them instead of
        # re-calling the same tool.
        directive = (
            "Earlier tool results appear in the conversation as "
            "`[tool NAME returned: VALUE]`. Treat each as an authoritative "
            "result you already obtained — use it directly and do NOT call the "
            "same tool again with the same arguments."
        )
        system_prompt = f"{system_prompt}\n\n{directive}" if system_prompt else directive

    async def can_use_tool(tool_name, input_data, context):
        if not captured:  # capture only the first requested call
            captured["name"] = _unwrap_tool_name(tool_name)
            captured["input"] = dict(input_data or {})
            captured["id"] = getattr(context, "tool_use_id", None) or f"call_{len(sdk_msgs)}"
        return sdk.PermissionResultDeny(message="eVi drives tools", interrupt=True)

    opts_kwargs: dict[str, Any] = {
        "model": model or None,
        "system_prompt": system_prompt or None,
        "tools": [],                 # no built-in Claude Code tools
        "setting_sources": [],       # never load a CLAUDE.md / settings from disk
        "max_turns": 1,
    }
    if has_tools:
        opts_kwargs["mcp_servers"] = {_MCP_SERVER: server}
        opts_kwargs["allowed_tools"] = []      # force fall-through to can_use_tool
        opts_kwargs["can_use_tool"] = can_use_tool
    else:
        opts_kwargs["permission_mode"] = "bypassPermissions"
    options = sdk.ClaudeAgentOptions(**opts_kwargs)

    async def _prompt():
        for m in sdk_msgs:
            yield m

    usage: dict | None = None
    agen = sdk.query(prompt=_prompt(), options=options)
    try:
        async for msg in agen:
            if isinstance(msg, sdk.AssistantMessage):
                for b in msg.content:
                    if isinstance(b, sdk.TextBlock) and b.text:
                        out.put(_delta_chunk(content=b.text))
                    elif isinstance(b, sdk.ThinkingBlock) and getattr(b, "thinking", ""):
                        # Wrap reasoning in eVi's think tags so the ThinkParser routes it.
                        out.put(_delta_chunk(content=f"<think>{b.thinking}</think>"))
            elif isinstance(msg, sdk.ResultMessage):
                usage = msg.usage
                if msg.is_error and not captured:
                    out.put(("__error__", RuntimeError(msg.result or "claude_agent error")))
                    return
    except Exception as exc:  # noqa: BLE001
        # A captured tool call surfaces the interrupt as an error result — expected.
        if not captured:
            out.put(("__error__", exc))
            return
    finally:
        # Best-effort close of the SDK's transport generator so an error path
        # doesn't spew "asynchronous generator is already running" finalizer noise.
        _aclose = getattr(agen, "aclose", None)
        if _aclose is not None:
            try:
                await _aclose()
            except Exception:  # noqa: BLE001
                pass

    if captured:
        out.put(_tool_call_delta(0, captured["id"], captured["name"],
                                 json.dumps(captured["input"])))
        out.put(_delta_chunk(finish_reason="tool_calls"))
    else:
        out.put(_delta_chunk(finish_reason="stop"))
    out.put(_usage_chunk(usage))


def _spawn(sdk, *, model, system_prompt, sdk_msgs, tools) -> queue.Queue:
    q: queue.Queue = queue.Queue()

    def worker():
        import asyncio  # noqa: PLC0415
        try:
            asyncio.run(_run_turn(sdk, model=model, system_prompt=system_prompt,
                                  sdk_msgs=sdk_msgs, tools=tools, out=q))
        except Exception as exc:  # noqa: BLE001
            q.put(("__error__", exc))
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=worker, name="claude-agent-turn", daemon=True).start()
    return q


def _drain(q: queue.Queue):
    while True:
        item = q.get()
        if item is _SENTINEL:
            return
        if isinstance(item, tuple) and item and item[0] == "__error__":
            raise item[1]
        yield item


# --- the client the backend hands to the agent --------------------------------


class _Completions:
    def __init__(self, client: "ClaudeAgentClient"):
        self._client = client

    def create(self, *, model=None, messages=None, tools=None, stream=False,
               n=1, **_ignored):
        """OpenAI-compatible entry point. Extra kwargs (temperature, top_p,
        tool_choice, stream_options, ...) are accepted and ignored — the CLI
        does not take them."""
        sdk = self._client._sdk
        system_prompt, sdk_msgs = translate_messages(messages or [])
        model = model or self._client.model

        if stream:
            q = _spawn(sdk, model=model, system_prompt=system_prompt,
                       sdk_msgs=sdk_msgs, tools=tools)
            return _drain(q)

        # Non-streaming: run to completion, collect text into `n` choices.
        choices = []
        usage_payload = None
        for i in range(max(1, int(n or 1))):
            q = _spawn(sdk, model=model, system_prompt=system_prompt,
                       sdk_msgs=sdk_msgs, tools=tools)
            text_parts: list[str] = []
            tool_calls: list[Any] = []
            for chunk in _drain(q):
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
            choices.append(SimpleNamespace(index=i, message=message,
                                           finish_reason="stop"))
        return SimpleNamespace(choices=choices, usage=usage_payload)


class _Chat:
    def __init__(self, client: "ClaudeAgentClient"):
        self.completions = _Completions(client)


class ClaudeAgentClient:
    """OpenAI-``client``-shaped object backed by the Claude Agent SDK.

    Only the surface eVi's agent loop touches is implemented: ``.chat.completions
    .create(...)``. ``model`` is the default model id/alias when a call omits one.
    """

    def __init__(self, model: str = "", cli_path: str = ""):
        self._sdk = _import_sdk()
        self.model = model or ""
        self.cli_path = cli_path or ""
        self.chat = _Chat(self)
