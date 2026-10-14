"""eVi's MCP layer against the real SDK: no transport or session monkeypatching.

CI runs this file (with the other tests/test_mcp* files) on mcp 1.30.x, 2.0.x
and the newest release the [mcp] pin admits, so each test here talks to a real
server over a real loopback socket:

- the streamable-HTTP client sends the configured headers and round-trips calls
  (a fake transport once hid that every `transport="http"` server failed), and
  a failed connect logs its real cause (an HTTP 401, a refused connection);
- `evi mcp serve --http` refuses DNS-rebinding requests (`Host: evil.example`)
  when it has no token, and refuses a non-loopback bind without one;
- a hung tool times out, and the server is told to cancel it;
- tool failures are served with the MCP error flag set.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")

from evi.mcp import publish  # noqa: E402
from evi.mcp.bridge import MCPBridge  # noqa: E402
from evi.mcp.manager import MCPManager  # noqa: E402
from evi.mcp.servers import MCPServer  # noqa: E402
from evi.tools.base import REGISTRY, Tool  # noqa: E402

CATEGORY = "sdkcompat-test"
TOKEN = "s3cret-token"
ERRCODES = "ERROR codes reference\n404 not found"


# ---- helpers ----------------------------------------------------------------


class _Served:
    """Serve an ASGI app with uvicorn on a real 127.0.0.1 socket, in a thread."""

    def __init__(self, app) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
            lifespan="on", timeout_graceful_shutdown=2,
        ))
        self._thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self._sock]}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def __enter__(self) -> "_Served":
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test MCP server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=15)
        self._sock.close()


def _asgi_app(server):
    """A bare streamable-HTTP app around an SDK `Server` (no eVi guards)."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(app=server, json_response=True)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)


def _hang_server(started: threading.Event, cancelled: threading.Event):
    """An SDK server with one tool, `hang`, that never returns on its own and
    records whether the server cancelled it. Registered for either SDK line."""
    import anyio
    import mcp.types as types
    from mcp.server import Server

    schema_field = "input_schema" if "input_schema" in types.Tool.model_fields else "inputSchema"
    tool = types.Tool(name="hang", description="never returns",
                      **{schema_field: {"type": "object", "properties": {}}})

    async def hang() -> types.CallToolResult:
        started.set()
        try:
            await anyio.sleep(60)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise
        return types.CallToolResult(content=[types.TextContent(type="text", text="done")])

    server = Server("hang-test")
    if hasattr(server, "list_tools"):  # mcp 1.x decorators
        @server.list_tools()
        async def _lt():
            return [tool]

        @server.call_tool()
        async def _ct(_name, _arguments):
            return await hang()
    else:  # mcp 2.x
        async def _lt2(_ctx, _params):
            return types.ListToolsResult(tools=[tool])

        async def _ct2(_ctx, _params):
            return await hang()

        server.add_request_handler("tools/list", types.PaginatedRequestParams, _lt2)
        server.add_request_handler("tools/call", types.CallToolRequestParams, _ct2)
    return server


def _initialize_body() -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "evi-test", "version": "0"},
        },
    }


_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def served_tools(monkeypatch):
    """Two eVi tools in a private category, served by `publish`."""
    def _boom() -> str:
        raise ValueError("kaput")

    monkeypatch.setitem(REGISTRY, "echo", Tool(
        name="echo", description="echo back",
        parameters={"type": "object", "properties": {"x": {"type": "string"}},
                    "required": ["x"]},
        func=lambda x: f"got {x}", category=CATEGORY,
    ))
    monkeypatch.setitem(REGISTRY, "boom", Tool(
        name="boom", description="always fails",
        parameters={"type": "object", "properties": {}},
        func=_boom, category=CATEGORY,
    ))
    # Succeeds, with output that happens to start with the word (a file read).
    monkeypatch.setitem(REGISTRY, "errcodes", Tool(
        name="errcodes", description="reads a file about error codes",
        parameters={"type": "object", "properties": {}},
        func=lambda: ERRCODES, category=CATEGORY,
    ))
    return (CATEGORY,)


