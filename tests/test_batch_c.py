"""Batch C: project-intelligence pack — anatomy map, bug ledger, reflection."""

import json

from evi import anatomy, bugledger, reflect


# --- anatomy map --------------------------------------------------------------

def test_build_anatomy_lists_files_with_tokens(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n" * 50, encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    md = anatomy.build_anatomy(tmp_path)
    assert "Project map" in md
    assert "a.py" in md and "README.md" in md
    assert "tok" in md  # token estimates rendered


def test_anatomy_ignores_junk(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (tmp_path / "keep.py").write_text("ok\n", encoding="utf-8")
    md = anatomy.build_anatomy(tmp_path)
    assert "keep.py" in md
    assert "x.pyc" not in md


def test_anatomy_keep_predicate():
    from pathlib import Path

    # Ignored DIRECTORIES are excluded...
    assert not anatomy._keep(Path(".evi/anatomy.md"))
    assert not anatomy._keep(Path("dist/app.js"))
    assert not anatomy._keep(Path("a.png"))      # ignored suffix
    # ...but a FILE merely named like an ignore token is kept.
    assert anatomy._keep(Path("build"))
    assert anatomy._keep(Path("src/target"))
    assert anatomy._keep(Path("a.py"))


def test_write_and_load_anatomy(tmp_path):
    p = anatomy.write_anatomy(tmp_path)
    assert p == tmp_path / ".evi" / "anatomy.md"
    assert p.is_file()
    assert anatomy.load_anatomy(tmp_path).startswith("# Project map")
    assert anatomy.load_anatomy(tmp_path / "nope") is None


def test_anatomy_injected_into_project_context(tmp_path):
    from evi.project import load_project_context

    (tmp_path / "EVI.md").write_text("project rules\n", encoding="utf-8")
    anatomy.write_anatomy(tmp_path)
    ctx = load_project_context(start=tmp_path)
    assert ctx is not None
    assert "project rules" in ctx.content
    assert "Project map" in ctx.content  # anatomy appended


# --- bug ledger ---------------------------------------------------------------

def test_bugledger_record_and_search(tmp_path):
    bugledger.record("crash on startup", "null config", "guard the config load", root=tmp_path)
    bugledger.record("slow query", "missing index", "add index on user_id", root=tmp_path)
    assert (tmp_path / ".evi" / "bug-ledger.jsonl").is_file()
    hits = bugledger.search("index", root=tmp_path)
    assert len(hits) == 1 and "user_id" in hits[0].fix
    # empty query → most recent first
    recent = bugledger.search("", root=tmp_path)
    assert recent[0].symptom == "slow query"


def test_bugledger_rejects_empty(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        bugledger.record("", "c", "", root=tmp_path)


def test_bugledger_tool_roundtrip(tmp_path, monkeypatch):
    from evi.tools import bugledger as bl_tool

    monkeypatch.setattr(bugledger, "_root", lambda root: tmp_path)
    out = bl_tool.record_fix("boom", "bad import", "fix the import")
    assert "recorded" in out
    res = json.loads(bl_tool.search_fixes("import"))
    assert res[0]["fix"] == "fix the import"


# --- reflection ---------------------------------------------------------------

def test_reflect_writes_memories(tmp_path):
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)
    msgs = [
        {"role": "user", "content": "always use tabs here"},
        {"role": "assistant", "content": "got it"},
    ]

    def fake_run(prompt):
        assert "always use tabs" in prompt
        return '[{"name": "indent-style", "content": "Use tabs in this repo", "tags": "style"}]'

    written = reflect.reflect(msgs, run_one=fake_run, store=store)
    assert written == ["indent-style"]
    assert "tabs" in store.read("indent-style")


def test_reflect_empty_and_bad_json(tmp_path):
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)
    msgs = [{"role": "user", "content": "hi"}]
    assert reflect.reflect(msgs, run_one=lambda p: "[]", store=store) == []
    assert reflect.reflect(msgs, run_one=lambda p: "not json", store=store) == []
    assert reflect.reflect([], run_one=lambda p: "[]", store=store) == []


def test_reflect_extracts_json_amid_prose(tmp_path):
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)
    msgs = [{"role": "user", "content": "always use tabs"}]
    # Brackets in surrounding prose used to break the greedy regex.
    reply = 'Sure, see [the docs].\n```json\n[{"name":"a","content":"x"}]\n```\nDone [end].'
    assert reflect.reflect(msgs, run_one=lambda p: reply, store=store) == ["a"]


def test_reflect_does_not_clobber_existing(tmp_path):
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)
    store.write("preferences", "ORIGINAL hand-authored", tags=["mine"])
    msgs = [{"role": "user", "content": "note this"}]
    reply = '[{"name":"preferences","content":"reflected fact"}]'
    written = reflect.reflect(msgs, run_one=lambda p: reply, store=store)
    assert written == ["preferences-reflected"]      # didn't overwrite
    assert "ORIGINAL" in store.read("preferences")    # original intact


def test_reflect_slugifies_name(tmp_path):
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)
    msgs = [{"role": "user", "content": "x"}]
    reply = '[{"name":"Use Tabs Here!","content":"tabs"}]'
    written = reflect.reflect(msgs, run_one=lambda p: reply, store=store)
    assert written == ["use-tabs-here"]
    assert "tabs" in store.read("use-tabs-here")
