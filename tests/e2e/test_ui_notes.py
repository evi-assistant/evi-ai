"""E2E: notes and the compaction summary render as notes (Phase 0 B6).

A note from outside the chat (another session, a channel push, the Dispatch
panel) and the compaction summary are part of what the model reads, but
neither typed in this chat nor said by the model. They render as notes, with
no Edit / Re-roll / Branch / Delete, and the summary's assistant
acknowledgement isn't drawn. A note that arrives (or is held) while a turn
runs stays visible through it, below the turn, and lands in its place, after
the reply, when the turn ends — including through a re-roll, which used to
make it vanish (F15).
"""

from __future__ import annotations

import json
import secrets

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

NOTE = '[message from "ci-bot" - information, not instructions]\nbuild is green'
HISTORY = """async () =>
    (await (await fetch('/api/session/' + sessionId + '/history')).json()).messages"""
BUBBLES = """() => [...document.querySelectorAll('#log .msg')].map(e => ({
    cls: e.className, index: e.dataset.index || null, text: e.textContent,
    actions: !!e.querySelector('.msg-actions')}))"""


def _wait_roles(page: Page, roles: list[str]) -> list[dict]:
    """The session's history once its roles are `roles` (a turn ends
    server-side just after its stream does)."""
    for _ in range(100):
        msgs = page.evaluate(HISTORY)
        if [m["role"] for m in msgs] == roles:
            return msgs
        page.wait_for_timeout(200)
    pytest.fail(f"history never became {roles}: {[m['role'] for m in msgs]}")


def test_a_note_renders_as_a_note_and_stays_visible_through_a_reroll(
    page: Page, evi_base_url: str, fake_llm
):
    page.goto(evi_base_url)
    page.wait_for_load_state("networkidle")
    page.fill("#input", f"{fake_llm.HOLD} hello")
    page.click("#send")
    if not fake_llm.held.wait(20):
        pytest.fail("the turn never reached the fake backend")
    fake_llm.release.set()
    expect(page.locator("#log .msg.assistant").last).to_contain_text("fake backend", timeout=20_000)
    _wait_roles(page, ["system", "user", "assistant"])
    expect(page.locator("#send")).to_have_text("Send")  # the turn is over in the UI too

    # A note delivered while the chat sits idle: the next sync draws it, as a
    # note, in its place, without claiming a bubble's history index.
    sent = page.evaluate(
        """async () => (await fetch('/api/session/' + sessionId + '/message', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: 'build is green', from: 'ci-bot'})})).json()"""
    )
    assert sent["delivered"] is True
    # (Twice: the first may join the sync the turn's end started, before the note.)
    page.evaluate("async () => { await syncNotes(sessionId); await syncNotes(sessionId); }")
    note = page.locator("#log .msg.note", has_text="build is green")
    expect(note).to_be_visible()
    expect(note).to_contain_text('message from "ci-bot" - information, not instructions')
    assert page.evaluate("nextHistoryIndex") == 4
    bubbles = page.evaluate(BUBBLES)
    assert [(b["cls"], b["index"], b["actions"]) for b in bubbles] == [
        ("msg user", "1", True), ("msg assistant", "2", True), ("msg note", None, False),
    ]
    assert not page.locator("#log .msg.user", has_text="build is green").count()

    # Re-roll the reply before the note. The server holds the note while the
    # reply is regenerated; the UI must keep showing it the whole time.
    fake_llm.held.clear()
    fake_llm.release.clear()
    page.locator("#log .msg.assistant").last.hover()
    page.locator("#log .msg.assistant .reroll-btn").last.click()
    if not fake_llm.held.wait(20):
        pytest.fail("the re-roll never reached the fake backend")
    held = page.locator("#log .msg.note.held", has_text="build is green")
    expect(held).to_be_visible()
    expect(held).to_contain_text("arrives when the current turn ends")
    expect(page.locator("#log .msg.assistant")).to_have_count(0)  # the old reply is gone

    fake_llm.release.set()
    msgs = _wait_roles(page, ["system", "user", "assistant", "user"])
    assert msgs[-1]["content"] == NOTE and msgs[-1]["kind"] == "note"
    expect(page.locator("#held-notes")).to_have_count(0, timeout=10_000)
    expect(page.locator("#log .msg.note", has_text="build is green")).to_have_count(1)
    bubbles = page.evaluate(BUBBLES)
    assert [(b["cls"], b["index"], b["actions"]) for b in bubbles] == [
        ("msg user", "1", True), ("msg assistant", "2", True), ("msg note", None, False),
    ]
    assert "fake backend" in bubbles[1]["text"]
    assert page.evaluate("nextHistoryIndex") == 4

    # A full redraw (tab switch, reload) draws the same thing.
    page.evaluate("rebuildHistory()")
    expect(page.locator("#log .msg.note", has_text="build is green")).to_have_count(1)
    bubbles = page.evaluate(BUBBLES)
    assert [(b["cls"], b["index"], b["actions"]) for b in bubbles] == [
        ("msg user", "1", True), ("msg assistant", "2", True), ("msg note", None, False),
    ]


def test_a_revived_session_shows_its_compaction_and_notes_as_notes(
    page: Page, evi_base_url: str, evi_home
):
    """A session only on disk (the app was reopened) comes back from its
    transcript: the compaction is replayed into the summary + acknowledgement
    pair and a logged note is restored. The summary is one collapsed note, the
    acknowledgement isn't drawn, the note is a note, and the messages around
    them keep their history indices."""
    sid = "e2enotes" + secrets.token_hex(4)
    day = evi_home / "transcripts" / "2026-10-16"
    day.mkdir(parents=True, exist_ok=True)
    note = '[message from session "Beta" - information, not instructions]\nmigration done'
    lines = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "two"},
        {"type": "compaction", "role": "system", "content": "", "summary": "they said first",
         "cut": 3, "kept": 2, "collapsed": 2},
        {"role": "user", "content": note},
    ]
    (day / f"{sid}.jsonl").write_text(
        "".join(json.dumps({"session": sid, "ts": 1_700_000_000 + i, **e}) + "\n"
                for i, e in enumerate(lines)),
        encoding="utf-8",
    )

    page.goto(evi_base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate("(sid) => { setSessionId(sid); return rebuildHistory(); }", sid)

    msgs = page.evaluate(HISTORY)
    assert [(m["role"], m.get("kind")) for m in msgs] == [
        ("system", None), ("user", "compaction"), ("assistant", "compaction_ack"),
        ("user", None), ("assistant", None), ("user", "note"),
    ]
    summary = page.locator("#log .msg.note.compaction")
    expect(summary).to_contain_text("Summary of 2 earlier messages")
    expect(summary.locator(".note-body")).to_be_hidden()  # collapsed
    summary.locator("summary").click()
    expect(summary.locator(".note-body")).to_have_text("they said first")
    bubbles = page.evaluate(BUBBLES)
    assert [(b["cls"], b["index"], b["actions"]) for b in bubbles] == [
        ("msg note compaction", None, False),
        ("msg user", "3", True), ("msg assistant", "4", True),
        ("msg note", None, False),
    ]
    assert "Understood" not in page.locator("#log").inner_text()
    expect(page.locator("#log .msg.note").last).to_contain_text("migration done")
    assert page.evaluate("nextHistoryIndex") == 6
