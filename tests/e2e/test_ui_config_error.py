"""A config.toml that can't be parsed: the UI still comes up (on the default
settings) and a banner says the settings file couldn't be read, with the
reason, and that changes won't be saved; the file is left as it was."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_banner_for_an_unreadable_settings_file(page: Page, evi_broken_config):
    base, path = evi_broken_config
    before = path.read_text(encoding="utf-8")
    page.goto(base)
    banner = page.locator("#config-banner")
    expect(banner).to_be_visible(timeout=15000)
    expect(banner).to_contain_text("Your settings file couldn't be read.")
    expect(banner).to_contain_text("won't be saved")
    expect(banner).to_contain_text("TOMLDecodeError")  # the reason, as text
    assert banner.locator("code").count() == 1
    # A settings change through the API is refused, and the file is untouched.
    r = httpx.post(base + "/api/config", json={"llm": {"temperature": 0.3}}, timeout=10)
    assert r.status_code == 409
    assert path.read_text(encoding="utf-8") == before


def test_no_banner_when_the_settings_file_is_fine(page: Page, evi_base_url):
    page.goto(evi_base_url)
    page.wait_for_function("() => document.getElementById('meta').textContent.startsWith('model=')",
                           timeout=15000)
    expect(page.locator("#config-banner")).to_be_hidden()
