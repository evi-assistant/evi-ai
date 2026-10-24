"""Stale tool results are masked before anything is compacted: past the
watermark, on an overflow, and by /compact --fast (Agent.mask_stale_results)."""

from __future__ import annotations

import io
from types import SimpleNamespace

import jsonschema
import pytest
from rich.console import Console

from evi import events
from evi.commands import CommandStore
from evi.config import Config
from evi.history import validate_history
from evi.llm.agent import MASK_PREFIX, Agent, ContextCompacted, Done, TextDelta
from tests.conftest import llm_text

_BIG = "result line\n" * 200  # 2 400 chars


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setattr("evi.config.HOME", path)
    return path


def _turn(n: int, content: str = _BIG) -> list[dict]:
    call = {"id": f"c{n}", "type": "function", "function": {"name": "read_file",
                                                            "arguments": "{}"}}
    return [{"role": "user", "content": f"question {n}"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": content},
            {"role": "assistant", "content": f"answer {n}"}]


def _agent(client=None, turns: int = 4, size: int = 32768) -> Agent:
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.context_size = "openai_compat", "https://x/v1", size
    agent = Agent(client=client, config=cfg, transcripts=None, tools=[],
                  unattended_policy="allow")
    for n in range(turns):
        agent.history += _turn(n)
    return agent


def _tool_contents(agent: Agent) -> list[str]:
    return [m["content"] for m in agent.history if m.get("role") == "tool"]


def test_old_results_become_stubs_naming_a_file_with_the_whole_result(home):
    agent = _agent(turns=4)
    assert agent.mask_stale_results(keep_turns=2) == 2
    old, recent = _tool_contents(agent)[:2], _tool_contents(agent)[2:]
    assert all(c.startswith(MASK_PREFIX) for c in old) and recent == [_BIG, _BIG]
    for stub in old:
        path = stub.split("The whole of it is in ", 1)[1].split(": read it", 1)[0]
        assert open(path, encoding="utf-8").read() == _BIG
    assert validate_history(agent.history) == []
    assert agent.mask_stale_results(keep_turns=2) == 0  # stubs aren't masked again


def test_a_result_already_spilled_keeps_its_file_and_small_ones_stay(home):
    agent = _agent(turns=0)
    named = (f"{_BIG}\n[... 9 characters cut. The whole output is in {home}/x/call.txt: read "
             "it with read_file ...]")
    agent.history += _turn(0, named) + _turn(1, "short") + _turn(2) + _turn(3)
    assert agent.mask_stale_results(keep_turns=2) == 1
    stub, small = _tool_contents(agent)[:2]
    assert f"{home}/x/call.txt" in stub and small == "short"
    assert not (home / "tool-output").exists()  # nothing new was spilled


def test_past_the_watermark_a_request_masks_before_it_compacts(home, fake_llm):
    # 4 x 2 400 chars of results ~ 2 400 tokens: over 60% of a 3 500-token window,
    # under the 85% that compacts once they're stubs.
    agent = _agent(fake_llm([llm_text("done")]), turns=4, size=3500)
    compacted: list[int] = []
    agent.compact_history = lambda *a, **k: compacted.append(1) or 0
    evs = list(agent.chat("next", max_turns=2))
    assert ContextCompacted("mask", 3) in evs and compacted == []
    assert evs[-1] == Done("stop")


class _Overflowing:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_kw):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("maximum context length is 4096 tokens (context_length_exceeded)")
        return iter(llm_text("answered"))


def test_an_overflow_masks_then_compacts_then_retries_once(home):
    client = _Overflowing()
    agent = _agent(client, turns=4)
    agent.compact_history = lambda *a, **k: 5
    evs = list(agent.chat("next", max_turns=3))
    kinds = [e for e in evs if isinstance(e, ContextCompacted)]
    assert kinds == [ContextCompacted("mask", 3), ContextCompacted("overflow", 5)]
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "answered"
    assert client.calls == 2


def test_compact_fast_masks_only_in_the_repl_and_the_web(home, tmp_path, monkeypatch):
    import evi.apps.cli.main as cli_main
    import evi.apps.web.server as server_mod

    out = io.StringIO()
    monkeypatch.setattr(cli_main, "console", Console(file=out))
    agent = _agent(turns=4)
    agent.compact_history = lambda *a, **k: pytest.fail("--fast must not summarise")
    cli_main._handle_compact(agent, "--fast", CommandStore(root=tmp_path))
    assert "masked" in out.getvalue() and _tool_contents(agent)[0].startswith(MASK_PREFIX)
    web_agent = _agent(turns=4)
    web_agent.compact_history = lambda *a, **k: pytest.fail("--fast must not summarise")
    reply = server_mod._handle_slash(web_agent, "/compact --fast", CommandStore(root=tmp_path))
    assert "masked" in reply.text


def test_the_mask_reason_is_in_the_event_schema():
    payload = {"schema_version": events.SCHEMA_VERSION, "seq": 1, "ts": "2026-10-24T00:00:00Z",
               **events.event_payload(ContextCompacted("mask", 3))}
    jsonschema.Draft202012Validator(events.SCHEMA).validate(payload)
