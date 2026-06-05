"""MCP (Model Context Protocol) integration.

Loads a list of MCP servers from `~/.evi/mcp.json`, launches each as a child
process over stdio, discovers its tool catalog, and registers those tools in
`evi.tools.base.REGISTRY` under the name `<server>.<tool>` so the existing
agent loop can call them transparently.

Public surface:

- `MCPServer` — dataclass for one server's launch config
- `load_servers()` — read `~/.evi/mcp.json` (returns [] if missing)
- `MCPManager` — start/stop lifecycle, owns the bridge thread + sessions
- `MCPBridge` — runs an asyncio loop on a background thread; lets sync code
  await the (async) MCP Python SDK calls
"""

from evi.mcp.bridge import MCPBridge
from evi.mcp.manager import MCPManager
from evi.mcp.servers import MCPServer, load_servers

__all__ = ["MCPBridge", "MCPManager", "MCPServer", "load_servers"]
