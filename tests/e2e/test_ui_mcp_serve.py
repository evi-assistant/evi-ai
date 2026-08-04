"""E2E for the 'Serve eVi over MCP' panel (Settings -> MCP).

Covers the web surface added for running eVi AS an MCP server: start an
in-process localhost HTTP MCP server, confirm it's running server-side, stop it,
and render the stdio client-config snippet. (Consuming external MCP servers is
covered by the existing MCP panel; this is the inverse — serving.)
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_mcp_serve_start_running_then_stop(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('mcp')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#mcp-serve-start")).to_be_visible(timeout=10_000)

    # Start the localhost HTTP MCP server.
    page.click("#mcp-serve-start")
    expect(page.locator("#mcp-serve-stop")).to_be_visible(timeout=15_000)
    expect(page.locator("#mcp-serve-box")).to_contain_text("running")

    # Server-side confirms it's actually serving.
    running = page.evaluate(
        "() => fetch('/api/mcp/serve').then(r => r.json()).then(d => d.running)"
    )
    assert running is True

    # Stop it.
    page.click("#mcp-serve-stop")
    expect(page.locator("#mcp-serve-start")).to_be_visible(timeout=10_000)
    stopped = page.evaluate(
        "() => fetch('/api/mcp/serve').then(r => r.json()).then(d => d.running)"
    )
    assert stopped is False


def test_mcp_serve_config_snippet_renders(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('mcp')")
    expect(page.locator("#mcp-serve-cfg")).to_be_visible(timeout=10_000)

    page.click("#mcp-serve-cfg")
    snippet = page.locator("#mcp-serve-snippet")
    expect(snippet).to_be_visible()
    # The stdio config a desktop MCP client pastes to spawn eVi.
    expect(snippet).to_contain_text("mcpServers")
    expect(snippet).to_contain_text("mcp")
    expect(snippet).to_contain_text("serve")
