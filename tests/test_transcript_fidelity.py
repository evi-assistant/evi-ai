"""A revived or resumed session is the conversation the user last saw.

The transcript is append-only, so a change to the history that isn't a new
message goes in as a meta record (evi.transcripts) that a replay applies
(evi.sessions.history_from_transcript with replay=True):

- a web edit (edit), a cut back (truncate: Delete from here, a re-roll's
  rewind, /reset), and the copied start of a web branch or `evi sessions fork`
  (fork_of). Before, none of them were written, so a restart brought back a
  different conversation: the original prompt with the old and the new reply
  one after the other, a branch with no start, a fork that lost its prefix.
- A session that runs past midnight has a file per day, and resume read one.
- Meta records are not messages: stats and fine-tune export don't see them.
- `evi sessions handoff` copies the session where `evi sync` carries it, and
  the other device takes the copy in when it resumes.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from evi import sessions
from evi.config import Config
from evi.history import pair_tool_results
from evi.llm.agent import Agent, Error
from evi.sessions import find_session, history_from_transcript, session_files
from evi.transcripts import COMPACTION, EDIT, FORK, TRUNCATE, TranscriptStore

DAY1 = datetime(2026, 10, 16, 23, 58).timestamp()
DAY2 = datetime(2026, 10, 17, 0, 2).timestamp()


# ---- helpers -------------------------------------------------------------


def _agent(client, root: Path, sid: str = "S1", **kw) -> Agent:
    return Agent(
        client=client, config=Config(), tools=list(kw.pop("tools", ())),
        transcripts=kw.pop("transcripts", None) or TranscriptStore(root=root),
        session_id=sid, unattended_policy="allow", **kw,
    )


def _errors(events) -> list[Error]:
    return [e for e in events if isinstance(e, Error)]


def _replay(root: Path, sid: str = "S1") -> list[dict]:
    path = find_session(sid, root=root)
    assert path is not None, f"no transcript for {sid}"
    return history_from_transcript(path, replay=True)


def _lines(root: Path, sid: str = "S1") -> list[dict]:
    return [
        json.loads(x)
        for path in session_files(sid, root=root)
        for x in path.read_text(encoding="utf-8").splitlines() if x.strip()
    ]


def _write(root: Path, day: str, sid: str, entries: list[dict], start: float = 1.0) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        for i, e in enumerate(entries):
            fh.write(json.dumps({"session": sid, "ts": start + i, **e}) + "\n")
    return f


class _Clock(TranscriptStore):
    """A store whose lines are stamped with `now`, so a test can cross midnight."""

    now = DAY1

    def write(self, entry) -> None:
        entry.timestamp = self.now
        super().write(entry)


# ---- the records ------------------------------------------------------------


def test_meta_records_are_system_lines_old_readers_skip(tmp_path) -> None:
    store = TranscriptStore(root=tmp_path)
    store.write_message("s", "user", "hi")
    store.write_message("s", "assistant", "hello")
    store.write_edit("s", index=0, role="user", text="hey", length=2)
    store.write_truncate("s", keep=1, length=1)
    store.write_fork("s", source="src", messages=[
        {"role": "system", "content": "the source's prompt"},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "ls", "content": "a.txt"},
    ])
    lines = _lines(tmp_path, "s")
    meta = [e for e in lines if e.get("type")]
    assert [e["type"] for e in meta] == [EDIT, TRUNCATE, FORK]
    # Readers that predate meta records skip any system line with no content.
    assert all(e["role"] == "system" and e["content"] == "" for e in meta)
    assert meta[0] | {"ts": 0} == {
        "session": "s", "ts": 0, "role": "system", "content": "", "type": EDIT,
        "index": 0, "message_role": "user", "text": "hey", "length": 2,
    }
    fork = meta[2]
    assert fork["source"] == "src" and fork["cut"] == 3
    # No system prompt and no image data in the copy: the text and a count.
    assert fork["messages"] == [
        {"role": "user", "content": "look\n\n[+1 image attachment(s)]"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "content": "a.txt", "name": "ls", "tool_call_id": "c1"},
    ]
    assert "base64" not in json.dumps(lines)
    # The log as written: every message, no record applied.
    assert [m["content"] for m in history_from_transcript(find_session("s", root=tmp_path))] == [
        "hi", "hello",
    ]


def test_replay_applies_the_records_in_order(tmp_path) -> None:
    _write(tmp_path, "2026-10-16", "s", [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "reply two"},
        {"type": EDIT, "role": "system", "content": "", "index": 2, "message_role": "user",
         "text": "two, edited", "length": 4},
        {"role": "assistant", "content": "reply to the edit"},
        {"role": "user", "content": "three"},
        {"type": TRUNCATE, "role": "system", "content": "", "keep": 4, "length": 5},
        {"role": "user", "content": "three again"},
    ])
    assert [(m["role"], m["content"]) for m in _replay(tmp_path, "s")] == [
        ("user", "one"), ("assistant", "reply one"),
        ("user", "two, edited"), ("assistant", "reply to the edit"),
        ("user", "three again"),
    ]


def test_replay_is_idempotent(tmp_path) -> None:
    """A record written twice (a retried request, say) changes nothing more,
    and replaying the same transcript twice gives the same history."""
    entries = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "reply two"},
    ]
    edit = {"type": EDIT, "role": "system", "content": "", "index": 2,
            "message_role": "user", "text": "two, edited", "length": 4}
    cut = {"type": TRUNCATE, "role": "system", "content": "", "keep": 1, "length": 3}
    fork = {"type": FORK, "role": "system", "content": "", "source": "x", "cut": 1,
            "messages": [{"role": "user", "content": "copied"}]}
    _write(tmp_path / "once", "2026-10-16", "s", [*entries, edit, cut, fork])
    _write(tmp_path / "twice", "2026-10-16", "s", [*entries, edit, edit, cut, cut, fork, fork])
    _write(tmp_path / "edit", "2026-10-16", "s", [*entries, edit])
    _write(tmp_path / "edit2", "2026-10-16", "s", [*entries, edit, edit])
    assert _replay(tmp_path / "once", "s") == _replay(tmp_path / "twice", "s") == [
        {"role": "user", "content": "copied"},
    ]
    assert _replay(tmp_path / "edit", "s") == _replay(tmp_path / "edit2", "s")
    assert _replay(tmp_path / "edit", "s") == _replay(tmp_path / "edit", "s")


def test_a_record_this_version_does_not_know_is_skipped(tmp_path) -> None:
    _write(tmp_path, "2026-10-16", "s", [
        {"role": "user", "content": "one"},
        {"type": "from_the_future", "role": "system", "content": "", "keep": 0},
        {"type": TRUNCATE, "role": "system", "content": "", "keep": "not a number"},
        {"role": "assistant", "content": "reply"},
    ])
    assert [m["content"] for m in _replay(tmp_path, "s")] == ["one", "reply"]


def test_a_record_with_a_number_out_of_range_is_skipped(tmp_path) -> None:
    """JSON reads Infinity and 1e999 as infinite floats, which have no int. A
    record holding one (a damaged or hand-edited transcript, a copy from
    another device) is skipped like any other it can't read; it crashed the
    replay, so a revive came back empty and `evi sessions resume` failed."""
    f = _write(tmp_path, "2026-10-16", "s", [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "reply"},
    ])
    head = '{"session": "s", "ts": 5, "role": "system", "content": "", '
    with f.open("a", encoding="utf-8") as fh:
        for rest in (
            '"type": "truncate", "keep": Infinity, "length": 2}',
            '"type": "truncate", "keep": NaN, "length": 2}',
            '"type": "compaction", "summary": "s", "kept": 1e999, "cut": 1}',
            '"type": "compaction", "summary": "s", "kept": 1, "collapsed": -Infinity}',
            '"type": "edit", "index": 1e999, "message_role": "user", "text": "x"}',
        ):
            fh.write(head + rest + "\n")
    assert [m["content"] for m in _replay(tmp_path, "s")] == ["one", "reply"]

    # A `length` it can't read only means nothing to line up: the edit applies.
    with f.open("a", encoding="utf-8") as fh:
        fh.write(head + '"type": "edit", "index": 0, "message_role": "user", '
                 '"text": "one, edited", "length": 1e999}\n')
    assert [m["content"] for m in _replay(tmp_path, "s")] == ["one, edited"]


# ---- the Agent writes them -------------------------------------------------


def test_edit_truncate_rewind_and_reset_are_recorded(tmp_path, fake_llm) -> None:
    client = fake_llm([fake_llm.text(t) for t in ("r1", "r2", "r3", "r4")])
    agent = _agent(client, tmp_path)
    for prompt in ("first", "second", "third"):
        assert _errors(agent.chat(prompt)) == []

    assert agent.edit_message(3, "second, edited")  # history[3] is "second"
    assert _replay(tmp_path) == agent.history[1:]
    assert _errors(agent.continue_chat()) == []  # the web UI re-rolls after an edit
    assert _replay(tmp_path) == agent.history[1:]
    assert [m["content"] for m in agent.history[1:]] == ["first", "r1", "second, edited", "r4"]

    assert agent.rewind_to_last_user() == 1
    assert _replay(tmp_path) == agent.history[1:]
    assert agent.truncate_history(1) == 2
    assert _replay(tmp_path) == agent.history[1:] == [{"role": "user", "content": "first"}]

    # Nothing removed, nothing recorded.
    n = len(_lines(tmp_path))
    assert agent.truncate_history(5) == 0 and agent.rewind_to_last_user() == 0
    assert len(_lines(tmp_path)) == n

    agent.reset()
    assert _replay(tmp_path) == []
    types = [e.get("type") for e in _lines(tmp_path) if e.get("type")]
    assert types == [EDIT, TRUNCATE, TRUNCATE, TRUNCATE]


def test_edit_and_reroll_then_restart_revive_what_the_user_saw(
    monkeypatch, tmp_path, fake_llm
) -> None:
    """The finding: an edit plus re-roll logged only the new reply, so a
    restart revived the original prompt followed by two replies in a row."""
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    root = tmp_path / "transcripts"
    app = _web(monkeypatch, tmp_path, root, [
        fake_llm([fake_llm.text("reply one"), fake_llm.text("reply two"),
                  fake_llm.text("reply to the edit")]),
    ])
    assert '"Done"' in app.post("/api/chat", json={"session_id": "w1", "message": "one"}).text
    assert '"Done"' in app.post("/api/chat", json={"session_id": "w1", "message": "two"}).text
    assert app.post("/api/session/w1/edit", json={"at_index": 1, "content": "one, edited"}).json()["ok"]
    assert '"Done"' in app.post("/api/session/w1/reroll").text
    seen = _history(app, "w1")
    assert [(m["role"], m["content"]) for m in seen] == [
        ("user", "one, edited"), ("assistant", "reply to the edit"),
    ]

    restarted = _web(monkeypatch, tmp_path, root, [fake_llm([])])
    assert _history(restarted, "w1") == seen


def test_delete_from_here_and_reset_survive_a_restart(monkeypatch, tmp_path, fake_llm) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    root = tmp_path / "transcripts"
    app = _web(monkeypatch, tmp_path, root, [
        fake_llm([fake_llm.text("reply one"), fake_llm.text("reply two")]),
        fake_llm([fake_llm.text("other reply")]),
    ])
    for sid, prompt in (("t1", "one"), ("t1", "two"), ("r1", "hello")):
        assert '"Done"' in app.post("/api/chat", json={"session_id": sid, "message": prompt}).text
    assert app.post("/api/session/t1/truncate", json={"after_index": 2}).json()["removed"] == 2
    assert app.post("/api/reset", json={"session_id": "r1", "message": "x"}).status_code == 200
    seen_t1 = _history(app, "t1")
    assert [m["content"] for m in seen_t1] == ["one", "reply one"]
    assert _history(app, "r1") == []

    restarted = _web(monkeypatch, tmp_path, root, [fake_llm([]), fake_llm([])])
    assert _history(restarted, "t1") == seen_t1
    assert _history(restarted, "r1") == []


def test_a_web_branch_survives_a_restart(monkeypatch, tmp_path, fake_llm) -> None:
    """The branch's history was set in memory only, so a restart revived an
    empty chat (and a message sent to it then had no context)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    root = tmp_path / "transcripts"
    app = _web(monkeypatch, tmp_path, root, [
        fake_llm([fake_llm.text("reply one"), fake_llm.text("reply two")]),
        fake_llm([fake_llm.text("branch reply")]),
    ])
    for prompt in ("one", "two"):
        assert '"Done"' in app.post("/api/chat", json={"session_id": "p1", "message": prompt}).text
    branch = app.post("/api/session/p1/branch", json={"at_index": 2}).json()["new_session_id"]
    assert '"Done"' in app.post(
        "/api/chat", json={"session_id": branch, "message": "on the branch"}).text
    seen = _history(app, branch)
    assert [m["content"] for m in seen] == ["one", "reply one", "on the branch", "branch reply"]
    [record] = [e for e in _lines(root, branch) if e.get("type")]
    assert (record["type"], record["source"], record["cut"]) == (FORK, "p1", 2)
    # The copied messages are in the record, not logged again as this session's.
    assert [e["content"] for e in _lines(root, branch) if not e.get("type")] == [
        "on the branch", "branch reply",
    ]

    restarted = _web(monkeypatch, tmp_path, root, [fake_llm([]), fake_llm([])])
    assert _history(restarted, branch) == seen
    assert [m["content"] for m in _history(restarted, "p1")] == [
        "one", "reply one", "two", "reply two",
    ]


