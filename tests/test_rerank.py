"""Tests for evi/tools/rerank.py — the cross-encoder is mocked everywhere.

We don't actually pull `sentence-transformers` in CI; the lazy-load
function is patched so we exercise the wiring (input parsing, sort
order, citation emission) against a deterministic fake scorer.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from evi.citations import ToolOutput
from evi.tools.base import REGISTRY

import evi.tools.rerank  # noqa: F401 — register


def _patch_encoder(score_map: dict[str, float]):
    """Return a CrossEncoder-like mock whose `predict` consults `score_map`."""
    encoder = MagicMock()

    def predict(pairs):
        return [score_map.get(p[1], 0.0) for p in pairs]
    encoder.predict.side_effect = predict
    return encoder


# ----- happy paths ---------------------------------------------------------


def test_rerank_sorts_descending_by_score() -> None:
    encoder = _patch_encoder({"foo": 0.2, "bar": 0.9, "baz": 0.5})
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "anything",
            "candidates": ["foo", "bar", "baz"],
            "top_k": 3,
        }))
    payload = json.loads(out.text)
    assert [p["text"] for p in payload] == ["bar", "baz", "foo"]
    assert payload[0]["score"] == 0.9


def test_rerank_top_k_caps_output() -> None:
    encoder = _patch_encoder({"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4})
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": ["a", "b", "c", "d"],
            "top_k": 2,
        }))
    payload = json.loads(out.text)
    assert len(payload) == 2
    assert [p["text"] for p in payload] == ["d", "c"]


def test_rerank_accepts_dict_candidates_with_path() -> None:
    """When passed `find_in_project`-style dicts, path + lines survive."""
    candidates = [
        {"text": "first chunk", "path": "foo.py", "lines": "10-20"},
        {"text": "second chunk", "path": "bar.py", "lines": "5-15"},
    ]
    encoder = _patch_encoder({"first chunk": 0.4, "second chunk": 0.8})
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": candidates,
            "top_k": 2,
        }))
    payload = json.loads(out.text)
    assert payload[0]["path"] == "bar.py"
    assert payload[1]["path"] == "foo.py"


def test_rerank_emits_citations_per_result() -> None:
    encoder = _patch_encoder({"alpha": 0.6, "beta": 0.3})
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": [
                {"text": "alpha", "path": "a.py", "lines": ""},
                {"text": "beta", "path": "b.py", "lines": ""},
            ],
            "top_k": 2,
        }))
    assert isinstance(out, ToolOutput)
    assert len(out.citations) == 2
    assert out.citations[0].id == "1"
    assert out.citations[0].source_id == "a.py"
    assert out.citations[1].source_id == "b.py"


def test_rerank_accepts_json_string_candidates() -> None:
    """The LLM may serialise the array; we should re-parse."""
    encoder = _patch_encoder({"x": 0.5})
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": json.dumps(["x"]),
            "top_k": 1,
        }))
    payload = json.loads(out.text)
    assert payload[0]["text"] == "x"


# ----- error paths ---------------------------------------------------------


def test_rerank_empty_query_returns_error() -> None:
    out = REGISTRY["rerank"].call_rich(json.dumps({
        "query": "   ",
        "candidates": ["a"],
        "top_k": 1,
    }))
    assert "empty query" in out.text


def test_rerank_no_candidates_returns_error() -> None:
    out = REGISTRY["rerank"].call_rich(json.dumps({
        "query": "q",
        "candidates": [],
        "top_k": 5,
    }))
    assert "no usable candidates" in out.text


def test_rerank_missing_dep_returns_clear_error() -> None:
    """When sentence-transformers isn't installed, the lazy loader raises
    RuntimeError. The tool should surface that as a clean ERROR: line."""
    def boom(name):
        raise RuntimeError("rerank needs sentence-transformers — pip install evi-ai[rerank]")

    with patch("evi.tools.rerank._load_encoder", side_effect=boom):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": ["x"],
            "top_k": 1,
        }))
    assert "sentence-transformers" in out.text
    assert out.text.startswith("ERROR")


def test_rerank_encoder_failure_wrapped() -> None:
    encoder = MagicMock()
    encoder.predict.side_effect = RuntimeError("OOM")
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": ["a"],
            "top_k": 1,
        }))
    assert "cross-encoder failed" in out.text


def test_rerank_filters_empty_strings_in_candidates() -> None:
    encoder = _patch_encoder({"real": 0.9})
    with patch("evi.tools.rerank._load_encoder", return_value=encoder):
        out = REGISTRY["rerank"].call_rich(json.dumps({
            "query": "q",
            "candidates": ["real", "", "   "],
            "top_k": 5,
        }))
    payload = json.loads(out.text)
    assert len(payload) == 1
    assert payload[0]["text"] == "real"
