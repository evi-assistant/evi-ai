"""MCP server config — list of stdio servers loaded from ~/.evi/mcp.json.

Stored as a separate JSON file (not config.toml) because TOML's array-of-tables
gets awkward to write and we want users to be able to hand-edit a simple list.

Example file:

    [
      {
        "name": "filesystem",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users"],
        "env": {},
        "enabled": true
      }
    ]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from evi.config import MCP_CONFIG_PATH


@dataclass
class MCPServer:
    """Launch config for a single MCP server (stdio transport only for now)."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def load_servers(path: Path | None = None) -> list[MCPServer]:
    """Read `~/.evi/mcp.json`. Returns [] if the file is missing or empty.

    Malformed entries are skipped with no fanfare — the manager will log
    when it tries to connect and fails, which is the right place to surface it.
    """
    p = path or MCP_CONFIG_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    servers: list[MCPServer] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        command = entry.get("command")
        if not name or not command:
            continue
        servers.append(
            MCPServer(
                name=str(name),
                command=str(command),
                args=[str(a) for a in entry.get("args", []) or []],
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return servers


def save_servers(servers: list[MCPServer], path: Path | None = None) -> None:
    """Write the server list back to disk. Convenience for the CLI."""
    p = path or MCP_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "name": s.name,
            "command": s.command,
            "args": s.args,
            "env": s.env,
            "enabled": s.enabled,
        }
        for s in servers
    ]
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
