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

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from evi.mcp.bridge import MCPBridge
from evi.mcp.servers import MCPServer
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
    ) -> None:
        self.servers = servers
        self.bridge = bridge or MCPBridge()
        self._owns_bridge = bridge is None
        self._live: list[_LiveServer] = []
        self._call_timeout = call_timeout
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

        async def _open_streams(stack: AsyncExitStack):
            """Open the right client transport and return (read, write)."""
            if transport == "http":
                from mcp.client.streamable_http import streamablehttp_client

                ctx = await stack.enter_async_context(
                    streamablehttp_client(server.url, headers=server.headers or None)
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

            params = StdioServerParameters(
                command=server.command, args=server.args, env=server.env or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write

        async def setup() -> tuple[AsyncExitStack, Any, list[Any]]:
            stack = AsyncExitStack()
            read, write = await _open_streams(stack)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_resp = await session.list_tools()
            return stack, session, list(tools_resp.tools)

        # Remote transports may be slower to hand-shake than a local spawn.
        connect_timeout = 30 if transport == "stdio" else 45
        stack, session, tools = self.bridge.run(setup(), timeout=connect_timeout)
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
        tname = mcp_tool.name
        full_name = f"{server_name}.{tname}"

        def call(**kwargs: Any) -> str:
            async def _call() -> Any:
                return await session.call_tool(tname, kwargs)

            result = bridge.run(_call(), timeout=timeout)
            return _flatten_content(result)

        parameters = (
            getattr(mcp_tool, "inputSchema", None)
            or {"type": "object", "properties": {}}
        )

        return Tool(
            name=full_name,
            description=(mcp_tool.description or tname),
            parameters=parameters,
            func=call,
            category="mcp",
        )


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
    if getattr(result, "isError", False):
        return "ERROR: " + ("\n".join(parts) if parts else "(no error message)")
    return "\n".join(parts) if parts else "(no content)"
