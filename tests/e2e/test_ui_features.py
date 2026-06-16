"""E2E tests for the newer UI surfaces — working-status indicator,
context-window popover, dispatch panel, and the Voice settings section.

Real browser (Playwright) + real eVi server + the fake streaming backend, so
these stay deterministic and CI-friendly. Run: `pytest tests/e2e -m e2e`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _console_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


# ---- working-status indicator (spinner + timer + tokens) -----------------


def test_work_status_controller(page: Page, evi_base_url: str):
    """Drive the indicator directly: start shows spinner + label; tokens render;
    stop hides it."""
    page.goto(evi_base_url)
    expect(page.locator("#work-status")).to_be_hidden()

    page.evaluate("window.workStatus.start()")
    expect(page.locator("#work-status")).to_be_visible()
    expect(page.locator("#work-status .work-spin")).to_be_visible()

    page.evaluate("window.workStatus.setPhase('thinking…')")
    expect(page.locator("#work-status .work-label")).to_have_text("thinking…")

    page.evaluate("window.workStatus.addText('x'.repeat(40)); window.workStatus.setTokens(123)")
    expect(page.locator("#work-status .work-meta")).to_contain_text("123")
    expect(page.locator("#work-status .work-meta")).to_contain_text("tok")

    page.evaluate("window.workStatus.stop()")
    expect(page.locator("#work-status")).to_be_hidden()


def test_work_status_clears_after_turn(page: Page, evi_base_url: str):
    """After a real (fake-backend) turn completes, the indicator is hidden again
    and the reply rendered."""
    errors = _console_errors(page)
    page.goto(evi_base_url)
    page.fill("#input", "hello evi")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20000)
    expect(page.locator("#work-status")).to_be_hidden(timeout=20000)
    assert errors == [], f"console errors: {errors}"


# ---- context-window popover (Phase 88) -----------------------------------


def test_context_popover(page: Page, evi_base_url: str):
    """After a turn the usage chip shows; clicking it opens the breakdown."""
    page.goto(evi_base_url)
    page.fill("#input", "hi")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20000)
    chip = page.locator("#chip-usage")
    expect(chip).to_be_visible(timeout=20000)
    chip.click()
    expect(page.locator(".ctx-pop")).to_be_visible()
    # one row per bucket (system / you / assistant / tools)
    assert page.locator(".ctx-pop .ctx-row").count() == 4


# ---- dispatch panel (Phase 85) -------------------------------------------


def test_dispatch_panel(page: Page, evi_base_url: str):
    """The 🗂 button opens the dispatch overlay listing live sessions."""
    page.goto(evi_base_url)
    # create a session first so it shows up
    page.fill("#input", "hi")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20000)

    page.click("#dispatch-btn")
    overlay = page.locator(".dispatch-overlay")
    expect(overlay).to_be_visible()
    expect(overlay).to_contain_text("Sessions")
    expect(overlay).to_contain_text("Workflows")
    # at least the current session row with an Open button
    expect(overlay.locator("button", has_text="Open").first).to_be_visible()
    overlay.locator("[data-close]").click()
    expect(page.locator(".dispatch-overlay")).to_have_count(0)


# ---- Voice settings (Phase 91) -------------------------------------------


def test_settings_voice_section(page: Page, evi_base_url: str):
    """Settings → Voice shows the TTS engine selector."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('voice')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    content = page.locator("#settings-content")
    expect(content).to_contain_text("TTS engine", timeout=10000)
    # the engine <select> offers the four engines
    expect(content.locator("select")).to_contain_text("coqui")


def test_guardrails_editor(page: Page, evi_base_url: str):
    """Settings → Guardrails loads the editor, saves valid TOML, rejects bad."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('guardrails')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    editor = page.locator("#gr-editor")
    expect(editor).to_be_visible(timeout=10000)
    # save valid TOML
    editor.fill('enabled = true\n[[rule]]\nname = "k"\npattern = "secret"\naction = "block"\n')
    page.click("#gr-save")
    expect(page.locator("#gr-status")).to_have_text("Saved", timeout=10000)
    # invalid TOML surfaces an error, not a crash
    page.locator("#gr-editor").fill('[[rule]]\nname="x"\npattern="([bad"\n')
    page.click("#gr-save")
    expect(page.locator("#gr-status")).to_contain_text("Error", timeout=10000)


def test_plugins_browser(page: Page, evi_base_url: str, tmp_path):
    """Settings → Plugins lists installed plugins and can install from a local
    directory, then remove — a real round-trip against the running server."""
    src = tmp_path / "e2e-plugin"
    src.mkdir()
    (src / "plugin.toml").write_text(
        'name = "e2e-plugin"\nversion = "9.9.9"\ndescription = "from e2e"\n',
        encoding="utf-8",
    )
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('plugins')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#plugins-box")).to_be_visible(timeout=10000)
    # install from a local directory
    page.fill("#pl-source", str(src))
    page.click("#pl-add")
    row = page.locator(".pl-row", has_text="e2e-plugin")
    expect(row).to_be_visible(timeout=10000)
    # remove it again to restore the empty state for other tests
    page.locator('.pl-row button[data-remove="e2e-plugin"]').click()
    expect(page.locator('.pl-row button[data-remove="e2e-plugin"]')).to_have_count(
        0, timeout=10000
    )


def test_usage_stats_panel(page: Page, evi_base_url: str):
    """Settings → Usage renders the stats panel with range buttons. The e2e home
    has no transcripts, so it shows the empty-state message gracefully."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('stats')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#stats-box")).to_be_visible(timeout=10000)
    # the range selector offers All / 30d / 7d
    expect(page.locator("#stats-box button", has_text="All")).to_be_visible()
    expect(page.locator("#stats-box button", has_text="7d")).to_be_visible()
    # body resolves to either the summary rows or the no-transcripts message
    expect(page.locator("#stats-body")).to_be_visible(timeout=10000)