def _result_is_error(result) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", False))


# ---- streamable-HTTP client: headers + a real round trip --------------------


def test_http_client_sends_headers_and_round_trips(served_tools, caplog):
    """The server demands a bearer token, so a connection only succeeds if the
    configured headers really reach it."""
    app = publish.build_http_app(served_tools, token=TOKEN)
    with _Served(app) as srv:
        mgr = MCPManager([MCPServer(
            name="remote", transport="http", url=srv.url,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )])
        try:
            mgr.start()
            assert "remote.echo" in mgr.registered_tool_names(), caplog.text
            assert REGISTRY["remote.echo"].call({"x": "hi"}) == "got hi"
            # The schema survived the trip (input_schema on 2.x, inputSchema on 1.x).
            assert REGISTRY["remote.echo"].parameters["required"] == ["x"]
        finally:
            mgr.stop()
    assert "remote.echo" not in REGISTRY


@pytest.mark.parametrize("headers", [None, {"Authorization": "Bearer wrong"}])
def test_http_client_without_the_token_is_refused(served_tools, caplog, headers):
    # The other half of the proof above: the token gate is real. The log says
    # why (the SDK reports "unhandled errors in a TaskGroup" on 1.x and "Server
    # returned an error response" on 2.x, neither naming the 401).
    app = publish.build_http_app(served_tools, token=TOKEN)
    with _Served(app) as srv, caplog.at_level("WARNING", logger="evi.mcp.manager"):
        mgr = MCPManager([MCPServer(name="remote", transport="http", url=srv.url,
                                    headers=headers or {})])
        try:
            mgr.start()
            assert mgr.registered_tool_names() == []
        finally:
            mgr.stop()
    failure = [r.getMessage() for r in caplog.records if "failed to start" in r.getMessage()]
    assert len(failure) == 1, caplog.text
    assert "HTTP 401 Unauthorized" in failure[0], failure[0]
    assert "TaskGroup" not in failure[0], failure[0]


def test_http_connect_failure_names_the_cause(caplog):
    # Nothing listens on the port: the log names the connect error, not the
    # task group that wrapped it.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with caplog.at_level("WARNING", logger="evi.mcp.manager"):
        mgr = MCPManager([MCPServer(name="gone", transport="http",
                                    url=f"http://127.0.0.1:{port}/mcp")])
        try:
            mgr.start()
        finally:
            mgr.stop()
    failure = [r.getMessage() for r in caplog.records if "failed to start" in r.getMessage()]
    assert len(failure) == 1, caplog.text
    assert "ConnectError" in failure[0] and "TaskGroup" not in failure[0], failure[0]


def test_served_tool_errors_carry_the_error_flag(served_tools):
    app = publish.build_http_app(served_tools, token=TOKEN)
    with _Served(app) as srv:
        mgr = MCPManager([MCPServer(
            name="remote", transport="http", url=srv.url,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )])
        try:
            mgr.start()
            session = mgr._live[0].session
            failed = mgr.bridge.run(session.call_tool("boom", {}), timeout=30)
            assert _result_is_error(failed)
            unknown = mgr.bridge.run(session.call_tool("nope", {}), timeout=30)
            assert _result_is_error(unknown)
            ok = mgr.bridge.run(session.call_tool("echo", {"x": "y"}), timeout=30)
            assert not _result_is_error(ok)
            # Output that only starts with the word is a success.
            codes = mgr.bridge.run(session.call_tool("errcodes", {}), timeout=30)
            assert not _result_is_error(codes)
            assert REGISTRY["remote.errcodes"].call({}) == ERRCODES
            # ...and the agent sees a flagged result as an ERROR, said once.
            assert REGISTRY["remote.boom"].call({}) == "ERROR: ValueError: kaput"
        finally:
            mgr.stop()


