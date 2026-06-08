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


# --- Claude-Code-style features (Phase 62) ------------------------------


def test_frontmatter_parsed_and_stripped(tmp_path: Path) -> None:
    (tmp_path / "commit.md").write_text(
        "---\n"
        "description: Draft a commit message\n"
        "argument-hint: [scope]\n"
        "model: qwen2.5-coder:14b\n"
        "---\n"
        "Write a commit for $ARGUMENTS\n"
    )
    store = CommandStore(root=tmp_path)
    entry = store.get("commit")
    assert entry.description == "Draft a commit message"
    assert entry.summary == "Draft a commit message"
    assert entry.argument_hint == "[scope]"
    assert entry.model == "qwen2.5-coder:14b"
    # body is expanded without the frontmatter block
    out = store.expand("commit", "auth")
    assert out == "Write a commit for auth"
    assert "description:" not in out


def test_arguments_and_positional(tmp_path: Path) -> None:
    (tmp_path / "pr.md").write_text("All: $ARGUMENTS | first: $1 | second: $2\n")
    store = CommandStore(root=tmp_path)
    assert store.expand("pr", "alpha beta") == "All: alpha beta | first: alpha | second: beta"
    # missing positional → blank
    assert store.expand("pr", "solo") == "All: solo | first: solo | second:"


def test_quoted_positional(tmp_path: Path) -> None:
    (tmp_path / "q.md").write_text("first=$1 second=$2\n")
    store = CommandStore(root=tmp_path)
    assert store.expand("q", '"two words" tail') == "first=two words second=tail"


def test_file_reference_inlined(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("FILE BODY", encoding="utf-8")
    (tmp_path / "read.md").write_text(f"Summarize @{tmp_path / 'data.txt'}\n")
    store = CommandStore(root=tmp_path)
    out = store.expand("read", "")
    assert "FILE BODY" in out


def test_file_reference_left_when_missing(tmp_path: Path) -> None:
    (tmp_path / "c.md").write_text("ping @nobody and email a@b.com\n")
    store = CommandStore(root=tmp_path)
    out = store.expand("c", "")
    assert "@nobody" in out and "a@b.com" in out  # neither is a readable file


def test_namespaced_subdirectory(tmp_path: Path) -> None:
    (tmp_path / "git").mkdir()
    (tmp_path / "git" / "commit.md").write_text("commit for $ARGUMENTS\n")
    store = CommandStore(root=tmp_path)
    assert any(e.name == "git:commit" for e in store.list())
    assert store.expand("git:commit", "x") == "commit for x"


def test_legacy_args_still_works(tmp_path: Path) -> None:
    (tmp_path / "old.md").write_text("legacy {args} here\n")
    store = CommandStore(root=tmp_path)
    assert store.expand("old", "Z") == "legacy Z here"
