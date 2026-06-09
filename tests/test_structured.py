"""Tests for Structured Outputs (JSON schema response_format)."""

from __future__ import annotations

import json

import pytest

from evi import structured


def test_load_schema_inline():
    s = structured.load_schema('{"type": "object", "properties": {"x": {"type": "number"}}}')
    assert s["type"] == "object" and "x" in s["properties"]


def test_load_schema_from_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    assert structured.load_schema(str(p)) == {"type": "object"}


def test_load_schema_missing_file():
    with pytest.raises(structured.SchemaError):
        structured.load_schema("does-not-exist.json")


def test_load_schema_bad_json():
    with pytest.raises(structured.SchemaError):
        structured.load_schema("{not valid")


def test_load_schema_non_object():
    with pytest.raises(structured.SchemaError):
        structured.load_schema("[1, 2, 3]")


def test_as_response_format_wraps_bare_schema():
    rf = structured.as_response_format({"type": "object"}, name="extract")
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "extract"
    assert rf["json_schema"]["schema"] == {"type": "object"}
    assert rf["json_schema"]["strict"] is True


def test_as_response_format_respects_full_wrapper():
    full = {"type": "json_schema", "json_schema": {"name": "n", "schema": {}}}
    assert structured.as_response_format(full) is full


def test_as_response_format_respects_name_schema_pair():
    rf = structured.as_response_format({"name": "n", "schema": {"type": "object"}})
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "n"


def test_headless_forwards_response_format():
    # run_headless should pass response_format into agent.chat.
    from evi.headless import run_headless
    from evi.llm.agent import Done, TextDelta

    seen = {}

    class _FakeAgent:
        def chat(self, prompt, **kw):
            seen.update(kw)
            yield TextDelta('{"x": 1}')
            yield Done(reason="stop")

    rf = structured.as_response_format({"type": "object"})
    res = run_headless(_FakeAgent(), "extract", response_format=rf)
    assert res.text == '{"x": 1}'
    assert seen["response_format"] == rf
