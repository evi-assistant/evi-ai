"""Per-message action regression tests (deterministic, fake backend).

Guards two of the message-action bugs the audit found:

* #25 — the 🔄 re-roll button appeared on EVERY assistant bubble but always
  regenerated the latest turn. It must now show on the last assistant turn only.
* #23 — user/tool bubbles lost their Edit/Branch/Delete toolbar after a history
  rebuild (a ``textContent=`` wiped the appended actions). After a rebuild the
  toolbar must still be there.

And that what they do lasts (Phase 0 fix-transcript-fidelity): an edit and
its re-roll, and a branch, are recorded in the transcript, so a restart revives
the conversation the user saw, not the original prompt with two replies.
"""
from __future__ import annotations

import shutil
import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _send(page: Page, text: str) -> None:
    n = page.locator(".msg.assistant").count()
    page.fill("#input", text)
    page.click("#send")
    # wait for a NEW assistant bubble to finish appearing
    expect(page.locator(".msg.assistant")).to_have_count(n + 1, timeout=20_000)
    expect(page.locator(".msg.assistant").last).to_contain_text("fake backend", timeout=20_000)


def test_reroll_button_only_on_last_assistant(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    _send(page, "first")
    _send(page, "second")

    # Exactly one re-roll button is shown, and it's inside the last assistant
    # bubble (updateRerollButtons hides it on earlier ones via display:none).
    result = page.evaluate(
        """() => {
            const shown = [...document.querySelectorAll('.reroll-btn')]
                .filter(b => b.style.display !== 'none');
            const assistants = [...document.querySelectorAll('.msg.assistant')];
            const last = assistants[assistants.length - 1];
            return { count: shown.length, inLast: shown.length === 1 && last.contains(shown[0]),
                     assistants: assistants.length };
        }"""
    )
    assert result["assistants"] == 2
    assert result["count"] == 1, f"expected 1 visible re-roll, got {result['count']}"
    assert result["inLast"], "the visible re-roll button is not in the last assistant bubble"


def test_sent_user_bubble_has_toolbar(page: Page, evi_base_url: str):
    # The LIVE send path (not a rebuild): `addBubble('user').textContent = text`
    # wiped the toolbar addBubble had just appended, so a just-sent message had no
    # Edit/Branch/Delete until a history rebuild. Found by dogfooding 1.0.18.
    page.goto(evi_base_url)
    _send(page, "hello there")
    has_actions = page.evaluate(
        "() => { const u = document.querySelector('.msg.user');"
        "        return !!(u && u.querySelector('.msg-actions')); }"
    )
    assert has_actions, "a just-sent user bubble is missing its action toolbar"


def test_user_bubble_keeps_toolbar_after_rebuild(page: Page, evi_base_url: str):
    page.goto(evi_base_url)
    _send(page, "hello")

    # Force a history rebuild: open a second (empty) tab, then switch back to the
    # first — switchTab() calls rebuildHistory(), which re-renders the bubbles.
    page.click(".tab.new-tab")
    expect(page.locator(".msg.user")).to_have_count(0, timeout=10_000)  # empty tab
    page.click('.tab[data-i="0"]')
    expect(page.locator(".msg.user")).to_have_count(1, timeout=10_000)  # rebuilt

    # The user bubble must still carry its action toolbar (it was wiped by a
    # textContent= before the fix).
    has_actions = page.evaluate(
        "() => { const u = document.querySelector('.msg.user');"
        "        return !!(u && u.querySelector('.msg-actions')); }"
    )
    assert has_actions, "user bubble lost its action toolbar after a rebuild"


HISTORY = """async (sid) =>
    (await (await fetch('/api/session/' + sid + '/history')).json()).messages"""


def _messages(page: Page, sid: str) -> list[dict]:
    """The session's messages as the server holds them, the system prompt left
    out (a revived session composes its own) and without their indices."""
    return [
        {k: v for k, v in m.items() if k != "index"}
        for m in page.evaluate(HISTORY, sid) if m["role"] != "system"
    ]


def _wait_contents(page: Page, sid: str, contents: list[str]) -> list[dict]:
    for _ in range(100):
        msgs = _messages(page, sid)
        if [m["content"] for m in msgs] == contents:
            return msgs
        page.wait_for_timeout(200)
    pytest.fail(f"history never became {contents}: {[m['content'] for m in msgs]}")


def _restart_copy(evi_home, sid: str) -> str:
    """The session's transcript under an id this server has never loaded: the
    server revives it from disk exactly as it would after a restart."""
    from evi.sessions import session_files

    copy = "revived-" + uuid.uuid4().hex[:12]
    files = session_files(sid, root=evi_home / "transcripts")
    assert files, f"no transcript for {sid}"
    for f in files:  # every day's file: the run may cross midnight
        shutil.copyfile(f, f.parent / f"{copy}.jsonl")
    return copy


def test_edit_and_reroll_survive_a_reload_and_a_restart(page: Page, evi_base_url: str, evi_home):
    page.goto(evi_base_url)
    _send(page, "first question")
    _send(page, "second question")
    sid = page.evaluate("sessionId")
    reply = _messages(page, sid)[1]["content"]

    # Edit the first prompt: Save cuts the history after it and re-rolls.
    first = page.locator(".msg.user").first
    first.hover()
    first.locator(".msg-actions button[title='Edit']").click()
    page.fill(".msg-edit", "edited question")
    page.click(".msg-edit-row button:not(.cancel)")
    seen = _wait_contents(page, sid, ["edited question", reply])
    expect(page.locator(".msg.assistant")).to_have_count(1, timeout=20_000)

    # A reload draws the same conversation from the server.
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.evaluate("sessionId") == sid
    expect(page.locator(".msg.user")).to_have_count(1, timeout=10_000)
    expect(page.locator(".msg.user")).to_contain_text("edited question")
    expect(page.locator(".msg.assistant")).to_have_count(1)

    # A restart revives it from the transcript: the same history, not the
    # original prompt followed by the old and the new reply.
    revived = _restart_copy(evi_home, sid)
    assert _messages(page, revived) == seen
    page.evaluate("(sid) => { setSessionId(sid); return rebuildHistory(); }", revived)
    expect(page.locator(".msg.user")).to_have_count(1, timeout=10_000)
    expect(page.locator(".msg.user")).to_contain_text("edited question")
    assert "first question" not in page.locator("#log").inner_text()
    expect(page.locator(".msg.assistant")).to_have_count(1)


def test_a_branch_survives_a_restart(page: Page, evi_base_url: str, evi_home):
    page.goto(evi_base_url)
    _send(page, "trunk question")
    _send(page, "later question")
    parent = page.evaluate("sessionId")

    # Branch at the first reply: a new tab holding the first exchange.
    first_reply = page.locator(".msg.assistant").first
    first_reply.hover()
    first_reply.locator(".msg-actions button[title='Branch']").click()
    expect(page.locator(".msg.user")).to_have_count(1, timeout=10_000)
    branch = page.evaluate("sessionId")
    assert branch != parent
    _send(page, "branch question")
    reply = _messages(page, branch)[1]["content"]
    seen = _wait_contents(page, branch, ["trunk question", reply, "branch question", reply])

    revived = _restart_copy(evi_home, branch)
    assert _messages(page, revived) == seen
