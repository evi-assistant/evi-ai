"""`evi companion` (evi/apps/cli/main.py): status, on / off, check, brief,
feed, proposals, approve / reject and instructions; exit codes (0 ok, 1 an
action failed, 2 usage or refused); JSON output; and feed or model text
never reaching the terminal with its escape sequences.

eVi's home is under tmp_path (tests.test_companion_engine.setup_home), the
model is the shared fake_llm, the sources and actions are stub tools.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import evi.apps.cli.main as cli_main
import evi.companion as companion_pkg
from evi import attest
from evi.companion.engine import Companion, args_hash
from evi.companion.store import CompanionStore
from evi.config import Config
from tests.test_companion_engine import Pings, Sources, answer, item, setup_home, write_config

ESC = "\x1b[31mred\x1b]0;owned\x07\u202eevil\r"


@pytest.fixture
def home(monkeypatch, tmp_path):
    home = setup_home(monkeypatch, tmp_path)
    # Run as the owner's own shell (the agent-parent guard has its own test).
    monkeypatch.setattr(attest, "agent_parent", lambda env=None: None)
    monkeypatch.delenv("EVI_AGENT", raising=False)
    return home


@pytest.fixture
def sources(home, monkeypatch) -> Sources:
    return Sources(monkeypatch)


def run(*args: str, input: str | None = None):
    return CliRunner().invoke(cli_main.app, ["companion", *args], input=input)


def store(home: Path) -> CompanionStore:
    return CompanionStore(home / "companion")


def seed_proposal(home: Path, pid: str = "p1", tool: str = "note_add",
                  args: dict | None = None, *, ttl: float = 3600,
                  state: str = "pending", summary: str = "Add a note") -> dict:
    args = {"text": "buy milk"} if args is None else args
    now = time.time()
    rec = {"tool": tool, "args": args, "args_hash": args_hash(args), "summary": summary,
           "dedupe_key": pid, "state": state, "created_at": now, "expires_at": now + ttl}
    s = store(home)
    props = s.load_proposals()
    props[pid] = rec
    s.save_proposals(props)
    return rec


def seed_feed(home: Path, *items: dict) -> None:
    s = store(home)
    entries = []
    for n, it in enumerate(items):
        ts = it.pop("ts", time.time() - 100 + n)
        entries.append({"id": f"f{n}", "at": "2026-10-22T08:00:00", "ts": ts,
                        "notified": False, "held": None, "source": "check_in", **it})
    s.append_feed(entries)


def use_client(monkeypatch, client) -> Pings:
    """`evi.companion`'s calls go to an engine with this model client."""
    pings = Pings()
    monkeypatch.setattr(companion_pkg, "_engine",
                        lambda: Companion(Config.load(), client=client, notifier=pings))
    return pings


# ---- status / on / off -----------------------------------------------------------------


def test_status_off_by_default_text_and_json(home):
    r = run("status")
    assert r.exit_code == 0, r.output
    assert "Companion: off" in r.output and "suggest-only" in r.output
    r = run("status", "--format", "json")
    assert r.exit_code == 0
    st = json.loads(r.stdout)
    assert st["enabled"] is False and st["sources"] == ["calendar", "memory"]
    assert st["pending_proposals"] == 0 and st["notifications_today"] == 0
    assert st["safe_mode"] is False
    assert run("status", "--format", "yaml").exit_code == 2


def test_on_then_off_writes_the_user_config_only(home):
    r = run("on")
    assert r.exit_code == 0, r.output
    assert "Companion on" in r.output and "instructions.md" in r.output
    assert "evi scheduler" in r.output and "suggest" in r.output
    assert "enabled = true" in (home / "config.toml").read_text(encoding="utf-8")
    assert json.loads(run("status", "-f", "json").stdout)["enabled"] is True
    assert [a["event"] for a in store(home).read_audit()] == ["set_enabled"]
    r = run("off")
    assert r.exit_code == 0 and "Companion off" in r.output
    assert json.loads(run("status", "-f", "json").stdout)["enabled"] is False


