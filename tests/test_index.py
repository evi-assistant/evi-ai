"""Tests for the semantic-search index. Embeddings stubbed so we don't
need a live backend; this exercises chunking, persistence, and ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

import evi.embeddings as embeddings_mod
import evi.index as index_mod
from evi.config import LLMSettings
from evi.index import ProjectIndex, _chunk_lines


def test_chunk_lines_short_file() -> None:
    chunks = _chunk_lines("a.txt", ["hello", "world"])
    assert len(chunks) == 1
    assert chunks[0].text == "hello\nworld"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2


def test_chunk_lines_splits_over_threshold() -> None:
    # Each line ~100 chars; with the 800-char cap we should see ~3 chunks.
    lines = [("x" * 100) for _ in range(20)]
    chunks = _chunk_lines("big.py", lines)
    assert len(chunks) >= 2
    # Line ranges should cover the whole file.
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 20


def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Return a deterministic vector per text. Records which texts were embedded."""
    calls: dict = {"texts": []}

    def fake_embed(texts: list[str], settings) -> list[list[float]]:
        out = []
        for t in texts:
            calls["texts"].append(t)
            # Three-dim toy embedding: count of "alpha", "beta", "gamma" tokens.
            out.append([
                float(t.lower().count("alpha")),
                float(t.lower().count("beta")),
                float(t.lower().count("gamma")),
            ])
        return out

    # The index module imports embed_texts at module import time; patch the
    # symbol the index module actually uses.
    monkeypatch.setattr(index_mod, "embed_texts", fake_embed)
    monkeypatch.setattr(embeddings_mod, "embed_texts", fake_embed)
    return calls


def test_build_and_query_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("alpha alpha alpha\n")
    (project / "b.py").write_text("beta beta\n")
    (project / "c.py").write_text("gamma gamma gamma gamma\n")

    # Redirect index storage into tmp_path so we don't touch real ~/.evi.
    monkeypatch.setattr(index_mod, "INDICES_DIR", tmp_path / "indices")
    _stub_embeddings(monkeypatch)

    settings = LLMSettings()
    idx = ProjectIndex(project, settings)
    n = idx.build()
    assert n == 3

    # Query semantically — "alpha story" should rank a.py first.
    hits = idx.query("alpha story", k=2)
    assert len(hits) == 2
    assert hits[0].chunk.path == "a.py"


def test_build_skips_non_text_and_hidden_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "real.py").write_text("alpha")
    # Binary / non-listed extension should be ignored.
    (project / "blob.bin").write_bytes(b"\x00\x01\x02")
    # Hidden + skip dirs should be pruned.
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("alpha")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "x.js").write_text("alpha")

    monkeypatch.setattr(index_mod, "INDICES_DIR", tmp_path / "indices")
    calls = _stub_embeddings(monkeypatch)

    settings = LLMSettings()
    n = ProjectIndex(project, settings).build()
    assert n == 1
    # The only embedded text comes from real.py.
    assert all("alpha" in t for t in calls["texts"])
    assert all("node_modules" not in t for t in calls["texts"])


def test_query_missing_index_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_mod, "INDICES_DIR", tmp_path / "indices")
    settings = LLMSettings()
    idx = ProjectIndex(tmp_path / "nonexistent", settings)
    assert idx.query("anything") == []


def test_stats_before_and_after_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.md").write_text("# hello")
    monkeypatch.setattr(index_mod, "INDICES_DIR", tmp_path / "indices")
    _stub_embeddings(monkeypatch)

    idx = ProjectIndex(project, LLMSettings())
    assert idx.stats() == {"indexed": False}
    idx.build()
    s = idx.stats()
    assert s["indexed"] is True
    assert s["chunks"] == 1
