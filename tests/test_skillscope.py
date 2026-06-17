"""Tests for active-skill tool scoping (allowed/disallowed-tools)."""

from __future__ import annotations

import pytest

from evi import skillscope
from evi.skills import SkillStore
from evi.tools.base import Tool


@pytest.fixture(autouse=True)
def _reset_scope():
    skillscope.clear()
    yield
    skillscope.clear()


def _tool(name):
    return Tool(name=name, description="", parameters={"type": "object", "properties": {}},
                func=lambda: "ok")


def test_no_scope_allows_everything():
    assert skillscope.allows("write_file") is True
    assert not skillscope.active()


def test_allow_list_restricts():
    skillscope.activate(frozenset({"read_file", "find_files"}), frozenset())
    assert skillscope.allows("read_file") is True
    assert skillscope.allows("write_file") is False
    assert skillscope.active()


def test_deny_list_blocks():
    skillscope.activate(None, frozenset({"run_command"}))
    assert skillscope.allows("read_file") is True
    assert skillscope.allows("run_command") is False


def test_filter_tools():
    skillscope.activate(frozenset({"read_file"}), frozenset())
    kept = skillscope.filter_tools([_tool("read_file"), _tool("write_file")])
    assert [t.name for t in kept] == ["read_file"]


def test_clear_resets():
    skillscope.activate(frozenset({"x"}), frozenset())
    skillscope.clear()
    assert skillscope.allows("anything") is True


def _make_skill(root, name, frontmatter_extra=""):
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: t\n{frontmatter_extra}---\nbody\n", encoding="utf-8"
    )


def test_tool_scope_parses_allowed(tmp_path):
    _make_skill(tmp_path, "reader", "allowed-tools: read_file, find_files\n")
    allowed, disallowed = SkillStore(root=tmp_path).tool_scope("reader")
    assert allowed == frozenset({"read_file", "find_files"})
    assert disallowed == frozenset()


def test_tool_scope_parses_disallowed(tmp_path):
    _make_skill(tmp_path, "safe", "disallowed-tools: write_file run_command\n")
    allowed, disallowed = SkillStore(root=tmp_path).tool_scope("safe")
    assert allowed is None
    assert disallowed == frozenset({"write_file", "run_command"})


def test_tool_scope_none_when_unset(tmp_path):
    _make_skill(tmp_path, "plain")
    assert SkillStore(root=tmp_path).tool_scope("plain") == (None, frozenset())


def test_invoke_skill_activates_scope(tmp_path, monkeypatch):
    import evi.tools.skills as skmod

    _make_skill(tmp_path, "reader", "allowed-tools: read_file\n")
    monkeypatch.setattr(skmod, "_store", SkillStore(root=tmp_path))
    out = skmod.invoke_skill("reader")
    assert "tools scoped to: read_file" in out
    assert skillscope.allows("read_file") is True
    assert skillscope.allows("write_file") is False
