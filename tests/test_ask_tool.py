"""Tests for the ask_user tool (interactive clarifying questions)."""

from __future__ import annotations

import builtins

import evi.tools.ask as ask
from evi.tools.ask import ask_user


def test_non_interactive_returns_fallback(monkeypatch):
    # No EVI_INTERACTIVE -> must not block; returns guidance for the model.
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    out = ask_user("Use approach A or B?", options="A, B")
    assert "isn't available" in out.lower()
    assert "Use approach A or B?" in out
    assert "A, B" in out


def test_requires_question(monkeypatch):
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    assert ask_user("").startswith("ERROR")


def test_interactive_numbered_choice(monkeypatch):
    monkeypatch.setenv("EVI_INTERACTIVE", "1")
    monkeypatch.setattr(ask, "_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *_: "2")
    assert ask_user("pick", options="red, green, blue") == "green"


def test_interactive_freeform(monkeypatch):
    monkeypatch.setattr(ask, "_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *_: "something else")
    assert ask_user("pick", options="red, green") == "something else"


def test_interactive_empty_answer(monkeypatch):
    monkeypatch.setattr(ask, "_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *_: "")
    assert "no answer" in ask_user("pick").lower()


def test_registered_in_registry():
    from evi.tools.base import REGISTRY

    assert "ask_user" in REGISTRY
    assert REGISTRY["ask_user"].category == "ask"
