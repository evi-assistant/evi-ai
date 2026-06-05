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
from evi.mcp.manager import MCPManager, _flatten_content
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
    """A bad server shouldn't take down Evi."""

    def _boom(self, server):  # pylint: disable=unused-argument
        raise RuntimeError("could not spawn")

    monkeypatch.setattr(MCPManager, "_connect", _boom)
    manager = MCPManager(servers=[MCPServer(name="bad", command="nope")])
    manager.start()
    assert manager.started is True
    assert manager.registered_tool_names() == []
    manager.stop()


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