# ---- alignment: a revived agent pairs what the log may not ------------------


def test_records_line_up_after_a_revive_that_paired_an_unanswered_call(
    tmp_path, fake_llm
) -> None:
    """A turn that died mid-tools left a call with no logged result. A revive
    adds a placeholder result for it (pair_tool_results), so the revived
    agent's positions are one past the log's from there on; the edit it
    records still lands on the right message."""
    _write(tmp_path, "2026-10-16", "S1", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "user", "content": "are you there?"},
        {"role": "assistant", "content": "yes"},
    ])
    agent = _agent(fake_llm([fake_llm.text("answer")]), tmp_path)
    agent.history = [agent.history[0], *_replay(tmp_path)]
    assert len(agent.history) == 6  # the placeholder result is in

    assert agent.edit_message(4, "still there?")
    assert _errors(agent.continue_chat()) == []
    assert _replay(tmp_path) == agent.history[1:]
    assert [m["content"] for m in agent.history[4:]] == ["still there?", "answer"]


def test_a_cut_inside_a_tool_round_then_more_turns_then_an_edit(tmp_path, fake_llm) -> None:
    """Delete from here on a tool result leaves the other call unanswered in the
    live history (what is sent pairs it). Later records still line up, and the
    replay is the live history with that call paired, as it is sent."""
    from evi.tools.base import Tool

    tool = Tool(name="echo", description="echo", category="fs",
                parameters={"type": "object", "properties": {}}, func=lambda: "echoed")
    client = fake_llm([
        fake_llm.call(("echo", {}), ("echo", {})), fake_llm.text("done"),
        fake_llm.text("next"), fake_llm.text("more"), fake_llm.text("edited more"),
    ])
    agent = _agent(client, tmp_path, tools=[tool])
    assert _errors(agent.chat("go")) == []
    agent.truncate_history([m["role"] for m in agent.history].index("tool"))
    assert _errors(agent.chat("again")) == []
    assert _errors(agent.chat("and again")) == []
    assert agent.edit_message(len(agent.history) - 2, "and again, edited")
    assert _errors(agent.continue_chat()) == []
    assert _replay(tmp_path) == pair_tool_results(agent.history[1:])
    assert [m["content"] for m in agent.history[-4:]] == [
        "again", "next", "and again, edited", "edited more",
    ]


