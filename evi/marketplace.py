"""Plugin marketplace — a searchable index of installable plugins.

Phase 68 added `evi plugin add <dir|git-url>`. This adds a *name → source*
index so you can `evi plugin search` and `evi plugin install <name>` without
pasting URLs. The index is plain JSON:

    {
      "plugins": [
        {
          "name": "git-helpers",
          "source": "https://github.com/you/evi-git-helpers.git",
          "description": "Handy git slash commands",
          "author": "you",
          "tags": ["git", "vcs"]
        }
      ]
    }

The local index lives at ``~/.evi/marketplace.json``; extra remote indexes can
be listed in ``[plugins] index_urls`` and are merged in (local entries win on a
name clash). Remote fetches are best-effort — a flaky URL never breaks search.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evi.config import MARKETPLACE_PATH


class MarketplaceError(Exception):
    """The index is missing or malformed, or a name can't be resolved."""


@dataclass
class MarketplaceEntry:
    name: str
    source: str
    description: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)


def _parse_entries(data: Any) -> list[MarketplaceEntry]:
    items = data.get("plugins", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[MarketplaceEntry] = []
    for e in items:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        source = str(e.get("source") or "").strip()
        if not name or not source:
            continue
        tags = e.get("tags") or []
        out.append(
            MarketplaceEntry(
                name=name,
                source=source,
                description=str(e.get("description", "")).strip(),
                author=str(e.get("author", "")).strip(),
                tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            )
        )
    return out


def _fetch_remote(url: str) -> list[MarketplaceEntry]:
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (user-configured)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return _parse_entries(data)
    except Exception:
        return []  # best-effort — a bad index URL must not break search


def load_index(
    path: Path | None = None, *, index_urls: list[str] | None = None
) -> list[MarketplaceEntry]:
    """Load the local index plus any remote ``index_urls``. Local entries win
    on a name clash; result is sorted by name. Never raises."""
    by_name: dict[str, MarketplaceEntry] = {}

    p = path or MARKETPLACE_PATH
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        for e in _parse_entries(data or {}):
            by_name.setdefault(e.name, e)

    for url in index_urls or []:
        for e in _fetch_remote(url):
            by_name.setdefault(e.name, e)

    return sorted(by_name.values(), key=lambda e: e.name.lower())


def search(query: str, entries: list[MarketplaceEntry]) -> list[MarketplaceEntry]:
    """Case-insensitive match over name / description / tags. Empty query → all."""
    q = query.strip().lower()
    if not q:
        return entries
    out = []
    for e in entries:
        hay = " ".join([e.name, e.description, " ".join(e.tags)]).lower()
        if q in hay:
            out.append(e)
    return out


def resolve(name: str, entries: list[MarketplaceEntry]) -> MarketplaceEntry | None:
    """Exact-name lookup (case-insensitive)."""
    n = name.strip().lower()
    for e in entries:
        if e.name.lower() == n:
            return e
    return None


def create_index(path: Path | None = None, overwrite: bool = False) -> Path:
    """Write a starter local index and return its path."""
    p = path or MARKETPLACE_PATH
    if p.exists() and not overwrite:
        raise MarketplaceError(f"{p} already exists (pass overwrite=True to replace)")
    p.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "plugins": [
            {
                "name": "example-plugin",
                "source": "https://github.com/you/evi-example-plugin.git",
                "description": "What this plugin does",
                "author": "you",
                "tags": ["example"],
            }
        ]
    }
    p.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
    return p


def add_entry(entry: MarketplaceEntry, path: Path | None = None) -> None:
    """Add (or replace by name) an entry in the local index, creating it if needed."""
    p = path or MARKETPLACE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = [e for e in load_index(p) if e.name.lower() != entry.name.lower()]
    existing.append(entry)
    payload = {
        "plugins": [
            {
                "name": e.name,
                "source": e.source,
                "description": e.description,
                "author": e.author,
                "tags": e.tags,
            }
            for e in sorted(existing, key=lambda e: e.name.lower())
        ]
    }
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
