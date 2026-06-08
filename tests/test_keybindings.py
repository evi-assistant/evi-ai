"""Tests for configurable REPL keybindings (Phase 82)."""

from __future__ import annotations

import pytest

from evi.keybindings import load_keybindings


def test_load_missing_returns_empty(tmp_path):
    assert load_keybindings(tmp_path / "none.toml") == {}


def test_load_parses_section(tmp_path):
    p = tmp_path / "keybindings.toml"
    p.write_text('[keybindings]\n"c-t" = "/tools"\n"f2" = "/model"\n', encoding="utf-8")
    assert load_keybindings(p) == {"c-t": "/tools", "f2": "/model"}


def test_bare_top_level_table(tmp_path):
    p = tmp_path / "k.toml"
    p.write_text('"c-t" = "/tools"\n', encoding="utf-8")
    assert load_keybindings(p) == {"c-t": "/tools"}


def test_reserved_keys_dropped(tmp_path):
    p = tmp_path / "k.toml"
    p.write_text(
        '[keybindings]\n"c-c" = "/exit"\n"tab" = "/help"\n"c-t" = "/tools"\n',
        encoding="utf-8",
    )
    # c-c and tab are reserved; only c-t survives.
    assert load_keybindings(p) == {"c-t": "/tools"}


def test_blank_command_dropped(tmp_path):
    p = tmp_path / "k.toml"
    p.write_text('[keybindings]\n"c-t" = "  "\n', encoding="utf-8")
    assert load_keybindings(p) == {}


def test_malformed_toml_returns_empty(tmp_path):
    p = tmp_path / "k.toml"
    p.write_text("this is = = not toml\n", encoding="utf-8")
    assert load_keybindings(p) == {}


def test_build_key_bindings():
    pytest.importorskip("prompt_toolkit")
    from evi.repl_input import _build_key_bindings

    assert _build_key_bindings({}) is None

    kb = _build_key_bindings({"c-t": "/tools", "escape g": "/goal"})
    assert kb is not None
    # Both bindings registered (the second is a two-key sequence).
    assert len(kb.bindings) == 2


def test_build_skips_bad_key():
    pytest.importorskip("prompt_toolkit")
    from evi.repl_input import _build_key_bindings

    # "not-a-real-key" is rejected by prompt_toolkit; the valid one stays.
    kb = _build_key_bindings({"not-a-real-key": "/x", "c-t": "/tools"})
    assert kb is not None and len(kb.bindings) == 1
