"""Per-message action regression tests (deterministic, fake backend).

Guards two of the message-action bugs the audit found:

* #25 — the 🔄 re-roll button appeared on EVERY assistant bubble but always
  regenerated the latest turn. It must now show on the last assistant turn only.
* #23 — user/tool bubbles lost their Edit/Branch/Delete toolbar after a history
  rebuild (a ``textContent=`` wiped the appended actions). After a rebuild the
  toolbar must still be there.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _send(page: Page, text: str) -> None:
    n = page.locator(".msg.assistant").count()
    page.fill("#input", text)
    page.click("#send")
    # wait for a NEW assistant bubble to finish appearing
    expect(page.locator(".msg.assistant")).to_have_count(n + 1, timeout=20_000)
    expect(page.locator(".msg.assistant").last).to_contain_text("fake backend", timeout=20_000)


def test_reroll_button_only_on_last_assistant(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    _send(page, "first")
    _send(page, "second")

    # Exactly one re-roll button is shown, and it's inside the last assistant
    # bubble (updateRerollButtons hides it on earlier ones via display:none).
    result = page.evaluate(
        """() => {
            const shown = [...document.querySelectorAll('.reroll-btn')]
                .filter(b => b.style.display !== 'none');
            const assistants = [...document.querySelectorAll('.msg.assistant')];
            const last = assistants[assistants.length - 1];
            return { count: shown.length, inLast: shown.length === 1 && last.contains(shown[0]),
                     assistants: assistants.length };
        }"""
    )
    assert result["assistants"] == 2
    assert result["count"] == 1, f"expected 1 visible re-roll, got {result['count']}"
    assert result["inLast"], "the visible re-roll button is not in the last assistant bubble"


def test_user_bubble_keeps_toolbar_after_rebuild(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    _send(page, "hello")

    # Force a history rebuild: open a second (empty) tab, then switch back to the
    # first — switchTab() calls rebuildHistory(), which re-renders the bubbles.
    page.click(".tab.new-tab")
    expect(page.locator(".msg.user")).to_have_count(0, timeout=10_000)  # empty tab
    page.click('.tab[data-i="0"]')
    expect(page.locator(".msg.user")).to_have_count(1, timeout=10_000)  # rebuilt

    # The user bubble must still carry its action toolbar (it was wiped by a
    # textContent= before the fix).
    has_actions = page.evaluate(
        "() => { const u = document.querySelector('.msg.user');"
        "        return !!(u && u.querySelector('.msg-actions')); }"
    )
    assert has_actions, "user bubble lost its action toolbar after a rebuild"
