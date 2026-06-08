"""Tests for evi.recipes — loading/listing/creating saved workflows.

Running recipes through the agent is exercised manually + via the CLI; here we
cover the pure parsing/validation logic with a temp recipes root.
"""

from __future__ import annotations

import pytest

from evi import recipes


def test_create_and_load_template(tmp_path):
    path = recipes.create_recipe("morning", root=tmp_path)
    assert path.is_file()
    rec = recipes.load_recipe("morning", root=tmp_path)
    assert rec.name == "morning"
    assert len(rec.steps) >= 2
    assert all(s.prompt for s in rec.steps)


def test_create_no_overwrite(tmp_path):
    recipes.create_recipe("dupe", root=tmp_path)
    with pytest.raises(recipes.RecipeError):
        recipes.create_recipe("dupe", root=tmp_path)
    # overwrite=True is allowed
    recipes.create_recipe("dupe", root=tmp_path, overwrite=True)


def test_list_recipes_sorted(tmp_path):
    recipes.create_recipe("bbb", root=tmp_path)
    recipes.create_recipe("aaa", root=tmp_path)
    names = [r.name for r in recipes.list_recipes(root=tmp_path)]
    assert names == ["aaa", "bbb"]


def test_load_missing_raises(tmp_path):
    with pytest.raises(recipes.RecipeError):
        recipes.load_recipe("nope", root=tmp_path)


def test_parses_steps_and_labels(tmp_path):
    d = recipes.recipes_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "standup.toml").write_text(
        'name = "standup"\n'
        'description = "demo"\n\n'
        '[[steps]]\n'
        'label = "Cal"\n'
        'prompt = "calendar?"\n\n'
        '[[steps]]\n'
        'prompt = "commits?"\n',
        encoding="utf-8",
    )
    rec = recipes.load_recipe("standup", root=tmp_path)
    assert rec.description == "demo"
    assert rec.steps[0].label == "Cal"
    assert rec.steps[0].prompt == "calendar?"
    assert rec.steps[1].label == ""


def test_no_steps_raises(tmp_path):
    d = recipes.recipes_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "empty.toml").write_text('name = "empty"\n', encoding="utf-8")
    with pytest.raises(recipes.RecipeError):
        recipes.load_recipe("empty", root=tmp_path)


def test_step_missing_prompt_raises(tmp_path):
    d = recipes.recipes_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.toml").write_text('[[steps]]\nlabel = "x"\n', encoding="utf-8")
    with pytest.raises(recipes.RecipeError):
        recipes.load_recipe("bad", root=tmp_path)


def test_list_skips_malformed(tmp_path):
    recipes.create_recipe("good", root=tmp_path)
    d = recipes.recipes_dir(tmp_path)
    (d / "broken.toml").write_text("not = valid = toml ==", encoding="utf-8")
    names = [r.name for r in recipes.list_recipes(root=tmp_path)]
    assert names == ["good"]


def test_slug_blocks_traversal(tmp_path):
    # load_recipe sanitises the name to a bare filename.
    recipes.create_recipe("safe", root=tmp_path)
    with pytest.raises(recipes.RecipeError):
        recipes.load_recipe("../../etc/passwd", root=tmp_path)
