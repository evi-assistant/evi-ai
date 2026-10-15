"""E2E: starter prompts in an empty chat (1.0.50).

A blank transcript offers *Say hi* · *What can you do?* · *Give me a quick
tour*; any reply proves the model, tools and settings work together. They are
for an empty chat only: a real message replaces them, a restored conversation
never shows them, and if nothing can answer they stay put instead of leaving a
blank transcript.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

LABELS = ["Say hi 👋", "What can you do?", "Give me a quick tour"]


def test_empty_chat_offers_three_starters(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    empty = page.locator("#chat-empty")
    expect(empty).to_be_visible(timeout=10_000)
    expect(empty).to_contain_text("How can I help?")
    expect(empty.locator(".starter")).to_have_text(LABELS)


def test_starter_sends_its_prompt_and_gives_way(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    page.locator("#chat-empty .starter", has_text="What can you do?").click()
    expect(page.locator("#log .msg.user").first).to_contain_text(
        "Give me a short, concrete list")
    expect(page.locator("#log .msg.assistant").last).to_contain_text(
        "fake backend", timeout=20_000)
    expect(page.locator("#chat-empty")).to_have_count(0)


def test_starters_survive_when_nothing_answers(page: Page, evi_base_url: str):
    """Regression (1.0.50 review): a click with no reachable backend wiped the
    prompts and left a blank transcript with nothing to click."""
    page.route("**/api/backend/status", lambda r: r.fulfill(json={
        "configured": {"backend": "openai_compat", "base_url": "http://127.0.0.1:9/v1",
                       "model": "fake", "reachable": False},
        # Something else is running, so this is the banner state, not the
        # first-run screen that would cover the chat.
        "candidates": [{"kind": "ollama", "url": "http://localhost:11434/v1", "reachable": True}],
        "any_reachable": True, "runtime_supported": False, "suggestions": [],
    }))
    page.goto(evi_base_url)
    expect(page.locator("#backend-banner")).to_be_visible(timeout=10_000)
    starters = page.locator("#chat-empty .starter")
    expect(starters).to_have_count(3, timeout=10_000)
    starters.first.click()
    # Nothing was sent; the prompt waits in the composer and the starters stay.
    expect(page.locator("#input")).to_have_value("Say hi in one short sentence.")
    expect(page.locator("#log .msg")).to_have_count(0)
    expect(starters).to_have_text(LABELS)


def test_new_chat_gets_starters_a_restored_one_does_not(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    page.fill("#input", "hello there")
    page.click("#send")
    expect(page.locator("#log .msg.assistant").last).to_contain_text(
        "fake backend", timeout=20_000)

    page.locator("#tabs [data-new]").click()
    expect(page.locator("#chat-empty .starter")).to_have_count(3)

    page.locator("#tabs .tab[data-i='0']").click()          # back to the conversation
    expect(page.locator("#log .msg.assistant")).to_have_count(1, timeout=10_000)
    expect(page.locator("#chat-empty")).to_have_count(0)

    page.reload()                                            # restored from the server
    expect(page.locator("#log .msg.assistant")).to_have_count(1, timeout=10_000)
    page.wait_for_timeout(1_000)    # past the deferred empty-state check (600 ms)
    expect(page.locator("#chat-empty")).to_have_count(0)
