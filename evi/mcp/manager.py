"""MCPManager — boot the MCP servers and expose their tools to the eVi agent.

For each enabled server in `~/.evi/mcp.json`:

1. Connect it over its transport (via the `mcp` Python SDK): spawn over **stdio**
   (`command`/`args`), or connect to a remote **http** (streamable-http) / **sse**
   endpoint by `url` (+ optional auth `headers`).
2. Call `initialize` and `list_tools`
3. Wrap each MCP tool as an `evi.tools.base.Tool` named `<server>.<tool>`
   and register it in `REGISTRY`. The wrapped `func` submits the actual call
   back through the bridge so the synchronous tool dispatch in `Agent.chat`
   can drive an async SDK.

Servers that fail to start are logged once and skipped — the rest of eVi
keeps working. On shutdown we close every server session and unregister
the tools we added.

The `mcp` package is imported lazily so users without that extra installed
can still run the rest of eVi.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from evi.mcp.bridge import MCPBridge
from evi.mcp.servers import MCPServer
from evi.proc import AGENT_ENV
from evi.tools.base import REGISTRY, Tool


logger = logging.getLogger(__name__)


@dataclass
class _LiveServer:
    name: str
    session: Any  # mcp.ClientSession at runtime
    stack: AsyncExitStack
    tool_names: list[str] = field(default_factory=list)


class MCPManager:
    """Lifecycle owner for all configured MCP servers.

    Construction is cheap; `start()` does the I/O. Safe to call `stop()` even
    if `start()` was never reached (e.g. user has no servers configured).
    """

    def __init__(
        self,
        servers: list[MCPServer],
        *,
        bridge: MCPBridge | None = None,
        call_timeout: float = 120.0,
        session_id: str = "",
        max_output_chars: int = 0,
    ) -> None:
        self.servers = servers
        self.bridge = bridge or MCPBridge()
        self._owns_bridge = bridge is None
        self._live: list[_LiveServer] = []
        self._call_timeout = call_timeout
        self.session_id = session_id
        # Cap on a single tool result's length (0 = unlimited); keeps a chatty
        # server from blowing the context window. Mirrors Claude Code's
        # --max-mcp-output-tokens (we measure characters).
        self._max_output_chars = max_output_chars
        self.started = False

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self.started:
            return
        if not any(s.enabled for s in self.servers):
            self.started = True
            return
        self.bridge.start()
        for s in self.servers:
            if not s.enabled:
                continue
            try:
                self._connect(s)
            except Exception as exc:
                logger.warning("MCP server %s failed to start: %s", s.name, exc)
        self.started = True

    def stop(self) -> None:
        # Unregister tools first so an in-flight Agent loop can't reach a
        # closed session.
        for live in self._live:
            for tname in live.tool_names:
                REGISTRY.pop(tname, None)

        # Close sessions on the bridge loop.
        for live in self._live:
            try:
                self.bridge.run(live.stack.aclose(), timeout=10)
            except Exception as exc:
                logger.debug("error closing MCP server %s: %s", live.name, exc)
        self._live.clear()

        if self._owns_bridge:
            self.bridge.stop()
        self.started = False

    # --- introspection ---------------------------------------------------

    def registered_tool_names(self) -> list[str]:
        out: list[str] = []
        for live in self._live:
            out.extend(live.tool_names)
        return out

    # --- internals -------------------------------------------------------

    def _connect(self, server: MCPServer) -> None:
        # Lazy import so the rest of eVi runs without the mcp extra installed.
        from mcp import ClientSession

        transport = (server.transport or "stdio").lower()
        # HTTP error statuses seen while connecting, for the failure message.
        http_errors: list[str] = []

        async def _open_streams(stack: AsyncExitStack):
            """Open the right client transport and return (read, write)."""
            if transport == "http":
                from mcp.client import streamable_http as sh

                # The new `streamable_http_client` (late 1.x and all of 2.x) takes
                # no headers: they ride on the HTTP client it is handed. Build that
                # with the SDK's own factory, which returns the client type this
                # SDK speaks (httpx on 1.x, httpx2 on 2.x). Only an SDK without the
                # new name falls back to the deprecated `streamablehttp_client`,
                # which takes headers itself.
                new_client = getattr(sh, "streamable_http_client", None)
                if new_client is not None:
                    make_http = getattr(sh, "create_mcp_http_client", None)
                    if make_http is None:
                        from mcp.shared._httpx_utils import create_mcp_http_client as make_http
                    http_client = await stack.enter_async_context(
                        make_http(headers=server.headers or None)
                    )
                    _record_http_errors(http_client, http_errors)
                    ctx = await stack.enter_async_context(
                        new_client(server.url, http_client=http_client)
                    )
                else:  # older 1.x
                    ctx = await stack.enter_async_context(
                        sh.streamablehttp_client(server.url, headers=server.headers or None)
                    )
                return ctx[0], ctx[1]  # (read, write, [get_session_id]) — SDK-version-safe
            if transport == "sse":
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(
                    sse_client(server.url, headers=server.headers or None)
                )
                return read, write
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            # Inject identifiers so a stdio MCP server knows it's running under
            # eVi (parity with Claude Code's SESSION_ID / CLAUDECODE=1). Merge
            # onto the SDK's safe default env so the child keeps PATH etc.
            # The server runs tools for an agent: EVI_AGENT=1 (evi.proc), last,
            # so a server's own env can't clear it. The SDK's default env keeps
            # only a few variables, so it is never inherited from eVi's.
            try:
                from mcp.client.stdio import get_default_environment

                base_env = dict(get_default_environment())
            except Exception:  # noqa: BLE001 — SDK without the helper
                import os

                base_env = dict(os.environ)
            child_env = {**base_env, **(server.env or {}), "EVI": "1", AGENT_ENV: "1"}
            if self.session_id:
                child_env["EVI_SESSION_ID"] = self.session_id
            params = StdioServerParameters(
                command=server.command, args=server.args, env=child_env
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write

        async def setup() -> tuple[AsyncExitStack, Any, list[Any]]:
            stack = AsyncExitStack()
            try:
                read, write = await _open_streams(stack)
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools_resp = await session.list_tools()
            except BaseException:
                # Failed, or timed out (MCPBridge.run cancels this task): close
                # what was opened (a spawned server, the HTTP client) instead of
                # leaking it.
                await stack.aclose()
                raise
            return stack, session, list(tools_resp.tools)

        # Remote transports may be slower to hand-shake than a local spawn.
        connect_timeout = 30 if transport == "stdio" else 45
        try:
            stack, session, tools = self.bridge.run(setup(), timeout=connect_timeout)
        except Exception as exc:
            # The SDK wraps transport errors in task-group exceptions, and 2.x
            # turns an HTTP error into a bare "Server returned an error
            # response": name the real cause (a wrong token is "HTTP 401").
            reason = _describe_error(exc)
            if http_errors and http_errors[-1] not in reason:
                reason += f" ({http_errors[-1]})"
            raise RuntimeError(reason) from exc
        live = _LiveServer(name=server.name, session=session, stack=stack)
        for mcp_tool in tools:
            evi_tool = self._wrap_tool(server.name, session, mcp_tool)
            REGISTRY[evi_tool.name] = evi_tool
            live.tool_names.append(evi_tool.name)
        self._live.append(live)
        logger.info(
            "MCP server %s connected — %d tools registered",
            server.name,
            len(live.tool_names),
        )

    def _wrap_tool(self, server_name: str, session: Any, mcp_tool: Any) -> Tool:
        bridge = self.bridge
        timeout = self._call_timeout
        cap = self._max_output_chars
        tname = mcp_tool.name
        full_name = f"{server_name}.{tname}"

        def call(**kwargs: Any) -> str:
            # A timeout cancels this task (MCPBridge.run), which ends the wait
            # here; _call_tool then tells the server to stop the work as well.
            result = bridge.run(_call_tool(session, tname, kwargs), timeout=timeout)
            return _truncate(_flatten_content(result), cap)

        # mcp 2.0 renamed the field inputSchema -> input_schema (the wire key
        # stays "inputSchema" via the pydantic alias). Read whichever exists.
        parameters = (
            getattr(mcp_tool, "input_schema", None)
            or getattr(mcp_tool, "inputSchema", None)
            or {"type": "object", "properties": {}}
        )

        return Tool(
            name=full_name,
            description=(mcp_tool.description or tname),
            parameters=parameters,
            func=call,
            category="mcp",
            trust="untrusted",
        )


async def _call_tool(session: Any, name: str, arguments: dict[str, Any]) -> Any:
    """`session.call_tool`, plus a `notifications/cancelled` when it's abandoned.

    mcp 2.x sends that courtesy cancel itself when the calling task is cancelled.
    mcp 1.x never does, so a timed-out call would keep running on the server;
    there we send it ourselves. 1.x numbers requests from `session._request_id`
    and reads it before its first await, so the value read just before the call
    is the id this call goes out with. 2.x sessions have no such attribute.
    """
    request_id = getattr(session, "_request_id", None)
    try:
        return await session.call_tool(name, arguments)
    except asyncio.CancelledError:
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            await _send_cancelled(session, request_id)
        raise


async def _send_cancelled(session: Any, request_id: int) -> None:
    """Best effort: tell an mcp 1.x server to stop request `request_id`."""
    try:
        import mcp.types as types

        note = types.ClientNotification(
            types.CancelledNotification(
                params=types.CancelledNotificationParams(
                    requestId=request_id, reason="timed out in eVi"
                )
            )
        )
        # Bounded: on shutdown the transport may already be gone.
        await asyncio.wait_for(session.send_notification(note), timeout=2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not send an MCP cancellation for request %s: %s", request_id, exc)


def _record_http_errors(client: Any, sink: list[str]) -> None:
    """Keep the latest HTTP error status `client` receives in `sink`, e.g.
    "HTTP 401 Unauthorized" (best effort: httpx and httpx2 both take hooks)."""

    async def _note(response: Any) -> None:
        if response.status_code >= 400:
            reason = getattr(response, "reason_phrase", "") or ""
            sink[:] = [f"HTTP {response.status_code} {reason}".strip()]

    try:
        hooks = dict(client.event_hooks)
        client.event_hooks = {**hooks, "response": [*hooks.get("response", []), _note]}
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not watch MCP HTTP statuses: %s", exc)


def _describe_error(exc: BaseException) -> str:
    """One line naming why a connect failed: the leaf errors inside the SDK's
    exception groups, with the HTTP status of an HTTP error."""
    if isinstance(exc, BaseExceptionGroup):
        leaves = [_describe_error(sub) for sub in exc.exceptions]
        return "; ".join(dict.fromkeys(leaves))
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        reason = getattr(exc.response, "reason_phrase", "") or ""  # type: ignore[attr-defined]
        return f"HTTP {status} {reason}".strip()
    text = str(exc) or ("timed out" if isinstance(exc, TimeoutError) else "")
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _truncate(text: str, cap: int) -> str:
    """Clip an MCP result to `cap` characters (0 = unlimited), with a marker
    so the model knows the output was elided rather than ending naturally."""
    if cap <= 0 or len(text) <= cap:
        return text
    elided = len(text) - cap
    return text[:cap] + f"\n…[{elided} chars truncated — MCP output cap]"


def _flatten_content(result: Any) -> str:
    """Turn an MCP call_tool result into a single string for the LLM.

    The MCP SDK returns a `CallToolResult` with a `content` list of typed
    items (TextContent, ImageContent, …). We concatenate text content;
    everything else gets a `[<type> omitted]` placeholder so the model knows
    something was elided rather than silently dropping it.
    """
    content = getattr(result, "content", None) or []
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
            continue
        kind = getattr(item, "type", type(item).__name__)
        parts.append(f"[{kind} omitted]")
    # mcp 2.x renamed the field isError -> is_error (the wire key is unchanged).
    if getattr(result, "is_error", None) or getattr(result, "isError", False):
        text = "\n".join(parts) if parts else "(no error message)"
        # A server that follows eVi's own convention (`evi mcp serve`) already
        # says so; don't stutter "ERROR: ERROR: ...".
        return text if text.startswith("ERROR:") else "ERROR: " + text
    return "\n".join(parts) if parts else "(no content)"
