"""Tests for --safe-mode (EVI_SAFE_MODE): disable all customizations."""

from __future__ import annotations


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")


def test_safe_mode_env_parsing(monkeypatch):
    from evi.apps.cli.main import _safe_mode

    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("EVI_SAFE_MODE", v)
        assert _safe_mode() is True, v
    for v in ("0", "", "no", "off"):
        monkeypatch.setenv("EVI_SAFE_MODE", v)
        assert _safe_mode() is False, v


def test_safe_mode_disables_customizations(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    from evi.apps.cli.main import _build_agent

    a = _build_agent(register=False)
    # every customization channel is off
    assert a.project is None
    assert a.memory is None
    assert a.skills is None
    assert a.hooks is None
    assert a.guardrails is None


def test_normal_mode_loads_customizations(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    from evi.apps.cli.main import _build_agent

    a = _build_agent(register=False)
    # the defaults (memory + hooks) load when not in safe mode
    assert a.memory is not None
    assert a.hooks is not None


# --- coverage beyond the CLI: build_agent is the single source of truth ---


def test_shared_helper_matches_cli(monkeypatch):
    from evi import safemode

    # setenv (not delenv) so monkeypatch TRACKS the key and reverts the
    # activate() below on teardown — otherwise safe mode leaks into later tests.
    monkeypatch.setenv("EVI_SAFE_MODE", "")
    assert safemode.enabled() is False
    safemode.activate()
    assert safemode.enabled() is True


def test_sdk_build_agent_honours_safe_mode(monkeypatch, tmp_path):
    # The web/desktop server builds agents via build_agent, not the CLI helper —
    # enforcing centrally means those surfaces get a clean boot too.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    from evi.sdk.builder import build_agent

    a = build_agent(enable_project=True, enable_hooks=True, enable_guardrails=True)
    assert a.project is None
    assert a.memory is None
    assert a.skills is None
    assert a.hooks is None
    assert a.guardrails is None


def test_safe_mode_drops_customization_tools(monkeypatch, tmp_path):
    # remember/recall + invoke_skill hold module-level stores, so the prompt-side
    # flags alone wouldn't keep a broken memory/skill out of the turn.
    _isolate(monkeypatch, tmp_path)
    from evi.sdk.builder import build_agent

    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    normal = set(build_agent(enable_memory=True, enable_skills=True).tools)
    assert "remember" in normal and "recall" in normal  # present normally

    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    safe = set(build_agent(enable_memory=True, enable_skills=True).tools)
    assert "remember" not in safe and "recall" not in safe
    assert "invoke_skill" not in safe and "list_skills" not in safe


def test_project_context_gated_at_the_loader(monkeypatch, tmp_path):
    # Covers the mid-session reloads (/cd, --cwd, web workdir change) that
    # bypass Agent construction.
    (tmp_path / "EVI.md").write_text("project rules\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from evi.project import load_project_context

    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    assert load_project_context() is not None
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    assert load_project_context() is None


def test_hooks_gated_at_the_loader(monkeypatch, tmp_path):
    # `evi review` and the SDK call load_hooks() directly.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    from evi.hooks import load_hooks

    assert load_hooks().hooks == []


def test_safe_mode_skips_profile_and_project_overlays(monkeypatch, tmp_path):
    # Either overlay could re-introduce what safe mode is ruling out.
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text('[llm]\nmodel = "base"\n', encoding="utf-8")
    monkeypatch.setattr("evi.profiles.load_profile_overlay",
                        lambda *a, **k: {"llm": {"model": "from-profile"}})
    monkeypatch.setattr("evi.project.load_project_config_overlay",
                        lambda *a, **k: {"llm": {"model": "from-project"}})
    from evi.config import Config

    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    assert Config.load().llm.model == "from-project"  # overlays apply normally
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    assert Config.load().llm.model == "base"  # stock config


def test_safe_mode_skips_output_style(monkeypatch, tmp_path):
    # A user ~/.evi/styles/<name>.md overrides the builtin of the same name, so a
    # broken one must not survive a clean boot. It's inline in the prompt build.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("evi.styles.style_text", lambda *a, **k: "STYLE-MARKER")
    from evi.sdk.builder import build_agent

    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    normal = build_agent()
    normal.config.llm.output_style = "concise"
    normal.reset()
    assert "STYLE-MARKER" in normal.history[0]["content"]

    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    safe = build_agent()
    safe.config.llm.output_style = "concise"
    safe.reset()
    assert "STYLE-MARKER" not in safe.history[0]["content"]
