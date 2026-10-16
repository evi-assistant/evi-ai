"""E2E: Settings → Permissions separates reading code from running it (Phase 0
B6, code-exec parity).

"code_read" (reading and analysing code) is its own auto-approve category,
apart from "code" (running it), each with a line saying what it lets run
unasked. A list saved before code_read existed still holds the old default's
"code", which now auto-approves running code: Settings says so in a banner and
switches it to the new default in one click ("code" → "code_read", every
other entry kept; evi.permissions.auto_approve_migration), saved at once
through the config API.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

# The new default, and the old one plus a category of the user's own, which
# the switch keeps.
DEFAULT = ["fs", "code_read", "memory", "skills", "image"]
LEGACY = ["fs", "code", "memory", "skills", "image", "web"]
MIGRATED = ["fs", "code_read", "memory", "skills", "image", "web"]


def _cfg(page: Page) -> dict:
    return page.evaluate("() => fetch('/api/config').then(r => r.json())")


def _post_cfg(page: Page, patch: dict) -> None:
    page.evaluate(
        """(p) => fetch('/api/config', {method: 'POST',
             headers: {'Content-Type': 'application/json'}, body: JSON.stringify(p)})""",
        patch,
    )


def _open_permissions(page: Page) -> None:
    page.evaluate("window.eviUI.openSettings('permissions')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#settings-content h3")).to_have_text("Permissions", timeout=10_000)


def test_code_read_toggle_and_the_migration_banner(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    before = _cfg(page)["auto"]["auto_approve"]
    code_read = page.locator('.set-check[data-category="code_read"]')
    code = page.locator('.set-check[data-category="code"]')
    banner = page.locator("#auto-approve-migration")
    status, save = page.locator("#settings-status"), page.locator("#settings-save")
    try:
        # The new default: code_read on, code off, no banner.
        _post_cfg(page, {"auto": {"auto_approve": DEFAULT}})
        assert _cfg(page)["auto_approve_migration"] is None
        _open_permissions(page)
        expect(code_read.locator("input")).to_be_checked()
        expect(code.locator("input")).not_to_be_checked()
        expect(banner).to_have_count(0)
        # Each says what it lets run unasked, and they sit side by side.
        expect(code_read.locator(".set-check-why")).to_contain_text("not running it")
        expect(code_read.locator(".set-check-why")).to_contain_text("python_symbols")
        expect(code.locator(".set-check-why")).to_contain_text("Running code: run_python")
        assert page.evaluate(
            """() => { const rows = [...document.querySelectorAll('.set-check[data-category]')]
                         .map(r => r.dataset.category);
                       return rows.indexOf('code') - rows.indexOf('code_read'); }"""
        ) == 1
        page.locator("#settings-close").click()

        # A list saved with the old default: the banner explains and offers the fix.
        _post_cfg(page, {"auto": {"auto_approve": LEGACY}})
        assert _cfg(page)["auto_approve_migration"] == MIGRATED
        _open_permissions(page)
        expect(banner).to_contain_text("Running code is approved without asking")
        expect(banner).to_contain_text("Switch to: " + ", ".join(MIGRATED))
        expect(code.locator("input")).to_be_checked()
        expect(code_read.locator("input")).not_to_be_checked()

        # Toggling code_read on beside code keeps running code unasked on
        # purpose: saved, and the banner goes.
        code_read.locator("input").check()
        expect(status).to_contain_text("Unsaved changes")
        save.click()
        expect(status).to_contain_text("Saved", timeout=10_000)
        cfg = _cfg(page)
        assert cfg["auto"]["auto_approve"] == LEGACY + ["code_read"]
        assert cfg["auto_approve_migration"] is None
        expect(banner).to_have_count(0)

        # ...and off again brings it back.
        code_read.locator("input").uncheck()
        save.click()
        expect(status).to_contain_text("Saved", timeout=10_000)
        assert _cfg(page)["auto"]["auto_approve"] == LEGACY
        expect(banner).to_be_visible()

        # One click: "code" becomes "code_read", the other entries stay, saved now.
        banner.get_by_role("button", name="Switch to code_read").click()
        expect(status).to_contain_text("running code now asks first", timeout=10_000)
        cfg = _cfg(page)
        assert cfg["auto"]["auto_approve"] == MIGRATED
        assert cfg["auto_approve_migration"] is None
        expect(banner).to_have_count(0)
        expect(code_read.locator("input")).to_be_checked()
        expect(code.locator("input")).not_to_be_checked()
        expect(page.locator('.set-check[data-category="web"] input')).to_be_checked()
        expect(save).to_be_disabled()  # nothing left unsaved
    finally:
        _post_cfg(page, {"auto": {"auto_approve": before}})


def test_the_switch_keeps_other_unsaved_edits(page: Page, evi_base_url: str):
    """The switch saves only the auto-approve list: another edit made in the
    same section stays pending, and an unsaved edit of the list itself is
    replaced by the list the switch saved."""
    page.goto(evi_base_url)
    before = _cfg(page)["auto"]
    try:
        _post_cfg(page, {"auto": {"auto_approve": LEGACY}})
        _open_permissions(page)
        page.locator('.set-check[data-category="skills"] input').uncheck()  # an edit of the list
        page.locator("#settings-content select").first.select_option("accept_edits")  # mode
        page.locator("#auto-approve-migration").get_by_role(
            "button", name="Switch to code_read").click()
        expect(page.locator("#auto-approve-migration")).to_have_count(0, timeout=10_000)
        cfg = _cfg(page)
        assert cfg["auto"]["auto_approve"] == MIGRATED  # skills: the saved list, as shown
        assert cfg["auto"]["mode"] == before["mode"]   # not saved yet...
        expect(page.locator('.set-check[data-category="skills"] input')).to_be_checked()
        expect(page.locator("#settings-status")).to_contain_text("Unsaved changes")
        expect(page.locator("#settings-content select").first).to_have_value("accept_edits")
        page.locator("#settings-save").click()            # ...until Save
        expect(page.locator("#settings-status")).to_contain_text("Saved", timeout=10_000)
        assert _cfg(page)["auto"]["mode"] == "accept_edits"
    finally:
        _post_cfg(page, {"auto": {"auto_approve": before["auto_approve"], "mode": before["mode"]}})