def test_evals_panel(page: Page, evi_base_url: str):
    """Settings → Evals lists the seeded suite and runs it against the fake
    backend — a deterministic 1-of-2 pass (greets ✓, missing ✗)."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('evals')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#evals-box")).to_be_visible(timeout=10000)
    card = page.locator(".eval-suite", has_text="smoke")
    expect(card).to_be_visible()
    card.locator('button[data-run="smoke"]').click()
    expect(card.locator(".eval-status")).to_contain_text("1/2 passed", timeout=30000)
    expect(card.locator('.eval-case[data-case="greets"] .eval-mark')).to_have_text("✓")
    expect(card.locator('.eval-case[data-case="missing"] .eval-mark')).to_have_text("✗")


def test_route_indicator(page: Page, evi_base_url: str):
    """A turn whose text matches a route shows the routed model + route on the
    model chip; a plain turn shows just the default model. (Routing is keyword-
    based, so this is deterministic against the fake backend.)"""
    page.goto(evi_base_url)
    label = page.locator("#model-btn-label")
    # routing keyword -> code route -> chip shows "fake-coder (code)"
    page.fill("#input", "please debug this stack trace")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20000)
    expect(label).to_contain_text("(code)", timeout=20000)
    expect(label).to_contain_text("fake-coder")
    # a plain turn -> default model, no route tag
    page.fill("#input", "hello there")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20000)
    expect(label).not_to_contain_text("(code)", timeout=20000)


def test_automation_routes_crud(page: Page, evi_base_url: str):
    """Settings → Routes & Recipes: add a route via the form, see it listed,
    then remove it."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('automation')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#automation-box")).to_be_visible(timeout=10000)
    page.fill("#rt-name", "e2e-route")
    page.fill("#rt-model", "some-model")
    page.fill("#rt-kw", "alpha, beta")
    page.click("#rt-add")
    row = page.locator(".rt-row", has_text="e2e-route")
    expect(row).to_be_visible(timeout=10000)
    expect(row).to_contain_text("some-model")
    page.locator('.rt-row button[data-rt-remove="e2e-route"]').click()
    expect(page.locator('.rt-row button[data-rt-remove="e2e-route"]')).to_have_count(
        0, timeout=10000
    )


def test_automation_recipe_run(page: Page, evi_base_url: str):
    """The seeded smoke recipe runs against the fake backend and shows output."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('automation')")
    card = page.locator(".recipe-card", has_text="smoke")
    expect(card).to_be_visible(timeout=10000)
    card.locator('button[data-recipe-run="smoke"]').click()
    # the fake backend always replies "Hello from the fake backend! …"
    expect(card.locator(".recipe-out")).to_contain_text("Hello", timeout=30000)


def test_peers_panel(page: Page, evi_base_url: str):
    """Settings → Peers: add the running e2e server as a peer of itself — the
    status probe must come back reachable (a real /api/health round-trip) —
    then remove it. Also exercises the scan endpoint deterministically by
    sweeping only 127.0.0.1 on the server's own port."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('peers')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#peers-box")).to_be_visible(timeout=10000)
    expect(page.locator("#pr-scan")).to_be_visible()

    # add the e2e server itself as a peer -> reachable, version shown
    page.fill("#pr-name", "self")
    page.fill("#pr-url", evi_base_url)
    page.click("#pr-add")
    row = page.locator(".peer-row", has_text="self")
    expect(row).to_be_visible(timeout=10000)
    expect(row).to_contain_text("eVi ", timeout=10000)  # reachable fingerprint

    # deterministic scan: only 127.0.0.1 on our own port (no LAN sweep in CI)
    port = int(evi_base_url.rsplit(":", 1)[1])
    found = page.evaluate(
        """async (port) => {
            const r = await fetch('/api/peers/scan', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({hosts: ['127.0.0.1'], port}),
            });
            return (await r.json()).found;
        }""",
        port,
    )
    assert len(found) == 1 and found[0]["host"] == "127.0.0.1"

    page.locator('.peer-row button[data-peer-remove="self"]').click()
    expect(page.locator('.peer-row button[data-peer-remove="self"]')).to_have_count(
        0, timeout=10000
    )


