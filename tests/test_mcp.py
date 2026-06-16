"""Tests for the MCP integration layer.

We don't spin up real MCP servers — those need Node/npx and live network.
Instead we exercise:

- `MCPBridge` lifecycle (start, run a coroutine, stop)
- `load_servers` JSON parsing (happy, missing, malformed)
- `MCPManager._wrap_tool` with a fake async session, so the bridge actually
  drives a coroutine all the way to the tool result
- `_flatten_content` content-type handling
- Manager keeps going when one server fails to connect
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from evi.mcp.bridge import MCPBridge
from evi.mcp.manager import MCPManager, _flatten_content, _truncate
from evi.mcp.servers import MCPServer, load_servers, save_servers
from evi.tools.base import REGISTRY


# ---- bridge -------------------------------------------------------------


def test_bridge_runs_coroutine() -> None:
    bridge = MCPBridge()
    bridge.start()
    try:
        async def add(a: int, b: int) -> int:
            await asyncio.sleep(0)
            return a + b

        assert bridge.run(add(2, 3)) == 5
    finally:
        bridge.stop()
    assert not bridge.is_running


def test_bridge_start_idempotent() -> None:
    bridge = MCPBridge()
    bridge.start()
    try:
        thread_a = bridge._thread
        bridge.start()  # should be a no-op
        assert bridge._thread is thread_a
    finally:
        bridge.stop()


def test_bridge_run_propagates_exception() -> None:
    bridge = MCPBridge()
    bridge.start()
    try:
        async def boom() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            bridge.run(boom())
    finally:
        bridge.stop()


# ---- server-list loader -------------------------------------------------


def test_load_servers_missing(tmp_path: Path) -> None:
    assert load_servers(tmp_path / "missing.json") == []


def test_load_servers_happy(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            [
                {"name": "fs", "command": "npx", "args": ["-y", "@x/fs"]},
                {"name": "disabled", "command": "noop", "enabled": False},
                {"name": "no-command"},  # skipped — missing command
                {"name": "with-env", "command": "x", "env": {"K": "V"}},
            ]
        ),
        encoding="utf-8",
    )
    servers = load_servers(p)
    names = [s.name for s in servers]
    assert names == ["fs", "disabled", "with-env"]
    assert servers[0].args == ["-y", "@x/fs"]
    assert servers[1].enabled is False
    assert servers[2].env == {"K": "V"}


def test_load_servers_malformed(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_servers(p) == []

    p.write_text('"a string, not a list"', encoding="utf-8")
    assert load_servers(p) == []


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    save_servers(
        [MCPServer(name="git", command="mcp-server-git", args=["--repo", "."])],
        path=p,
    )
    loaded = load_servers(p)
    assert len(loaded) == 1
    assert loaded[0].name == "git"
    assert loaded[0].command == "mcp-server-git"
    assert loaded[0].transport == "stdio"


# ---- HTTP / SSE transports ------------------------------------------------


def test_load_servers_http_transport(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps([
            {"name": "remote", "url": "https://mcp.example.com/mcp"},  # url => http inferred
            {"name": "legacy", "transport": "sse", "url": "https://x/sse",
             "headers": {"Authorization": "Bearer t"}},
            {"name": "bad-http", "transport": "http"},  # no url => skipped
        ]),
        encoding="utf-8",
    )
    servers = {s.name: s for s in load_servers(p)}
    assert set(servers) == {"remote", "legacy"}  # bad-http dropped
    assert servers["remote"].transport == "http"
    assert servers["remote"].url == "https://mcp.example.com/mcp"
    assert servers["legacy"].transport == "sse"
    assert servers["legacy"].headers == {"Authorization": "Bearer t"}


def test_save_load_roundtrip_http(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    save_servers([
        MCPServer(name="git", command="mcp-server-git"),  # stdio
        MCPServer(name="remote", transport="http", url="https://x/mcp",
                  headers={"Authorization": "Bearer t"}),
    ], path=p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    # stdio entry keeps its original shape (no transport/url keys)
    assert raw[0] == {"name": "git", "command": "mcp-server-git", "args": [], "env": {}, "enabled": True}
    assert raw[1]["transport"] == "http" and raw[1]["url"] == "https://x/mcp"
    loaded = {s.name: s for s in load_servers(p)}
    assert loaded["remote"].headers == {"Authorization": "Bearer t"}


class _FakeStreams:
    def __init__(self, n: int):
        self._n = n  # 3 for http (read, write, get_session_id), 2 for sse

    async def __aenter__(self):
        return ("r", "w", "sid")[: self._n]

    async def __aexit__(self, *a):
        return False


class _FakeTransportSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        tool = type("T", (), {"name": "ping", "description": "ping",
                              "inputSchema": {"type": "object", "properties": {}}})()
        return type("R", (), {"tools": [tool]})()


def test_manager_connects_http_transport(monkeypatch) -> None:
    import mcp
    import mcp.client.streamable_http as sh

    captured: dict = {}

    def fake_http(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeStreams(3)

    monkeypatch.setattr(sh, "streamablehttp_client", fake_http)
    monkeypatch.setattr(mcp, "ClientSession", _FakeTransportSession)

    mgr = MCPManager([MCPServer(
        name="remote", transport="http", url="https://x/mcp",
        headers={"Authorization": "Bearer t"},
    )])
    try:
        mgr.start()
        assert "remote.ping" in mgr.registered_tool_names()
        assert captured["url"] == "https://x/mcp"
        assert captured["headers"] == {"Authorization": "Bearer t"}
    finally:
        mgr.stop()
    assert "remote.ping" not in REGISTRY  # cleaned up on stop


def test_manager_stdio_injects_session_env(monkeypatch) -> None:
    import mcp
    import mcp.client.stdio as stdio_mod

    captured: dict = {}

    def fake_stdio(params):
        captured["env"] = dict(params.env or {})
        return _FakeStreams(2)

    monkeypatch.setattr(stdio_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp, "ClientSession", _FakeTransportSession)

    mgr = MCPManager([MCPServer(name="local", command="noop", args=[])], session_id="sess-123")
    try:
        mgr.start()
        assert captured["env"].get("EVI") == "1"  # marker like CLAUDECODE=1
        assert captured["env"].get("EVI_SESSION_ID") == "sess-123"
    finally:
        mgr.stop()


def test_manager_connects_sse_transport(monkeypatch) -> None:
    import mcp
    import mcp.client.sse as sse

    captured: dict = {}

    def fake_sse(url, headers=None):
        captured["url"] = url
        return _FakeStreams(2)

    monkeypatch.setattr(sse, "sse_client", fake_sse)
    monkeypatch.setattr(mcp, "ClientSession", _FakeTransportSession)

    mgr = MCPManager([MCPServer(name="ev", transport="sse", url="https://x/sse")])
    try:
        mgr.start()
        assert "ev.ping" in mgr.registered_tool_names()
        assert captured["url"] == "https://x/sse"
    finally:
        mgr.stop()


# ---- _flatten_content ---------------------------------------------------


@dataclass
class _TextItem:
    text: str
    type: str = "text"


@dataclass
class _ImageItem:
    type: str = "image"


@dataclass
class _Result:
    content: list[object]
    isError: bool = False


def test_flatten_text_only() -> None:
    r = _Result(content=[_TextItem("hello"), _TextItem("world")])
    assert _flatten_content(r) == "hello\nworld"


def test_flatten_mixed_content_marks_omitted() -> None:
    r = _Result(content=[_TextItem("ok"), _ImageItem()])
    out = _flatten_content(r)
    assert "ok" in out
    assert "[image omitted]" in out


def test_flatten_error_result() -> None:
    r = _Result(content=[_TextItem("bad thing")], isError=True)
    out = _flatten_content(r)
    assert out.startswith("ERROR:")
    assert "bad thing" in out


def test_flatten_empty() -> None:
    r = _Result(content=[])
    assert _flatten_content(r) == "(no content)"


def test_truncate_unlimited_and_under_cap() -> None:
    assert _truncate("hello", 0) == "hello"     # 0 = unlimited
    assert _truncate("hello", 100) == "hello"   # under cap, untouched


def test_truncate_clips_with_marker() -> None:
    out = _truncate("x" * 50, 10)
    assert out.startswith("x" * 10)
    assert "40 chars truncated" in out
    assert "MCP output cap" in out


# ---- manager: wrapped tool actually routes through bridge ----------------


@dataclass
class _FakeMCPTool:
    name: str
    description: str
    inputSchema: dict


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return _Result(content=[_TextItem(f"called {name} with {arguments}")])


def test_wrap_tool_invokes_session_via_bridge() -> None:
    bridge = MCPBridge()
    bridge.start()
    try:
        manager = MCPManager(servers=[], bridge=bridge)
        session = _FakeSession()
        mcp_tool = _FakeMCPTool(
            name="read",
            description="read a thing",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        evi_tool = manager._wrap_tool("fs", session, mcp_tool)
        assert evi_tool.name == "fs.read"
        assert evi_tool.category == "mcp"
        assert evi_tool.description == "read a thing"

        out = evi_tool.call(json.dumps({"path": "/tmp/x"}))
        assert "called read" in out
        assert session.calls == [("read", {"path": "/tmp/x"})]
    finally:
        bridge.stop()


def test_manager_start_skips_disabled_servers() -> None:
    manager = MCPManager(servers=[MCPServer(name="off", command="x", enabled=False)])
    manager.start()
    assert manager.started is True
    assert manager.registered_tool_names() == []
    manager.stop()


def test_manager_tolerates_failed_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad server shouldn't take down eVi."""

    def _boom(self, server):  # pylint: disable=unused-argument
        raise RuntimeError("could not spawn")

    monkeypatch.setattr(MCPManager, "_connect", _boom)
    manager = MCPManager(servers=[MCPServer(name="bad", command="nope")])
    manager.start()
    assert manager.started is True
    assert manager.registered_tool_names() == []
    manager.stop()


