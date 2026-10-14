"""Expose eVi's own tools, memory, and commands as an MCP server (Phases 53–54).

eVi has long been an MCP *client* (it consumes other servers — see
`evi/mcp/bridge.py`). This is the inverse: run eVi as an MCP *server* so other
agents — Claude Desktop, Cursor, Cline, Continue — can reach into eVi. It flips
the integration story: instead of building one bridge per app, the app's
existing MCP client connects to eVi.

What's exposed (all from eVi's own single sources of truth):
- **Tools** — entries from `evi.tools.base.REGISTRY` (same name/description/
  JSON-schema the agent already uses), filtered by category + optional
  allow-list. shell/computer/code-write are NOT in the default set.
- **Resources** — your long-term memory entries as `evi://memory/<name>`.
- **Prompts** — your saved slash-command templates (`~/.evi/commands/*.md`).

Transports: **stdio** (default; what a desktop MCP client spawns) and
**streamable HTTP** (`--http`, with a bearer `--token` for remote/untrusted
clients; required for a non-loopback `--host`, and without one only loopback
Host/Origin headers are answered).

Run it with `evi mcp serve` (see also `evi mcp serve-config`).
"""

from __future__ import annotations

from evi.tools.base import REGISTRY, Tool

# Curated default surface — high-value, relatively-safe tools to hand an
# external agent. `evi mcp serve --categories ...` / `--tools ...` widen or
# narrow it. We deliberately do NOT expose shell/computer/code-write by default.
DEFAULT_CATEGORIES: tuple[str, ...] = ("memory", "index", "calendar", "git")

MEMORY_URI_PREFIX = "evi://memory/"

def _populate_registry() -> None:
    """Populate REGISTRY with every built-in tool. Delegates to the canonical
    registrar so the import set lives in exactly one place (evi.tools)."""
    from evi.tools import register_builtin_tools

    register_builtin_tools()


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
    """Map eVi tools to MCP tool spec dicts (name/description/inputSchema).
    Pure + JSON-able — the schema is reused verbatim from the eVi tool."""
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
    blocked = _destructive_block(tool, arguments or {})
    if blocked is not None:
        return blocked
    return tool.call(arguments or {})


def is_error_text(text: str) -> bool:
    """True for a `dispatch` result that reports a failure: eVi's `ERROR: ...`
    convention (unknown tool, a tool that raised or reported an error; the
    agent reads tool results the same way) or a destructive-command guard
    block. Served to MCP clients with isError set. Output that merely begins
    with the word (a file whose first line is "ERROR codes") is not a failure."""
    return text.startswith(("ERROR:", "BLOCKED by the destructive-command guard"))


# MCP has no interactive permission prompt, so a matched destructive command is
# denied here (headless fail-safe) — the same posture the agent takes when it has
# no permission callback. Honours the [auto] block_destructive/allow/disable config.
# Which calls run a shell line comes from `evi.permissions.SHELL_EXEC_TOOLS`, the
# one map the agent's guard and decide() also use.


def _destructive_block(tool: Tool, arguments: dict) -> str | None:
    from evi.permissions import shell_command_of

    cmd = shell_command_of(tool.name, getattr(tool, "category", ""), arguments)
    if cmd is None:
        return None
    from evi.config import Config
    from evi.shell_guard import destructive_hit

    try:
        auto = Config.load().auto
    except Exception:  # noqa: BLE001
        return None
    if not getattr(auto, "block_destructive", True):
        return None
    hit = destructive_hit(
        cmd,
        disable_rules=getattr(auto, "destructive_disable_rules", []) or [],
        allow=getattr(auto, "destructive_allow", []) or [],
    )
    if hit is None:
        return None
    return (
        f"BLOCKED by the destructive-command guard: {hit.reason}. "
        "Not run (no interactive approval available over MCP)."
    )


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
    """Return the command body with `{args}` substituted, or None if unknown.

    `@file` refs are never inlined here: the expansion goes back to the MCP
    client, so inlining would hand it local files outside the curated tool
    surface (the client's args are plain text either way)."""
    from evi.commands import CommandStore

    args = (arguments or {}).get("args", "") or ""
    return CommandStore().expand(name, args, inline=False)


