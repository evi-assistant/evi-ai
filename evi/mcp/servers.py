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
    """Connection config for a single MCP server.

    ``transport`` is ``stdio`` (spawn ``command``/``args``) or a remote HTTP
    transport — ``http`` (streamable-http) / ``sse`` — driven by ``url`` (+
    optional ``headers`` for auth). ``command`` is unused for HTTP transports.
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    transport: str = "stdio"  # stdio | http | sse
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # Per-tool policy, by the tool's own name on this server (a glob, matched
    # ignoring case): always_deny is refused in every mode, yolo included;
    # always_ask asks in every mode and is refused when nothing can ask.
    always_ask: list[str] = field(default_factory=list)
    always_deny: list[str] = field(default_factory=list)
    # Trust this server's readOnlyHint annotations: a tool it marks read-only
    # (and not destructive) runs without asking. Read from the user's own
    # mcp.json only, never a plugin's.
    read_only_hints: bool = False

    def rule_for(self, tool: str, annotations: object = None) -> str:
        """The `evi.tools.base.Tool.mcp_rule` for `tool` on this server:
        "deny", "ask", "read_only" or ""."""
        import fnmatch

        name = tool.lower()
        if any(fnmatch.fnmatchcase(name, g.lower()) for g in self.always_deny if g):
            return "deny"
        if any(fnmatch.fnmatchcase(name, g.lower()) for g in self.always_ask if g):
            return "ask"
        if self.read_only_hints and _hint(annotations, "readOnlyHint", "read_only_hint") is True                 and _hint(annotations, "destructiveHint", "destructive_hint") is not True:
            return "read_only"
        return ""


def _hint(annotations: object, *names: str) -> object:
    """An MCP tool annotation, whichever SDK spelling (or a plain dict) it has."""
    if annotations is None:
        return None
    for n in names:
        v = annotations.get(n) if isinstance(annotations, dict) else getattr(annotations, n, None)
        if v is not None:
            return v
    return None


def _names(value: object) -> list[str]:
    return [str(v) for v in value if isinstance(v, str) and v.strip()]         if isinstance(value, list) else []


def filter_allowed(servers: list[MCPServer], allow) -> list[MCPServer]:
    """Apply a consume-side allowlist. Empty/None `allow` → unchanged (the
    manager still honours each server's `enabled`). Non-empty → keep only
    servers whose name is in the allowlist, so a shared/synced mcp.json can be
    gated per machine via `[tools] mcp_allow`."""
    names = {n for n in (allow or ()) if n}
    if not names:
        return servers
    return [s for s in servers if s.name in names]


def _parse_server_file(p: Path, name_prefix: str = "") -> list[MCPServer]:
    """Read one mcp.json into a list of MCPServers. Missing/empty/malformed → [].
    `name_prefix` namespaces plugin-supplied servers (e.g. ``git-helpers:``)."""
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
        if not name:
            continue
        command = entry.get("command")
        url = str(entry.get("url") or "")
        # Default transport: http when a url is present, else stdio. An explicit
        # "transport" wins (so sse can be requested even with a url).
        transport = str(entry.get("transport") or ("http" if url else "stdio")).lower()
        if transport in ("http", "sse"):
            if not url:
                continue  # remote transport needs a url
        elif not command:
            continue  # stdio needs a command
        servers.append(
            MCPServer(
                name=f"{name_prefix}{name}",
                command=str(command or ""),
                args=[str(a) for a in entry.get("args", []) or []],
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                enabled=bool(entry.get("enabled", True)),
                transport=transport,
                url=url,
                headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                always_ask=_names(entry.get("always_ask")),
                always_deny=_names(entry.get("always_deny")),
                # A plugin's file may tighten (ask, deny), never trust hints.
                read_only_hints=(not name_prefix) and entry.get("read_only_hints") is True,
            )
        )
    return servers


def load_servers(path: Path | None = None) -> list[MCPServer]:
    """Read `~/.evi/mcp.json` plus every installed plugin's `mcp.json`.

    Returns [] if nothing is configured. Plugin servers are namespaced
    ``<plugin>:<name>`` (so they can't collide with the user's own and read
    cleanly in the allowlist). Malformed entries are skipped silently — the
    manager logs when a connection actually fails, which is the right place.
    """
    servers = _parse_server_file(path or MCP_CONFIG_PATH)
    try:
        from evi.plugins import plugin_dirs

        for pd in plugin_dirs():
            servers.extend(_parse_server_file(pd / "mcp.json", name_prefix=f"{pd.name}:"))
    except Exception:  # plugin scanning must never break core MCP
        pass
    return servers


def save_servers(servers: list[MCPServer], path: Path | None = None) -> None:
    """Write the server list back to disk. Convenience for the CLI."""
    p = path or MCP_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for s in servers:
        if s.transport in ("http", "sse"):
            entry: dict = {"name": s.name, "transport": s.transport, "url": s.url}
            if s.headers:
                entry["headers"] = s.headers
        else:
            # stdio: keep the original shape so existing files don't churn.
            entry = {"name": s.name, "command": s.command, "args": s.args, "env": s.env}
        entry["enabled"] = s.enabled
        for key in ("always_ask", "always_deny"):
            if getattr(s, key):
                entry[key] = list(getattr(s, key))
        if s.read_only_hints:
            entry["read_only_hints"] = True
        payload.append(entry)
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# --- managing the USER file (~/.evi/mcp.json) -------------------------------
#
# These operate on the user's own mcp.json only — never on plugin-supplied
# servers (namespaced "<plugin>:<name>"), which are owned by their plugin and
# removed with `evi plugin remove`.


def user_servers(path: Path | None = None) -> list[MCPServer]:
    """Just the user's own servers (no plugin merge), for editing."""
    return _parse_server_file(path or MCP_CONFIG_PATH)


def add_server(
    server: MCPServer, path: Path | None = None, *, overwrite: bool = False
) -> bool:
    """Add (or with `overwrite` replace) a user server by name. False if it
    exists and overwrite is off."""
    servers = user_servers(path)
    for i, existing in enumerate(servers):
        if existing.name.lower() == server.name.lower():
            if not overwrite:
                return False
            servers[i] = server
            save_servers(servers, path)
            return True
    servers.append(server)
    save_servers(servers, path)
    return True


def remove_server(name: str, path: Path | None = None) -> bool:
    """Remove a user server by name. False if no such server in the user file
    (plugin servers never match — they live in the plugin's own mcp.json)."""
    servers = user_servers(path)
    kept = [s for s in servers if s.name.lower() != name.strip().lower()]
    if len(kept) == len(servers):
        return False
    save_servers(kept, path)
    return True


def set_enabled(name: str, enabled: bool, path: Path | None = None) -> bool:
    """Flip a user server's `enabled` flag. False if no such user server."""
    servers = user_servers(path)
    for s in servers:
        if s.name.lower() == name.strip().lower():
            s.enabled = enabled
            save_servers(servers, path)
            return True
    return False