def test_hooks_editor(page: Page, evi_base_url: str):
    """Settings → Hooks loads the editor, saves valid TOML, and rejects a
    typo'd event name (which the runtime loader would silently skip)."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('hooks')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    editor = page.locator("#hk-editor")
    expect(editor).to_be_visible(timeout=10000)
    editor.fill('[[before_tool_call]]\nname = "audit"\nmatch = "*"\ncommand = ["echo", "hi"]\n')
    page.click("#hk-save")
    expect(page.locator("#hk-status")).to_have_text("Saved", timeout=10000)
    # the summary chip for the saved hook appears after re-render
    expect(page.locator("#hooks-box")).to_contain_text("audit", timeout=10000)
    # a typo'd event errors instead of saving
    page.locator("#hk-editor").fill('[[before_toolcall]]\nname = "x"\ncommand = ["echo"]\n')
    page.click("#hk-save")
    expect(page.locator("#hk-status")).to_contain_text("Error", timeout=10000)
    # restore an empty file so other tests see a clean slate
    page.locator("#hk-editor").fill("# none\n")
    page.click("#hk-save")
    expect(page.locator("#hk-status")).to_have_text("Saved", timeout=10000)


def test_ultracode_panel(page: Page, evi_base_url: str):
    """Settings → Ultracode runs a real pipeline against the fake backend
    (breadth=1, rounds=0 → decompose + solve + synthesize = 3 stages)."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('ultracode')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#ultra-box")).to_be_visible(timeout=10000)
    page.fill("#uc-breadth", "1")
    page.fill("#uc-rounds", "0")
    page.fill("#uc-task", "say hello to the world in a friendly way")
    page.click("#uc-run")
    expect(page.locator("#uc-status")).to_contain_text("stages done", timeout=30000)
    expect(page.locator("#uc-stages details")).to_have_count(3, timeout=10000)
    expect(page.locator("#uc-answer")).to_contain_text("Hello", timeout=10000)


def test_mcp_panel(page: Page, evi_base_url: str):
    """Settings → MCP: add a server via the form, toggle it off/on, remove it.
    Pure mcp.json file ops — no MCP server is actually launched."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('mcp')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#mcp-box")).to_be_visible(timeout=10000)

    page.fill("#mcp-name", "e2e-fs")
    page.fill("#mcp-command", "npx")
    page.fill("#mcp-args", "-y @modelcontextprotocol/server-filesystem C:/tmp")
    page.click("#mcp-add")
    row = page.locator(".mcp-row", has_text="e2e-fs")
    expect(row).to_be_visible(timeout=10000)
    expect(row).to_contain_text("server-filesystem")

    # toggle off → button flips to Enable
    row.locator('button[data-mcp-toggle="e2e-fs"]').click()
    expect(
        page.locator('.mcp-row', has_text="e2e-fs").locator("button", has_text="Enable")
    ).to_be_visible(timeout=10000)

    page.locator('.mcp-row button[data-mcp-remove="e2e-fs"]').click()
    expect(page.locator('.mcp-row button[data-mcp-remove="e2e-fs"]')).to_have_count(
        0, timeout=10000
    )


@pytest.mark.parametrize(
    "section,title",
    [
        ("general", "General"),
        ("model", "Model & Backend"),
        ("tools", "Tools"),
        ("permissions", "Permissions"),
        ("context", "Context"),
        ("integrations", "Integrations"),
        ("server", "Server"),
        ("voice", "Voice"),
        ("guardrails", "Guardrails"),
        ("plugins", "Plugins"),
        ("stats", "Usage"),
        ("evals", "Evals"),
        ("automation", "Routes & Recipes"),
        ("peers", "Peers"),
        ("mcp", "MCP"),
        ("hooks", "Hooks"),
        ("ultracode", "Ultracode"),
        ("about", "About"),
    ],
)
def test_every_settings_section_renders(page: Page, evi_base_url: str, section, title):
    """Every settings screen opens + renders its nav entry with no console
    errors — covers all sections in one sweep."""
    errors = _console_errors(page)
    page.goto(evi_base_url)
    page.evaluate(f"window.eviUI.openSettings('{section}')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#settings-nav button", has_text=title)).to_be_visible()
    expect(page.locator("#settings-content")).to_be_visible()
    assert errors == [], f"console errors on settings/{section}: {errors}"
