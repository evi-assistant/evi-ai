"""Tests for the persistent memory layer and its tool wrappers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evi.tools.memory as memory_tools
from evi.memory import MemoryStore
from evi.tools.base import REGISTRY


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    s = MemoryStore(root=tmp_path)
    # The tool wrappers use a module-level _store — point it at the temp dir too.
    monkeypatch.setattr(memory_tools, "_store", s)
    return s


def test_write_read_roundtrip(store: MemoryStore) -> None:
    store.write("preferences", "# Prefs\n\nlikes dark mode")
    assert "dark mode" in store.read("preferences")


def test_list_returns_summaries(store: MemoryStore) -> None:
    store.write("a", "# Title A\nbody")
    store.write("b", "first line of b\nmore")
    names = {e.name for e in store.list()}
    assert names == {"a", "b"}
    summary_by_name = {e.name: e.summary for e in store.list()}
    assert summary_by_name["a"] == "Title A"
    assert summary_by_name["b"] == "first line of b"


def test_delete_returns_existence(store: MemoryStore) -> None:
    store.write("temp", "x")
    assert store.delete("temp") is True
    assert store.delete("temp") is False


def test_delete_moves_to_attic(store: MemoryStore, tmp_path: Path) -> None:
    """Soft-delete leaves the file recoverable under .attic/."""
    store.write("preferences", "dark mode")
    store.delete("preferences")
    attic_files = list((tmp_path / ".attic").glob("preferences-*.md"))
    assert len(attic_files) == 1
    assert "dark mode" in attic_files[0].read_text("utf-8")


def test_restore_from_attic(store: MemoryStore, tmp_path: Path) -> None:
    store.write("prefs", "the body")
    store.delete("prefs")
    attic_file = next((tmp_path / ".attic").iterdir())
    restored = store.restore_from_attic(attic_file.name)
    assert restored is not None
    assert "the body" in store.read("prefs")


def test_hard_delete_skips_attic(store: MemoryStore, tmp_path: Path) -> None:
    store.write("x", "y")
    assert store.hard_delete("x") is True
    assert not (tmp_path / ".attic").exists() or not any(
        (tmp_path / ".attic").iterdir()
    )


def test_invalid_name_rejected(store: MemoryStore) -> None:
    with pytest.raises(ValueError):
        store.write("bad name with spaces", "x")
    with pytest.raises(ValueError):
        store.write("../escape", "x")
    with pytest.raises(ValueError):
        store.write("", "x")


def test_oversize_content_rejected(store: MemoryStore) -> None:
    huge = "a" * (64 * 1024 + 1)
    with pytest.raises(ValueError):
        store.write("big", huge)


def test_format_for_prompt_empty(store: MemoryStore) -> None:
    assert store.format_for_prompt() == ""


def test_format_for_prompt_lists_entries(store: MemoryStore) -> None:
    store.write("project", "eVi notes")
    out = store.format_for_prompt()
    assert "Memory index" in out
    assert "project" in out
    assert "eVi notes" in out


def test_index_file_kept_in_sync(tmp_path: Path) -> None:
    s = MemoryStore(root=tmp_path)
    s.write("one", "first")
    # Index lives at s.index_path now (formerly global INDEX_FILE). Confirm it
    # gets written, then disappears when the only entry is removed.
    assert s.index_path.is_file()
    assert "one" in s.index_path.read_text("utf-8")
    assert [e.name for e in s.list()] == ["one"]
    s.delete("one")
    assert s.list() == []


def test_tool_wrappers(store: MemoryStore) -> None:
    out = REGISTRY["remember"].call(
        json.dumps({"name": "facts", "content": "evi is at C:/evi"})
    )
    assert "saved memory 'facts'" in out

    assert "evi is at C:/evi" in REGISTRY["recall"].call(
        json.dumps({"name": "facts"})
    )

    listed = json.loads(REGISTRY["list_memories"].call("{}"))
    assert any(e["name"] == "facts" for e in listed)

    assert REGISTRY["forget"].call(json.dumps({"name": "facts"})) == "deleted"
    assert REGISTRY["recall"].call(json.dumps({"name": "facts"})).startswith(
        "ERROR:"
    )