def test_owner_actions_are_refused_where_an_agent_may_have_started_the_process(
        home, sources, monkeypatch):
    write_config(home, 'allowed_actions = ["note_add"]')
    seed_proposal(home)
    (home / "steer.md").write_text("watch everything", encoding="utf-8")
    monkeypatch.setattr(attest, "agent_parent",
                        lambda env=None: "EVI_AGENT is set, so an eVi agent started this process")
    before = (home / "config.toml").read_text(encoding="utf-8")
    for args in (["approve", "p1", "--yes"], ["reject", "p1"], ["off"], ["on"],
                 ["instructions", "--set", str(home / "steer.md")]):
        r = run(*args)
        assert r.exit_code == 2, (args, r.output)
        assert "refusing to" in r.output and "EVI_AGENT" in r.output
    assert sources.calls == []
    assert store(home).load_proposals()["p1"]["state"] == "pending"
    assert (home / "config.toml").read_text(encoding="utf-8") == before
    assert companion_pkg.read_instructions() == ""
    # Reading is fine.
    assert run("status").exit_code == 0 and run("proposals").exit_code == 0


# ---- check / brief ---------------------------------------------------------------------


def test_check_when_off_is_refused(home):
    r = run("check")
    assert r.exit_code == 2 and "it is off" in r.output


def test_check_prints_what_ran_and_the_new_items_then_skips_unchanged(
        home, sources, fake_llm, monkeypatch):
    write_config(home, 'sources = ["calendar_week"]\nnotify_min_priority = "urgent"')
    client = fake_llm([answer(item("a", "high"))])
    use_client(monkeypatch, client)
    r = run("check")
    assert r.exit_code == 0, r.output
    assert "checked in: calendar_week" in r.output
    assert "title a" in r.output and "body a" in r.output and "needs_you" in r.output
    assert "not notified: below urgent" in r.output
    # Nothing changed since: no model call (the script has no second turn).
    r = run("check")
    assert r.exit_code == 0 and "skipped: nothing changed" in r.output
    assert client.chat.completions.calls == 1


