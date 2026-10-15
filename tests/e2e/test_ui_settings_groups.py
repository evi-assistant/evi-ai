"""E2E: Settings are grouped instead of stacked (1.0.50).

Twenty-odd sections sit under **Assistant**, **This computer**, **Extend** and
**App**. A section no group names still appears, under "More", so adding one
can never make it vanish; and because grouping reorders the list, the active
entry is scrolled into view when Settings opens.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

GROUPS = ["Assistant", "This computer", "Extend", "App"]
# A few members per group, as the release described them.
MEMBERS = {
    "Assistant": ["Model & Backend", "Tools", "Specialty models"],
    "This computer": ["Permissions", "Guardrails", "Voice", "Server"],
    "Extend": ["Integrations", "Channels", "Plugins", "MCP", "Peers"],
    "App": ["General", "Usage", "About"],
}


def _nav(page: Page) -> list[tuple[str, list[str]]]:
    """The nav as [(group header, [section titles…]), …], in display order."""
    return [(g, items) for g, items in page.evaluate("""() => {
        const out = [];
        for (const el of document.getElementById('settings-nav').children) {
          if (el.tagName === 'BUTTON') out[out.length - 1][1].push(el.textContent);
          else out.push([el.textContent, []]);
        }
        return out;
    }""")]


def _open(page: Page, section: str) -> None:
    page.evaluate(f"window.eviUI.openSettings('{section}')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#settings-nav button.active")).to_have_count(1, timeout=10_000)


def test_sections_are_grouped(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    _open(page, "general")
    nav = _nav(page)
    assert [g for g, _ in nav] == GROUPS, nav      # no "More": every section is placed
    by_group = dict(nav)
    for group, titles in MEMBERS.items():
        for t in titles:
            assert t in by_group[group], (group, t, by_group[group])
    flat = [t for _, items in nav for t in items]
    assert len(flat) == len(set(flat)), flat        # each section exactly once
    assert "Desktop" not in flat                    # desktop-only, hidden in a browser
    # Picking an entry still opens it.
    page.locator("#settings-nav button", has_text="Channels").click()
    expect(page.locator("#settings-content h3")).to_have_text("Channels")
    expect(page.locator("#settings-nav button.active")).to_have_text("Channels")


def test_ungrouped_section_lands_under_more(page: Page, evi_base_url: str):
    """Serve the page with one extra section that no group names: it must
    show up under "More" rather than silently disappear."""
    anchor = "{ id:'about', title:'About'"
    extra = "{ id:'e2e-extra', title:'E2E Extra', blurb:'In no group.', fields:[] },\n    "

    def patch(route):
        resp = route.fetch()
        body = resp.text()
        assert body.count(anchor) == 1, "SCHEMA anchor moved — update this test"
        route.fulfill(response=resp, body=body.replace(anchor, extra + anchor))

    page.route(evi_base_url + "/", patch)
    page.goto(evi_base_url)
    _open(page, "general")
    nav = _nav(page)
    assert [g for g, _ in nav] == GROUPS + ["More"], nav
    assert nav[-1][1] == ["E2E Extra"], nav
    page.locator("#settings-nav button", has_text="E2E Extra").click()
    expect(page.locator("#settings-content h3")).to_have_text("E2E Extra")


def test_active_section_is_scrolled_into_view(page: Page, evi_base_url: str):
    """Grouping moves sections down the list; Settings must not open with the
    selected entry below the fold, looking like nothing is selected."""
    page.set_viewport_size({"width": 1100, "height": 420})
    page.goto(evi_base_url)
    _open(page, "about")                          # the very last entry
    nav = page.locator("#settings-nav")
    assert nav.evaluate("n => n.scrollHeight > n.clientHeight"), "nav doesn't overflow here"
    expect(nav.locator("button.active")).to_have_text("About")
    expect(nav.locator("button.active")).to_be_in_viewport()
