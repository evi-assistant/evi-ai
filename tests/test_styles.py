"""Tests for output styles (Phase 69)."""

from __future__ import annotations

from evi import styles
from evi.config import Config
from evi.llm.agent import Agent


def test_builtins_listed_and_resolved():
    names = styles.list_styles()
    assert {"concise", "explanatory", "teacher"} <= set(names)
    assert "Concise" in styles.style_text("concise")
    assert styles.style_text("") == ""
    assert styles.style_text("nope") == ""


def test_user_file_overrides_builtin(tmp_path):
    d = styles.styles_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "concise.md").write_text("MY CUSTOM CONCISE", encoding="utf-8")
    (d / "custom.md").write_text("a custom style", encoding="utf-8")
    assert styles.style_text("concise", root=tmp_path) == "MY CUSTOM CONCISE"
    assert "custom" in styles.list_styles(root=tmp_path)
    assert styles.style_text("custom", root=tmp_path) == "a custom style"


def test_style_injected_into_system_prompt():
    cfg = Config()
    cfg.llm.output_style = "concise"
    agent = Agent(client=object(), config=cfg, tools=[])
    system = agent.history[0]["content"]
    assert "Concise" in system


def test_no_style_leaves_prompt_clean():
    cfg = Config()
    cfg.llm.output_style = ""
    agent = Agent(client=object(), config=cfg, tools=[])
    system = agent.history[0]["content"]
    assert "Response style" not in system
