"""E2E: the Stop button ends a running turn (POST /api/session/{id}/cancel).

While a turn this page started runs, a Stop button sits next to Send (which
reads Queue then). Pressing it cancels the turn server-side: the stream ends
with Done "interrupted", the UI says the turn was stopped, Send and the
composer come back, and the session is free, so the next message is a new
turn rather than a follow-up queued onto the old one.

Deterministic against the fake backend (tests/e2e/conftest.py): a
`[e2e-hold]` prompt keeps the model's reply from starting until the test
releases it, so the turn is in flight server-side when Stop is pressed.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _console_errors(page: Page) -> list[str]:
    errors: list[str] = []

    def _on_console(m):
        if m.type == "error" and "Failed to load resource" not in m.text and "net::ERR" not in m.text:
            errors.append(m.text)

    page.on("console", _on_console)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def _history(page: Page) -> list[dict]:
    return page.evaluate("""async () =>
        (await (await fetch('/api/session/' + sessionId + '/history')).json()).messages""")


def test_stop_ends_the_running_turn_and_frees_the_session(
    page: Page, evi_base_url: str, fake_llm
):
    errors = _console_errors(page)
    page.goto(evi_base_url)
    page.wait_for_load_state("networkidle")
    stop = page.locator("#stop")
    expect(stop).to_be_hidden()  # nothing to stop yet

    page.fill("#input", f"{fake_llm.HOLD} write me a long essay")
    page.click("#send")
    if not fake_llm.held.wait(20):
        pytest.fail("the held turn never reached the fake backend")
    expect(stop).to_be_visible()
    expect(stop).to_have_text("Stop")
    expect(page.locator("#send")).to_have_text("Queue")

    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith("/cancel")
    ) as cancelled:
        stop.click()
    assert cancelled.value.json() == {"cancelled": True, "busy": False}

    expect(page.locator("#log .msg.system").last).to_have_text(
        "Stopped — the turn was cancelled.", timeout=10_000)
    expect(page.locator("#send")).to_have_text("Send", timeout=10_000)
    expect(stop).to_be_hidden()
    expect(page.locator("#work-status")).to_be_hidden()

    # The session is free: the next message is a turn of its own (a busy
    # session would have queued it as a follow-up instead). The stopped
    # prompt got no reply, so the model reads it with the next one (marker
    # and all): let the fake backend answer from here on.
    fake_llm.release.set()
    page.fill("#input", "and now a short answer")
    page.click("#send")
    expect(page.locator("#log .msg.assistant").last).to_contain_text(
        "fake backend", timeout=20_000)
    expect(page.locator("#send")).to_have_text("Send", timeout=10_000)
    expect(page.locator("#log .steer-tag")).to_have_count(0)

    users = [str(m["content"] or "") for m in _history(page) if m["role"] == "user"]
    assert any("write me a long essay" in u for u in users), users
    assert any("and now a short answer" in u for u in users), users
    assert errors == [], f"console errors: {errors}"


def test_stop_ends_a_re_roll_too(page: Page, evi_base_url: str, fake_llm):
    """A re-roll is a turn: Stop shows for it and ends it the same way."""
    page.goto(evi_base_url)
    page.wait_for_load_state("networkidle")
    page.fill("#input", "say something")
    page.click("#send")
    expect(page.locator("#log .msg.assistant").last).to_contain_text(
        "fake backend", timeout=20_000)
    expect(page.locator("#send")).to_have_text("Send", timeout=10_000)

    # Hold the next model reply, then re-roll the answer through the UI's own
    # re-roll path (the bubble's button calls it).
    page.evaluate("""async () => {
        const r = await fetch('/api/session/' + sessionId + '/edit', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({at_index: 1, content: '[e2e-hold] say it again'}),
        });
        if (!r.ok) throw new Error('edit failed: ' + r.status);
    }""")
    fake_llm.held.clear()
    page.evaluate("() => { streamReroll(); }")  # not awaited: the turn is held
    if not fake_llm.held.wait(20):
        pytest.fail("the held re-roll never reached the fake backend")
    stop = page.locator("#stop")
    expect(stop).to_be_visible()
    stop.click()
    expect(page.locator("#log .msg.system").last).to_have_text(
        "Stopped — the turn was cancelled.", timeout=10_000)
    expect(stop).to_be_hidden(timeout=10_000)
    expect(page.locator("#send")).to_be_enabled()


def test_stop_closes_the_permission_question_the_turn_waits_on(
    page: Page, evi_base_url: str, fake_llm
):
    """The question's overlay covers the composer, so it carries a Stop of
    its own. The stream's reader waits on the open question: Stop closes it
    as the server denies it, and the turn's end comes through."""
    import httpx

    fake_llm.release.set()  # nothing held here
    before = httpx.get(evi_base_url + "/api/config", timeout=10).json()["auto"]["auto_approve"]
    asking = [c for c in before if c != "memory"]  # FAKE_TOOL now asks first
    httpx.post(evi_base_url + "/api/config", json={"auto": {"auto_approve": asking}}, timeout=10)
    try:
        page.goto(evi_base_url)
        page.wait_for_load_state("networkidle")
        page.fill("#input", f"{fake_llm.TOOL} look it up")
        page.click("#send")
        overlay = page.locator(".perm-overlay")
        expect(overlay).to_be_visible(timeout=20_000)
        expect(overlay).to_contain_text(fake_llm.TOOL_NAME)

        overlay.locator("button.stop-turn").click()
        expect(overlay).to_have_count(0, timeout=10_000)
        expect(page.locator("#log .msg.system").last).to_have_text(
            "Stopped — the turn was cancelled.", timeout=10_000)
        expect(page.locator("#log .msg.tool").last).to_contain_text("(cancelled)")
        expect(page.locator("#send")).to_have_text("Send", timeout=10_000)
        expect(page.locator("#stop")).to_be_hidden()
    finally:
        httpx.post(evi_base_url + "/api/config", json={"auto": {"auto_approve": before}},
                   timeout=10)
