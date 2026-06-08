"""Tests for the plugin system (Phase 68)."""

from __future__ import annotations

import pytest

from evi import plugins
from evi.commands import CommandStore


def _make_plugin(src, name="gitx", with_command=True):
    src.mkdir(parents=True, exist_ok=True)
    (src / "plugin.toml").write_text(
        f'name = "{name}"\ndescription = "git helpers"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    if with_command:
        (src / "commands").mkdir(exist_ok=True)
        (src / "commands" / "status.md").write_text(
            "Show git status for $ARGUMENTS\n", encoding="utf-8"
        )


def test_install_lists_and_exposes_commands(tmp_path):
    src = tmp_path / "src"
    _make_plugin(src)
    root = tmp_path / "home"

    name = plugins.install(str(src), root=root)
    assert name == "gitx"

    items = plugins.list_plugins(root=root)
    assert len(items) == 1
    assert items[0].name == "gitx" and items[0].commands == 1 and items[0].version == "0.1.0"

    # The command loader exposes it as gitx:status with no copying.
    cs = CommandStore(root=root / "commands")
    assert cs.get("gitx:status") is not None
    assert cs.expand("gitx:status", "now") == "Show git status for now"


def test_remove(tmp_path):
    src = tmp_path / "src"
    _make_plugin(src)
    root = tmp_path / "home"
    plugins.install(str(src), root=root)
    assert plugins.remove("gitx", root=root) is True
    assert plugins.list_plugins(root=root) == []
    assert plugins.remove("gitx", root=root) is False  # already gone


def test_name_override(tmp_path):
    src = tmp_path / "src"
    _make_plugin(src, name="gitx")
    root = tmp_path / "home"
    name = plugins.install(str(src), name="custom", root=root)
    assert name == "custom"
    assert plugins.list_plugins(root=root)[0].name == "custom"


def test_missing_manifest_errors(tmp_path):
    src = tmp_path / "bad"
    src.mkdir()
    with pytest.raises(plugins.PluginError):
        plugins.install(str(src), root=tmp_path / "home")


def test_install_unknown_source(tmp_path):
    with pytest.raises(plugins.PluginError):
        plugins.install(str(tmp_path / "does-not-exist"), root=tmp_path / "home")


def test_plugin_skills_exposed(tmp_path):
    from evi.skills import SkillStore

    src = tmp_path / "src"
    src.mkdir()
    (src / "plugin.toml").write_text('name = "kit"\nversion = "1.0"\n', encoding="utf-8")
    skill = src / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: do a review\n---\nReview steps here\n", encoding="utf-8"
    )
    root = tmp_path / "home"
    plugins.install(str(src), root=root)

    # counted in the plugin listing
    p = plugins.list_plugins(root=root)[0]
    assert p.skills == 1

    # exposed via SkillStore as kit:review (no copying)
    ss = SkillStore(root=root / "skills")
    names = {e.name for e in ss.list()}
    assert "kit:review" in names
    assert "Review steps here" in ss.read("kit:review")


def test_user_commands_and_plugin_coexist(tmp_path):
    root = tmp_path / "home"
    # a user command
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "mine.md").write_text("my command\n", encoding="utf-8")
    # a plugin command
    src = tmp_path / "src"
    _make_plugin(src)
    plugins.install(str(src), root=root)

    cs = CommandStore(root=root / "commands")
    names = {e.name for e in cs.list()}
    assert "mine" in names and "gitx:status" in names


def test_plugin_hooks_loaded(tmp_path, monkeypatch):
    """A plugin's hooks.toml is merged into the hook registry (Phase 80)."""
    import evi.config as config
    from evi import hooks

    src = tmp_path / "src"
    src.mkdir()
    (src / "plugin.toml").write_text('name = "audit"\nversion = "1.0"\n', encoding="utf-8")
    (src / "hooks.toml").write_text(
        '[[before_tool_call]]\nname = "log"\nmatch = "*"\ncommand = ["echo", "hi"]\n',
        encoding="utf-8",
    )
    root = tmp_path / "home"
    plugins.install(str(src), root=root)

    # counted in the listing
    assert plugins.list_plugins(root=root)[0].hooks == 1

    # picked up by load_hooks even when the user's own hooks.toml is absent
    monkeypatch.setattr(config, "HOME", root)
    reg = hooks.load_hooks(path=tmp_path / "no-such-hooks.toml")
    assert [h.name for h in reg.hooks] == ["log"]


def test_plugin_mcp_loaded(tmp_path, monkeypatch):
    """A plugin's mcp.json is merged, namespaced <plugin>:<name> (Phase 80)."""
    import evi.config as config
    from evi.mcp import servers

    src = tmp_path / "src"
    src.mkdir()
    (src / "plugin.toml").write_text('name = "tools"\nversion = "1.0"\n', encoding="utf-8")
    (src / "mcp.json").write_text(
        '[{"name": "files", "command": "mcp-files"}]', encoding="utf-8"
    )
    root = tmp_path / "home"
    plugins.install(str(src), root=root)

    assert plugins.list_plugins(root=root)[0].mcp == 1

    monkeypatch.setattr(config, "HOME", root)
    srv = servers.load_servers(path=tmp_path / "no-such-mcp.json")
    assert [s.name for s in srv] == ["tools:files"]
