"""Expose Evi's own tools as an MCP server (Phase 53).

Evi has long been an MCP *client* (it consumes other servers — see
`evi/mcp/bridge.py`). This is the inverse: run Evi as an MCP *server* over
stdio so other agents — Claude Desktop, Cursor, Cline, Continue — can reach
into Evi's tools (memory, semantic index, calendar, git, …). It flips the
integration story: instead of building one bridge per app, the app's existing
MCP client connects to Evi.

Each exposed tool is just a thin wrapper over an entry in
`evi.tools.base.REGISTRY` — same name, description, and JSON-schema parameters
the agent already uses — so there's a single source of truth.

Run it with `evi mcp serve` (that's the command an MCP client spawns).
"""

from __future__ import annotations

import importlib

from evi.tools.base import REGISTRY, Tool

# Curated default surface. These are the high-value, relatively-safe tools to
# hand an external agent; `evi mcp serve --categories ...` can widen it. We
# deliberately do NOT expose shell/computer/code-write by default.
DEFAULT_CATEGORIES: tuple[str, ...] = ("memory", "index", "calendar", "git")

# Tool modules to import so REGISTRY is populated (importing the package alone
# doesn't pull these in). Optional-dep failures are tolerated.
_TOOL_MODULES = (
    "memory", "index", "calendar", "git", "fs", "code",
    "websearch", "pdf", "sqlite", "skills",
)


def _populate_registry() -> None:
    for mod in _TOOL_MODULES:
        try:
            importlib.import_module(f"evi.tools.{mod}")
        except Exception:  # noqa: BLE001 — a missing optional dep just omits its tools
            pass


def selected_tools(categories: tuple[str, ...] = DEFAULT_CATEGORIES) -> list[Tool]:
    """Tools (from REGISTRY) whose category is in `categories`, after importing
    the tool modules so the registry is populated."""
    _populate_registry()
    cats = set(categories)
    return sorted(
        (t for t in REGISTRY.values() if t.category in cats),
        key=lambda t: t.name,
    )


def mcp_tool_specs(tools: list[Tool]) -> list[dict]:
    """Map Evi tools to MCP tool spec dicts (name/description/inputSchema).
    Pure + JSON-able — the schema is reused verbatim from the Evi tool."""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.parameters}
        for t in tools
    ]


def dispatch(by_name: dict[str, Tool], name: str, arguments: dict | None) -> str:
    """Invoke a tool by name and return its visible text. Unknown tool names
    return an error string rather than raising (the agent sees the message)."""
    tool = by_name.get(name)
    if tool is None:
        return f"ERROR: unknown tool {name!r}"
    return tool.call(arguments or {})


def build_server(categories: tuple[str, ...] = DEFAULT_CATEGORIES):
    """Construct (but don't run) the MCP `Server` exposing the selected tools."""
    import anyio
    import mcp.types as types
    from mcp.server import Server

    from evi import __version__

    tools = selected_tools(categories)
    by_name = {t.name: t for t in tools}
    specs = mcp_tool_specs(tools)
    server: Server = Server("evi", version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [types.Tool(**s) for s in specs]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        # Evi tools are synchronous (and some do I/O), so run off the event loop.
        text = await anyio.to_thread.run_sync(lambda: dispatch(by_name, name, arguments))
        return [types.TextContent(type="text", text=text)]

    return server


def serve(categories: tuple[str, ...] = DEFAULT_CATEGORIES) -> None:
    """Run the Evi MCP server over stdio (blocking). This is the entry point an
    MCP client launches as a subprocess."""
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(categories)

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_run)