# --- server construction -------------------------------------------------


def _field_name(model, new: str, old: str) -> str:
    """The name `model` declares for a field mcp 2.x renamed from `old` to `new`."""
    return new if new in getattr(model, "model_fields", {}) else old


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

    # --- version-independent handler bodies ------------------------------
    # mcp 2.x renamed the pydantic fields `inputSchema` -> `input_schema` and
    # `isError` -> `is_error` (the wire keys are unchanged). Build models by
    # whichever field name this SDK declares rather than leaning on the alias.
    schema_field = _field_name(types.Tool, "input_schema", "inputSchema")
    error_field = _field_name(types.CallToolResult, "is_error", "isError")

    def _tool_list() -> list[types.Tool]:
        return [
            types.Tool(name=s["name"], description=s["description"],
                       **{schema_field: s["inputSchema"]})
            for s in specs
        ]

    async def _call_result(name: str, arguments: dict | None) -> types.CallToolResult:
        # A failure the model should read and react to (unknown tool, the
        # destructive-command guard, a tool's own ERROR) is a tool result with
        # the error flag set, never a protocol error, on both SDK lines.
        text = await anyio.to_thread.run_sync(lambda: dispatch(by_name, name, arguments))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            **{error_field: is_error_text(text)},
        )

    def _resource_list() -> list[types.Resource]:
        return [types.Resource(**s) for s in memory_resource_specs()]

    def _prompt_message(body: str) -> "types.GetPromptResult":
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user", content=types.TextContent(type="text", text=body)
                )
            ]
        )

    def _prompt_list() -> list[types.Prompt]:
        return [
            types.Prompt(
                name=s["name"],
                description=s["description"],
                arguments=[types.PromptArgument(**a) for a in s["arguments"]],
            )
            for s in command_prompt_specs()
        ]

    # mcp 1.x registers handlers with per-method decorators; 2.0 removed them
    # in favour of the low-level Server.add_request_handler(method, params, fn).
    # Both drive the same handler bodies above.
    if hasattr(server, "list_tools"):  # mcp 1.x
        @server.list_tools()
        async def _lt() -> list[types.Tool]:
            return _tool_list()

        @server.call_tool()
        async def _ct(name: str, arguments: dict | None) -> types.CallToolResult:
            return await _call_result(name, arguments)

        @server.list_resources()
        async def _lr() -> list[types.Resource]:
            return _resource_list()

        @server.read_resource()
        async def _rr(uri) -> str:
            return await anyio.to_thread.run_sync(lambda: read_memory_resource(str(uri)))

        @server.list_prompts()
        async def _lp() -> list[types.Prompt]:
            return _prompt_list()

        @server.get_prompt()
        async def _gp(name: str, arguments: dict | None) -> types.GetPromptResult:
            body = await anyio.to_thread.run_sync(
                lambda: expand_command_prompt(name, arguments)
            )
            if body is None:
                raise ValueError(f"unknown prompt {name!r}")
            return _prompt_message(body)
    else:  # mcp >= 2.0 — raw request handlers, (context, params) -> Result
        page = types.PaginatedRequestParams

        async def _lt(_ctx, _params) -> types.ListToolsResult:
            return types.ListToolsResult(tools=_tool_list())

        async def _ct(_ctx, params) -> types.CallToolResult:
            return await _call_result(params.name, params.arguments)

        async def _lr(_ctx, _params) -> types.ListResourcesResult:
            return types.ListResourcesResult(resources=_resource_list())

        async def _rr(_ctx, params) -> types.ReadResourceResult:
            text = await anyio.to_thread.run_sync(
                lambda: read_memory_resource(str(params.uri))
            )
            return types.ReadResourceResult(
                contents=[types.TextResourceContents(uri=params.uri, text=text)]
            )

        async def _lp(_ctx, _params) -> types.ListPromptsResult:
            return types.ListPromptsResult(prompts=_prompt_list())

        async def _gp(_ctx, params) -> types.GetPromptResult:
            body = await anyio.to_thread.run_sync(
                lambda: expand_command_prompt(params.name, params.arguments)
            )
            if body is None:
                raise ValueError(f"unknown prompt {params.name!r}")
            return _prompt_message(body)

        server.add_request_handler("tools/list", page, _lt)
        server.add_request_handler("tools/call", types.CallToolRequestParams, _ct)
        server.add_request_handler("resources/list", page, _lr)
        server.add_request_handler("resources/read", types.ReadResourceRequestParams, _rr)
        server.add_request_handler("prompts/list", page, _lp)
        server.add_request_handler("prompts/get", types.GetPromptRequestParams, _gp)

    return server


