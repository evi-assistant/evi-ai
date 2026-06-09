"""Tests for long-context awareness in the model registry (lighter/later item)."""

from __future__ import annotations

from evi import recommend


def test_registry_entries_are_context_aware():
    # Every curated entry gets a non-zero native window (backfilled by family).
    assert all(m.context_window > 0 for m in recommend.REGISTRY)


def test_context_window_exact_id():
    win = recommend.context_window_for("qwen2.5:14b-instruct-q4_K_M")
    assert win == 32768


def test_context_window_family_prefix_for_unlisted_tag():
    # An unlisted quant still resolves via the family prefix.
    assert recommend.context_window_for("qwen2.5:14b-instruct-q8_0") == 32768
    assert recommend.context_window_for("llama3.1:70b-instruct-q4_K_M") == 131072


def test_context_window_coder_family():
    assert recommend.context_window_for("qwen2.5-coder:7b") == 32768


def test_context_window_unknown_returns_none():
    assert recommend.context_window_for("totally-unknown-model") is None
    assert recommend.context_window_for("") is None