# ---- CLI: fork, then resume -------------------------------------------------


def _cli(monkeypatch, root: Path, clients: list, seen: list):
    """`evi` with agents built against `root` from `clients` (one per command)
    and a REPL that runs `prompts` (if any) then records the agent."""
    from evi.apps.cli import main as cli_main

    monkeypatch.setattr(sessions, "TRANSCRIPTS_DIR", root)
    monkeypatch.setattr(sessions, "HANDOFF_DIR", root.parent / "handoff")
    monkeypatch.setattr(cli_main, "_project_trust_prompt", lambda *a, **k: None)

    def build(*_a, **_k) -> Agent:
        return Agent(client=clients.pop(0), config=Config(), tools=[],
                     transcripts=TranscriptStore(root=root), unattended_policy="allow")

    def repl(agent: Agent) -> None:
        for prompt in getattr(repl, "prompts", []):
            assert _errors(agent.chat(prompt)) == []
        seen.append(agent)

    monkeypatch.setattr(cli_main, "_build_agent", build)
    monkeypatch.setattr(cli_main, "_run_repl", repl)
    return cli_main, repl


def test_cli_fork_then_resume_keeps_the_copied_prefix(monkeypatch, tmp_path, fake_llm) -> None:
    from typer.testing import CliRunner

    root = tmp_path / "transcripts"
    _write(root, "2026-10-16", "src", [
        {"role": "user", "content": "plan the trip"},
        {"role": "assistant", "content": "here is a plan"},
    ])
    seen: list[Agent] = []
    cli_main, repl = _cli(monkeypatch, root, [
        fake_llm([fake_llm.text("forked reply")]), fake_llm([]),
    ], seen)

    repl.prompts = ["and on the fork?"]
    r = CliRunner().invoke(cli_main.app, ["sessions", "fork", "src"])
    assert r.exit_code == 0, r.output
    fork = seen[-1]
    assert fork.session_id != "src"
    forked = [(m["role"], m["content"]) for m in fork.history[1:]]
    assert forked == [
        ("user", "plan the trip"), ("assistant", "here is a plan"),
        ("user", "and on the fork?"), ("assistant", "forked reply"),
    ]

    repl.prompts = []
    r = CliRunner().invoke(cli_main.app, ["sessions", "resume", fork.session_id])
    assert r.exit_code == 0, r.output
    assert "4 messages restored" in r.output
    assert [(m["role"], m["content"]) for m in seen[-1].history[1:]] == forked
    # The source is left as it was.
    assert [m["content"] for m in _replay(root, "src")] == ["plan the trip", "here is a plan"]


