"""Tests for the Settings → Tools availability probe.

The probe must reflect the REAL install gate (find_spec / binary), not registry
presence — because most tool modules register even when their optional dep is
missing (the lazy-import property that once shipped web search broken).
"""

from __future__ import annotations

import evi.tools.availability as av


def test_missing_import_gate_is_reported(monkeypatch) -> None:
    # ddgs missing, every other import present; binaries present.
    monkeypatch.setattr(av, "_have_module", lambda name: name != "ddgs")
    monkeypatch.setattr(av, "_git_ok", lambda: True)
    monkeypatch.setattr(av, "_tesseract_ok", lambda: True)
    out = av.tool_availability()
    assert "web" in out
    assert out["web"]["available"] is False
    assert out["web"]["reason"]
    assert "web-tools" in out["web"]["how_to_enable"]


def test_satisfied_gates_are_not_reported(monkeypatch) -> None:
    # Everything present → no badges at all (absent key == available).
    monkeypatch.setattr(av, "_have_module", lambda name: True)
    monkeypatch.setattr(av, "_git_ok", lambda: True)
    monkeypatch.setattr(av, "_tesseract_ok", lambda: True)
    assert av.tool_availability() == {}


def test_missing_binary_gate_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(av, "_have_module", lambda name: True)
    monkeypatch.setattr(av, "_git_ok", lambda: False)   # git off PATH
    monkeypatch.setattr(av, "_tesseract_ok", lambda: True)
    out = av.tool_availability()
    assert "git" in out and out["git"]["available"] is False
    assert "ocr" not in out  # tesseract present → no badge


def test_frozen_hint_points_at_desktop(monkeypatch) -> None:
    # In the frozen desktop sidecar, "pip install" is misleading — the hint must
    # say the tool isn't bundled instead.
    monkeypatch.setattr(av, "_have_module", lambda name: name != "faster_whisper")
    monkeypatch.setattr(av, "_git_ok", lambda: True)
    monkeypatch.setattr(av, "_tesseract_ok", lambda: True)
    monkeypatch.setattr(av, "_FROZEN", True)
    out = av.tool_availability()
    assert "voice" in out
    assert "desktop app" in out["voice"]["how_to_enable"]


def test_never_flags_always_available_core(monkeypatch) -> None:
    # Even with everything "missing", core categories must never be badged —
    # they have no install gate at all.
    monkeypatch.setattr(av, "_have_module", lambda name: False)
    monkeypatch.setattr(av, "_git_ok", lambda: False)
    monkeypatch.setattr(av, "_tesseract_ok", lambda: False)
    out = av.tool_availability()
    for core in ("fs", "code", "shell", "memory", "subagent", "skills",
                 "transcripts", "sqlite", "ask", "federation"):
        assert core not in out


def test_probe_never_raises(monkeypatch) -> None:
    # A find_spec that explodes (broken partial install) counts as unavailable,
    # never an exception.
    import importlib.util

    def boom(_name):
        raise ImportError("partial install")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    out = av.tool_availability()  # must not raise
    assert out["web"]["available"] is False
