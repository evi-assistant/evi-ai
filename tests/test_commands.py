"""Tests for the user-defined slash command store."""

from __future__ import annotations

from pathlib import Path

from evi.commands import CommandStore


def test_list_skips_invalid_names_and_summarises(tmp_path: Path) -> None:
    (tmp_path / "commit.md").write_text(
        "# header\n\nWrite a conventional commit message. Args: {args}\n"
    )
    (tmp_path / "bad name.md").write_text("ignored")
    (tmp_path / "README.txt").write_text("ignored")
    store = CommandStore(root=tmp_path)
    entries = store.list()
    assert [e.name for e in entries] == ["commit"]
    assert "commit message" in entries[0].summary.lower()


def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    store = CommandStore(root=tmp_path)
    assert store.get("nope") is None


def test_expand_substitutes_args(tmp_path: Path) -> None:
    (tmp_path / "echo.md").write_text("Please repeat: {args}\n")
    store = CommandStore(root=tmp_path)
    assert store.expand("echo", "hello there") == "Please repeat: hello there"


def test_expand_without_args_leaves_placeholder_blank(tmp_path: Path) -> None:
    (tmp_path / "review.md").write_text("Review this code: {args}\n")
    store = CommandStore(root=tmp_path)
    assert store.expand("review", "") == "Review this code:"


def test_expand_returns_none_for_unknown(tmp_path: Path) -> None:
    store = CommandStore(root=tmp_path)
    assert store.expand("nope", "x") is None


def test_expand_rejects_invalid_names(tmp_path: Path) -> None:
    """A traversal attempt via a path-like name shouldn't escape the dir."""
    store = CommandStore(root=tmp_path)
    assert store.expand("../etc/passwd", "") is None