def test_cli_resume_replays_web_edits_and_compactions(monkeypatch, tmp_path, fake_llm) -> None:
    """`evi sessions resume` read the whole log, so a session edited in the web
    UI came back with the abandoned reply; now it replays like a revive."""
    from typer.testing import CliRunner

    root = tmp_path / "transcripts"
    _write(root, "2026-10-16", "w", [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "old reply"},
        {"type": EDIT, "role": "system", "content": "", "index": 0, "message_role": "user",
         "text": "one, edited", "length": 2},
        {"role": "assistant", "content": "new reply"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "reply two"},
        {"type": COMPACTION, "role": "system", "content": "", "summary": "they edited one",
         "cut": 3, "kept": 2, "collapsed": 2},
    ])
    seen: list[Agent] = []
    cli_main, _ = _cli(monkeypatch, root, [fake_llm([])], seen)
    r = CliRunner().invoke(cli_main.app, ["sessions", "resume", "w"])
    assert r.exit_code == 0, r.output
    contents = [m["content"] for m in seen[-1].history[1:]]
    assert "old reply" not in " ".join(contents)
    assert contents[-2:] == ["two", "reply two"] and "they edited one" in contents[0]


# ---- midnight ----------------------------------------------------------------


def test_a_session_crossing_midnight_resumes_completely(monkeypatch, tmp_path, fake_llm) -> None:
    root = tmp_path / "transcripts"
    store = _Clock(root=root)
    agent = _agent(fake_llm([fake_llm.text(t) for t in ("before", "after", "edited after")]),
                   root, transcripts=store)
    assert _errors(agent.chat("late question")) == []
    store.now = DAY2
    assert _errors(agent.chat("early question")) == []
    # An edit on the second day of a message from the first.
    assert agent.edit_message(1, "late question, edited")
    assert _errors(agent.continue_chat()) == []

    files = session_files("S1", root=root)
    assert [f.parent.name for f in files] == ["2026-10-16", "2026-10-17"]
    assert find_session("S1", root=root) == files[0]
    assert _replay(root) == agent.history[1:]

    # Every resume path: the CLI (_hydrate_agent), the web revive, export, handoff.
    from evi.apps.cli import main as cli_main

    fresh = _agent(fake_llm([]), root)
    assert cli_main._hydrate_agent(fresh, find_session("S1", root=root)) == 2
    assert fresh.history[1:] == agent.history[1:]
    md = sessions.export_session("S1", root=root)
    assert "late question" in md and "edited after" in md and "- Messages: 5" in md
    info = sessions.handoff_info("S1", root=root)
    assert info["day"] == "2026-10-16" and info["messages"] == 5

    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    app = _web(monkeypatch, tmp_path, root, [fake_llm([])])
    assert [(m["role"], m["content"]) for m in _history(app, "S1")] == [
        (m["role"], m["content"]) for m in agent.history[1:]
    ]


