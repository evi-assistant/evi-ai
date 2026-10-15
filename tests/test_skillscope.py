"""Tests for active-skill tool scoping (allowed/disallowed-tools)."""

from __future__ import annotations

from evi import skillscope
from evi.skills import SkillStore


def test_no_scope_allows_everything():
    assert skillscope.allows("write_file", None) is True


def test_allow_list_restricts():
    scope = (frozenset({"read_file", "find_files"}), frozenset())
    assert skillscope.allows("read_file", scope) is True
    assert skillscope.allows("write_file", scope) is False


def test_deny_list_blocks():
    scope = (None, frozenset({"run_command"}))
    assert skillscope.allows("read_file", scope) is True
    assert skillscope.allows("run_command", scope) is False


def test_narrow_never_widens():
    # A second skill in the same turn can only take tools away.
    a = (frozenset({"read_file", "write_file"}), frozenset())
    b = (frozenset({"read_file", "run_command"}), frozenset({"edit_file"}))
    assert skillscope.narrow(None, a) == a
    assert skillscope.narrow(a, b) == (frozenset({"read_file"}), frozenset({"edit_file"}))
    deny_only = (None, frozenset({"write_file"}))
    allow_write = (frozenset({"write_file", "read_file"}), frozenset())
    narrowed = skillscope.narrow(deny_only, allow_write)
    assert skillscope.allows("write_file", narrowed) is False
    assert skillscope.allows("read_file", narrowed) is True


def test_scope_of_only_reads_scoped_outputs():
    from evi.citations import ToolOutput

    scope = (None, frozenset({"x"}))
    assert skillscope.scope_of(skillscope.ScopedOutput(text="t", scope=scope)) == scope
    assert skillscope.scope_of(ToolOutput(text="t")) is None
    assert skillscope.scope_of(None) is None


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


def test_invoke_skill_returns_scope(tmp_path, monkeypatch):
    # invoke_skill runs on a tool thread, so it hands the scope back on its
    # result for the agent loop to apply (the Agent.chat-level check is in
    # tests/test_loop_state.py).
    import evi.tools.skills as skmod

    _make_skill(tmp_path, "reader", "allowed-tools: read_file\n")
    monkeypatch.setattr(skmod, "_store", SkillStore(root=tmp_path))
    out = skmod.invoke_skill("reader")
    assert "tools scoped to: read_file" in out.text
    scope = skillscope.scope_of(out)
    assert skillscope.allows("read_file", scope) is True
    assert skillscope.allows("write_file", scope) is False


def test_invoke_unscoped_skill_returns_plain_text(tmp_path, monkeypatch):
    import evi.tools.skills as skmod

    _make_skill(tmp_path, "plain")
    monkeypatch.setattr(skmod, "_store", SkillStore(root=tmp_path))
    out = skmod.invoke_skill("plain")
    assert isinstance(out, str) and "body" in out


def test_narrow_with_nothing_new_keeps_the_scope():
    a = (None, frozenset({"write_file"}))
    assert skillscope.narrow(a, None) == a
    assert skillscope.narrow(None, None) is None


def test_claude_code_names_map_to_evi_tools():
    # `Bash(git status:*)` reaches us split on its space.
    names = {"Read", "Edit", "Bash(git", "status:*)", "read_file", "server.tool"}
    assert skillscope.normalize_names(names) == frozenset(
        {"read_file", "edit_file", "apply_patch", "run_command", "server.tool"}
    )


def test_fit_ignores_an_allow_list_naming_no_known_tool():
    known = {"read_file", "write_file"}
    assert skillscope.fit((frozenset({"TodoWrite"}), frozenset()), known) is None
    deny = skillscope.fit((frozenset({"TodoWrite"}), frozenset({"write_file"})), known)
    assert deny == (None, frozenset({"write_file"}))
    kept = (frozenset({"read_file", "TodoWrite"}), frozenset())
    assert skillscope.fit(kept, known) == kept
    nothing = (frozenset(), frozenset())  # written empty on purpose
    assert skillscope.fit(nothing, known) == nothing
    assert skillscope.fit(None, known) is None


def test_invoke_skill_maps_claude_names(tmp_path, monkeypatch):
    import evi.tools.skills as skmod

    _make_skill(tmp_path, "claude", "allowed-tools: Read, Bash(git status:*)\n")
    monkeypatch.setattr(skmod, "_store", SkillStore(root=tmp_path))
    out = skmod.invoke_skill("claude")
    assert skillscope.scope_of(out) == (frozenset({"read_file", "run_command"}), frozenset())
    assert "[tools scoped to: read_file, run_command]" in out.text


def test_peek_reads_the_scope_invoke_skill_will_set(tmp_path, monkeypatch):
    import evi.tools.skills as skmod
    from evi.tools.base import REGISTRY

    _make_skill(tmp_path, "safe", "disallowed-tools: write_file\n")
    monkeypatch.setattr(skmod, "_store", SkillStore(root=tmp_path))
    invoke = REGISTRY["invoke_skill"]
    assert skillscope.peek(invoke, '{"name": "safe"}') == (None, frozenset({"write_file"}))
    assert skillscope.peek(invoke, '{"name": "missing"}') is None
    assert skillscope.peek(invoke, "not json") is None
    assert skillscope.peek(REGISTRY["list_skills"], "{}") is None


def test_bound_scope_flows_down_only():
    scope = (None, frozenset({"write_file"}))
    assert skillscope.inherited() is None
    token = skillscope.bind(scope)
    try:
        assert skillscope.inherited() == scope
    finally:
        skillscope.unbind(token)
    assert skillscope.inherited() is None
