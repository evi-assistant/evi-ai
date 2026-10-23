"""write_file overwrites only a file the agent has read, unchanged since
(evi/tools/fs.py, the read ledger); outside an agent nothing is checked."""

from __future__ import annotations

import evi.tools.fs  # noqa: F401  registers read_file, write_file, edit_file
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.tools.base import REGISTRY
from evi.tools.fs import write_file
from tests.conftest import llm_call, llm_text


def _agent(client) -> Agent:
    return Agent(client=client, config=Config(), transcripts=None, unattended_policy="allow",
                 tools=[REGISTRY[n] for n in ("read_file", "write_file", "edit_file")])


def _results(agent: Agent, prompt: str = "go") -> list[str]:
    return [e.output for e in agent.chat(prompt, max_turns=8) if isinstance(e, ToolResult)]


def test_an_unread_existing_file_is_not_overwritten(fake_llm, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"keep me\nline two\n")
    client = fake_llm([llm_call(("write_file", {"path": str(f), "content": "gone\n"})),
                       llm_text("done")])
    [out] = _results(_agent(client))
    assert out.startswith("ERROR: not written") and "haven't read it" in out
    assert "keep me" in out  # how the file begins, so the model sees what it would lose
    assert f.read_bytes() == b"keep me\nline two\n"


def test_reading_first_lets_it_write(fake_llm, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"old\n")
    client = fake_llm([llm_call(("read_file", {"path": str(f)})),
                       llm_call(("write_file", {"path": str(f), "content": "new\n"})),
                       llm_text("done")])
    read, wrote = _results(_agent(client))
    assert wrote.startswith("wrote") and f.read_bytes() == b"new\n"


def test_a_file_changed_since_it_was_read_is_refused(fake_llm, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"old\n")
    client = fake_llm([llm_call(("read_file", {"path": str(f)})), llm_text("read"),
                       llm_call(("write_file", {"path": str(f), "content": "mine\n"})),
                       llm_text("done")])
    agent = _agent(client)
    _results(agent, "read it")
    f.write_bytes(b"someone else's change, longer\n")  # the user, a command, another tool
    [out] = _results(agent, "now write it")
    assert out.startswith("ERROR: not written") and "changed since" in out
    assert f.read_bytes() == b"someone else's change, longer\n"


def test_its_own_new_files_and_edits_need_no_reread(fake_llm, tmp_path):
    f = tmp_path / "made.txt"
    g = tmp_path / "edited.txt"
    g.write_bytes(b"a = 1\n")
    client = fake_llm([
        llm_call(("write_file", {"path": str(f), "content": "v1\n"})),
        llm_call(("write_file", {"path": str(f), "content": "v2\n"})),
        llm_call(("edit_file", {"path": str(g), "old_string": "a = 1", "new_string": "a = 2"})),
        llm_call(("write_file", {"path": str(g), "content": "a = 3\n"})),
        llm_text("done")])
    outs = _results(_agent(client))
    # edit_file needs no read (its old_string must match); a write after it needs none either.
    assert [o.split(" ")[0] for o in outs] == ["wrote", "wrote", "edited", "wrote"], outs
    assert f.read_bytes() == b"v2\n" and g.read_bytes() == b"a = 3\n"


def test_outside_an_agent_nothing_is_checked(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"old\n")
    assert write_file(str(f), "new\n").startswith("wrote")
    assert f.read_bytes() == b"new\n"