def test_find_session_is_the_first_day_and_the_log_reader_reads_one_file(tmp_path) -> None:
    _write(tmp_path, "2026-10-17", "s", [{"role": "assistant", "content": "after"}], DAY2)
    _write(tmp_path, "2026-10-16", "s", [{"role": "user", "content": "before"}], DAY1)
    first = find_session("s", root=tmp_path)
    assert first.parent.name == "2026-10-16"
    assert [m["content"] for m in history_from_transcript(first, replay=True)] == [
        "before", "after",
    ]
    # Without replay it reads the file it was given (fine-tune export goes file by file).
    assert [m["content"] for m in history_from_transcript(first)] == ["before"]
    assert find_session("../s", root=tmp_path) is None


def test_day_files_are_read_in_the_order_their_lines_were_written(tmp_path) -> None:
    """A device whose time zone moved west across midnight files later lines
    under the day before, so the day directories' order isn't the session's:
    read in that order, an edit landed on the wrong message, and a handed-off
    copy that came back looked like one that went on differently."""
    here, there, synced = tmp_path / "here", tmp_path / "there", tmp_path / "synced"
    start = DAY2 + 30 * 60  # 00:32 on the 17th, then 08:42 on the 16th further west
    _write(here, "2026-10-17", "z", [
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "reply one"},
    ], start)
    _write(here, "2026-10-16", "z", [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply two"},
        {"type": EDIT, "role": "system", "content": "", "index": 2, "message_role": "user",
         "text": "second, edited", "length": 4},
        {"role": "assistant", "content": "reply to the edit"},
    ], start + 600)
    want = ["first", "reply one", "second, edited", "reply to the edit"]
    assert [m["content"] for m in _replay(here, "z")] == want
    md = sessions.export_session("z", root=here)
    assert md.index("reply one") < md.index("reply two") < md.index("reply to the edit")

    # Handed off, went on over there, and handed back: this device's
    # transcript is how the copy starts.
    sessions.stage_handoff("z", root=here, dest=synced)
    assert sessions.adopt_handoff("z", root=there, src=synced) == "adopted"
    assert [m["content"] for m in _replay(there, "z")] == want
    TranscriptStore(root=there).write_message("z", "user", "third", timestamp=start + 1200)
    sessions.stage_handoff("z", root=there, dest=synced)
    assert sessions.adopt_handoff("z", root=here, src=synced) == "adopted"
    assert [m["content"] for m in _replay(here, "z")] == [*want, "third"]


def test_retention_keeps_a_session_that_ran_on_into_a_kept_day(tmp_path) -> None:
    """Pruning a session's first day made its replay bring back what an edit
    had dropped: the records count messages from the session's start. A
    session now goes by its last day."""
    from datetime import timedelta

    now = datetime.now()
    old, older = now - timedelta(days=60), now - timedelta(days=61)
    day = {d: d.strftime("%Y-%m-%d") for d in (now, old, older)}
    _write(tmp_path, day[older], "gone", [{"role": "user", "content": "bye"}], older.timestamp())
    _write(tmp_path, day[old], "gone too", [{"role": "user", "content": "x"}], old.timestamp())
    _write(tmp_path, day[old], "s", [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "r1"},
    ], old.timestamp())
    _write(tmp_path, day[now], "s", [
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "r2"},
        {"type": EDIT, "role": "system", "content": "", "index": 2, "message_role": "user",
         "text": "two, edited", "length": 4},
        {"role": "assistant", "content": "r2b"},
    ], now.timestamp())

    # Only the day with nothing still going is removed.
    assert TranscriptStore(root=tmp_path).prune(keep_days=30) == 1
    assert not (tmp_path / day[older]).exists()
    assert sorted(p.name for p in (tmp_path / day[old]).iterdir()) == ["s.jsonl"]
    assert [m["content"] for m in _replay(tmp_path, "s")] == ["one", "r1", "two, edited", "r2b"]


# ---- stats and fine-tune export ignore meta records -------------------------


