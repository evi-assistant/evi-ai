"""Tests for the tar.gz backup / restore round-trip."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from evi.backup import (
    DEFAULT_EXCLUDES,
    create_backup,
    restore_backup,
)


def _make_home(tmp_path: Path) -> Path:
    """Build a fake ~/.evi/ with a mix of stuff worth keeping and skippable."""
    home = tmp_path / "evi-home"
    home.mkdir()
    (home / "config.toml").write_text("[llm]\nmodel = 'x'\n", encoding="utf-8")
    (home / "memory").mkdir()
    (home / "memory" / "prefs.md").write_text("dark mode", encoding="utf-8")
    (home / "skills").mkdir()
    (home / "skills" / "s1").mkdir()
    (home / "skills" / "s1" / "SKILL.md").write_text("hi", encoding="utf-8")

    # Skippable by default:
    (home / "models").mkdir()
    (home / "models" / "huge.gguf").write_bytes(b"x" * 1024)
    (home / "transcripts").mkdir()
    (home / "transcripts" / "2026-05-27").mkdir()
    (home / "transcripts" / "2026-05-27" / "s.jsonl").write_text("{}\n")
    (home / "logs").mkdir()
    (home / "logs" / "scheduled.log").write_text("noise")
    return home


def test_create_excludes_models_and_transcripts_by_default(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    summary = create_backup(out_path=archive, home=home)
    assert archive.is_file()
    assert summary.archive == archive
    assert "models" in summary.excluded_top
    assert "transcripts" in summary.excluded_top

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any("config.toml" in n for n in names)
    assert any("memory/prefs.md" in n for n in names)
    assert not any("models/huge.gguf" in n for n in names)
    assert not any("transcripts/" in n for n in names)
    assert not any("logs/" in n for n in names)


def test_create_with_explicit_includes(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    archive = tmp_path / "with-everything.tar.gz"
    create_backup(out_path=archive, home=home, includes={"models"})
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any("models/huge.gguf" in n for n in names)


def test_roundtrip_restore(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    archive = tmp_path / "rt.tar.gz"
    create_backup(out_path=archive, home=home)

    new_home = tmp_path / "new-home"
    new_home.mkdir()  # empty target
    summary = restore_backup(archive, home=new_home)
    assert summary.file_count >= 2
    assert (new_home / "config.toml").read_text() == "[llm]\nmodel = 'x'\n"
    assert (new_home / "memory" / "prefs.md").read_text() == "dark mode"
    assert (new_home / "skills" / "s1" / "SKILL.md").read_text() == "hi"


def test_restore_refuses_non_empty_home(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    archive = tmp_path / "x.tar.gz"
    create_backup(out_path=archive, home=home)

    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("hands off", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing to restore"):
        restore_backup(archive, home=target)


def test_restore_with_overwrite_merges(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    archive = tmp_path / "x.tar.gz"
    create_backup(out_path=archive, home=home)

    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("preserved", encoding="utf-8")

    restore_backup(archive, home=target, overwrite=True)
    # Pre-existing file untouched.
    assert (target / "secret.txt").read_text() == "preserved"
    # Restored files landed.
    assert (target / "config.toml").is_file()


def test_default_excludes_value() -> None:
    # Tripwire so we don't accidentally start packing models again.
    assert "models" in DEFAULT_EXCLUDES
    assert "transcripts" in DEFAULT_EXCLUDES
