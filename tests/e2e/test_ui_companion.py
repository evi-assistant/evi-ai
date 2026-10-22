"""E2E: Settings → Companion — the feed, approval cards and instructions.

The server's throwaway EVI_HOME is seeded with a feed (one item whose body is
an HTML payload, which must render as text), two pending proposals for the
real write_file tool (to a file in another tmp folder) and instructions.md.
In a real browser against the real server: switch the Companion on, approve
one proposal (two clicks: the first only arms it) and see its result and the
file it wrote, reject the other, edit the instructions, switch it off. No
model runs: approving executes the stored call as it is.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

PAYLOAD = '<img src=x onerror="window.__cmp_xss=1">'
RLO, ZWSP = chr(0x202E), chr(0x200B)  # a bidi override and a zero-width space


def _hash(args: dict) -> str:
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@pytest.fixture
def companion_home(evi_home, evi_base_url, tmp_path_factory):
    root = evi_home / "companion"
    root.mkdir(exist_ok=True)
    target_dir = tmp_path_factory.mktemp("companion-target")
    keep, skip = target_dir / "approved.txt", target_dir / "rejected.txt"
    now = time.time()
    feed = [
        {"id": "f1", "at": "2026-10-22T07:30:00", "ts": now - 600, "kind": "brief",
         "title": "Your brief for today", "body": "Today:\n09:00 standup", "priority": "medium",
         "why_now": "the morning brief", "dedupe_key": "brief:today", "source": "brief",
         "notified": True, "held": None},
        {"id": "f2", "at": "2026-10-22T08:00:00", "ts": now - 60, "kind": "needs_you",
         "title": "Dentist clash", "body": "Your calendar says " + PAYLOAD, "priority": "high",
         "why_now": "it starts in an hour", "dedupe_key": "dentist", "source": "check_in",
         "notified": False, "held": "quiet hours"},
    ]
    (root / "feed.jsonl").write_text("".join(json.dumps(i) + "\n" for i in feed),
                                     encoding="utf-8")
    props = {}
    for pid, path, summary in (("cmpok1", keep, "Write the packing list"),
                               ("cmpno1", skip, "Write a note nobody wants")):
        args = {"path": str(path), "content": "passport\ncharger\n"}
        props[pid] = {"tool": "write_file", "args": args, "args_hash": _hash(args),
                      "summary": summary, "dedupe_key": pid, "state": "pending",
                      "created_at": now, "expires_at": now + 3600}
    # A spoofing attempt: a path the browser would draw reordered (report
    # "exe.txt"), a zero-width character, and a long content first so the
    # path would sit below the card's scroll box.
    args = {"content": "x" + ZWSP + "y\n" * 400,
            "path": str(target_dir / ("report" + RLO + "txt.exe"))}
    props["cmpbidi1"] = {"tool": "write_file", "args": args, "args_hash": _hash(args),
                         "summary": "Save the report" + RLO, "dedupe_key": "bidi",
                         "state": "pending", "created_at": now, "expires_at": now + 3600}
    (root / "proposals.json").write_text(json.dumps(props), encoding="utf-8")
    (root / "instructions.md").write_text("Watch the dentist.", encoding="utf-8")
    # A proposal may only use a tool [companion] allowed_actions names.
    api = httpx.Client(base_url=evi_base_url, timeout=10)
    assert api.post("/api/config", json={"companion": {
        "allowed_actions": ["write_file"]}}).status_code == 200
    try:
        yield root, keep, skip
    finally:
        api.post("/api/config", json={"companion": {"enabled": False, "allowed_actions": []}})
        api.close()
        shutil.rmtree(root, ignore_errors=True)


def test_companion_panel(page: Page, evi_base_url: str, evi_home, companion_home):
    root, keep, skip = companion_home
    page.goto(evi_base_url)
    page.evaluate("window.eviUI.openSettings('companion')")
    expect(page.locator("#settings-content h3")).to_have_text("Companion")
    head = page.locator("#cmp-head")
    expect(head).to_contain_text("Companion is off", timeout=10_000)

    # The feed: newest first, kind badges, and model text as text.
    feed = page.locator("#cmp-feed")
    items = feed.locator(".cmp-item")
    expect(items).to_have_count(2)
    expect(items.nth(0)).to_contain_text("Dentist clash")
    expect(items.nth(0).locator(".cmp-badge")).to_have_text("needs you")
    expect(items.nth(0)).to_contain_text("Your calendar says " + PAYLOAD)
    expect(items.nth(0)).to_contain_text("Not notified: quiet hours")
    expect(items.nth(1).locator(".cmp-badge")).to_have_text("brief")
    expect(feed.locator("img")).to_have_count(0)
    assert page.evaluate("window.__cmp_xss") is None

    # On.
    page.locator("#cmp-enabled").check()
    expect(head.locator(".cmp-state")).to_have_text("on", timeout=10_000)
    expect(head).not_to_contain_text("Companion is off")
    assert "enabled = true" in (evi_home / "config.toml").read_text(encoding="utf-8")

    # Format characters in a proposal are shown as escapes, never raw, and
    # the short `path` comes before the long `content`.
    bidi = page.locator('.cmp-card[data-id="cmpbidi1"]')
    shown = bidi.locator(".cmp-args").text_content(timeout=10_000)
    assert RLO not in shown and ZWSP not in shown
    assert "report\\u202etxt.exe" in shown and "x\\u200by" in shown
    assert shown.index('"path"') < shown.index('"content"')
    assert bidi.locator(".cmp-summary").text_content() == "Save the report\\u202e"

    # Approve: the exact tool and arguments are shown; the first click only arms.
    card = page.locator('.cmp-card[data-id="cmpok1"]')
    expect(card.locator(".cmp-tool")).to_have_text("write_file")
    expect(card.locator(".cmp-args")).to_contain_text("approved.txt")
    expect(card.locator(".cmp-summary")).to_have_text("Write the packing list")
    expect(card.locator(".cmp-exp")).to_contain_text("expires in")
    card.locator(".cmp-approve").click()
    expect(card.locator(".cmp-approve")).to_have_text("Run write_file now")
    time.sleep(0.5)
    assert not keep.exists()  # armed, not run
    card.locator(".cmp-approve").click()
    expect(card.locator(".cmp-badge")).to_have_text("executed", timeout=10_000)
    expect(card.locator(".cmp-result")).to_be_visible()
    assert keep.read_text(encoding="utf-8") == "passport\ncharger\n"
    expect(feed).to_contain_text("Done: Write the packing list", timeout=10_000)

    # Not now.
    other = page.locator('.cmp-card[data-id="cmpno1"]')
    other.locator(".cmp-reject").click()
    expect(other.locator(".cmp-badge")).to_have_text("rejected", timeout=10_000)
    assert not skip.exists()
    states = {k: v["state"] for k, v in json.loads(
        (root / "proposals.json").read_text(encoding="utf-8")).items()}
    assert states == {"cmpok1": "executed", "cmpno1": "rejected", "cmpbidi1": "pending"}

    # Instructions.
    ta = page.locator("#cmp-instr-text")
    expect(ta).to_have_value("Watch the dentist.")
    ta.fill("Watch the dentist.\nIgnore newsletters.")
    page.locator("#cmp-instr-save").click()
    expect(page.locator("#cmp-instr-msg")).to_have_text("Saved.")
    assert (root / "instructions.md").read_text(encoding="utf-8") == (
        "Watch the dentist.\nIgnore newsletters.")

    # Off.
    page.locator("#cmp-enabled").uncheck()
    expect(head).to_contain_text("Companion is off", timeout=10_000)
    expect(page.locator("#cmp-check")).to_be_disabled()
    assert "enabled = false" in (evi_home / "config.toml").read_text(encoding="utf-8")