# ---- `evi mcp serve --http`: DNS rebinding + bind policy --------------------


def test_tokenless_http_serve_rejects_a_foreign_host(served_tools):
    app = publish.build_http_app(served_tools)
    with _Served(app) as srv:
        url = srv.url + "/"
        local = f"127.0.0.1:{srv.port}"
        with httpx.Client(timeout=15) as c:
            evil = c.post(url, json=_initialize_body(),
                          headers={**_MCP_HEADERS, "Host": "evil.example"})
            assert evil.status_code == 421
            evil_port = c.post(url, json=_initialize_body(),
                               headers={**_MCP_HEADERS, "Host": f"evil.example:{srv.port}"})
            assert evil_port.status_code == 421
            evil_get = c.get(url, headers={"Accept": "text/event-stream",
                                           "Host": "evil.example"})
            assert evil_get.status_code == 421
            # A loopback Host but a foreign page's Origin: a browser was driven.
            cross = c.post(url, json=_initialize_body(),
                           headers={**_MCP_HEADERS, "Host": local,
                                    "Origin": "http://evil.example"})
            assert cross.status_code == 403
            for host in (local, f"localhost:{srv.port}"):
                ok = c.post(url, json=_initialize_body(),
                            headers={**_MCP_HEADERS, "Host": host})
                assert ok.status_code == 200, (host, ok.text)
            # A local page (an MCP inspector on another loopback port) may call it.
            inspector = c.post(url, json=_initialize_body(),
                               headers={**_MCP_HEADERS, "Host": local,
                                        "Origin": "http://localhost:6274"})
            assert inspector.status_code == 200


def test_token_is_the_control_when_set(served_tools):
    # With a token a LAN or proxy hostname must work, and without the token
    # nothing does.
    app = publish.build_http_app(served_tools, token=TOKEN)
    with _Served(app) as srv:
        url = srv.url + "/"
        with httpx.Client(timeout=15) as c:
            anon = c.post(url, json=_initialize_body(),
                          headers={**_MCP_HEADERS, "Host": "evil.example"})
            assert anon.status_code == 401
            authed = c.post(url, json=_initialize_body(),
                            headers={**_MCP_HEADERS, "Host": "my-box.lan:8765",
                                     "Authorization": f"Bearer {TOKEN}"})
            assert authed.status_code == 200, authed.text


def test_serve_runs_the_guarded_app(served_tools, monkeypatch):
    """`serve(http=True)` (what `evi mcp serve --http` calls) hands uvicorn an
    app that rejects a rebinding Host."""
    ran: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.update(app=app, **kw))
    publish.serve(served_tools, http=True, host="127.0.0.1", port=1)
    assert ran["host"] == "127.0.0.1"
    with _Served(ran["app"]) as srv, httpx.Client(timeout=15) as c:
        r = c.post(srv.url + "/", json=_initialize_body(),
                   headers={**_MCP_HEADERS, "Host": "evil.example"})
        assert r.status_code == 421


def test_tokenless_serve_on_another_loopback_address_answers_its_own_host(
    served_tools, monkeypatch,
):
    """Any 127/8 address is a loopback bind, so `serve` starts tokenless on
    127.0.0.2; its clients send `Host: 127.0.0.2:<port>`, which must pass. The
    app is served on 127.0.0.1 here (macOS has no 127.0.0.2) with that Host."""
    ran: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.update(app=app, **kw))
    publish.serve(served_tools, http=True, host="127.0.0.2", port=1)
    assert ran["host"] == "127.0.0.2"
    with _Served(ran["app"]) as srv, httpx.Client(timeout=15) as c:
        url = srv.url + "/"
        own = c.post(url, json=_initialize_body(),
                     headers={**_MCP_HEADERS, "Host": f"127.0.0.2:{srv.port}"})
        assert own.status_code == 200, own.text
        evil = c.post(url, json=_initialize_body(),
                      headers={**_MCP_HEADERS, "Host": "evil.example"})
        assert evil.status_code == 421


