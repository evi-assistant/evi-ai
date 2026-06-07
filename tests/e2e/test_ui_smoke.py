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
