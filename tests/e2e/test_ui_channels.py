"""E2E: Settings → Channels — approve and revoke Telegram senders (1.0.51).

An unknown sender gets a pairing code and no agent turn until it's approved
from this machine. The pairing queue is seeded straight into the server's tmp
EVI_HOME (`channels/telegram.json`, read on every request); approving and
revoking go through the real endpoints. Telegram itself is never contacted:
the channel stays off and has no token.
"""

from __future__ import annotations

import json
import time

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

CODE, UID, NAME = "K7M2QX", "424242", "Pat (e2e)"


@pytest.fixture
def pairing_store(evi_home):
    store = evi_home / "channels" / "telegram.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    store.write_text(json.dumps({"paired": {}, "pending": {
        CODE: {"user_id": UID, "name": NAME, "at": now},
        # Older than the one-hour TTL: must not be offered for approval.
        "ZZZZZZ": {"user_id": "777", "name": "Expired (e2e)", "at": now - 7200},
    }}), encoding="utf-8")
    try:
        yield store
    finally:
        store.unlink(missing_ok=True)


def _open_channels(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('channels')")
    expect(page.locator("#settings-content h3")).to_have_text("Channels")
    lists = page.locator("#ch-lists")
    expect(lists).to_be_visible(timeout=10_000)
    return lists


def test_approve_then_revoke_a_sender(page: Page, evi_base_url: str, pairing_store):
    lists = _open_channels(page, evi_base_url)
    expect(lists).to_contain_text("Waiting for approval")
    expect(lists).to_contain_text(NAME)
    expect(lists).to_contain_text(CODE)
    expect(lists).not_to_contain_text("Expired (e2e)")
    expect(lists).to_contain_text("Nobody is paired yet.")

    lists.locator(f'.ch-ok[data-code="{CODE}"]').click()
    expect(lists).to_contain_text("Paired", timeout=10_000)
    expect(lists).not_to_contain_text("Waiting for approval")
    revoke = lists.locator(f'.ch-no[data-uid="{UID}"]')
    expect(revoke).to_have_text("Revoke")
    store = json.loads(pairing_store.read_text(encoding="utf-8"))
    assert UID in store["paired"] and store["paired"][UID]["name"] == NAME, store
    assert CODE not in store["pending"], store

    revoke.click()
    expect(lists).to_contain_text("Nobody is paired yet.", timeout=10_000)
    expect(lists.locator(".ch-no")).to_have_count(0)
    store = json.loads(pairing_store.read_text(encoding="utf-8"))
    assert store["paired"] == {}, store


def test_channel_is_off_and_unknown_codes_pair_nobody(
    page: Page, evi_base_url: str, pairing_store
):
    _open_channels(page, evi_base_url)
    content = page.locator("#settings-content")
    expect(content.locator(".set-about-row").first).to_contain_text("Telegram")
    expect(content.locator(".set-about-row").first).to_contain_text("off")
    expect(page.locator("#ch-enabled")).not_to_be_checked()
    expect(page.locator("#ch-token")).to_have_attribute("type", "password")
    # The paired senders' toolset is spelled out, and it is not "nothing".
    tools = page.evaluate(
        "() => fetch('/api/channels/telegram').then(r => r.json()).then(s => s.tools)")
    assert tools, tools
    expect(content).to_contain_text("Approved senders can use: " + ", ".join(tools))

    # A code nobody was given (or an expired one) pairs nobody.
    for code in ("NOPE22", "ZZZZZZ"):
        status = page.evaluate(
            """(code) => fetch('/api/channels/telegram/approve', {method: 'POST',
                 headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({code})}).then(r => r.status)""",
            code,
        )
        assert status == 404, (code, status)
    store = json.loads(pairing_store.read_text(encoding="utf-8"))
    assert store["paired"] == {}, store
