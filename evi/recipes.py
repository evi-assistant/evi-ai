"""Saved multi-turn workflows — "recipes".

A recipe is an ordered list of prompts run through one agent in a single shared
conversation, so later steps can build on earlier ones (e.g. a "morning
standup" that pulls your calendar, then your commits, then drafts a summary of
both). Recipes live in ``~/.evi/recipes/*.toml``:

    name = "morning-standup"
    description = "Calendar + commits, then a summary"

    [[steps]]
    label = "Calendar"
    prompt = "What's on my calendar today?"

    [[steps]]
    label = "Commits"
    prompt = "List my git commits from yesterday in this repo."

    [[steps]]
    prompt = "Write a 3-bullet standup from the two answers above."

This module loads/lists/creates recipes (pure + testable). Running them through
the agent lives in the CLI, which owns the agent wiring. All functions take an
optional ``root`` so tests can use a temp home.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

import evi.config as config


class RecipeError(Exception):
    """A recipe is missing or malformed."""


@dataclass
class Step:
    prompt: str
    label: str = ""


@dataclass
class Recipe:
    name: str
    description: str
    steps: list[Step] = field(default_factory=list)
    path: Path | None = None


def recipes_dir(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "recipes"


def _slug(name: str) -> str:
    """Filesystem-safe recipe name (also blocks path traversal)."""
    return Path(name).name.removesuffix(".toml")


def load_recipe_file(path: Path) -> Recipe:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RecipeError(f"could not read {path.name}: {exc}") from exc

    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RecipeError(f"{path.name}: a recipe needs at least one [[steps]] entry")
    steps: list[Step] = []
    for i, s in enumerate(raw_steps, 1):
        if not isinstance(s, dict) or not str(s.get("prompt", "")).strip():
            raise RecipeError(f"{path.name}: step {i} is missing a non-empty `prompt`")
        steps.append(Step(prompt=str(s["prompt"]).strip(), label=str(s.get("label", "")).strip()))

    return Recipe(
        name=str(data.get("name") or path.stem).strip(),
        description=str(data.get("description", "")).strip(),
        steps=steps,
        path=path,
    )


def load_recipe(name: str, root: Path | None = None) -> Recipe:
    path = recipes_dir(root) / f"{_slug(name)}.toml"
    if not path.is_file():
        raise RecipeError(f"no recipe named {_slug(name)!r} (looked in {recipes_dir(root)})")
    return load_recipe_file(path)


def list_recipes(root: Path | None = None) -> list[Recipe]:
    d = recipes_dir(root)
    out: list[Recipe] = []
    if d.is_dir():
        for p in sorted(d.glob("*.toml")):
            try:
                out.append(load_recipe_file(p))
            except RecipeError:
                continue  # skip malformed files rather than abort the listing
    return out


_TEMPLATE = '''\
name = "{name}"
description = "What this workflow does"

# Each [[steps]] is one turn, run in order through a shared conversation, so a
# later step can refer to earlier answers. `label` is optional (shown when run).

[[steps]]
label = "First step"
prompt = "Ask eVi to do the first thing."

[[steps]]
prompt = "Now build on the previous answer."
'''


def run_recipe_headless(agent, recipe: "Recipe") -> list[dict]:
    """Run a recipe's steps through `agent` non-interactively (shared
    conversation), returning [{label, prompt, text, error}, …]. Used by the
    routine webhook + `evi routine run`."""
    from evi.headless import run_headless

    results: list[dict] = []
    for step in recipe.steps:
        res = run_headless(agent, step.prompt)
        results.append(
            {"label": step.label, "prompt": step.prompt, "text": res.text, "error": res.error}
        )
    return results


def create_recipe(name: str, root: Path | None = None, overwrite: bool = False) -> Path:
    """Write a starter recipe template and return its path."""
    d = recipes_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)
    path = d / f"{slug}.toml"
    if path.exists() and not overwrite:
        raise RecipeError(f"recipe {slug!r} already exists (pass --overwrite to replace)")
    path.write_text(_TEMPLATE.format(name=slug), encoding="utf-8")
    return path
