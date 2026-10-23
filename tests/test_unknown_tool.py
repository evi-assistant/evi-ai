from evi.llm.agent import Agent
from evi.tools.base import REGISTRY
from evi.config import Config
from tests.conftest import llm_call, llm_text
from evi.tools.repair import unknown_tool_message

import evi.tools.fs  # noqa: F401  registers read_file and list_dir

def test_unknown_tool_message():
    # AC 1: Close match
    msg = unknown_tool_message('read_fle', ['read_file', 'list_dir', 'write_file'])
    assert msg.startswith("ERROR: unknown tool 'read_fle'")
    assert 'Did you mean: read_file' in msg

    # AC 2: search_tools
    msg = unknown_tool_message('something_weird', ['search_tools', 'list_dir'])
    assert 'Use search_tools to find a tool by what it does.' in msg

    # AC 3: List tools (small)
    msg = unknown_tool_message('something_weird', ['tool1', 'tool2'])
    assert 'The tools are: tool1, tool2' in msg

    # AC 3: List tools (large)
    tools = [f'tool{i}' for i in range(50)]
    msg = unknown_tool_message('something_weird', tools)
    assert 'The tools are:' in msg
    assert ', ...' in msg
    # Check that it doesn't list all 50
    # The message contains "unknown tool", "The tools are", and then up to 40 tool names.
    # Total occurrences of 'tool' should be 2 + 40 = 42.
    assert msg.count('tool') <= 42

def test_agent_unknown_tool_repair(fake_llm):
    # AC 4: Agent run with unknown tool
    # Setup: Agent with read_file and list_dir. Model calls 'read_fle'.
    agent = Agent(
        client=fake_llm([
            llm_call(('read_fle', {'path': 'x'})),
            llm_text('ok')
        ]),
        config=Config(),
        transcripts=None,
        unattended_policy='allow',
        tools=[REGISTRY[n] for n in ('read_file', 'list_dir')]
    )

    results = list(agent.chat('go', max_turns=4))

    # Find the ToolResult for 'read_fle'
    from evi.llm.agent import ToolResult
    tool_result = next((r for r in results if isinstance(r, ToolResult) and r.name == 'read_fle'), None)
    assert tool_result is not None
    assert "unknown tool 'read_fle'. Did you mean: read_file" in tool_result.output
