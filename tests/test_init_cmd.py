"""Tests for `evi init` (project-context scaffolder)."""

from __future__ import annotations

import pytest
import typer

from evi.apps.cli.main import init
from evi.project import find_project_file


def test_init_creates_and_is_discovered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init(name="AGENTS.md", force=False)
    f = tmp_path / "AGENTS.md"
    assert f.is_file()
    assert "project context" in f.read_text(encoding="utf-8").lower()
    # project.py discovers it as project context
    assert find_project_file(tmp_path) == f


def test_init_refuses_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("existing", encoding="utf-8")
    with pytest.raises(typer.Exit):
        init(name="AGENTS.md", force=False)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "existing"


def test_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "EVI.md").write_text("old", encoding="utf-8")
    init(name="EVI.md", force=True)
    assert "project context" in (tmp_path / "EVI.md").read_text(encoding="utf-8").lower()