def test_check_json_and_an_error_exits_1(home, sources, fake_llm, monkeypatch):
    write_config(home, 'sources = ["calendar_week"]')
    use_client(monkeypatch, fake_llm([answer(item("a"))]))
    r = run("check", "--format", "json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["ran"] is True and [i["dedupe_key"] for i in out["items"]] == ["a"]

    def boom(self, now, result):
        raise RuntimeError("source exploded")

    monkeypatch.setattr(Companion, "_check_in", boom)
    r = run("check")
    assert r.exit_code == 1 and "source exploded" in r.output


def test_brief_force_and_off(home, sources):
    assert run("brief").exit_code == 2
    write_config(home, 'sources = ["calendar_week"]')
    r = run("brief", "--force")
    assert r.exit_code == 0, r.output
    assert "Your brief for" in r.output and "Needs you" in r.output
    r = run("brief")  # already out today
    assert r.exit_code == 0 and "no brief" in r.output


# ---- feed ------------------------------------------------------------------------------


def test_feed_text_escapes_control_and_format_characters(home):
    seed_feed(home, {"kind": "alert", "priority": "urgent", "title": "Pay " + ESC,
                     "body": "line one\n" + ESC, "why_now": "now" + ESC,
                     "dedupe_key": "k"})
    r = run("feed")
    assert r.exit_code == 0, r.output
    for raw in ("\x1b", "\x07", "\u202e", "\r"):
        assert raw not in r.output
    assert "\\x1b[31mred" in r.output and "\\u202eevil" in r.output and "\\x07" in r.output
    assert "line one\n" in r.output  # newlines stay
    data = json.loads(run("feed", "--format", "json").stdout)
    assert data[0]["title"] == "Pay " + ESC  # JSON escapes it, losslessly
    assert "\x1b" not in run("feed", "--format", "json").stdout


def test_feed_text_is_not_rewritten_by_emoji_codes(home):
    seed_feed(home, {"kind": "alert", "priority": "high", "title": "pay :thumbs_up: now",
                     "body": ":smile: [bold]x[/bold]", "why_now": ":warning:",
                     "dedupe_key": "e"})
    seed_proposal(home, "p1", summary="send :thumbs_up:")
    out = run("feed").output
    assert "pay :thumbs_up: now" in out and ":smile: [bold]x[/bold]" in out
    assert "why now: :warning:" in out
    assert "\U0001f44d" not in out and "\U0001f604" not in out
    assert "send :thumbs_up:" in run("proposals").output


def test_feed_limit_and_since(home):
    assert "nothing yet" in run("feed").output
    old, new = time.time() - 7200, time.time() - 60
    seed_feed(home, {"kind": "suggestion", "priority": "low", "title": "old one",
                     "body": "", "why_now": "", "dedupe_key": "o", "ts": old},
              {"kind": "working_on", "priority": "medium", "title": "new one",
               "body": "", "why_now": "", "dedupe_key": "n", "ts": new})
    out = run("feed", "--limit", "1").output
    assert "new one" in out and "old one" not in out
    from datetime import datetime

    since = datetime.fromtimestamp(time.time() - 3600).isoformat(timespec="seconds")
    data = json.loads(run("feed", "--since", since, "-f", "json").stdout)
    assert [i["title"] for i in data] == ["new one"]
    assert run("feed", "--since", "yesterday").exit_code == 2
    assert run("feed", "--limit", "0").exit_code == 2


# ---- proposals / approve / reject ----------------------------------------------------------


def test_proposals_list_filter_and_json(home):
    seed_proposal(home, "p1")
    seed_proposal(home, "p2", state="rejected")
    out = run("proposals").output
    assert "p1  pending  note_add (expires in" in out and "p2  rejected" in out
    rows = json.loads(run("proposals", "--state", "pending", "-f", "json").stdout)
    assert [r["id"] for r in rows] == ["p1"] and rows[0]["args"] == {"text": "buy milk"}
    r = run("proposals", "--state", "approvedd")
    assert r.exit_code == 2 and "pending | running" in r.output


def test_proposals_state_takes_the_spec_names_approved_and_done(home):
    for pid, state in (("p1", "pending"), ("p2", "running"), ("p3", "executed"),
                       ("p4", "failed"), ("p5", "rejected"), ("p6", "expired")):
        seed_proposal(home, pid, state=state)
    ids = lambda state: [r["id"] for r in json.loads(  # noqa: E731
        run("proposals", "--state", state, "-f", "json").stdout)]
    assert ids("approved") == ["p2", "p3", "p4"]
    assert ids("done") == ["p3"]
    assert ids("failed") == ["p4"] and ids("expired") == ["p6"]
    assert "p3  executed" in run("proposals", "--state", "done").output


def test_approve_without_yes_and_without_a_terminal_exits_2(home, sources):
    write_config(home, 'allowed_actions = ["note_add"]')
    seed_proposal(home)
    r = run("approve", "p1")
    assert r.exit_code == 2, r.output
    assert "tool: note_add" in r.output and '"text": "buy milk"' in r.output
    assert "no terminal" in r.output and "--yes" in r.output
    assert sources.calls == []
    assert store(home).load_proposals()["p1"]["state"] == "pending"


def test_approve_asks_on_a_terminal(home, sources, monkeypatch):
    write_config(home, 'allowed_actions = ["note_add"]')
    seed_proposal(home)
    monkeypatch.setattr(cli_main, "_companion_tty", lambda: True)
    r = run("approve", "p1", input="n\n")
    assert r.exit_code == 2 and "not approved" in r.output and sources.calls == []
    r = run("approve", "p1", input="y\n")
    assert r.exit_code == 0, r.output
    assert "executed" in r.output and "did note_add" in r.output
    assert sources.calls == [("note_add", {"text": "buy milk"})]


def test_approve_yes_prints_the_exact_call_and_the_result(home, sources):
    write_config(home, 'allowed_actions = ["note_add"]')
    seed_proposal(home, args={"text": "milk \x1b[2J"})
    r = run("approve", "p1", "--yes")
    assert r.exit_code == 0, r.output
    assert r.output.index("tool: note_add") < r.output.index("executed")
    assert "\x1b" not in r.output and "\\u001b[2J" in r.output
    assert sources.calls == [("note_add", {"text": "milk \x1b[2J"})]
    assert store(home).load_proposals()["p1"]["state"] == "executed"
    # Once only.
    r = run("approve", "p1", "--yes")
    assert r.exit_code == 2 and "executed, not pending" in r.output
    assert len(sources.calls) == 1


def test_approve_refusals_exit_2_and_a_failed_action_exits_1(home, sources, monkeypatch):
    write_config(home, 'allowed_actions = ["note_add"]')
    assert run("approve", "nope", "--yes").exit_code == 2
    seed_proposal(home, "old", ttl=-5)
    r = run("approve", "old", "--yes")
    assert r.exit_code == 2 and "expired" in r.output
    rec = seed_proposal(home, "tampered")
    props = store(home).load_proposals()
    props["tampered"]["args"] = {"text": "something else"}
    store(home).save_proposals(props)
    r = run("approve", "tampered", "--yes")
    assert r.exit_code == 2 and "arguments changed" in r.output
    assert rec["args"] == {"text": "buy milk"} and sources.calls == []

    from evi.tools.base import REGISTRY, Tool

    monkeypatch.setitem(REGISTRY, "note_add", Tool(
        name="note_add", description="stub", parameters={"type": "object", "properties": {}},
        func=lambda **kw: "ERROR: the notes folder is read-only", category="general"))
    seed_proposal(home, "fails")
    r = run("approve", "fails", "--yes")
    assert r.exit_code == 1 and "failed" in r.output and "read-only" in r.output


def test_reject(home, sources):
    seed_proposal(home)
    r = run("reject", "p1")
    assert r.exit_code == 0 and "rejected" in r.output
    assert store(home).load_proposals()["p1"]["state"] == "rejected"
    assert run("reject", "p1").exit_code == 2
    assert run("reject", "ghost").exit_code == 2
    assert sources.calls == []


# ---- instructions ------------------------------------------------------------------------


def test_instructions_set_show_and_edit(home, tmp_path, monkeypatch):
    r = run("instructions")
    assert r.exit_code == 0 and "no instructions yet" in r.output
    f = tmp_path / "steer.md"
    f.write_text("Watch the dentist.\x1b[2J\n", encoding="utf-8")
    assert run("instructions", "--set", str(f)).exit_code == 0
    assert companion_pkg.read_instructions() == "Watch the dentist.\x1b[2J\n"
    out = run("instructions", "--show").output
    assert "Watch the dentist." in out and "\x1b" not in out and "\\x1b[2J" in out
    import click

    monkeypatch.setattr(click, "edit", lambda text, **kw: text + "Ignore newsletters.\n")
    assert run("instructions", "--edit").exit_code == 0
    assert companion_pkg.read_instructions().endswith("Ignore newsletters.\n")
    monkeypatch.setattr(click, "edit", lambda text, **kw: None)  # closed without saving
    assert "unchanged" in run("instructions", "--edit").output
    assert [a["event"] for a in store(home).read_audit()] == ["instructions", "instructions"]
    f.write_text("x" * 20_000, encoding="utf-8")
    assert run("instructions", "--set", str(f)).exit_code == 2
    assert run("instructions", "--set", str(tmp_path / "missing.md")).exit_code == 2
    assert run("instructions", "--show", "--edit").exit_code == 2


def test_printable_helper():
    p = companion_pkg.printable
    assert p("a\tb\nc") == "a\tb\nc"
    assert p("\x1b[0m\x9b\u200b\u202e\u2028") == "\\x1b[0m\\x9b\\u200b\\u202e\\u2028"
    assert p("café ✓ 日本") == "café ✓ 日本"
