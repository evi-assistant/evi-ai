"""Recovery of a tool call a model wrote as its bare arguments, with no name.

gpt-oss:20b (Ollama) once ended a task run by replying with only
``{"patch": "*** Begin Patch..."}``: the arguments of an apply_patch call, as
text. eVi took that as the final answer. Now a reply that is only such an
object becomes a call to the one tool it fits.
"""

from __future__ import annotations

import json

import evi.tools.code  # noqa: F401  registers run_python
import evi.tools.fs  # noqa: F401  registers the file tools
from evi.config import Config
from evi.llm.agent import Agent, TextDelta, ToolResult, recover_text_tool_calls
from evi.tools.base import REGISTRY
from tests.conftest import llm_text

FS = ("read_file", "write_file", "edit_file", "apply_patch", "list_dir", "find_files",
      "search_files")
SCHEMAS = {n: REGISTRY[n].parameters for n in FS}
KNOWN = set(SCHEMAS)
GPT_OSS_REPLY = (
    '{"patch":"*** Begin Patch\\n*** Update File: evi/evals.py\\n@@\\n'
    '     tags: list[str] = field(default_factory=list)\\n'
    '+    compact: bool = False\\n*** End Patch"}'
)


def test_the_gpt_oss_reply_becomes_an_apply_patch_call():
    calls = recover_text_tool_calls(GPT_OSS_REPLY, KNOWN, SCHEMAS)
    assert [c["name"] for c in calls] == ["apply_patch"]
    assert json.loads(calls[0]["arguments"])["patch"].startswith("*** Begin Patch")


def test_a_fenced_nameless_call_is_recovered():
    text = '```json\n{"path": "a.py", "old_string": "x", "new_string": "y"}\n```'
    calls = recover_text_tool_calls(text, KNOWN, SCHEMAS)
    assert [c["name"] for c in calls] == ["edit_file"]
    assert json.loads(calls[0]["arguments"]) == {
        "path": "a.py", "old_string": "x", "new_string": "y"}


def test_the_tool_given_all_its_required_arguments_wins():
    # path + content is all of write_file's required set and part of others'.
    calls = recover_text_tool_calls('{"path": "a.py", "content": "x"}', KNOWN, SCHEMAS)
    assert [c["name"] for c in calls] == ["write_file"]


def test_an_object_several_tools_fit_is_not_recovered():
    # find_files and search_files both require just a pattern.
    assert recover_text_tool_calls('{"pattern": "x"}', KNOWN, SCHEMAS) == []
    # read_file alone requires just a path (list_dir requires nothing, so it
    # never fits); another tool requiring a path makes it ambiguous.
    assert [c["name"] for c in recover_text_tool_calls('{"path": "a"}', KNOWN, SCHEMAS)] \
        == ["read_file"]
    both = {**SCHEMAS, "read_pdf": {"type": "object", "properties": {"path": {}},
                                    "required": ["path"]}}
    assert recover_text_tool_calls('{"path": "a"}', KNOWN | {"read_pdf"}, both) == []


def test_keys_no_tool_declares_stay_text():
    # A JSON answer the user asked for.
    assert recover_text_tool_calls('{"status": "ok", "count": 3}', KNOWN, SCHEMAS) == []
    # One undeclared key is enough to rule a tool out.
    text = '{"path": "a.py", "content": "x", "mood": "happy"}'
    assert recover_text_tool_calls(text, KNOWN, SCHEMAS) == []


def test_only_a_reply_that_is_the_object_alone_counts():
    obj = '{"path": "a.py", "old_string": "x", "new_string": "y"}'
    assert recover_text_tool_calls(obj + "\nThat should do it.", KNOWN, SCHEMAS) == []
    assert recover_text_tool_calls("Try " + obj, KNOWN, SCHEMAS) == []
    assert recover_text_tool_calls(f"[{obj}]", KNOWN, SCHEMAS) == []


def test_without_schemas_nothing_nameless_is_recovered():
    assert recover_text_tool_calls(GPT_OSS_REPLY, KNOWN) == []


def test_a_named_call_still_wins_over_the_schemas():
    text = '{"name": "read_file", "arguments": {"path": "x"}}'
    calls = recover_text_tool_calls(text, KNOWN, SCHEMAS)
    assert [c["name"] for c in calls] == ["read_file"]


def _agent(fake_llm, tmp_path, names, replies):
    agent = Agent(
        client=fake_llm([llm_text(r) for r in replies]),
        config=Config(),
        transcripts=None,
        unattended_policy="allow",
        tools=[REGISTRY[n] for n in names],
    )
    agent.cwd = str(tmp_path)
    return agent


def test_the_agent_runs_a_nameless_edit(tmp_path, fake_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    reply = '{"path": "calc.py", "old_string": "a - b", "new_string": "a + b"}'
    agent = _agent(fake_llm, tmp_path, FS, [reply, "done"])
    events = list(agent.chat("fix add", max_turns=4))
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.name for r in results] == ["edit_file"]
    assert (tmp_path / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    # The JSON is a call, not part of the answer.
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "done"


def test_the_agent_tells_the_model_what_the_gpt_oss_call_lacks(tmp_path, fake_llm):
    agent = _agent(fake_llm, tmp_path, FS, [GPT_OSS_REPLY, "giving up"])
    events = list(agent.chat("edit evals.py", max_turns=4))
    results = [e for e in events if isinstance(e, ToolResult)]
    # The run goes on instead of stopping on the patch as its answer; the
    # model hears the call is missing `path`.
    assert [r.name for r in results] == ["apply_patch"]
    assert "path" in results[0].output
    assert agent.client.requests[-1]["messages"][-1]["role"] == "tool"


def test_a_nameless_object_never_becomes_code(tmp_path, fake_llm):
    # run_python fits {"code": ...} alone, but code tools are never guessed.
    reply = '{"code": "print(1)"}'
    unfiltered = {**SCHEMAS, "run_python": REGISTRY["run_python"].parameters}
    assert recover_text_tool_calls(reply, KNOWN | {"run_python"}, unfiltered)  # it would fit
    agent = _agent(fake_llm, tmp_path, (*FS, "run_python"), [reply])
    events = list(agent.chat("give me a JSON object with a code field", max_turns=4))
    assert not any(isinstance(e, ToolResult) for e in events)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == reply
