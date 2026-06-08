"""Routines — trigger a recipe from a webhook.

A routine binds a saved [recipe](recipes.py) to an unguessable token. Hitting
``POST /api/routine/<token>`` runs that recipe headless — so an external service
(cron box, IFTTT, a GitHub Action, a home-automation hub) can kick off an eVi
workflow over HTTP.

Stored as ``~/.evi/routines.json``. The token is the capability — keep it
secret; the endpoint bypasses the web auth token (external callers don't have
it) and instead validates the path token. By default a routine runs with the
**restricted** permission policy (only your auto-approved tool categories;
everything else is denied, never prompted) — set ``yes`` to auto-approve all
tools for that routine if you trust it.

Functions take an optional ``root`` (eVi home) for tests.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

import evi.config as config


class RoutineError(Exception):
    pass


@dataclass
class Routine:
    name: str
    recipe: str
    token: str
    enabled: bool = True
    yes: bool = False  # auto-approve all tools for this routine


def _path(root: Path | None = None) -> Path:
    return (root if root is not None else config.HOME) / "routines.json"


def load(root: Path | None = None) -> list[Routine]:
    p = _path(root)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Routine] = []
    for d in data if isinstance(data, list) else []:
        try:
            out.append(Routine(
                name=str(d["name"]), recipe=str(d["recipe"]), token=str(d["token"]),
                enabled=bool(d.get("enabled", True)), yes=bool(d.get("yes", False)),
            ))
        except (KeyError, TypeError):
            continue
    return out


def save(routines: list[Routine], root: Path | None = None) -> None:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(r) for r in routines], indent=2), encoding="utf-8")


def get(name: str, root: Path | None = None) -> Routine | None:
    return next((r for r in load(root) if r.name == name), None)


def get_by_token(token: str, root: Path | None = None) -> Routine | None:
    if not token:
        return None
    return next((r for r in load(root) if secrets.compare_digest(r.token, token)), None)


def add(
    name: str,
    recipe: str,
    *,
    yes: bool = False,
    root: Path | None = None,
    overwrite: bool = False,
) -> Routine:
    routines = load(root)
    if any(r.name == name for r in routines):
        if not overwrite:
            raise RoutineError(f"routine {name!r} already exists (pass --overwrite)")
        routines = [r for r in routines if r.name != name]
    routine = Routine(name=name, recipe=recipe, token=secrets.token_urlsafe(18))
    routine.yes = yes
    routines.append(routine)
    save(routines, root)
    return routine


def remove(name: str, root: Path | None = None) -> bool:
    routines = load(root)
    kept = [r for r in routines if r.name != name]
    if len(kept) == len(routines):
        return False
    save(kept, root)
    return True
