"""Tests for disabled plugins not contributing skills or commands."""

from __future__ import annotations

import pytest

from evi import plugins
from evi.commands import CommandStore
from evi.skills import SkillStore


def _make_plugin(src, name="gitx", with_command=True, with_skill=True, default_enabled=True):
    src.mkdir(parents=True, exist_ok=True)
    enabled_str = f'default_enabled = {str(default_enabled).lower()}\n'
    (src / "plugin.toml").write_text(
        f'name = "{name}"\ndescription = "test plugin"\nversion = "0.1.0"\n{enabled_str}',
        encoding="utf-8",
    )
    if with_command:
        (src / "commands").mkdir(exist_ok=True)
        (src / "commands" / "status.md").write_text(
            "Show status\n", encoding="utf-8"
        )
    if with_skill:
        (src / "skills" / "review").mkdir(parents=True, exist_ok=True)
        (src / "skills" / "review" / "SKILL.md").write_text(
            "---\ndescription: review\n---\nReview body\n", encoding="utf-8"
        )


def test_disabled_plugin_contributes_nothing(tmp_path):
    src = tmp_path / "src"
    _make_plugin(src, name="disabled_plugin")
    root = tmp_path / "home"
    plugins.install(str(src), root=root)

    # Disable it
    plugins.set_enabled("disabled_plugin", False, root=root)

    # 1. No skills
    ss = SkillStore(root=root / "skills")
    names = {e.name for e in ss.list()}
    assert "disabled_plugin:review" not in names
    with pytest.raises(KeyError):
        ss.read("disabled_plugin:review")

    # 2. No commands
    cs = CommandStore(root=root / "commands")
    assert cs.get("disabled_plugin:status") is None

    # Re-enable
    plugins.set_enabled("disabled_plugin", True, root=root)
    assert "disabled_plugin:review" in {e.name for e in ss.list()}
    assert cs.get("disabled_plugin:status") is not None


def test_default_disabled_plugin(tmp_path):
    src = tmp_path / "src"
    _make_plugin(src, name="off_plugin", default_enabled=False)
    root = tmp_path / "home"
    plugins.install(str(src), root=root)

    # 3. default_enabled = false contributes nothing
    ss = SkillStore(root=root / "skills")
    assert "off_plugin:review" not in {e.name for e in ss.list()}
    cs = CommandStore(root=root / "commands")
    assert cs.get("off_plugin:status") is None

    # Enable it
    plugins.set_enabled("off_plugin", True, root=root)
    assert "off_plugin:review" in {e.name for e in ss.list()}
    assert cs.get("off_plugin:status") is not None


def test_folder_without_manifest_contributes_nothing(tmp_path):
    root = tmp_path / "home"
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True)

    # 4. Folder without plugin.toml
    bad_plugin = plugins_dir / "no_manifest"
    bad_plugin.mkdir()
    (bad_plugin / "commands").mkdir()
    (bad_plugin / "commands" / "status.md").write_text("status\n", encoding="utf-8")
    (bad_plugin / "skills" / "review").mkdir(parents=True)
    (bad_plugin / "skills" / "review" / "SKILL.md").write_text("review\n", encoding="utf-8")

    ss = SkillStore(root=root / "skills")
    assert "no_manifest:review" not in {e.name for e in ss.list()}
    cs = CommandStore(root=root / "commands")
    assert cs.get("no_manifest:status") is None


def test_user_content_still_present(tmp_path):
    root = tmp_path / "home"
    (root / "skills" / "myskill").mkdir(parents=True)
    (root / "skills" / "myskill" / "SKILL.md").write_text("my skill\n", encoding="utf-8")
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "mine.md").write_text("my command\n", encoding="utf-8")

    # Install a plugin and disable it
    src = tmp_path / "src"
    _make_plugin(src, name="disabled_plugin")
    plugins.install(str(src), root=root)
    plugins.set_enabled("disabled_plugin", False, root=root)

    # 5. User's own skills and commands are still listed
    ss = SkillStore(root=root / "skills")
    assert "myskill" in {e.name for e in ss.list()}
    cs = CommandStore(root=root / "commands")
    assert "mine" in {e.name for e in cs.list()}


def test_count_hooks_all_events(tmp_path):
    root = tmp_path / "home"
    src = tmp_path / "src"
    _make_plugin(src, name="hook_plugin")
    hooks_toml = src / "hooks.toml"
    hooks_toml.write_text(
        '[[before_tool_call]]\n1 = "a"\n'
        '[[session_start]]\n2 = "b"\n'
        '[[stop]]\n3 = "c"\n'
        '[[user_prompt_submit]]\n4 = "d"\n'
        '[[bogus]]\n5 = "e"\n',
        encoding="utf-8",
    )
    plugins.install(str(src), root=root)

    # 6. Count entries under every event in ALL_EVENTS
    # list_plugins(root=home)[0].hooks == 4
    p = plugins.list_plugins(root=root)[0]
    assert p.hooks == 4


def test_count_hooks_malformed_or_missing(tmp_path):
    root = tmp_path / "home"
    src = tmp_path / "src"
    _make_plugin(src, name="malformed_plugin")
    # Missing hooks.toml is already handled by _count_hooks returning 0
    # but let's test malformed
    (src / "hooks.toml").write_text("this is not toml", encoding="utf-8")
    plugins.install(str(src), root=root)

    p = plugins.list_plugins(root=root)[0]
    assert p.hooks == 0
