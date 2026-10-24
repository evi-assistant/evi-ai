"""Every tool result is held to its share of the context window before it
enters the history (Agent._fit_output -> evi.tool_output): a longer one is
cut to its head and tail, and the whole of it goes to a spill file the model
can page with read_file, under the implementer policy too."""

from __future__ import annotations

import json

import pytest

import evi.tools.fs  # noqa: F401  registers read_file
from evi import tool_output
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.policy import implementer_policy
from evi.tools.base import REGISTRY, Tool
from tests.conftest import llm_call, llm_text

_BIG = "".join(f"line {i}: {'x' * 50}\n" for i in range(40_000))  # ~2.4 MB


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setattr("evi.config.HOME", path)
    return path


def _dump(name: str = "dump") -> Tool:
    return Tool(name=name, description="prints a lot", parameters={"type": "object",
                                                                  "properties": {}},
                func=lambda **_k: _BIG, category="code")


def _agent(client, *tools: Tool, size: int = 8192, **kw) -> Agent:
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.context_size = "openai_compat", "https://x/v1", size
    kw.setdefault("unattended_policy", "allow")
    return Agent(client=client, config=cfg, transcripts=None, tools=list(tools), **kw)


def _results(agent: Agent) -> list[str]:
    return [e.output for e in agent.chat("go", max_turns=4) if isinstance(e, ToolResult)]


def test_a_huge_result_enters_the_history_cut_with_the_whole_in_a_spill_file(fake_llm, home):
    agent = _agent(fake_llm([llm_call(("dump", {})), llm_text("done")]), _dump())
    [out] = _results(agent)
    assert len(out) <= tool_output.cap_chars(8192) and out.startswith("line 0:")
    [spill] = list((home / "tool-output").rglob("*.txt"))
    assert str(spill) in out and spill.read_text(encoding="utf-8") == _BIG
    stored = [m for m in agent.history if m.get("role") == "tool"]
    assert stored[0]["content"] == out  # what the model sees is the cut one


def test_command_output_keeps_more_of_its_tail(fake_llm, home):
    agent = _agent(fake_llm([llm_call(("run_python", {})), llm_text("done")]),
                   _dump("run_python"))
    [out] = _results(agent)
    head, _cut, tail = out.partition("characters cut.")
    assert len(tail) > len(head) and out.rstrip().endswith("line 39999: " + "x" * 50)


def test_a_small_result_is_untouched_and_spills_nothing(fake_llm, home, tmp_path):
    (tmp_path / "f.txt").write_text("small\n", encoding="utf-8")
    agent = _agent(fake_llm([llm_call(("read_file", {"path": str(tmp_path / "f.txt")})),
                             llm_text("done")]), REGISTRY["read_file"])
    [out] = _results(agent)
    assert "small" in out and "characters cut" not in out
    assert not (home / "tool-output").exists()


def test_the_implementer_policy_lets_the_model_page_its_spill_file(fake_llm, home, tmp_path):
    # A large slice of a file in the run's folder spills; the policy may then read the spill.
    work = tmp_path / "work"
    work.mkdir()
    (work / "big.txt").write_text(_BIG, encoding="utf-8")
    agent = _agent(fake_llm([llm_call(("read_file", {"path": "big.txt", "offset": 1,
                                                     "limit": 20_000})), llm_text("ok")]),
                   REGISTRY["read_file"])
    agent.cwd = str(work)
    agent.policy = implementer_policy(work)
    [cut] = _results(agent)
    [spill] = list((home / "tool-output").rglob("*.txt"))
    assert str(spill) in cut
    agent.client = fake_llm([llm_call(("read_file", {"path": str(spill), "offset": 1,
                                                     "limit": 3})), llm_text("done")])
    [page] = _results(agent)
    assert "line 0:" in page and "line 1:" in page and "outside" not in page


def test_reading_a_long_stretch_of_a_spill_file_is_cut_but_not_spilled_again(fake_llm, home):
    agent = _agent(fake_llm([llm_call(("dump", {})), llm_text("ok")]), _dump(),
                   REGISTRY["read_file"])
    _results(agent)
    [spill] = list((home / "tool-output").rglob("*.txt"))
    agent.client = fake_llm([llm_call(("read_file", json.dumps({"path": str(spill), "offset": 1,
                                                                "limit": 20_000}))),
                             llm_text("done")])
    [out] = _results(agent)
    assert "offset and limit" in out and len(out) <= tool_output.cap_chars(8192)
    assert list((home / "tool-output").rglob("*.txt")) == [spill]