def test_stats_and_finetune_output_are_unchanged_by_meta_records(tmp_path) -> None:
    from evi import finetune, stats

    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "content": "a.txt", "tool_name": "ls", "tool_call_id": "c1"},
        {"role": "assistant", "content": "reply one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "reply two"},
    ]
    messages = [{**m, "ts": DAY1 + i} for i, m in enumerate(messages)]
    records = [
        {"type": EDIT, "role": "system", "content": "", "index": 4, "message_role": "user",
         "text": "two, edited", "length": 6},
        {"type": TRUNCATE, "role": "system", "content": "", "keep": 3, "length": 5},
        {"type": COMPACTION, "role": "system", "content": "", "summary": "s", "cut": 2,
         "kept": 2, "collapsed": 1},
        {"type": FORK, "role": "system", "content": "", "source": "x", "cut": 1,
         "messages": [{"role": "user", "content": "copied"}]},
    ]
    records = [{**r, "ts": DAY1 + 2.5} for r in records]
    plain, meta = tmp_path / "plain", tmp_path / "meta"
    _write(plain, "2026-10-16", "s", messages)
    _write(meta, "2026-10-16", "s", [*messages[:3], *records[:2], *messages[3:], *records[2:]])

    a, b = stats.compute_stats(root=plain), stats.compute_stats(root=meta)
    assert a == b and b["messages"] == 6 and b["tools"] == {"ls": 1}

    for include_tools in (False, True):
        out_a, out_b = tmp_path / f"a{include_tools}.jsonl", tmp_path / f"b{include_tools}.jsonl"
        assert finetune.export_dataset(out_a, root=plain, include_tools=include_tools) == (1, 1)
        assert finetune.export_dataset(out_b, root=meta, include_tools=include_tools) == (1, 1)
        assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")


def test_a_branch_nobody_wrote_in_changes_no_count(tmp_path) -> None:
    """A web Branch or `evi sessions fork` with no turn of its own leaves a
    file holding only its fork_of record. It counted as a session in `evi
    stats` and fine-tune export, and `evi sessions continue` picked it over
    the session you were working in. It still resumes by id."""
    from evi import finetune, stats

    copied = [{"role": "user", "content": "copied ask"}, {"role": "assistant", "content": "reply"}]
    plain, branched = tmp_path / "plain", tmp_path / "branched"
    for root in (plain, branched):
        _write(root, "2026-10-16", "src", copied, DAY1 - 60)
    TranscriptStore(root=branched).write_fork("f", source="src", messages=copied, timestamp=DAY1)

    assert stats.compute_stats(root=plain) == stats.compute_stats(root=branched)
    for include_tools in (False, True):
        out_a, out_b = tmp_path / f"a{include_tools}.jsonl", tmp_path / f"b{include_tools}.jsonl"
        assert finetune.export_dataset(out_a, root=plain, include_tools=include_tools) == (1, 1)
        assert finetune.export_dataset(out_b, root=branched, include_tools=include_tools) == (1, 1)
        assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")
    assert [s.session_id for s in sessions.list_sessions(root=branched)] == ["src"]
    assert sessions.most_recent_session_id(root=branched) == "src"
    assert [m["content"] for m in _replay(branched, "f")] == ["copied ask", "reply"]

    # With a message of its own it is a session, listed by the first prompt it copied.
    TranscriptStore(root=branched).write_message("f", "user", "on the branch", timestamp=DAY1 + 1)
    [fork] = [s for s in sessions.list_sessions(root=branched) if s.session_id == "f"]
    assert (fork.first_user_message, fork.message_count) == ("copied ask", 1)
    assert sessions.most_recent_session_id(root=branched) == "f"


def test_search_and_dream_skip_meta_records(tmp_path) -> None:
    from evi import dream

    store = TranscriptStore(root=tmp_path)
    store.write_message("s", "user", "hello")
    store.write_edit("s", index=0, role="user", text="needle in an edit", length=1)
    store.write_fork("s", source="x", messages=[{"role": "user", "content": "needle copied"}])
    assert sessions.search_sessions("needle", root=tmp_path) == []
    entries = list(store.iter_since(datetime(2000, 1, 1)))
    assert "needle" not in dream._format_transcripts(entries)


# ---- handoff: sync carries a copy, resume takes it in -----------------------


def test_handoff_copy_round_trip(tmp_path) -> None:
    here, there, synced = tmp_path / "here", tmp_path / "there", tmp_path / "synced"
    _write(here, "2026-10-16", "h", [{"role": "user", "content": "one"}], DAY1)
    _write(here, "2026-10-17", "h", [{"role": "assistant", "content": "reply one"}], DAY2)

    assert sessions.adopt_handoff("h", root=there, src=synced) == "none"
    staged = sessions.stage_handoff("h", root=here, dest=synced)
    assert staged == synced / "h.jsonl"
    assert sessions.adopt_handoff("h", root=there, src=synced) == "adopted"
    assert [f.parent.name for f in session_files("h", root=there)] == ["2026-10-16", "2026-10-17"]
    assert _replay(there, "h") == _replay(here, "h")
    assert sessions.adopt_handoff("h", root=there, src=synced) == "current"

    # It goes on over there and comes back: this device's copy is how it starts.
    TranscriptStore(root=there).write_message("h", "user", "two", timestamp=DAY2 + 60)
    sessions.stage_handoff("h", root=there, dest=synced)
    assert sessions.adopt_handoff("h", root=here, src=synced) == "adopted"
    assert [m["content"] for m in _replay(here, "h")] == ["one", "reply one", "two"]

    # Both went on: this device's transcript is kept as it is.
    TranscriptStore(root=here).write_message("h", "user", "three here", timestamp=DAY2 + 120)
    TranscriptStore(root=there).write_message("h", "user", "three there", timestamp=DAY2 + 120)
    sessions.stage_handoff("h", root=there, dest=synced)
    assert sessions.adopt_handoff("h", root=here, src=synced) == "diverged"
    assert _replay(here, "h")[-1]["content"] == "three here"
    assert sessions.stage_handoff("missing", root=here, dest=synced) is None
    assert sessions.adopt_handoff("../h", root=here, src=synced) == "none"


