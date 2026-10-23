import json
from evi.llm.agent import recover_text_tool_calls

def test_recover_text_tool_calls_hermes_qwen25():
    known = {"list_dir", "read_file"}

    # Single call
    text = '<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>'
    assert recover_text_tool_calls(text, known) == [
        {"name": "list_dir", "arguments": json.dumps({"path": "."})}
    ]

    # Multiple calls
    text = (
        '<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>\n'
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    )
    assert recover_text_tool_calls(text, known) == [
        {"name": "list_dir", "arguments": json.dumps({"path": "."})},
        {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
    ]

    # Missing closing tag
    text = '<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}'
    assert recover_text_tool_calls(text, known) == [
        {"name": "list_dir", "arguments": json.dumps({"path": "."})}
    ]

def test_recover_text_tool_calls_qwen3_coder():
    known = {"read_file", "list_dir"}

    # Wrapped in <tool_call>
    text = (
        '<tool_call>\n'
        '<function=read_file>\n'
        '<parameter=path>\nsrc/a.py\n</parameter>\n'
        '<parameter=offset>\n5\n</parameter>\n'
        '</function>\n'
        '</tool_call>'
    )
    # Note: recover_text_tool_calls returns arguments as JSON string
    res = recover_text_tool_calls(text, known)
    assert len(res) == 1
    assert res[0]["name"] == "read_file"
    args = json.loads(res[0]["arguments"])
    assert args == {"path": "src/a.py", "offset": "5"}

    # Bare <function> block
    text = (
        '<function=read_file>\n'
        '<parameter=path>\nsrc/b.py\n</parameter>\n'
        '</function>'
    )
    res = recover_text_tool_calls(text, known)
    assert len(res) == 1
    assert res[0]["name"] == "read_file"
    assert json.loads(res[0]["arguments"]) == {"path": "src/b.py"}

    # Multi-line parameter
    text = (
        '<function=read_file>\n'
        '<parameter=path>\n'
        'line1\n'
        'line2\n'
        '</parameter>\n'
        '</function>'
    )
    res = recover_text_tool_calls(text, known)
    assert json.loads(res[0]["arguments"]) == {"path": "line1\nline2"}

def test_recover_text_tool_calls_constraints():
    known = {"list_dir"}

    # Unknown tool
    text = '<tool_call>\n{"name": "unknown_tool", "arguments": {}}\n</tool_call>'
    assert recover_text_tool_calls(text, known) == []

    # Does not start with tag
    text = 'You could call <tool_call>\n{"name": "list_dir", "arguments": {}}\n</tool_call> yourself.'
    assert recover_text_tool_calls(text, known) == []

    # Starts with other markup
    text = '<b>bold</b> <tool_call>\n{"name": "list_dir", "arguments": {}}\n</tool_call>'
    assert recover_text_tool_calls(text, known) == []
