"""Tests for the public Agent SDK surface (evi.sdk)."""

from __future__ import annotations

import evi.sdk as sdk
from evi.sdk import build_agent, register_builtin_tools, tool


# ---- the curated re-export surface is complete + resolvable ----------------


def test_all_names_resolve():
    missing = [name for name in sdk.__all__ if not hasattr(sdk, name)]
    assert missing == []


def test_headline_symbols_present():
    # A representative slice of every category the SDK promises.
    for name in (
        "Agent", "build_agent", "tool", "Tool", "ToolOutput",
        "run_subagent", "run_subagents_parallel",
        "SkillStore", "CommandStore", "HookRegistry", "load_hooks",
        "MCPManager", "Config", "make_client",
        "load_schema", "as_response_format",
        "run_headless", "HeadlessResult",
        "list_sessions", "rewind", "list_checkpoints",
        "run_ultracode", "run_workflow", "fan_out",
        "TextDelta", "ToolCall", "Done", "RouteInfo", "otel",
    ):
        assert hasattr(sdk, name), name


def test_version_reexported():
    from evi import __version__

    assert sdk.__version__ == __version__


# ---- register_builtin_tools is the canonical registrar ---------------------


def test_register_builtin_tools_populates_registry():
    reg = register_builtin_tools()
    assert reg is sdk.REGISTRY
    # core categories always present (no optional deps required)
    cats = {t.category for t in reg.values()}
    assert {"fs", "code", "memory"} <= cats


def test_register_builtin_tools_idempotent():
    a = len(register_builtin_tools())
    b = len(register_builtin_tools())
    assert a == b


def test_publish_populate_registry_delegates():
    # The MCP server's registry population now routes through the SDK registrar;
    # both must agree on the registry object.
    from evi.mcp import publish

    publish._populate_registry()
    assert "read_file" in sdk.REGISTRY


# ---- build_agent ------------------------------------------------------------


def _offline_config(tmp_path, monkeypatch):
    monkeypatch.setattr("evi.config.HOME", tmp_path)
    monkeypatch.setattr("evi.config.CONFIG_PATH", tmp_path / "config.toml")
    from evi.config import Config

    return Config.load()


def test_build_agent_defaults(tmp_path, monkeypatch):
    cfg = _offline_config(tmp_path, monkeypatch)
    agent = build_agent(config=cfg)
    assert agent.config is cfg
    # default selection follows config toggles; the agent has *some* tools
    assert isinstance(agent.tools, dict)


def test_build_agent_explicit_tools_skip_registry(tmp_path, monkeypatch):
    cfg = _offline_config(tmp_path, monkeypatch)

    @tool(category="custom", description="echo back")
    def echo(text: str) -> str:
        return text

    agent = build_agent(config=cfg, tools=[echo])
    assert set(agent.tools) == {"echo"}


def test_build_agent_tool_categories(tmp_path, monkeypatch):
    cfg = _offline_config(tmp_path, monkeypatch)
    agent = build_agent(config=cfg, tool_categories=["fs"])
    assert agent.tools  # at least one fs tool
    assert all(t.category == "fs" for t in agent.tools.values())


def test_build_agent_system_prompt_override(tmp_path, monkeypatch):
    cfg = _offline_config(tmp_path, monkeypatch)
    agent = build_agent(config=cfg, system_prompt="You are a teapot.", tools=[])
    assert "teapot" in agent.history[0]["content"]


def test_build_agent_toggles_off(tmp_path, monkeypatch):
    cfg = _offline_config(tmp_path, monkeypatch)
    agent = build_agent(
        config=cfg,
        tools=[],
        enable_memory=False,
        enable_skills=False,
        enable_project=False,
        enable_hooks=False,
        enable_guardrails=False,
    )
    assert agent.memory is None
    assert agent.skills is None
    assert agent.project is None
    assert agent.hooks is None
    assert agent.guardrails is None


def test_build_agent_enable_memory_override(tmp_path, monkeypatch):
    cfg = _offline_config(tmp_path, monkeypatch)
    agent = build_agent(config=cfg, tools=[], enable_memory=True)
    assert agent.memory is not None