@pytest.mark.parametrize("bind,expected", [
    ("127.0.0.2", ["127.0.0.2"]),
    ("0:0:0:0:0:0:0:1", ["[0:0:0:0:0:0:0:1]", "[::1]"]),
    ("[::1]", ["[::1]"]),
    ("localhost", ["localhost"]),
])
def test_loopback_settings_name_the_bind_address(bind, expected):
    settings = publish.loopback_security_settings(bind)
    for name in expected:
        assert f"{name}:*" in settings.allowed_hosts
        assert f"http://{name}:*" in settings.allowed_origins
    # The defaults stay, and a non-loopback bind adds nothing.
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert publish.loopback_security_settings("0.0.0.0").allowed_hosts == \
        publish.loopback_security_settings().allowed_hosts


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "my-box.lan", ""])
def test_serve_refuses_a_non_loopback_bind_without_a_token(host, monkeypatch):
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("must not serve"))
    with pytest.raises(ValueError, match="without a token"):
        publish.serve(("git",), http=True, host=host)


def test_serve_allows_loopback_or_a_token(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw["host"]))
    publish.serve(("git",), http=True, host="localhost")
    publish.serve(("git",), http=True, host="0.0.0.0", token=TOKEN)
    assert calls == ["localhost", "0.0.0.0"]


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.0.0.2", True), ("localhost", True), ("::1", True),
    ("[::1]", True), ("0.0.0.0", False), ("::", False), ("10.0.0.5", False),
    ("example.com", False), ("", False),
])
def test_is_loopback_bind(host, expected):
    assert publish.is_loopback_bind(host) is expected


# ---- timeouts cancel the call, and the server hears about it ----------------


def test_bridge_timeout_cancels_the_coroutine():
    import concurrent.futures

    bridge = MCPBridge()
    bridge.start()
    cancelled = threading.Event()
    try:
        async def slow() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(concurrent.futures.TimeoutError):
            bridge.run(slow(), timeout=0.2)
        assert cancelled.wait(5), "timed-out coroutine kept running on the bridge loop"
    finally:
        bridge.stop()


def test_hung_tool_times_out_and_the_server_is_told_to_cancel(caplog):
    started, cancelled = threading.Event(), threading.Event()
    with _Served(_asgi_app(_hang_server(started, cancelled))) as srv:
        mgr = MCPManager(
            [MCPServer(name="slow", transport="http", url=srv.url)], call_timeout=1.0
        )
        try:
            mgr.start()
            assert "slow.hang" in mgr.registered_tool_names(), caplog.text
            t0 = time.monotonic()
            out = REGISTRY["slow.hang"].call({})
            assert out.startswith("ERROR: TimeoutError"), out
            assert time.monotonic() - t0 < 10
            assert started.is_set()
            assert cancelled.wait(10), "the server never received a cancellation"
        finally:
            mgr.stop()


def test_failed_connect_closes_what_it_opened(monkeypatch):
    """A connect that times out is cancelled, and closes the transport it had
    opened (a spawned server process) instead of leaking it."""
    import mcp
    import mcp.client.stdio as stdio_mod

    exited = threading.Event()

    class _Streams:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *a):
            exited.set()
            return False

    class _HangingSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            await asyncio.sleep(60)

    class _ShortBridge(MCPBridge):
        def run(self, coro, timeout=60.0):
            return super().run(coro, timeout=min(timeout, 0.5))

    monkeypatch.setattr(stdio_mod, "stdio_client", lambda params: _Streams())
    monkeypatch.setattr(mcp, "ClientSession", _HangingSession)
    bridge = _ShortBridge()
    mgr = MCPManager([MCPServer(name="stuck", command="noop")], bridge=bridge)
    try:
        mgr.start()
        assert mgr.registered_tool_names() == []
        assert exited.wait(5), "the transport opened by a timed-out connect was leaked"
    finally:
        mgr.stop()
        bridge.stop()
