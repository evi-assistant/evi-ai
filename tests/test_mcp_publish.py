"""Tests for exposing eVi's tools as an MCP server (Phase 53).

Asserts only on tool categories that don't need optional extras (`git`,
`memory`) so the suite passes in CI, which doesn't install every extra.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evi.mcp import publish  # noqa: E402
from evi.tools.base import Tool  # noqa: E402


def _fake_tool(name="echo"):
    return Tool(
        name=name,
        description="echo back",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        func=lambda x: f"got {x}",
        category="test",
    )


def _shell_tool():
    return Tool(
        name="run_command",
        description="run a shell command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        func=lambda command="", cwd="", timeout=60: f"ran {command}",
        category="shell",
    )


def test_dispatch_blocks_destructive_shell_command(monkeypatch):
    # MCP has no interactive prompt → a matched destructive command is refused
    # (headless fail-safe), so it never reaches tool.call.
    monkeypatch.setattr("evi.config.Config.load", classmethod(lambda cls: cls()))
    by_name = {"run_command": _shell_tool()}
    blocked = publish.dispatch(by_name, "run_command", {"command": "rm -rf ~"})
    assert "destructive-command guard" in blocked
    # a benign command is not guard-blocked (the tool runs)
    ok = publish.dispatch(by_name, "run_command", {"command": "echo hi"})
    assert "destructive-command guard" not in ok and "ran echo hi" in ok


def _monitor_tool():
    return Tool(
        name="monitor",
        description="watch a file or a command",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        func=lambda target="", kind="file": f"watched {kind} {target}",
        category="code",
    )


def test_dispatch_guard_covers_monitor_commands(monkeypatch):
    # monitor (category "code") runs `target` through a shell only for kind=command.
    monkeypatch.setattr("evi.config.Config.load", classmethod(lambda cls: cls()))
    by_name = {"monitor": _monitor_tool()}
    out = publish.dispatch(by_name, "monitor", {"kind": "command", "target": "rm -rf ~"})
    assert "destructive-command guard" in out
    out = publish.dispatch(by_name, "monitor", {"kind": "file", "target": "rm -rf ~"})
    assert out == "watched file rm -rf ~"  # a file path, never run


def test_dispatch_guard_shares_the_permissions_shell_map(monkeypatch):
    # One map (permissions.SHELL_EXEC_TOOLS) decides which calls run a shell
    # line for decide(), the agent's guard and MCP dispatch alike.
    from evi import permissions

    monkeypatch.setattr("evi.config.Config.load", classmethod(lambda cls: cls()))
    monkeypatch.setitem(permissions.SHELL_EXEC_TOOLS, "echo", "x")
    out = publish.dispatch({"echo": _fake_tool()}, "echo", {"x": "rm -rf ~"})
    assert "destructive-command guard" in out
    assert not hasattr(publish, "_MCP_SHELL_TOOLS")


def test_dispatch_guard_respects_disable(monkeypatch):
    cfg = __import__("evi.config", fromlist=["Config"]).Config()
    cfg.auto.block_destructive = False
    monkeypatch.setattr("evi.config.Config.load", classmethod(lambda cls: cfg))
    by_name = {"run_command": _shell_tool()}
    out = publish.dispatch(by_name, "run_command", {"command": "rm -rf ~"})
    assert "destructive-command guard" not in out  # guard off → tool runs


# --- tool selection ------------------------------------------------------


def test_selected_tools_includes_always_available_categories():
    git = publish.selected_tools(("git",))
    mem = publish.selected_tools(("memory",))
    assert git and all(t.category == "git" for t in git)
    assert mem and all(t.category == "memory" for t in mem)


def test_selected_tools_unknown_category_is_empty():
    assert publish.selected_tools(("definitely-not-a-category",)) == []


def test_selected_tools_sorted_by_name():
    tools = publish.selected_tools(("git", "memory"))
    names = [t.name for t in tools]
    assert names == sorted(names)


def test_default_categories_are_reasonable():
    assert "memory" in publish.DEFAULT_CATEGORIES
    assert "git" in publish.DEFAULT_CATEGORIES


# --- spec mapping --------------------------------------------------------


def test_mcp_tool_specs_shape():
    specs = publish.mcp_tool_specs([_fake_tool("a")])
    assert specs == [{
        "name": "a",
        "description": "echo back",
        "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    }]


def test_specs_construct_valid_mcp_tools():
    mcp_types = pytest.importorskip("mcp.types")
    spec = publish.mcp_tool_specs([_fake_tool("b")])[0]
    mt = mcp_types.Tool(**spec)  # must not raise — the inputSchema is reused verbatim
    assert mt.name == "b"
    # mcp 2.0 renamed the field inputSchema -> input_schema (same wire key).
    schema = getattr(mt, "input_schema", None) or getattr(mt, "inputSchema", None)
    assert schema["type"] == "object"


# --- dispatch ------------------------------------------------------------


def test_dispatch_invokes_known_tool():
    by_name = {"echo": _fake_tool("echo")}
    assert publish.dispatch(by_name, "echo", {"x": "hi"}) == "got hi"


def test_dispatch_unknown_tool_returns_error():
    out = publish.dispatch({}, "nope", {})
    assert out.startswith("ERROR: unknown tool")


def test_is_error_text_flags_failures_only():
    # Failures are served to MCP clients with isError set (see build_server).
    assert publish.is_error_text(publish.dispatch({}, "nope", {}))
    assert publish.is_error_text("ERROR: ValueError: kaput")
    assert publish.is_error_text("BLOCKED by the destructive-command guard: rm -rf. Not run.")
    assert not publish.is_error_text("got hi")
    assert not publish.is_error_text("no ERROR here")
    # Successful output that merely starts with the word (git_show of a file
    # whose first line is "ERROR codes") is not a failure.
    assert not publish.is_error_text("ERROR codes reference\n404 not found")
    assert not publish.is_error_text("ERRORS: none")


def test_dispatch_tolerates_none_arguments():
    # The fake tool requires `x`; missing args surface as a tool ERROR, not a crash.
    out = publish.dispatch({"echo": _fake_tool("echo")}, "echo", None)
    assert out.startswith("ERROR")


# --- server construction (smoke) ----------------------------------------


def test_build_server_smoke():
    pytest.importorskip("mcp")
    server = publish.build_server(("git",))
    assert server is not None
    assert getattr(server, "name", "evi") == "evi"


# --- per-tool allow-list (Phase 54) -------------------------------------


def test_allow_list_narrows_to_named_tools():
    only = publish.selected_tools(("git",), allow=("git_status",))
    assert [t.name for t in only] == ["git_status"]


def test_allow_list_outside_category_excluded():
    assert publish.selected_tools(("memory",), allow=("git_status",)) == []


# --- resources: long-term memory ----------------------------------------


def test_memory_resource_specs_and_read(monkeypatch):
    import evi.memory as mem
    from evi.memory import MemoryEntry

    class FakeStore:
        def __init__(self, root=None):
            pass

        def list(self):
            return [MemoryEntry(name="foo", summary="about foo")]

        def read(self, name):
            if name == "foo":
                return "# foo\nbody"
            raise KeyError(name)

    monkeypatch.setattr(mem, "MemoryStore", FakeStore)
    specs = publish.memory_resource_specs()
    assert specs == [{
        "uri": "evi://memory/foo", "name": "foo",
        "description": "about foo", "mimeType": "text/markdown",
    }]
    assert publish.read_memory_resource("evi://memory/foo") == "# foo\nbody"


def test_read_memory_resource_rejects_bad_uri():
    with pytest.raises(ValueError):
        publish.read_memory_resource("http://example.com/x")


# --- prompts: saved slash-command templates -----------------------------


def test_command_prompt_specs_and_expand(monkeypatch):
    import evi.commands as cmds
    from pathlib import Path

    from evi.commands import SlashCommandEntry

    class FakeCmds:
        def __init__(self, root=None):
            pass

        def list(self):
            return [SlashCommandEntry(name="commit", path=Path("x"), summary="make a commit")]

        def expand(self, name, args="", *, inline=True):
            seen_inline.append(inline)
            return f"do {name} {args}".strip() if name == "commit" else None

    seen_inline: list[bool] = []
    monkeypatch.setattr(cmds, "CommandStore", FakeCmds)
    specs = publish.command_prompt_specs()
    assert specs[0]["name"] == "commit"
    assert specs[0]["arguments"][0]["name"] == "args"
    assert publish.expand_command_prompt("commit", {"args": "now"}) == "do commit now"
    assert publish.expand_command_prompt("missing", {}) is None
    # MCP-served prompts never inline @file refs (see test_command_file_inline).
    assert seen_inline == [False, False]


# --- HTTP transport ------------------------------------------------------


def test_build_http_app_smoke():
    pytest.importorskip("mcp")
    pytest.importorskip("starlette")
    app = publish.build_http_app(("git",), token="secret")
    assert app is not None


# --- end-to-end: a real MCP client drives eVi's server -------------------


async def test_served_tools_round_trip():
    """A real MCP client connects to eVi's server over an in-memory transport,
    lists tools, and calls one — exercising the actual list_tools / call_tool
    handlers, not just that build_server constructs. This is the test that would
    have caught the mcp 2.0 server-API break (the smoke tests above only build).

    Version-adaptive: mcp 1.x and 2.0 expose different in-memory helpers.
    """
    pytest.importorskip("mcp")
    import mcp

    server = publish.build_server(("git",))

    try:  # mcp 1.x — one helper yields a connected client session
        from mcp.shared.memory import (
            create_connected_server_and_client_session as _connect,
        )
    except ImportError:
        _connect = None

    if _connect is not None:
        async with _connect(server) as client:
            await _assert_round_trip(client)
        return

    # mcp >= 2.0 — wire raw memory streams + run the server in a task group.
    import anyio
    from mcp.shared.memory import create_client_server_memory_streams

    init_opts = server.create_initialization_options()
    async with create_client_server_memory_streams() as ((c_read, c_write), (s_read, s_write)):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(s_read, s_write, init_opts, raise_exceptions=True)
            )
            async with mcp.ClientSession(c_read, c_write) as client:
                await client.initialize()
                await _assert_round_trip(client)
            tg.cancel_scope.cancel()


async def _assert_round_trip(client) -> None:
    listed = await client.list_tools()
    assert listed.tools, "server exposed no tools"
    for t in listed.tools:
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
        assert schema, f"tool {t.name} lost its schema over the wire"

    # Call a tool with no required args and confirm the round-trip returns a
    # CallToolResult with content (the tool's own success is not under test).
    def _no_required(t):
        sch = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        return not sch.get("required")

    name = next((t.name for t in listed.tools if _no_required(t)), listed.tools[0].name)
    result = await client.call_tool(name, {})
    assert isinstance(result.content, list), "call_tool returned no content list"


# --- `evi mcp serve` (the CLI) ---------------------------------------------


@pytest.fixture
def serve_cli(monkeypatch, tmp_path):
    """The CLI with an isolated config and a stubbed `publish.serve`; returns
    (invoke(args) -> result, the serve() calls)."""
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    calls: list[dict] = []
    monkeypatch.setattr(publish, "serve", lambda *a, **k: calls.append(k))
    return (lambda args: CliRunner().invoke(cli_main.app, ["mcp", "serve", *args])), calls


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", ""])
def test_cli_refuses_a_tokenless_non_loopback_http_host(host, serve_cli):
    # Re-review: the CLI printed a stale "Bind to localhost only" warning, then
    # serve() raised ValueError and Typer printed a Rich traceback.
    invoke, calls = serve_cli
    r = invoke(["--http", "--host", host])
    assert r.exit_code == 2, r.output
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert len(lines) == 1, r.output
    assert lines[0].startswith("error: refusing to serve MCP over HTTP") and "--token" in lines[0]
    assert "Traceback" not in r.output and "Bind to localhost" not in r.output
    assert not calls


def test_cli_serves_tokenless_on_loopback_and_with_a_token_anywhere(serve_cli):
    invoke, calls = serve_cli
    r = invoke(["--http"])
    assert r.exit_code == 0, r.output
    assert "Bind to localhost" not in r.output and "--token" in r.output
    r = invoke(["--http", "--host", "0.0.0.0", "--token", "s3cret"])
    assert r.exit_code == 0, r.output
    assert [(c["host"], c["token"]) for c in calls] == [("127.0.0.1", ""), ("0.0.0.0", "s3cret")]
