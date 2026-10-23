import json

import evi.tools.fs  # noqa: F401  registers read_file and list_dir
from evi.config import Config
from evi.llm.agent import Agent, TextDelta, ToolResult, recover_text_tool_calls
from evi.tools.base import REGISTRY
from tests.conftest import llm_text


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


def test_agent_tagged_calls(tmp_path, fake_llm):
    marker = tmp_path / "marker.txt"
    marker.write_text("found it")

    agent = Agent(
        client=fake_llm([]),
        config=Config(),
        transcripts=None,
        unattended_policy="allow",
        tools=[REGISTRY[n] for n in ("read_file", "list_dir")],
    )
    agent.cwd = str(tmp_path)

    # 1. Valid <tool_call> list_dir runs and no TextDelta shows the tag
    client = fake_llm([
        llm_text('<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>'),
        llm_text("done"),
    ])
    agent.client = client
    events = list(agent.chat("go", max_turns=4))

    tool_results = [e for e in events if isinstance(e, ToolResult)]
    assert len(tool_results) == 1
    assert "marker.txt" in tool_results[0].output

    text_deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert not any("<tool_call>" in t for t in text_deltas)

    # 2. Example call in prose runs nothing
    client = fake_llm([
        llm_text('You could call <tool_call>{"name": "list_dir", "arguments": {}}</tool_call> yourself.'),
    ])
    agent.client = client
    events = list(agent.chat("go", max_turns=4))
    assert not any(isinstance(e, ToolResult) for e in events)

    # 3. Markup arrives as text
    client = fake_llm([
        llm_text("<b>bold</b> answer"),
    ])
    agent.client = client
    events = list(agent.chat("go", max_turns=4))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "<b>bold</b> answer"