# ---- managing the user mcp.json (add/remove/enable) -----------------------


def test_add_remove_server_roundtrip(tmp_path: Path) -> None:
    from evi.mcp.servers import add_server, remove_server, user_servers

    p = tmp_path / "mcp.json"
    assert add_server(MCPServer(name="fs", command="npx", args=["-y", "pkg"]), p)
    assert add_server(MCPServer(name="git", command="uvx", args=["mcp-server-git"]), p)
    assert [s.name for s in user_servers(p)] == ["fs", "git"]
    # duplicate name: rejected without overwrite, replaced with it
    assert not add_server(MCPServer(name="fs", command="other"), p)
    assert add_server(MCPServer(name="fs", command="other"), p, overwrite=True)
    assert user_servers(p)[0].command == "other"
    assert remove_server("git", p)
    assert not remove_server("ghost", p)
    assert [s.name for s in user_servers(p)] == ["fs"]


def test_set_enabled_flips_flag(tmp_path: Path) -> None:
    from evi.mcp.servers import add_server, set_enabled, user_servers

    p = tmp_path / "mcp.json"
    add_server(MCPServer(name="fs", command="npx"), p)
    assert set_enabled("fs", False, p)
    assert user_servers(p)[0].enabled is False
    assert set_enabled("fs", True, p)
    assert user_servers(p)[0].enabled is True
    assert not set_enabled("ghost", True, p)


