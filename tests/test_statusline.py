"""Tests for the customizable REPL status line (Phase 72)."""

from __future__ import annotations

from evi import statusline
from evi.config import Config, StatusLineSettings
from evi.llm.agent import Agent


def test_render_default():
    state = {"model": "qwen", "pct": 12, "branch": "main", "goal": "", "fast": ""}
    out = statusline.render(state)
    assert "qwen" in out and "12% ctx" in out and "main" in out
    assert "goal:" not in out  # empty goal omitted


def test_render_goal_and_fast():
    state = {"model": "m", "pct": 0, "branch": "-", "goal": "ship it", "fast": "fast"}
    out = statusline.render(state)
    assert "goal: ship it" in out and "fast" in out


def test_render_custom_format():
    state = {"model": "m", "pct": 5, "branch": "dev", "goal": "", "fast": "",
             "used": 100, "ceiling": 2000, "effort": "high"}
    out = statusline.render(state, "{model}@{branch} {used}/{ceiling} {effort}")
    assert out == "m@dev 100/2000 high"


def test_render_bad_format_falls_back():
    state = {"model": "m", "pct": 0, "branch": "-", "goal": "", "fast": ""}
    # {nope} is not a valid token → fall back to default rather than crash
    out = statusline.render(state, "{nope}")
    assert "m" in out


def test_status_line_disabled_by_default():
    agent = Agent(client=object(), config=Config(), tools=[])
    assert statusline.status_line(agent, agent.config) is None


def test_status_line_enabled():
    cfg = Config()
    cfg.statusline = StatusLineSettings(enabled=True, format="model={model}")
    agent = Agent(client=object(), config=cfg, tools=[])
    line = statusline.status_line(agent, cfg)
    assert line is not None and line.startswith("model=")


def test_command_overrides_format(monkeypatch):
    cfg = Config()
    cfg.statusline = StatusLineSettings(enabled=True, command="echo from-cmd")
    agent = Agent(client=object(), config=cfg, tools=[])
    monkeypatch.setattr(statusline, "render_via_command", lambda c, s, timeout=5.0: "CUSTOM")
    assert statusline.status_line(agent, cfg) == "CUSTOM"