def test_adopts_at_once_add_the_copy_once(tmp_path) -> None:
    """The web's first two requests for a session both revive it, and so both
    take the copy in."""
    import threading

    here, there, synced = tmp_path / "here", tmp_path / "there", tmp_path / "synced"
    _write(here, "2026-10-16", "h", [
        {"role": "user", "content": f"line {i}"} for i in range(50)
    ], DAY1 - 100)
    sessions.stage_handoff("h", root=here, dest=synced)
    results: list[str] = []
    gate = threading.Barrier(4)

    def adopt() -> None:
        gate.wait()
        results.append(sessions.adopt_handoff("h", root=there, src=synced))

    threads = [threading.Thread(target=adopt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["adopted", "current", "current", "current"]
    assert len(_lines(there, "h")) == 50


_ADOPT_IN_A_PROCESS = """
import sys, time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from evi import sessions

there, synced, ready, go = map(Path, sys.argv[2:6])
files = sessions.session_files


def slow(*a, **k):
    out = files(*a, **k)
    time.sleep(0.5)  # between comparing and appending: room for the other process
    return out


sessions.session_files = slow
ready.touch()
while not go.exists():
    time.sleep(0.01)
print(sessions.adopt_handoff("h", root=there, src=synced))
"""


def test_adopts_in_two_processes_add_the_copy_once(tmp_path) -> None:
    """`evi sessions resume` and the desktop app reviving the same session take
    the copy in from two processes, which a lock in one process doesn't stop:
    both appended it, and the conversation was there twice for good."""
    import os
    import sys
    import time

    here, there, synced = tmp_path / "here", tmp_path / "there", tmp_path / "synced"
    _write(here, "2026-10-16", "h", [
        {"role": "user", "content": f"line {i}"} for i in range(5)
    ], DAY1 - 100)
    sessions.stage_handoff("h", root=here, dest=synced)
    repo = str(Path(sessions.__file__).resolve().parents[1])
    go = tmp_path / "go"
    ready = [tmp_path / f"ready{i}" for i in range(2)]
    env = {**os.environ, "EVI_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _ADOPT_IN_A_PROCESS, repo, str(there), str(synced),
             str(flag), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for flag in ready
    ]
    try:
        deadline = time.monotonic() + 60
        while not all(f.exists() for f in ready) and time.monotonic() < deadline:
            if any(p.poll() is not None for p in procs):
                break
            time.sleep(0.02)
        go.touch()
        outs = [p.communicate(timeout=60) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    assert sorted(out.strip() for out, _ in outs) == ["adopted", "current"], outs
    assert len(_lines(there, "h")) == 5


def test_a_copy_that_cannot_be_taken_in_leaves_the_session_to_load(
    monkeypatch, tmp_path, fake_llm
) -> None:
    """A copy that isn't UTF-8 text made adopt_handoff raise, which emptied the
    web revive (it shared the revive's try) and crashed `evi sessions resume`.
    Now an unreadable copy is no copy, and whatever else fails taking one in,
    this device's transcript still loads."""
    from typer.testing import CliRunner

    root = tmp_path / "transcripts"
    _write(root, "2026-10-16", "w", [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "reply"},
    ])
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    (handoff / "w.jsonl").write_bytes(b"\xff\xfe garbage\n")
    assert sessions.adopt_handoff("w", root=root, src=handoff) == "none"
    assert len(_lines(root, "w")) == 2

    # A copy with lines that aren't entries: only the entries come in, and
    # the readers (replay, the listing) take what came in.
    there = tmp_path / "there"
    (handoff / "g.jsonl").write_text("\n".join([
        "[1, 2]", '"text"',
        json.dumps({"session": "g", "ts": "x", "role": "user", "content": "hi"}),
        json.dumps({"session": "g", "ts": DAY1, "role": "assistant", "content": "hello"}),
        '{"session": "g", "cut short',
    ]) + "\n", encoding="utf-8")
    assert sessions.adopt_handoff("g", root=there, src=handoff) == "adopted"
    assert len(_lines(there, "g")) == 2
    assert [m["content"] for m in _replay(there, "g")] == ["hi", "hello"]
    assert sum(s.message_count for s in sessions.list_sessions(root=there)) == 2
    assert sessions.adopt_handoff("g", root=there, src=handoff) == "current"

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "adopt_handoff", broken)
    seen: list[Agent] = []
    cli_main, _ = _cli(monkeypatch, root, [fake_llm([])], seen)
    r = CliRunner().invoke(cli_main.app, ["sessions", "resume", "w"])
    assert r.exit_code == 0, r.output
    assert "could not take in" in r.output
    assert [m["content"] for m in seen[-1].history[1:]] == ["one", "reply"]

    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    app = _web(monkeypatch, tmp_path, root, [fake_llm([])])
    assert [m["content"] for m in _history(app, "w")] == ["one", "reply"]


def test_cli_handoff_copies_the_session_and_resume_takes_it_in(
    monkeypatch, tmp_path, fake_llm
) -> None:
    from typer.testing import CliRunner

    root = tmp_path / "transcripts"
    _write(root, "2026-10-16", "h", [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "reply"},
    ])
    seen: list[Agent] = []
    cli_main, _ = _cli(monkeypatch, root, [fake_llm([])], seen)
    r = CliRunner().invoke(cli_main.app, ["sessions", "handoff", "h"])
    assert r.exit_code == 0, r.output
    assert "evi sync push" in r.output and "evi sessions resume h" in r.output
    staged = tmp_path / "handoff" / "h.jsonl"
    assert staged.is_file()

    # The other device: no transcript of its own, the copy arrived by sync.
    other = tmp_path / "other"
    cli_main, _ = _cli(monkeypatch, other / "transcripts", [fake_llm([])], seen)
    monkeypatch.setattr(sessions, "HANDOFF_DIR", staged.parent)
    r = CliRunner().invoke(cli_main.app, ["sessions", "resume", "h"])
    assert r.exit_code == 0, r.output
    assert [m["content"] for m in seen[-1].history[1:]] == ["one", "reply"]


