"""A call to a tool there is none of names the close tools, or how to find
one (evi.tools.repair.unknown_tool_message, used by the agent loop)."""

import evi.tools.fs  # noqa: F401  registers read_file and list_dir
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.tools.base import REGISTRY
from evi.tools.repair import unknown_tool_message
from tests.conftest import llm_call, llm_text


def test_a_close_name_is_suggested():
    msg = unknown_tool_message('read_fle', ['read_file', 'list_dir', 'write_file'])
    assert msg.startswith("ERROR: unknown tool 'read_fle'")
    assert 'Did you mean: read_file' in msg


def test_with_no_close_name_search_tools_is_pointed_at():
    msg = unknown_tool_message('something_weird', ['search_tools', 'list_dir'])
    assert msg.endswith('Use search_tools to find a tool by what it does.')


def test_with_no_close_name_and_no_search_the_tools_are_listed():
    msg = unknown_tool_message('something_weird', ['tool2', 'tool1'])
    assert msg.endswith('The tools are: tool1, tool2')


def test_a_long_tool_list_stops_at_forty():
    msg = unknown_tool_message('something_weird', [f'tool{i:02}' for i in range(50)])
    listed = msg.split('The tools are: ')[1]
    assert listed.endswith(', ...')
    assert len(listed[:-len(', ...')].split(', ')) == 40


def test_with_no_tools_only_the_error_is_given():
    assert unknown_tool_message('x', []) == "ERROR: unknown tool 'x'"


def test_an_agent_run_answers_an_unknown_tool_with_the_close_one(fake_llm):
    agent = Agent(
        client=fake_llm([llm_call(('read_fle', {'path': 'x'})), llm_text('ok')]),
        config=Config(),
        transcripts=None,
        unattended_policy='allow',
        tools=[REGISTRY[n] for n in ('read_file', 'list_dir')],
    )
    [result] = [e for e in agent.chat('go', max_turns=4) if isinstance(e, ToolResult)]
    assert "unknown tool 'read_fle'. Did you mean: read_file" in result.output
