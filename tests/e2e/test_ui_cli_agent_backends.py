"""Settings → Model & Backend: the "Add a backend" copy for the CLI-agent kinds.

Gemini CLI no longer serves free / Google AI Pro / Ultra "Login with Google"
accounts (2026-06-18), so the UI must point at a Gemini API key or Vertex AI,
and amp at its current package (@ampcode/cli). Real browser + real eVi server
+ the fake backend; nothing is added, only the form is driven.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _open_add_backend(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('model')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    content = page.locator("#settings-content")
    expect(content.locator("#bk-preset")).to_be_attached(timeout=10000)
    content.locator("#bk-preset").select_option("__custom__")
    return content


def test_gemini_kind_points_at_api_key_or_vertex(page: Page, evi_base_url: str):
    content = _open_add_backend(page, evi_base_url)
    kind = content.locator("#bk-kind")
    expect(kind.locator("option[value=gemini]")).to_have_text("gemini — Gemini API key / Vertex AI")
    expect(kind).not_to_contain_text("free login")
    kind.select_option("gemini")
    msg = content.locator("#bk-msg")
    expect(msg).to_contain_text("GEMINI_API_KEY")
    expect(msg).to_contain_text("Vertex AI")
    expect(msg).to_contain_text("2026-06-18")
    # A CLI-agent kind takes no URL and no key.
    expect(content.locator("#bk-url")).to_be_hidden()
    expect(content.locator("#bk-key")).to_be_hidden()


def test_amp_and_other_cli_kinds_hint(page: Page, evi_base_url: str):
    content = _open_add_backend(page, evi_base_url)
    kind, msg = content.locator("#bk-kind"), content.locator("#bk-msg")
    kind.select_option("amp")
    expect(msg).to_contain_text("npm i -g @ampcode/cli")
    expect(msg).to_contain_text("AMP_API_KEY")
    kind.select_option("copilot")
    expect(msg).to_contain_text("copilot uses the local CLI's subscription/login")
