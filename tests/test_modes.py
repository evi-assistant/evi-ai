"""Tests for session modes (Chat / Cowork / Code)."""

from __future__ import annotations

# Importing the web server registers every @tool, populating the REGISTRY that
# mode_tools() filters over.
import evi.apps.web.server  # noqa: F401
from evi import modes


def test_resolve_defaults_unknown_to_chat():
    assert modes.resolve(None).name == "chat"
    assert modes.resolve("bogus").name == "chat"
    assert modes.resolve("code").name == "code"


def test_chat_mode_is_conversation_only():
    cats = {t.category for t in modes.mode_tools("chat")}
    assert cats <= {"memory", "skills"}
    assert "code" not in cats and "shell" not in cats and "fs" not in cats


def test_code_mode_includes_engineering_tools():
    cats = {t.category for t in modes.mode_tools("code")}
    assert "code" in cats
    assert "git" in cats
    assert "fs" in cats


def test_cowork_has_web_but_not_code():
    cats = {t.category for t in modes.mode_tools("cowork")}
    assert "web" in cats
    assert "fs" in cats
    assert "code" not in cats


def test_all_modes_have_memory_and_skills():
    for name in ("chat", "cowork", "code"):
        cats = {t.category for t in modes.mode_tools(name)}
        assert "memory" in cats and "skills" in cats
