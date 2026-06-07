"""E2E UI smoke tests — a real browser (Playwright) against the real eVi web
server + a fake streaming LLM backend.

Run: `pytest tests/e2e -m e2e` (needs `pip install -e '.[e2e]'` +
`playwright install chromium`). Excluded from the default unit run.

These cover the layer unit tests can't: that the JS actually *renders* what the
server streams. `test_chat_renders_reply` is the regression guard for the
0.24.2 SSE-CRLF bug (server streamed fine, browser rendered nothing).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _console_errors(page: Page) -> list[str]:
    """Attach console.error + uncaught-exception collectors to a page."""
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def test_app_loads(page: Page, evi_base_url: str):
    errors = _console_errors(page)
    page.goto(evi_base_url)
    expect(page).to_have_title("eVi")
    expect(page.locator("#input")).to_be_visible()
    expect(page.locator("#send")).to_be_visible()
    assert errors == [], f"console errors on load: {errors}"


def test_chat_renders_reply(page: Page, evi_base_url: str):
    """The whole point: send a message and confirm the streamed reply RENDERS.
    Guards against the SSE-frame-separator class of bug (0.24.2)."""
    errors = _console_errors(page)
    page.goto(evi_base_url)
    page.fill("#input", "hello evi")
    page.click("#send")
    assistant = page.locator(".msg.assistant").last
    expect(assistant).to_be_visible(timeout=20000)
    expect(assistant).to_contain_text("fake backend", timeout=20000)
    assert errors == [], f"console errors during chat: {errors}"


def test_backend_configured_hides_banner(page: Page, evi_base_url: str):
    """With a reachable configured backend, the no-backend banner stays hidden
    (the 0.24.1 fix: gate on the configured backend, not 'any reachable')."""
    page.goto(evi_base_url)
    # trigger a status check, then the banner must be hidden
    page.fill("#input", "ping")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20000)
    expect(page.locator("#backend-banner")).to_be_hidden()


def test_settings_opens_and_renders(page: Page, evi_base_url: str):
    """The ⚙ button opens the settings screen with the section nav + General
    fields populated from /api/config."""
    errors = _console_errors(page)
    page.goto(evi_base_url)
    page.click("#settings-btn")
    expect(page.locator("#settings-overlay")).to_be_visible()
    # left-nav sections + the General heading
    expect(page.locator("#settings-nav button", has_text="Model & Backend")).to_be_visible()
    expect(page.locator("#settings-content h3")).to_contain_text("General")
    assert errors == [], f"console errors opening settings: {errors}"


def test_settings_persists_change(page: Page, evi_base_url: str):
    """Toggling a setting + Save writes through /api/config (round-trips). The
    e2e backend uses an isolated EVI_HOME, so this never touches a real config."""
    page.goto(evi_base_url)
    page.click("#settings-btn")
    expect(page.locator("#settings-overlay")).to_be_visible()
    # General's first toggle is Crash reporting (default off) — flip it on.
    page.locator("#settings-content .set-toggle").first.click()
    page.click("#settings-save")
    expect(page.locator("#settings-status")).to_contain_text("Saved", timeout=5000)
    persisted = page.evaluate(
        "fetch('/api/config').then(r => r.json()).then(c => c.telemetry.crash_reports)"
    )
    assert persisted is True


def test_docs_dialog_renders(page: Page, evi_base_url: str):
    """In-app documentation renders Markdown server-side (offline, no CDN)."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openDocs()")
    expect(page.locator("#dialog-overlay")).to_be_visible()
    expect(page.locator("#dialog-content h1")).to_be_visible(timeout=10000)
    # the page-switcher nav lists multiple docs
    assert page.locator("#dialog-content .docs-nav-link").count() >= 2


def test_diagnostics_dialog_renders(page: Page, evi_base_url: str):
    """Help → Run Diagnostics shows evi-doctor checks."""
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openDiagnostics()")
    expect(page.locator("#dialog-overlay")).to_be_visible()
    expect(page.locator("#dialog-content .set-about-row").first).to_be_visible(timeout=10000)
