"""Claude Agent SDK driver — Claude Code over the local ``claude`` CLI, presented
to eVi as an OpenAI ``chat.completions`` client via the shared shim in
:mod:`evi.llm.cli_agent`.

Auth is the local ``claude`` CLI login (Max/Pro subscription) — nothing here reads
an API key. The generic parts (OpenAI-shaped chunks, the async→sync bridge, the
client shell) live in ``cli_agent``; this module holds only the Claude-specific
bits: importing ``claude-agent-sdk``, translating eVi's OpenAI messages into the
SDK's streaming-input format, and running one SDK turn with a ``can_use_tool``
interceptor so **eVi keeps driving its own tools** (the SDK proposes one tool call,
eVi executes it — see the round-trip in ``docs``). See
``evi/backends/claude_agent.py`` for the backend that returns this client.
"""

from __future__ import annotations

import json
from typing import Any

from evi.llm import cli_agent
from evi.llm.cli_agent import (
    CliAgentClient,
    CliUnavailable,
    delta_chunk,
    flatten_content as _as_text,
    tool_call_delta,
    usage_chunk,
)

# The in-process MCP server prefixes tool names ``mcp__<server>__<tool>``.
_MCP_SERVER = "evi"
_TOOL_PREFIX = f"mcp__{_MCP_SERVER}__"


class ClaudeAgentUnavailable(CliUnavailable):
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


def translate_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI messages -> (system_prompt, [SDK streaming-input message dicts]).

    System messages become ``system_prompt``. Prior tool interactions are
    rendered as PLAIN TEXT rather than replayed as structured
    ``tool_use``/``tool_result`` blocks: replaying a structured tool_use that
    names an in-process MCP tool trips the CLI's "tool use concurrency" guard.
    eVi still *drives* tools (a live call is intercepted on the current turn) —
    only the on-the-wire representation of past calls changes. Content is always
    a list of blocks: the CLI scans assistant content with
    ``content.some(r => r.type === "tool_use")``, which throws on a bare string.
    """
    system_parts: list[str] = []
    sdk_msgs: list[dict] = []
    id_to_name: dict[str, str] = {}

    def _push(role: str, text: str) -> None:
        if not text:
            return
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


async def _run_claude_turn(sdk, *, model, system_prompt, sdk_msgs, tools, out):
    """One SDK turn. Streams text chunks onto `out`; if Claude requests a tool,
    captures the first call (deny+interrupt) and emits it as a tool_calls chunk."""
    server, has_tools = _build_tool_server(sdk, tools)
    captured: dict[str, Any] = {}

    if has_tools:
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
                        out.put(delta_chunk(content=b.text))
                    elif isinstance(b, sdk.ThinkingBlock) and getattr(b, "thinking", ""):
                        out.put(delta_chunk(content=f"<think>{b.thinking}</think>"))
            elif isinstance(msg, sdk.ResultMessage):
                usage = msg.usage
                if msg.is_error and not captured:
                    out.put(cli_agent.error(RuntimeError(msg.result or "claude_agent error")))
                    return
    except Exception as exc:  # noqa: BLE001
        # A captured tool call surfaces the interrupt as an error result — expected.
        if not captured:
            out.put(cli_agent.error(exc))
            return
    finally:
        _aclose = getattr(agen, "aclose", None)
        if _aclose is not None:
            try:
                await _aclose()
            except Exception:  # noqa: BLE001
                pass

    if captured:
        out.put(tool_call_delta(0, captured["id"], captured["name"], json.dumps(captured["input"])))
        out.put(delta_chunk(finish_reason="tool_calls"))
    else:
        out.put(delta_chunk(finish_reason="stop"))
    u = usage or {}
    out.put(usage_chunk(u.get("input_tokens", 0), u.get("output_tokens", 0)))


class _ClaudeDriver:
    """cli_agent driver: translate messages, then run one async SDK turn."""

    def __init__(self):
        # Eager import so selecting the backend fails fast if the SDK/CLI is
        # missing (matches the pre-refactor behaviour the tests rely on).
        self._sdk = _import_sdk()

    def run_turn(self, *, model, messages, tools, out):
        import asyncio  # noqa: PLC0415

        system_prompt, sdk_msgs = translate_messages(messages or [])
        asyncio.run(_run_claude_turn(
            self._sdk, model=model or None, system_prompt=system_prompt,
            sdk_msgs=sdk_msgs, tools=tools, out=out,
        ))


class ClaudeAgentClient(CliAgentClient):
    """OpenAI-client-shaped Claude backend over the local ``claude`` CLI."""

    def __init__(self, model: str = ""):
        super().__init__(_ClaudeDriver(), model)
