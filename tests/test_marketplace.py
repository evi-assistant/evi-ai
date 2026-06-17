"""Tests for the plugin marketplace index (lighter/later item)."""

from __future__ import annotations

import json

from evi import marketplace
from evi.marketplace import MarketplaceEntry


def _write_index(path, plugins):
    path.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")


def test_load_index(tmp_path):
    p = tmp_path / "marketplace.json"
    _write_index(p, [
        {"name": "gitx", "source": "https://x/gitx.git", "description": "git", "tags": ["git"]},
        {"name": "notes", "source": "./notes"},
        {"name": "bad-no-source"},  # dropped
    ])
    entries = marketplace.load_index(p)
    assert [e.name for e in entries] == ["gitx", "notes"]  # sorted, bad dropped
    assert entries[0].tags == ["git"]


def test_load_index_missing_is_empty(tmp_path):
    assert marketplace.load_index(tmp_path / "nope.json") == []


def test_load_index_merges_remote_local_wins(tmp_path, monkeypatch):
    p = tmp_path / "marketplace.json"
    _write_index(p, [{"name": "dup", "source": "local-src"}])
    monkeypatch.setattr(
        marketplace,
        "_fetch_remote",
        lambda url, key="plugins": [
            MarketplaceEntry(name="dup", source="remote-src"),
            MarketplaceEntry(name="extra", source="remote-extra"),
        ],
    )
    entries = marketplace.load_index(p, index_urls=["http://example/index.json"])
    by = {e.name: e.source for e in entries}
    assert by["dup"] == "local-src"  # local wins
    assert by["extra"] == "remote-extra"


def test_search():
    entries = [
        MarketplaceEntry(name="gitx", source="s", description="git helpers", tags=["vcs"]),
        MarketplaceEntry(name="notes", source="s", description="note taking"),
    ]
    assert [e.name for e in marketplace.search("git", entries)] == ["gitx"]
    assert [e.name for e in marketplace.search("vcs", entries)] == ["gitx"]
    assert len(marketplace.search("", entries)) == 2  # empty = all


def test_resolve():
    entries = [MarketplaceEntry(name="GitX", source="s")]
    assert marketplace.resolve("gitx", entries).source == "s"  # case-insensitive
    assert marketplace.resolve("missing", entries) is None


def test_create_and_add_entry(tmp_path):
    p = tmp_path / "marketplace.json"
    marketplace.create_index(p)
    assert p.is_file()
    marketplace.add_entry(MarketplaceEntry(name="zeta", source="z-src", tags=["t"]), p)
    names = [e.name for e in marketplace.load_index(p)]
    assert "zeta" in names
    # add_entry replaces by name (no dup)
    marketplace.add_entry(MarketplaceEntry(name="zeta", source="z2"), p)
    entries = marketplace.load_index(p)
    zetas = [e for e in entries if e.name == "zeta"]
    assert len(zetas) == 1 and zetas[0].source == "z2"


def test_create_index_no_overwrite(tmp_path):
    import pytest

    p = tmp_path / "marketplace.json"
    marketplace.create_index(p)
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.create_index(p)