# --- HTTP transport ------------------------------------------------------

# The names a tokenless HTTP server answers to, on any port.
_LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")


def loopback_security_settings(bind_host: str = ""):
    """The SDK's DNS-rebinding guard, limited to loopback Host and Origin.

    A web page can make the browser send requests to 127.0.0.1 under its own
    hostname (DNS rebinding), but it can't forge the Host header, so a request
    for `evil.example` is refused (421), as is a browser Origin that isn't a
    loopback page (403). Non-browser clients send no Origin and pass. A
    loopback `bind_host` the defaults don't name (127.0.0.2, an expanded ::1)
    is answered to as well, or its own clients would be refused."""
    from mcp.server.transport_security import TransportSecuritySettings

    names = list(dict.fromkeys([*_LOOPBACK_HOSTS, *_host_header_names(bind_host)]))
    hosts = [h for name in names for h in (name, f"{name}:*")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{h}" for scheme in ("http", "https") for h in hosts],
    )


def _loopback_ip(host: str):
    """`host` as a loopback ip_address, "localhost" as is, else None."""
    import ipaddress

    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h == "localhost":
        return h
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:  # a hostname, or "" (all interfaces)
        return None
    return ip if ip.is_loopback else None


def _host_header_names(host: str) -> list[str]:
    """How a client names a loopback bind `host` in its Host header (IPv6 in
    brackets, as written and compressed); [] for anything else."""
    ip = _loopback_ip(host)
    if ip is None:
        return []
    if isinstance(ip, str) or ip.version == 4:  # "localhost", or an IPv4 address
        return [str(ip)]
    written = (host or "").strip().lower().strip("[]")
    return list(dict.fromkeys([f"[{written}]", f"[{ip.compressed}]"]))


def is_loopback_bind(host: str) -> bool:
    """True if binding to `host` keeps the server on this machine."""
    return _loopback_ip(host) is not None


def build_http_app(
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
    allow: tuple[str, ...] | None = None,
    token: str = "",
    bind_host: str = "",
):
    """Build a Starlette ASGI app serving the MCP server over streamable HTTP
    at `/mcp`. If `token` is set, requests must carry `Authorization: Bearer
    <token>` — the recommended gate for any non-localhost exposure. Without a
    token the app only answers to loopback Host/Origin headers (see
    `loopback_security_settings`), which a tokenless server is bound to;
    `bind_host` is the address it is served on, when that isn't 127.0.0.1."""
    import secrets as _secrets
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    server = build_server(categories, allow)
    # Tokenless, the Host/Origin check is all that keeps a DNS-rebinding page
    # away from these tools (the web server guards itself the same way). With a
    # token, the token is the control, and a LAN or proxy hostname must work.
    security = None if token else loopback_security_settings(bind_host)
    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, security_settings=security
    )

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
    """Run the eVi MCP server. Default transport is **stdio** (what a desktop
    MCP client spawns). With `http=True`, serve streamable HTTP on host:port
    (optionally bearer-token gated). Raises ValueError for a non-loopback
    `host` without a `token`: anyone who can reach the port could call the
    exposed tools."""
    if http:
        if not token and not is_loopback_bind(host):
            raise ValueError(
                f"refusing to serve MCP over HTTP on {host!r} without a token: anyone "
                "who can reach it could call eVi's tools. Pass --token, or bind to "
                "127.0.0.1."
            )
        import uvicorn

        uvicorn.run(build_http_app(categories, allow, token, bind_host=host), host=host,
                    port=port, log_level="warning")
        return

    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(categories, allow)

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_run)
