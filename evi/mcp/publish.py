"""Expose Evi's own tools, memory, and commands as an MCP server (Phases 53–54).

Evi has long been an MCP *client* (it consumes other servers — see
`evi/mcp/bridge.py`). This is the inverse: run Evi as an MCP *server* so other
agents — Claude Desktop, Cursor, Cline, Continue — can reach into Evi. It flips
the integration story: instead of building one bridge per app, the app's
existing MCP client connects to Evi.

What's exposed (all from Evi's own single sources of truth):
- **Tools** — entries from `evi.tools.base.REGISTRY` (same name/description/
  JSON-schema the agent already uses), filtered by category + optional
  allow-list. shell/computer/code-write are NOT in the default set.
- **Resources** — your long-term memory entries as `evi://memory/<name>`.
- **Prompts** — your saved slash-command templates (`~/.evi/commands/*.md`).

Transports: **stdio** (default; what a desktop MCP client spawns) and
**streamable HTTP** (`--http`, with an optional bearer `--token` for
remote/untrusted clients).

Run it with `evi mcp serve` (see also `evi mcp serve-config`).
"""

from __future__ import annotations

import importlib

from evi.tools.base import REGISTRY, Tool

# Curated default surface — high-value, relatively-safe tools to hand an
# external agent. `evi mcp serve --categories ...` / `--tools ...` widen or
# narrow it. We deliberately do NOT expose shell/computer/code-write by default.
DEFAULT_CATEGORIES: tuple[str, ...] = ("memory", "index", "calendar", "git")

MEMORY_URI_PREFIX = "evi://memory/"

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


# --- tools ---------------------------------------------------------------


def selected_tools(
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    allow: tuple[str, ...] | None = None,
) -> list[Tool]:
    """Tools (from REGISTRY) whose category is in `categories`. If `allow` is
    given, additionally restrict to those exact tool names (per-tool
    allow-listing). The tool modules are imported first so the registry is full.
    """
    _populate_registry()
    cats = set(categories)
    allowset = set(allow) if allow else None
    return sorted(
        (
            t for t in REGISTRY.values()
            if t.category in cats and (allowset is None or t.name in allowset)
        ),
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


# --- resources (long-term memory) ----------------------------------------


def memory_resource_specs() -> list[dict]:
    """One MCP resource per memory entry: uri/name/description/mimeType."""
    from evi.memory import MemoryStore

    return [
        {
            "uri": f"{MEMORY_URI_PREFIX}{e.name}",
            "name": e.name,
            "description": e.summary,
            "mimeType": "text/markdown",
        }
        for e in MemoryStore().list()
    ]


def read_memory_resource(uri: str) -> str:
    """Read a memory entry by its `evi://memory/<name>` URI."""
    from evi.memory import MemoryStore

    if not uri.startswith(MEMORY_URI_PREFIX):
        raise ValueError(f"not a memory resource: {uri!r}")
    name = uri[len(MEMORY_URI_PREFIX):]
    return MemoryStore().read(name)


# --- prompts (saved slash-command templates) -----------------------------


def command_prompt_specs() -> list[dict]:
    """One MCP prompt per `~/.evi/commands/*.md`. Each takes an optional
    free-text `args` argument (substituted for `{args}` in the template)."""
    from evi.commands import CommandStore

    return [
        {
            "name": c.name,
            "description": c.summary,
            "arguments": [
                {"name": "args", "description": "Free-text args for {args}", "required": False}
            ],
        }
        for c in CommandStore().list()
    ]


def expand_command_prompt(name: str, arguments: dict | None) -> str | None:
    """Return the command body with `{args}` substituted, or None if unknown."""
    from evi.commands import CommandStore

    args = (arguments or {}).get("args", "") or ""
    return CommandStore().expand(name, args)


# --- server construction -------------------------------------------------


def build_server(
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    allow: tuple[str, ...] | None = None,
):
    """Construct (but don't run) the MCP `Server` exposing the selected tools,
    plus memory resources and command prompts."""
    import anyio
    import mcp.types as types
    from mcp.server import Server

    from evi import __version__

    tools = selected_tools(categories, allow)
    by_name = {t.name: t for t in tools}
    specs = mcp_tool_specs(tools)
    server: Server = Server("evi", version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [types.Tool(**s) for s in specs]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        text = await anyio.to_thread.run_sync(lambda: dispatch(by_name, name, arguments))
        return [types.TextContent(type="text", text=text)]

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return [types.Resource(**s) for s in memory_resource_specs()]

    @server.read_resource()
    async def _read_resource(uri) -> str:
        return await anyio.to_thread.run_sync(lambda: read_memory_resource(str(uri)))

    @server.list_prompts()
    async def _list_prompts() -> list[types.Prompt]:
        return [
            types.Prompt(
                name=s["name"],
                description=s["description"],
                arguments=[types.PromptArgument(**a) for a in s["arguments"]],
            )
            for s in command_prompt_specs()
        ]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
        body = await anyio.to_thread.run_sync(lambda: expand_command_prompt(name, arguments))
        if body is None:
            raise ValueError(f"unknown prompt {name!r}")
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user", content=types.TextContent(type="text", text=body)
                )
            ]
        )

    return server


# --- HTTP transport ------------------------------------------------------


def build_http_app(
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    allow: tuple[str, ...] | None = None,
    token: str = "",
):
    """Build a Starlette ASGI app serving the MCP server over streamable HTTP
    at `/mcp`. If `token` is set, requests must carry `Authorization: Bearer
    <token>` — the recommended gate for any non-localhost exposure."""
    import secrets as _secrets
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    server = build_server(categories, allow)
    manager = StreamableHTTPSessionManager(app=server, json_response=True)

    async def handle_mcp(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    middleware = []
    if token:
        async def _auth(request, call_next):
            header = request.headers.get("Authorization", "")
            provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
            if not (provided and _secrets.compare_digest(provided, token)):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        middleware.append(Middleware(BaseHTTPMiddleware, dispatch=_auth))

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan,
                     middleware=middleware)


def serve(
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    allow: tuple[str, ...] | None = None,
    *,
    http: bool = False,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
) -> None:
    """Run the Evi MCP server. Default transport is **stdio** (what a desktop
    MCP client spawns). With `http=True`, serve streamable HTTP on host:port
    (optionally bearer-token gated)."""
    if http:
        import uvicorn

        uvicorn.run(build_http_app(categories, allow, token), host=host, port=port,
                    log_level="warning")
        return

    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(categories, allow)

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_run)
