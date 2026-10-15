"""E2E: Settings says when an edit hasn't been saved yet (1.0.42).

The fields and the health banners reflect *saved* config, so an edit does
nothing until Save. Editing shows an amber "● Unsaved changes — click Save to
apply"; Save turns the status green, a failed save red (with Save still
enabled to retry), and a banner moves only once the change is saved.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

CUE = "● Unsaved changes — click Save to apply"
AMBER, GREEN, RED = "rgb(224, 165, 46)", "rgb(76, 175, 80)", "rgb(224, 82, 82)"


def _cfg(page: Page) -> dict:
    return page.evaluate("() => fetch('/api/config').then(r => r.json())")


def _post_cfg(page: Page, patch: dict) -> None:
    page.evaluate(
        """(p) => fetch('/api/config', {method: 'POST',
             headers: {'Content-Type': 'application/json'}, body: JSON.stringify(p)})""",
        patch,
    )


def _open(page: Page, section: str) -> None:
    page.evaluate(f"window.eviUI.openSettings('{section}')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#settings-content h3")).to_be_visible(timeout=10_000)


def test_unsaved_cue_then_green_saved_and_red_failure(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    before = _cfg(page)["statusline"]["enabled"]
    _open(page, "statusline")
    status, save = page.locator("#settings-status"), page.locator("#settings-save")
    toggle = page.locator('.set-toggle[aria-label="Enable status line"]')
    try:
        expect(save).to_be_disabled()
        expect(status).to_have_text("")

        toggle.click()
        expect(status).to_have_text(CUE)
        expect(status).to_have_css("color", AMBER)
        expect(save).to_be_enabled()

        save.click()
        expect(status).to_have_text("Saved.", timeout=10_000)
        expect(status).to_have_css("color", GREEN)
        expect(save).to_be_disabled()
        assert _cfg(page)["statusline"]["enabled"] is (not before)

        # A failed save is red, keeps the edit, and leaves Save enabled to retry.
        page.locator('.set-toggle[aria-label="Enable status line"]').click()
        expect(status).to_have_text(CUE)
        page.route("**/api/config", lambda r: r.fulfill(status=500, json={"detail": "boom"})
                   if r.request.method == "POST" else r.continue_())
        save.click()
        expect(status).to_have_text("Save failed: HTTP 500", timeout=10_000)
        expect(status).to_have_css("color", RED)
        expect(save).to_be_enabled()
        assert _cfg(page)["statusline"]["enabled"] is (not before)   # nothing written

        page.unroute("**/api/config")
        save.click()
        expect(status).to_have_text("Saved.", timeout=10_000)
        assert _cfg(page)["statusline"]["enabled"] is before
    finally:
        page.unroute("**/api/config")
        _post_cfg(page, {"statusline": {"enabled": before}})


def test_banner_moves_only_after_save(page: Page, evi_base_url: str):
    """Settings → Server: turning on A2A with no access token gets flagged —
    but only once the change is saved."""
    page.goto(evi_base_url)
    cfg = _cfg(page)
    assert cfg["web"]["auth_token"] == "" and not cfg["federation"]["a2a"], cfg
    _open(page, "server")
    banner = page.locator("#health-server")
    expect(banner).to_contain_text("Localhost only", timeout=10_000)
    try:
        page.locator('.set-toggle[aria-label="A2A interop endpoint"]').click()
        expect(page.locator("#settings-status")).to_have_text(CUE)
        expect(banner).to_contain_text("Localhost only")      # unsaved: nothing changed yet

        page.click("#settings-save")
        expect(page.locator("#settings-status")).to_have_text("Saved.", timeout=10_000)
        # The banner's wording is the Server item's to change; what it must
        # say is that a network surface is on with no token.
        expect(page.locator("#health-server")).to_contain_text("no token", timeout=10_000)
        expect(page.locator("#health-server")).not_to_contain_text("Localhost only")

        page.locator('.set-toggle[aria-label="A2A interop endpoint"]').click()
        page.click("#settings-save")
        expect(page.locator("#health-server")).to_contain_text("Localhost only",
                                                               timeout=10_000)
    finally:
        _post_cfg(page, {"federation": {"a2a": False}})
