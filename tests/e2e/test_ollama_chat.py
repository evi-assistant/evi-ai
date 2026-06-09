"""E2E with a REAL local Ollama model — so the streamed reply is a genuine
model response, not a canned fake. Skips cleanly when Ollama isn't running
(see the `evi_ollama_url` fixture). Run: `pytest tests/e2e -m e2e`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

_LLM_TIMEOUT = 120_000  # a small local model can still take a while cold


def _console_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def test_real_ollama_reply_renders(page: Page, evi_ollama_url: str):
    """Send a real prompt to a local model and confirm a non-empty reply
    streams in and the working-status indicator clears afterward."""
    errors = _console_errors(page)
    page.goto(evi_ollama_url)
    page.fill("#input", "Reply with exactly one word: PONG")
    page.click("#send")

    assistant = page.locator(".msg.assistant").last
    expect(assistant).to_be_visible(timeout=_LLM_TIMEOUT)
    # a genuine, non-empty model response rendered
    expect(assistant).not_to_have_text("", timeout=_LLM_TIMEOUT)
    text = (assistant.inner_text() or "").strip()
    assert len(text) > 0

    # the indicator went away once the turn finished
    expect(page.locator("#work-status")).to_be_hidden(timeout=_LLM_TIMEOUT)
    assert errors == [], f"console errors during real chat: {errors}"


def test_real_ollama_usage_chip(page: Page, evi_ollama_url: str):
    """After a real turn, the usage chip reflects real token counts."""
    page.goto(evi_ollama_url)
    page.fill("#input", "Say hi.")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=_LLM_TIMEOUT)
    chip = page.locator("#chip-usage")
    expect(chip).to_be_visible(timeout=_LLM_TIMEOUT)
    expect(chip).to_contain_text("token", timeout=_LLM_TIMEOUT)