def test_cli_fork_takes_in_a_handed_off_copy(monkeypatch, tmp_path, fake_llm) -> None:
    """`evi sessions fork <id>` of a session that is on this device only as a
    copy handed off from another said 'no session'."""
    from typer.testing import CliRunner

    here, synced = tmp_path / "here", tmp_path / "synced"
    _write(here, "2026-10-16", "h", [
        {"role": "user", "content": "one"}, {"role": "assistant", "content": "reply"},
    ])
    sessions.stage_handoff("h", root=here, dest=synced)
    seen: list[Agent] = []
    cli_main, _ = _cli(monkeypatch, tmp_path / "other" / "transcripts", [fake_llm([])], seen)
    monkeypatch.setattr(sessions, "HANDOFF_DIR", synced)
    r = CliRunner().invoke(cli_main.app, ["sessions", "fork", "h"])
    assert r.exit_code == 0, r.output
    fork = seen[-1]
    assert fork.session_id != "h"
    assert [m["content"] for m in fork.history[1:]] == ["one", "reply"]


def test_sync_carries_handed_off_sessions(tmp_path) -> None:
    """Even from a sync repo set up before handoff/ was synced: its managed
    .gitignore still ignores the folder."""
    from evi import sync

    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
    a = tmp_path / "a"
    a.mkdir()
    sync.init(remote=str(bare), root=a)
    old = sync.GITIGNORE.replace("!/handoff/\n", "")
    (a / ".gitignore").write_text(old, encoding="utf-8")
    git(a, "config", "user.email", "test@example.com")
    git(a, "config", "user.name", "Test")
    (a / "transcripts" / "2026-10-16").mkdir(parents=True)
    (a / "transcripts" / "2026-10-16" / "h.jsonl").write_text("{}\n", encoding="utf-8")
    sessions.stage_handoff("h", root=a / "transcripts", dest=a / "handoff")
    assert "pushed" in sync.push(message="handoff", root=a)
    tracked = git(a, "ls-files").stdout.split()
    assert "handoff/h.jsonl" in tracked
    assert not any(t.startswith("transcripts/") for t in tracked)

    b = tmp_path / "b"
    b.mkdir()
    sync.init(remote=str(bare), root=b)
    sync.pull(root=b)
    assert (b / "handoff" / "h.jsonl").read_text(encoding="utf-8") == "{}\n"


# ---- web helpers (TestClient against the real app) --------------------------


def _web(monkeypatch, tmp_path: Path, root: Path, clients: list):
    """The web app with sessions built against `root` from `clients`, one per
    new session (a branch makes one too). A second _web over the same root is
    the same eVi after a restart: nothing live, sessions revived from disk."""
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.sdk.builder as builder_mod

    monkeypatch.setattr(sessions, "TRANSCRIPTS_DIR", root)
    monkeypatch.setattr(sessions, "HANDOFF_DIR", tmp_path / "handoff")
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    pool = list(clients)

    def build(**kw) -> Agent:
        return Agent(client=pool.pop(0), config=Config(), tools=[],
                     transcripts=TranscriptStore(root=root), session_id=kw["session_id"],
                     unattended_policy="allow")

    def no_client(*_a):
        raise RuntimeError("scripted clients only")

    monkeypatch.setattr(builder_mod, "build_agent", build)
    # A branch rebuilds its client from the parent's config; failing, it keeps
    # the scripted one it was built with.
    monkeypatch.setattr(server_mod, "make_client", no_client)
    return TestClient(server_mod.create_app())


def _history(app, sid: str) -> list[dict]:
    """The session's messages as the browser gets them, the system prompt left out."""
    msgs = app.get(f"/api/session/{sid}/history").json()["messages"]
    return [{k: v for k, v in m.items() if k != "index"} for m in msgs if m["role"] != "system"]
