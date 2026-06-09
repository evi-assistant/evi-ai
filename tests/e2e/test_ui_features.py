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
