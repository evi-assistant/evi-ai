"""The shipped examples/ files must stay loadable by the real eVi loaders.

Examples are documentation users copy verbatim — if a format drifts, these break
loudly instead of silently shipping a broken sample.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"


def test_guardrails_example_loads():
    from evi.guardrails import Guardrails, validate

    text = (EX / "guardrails.toml").read_text(encoding="utf-8")
    assert validate(text) is None  # regexes compile, judges have policies
    g = Guardrails.load(EX / "guardrails.toml")
    assert g.enabled and g.rules and g.judge_rules and g.classifier_rules


def test_hooks_example_parses():
    data = tomllib.loads((EX / "hooks.toml").read_text(encoding="utf-8"))
    # at least the documented events are present and well-formed
    assert data["before_tool_call"] and data["stop"]
    for ev in data.values():
        for hook in ev:
            assert hook.get("command") or hook.get("url")


def test_routes_example_loads():
    from evi.routing import RouterStore

    routes = RouterStore(EX / "routes.json").load()
    assert {r.name for r in routes} == {"code", "quick"}
    assert all(r.model and r.match_keywords for r in routes)


def test_mcp_and_peers_examples_are_valid_json():
    mcp = json.loads((EX / "mcp.json").read_text(encoding="utf-8"))
    assert {s["name"] for s in mcp} >= {"filesystem", "git", "sqlite"}
    peers = json.loads((EX / "peers.json").read_text(encoding="utf-8"))
    assert peers and all("url" in p for p in peers)


def test_skill_examples_load():
    from evi.skills import SkillStore

    names = {e.name for e in SkillStore(root=EX / "skills").list()}
    assert {"code-review", "summarize-paper", "sql-explain"} <= names


def test_command_example_loads():
    from evi.commands import CommandStore

    cmds = {e.name for e in CommandStore(root=EX / "commands").list()}
    assert "commit" in cmds


def test_style_example_nonempty():
    assert (EX / "styles" / "concise.md").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("name", [
    "guardrails.toml", "hooks.toml", "routes.json", "mcp.json", "peers.json",
    "README.md", "EVI.md",
])
def test_example_file_present(name):
    assert (EX / name).is_file()
