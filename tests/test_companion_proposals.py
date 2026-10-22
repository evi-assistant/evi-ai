"""Companion proposals (evi/companion/engine.py): made only for allowed,
non-money, non-deleting, guard-clean actions; approved once, exactly as
stored, with no model in the loop; expiry, tampering, rejection and every
step in the audit.

eVi's home is under tmp_path; the action is a stub tool in the registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evi.companion import engine as engine_mod
from evi.companion.engine import ProposalError, action_refusal, args_hash, money_or_delete
from evi.config import Config
from evi.tools.base import REGISTRY, Tool
from tests.conftest import llm_text
from tests.test_companion_engine import (
    Sources,
    answer,
    at,
    audit,
    item,
    make,
    setup_home,
    write_config,
)

ARGS = {"text": "buy milk", "tags": "shopping"}


def proposal(tool: str = "note_add", args: dict | None = None) -> dict:
    return {"tool": tool, "args": ARGS if args is None else args, "summary": "Add a note"}


@pytest.fixture
def home(monkeypatch, tmp_path):
    return setup_home(monkeypatch, tmp_path)


@pytest.fixture
def sources(home, monkeypatch) -> Sources:
    return Sources(monkeypatch)


@pytest.fixture
def proposed(home, sources, fake_llm):
    """A Companion with one pending note_add proposal (made at 10:00)."""
    write_config(home, 'sources = ["calendar_week"]\nallowed_actions = ["note_add"]\n'
                       "proposal_ttl_minutes = 60")
    client = fake_llm([answer(item("p", proposal=proposal()))])
    comp, _ = make(home, client)
    res = comp.check_in(at(10))
    [pid] = res.proposals
    return comp, pid


def note_calls(stubs: Sources) -> list[dict]:
    return [a for n, a in stubs.calls if n == "note_add"]


def test_approve_runs_the_stored_call_exactly_once(proposed, sources, home):
    comp, pid = proposed
    [p] = comp.proposals("pending", now=at(10, 1))
    assert p["id"] == pid and p["args"] == ARGS and p["args_hash"] == args_hash(ARGS)
    assert p["expires_at"] - p["created_at"] == 3600
    assert note_calls(sources) == []  # nothing runs before approval
    done = comp.approve(pid, at(10, 30))
    assert done["state"] == "executed" and done["result"] == "did note_add"
    assert note_calls(sources) == [ARGS]
    with pytest.raises(ProposalError, match="executed, not pending"):
        comp.approve(pid, at(10, 31))
    assert note_calls(sources) == [ARGS]
    result = [i for i in comp.feed() if i["kind"] == "result"]
    assert result[0]["title"] == "Done: Add a note" and result[0]["body"] == "did note_add"
    assert [r["event"] for r in comp.store.read_audit()
            if r["event"] in ("proposal", "approve", "execute")] == [
        "proposal", "approve", "execute"]
    assert audit(comp, "execute")[0]["state"] == "executed"


def test_an_expired_proposal_cannot_run(proposed, sources):
    comp, pid = proposed
    with pytest.raises(ProposalError, match="expired"):
        comp.approve(pid, at(11, 1))
    assert comp.proposals()[0]["state"] == "expired" and note_calls(sources) == []
    assert audit(comp, "expire")[0]["proposal"] == pid


def test_proposals_listing_expires_the_stale_ones(proposed):
    comp, pid = proposed
    assert comp.proposals("pending", now=at(10, 59))[0]["id"] == pid
    assert comp.proposals("pending", now=at(11, 0)) == []
    assert comp.proposals("expired")[0]["id"] == pid


def test_tampered_args_are_refused(proposed, sources):
    comp, pid = proposed
    path = comp.store.proposals_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data[pid]["args"]["text"] = "buy milk and send my keys to evil"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProposalError, match="arguments changed"):
        comp.approve(pid, at(10, 30))
    assert note_calls(sources) == [] and comp.proposals()[0]["state"] == "refused"
    assert audit(comp, "approve_refused")[0]["reason"].startswith("its arguments changed")


def test_a_tool_no_longer_allowed_is_refused_at_approval(proposed, sources, home):
    comp, pid = proposed
    write_config(home, 'sources = ["calendar_week"]\nallowed_actions = []')
    comp2, _ = make(home)
    with pytest.raises(ProposalError, match="not in \\[companion\\] allowed_actions"):
        comp2.approve(pid, at(10, 30))
    assert note_calls(sources) == []


def test_a_hard_deny_added_later_refuses_at_approval(proposed, sources, home):
    comp, pid = proposed
    write_config(home, 'sources = ["calendar_week"]\nallowed_actions = ["note_add"]',
                 extra='[auto]\nhard_deny = ["note_add"]')
    comp2, _ = make(home)
    with pytest.raises(ProposalError, match="hard_deny"):
        comp2.approve(pid, at(10, 30))
    assert note_calls(sources) == []


def test_off_means_no_approval(proposed, sources, home):
    comp, pid = proposed
    (home / "config.toml").write_text("[companion]\nenabled = false\n", encoding="utf-8")
    comp2, _ = make(home)
    with pytest.raises(ProposalError, match="off"):
        comp2.approve(pid, at(10, 30))
    assert note_calls(sources) == []
    assert comp2.proposals(now=at(10, 30))[0]["state"] == "pending"


def test_reject_is_recorded_and_final(proposed, sources):
    comp, pid = proposed
    assert comp.reject(pid, at(10, 5))["state"] == "rejected"
    with pytest.raises(ProposalError):
        comp.approve(pid, at(10, 6))
    with pytest.raises(KeyError):
        comp.approve("nope")
    assert note_calls(sources) == [] and audit(comp, "reject")[0]["proposal"] == pid


def test_a_corrupt_proposals_file_approves_nothing(proposed, sources):
    comp, pid = proposed
    comp.store.proposals_path.write_text("{oops", encoding="utf-8")
    with pytest.raises(KeyError):
        comp.approve(pid, at(10, 30))
    assert note_calls(sources) == []
    assert list(comp.store.root.glob("proposals.json.corrupt-*"))


def test_the_result_is_redacted(proposed, monkeypatch):
    comp, pid = proposed
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    monkeypatch.setitem(REGISTRY, "note_add", Tool(
        name="note_add", description="x", category="general",
        parameters={"type": "object", "properties": {}}, func=lambda **kw: f"token {secret}"))
    done = comp.approve(pid, at(10, 30))
    assert secret not in done["result"] and "[REDACTED" in done["result"]
    assert secret not in comp.store.audit_path.read_text(encoding="utf-8")
    assert secret not in comp.store.feed_path.read_text(encoding="utf-8")


# ---- refused when made ---------------------------------------------------------------


@pytest.mark.parametrize("name, category", [
    ("bank_transfer", "general"), ("sendPayment", "general"), ("stocks.buy", "mcp"),
    ("delete_file", "fs"), ("forget", "memory"), ("wallet.swap", "mcp"),
    ("crm.purgeContacts", "mcp"), ("trash_mail", "general"), ("x", "payments"),
])
def test_money_and_deletion_tools_are_never_proposed(name, category):
    assert money_or_delete(name, category)


@pytest.mark.parametrize("name", ["note_add", "calendar_add_event", "display_message",
                                  "mail.draft", "reorder_list"])
def test_ordinary_tools_are_not_caught_by_the_denylist(name):
    assert not money_or_delete(name)


def test_the_denylist_wins_over_allowed_actions(home, sources, fake_llm):
    write_config(home, 'sources = ["calendar_week"]\n'
                       'allowed_actions = ["bank_transfer", "forget", "note_add"]')
    client = fake_llm([answer(
        item("a", proposal=proposal("bank_transfer", {"text": "100 EUR"})),
        item("b", proposal=proposal("forget", {"text": "x"})),
        item("c", proposal=proposal("note_add")),
    )])
    comp, _ = make(home, client)
    res = comp.check_in(at(10))
    assert [p["tool"] for p in comp.proposals(now=at(10, 1))] == ["note_add"]
    assert len(res.proposals) == 1
    assert {r["tool"] for r in res.refused} == {"bank_transfer", "forget"}


def test_the_destructive_guard_and_hard_deny_refuse_at_creation(home, sources):
    write_config(home, 'allowed_actions = ["run_command", "note_add", "write_file"]',
                 extra='[auto]\nhard_deny = ["note_add *secret*"]')
    cfg = Config.load()
    allowed = ["run_command", "note_add", "write_file"]
    assert "destructive-command guard" in action_refusal(
        "run_command", {"command": "git push --force origin main"}, cfg, allowed,
        need_tool=False)
    assert "hard_deny" in action_refusal("note_add", {"text": "the secret plan"}, cfg,
                                         allowed, need_tool=False)
    assert "protected path" in action_refusal(
        "write_file", {"path": str(home / "new.toml"), "content": "x"}, cfg, allowed,
        need_tool=False)
    assert "not absolute" in action_refusal(
        "write_file", {"path": "notes.md", "content": "x"}, cfg, allowed, need_tool=False)
    assert "not in [companion] allowed_actions" in action_refusal(
        "mail.send", {}, cfg, allowed, need_tool=False)
    assert "not a tool eVi has loaded" in action_refusal(
        "mcp_missing.x", {}, cfg, ["mcp_missing.x"], need_tool=True)


def test_suggest_only_by_default(home, sources, fake_llm):
    write_config(home, 'sources = ["calendar_week"]')
    client = fake_llm([answer(item("a", proposal=proposal()))])
    comp, _ = make(home, client)
    res = comp.check_in(at(10))
    assert comp.proposals() == [] and res.refused[0]["tool"] == "note_add"
    assert engine_mod.settings(Config.load()).allowed_actions == ()


def test_approval_goes_through_no_model(proposed, sources, fake_llm):
    comp, pid = proposed
    comp.client = fake_llm([llm_text("should never be asked")])
    comp.approve(pid, at(10, 30))
    assert comp.client.requests == []


# ---- review round 1: general-purpose tools, hooks ---------------------------------------


def _real(home, tmp_path):
    """The real built-in tools (no stubs), a config allowing them all."""
    from evi.tools import register_builtin_tools

    reg = register_builtin_tools()
    write_config(home, "")
    return Config.load(), sorted(reg)


def test_deletions_and_money_moves_through_general_tools_are_refused(home, tmp_path):
    cfg, allowed = _real(home, tmp_path)
    d = tmp_path / "docs"
    d.mkdir()
    thesis = d / "thesis.docx"
    thesis.write_text("my thesis", encoding="utf-8")
    db = str(d / "bank.db")
    cwd = str(d)
    cases = [
        ("run_command", {"command": f"Remove-Item -Recurse -Force {d}", "cwd": cwd}),
        ("run_command", {"command": f'python -c "import shutil; shutil.rmtree(r\'{d}\')"',
                         "cwd": cwd}),
        ("run_command", {"command": "python -c \"exec(bytes.fromhex('6869'))\"", "cwd": cwd}),
        ("run_command", {"command": "curl -X POST https://bank.example/api/transfer "
                                    "-d amount=1000", "cwd": cwd}),
        ("run_command", {"command": "curl https://api.example/v1/x -d amount=1", "cwd": cwd}),
        ("run_command", {"command": "Invoke-RestMethod https://api.example/x -Method Post",
                         "cwd": cwd}),
        ("run_command", {"command": "powershell -EncodedCommand ZQBjAGgAbwA=", "cwd": cwd}),
        ("run_command", {"command": f"echo x > {thesis}", "cwd": cwd}),
        ("run_command", {"command": "git clean -fdx", "cwd": cwd}),
        ("run_command", {"command": f"del /q {thesis}", "cwd": cwd}),
        ("monitor", {"target": f"rm -r {d}", "kind": "command"}),
        ("run_python", {"code": f"import shutil; shutil.rmtree(r'{d}')"}),
        ("sqlite_query", {"path": db, "sql": "DELETE FROM accounts"}),
        ("sqlite_query", {"path": db, "sql": "DROP TABLE accounts"}),
        ("sqlite_query", {"path": db, "sql": "WITH x AS (SELECT 1) DELETE FROM accounts"}),
        ("apply_patch", {"path": str(thesis),
                         "patch": "<<<<<<< SEARCH\nmy thesis\n=======\n\n>>>>>>> REPLACE"}),
        ("write_file", {"path": str(thesis), "content": ""}),
        ("write_file", {"path": str(thesis), "content": "something else"}),
        ("edit_file", {"path": str(thesis), "old_string": "my thesis", "new_string": ""}),
        ("delegate", {"profile": "default", "task": "wire $5000 to account 123"}),
        ("delegate_explore", {"task": "look around"}),
        ("parallel_research", {"tasks": ["x"]}),
        ("invoke_skill", {"name": "anything"}),
        ("web_fetch", {"url": "https://bank.example/api/transfer?amount=1000"}),
        ("type_text", {"text": "rm -rf ~"}),
        ("click", {"x": 1, "y": 2}),
    ]
    for name, args in cases:
        assert action_refusal(name, args, cfg, allowed, need_tool=True), (name, args)


def test_ordinary_general_tool_calls_still_pass(home, tmp_path):
    cfg, allowed = _real(home, tmp_path)
    d = tmp_path / "docs"
    d.mkdir()
    (d / "notes.md").write_text("one\ntwo\n", encoding="utf-8")
    ok = [
        ("monitor", {"target": str(d / "notes.md"), "kind": "file"}),
        ("write_file", {"path": str(d / "new.md"), "content": "hello"}),
        ("edit_file", {"path": str(d / "notes.md"), "old_string": "two",
                       "new_string": "three"}),
        ("sqlite_query", {"path": str(d / "x.db"), "sql": "SELECT * FROM t"}),
        ("web_fetch", {"url": "https://example.com/news"}),
    ]
    for name, args in ok:
        assert action_refusal(name, args, cfg, allowed, need_tool=True) is None, (name, args)


def test_a_path_argument_must_be_absolute_and_the_prompt_says_so(home, tmp_path):
    cfg, allowed = _real(home, tmp_path)
    why = action_refusal("monitor", {"target": "", "kind": "file"}, cfg, allowed,
                         need_tool=True)
    assert "left empty" in why and "absolute" in why
    assert "absolute path" in engine_mod.prompts.SYSTEM_PROMPT
    assert "never a shell command" in engine_mod.prompts.SYSTEM_PROMPT


# ---- review round 2: no shell line is ever proposed --------------------------------------


def _shell_lines(target: Path, empty: Path) -> list[str]:
    """Shell lines a word list let through in round 2: each deletes, empties
    or overwrites `target`, wipes something else or posts to the network."""
    return [
        f'copy /y nul "{target}"',
        f"powershell -Command \"Set-Content -Path '{target}' -Value ''\"",
        f"powershell -Command \"[IO.File]::WriteAllText('{target}','')\"",
        f"echo x | Out-File {target}",
        f"mv {target} /dev/null",
        f"tar -cf {target} README",
        f"robocopy {empty} {target.parent} /MIR",
        "docker system prune -af",
        "git push origin :main",
        "powershell -Command \"(New-Object Net.WebClient).UploadString("
        "'https://api.example.com/x','amount=5000&to=123')\"",
        f"bash -c 'cat /dev/null > {target}'",
        f"cmd /c type nul > {target}",
        "git status",
        "echo hi",
    ]


def test_no_shell_line_is_ever_proposed_even_when_listed(home, tmp_path, monkeypatch):
    cfg, allowed = _real(home, tmp_path)
    monkeypatch.setitem(REGISTRY, "other_shell", Tool(
        name="other_shell", description="stub", parameters={"type": "object"},
        func=lambda **_: "ran", category="shell"))
    d = tmp_path / "docs"
    d.mkdir()
    (d / "empty").mkdir()
    thesis = d / "thesis.txt"
    thesis.write_text("my thesis", encoding="utf-8")
    for line in _shell_lines(thesis, d / "empty"):
        for name, args in (("run_command", {"command": line, "cwd": str(d)}),
                           ("monitor", {"target": line, "kind": "command"})):
            for need_tool in (False, True):
                why = action_refusal(name, args, cfg, allowed, need_tool=need_tool)
                assert why, (name, line)
    # A tool of the "shell" category under another name, and arguments that
    # don't even parse as a command line, are refused all the same.
    assert "shell" in action_refusal("other_shell", {"command": "echo hi"}, cfg,
                                     ["other_shell"], need_tool=False)
    assert action_refusal("run_command", {"command": ["echo", "hi"], "cwd": str(d)}, cfg,
                          allowed, need_tool=True)
    assert thesis.read_text(encoding="utf-8") == "my thesis"


def test_a_shell_proposal_is_refused_at_check_in_and_at_approval(home, sources, fake_llm,
                                                                 tmp_path):
    thesis = tmp_path / "thesis.txt"
    thesis.write_text("my thesis", encoding="utf-8")
    write_config(home, 'sources = ["calendar_week"]\n'
                       'allowed_actions = ["run_command", "note_add"]')
    line = f'copy /y nul "{thesis}"'
    shell = {"command": line, "cwd": str(tmp_path)}
    client = fake_llm([answer(
        item("a", proposal={"tool": "run_command", "args": shell,
                            "summary": "Tidy up the notes file"}),
        item("b", proposal=proposal()))])
    comp, _ = make(home, client)
    res = comp.check_in(at(10))
    assert [r["tool"] for r in res.refused] == ["run_command"]
    assert "shell line" in res.refused[0]["reason"]
    [pid] = res.proposals
    assert [p["tool"] for p in comp.proposals()] == ["note_add"]
    # One stored before this rule (or written by hand) is refused on approval.
    path = comp.store.proposals_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data[pid].update(tool="run_command", args=shell, args_hash=args_hash(shell))
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProposalError, match="shell line"):
        comp.approve(pid, at(10, 30))
    assert sources.side_effects() == [] and comp.proposals()[0]["state"] == "refused"
    assert thesis.read_text(encoding="utf-8") == "my thesis"


@pytest.mark.parametrize("name", [
    "calendar.cancel_event", "slack.archive_channel", "trello.archive_card",
    "payroll.run_payroll", "cashapp.send_cash", "exchange.place_bid", "donate",
    "revoke_access", "docs.overwrite_doc", "reset_repo", "casino.bet", "tipJar.tip",
    "bank.xfer"])
def test_more_money_and_deletion_names_are_caught(name):
    assert money_or_delete(name, "mcp")


def _veto_hook(home, tmp_path, *, veto: bool) -> Path:
    """hooks.toml: a before_tool_call guard on note_add (exits 3 when
    `veto`) and an after_tool_call hook that writes the result to a file."""
    import sys

    out = tmp_path / "after.txt"
    code = "import sys; sys.exit(3)" if veto else "pass"
    after = (f"import os, pathlib; pathlib.Path('{out.as_posix()}')"
             ".write_text(os.environ.get('EVI_HOOK_RESULT', ''))")
    (home / "hooks.toml").write_text(
        "[[before_tool_call]]\nname = \"guard\"\nmatch = \"note_add\"\n"
        f"command = ['{sys.executable}', '-c', \"{code}\"]\nveto_on_nonzero = true\n\n"
        "[[after_tool_call]]\nname = \"log\"\nmatch = \"note_add\"\n"
        f"command = ['{sys.executable}', '-c', \"{after}\"]\n", encoding="utf-8")
    return out


def test_a_vetoing_hook_refuses_the_approval(proposed, sources, home, tmp_path):
    comp, pid = proposed
    out = _veto_hook(home, tmp_path, veto=True)
    with pytest.raises(ProposalError, match="BLOCKED BY HOOK 'guard'"):
        comp.approve(pid, at(10, 30))
    assert note_calls(sources) == [] and not out.exists()
    assert comp.proposals()[0]["state"] == "refused"
    assert "BLOCKED BY HOOK" in audit(comp, "approve_refused")[0]["reason"]
    with pytest.raises(ProposalError, match="refused, not pending"):
        comp.approve(pid, at(10, 31))


def test_hooks_see_an_approved_call(proposed, sources, home, tmp_path):
    comp, pid = proposed
    out = _veto_hook(home, tmp_path, veto=False)
    assert comp.approve(pid, at(10, 30))["state"] == "executed"
    assert note_calls(sources) == [ARGS]
    assert out.read_text(encoding="utf-8") == "did note_add"


def test_safe_mode_means_no_approval(proposed, sources, monkeypatch):
    comp, pid = proposed
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    with pytest.raises(ProposalError, match="safe mode"):
        comp.approve(pid, at(10, 30))
    assert note_calls(sources) == []
    assert comp.proposals(now=at(10, 30))[0]["state"] == "pending"
