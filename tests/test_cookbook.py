"""The cookbook must stay buildable and internally consistent.

Recipes are published to evi-ai.dev/cookbook and the wiki by the docs-publish
workflow, which runs scripts/build-cookbook.py. That generator verifies its own
output (structure, links resolve, no unrewritten markdown links, every recipe
linked from the index) and exits non-zero on any failure. This test runs it so a
broken recipe fails the unit suite too, not only the publish job — the same
guard that already caught a `--url` vs `--base-url` slip while these were written.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COOKBOOK = ROOT / "cookbook"
GENERATOR = ROOT / "scripts" / "build-cookbook.py"


def test_cookbook_dir_exists() -> None:
    assert (COOKBOOK / "README.md").is_file(), "cookbook/README.md (the index) is missing"
    assert list(COOKBOOK.glob("*.md")), "no recipes found under cookbook/"


def test_cookbook_builds_and_verifies(tmp_path: Path) -> None:
    # A stub that satisfies the generator's "is this the site repo?" check; the
    # real output structure is written here and verified.
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (ROOT / "scripts" / "site-docs.css").exists()  # generator copies this in

    r = subprocess.run(
        [sys.executable, str(GENERATOR), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert r.returncode == 0, (
        f"cookbook build/verify failed:\n{r.stdout}\n{r.stderr}"
    )
    out = tmp_path / "cookbook"
    assert (out / "index.html").is_file()
    recipes = [p for p in COOKBOOK.glob("*.md") if p.name != "README.md"]
    for recipe in recipes:
        assert (out / f"{recipe.stem}.html").is_file(), f"{recipe.stem} did not render"


@pytest.mark.parametrize(
    "recipe", sorted(p for p in COOKBOOK.glob("*.md") if p.name != "README.md")
)
def test_recipe_has_title_and_uses_line(recipe: Path) -> None:
    """Every recipe leads with an H1 and declares which surface it uses, so the
    index cards and the 'Uses:' contract in the README stay honest."""
    text = recipe.read_text(encoding="utf-8")
    assert text.lstrip().startswith("# "), f"{recipe.name}: no H1 title on the first line"
    assert "**Uses:**" in text, f"{recipe.name}: missing a '**Uses:**' line"
