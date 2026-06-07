"""Tests for the Obsidian sync (push / pull / sync / status)."""

from __future__ import annotations

from pathlib import Path

from evi.memory import MemoryStore
from evi.obsidian import _build_frontmatter, _strip_frontmatter, pull, push, status, sync


def _make_memory(tmp_path: Path) -> MemoryStore:
    root = tmp_path / "memory"
    return MemoryStore(root=root)


# ---- frontmatter helpers ------------------------------------------------


def test_strip_frontmatter_happy() -> None:
    text = "---\nsource: evi-memory\nname: x\n---\n\nbody here"
    meta, body = _strip_frontmatter(text)
    assert meta == {"source": "evi-memory", "name": "x"}
    assert body == "body here"


def test_strip_frontmatter_absent() -> None:
    text = "# just a body, no front-matter"
    meta, body = _strip_frontmatter(text)
    assert meta == {}
    assert body == text


def test_strip_handles_quoted_values() -> None:
    text = "---\nname: \"with quotes\"\n---\n\nbody"
    meta, _ = _strip_frontmatter(text)
    assert meta["name"] == "with quotes"


def test_build_frontmatter_round_trips_body() -> None:
    out = _build_frontmatter("foo", "the body", source_path=None)
    meta, body = _strip_frontmatter(out)
    assert meta["name"] == "foo"
    assert meta["source"] == "evi-memory"
    assert body.strip() == "the body"


# ---- push ---------------------------------------------------------------


def test_push_writes_each_memory_with_frontmatter(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    mem.write("prefs", "dark mode")
    mem.write("project", "C:/evi")

    vault = tmp_path / "vault"
    vault.mkdir()
    stats = push(mem, vault, "eVi")
    assert sorted(stats.pushed) == ["prefs", "project"]
    out_dir = vault / "eVi"
    assert (out_dir / "prefs.md").is_file()
    assert (out_dir / "project.md").is_file()
    text = (out_dir / "prefs.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: prefs" in text
    assert "dark mode" in text


def test_push_dry_run_writes_nothing(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    mem.write("solo", "only entry")
    vault = tmp_path / "vault"
    vault.mkdir()
    stats = push(mem, vault, "eVi", dry_run=True)
    assert stats.pushed == ["solo"]
    assert not (vault / "eVi" / "solo.md").exists()


def test_push_missing_vault_reports_error(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    mem.write("x", "y")
    stats = push(mem, tmp_path / "no-such-vault", "eVi")
    assert stats.pushed == []
    assert any("does not exist" in e for e in stats.errors)


# ---- pull ---------------------------------------------------------------


def test_pull_strips_frontmatter(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    vault = tmp_path / "vault" / "eVi"
    vault.mkdir(parents=True)
    (vault / "alpha.md").write_text(
        "---\nsource: evi-memory\nname: alpha\n---\n\nalpha body\n",
        encoding="utf-8",
    )
    stats = pull(mem, tmp_path / "vault", "eVi")
    assert "alpha" in stats.pulled
    assert "alpha body" in mem.read("alpha")
    assert "---" not in mem.read("alpha")


def test_pull_skips_invalid_names(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    vault = tmp_path / "vault" / "eVi"
    vault.mkdir(parents=True)
    (vault / "bad name with spaces.md").write_text("hello", encoding="utf-8")
    stats = pull(mem, tmp_path / "vault", "eVi")
    assert stats.pulled == []
    assert stats.skipped


def test_pull_missing_subdir_errors_cleanly(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    stats = pull(mem, vault, "eVi")
    assert stats.pulled == []
    assert any("not found" in e for e in stats.errors)


# ---- sync ---------------------------------------------------------------


def test_sync_pushes_memory_only(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    mem.write("only_in_mem", "value")
    vault = tmp_path / "vault"
    vault.mkdir()
    stats = sync(mem, vault, "eVi")
    assert "only_in_mem" in stats.pushed
    assert (vault / "eVi" / "only_in_mem.md").is_file()


def test_sync_pulls_vault_only(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    vault_sub = tmp_path / "vault" / "eVi"
    vault_sub.mkdir(parents=True)
    (vault_sub / "from_vault.md").write_text("body only", encoding="utf-8")
    stats = sync(mem, tmp_path / "vault", "eVi")
    assert "from_vault" in stats.pulled
    assert "body only" in mem.read("from_vault")


def test_sync_resolves_conflict_by_mtime(tmp_path: Path) -> None:
    """Newer side wins; equal mtimes are skipped."""
    import os
    import time

    mem = _make_memory(tmp_path)
    mem.write("shared", "memory version")
    vault_sub = tmp_path / "vault" / "eVi"
    vault_sub.mkdir(parents=True)
    vault_file = vault_sub / "shared.md"
    vault_file.write_text("---\nname: shared\n---\nvault version", encoding="utf-8")

    # Memory is older — vault should win.
    older = time.time() - 1000
    os.utime(mem._path_for("shared"), (older, older))
    stats = sync(mem, tmp_path / "vault", "eVi")
    assert "shared" in stats.pulled
    assert "vault version" in mem.read("shared")


# ---- status -------------------------------------------------------------


def test_status_classifies_entries(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    mem.write("only_mem", "a")
    mem.write("both", "b")
    vault_sub = tmp_path / "vault" / "eVi"
    vault_sub.mkdir(parents=True)
    (vault_sub / "both.md").write_text("vault both", encoding="utf-8")
    (vault_sub / "only_vault.md").write_text("vault solo", encoding="utf-8")

    info = status(mem, tmp_path / "vault", "eVi")
    assert info["only_in_memory"] == ["only_mem"]
    assert info["only_in_vault"] == ["only_vault"]
    assert info["in_both"] == ["both"]


def test_status_empty_vault(tmp_path: Path) -> None:
    mem = _make_memory(tmp_path)
    mem.write("x", "y")
    info = status(mem, tmp_path / "vault", "eVi")
    assert info["only_in_memory"] == ["x"]
    assert info["only_in_vault"] == []
    assert info["in_both"] == []
