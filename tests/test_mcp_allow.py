"""Tests for the consume-side MCP server allowlist (Phase 78)."""

from __future__ import annotations

from evi.mcp import filter_allowed
from evi.mcp.servers import MCPServer


def _servers():
    return [
        MCPServer(name="files", command="x"),
        MCPServer(name="github", command="y"),
        MCPServer(name="off", command="z", enabled=False),
    ]


def test_empty_allow_is_passthrough():
    s = _servers()
    assert filter_allowed(s, []) == s
    assert filter_allowed(s, None) == s


def test_allow_restricts_by_name():
    out = filter_allowed(_servers(), ["files"])
    assert [s.name for s in out] == ["files"]


def test_allow_ignores_unknown_names():
    out = filter_allowed(_servers(), ["files", "nonexistent"])
    assert [s.name for s in out] == ["files"]


def test_allow_can_include_a_disabled_server():
    # The allowlist selects by name; the manager still honours `enabled`.
    out = filter_allowed(_servers(), ["off"])
    assert [s.name for s in out] == ["off"] and out[0].enabled is False
