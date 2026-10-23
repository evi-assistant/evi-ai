"""Secrets are redacted at the display boundary (safety-secret-redaction,
acceptance criterion 1: "An env API key echoed by a tool never appears in
transcripts, exports or tool-call displays").

The displays: the web UI's ToolCall / ToolResult / PermissionRequest events
and the tool messages /history redraws (evi.apps.web.server), the CLI's
tool-call lines, result previews and permission prompts (evi.apps.cli.main),
and `evi run --format json`'s tool outputs (evi.headless). Each redacts
before it cuts anything short, so no prefix of a secret is shown either. The
live history the model works from keeps the real values, as designed.

Every secret is a test value set in this process's environment; nothing here
reads the real ~/.evi (tests/conftest.py points EVI_HOME at a throwaway).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from evi import redact
from evi.config import Config
from evi.llm.agent import Agent, Done, ToolCall, ToolResult
from evi.tools.base import Tool
from tests._live_web import Scripted
from tests.conftest import llm_call, llm_text

# Shapeless, so only the known-value set (the env var) can catch it.
SECRET = "Zq8vN2kLp4Xr7Tw1Ys5M-env"
MARK = "[REDACTED:ACME_API_KEY]"


@pytest.fixture(autouse=True)
def env_key(monkeypatch):
    for name in list(os.environ):
        if redact._SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACME_API_KEY", SECRET)
    redact.refresh()
    yield
    redact.refresh()


def _echo_env() -> Tool:
    return Tool(
        name="echo_env", description="print an environment variable",
        parameters={"type": "object", "properties": {"name": {"type": "string"},
                                                     "note": {"type": "string"},
                                                     "pin": {"type": "integer"}}},
        func=lambda name="", note="", pin=0: f"{name}={os.environ.get(name, '')}",
    )


def _no_prefix(text: str) -> bool:
    """No 8-character run of SECRET shows: not whole, not cut short."""
    return not any(SECRET[i:i + 8] in text for i in range(len(SECRET) - 7))


# ---- the web UI -----------------------------------------------------------------


def test_web_tool_call_and_result_events_and_the_redrawn_history_are_redacted(
    monkeypatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from tests._live_web import sse_events, web_app

    agent = Agent(
        client=Scripted([
            llm_call(("echo_env", {"name": "ACME_API_KEY", "note": f"it is {SECRET}"})),
            llm_text("done"),
        ]),
        config=Config(), tools=[_echo_env()], transcripts=None,
    )
    agent.enable_auto_all()
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    body = c.post("/api/chat", json={"session_id": "r1", "message": "show it"}).text
    events = sse_events(body)
    assert _no_prefix(body)
    call = next(e for e in events if e["kind"] == "ToolCall")
    assert json.loads(call["arguments"]) == {"name": "ACME_API_KEY", "note": f"it is {MARK}"}
    result = next(e for e in events if e["kind"] == "ToolResult")
    assert result["output"] == f"ACME_API_KEY={MARK}"

    hist = c.get("/api/session/r1/history").json()["messages"]
    assert _no_prefix(json.dumps(hist))
    tool = next(m for m in hist if m["role"] == "tool")
    assert tool["content"] == f"ACME_API_KEY={MARK}"
    asked = next(m for m in hist if m.get("tool_calls"))
    args = asked["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args)["note"] == f"it is {MARK}"
    assert asked["tool_calls"][0]["id"]  # still pairs with its result

    # The model's own history keeps what the tool returned (not a display).
    assert f"ACME_API_KEY={SECRET}" in [m.get("content") for m in agent.history]


def test_duplicate_key_and_numeric_arguments_are_redacted_in_events_history_and_transcript(
    monkeypatch, tmp_path: Path
) -> None:
    """Arguments whose secret the parse misses (a duplicate key's earlier value,
    which json.loads drops; a known value sent as a JSON number) are redacted
    in the ToolCall event, the redrawn history and the transcript on disk."""
    from fastapi.testclient import TestClient

    from evi.transcripts import TranscriptStore
    from tests._live_web import sse_events, web_app

    pin = "4815162342108"
    monkeypatch.setenv("PROBE_PIN_TOKEN", pin)
    redact.refresh()
    dup = '{"note": "' + SECRET + '", "note": "x"}'
    num = '{"name": "HOME", "pin": ' + pin + "}"
    store = TranscriptStore(root=tmp_path / "transcripts")
    agent = Agent(
        client=Scripted([llm_call(("echo_env", dup), ("echo_env", num)), llm_text("done")]),
        config=Config(), tools=[_echo_env()], transcripts=store,
    )
    agent.enable_auto_all()
    c = TestClient(web_app(monkeypatch, tmp_path, agent))
    body = c.post("/api/chat", json={"session_id": "d1", "message": "go"}).text
    hist = c.get("/api/session/d1/history").text
    disk = "".join(p.read_text("utf-8") for p in (tmp_path / "transcripts").rglob("*.jsonl"))
    assert disk  # the transcript was written
    for shown in (body, hist, disk):
        assert _no_prefix(shown) and pin not in shown
    calls = [e for e in sse_events(body) if e["kind"] == "ToolCall"]
    assert calls[0]["arguments"] == '{"note": "' + MARK + '", "note": "x"}'
    assert json.loads(calls[1]["arguments"]) == {"name": "HOME",
                                                 "pin": "[REDACTED:PROBE_PIN_TOKEN]"}


def test_a_web_permission_request_shows_the_arguments_redacted() -> None:
    import evi.apps.web.server as server_mod

    agent = Agent(client=Scripted([]), config=Config(), tools=[], transcripts=None)
    agent._begin_turn()
    sess = server_mod.WebSession(agent=agent)
    sent: list[dict] = []
    callback = server_mod._make_permission_callback({"p": sess}, "p", sent.append, 0.05)
    args = json.dumps({"command": f"curl -H 'X-Key: {SECRET}' https://x.test"})
    assert callback("run_command", args, "shell") is False  # nobody answered in time
    req = next(e for e in sent if e["kind"] == "PermissionRequest")
    assert _no_prefix(json.dumps(sent))
    assert json.loads(req["args"]) == {"command": f"curl -H 'X-Key: {MARK}' https://x.test"}


def test_event_payloads_redact_tool_events_only() -> None:
    from evi.apps.web.server import _event_payload, _serialize

    assert _event_payload(ToolCall("t", json.dumps({"k": SECRET}))) == {
        "kind": "ToolCall", "name": "t", "arguments": json.dumps({"k": MARK}, ensure_ascii=False)}
    out = _event_payload(ToolResult("t", f"x {SECRET} y"))
    assert out["output"] == f"x {MARK} y" and out["citations"] == []
    clean = json.dumps({"k": "nothing secret"})
    assert _event_payload(ToolCall("t", clean))["arguments"] == clean  # byte for byte
    assert json.loads(_serialize(ToolResult("t", SECRET)))["output"] == MARK
    assert _event_payload(Done("stop")) == {"kind": "Done", "reason": "stop"}


# ---- the CLI ----------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    import evi.apps.cli.main as cli_main

    answers: list[str] = []
    monkeypatch.setattr(cli_main.console, "input", lambda *_a, **_k: answers.pop(0) if answers else "")
    # No wrapping: a secret split across two lines would hide from the checks.
    monkeypatch.setattr(cli_main.console, "width", 100_000)
    return cli_main, answers


def test_cli_permission_prompt_shows_the_arguments_redacted_before_cutting(cli, capsys) -> None:
    cli_main, answers = cli
    answers.append("n")
    # The secret straddles the 200-character cut: cutting first would show
    # its first ten characters. Redacted first, the cut takes the marker's.
    args = json.dumps({"command": "echo " + "x" * 172 + SECRET})
    assert args.index(SECRET) == 190
    assert cli_main._cli_permission_prompt("run_command", args, "shell") is False
    out = capsys.readouterr().out
    assert "permission:" in out and _no_prefix(out)
    assert "x[REDACTED:…" in out
    answers.append("n")
    cli_main._cli_permission_prompt("run_command", json.dumps({"command": SECRET}), "shell")
    assert f'args={{"command": "{MARK}"}}' in capsys.readouterr().out


def test_cli_batch_permission_prompt_shows_every_call_redacted(cli, capsys) -> None:
    cli_main, answers = cli
    answers.append("a")
    calls = [
        ("run_command", json.dumps({"command": f"echo {SECRET}"}), "shell"),
        ("web_fetch", json.dumps({"url": "https://x.test/?k=" + "y" * 140 + SECRET}), "network"),
    ]
    assert cli_main._cli_permission_prompt_batch(calls) == [True, True]
    out = capsys.readouterr().out
    assert out.count("[REDACTED:ACME_API_KEY]") == 1 and _no_prefix(out)
    assert "2 tool calls requested" in out


def test_cli_tool_call_and_result_lines_are_redacted_through_a_turn(cli, capsys) -> None:
    """`_print_turn` (evi ask / scheduled output), driven through Agent.chat."""
    cli_main, _answers = cli
    agent = Agent(
        client=Scripted([llm_call(("echo_env", {"name": "ACME_API_KEY", "note": SECRET})),
                              llm_text("ok")]),
        config=Config(), tools=[_echo_env()], transcripts=None, unattended_policy="allow",
    )
    cli_main._print_turn(agent, "show it")
    out = capsys.readouterr().out
    assert "→ tool" in out and "← result" in out
    assert f"ACME_API_KEY={MARK}" in out and _no_prefix(out)


def test_cli_tool_lines_redact_then_cut_and_never_read_markup(cli, capsys) -> None:
    cli_main, _answers = cli
    out_text = "y" * 390 + SECRET  # the cut at 400 would show its first ten characters
    cli_main.console.print(cli_main._tool_result_line(ToolResult("t", out_text)))
    cli_main.console.print(cli_main._tool_call_line(ToolCall("t", json.dumps({"k": "[/bold] [red]"}))))
    out = capsys.readouterr().out
    assert _no_prefix(out) and "y[REDACTED:…" in out
    assert "[/bold] [red]" in out  # shown as typed, not parsed as Rich markup


def test_repl_tool_lines_are_redacted(monkeypatch, tmp_path, capsys) -> None:
    from tests.test_agent_cancel import _repl, _run

    cli_main, reads = _repl(monkeypatch, tmp_path, ["show it"])
    agent = Agent(
        client=Scripted([llm_call(("echo_env", {"name": "ACME_API_KEY"})), llm_text("ok")]),
        config=Config(), tools=[_echo_env()], transcripts=None, unattended_policy="allow",
    )
    _run(cli_main, agent)
    out = capsys.readouterr().out
    assert reads == ["show it"]
    assert f"ACME_API_KEY={MARK}" in out and _no_prefix(out)


# ---- evi run ----------------------------------------------------------------------


class _Fake:
    def __init__(self, events):
        self._events = events

    def chat(self, prompt, max_turns=12):
        yield from self._events


def test_evi_run_tool_outputs_are_redacted_before_they_are_cut() -> None:
    from evi.headless import run_headless, to_json

    res = run_headless(_Fake([ToolResult("echo_env", "z" * 1995 + SECRET), Done("stop")]), "x")
    out = to_json(res)
    assert _no_prefix(out)
    assert res.tools[0]["output"] == "z" * 1995 + MARK[:5]
    assert len(res.tools[0]["output"]) == 2000


def test_display_redaction_never_blocks_on_a_thread() -> None:
    """Displays redact from worker threads (the web's turn worker, a tool
    thread asking a question): a Redactor made on another thread works."""
    seen: list[str] = []
    t = threading.Thread(target=lambda: seen.append(redact.Redactor()(f"k={SECRET}")))
    t.start()
    t.join(10)
    assert seen == [f"k={MARK}"]
