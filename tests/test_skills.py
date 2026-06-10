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


# ---- load() + resource-aware invoke_skill --------------------------------


def test_load_returns_body_dir_and_resources(store: SkillStore, tmp_path: Path) -> None:
    skill_dir = _make_skill(tmp_path, "pdf", "Fill PDFs.", "See reference.md")
    (skill_dir / "reference.md").write_text("details", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "fill.py").write_text("print(1)", encoding="utf-8")
    body, sdir, resources = store.load("pdf")
    assert body == "See reference.md"
    assert sdir == skill_dir
    names = {p.name for p in resources}
    assert names == {"reference.md", "fill.py"}  # SKILL.md excluded
    assert all(p.is_absolute() for p in resources)


def test_invoke_skill_lists_bundled_files(store: SkillStore, tmp_path: Path) -> None:
    skill_dir = _make_skill(tmp_path, "pdf", "Fill PDFs.", "Body here")
    (skill_dir / "reference.md").write_text("x", encoding="utf-8")
    out = REGISTRY["invoke_skill"].call(json.dumps({"name": "pdf"}))
    assert "Body here" in out
    assert "Bundled files" in out
    assert str(skill_dir / "reference.md") in out


def test_invoke_skill_no_resources_is_plain_body(store: SkillStore, tmp_path: Path) -> None:
    _make_skill(tmp_path, "plain", "No extras.", "Just the body")
    out = REGISTRY["invoke_skill"].call(json.dumps({"name": "plain"}))
    assert out == "Just the body"
    assert "Bundled files" not in out


# ---- import_skill (load Claude-style skills) ------------------------------


def test_import_skill_from_dir(tmp_path: Path) -> None:
    from evi import skills

    src = _make_skill(tmp_path, "code-review", "Review code.", "body")
    dest_root = tmp_path / "skills"
    name = skills.import_skill(str(src), root=dest_root)
    assert name == "code-review"
    assert (dest_root / "code-review" / "SKILL.md").is_file()


def test_import_skill_from_skill_md_path(tmp_path: Path) -> None:
    from evi import skills

    src = _make_skill(tmp_path, "x", "d", "b")
    dest_root = tmp_path / "skills"
    name = skills.import_skill(str(src / "SKILL.md"), root=dest_root)
    assert name == "x" and (dest_root / "x" / "SKILL.md").is_file()


def test_import_skill_copies_companion_files(tmp_path: Path) -> None:
    from evi import skills

    src = _make_skill(tmp_path, "pdf", "Fill PDFs.", "see reference.md")
    (src / "reference.md").write_text("details", encoding="utf-8")
    dest_root = tmp_path / "skills"
    skills.import_skill(str(src), root=dest_root)
    assert (dest_root / "pdf" / "reference.md").read_text(encoding="utf-8") == "details"


def test_import_skill_name_override_and_slug(tmp_path: Path) -> None:
    from evi import skills

    src = _make_skill(tmp_path, "orig", "d", "b")
    dest_root = tmp_path / "skills"
    name = skills.import_skill(str(src), name="My Cool Skill", root=dest_root)
    assert name == "My-Cool-Skill"
    assert (dest_root / "My-Cool-Skill").is_dir()


def test_import_skill_rejects_missing_skill_md(tmp_path: Path) -> None:
    from evi import skills

    (tmp_path / "empty").mkdir()
    with pytest.raises(skills.SkillError):
        skills.import_skill(str(tmp_path / "empty"), root=tmp_path / "skills")


def test_import_skill_overwrite(tmp_path: Path) -> None:
    from evi import skills

    src = _make_skill(tmp_path, "dup", "d", "v1")
    dest_root = tmp_path / "skills"
    skills.import_skill(str(src), root=dest_root)
    with pytest.raises(skills.SkillError):
        skills.import_skill(str(src), root=dest_root)  # exists, no overwrite
    (src / "SKILL.md").write_text("---\nname: dup\ndescription: d\n---\nv2", encoding="utf-8")
    skills.import_skill(str(src), root=dest_root, overwrite=True)
    assert "v2" in (dest_root / "dup" / "SKILL.md").read_text(encoding="utf-8")


def test_import_skill_rewrite_paths(tmp_path: Path) -> None:
    from evi import skills

    body = "First read reference.md then run scripts/fill.py to finish."
    src = _make_skill(tmp_path, "pdf", "Fill PDFs.", body)
    (src / "reference.md").write_text("x", encoding="utf-8")
    (src / "scripts").mkdir()
    (src / "scripts" / "fill.py").write_text("y", encoding="utf-8")
    dest_root = tmp_path / "skills"
    skills.import_skill(str(src), root=dest_root, rewrite_paths=True)
    out = (dest_root / "pdf" / "SKILL.md").read_text(encoding="utf-8")
    assert str((dest_root / "pdf" / "reference.md")).replace("\\", "/") in out
    assert str((dest_root / "pdf" / "scripts" / "fill.py")).replace("\\", "/") in out
    assert " reference.md " not in out  # the bare relative ref was replaced
