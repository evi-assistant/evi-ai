"""Model-switch regression tests (deterministic, fake backend).

The footer model picker had a cluster of state bugs this suite pins:

* #3 — after switching models the footer capability chips stayed on the PREVIOUS
  model (applyPicker copied a subset of the POST response and never re-read
  active_capabilities).
* #1 — the RouteInfo event fired every turn and overwrote the footer label with a
  bare model id, wiping the capability chips / effort / fast suffix.
* header/footer sync — the header ``#meta`` must follow a footer switch.

The fake backend (tests/e2e/conftest.py) serves two models with different
capability profiles so a real switch is observable: ``fake`` has no capability
chips; ``fake-o1`` resolves to reasoning (🧠) + tools (🔧) via evi.capabilities.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

_REASONING_CHIP = "🧠"


@pytest.fixture(autouse=True)
def _reset_model(evi_base_url):
    """The evi_base_url server is session-scoped and model_picker_set persists +
    hot-applies the model. Reset to the default `fake` model before and after each
    test here so switches don't leak into sibling tests (or later files)."""
    import httpx

    def reset():
        try:
            httpx.post(f"{evi_base_url}/api/model-picker", json={"model": "fake"}, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    reset()
    yield
    reset()


def _pick_model(page: Page, model_id: str) -> None:
    # Deterministic open: close any lingering picker, then reopen fresh via the
    # Ctrl+I shortcut (idempotent, and doesn't depend on #model-btn being
    # un-covered — the picker stays open after a pick by design).
    page.keyboard.press("Escape")
    page.keyboard.press("Control+i")
    row = page.locator(f'.picker-row[data-action="model"][data-value="{model_id}"]')
    expect(row).to_be_visible(timeout=10_000)
    row.click()


def _close_picker(page: Page) -> None:
    page.keyboard.press("Escape")
    expect(page.locator("#picker-card")).to_be_hidden()


def _active_model(page: Page) -> str:
    return page.evaluate(
        "() => fetch('/api/model-picker').then(r => r.json()).then(d => d.active)"
    )


def test_switch_updates_label_header_and_caps(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    lbl = page.locator("#model-btn-label")

    _pick_model(page, "fake-o1")
    # Footer label reflects the new model AND its capability chips (the staleness
    # bug left the chips on the previously-active model).
    expect(lbl).to_contain_text("fake-o1", timeout=10_000)
    expect(lbl).to_contain_text(_REASONING_CHIP)
    # Header chip stays in sync with the footer switch.
    expect(page.locator("#meta")).to_contain_text("model=fake-o1")
    # The server actually switched, not just the label.
    assert _active_model(page) == "fake-o1"


def test_switching_back_clears_stale_caps(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    lbl = page.locator("#model-btn-label")

    _pick_model(page, "fake-o1")
    expect(lbl).to_contain_text(_REASONING_CHIP, timeout=10_000)

    # Switch to the plain model — the reasoning chip must clear. Before the fix
    # the footer kept showing fake-o1's chips until the picker was reopened.
    # (Assert it left fake-o1 explicitly — "fake" is a substring of "fake-o1".)
    _pick_model(page, "fake")
    expect(lbl).not_to_contain_text("fake-o1", timeout=10_000)
    expect(lbl).not_to_contain_text(_REASONING_CHIP)
    assert _active_model(page) == "fake"


def test_route_info_does_not_wipe_caps_after_a_turn(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    lbl = page.locator("#model-btn-label")

    _pick_model(page, "fake-o1")
    expect(lbl).to_contain_text(_REASONING_CHIP, timeout=10_000)
    _close_picker(page)  # the picker stays open after a pick; get it off the composer

    # Send a turn: the default-route RouteInfo event fires. It must re-render the
    # footer (keeping the chips), not overwrite the label with a bare model id.
    page.fill("#input", "hi")
    page.click("#send")
    expect(page.locator(".msg.assistant").last).to_be_visible(timeout=20_000)
    expect(lbl).to_contain_text("fake-o1")
    expect(lbl).to_contain_text(_REASONING_CHIP)
