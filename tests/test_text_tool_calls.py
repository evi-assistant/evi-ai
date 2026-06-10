"""Recovery of tool calls that local models emit as TEXT, not structured.

Some local models (e.g. qwen2.5 via Ollama) print a `{"name": …, "arguments":
…}` JSON object as assistant content instead of returning it in the structured
`tool_calls` field. `recover_text_tool_calls` salvages those so eVi can still
act on them — the key enabler for self-development with local coder models.
"""

from __future__ import annotations

import json

from evi.llm.agent import _find_json_blobs, recover_text_tool_calls

KNOWN = {"edit", "read_file", "write_file"}


def test_recovers_fenced_json_block():
    text = (
        "```json\n"
        '{"name": "edit", "arguments": {"path": "a.py", '
        '"old_string": "x", "new_string": "y"}}\n'
        "```"
    )
    calls = recover_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0]["name"] == "edit"
    args = json.loads(calls[0]["arguments"])  # arguments is a JSON string
    assert args == {"path": "a.py", "old_string": "x", "new_string": "y"}


def test_recovers_bare_object():
    text = '{"name": "read_file", "arguments": {"path": "x"}}'
    calls = recover_text_tool_calls(text, KNOWN)
    assert calls and calls[0]["name"] == "read_file"


def test_recovers_openai_function_shape():
    text = '{"function": {"name": "edit", "arguments": {"path": "z"}}}'
    calls = recover_text_tool_calls(text, KNOWN)
    assert calls and calls[0]["name"] == "edit"


def test_arguments_already_a_string_passthrough():
    text = '{"name": "edit", "arguments": "{\\"path\\": \\"a\\"}"}'
    calls = recover_text_tool_calls(text, KNOWN)
    assert calls and json.loads(calls[0]["arguments"]) == {"path": "a"}


def test_list_of_calls():
    text = (
        '[{"name": "read_file", "arguments": {"path": "a"}}, '
        '{"name": "read_file", "arguments": {"path": "b"}}]'
    )
    calls = recover_text_tool_calls(text, KNOWN)
    assert [c["name"] for c in calls] == ["read_file", "read_file"]


def test_unknown_tool_name_ignored():
    text = '{"name": "rm_rf_everything", "arguments": {}}'
    assert recover_text_tool_calls(text, KNOWN) == []


def test_plain_prose_returns_nothing():
    assert recover_text_tool_calls("Here is how the edit tool works.", KNOWN) == []
    assert recover_text_tool_calls("", KNOWN) == []


def test_braces_inside_string_values_dont_break_scan():
    text = '{"name": "edit", "arguments": {"new_string": "a { nested } brace"}}'
    calls = recover_text_tool_calls(text, KNOWN)
    assert calls and json.loads(calls[0]["arguments"])["new_string"] == "a { nested } brace"


def test_recovers_single_quoted_python_hybrid():
    # Observed local-model output: double-quoted keys, single-quoted values
    # (because a value contains a double quote), and lowercase `false`.
    text = (
        "{\n"
        '  "name": "edit_file",\n'
        '  "arguments": {\n'
        "    \"path\": \"recommend.py\",\n"
        "    'old_string': 'a < \"6.0\"',\n"
        "    'new_string': 'float(a) < 6.0',\n"
        "    \"replace_all\": false\n"
        "  }\n"
        "}"
    )
    calls = recover_text_tool_calls(text, {"edit_file"})
    assert len(calls) == 1 and calls[0]["name"] == "edit_file"
    args = json.loads(calls[0]["arguments"])
    assert args["old_string"] == 'a < "6.0"'
    assert args["new_string"] == "float(a) < 6.0"


def test_find_json_blobs_finds_multiple():
    blobs = _find_json_blobs('noise {"a": 1} more [1, 2] tail')
    assert blobs == ['{"a": 1}', "[1, 2]"]
