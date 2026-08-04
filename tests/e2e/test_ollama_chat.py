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
    # Ignore resource-load / network failures (e.g. a CDN asset transiently
    # failing under load — "Failed to load resource: net::ERR_NO_BUFFER_SPACE").
    # Those are environmental, not app/JS bugs, and make the suite flaky.
    def _on_console(m):
        if m.type == "error" and "Failed to load resource" not in m.text and "net::ERR" not in m.text:
            errors.append(m.text)
    page.on("console", _on_console)
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


def _installed_ollama_models() -> set[str]:
    import os

    import httpx

    base = os.environ.get("EVI_TEST_OLLAMA", "http://127.0.0.1:11434")
    try:
        return {m["name"] for m in httpx.get(f"{base}/api/tags", timeout=5).json().get("models", [])}
    except Exception:  # noqa: BLE001
        return set()


def test_switch_between_two_real_models(page: Page, evi_ollama_url: str):
    """Prove the whole model-switch path with TWO real local models: pick each in
    the footer picker, send a prompt, and confirm a genuine reply streams from the
    switched-to model (the backend client is rebuilt on switch). Also exercises
    the no-registry backend-resolve fix against a real backend."""
    wanted = [m for m in ("llama3.2:1b", "qwen2.5:3b") if m in _installed_ollama_models()]
    if len(wanted) < 2:
        pytest.skip("needs two small models (llama3.2:1b + qwen2.5:3b) installed")

    page.goto(evi_ollama_url)
    for model in wanted:
        # Open the picker via Ctrl+I (idempotent, and doesn't depend on the
        # footer button being un-covered — more robust than clicking #model-btn
        # under load).
        page.keyboard.press("Escape")
        page.keyboard.press("Control+i")
        row = page.locator(f'.picker-row[data-action="model"][data-value="{model}"]')
        expect(row).to_be_visible(timeout=10_000)
        row.click()
        # The footer reflects the switched-to model (family name is enough).
        expect(page.locator("#model-btn-label")).to_contain_text(
            model.split(":")[0], timeout=10_000
        )
        page.keyboard.press("Escape")  # get the picker off the composer
        expect(page.locator("#picker-card")).to_be_hidden()

        # A genuine turn served by the switched-to model. Submit with Enter
        # (form.requestSubmit) rather than clicking #send — keyboard events don't
        # need the footer button to be un-covered, which keeps this robust when
        # the full suite makes page interactions janky under load.
        page.fill("#input", "Reply with exactly one word: OK")
        page.press("#input", "Enter")
        assistant = page.locator(".msg.assistant").last
        expect(assistant).to_be_visible(timeout=_LLM_TIMEOUT)
        # A genuine, non-empty reply from the switched-to model proves the switch
        # + client rebuild worked. (We don't assert on work-status settling here —
        # Ollama swapping model weights between the two makes that timing flaky.)
        expect(assistant).not_to_have_text("", timeout=_LLM_TIMEOUT)
