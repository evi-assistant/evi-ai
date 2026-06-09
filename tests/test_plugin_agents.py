"""Tests for plugin-supplied subagent profiles (lighter/later item)."""

from __future__ import annotations

from evi import plugins
from evi.llm import subagent


def _make_agent_plugin(root, name="kit"):
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "plugin.toml").write_text(f'name = "{name}"\nversion = "1.0"\n', encoding="utf-8")
    (src / "agents.toml").write_text(
        '[[agent]]\nname = "security"\n'
        'system_prompt = "You are a security reviewer."\ntools = ["fs"]\n'
        '[[agent]]\nname = "writer"\nsystem_prompt = "You write docs."\n',
        encoding="utf-8",
    )
    return src


def test_load_plugin_profiles(tmp_path, monkeypatch):
    import evi.config as config

    home = tmp_path / "home"
    src = _make_agent_plugin(tmp_path)
    plugins.install(str(src), root=home)
    monkeypatch.setattr(config, "HOME", home)

    profs = subagent.load_plugin_profiles()
    assert "kit:security" in profs and "kit:writer" in profs
    assert profs["kit:security"]["tool_categories"] == ("fs",)
    assert profs["kit:writer"]["tool_categories"] == ()  # default when omitted


def test_all_profiles_merges_builtin(tmp_path, monkeypatch):
    import evi.config as config

    home = tmp_path / "home"
    plugins.install(str(_make_agent_plugin(tmp_path)), root=home)
    monkeypatch.setattr(config, "HOME", home)

    allp = subagent.all_profiles()
    assert {"explore", "plan", "kit:security", "kit:writer"} <= set(allp)
    assert subagent.get_profile("kit:security") is not None
    assert subagent.get_profile("nope") is None


def test_plugin_listing_counts_agents(tmp_path):
    home = tmp_path / "home"
    plugins.install(str(_make_agent_plugin(tmp_path)), root=home)
    p = plugins.list_plugins(root=home)[0]
    assert p.agents == 2


def test_delegate_tool_unknown_profile():
    from evi.tools.subagent import delegate

    out = delegate("does-not-exist", "do a thing")
    assert out.startswith("ERROR:") and "unknown subagent profile" in out
