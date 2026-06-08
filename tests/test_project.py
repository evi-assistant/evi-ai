"""Tests for the EVI.md project context loader."""

from __future__ import annotations

from pathlib import Path

from evi.project import (
    find_project_config,
    find_project_file,
    load_project_config_overlay,
    load_project_context,
)


def test_find_walks_up_to_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "src" / "deep" / "module"
    nested.mkdir(parents=True)
    (root / "EVI.md").write_text("# project notes\n")
    found = find_project_file(start=nested)
    assert found == root / "EVI.md"


def test_find_returns_none_when_absent(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_file(start=nested) is None


def test_find_prefers_closest(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / "EVI.md").write_text("outer\n")
    (inner / "EVI.md").write_text("inner\n")
    assert find_project_file(start=inner) == inner / "EVI.md"


def test_load_returns_content(tmp_path: Path) -> None:
    (tmp_path / "EVI.md").write_text("hello project\n", encoding="utf-8")
    ctx = load_project_context(start=tmp_path)
    assert ctx is not None
    assert ctx.content.strip() == "hello project"
    assert "Project context" in ctx.format_for_prompt()
    assert "hello project" in ctx.format_for_prompt()


def test_load_truncates_oversize(tmp_path: Path) -> None:
    (tmp_path / "EVI.md").write_text("x" * (200 * 1024), encoding="utf-8")
    ctx = load_project_context(start=tmp_path)
    assert ctx is not None
    assert len(ctx.content) <= 64 * 1024


def test_load_skips_binary(tmp_path: Path) -> None:
    (tmp_path / "EVI.md").write_bytes(b"\xff\xfe\xfd not utf-8")
    assert load_project_context(start=tmp_path) is None


# --- project config overlay + AGENTS.md (Phase 74) ----------------------


def test_recognizes_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    ctx = load_project_context(start=tmp_path)
    assert ctx is not None and "agent rules" in ctx.content


def test_evi_md_wins_over_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("generic", encoding="utf-8")
    (tmp_path / "EVI.md").write_text("evi-specific", encoding="utf-8")
    assert find_project_file(start=tmp_path).name == "EVI.md"


def test_find_project_config_walks_up(tmp_path: Path) -> None:
    (tmp_path / ".evi.toml").write_text("[llm]\ntemperature = 0.123\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_project_config(start=nested) == tmp_path / ".evi.toml"
    # no .evi.toml anywhere up-tree → None
    assert find_project_config(start=tmp_path.parent / "elsewhere-xyz") is None


def test_load_project_config_overlay(tmp_path: Path) -> None:
    (tmp_path / ".evi.toml").write_text(
        "[llm]\ntemperature = 0.123\nmodel = 'proj-model'\n", encoding="utf-8"
    )
    overlay = load_project_config_overlay(start=tmp_path)
    assert overlay["llm"]["temperature"] == 0.123
    assert overlay["llm"]["model"] == "proj-model"
    # walked up from a nested dir → still finds the ancestor's .evi.toml
    nested = tmp_path / "deep" / "x"
    nested.mkdir(parents=True)
    assert load_project_config_overlay(start=nested)["llm"]["temperature"] == 0.123


def test_config_load_applies_project_overlay(tmp_path: Path, monkeypatch) -> None:
    from evi.config import Config

    (tmp_path / ".evi.toml").write_text("[llm]\ntemperature = 0.123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    assert cfg.llm.temperature == 0.123  # project overlay wins
