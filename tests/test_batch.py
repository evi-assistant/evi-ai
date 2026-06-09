"""Tests for batch mode (evi batch)."""

from __future__ import annotations

import json

import pytest

from evi import batch


def test_parse_jsonl(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text(
        '{"prompt": "one"}\n\n{"id": "x", "prompt": "two", "mode": "code"}\n',
        encoding="utf-8",
    )
    items = batch.parse_batch_file(p)
    assert [i["prompt"] for i in items] == ["one", "two"]
    assert items[0]["id"] == 0  # auto id
    assert items[1]["id"] == "x" and items[1]["mode"] == "code"


def test_parse_plain_lines(tmp_path):
    p = tmp_path / "in.txt"
    p.write_text("first prompt\n# a comment\n\nsecond prompt\n", encoding="utf-8")
    items = batch.parse_batch_file(p)
    assert [i["prompt"] for i in items] == ["first prompt", "second prompt"]


def test_parse_json_array(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps([{"prompt": "a"}, {"prompt": "b"}]), encoding="utf-8")
    assert len(batch.parse_batch_file(p)) == 2


def test_parse_missing_prompt_errors(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text('{"id": 1}\n', encoding="utf-8")
    with pytest.raises(batch.BatchError):
        batch.parse_batch_file(p)


def test_parse_missing_file():
    with pytest.raises(batch.BatchError):
        batch.parse_batch_file("nope.jsonl")


def test_run_batch_sequential_preserves_order():
    items = [{"id": i, "prompt": str(i)} for i in range(5)]
    out = batch.run_batch(items, lambda it: {"id": it["id"], "text": it["prompt"].upper()})
    assert [r["id"] for r in out] == [0, 1, 2, 3, 4]
    assert out[2]["text"] == "2"


def test_run_batch_parallel_preserves_order():
    items = [{"id": i, "prompt": str(i)} for i in range(8)]
    out = batch.run_batch(items, lambda it: {"id": it["id"]}, parallel=4)
    assert [r["id"] for r in out] == list(range(8))


def test_run_batch_captures_item_errors():
    def boom(it):
        if it["id"] == 1:
            raise RuntimeError("nope")
        return {"id": it["id"], "text": "ok"}

    out = batch.run_batch([{"id": 0}, {"id": 1}, {"id": 2}], boom)
    assert out[0]["text"] == "ok"
    assert "nope" in out[1]["error"]
    assert out[2]["text"] == "ok"


def test_to_jsonl():
    s = batch.to_jsonl([{"a": 1}, {"b": 2}])
    assert s.splitlines() == ['{"a": 1}', '{"b": 2}']
