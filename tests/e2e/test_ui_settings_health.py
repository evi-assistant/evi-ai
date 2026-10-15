"""E2E: Settings "How this works" explainers and live health banners
(1.0.39 / 1.0.40 / 1.0.41).

Each banner is computed from real state: the backend probe, the integrations /
voice / specialty / guardrails probes, and plugins whose manifest failed to
load. The probes that reach outside the machine (ComfyUI, a TTS engine, a
specialty model server) are stubbed in the browser; the backend banner and the
plugin banner run against the real server.
"""

from __future__ import annotations

import shutil

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _open(page: Page, section: str) -> None:
    page.evaluate(f"window.eviUI.openSettings('{section}')")
    expect(page.locator("#settings-overlay")).to_be_visible()
    expect(page.locator("#settings-content h3")).to_be_visible(timeout=10_000)


def _goto(page: Page, title: str) -> None:
    page.locator("#settings-nav button", has_text=title).click()
    expect(page.locator("#settings-content h3")).to_have_text(title)


def test_explainer_is_collapsed_until_asked(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    _open(page, "permissions")
    help_ = page.locator("#settings-content details.sec-help")
    expect(help_).to_have_count(1)
    expect(help_.locator("summary")).to_have_text("How this works")
    body = help_.locator("div")
    expect(body).to_be_hidden()
    help_.locator("summary").click()
    expect(body).to_be_visible()
    expect(body).to_contain_text("hard_deny")


def test_backend_banner_follows_the_probe(page: Page, evi_base_url: str):
    """Model & Backend: green when the configured backend answers (the real
    fake backend), red when the probe says nothing does."""
    page.goto(evi_base_url)
    _open(page, "model")
    banner = page.locator("#health-model")
    expect(banner).to_contain_text("Backend reachable", timeout=10_000)
    expect(banner).to_contain_text("✓")

    page.route("**/api/backend/status", lambda r: r.fulfill(json={
        "configured": {"backend": "openai_compat", "base_url": "http://127.0.0.1:9/v1",
                       "model": "fake", "reachable": False},
        "candidates": [{"kind": "ollama", "url": "http://localhost:11434/v1", "reachable": True}],
        "any_reachable": True, "suggestions": [],
    }))
    _goto(page, "Tools")
    _goto(page, "Model & Backend")
    expect(page.locator("#health-model")).to_contain_text("Backend not answering", timeout=10_000)
    expect(page.locator("#health-model")).to_contain_text("✕")


def test_integrations_and_specialty_banners(page: Page, evi_base_url: str):
    page.route("**/api/integrations/status", lambda r: r.fulfill(json={
        "comfy": {"configured_url": "http://127.0.0.1:8188", "reachable": False},
        "searxng": {"active": False},
    }))
    page.route("**/api/specialty/status", lambda r: r.fulfill(json={
        "ocr": {"set": True, "reachable": False}, "vision": {"set": False},
        "guard": {"set": False}, "stt": {"set": False},
    }))
    page.goto(evi_base_url)
    _open(page, "integrations")
    expect(page.locator("#health-integrations")).to_contain_text("ComfyUI unreachable",
                                                                 timeout=10_000)

    _goto(page, "Specialty models")
    banner = page.locator("#health-specialty")
    expect(banner).to_contain_text("Specialty backend unreachable", timeout=10_000)
    expect(banner).to_contain_text("ocr")
    # Its call to action jumps to the panel that fixes it.
    banner.locator("[data-health-cta]", has_text="Open Model panel").click()
    expect(page.locator("#settings-content h3")).to_have_text("Model & Backend")
    expect(page.locator("#settings-nav button.active")).to_have_text("Model & Backend")


def test_voice_banner_hint_has_no_double_period(page: Page, evi_base_url: str):
    """1.0.41: the server's install hint ends in a period and the banner adds
    its own, which rendered "…neural engine..". Then the "Use system voice"
    call to action switches engines and the banner re-checks."""
    voice = {"available": False, "engine": "system",
             "how_to_enable": "Install espeak-ng, or pick a neural engine."}
    page.route("**/api/voice/status", lambda r: r.fulfill(json=voice))
    page.goto(evi_base_url)
    _open(page, "voice")
    banner = page.locator("#health-voice")
    expect(banner).to_contain_text("No system voice found", timeout=10_000)
    expect(banner).to_contain_text("pick a neural engine.")
    expect(banner).not_to_contain_text("..")

    voice = {"available": False, "engine": "piper",
             "how_to_enable": "Install the piper CLI (pip install piper-tts)."}
    _goto(page, "Notifications")
    _goto(page, "Voice")
    banner = page.locator("#health-voice")
    expect(banner).to_contain_text("Engine not installed", timeout=10_000)
    expect(banner).to_contain_text("(pip install piper-tts), or switch back")

    voice = {"available": True, "engine": "system", "detail": "espeak-ng"}
    banner.locator("[data-health-cta]", has_text="Use system voice").click()
    expect(page.locator("#health-voice")).to_contain_text("Voice engine ready", timeout=10_000)
    engine = page.evaluate(
        "() => fetch('/api/config').then(r => r.json()).then(c => c.voice.engine)")
    assert engine == "system"


def test_guardrails_banner_warns_when_guard_model_is_down(page: Page, evi_base_url: str):
    page.route("**/api/guardrails", lambda r: r.fulfill(json={
        "raw": "enabled = true\n", "enabled": True,
        "summary": {"regex": 0, "judge": 1, "classifier": 0},
        "rules": [], "judge": [{"name": "toxicity", "applies_to": "input"}], "classifier": [],
    }) if r.request.method == "GET" else r.continue_())
    page.route("**/api/specialty/status", lambda r: r.fulfill(json={
        "ocr": {"set": False}, "vision": {"set": False},
        "guard": {"set": True, "reachable": False}, "stt": {"set": False},
    }))
    page.goto(evi_base_url)
    _open(page, "guardrails")
    banner = page.locator("#health-guardrails")
    expect(banner).to_contain_text("Guard model unreachable", timeout=10_000)
    expect(banner).to_contain_text("fail open")


def test_plugins_banner_names_a_plugin_that_failed_to_load(
    page: Page, evi_base_url: str, evi_home
):
    """A plugin dir with no readable manifest used to vanish from the list."""
    broken = evi_home / "plugins" / "e2e-broken-plugin"
    broken.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(evi_base_url)
        _open(page, "plugins")
        banner = page.locator("#health-plugins")
        expect(banner).to_contain_text("Plugin failed to load", timeout=10_000)
        expect(banner).to_contain_text("e2e-broken-plugin")
    finally:
        shutil.rmtree(broken, ignore_errors=True)
    # Gone again once the bad dir is: the banner no longer flags it.
    _goto(page, "Usage")
    _goto(page, "Plugins")
    expect(page.locator("#health-plugins")).not_to_contain_text("e2e-broken-plugin",
                                                                timeout=10_000)
