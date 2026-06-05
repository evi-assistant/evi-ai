"""Tests for SkillStore and the skill tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evi.tools.skills as skill_tools
from evi.skills import SkillStore, _split_frontmatter
from evi.tools.base import REGISTRY


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillStore:
    s = SkillStore(root=tmp_path)
    monkeypatch.setattr(skill_tools, "_store", s)
    return s


def _make_skill(root: Path, name: str, description: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir()
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def test_split_frontmatter_happy() -> None:
    text = "---\nname: x\ndescription: y\n---\n\n# body"
    meta, body = _split_frontmatter(text)
    assert meta == {"name": "x", "description": "y"}
    assert body.strip() == "# body"


def test_split_frontmatter_quoted_values() -> None:
    text = '---\nname: "x"\ndescription: \'y\'\n---\nbody'
    meta, _ = _split_frontmatter(text)
    assert meta["name"] == "x"
    assert meta["description"] == "y"


def test_split_frontmatter_no_header() -> None:
    text = "# Just a body, no frontmatter."
    meta, body = _split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_unterminated() -> None:
    text = "---\nname: x\nno closer"
    meta, body = _split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_list_returns_entries(store: SkillStore, tmp_path: Path) -> None:
    _make_skill(tmp_path, "code-review", "Review code for bugs.", "## Instructions")
    _make_skill(tmp_path, "summarize", "Summarize text.", "## How to summarize")
    entries = store.list()
    names = {e.name for e in entries}
    assert names == {"code-review", "summarize"}
    by_name = {e.name: e.description for e in entries}
    assert by_name["code-review"] == "Review code for bugs."


def test_list_skips_dirs_without_skill_md(store: SkillStore, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    _make_skill(tmp_path, "real", "real one", "body")
    assert [e.name for e in store.list()] == ["real"]


def test_list_skips_invalid_names(store: SkillStore, tmp_path: Path) -> None:
    # Directory name has a space — invalid as a skill name.
    bad = tmp_path / "bad name"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\ndescription: nope\n---\nbody")
    assert store.list() == []


def test_read_strips_frontmatter(store: SkillStore, tmp_path: Path) -> None:
    _make_skill(tmp_path, "foo", "desc", "## Body\nhello")
    body = store.read("foo")
    assert "---" not in body
    assert "Body" in body
    assert "hello" in body


def test_read_missing_raises(store: SkillStore) -> None:
    with pytest.raises(KeyError):
        store.read("nope")


def test_format_for_prompt_empty(store: SkillStore) -> None:
    assert store.format_for_prompt() == ""


def test_format_for_prompt_lists_entries(store: SkillStore, tmp_path: Path) -> None:
    _make_skill(tmp_path, "alpha", "first", "body")
    out = store.format_for_prompt()
    assert "Available skills" in out
    assert "alpha" in out
    assert "invoke_skill" in out


def test_tool_wrappers(store: SkillStore, tmp_path: Path) -> None:
    _make_skill(tmp_path, "code-review", "Review", "## How\nstep 1")

    listed = json.loads(REGISTRY["list_skills"].call("{}"))
    assert any(e["name"] == "code-review" for e in listed)

    body = REGISTRY["invoke_skill"].call(json.dumps({"name": "code-review"}))
    assert "step 1" in body

    missing = REGISTRY["invoke_skill"].call(json.dumps({"name": "nope"}))
    assert missing.startswith("ERROR:")
