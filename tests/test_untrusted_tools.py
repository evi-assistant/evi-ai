import evi.tools.calendar  # noqa: F401  registers the calendar tools
import evi.tools.fs  # noqa: F401  registers read_file and write_file
import evi.tools.websearch  # noqa: F401  registers web_search and web_fetch
from evi.mcp.bridge import MCPBridge
from evi.mcp.manager import MCPManager
from evi.tools.base import REGISTRY


def test_web_tools_untrusted():
    assert REGISTRY['web_search'].trust == 'untrusted'
    assert REGISTRY['web_fetch'].trust == 'untrusted'


def test_calendar_tools_untrusted():
    assert REGISTRY['calendar_today'].trust == 'untrusted'
    assert REGISTRY['calendar_week'].trust == 'untrusted'
    assert REGISTRY['calendar_search'].trust == 'untrusted'
    assert REGISTRY['calendar_next'].trust == 'untrusted'


def test_mcp_tool_untrusted():
    bridge = MCPBridge()
    bridge.start()
    try:
        manager = MCPManager(servers=[], bridge=bridge)
        session = type("Session", (), {"call_tool": lambda s, n, a: None})()
        mcp_tool = type("Tool", (), {
            "name": "test_tool",
            "description": "test desc",
            "inputSchema": {"type": "object", "properties": {}},
        })()
        wrapped = manager._wrap_tool("test_server", session, mcp_tool)
        assert wrapped.trust == 'untrusted'
    finally:
        bridge.stop()


def test_core_tools_trusted():
    # Tools that return the user's own files and commands stay trusted.
    for name in ['read_file', 'write_file', 'run_command']:
        if name in REGISTRY:
            assert REGISTRY[name].trust == 'trusted'