def test_user_servers_excludes_plugin_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # user_servers must read ONLY the user file — plugin servers are owned by
    # their plugin and must never be rewritten into ~/.evi/mcp.json.
    from evi.mcp import servers as srv_mod
    from evi.mcp.servers import add_server, user_servers

    p = tmp_path / "mcp.json"
    add_server(MCPServer(name="mine", command="npx"), p)
    pd = tmp_path / "someplugin"
    pd.mkdir()
    (pd / "mcp.json").write_text('[{"name": "tool", "command": "uvx"}]', encoding="utf-8")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [pd])
    merged = srv_mod.load_servers(p)
    assert {s.name for s in merged} == {"mine", "someplugin:tool"}
    assert [s.name for s in user_servers(p)] == ["mine"]


def test_manager_unregisters_tools_on_stop() -> None:
    """After stop(), the MCP tool names must be gone from REGISTRY."""
    bridge = MCPBridge()
    bridge.start()
    try:
        manager = MCPManager(servers=[], bridge=bridge)
        session = _FakeSession()
        mcp_tool = _FakeMCPTool(
            name="t", description="d", inputSchema={"type": "object", "properties": {}}
        )
        # Manually plumb a live server entry so stop() can clean it up.
        from contextlib import AsyncExitStack
        from evi.mcp.manager import _LiveServer

        evi_tool = manager._wrap_tool("srv", session, mcp_tool)
        REGISTRY[evi_tool.name] = evi_tool
        live = _LiveServer(
            name="srv",
            session=session,
            stack=AsyncExitStack(),
            tool_names=[evi_tool.name],
        )
        manager._live.append(live)
        manager.started = True

        assert "srv.t" in REGISTRY
        manager.stop()
        assert "srv.t" not in REGISTRY
    finally:
        if bridge.is_running:
            bridge.stop()
