"""codex backend + driver: prompt rendering, `codex exec --json` JSONL parsing,
backend registration. Uses a FAKE subprocess so these run without the `codex`
CLI installed (as in CI)."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from evi.llm import codex_agent as cx
from evi.llm.codex_agent import render_prompt, run_codex_turn


# --- render_prompt (pure) ----------------------------------------------------


def test_render_prompt_system_and_turns():
    out = render_prompt([
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
        {"role": "user", "content": "bye"},
    ])
    assert out == "Be terse.\n\nUser: hi\nAssistant: yo\nUser: bye"


def test_render_prompt_tool_history_as_text():
    out = render_prompt([
        {"role": "user", "content": "add 2 and 3"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "function": {"name": "add", "arguments": '{"a": 2}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "5"},
    ])
    assert "[called add(" in out
    assert "[tool add returned: 5]" in out


# --- run_codex_turn (JSONL parser, fake subprocess) --------------------------


class _FakeStdin:
    def write(self, _s):
        pass

    def close(self):
        pass


class _FakeStdout:
    def __init__(self, lines):
        self._it = iter(lines)

    def __iter__(self):
        return self._it

    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines, rc=0):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self._rc = rc

    def wait(self):
        return self._rc


def _popen_of(lines, rc=0):
    def _popen(argv, **kw):
        return _FakeProc(lines, rc)
    return _popen


def _collect(lines, rc=0):
    q: queue.Queue = queue.Queue()
    run_codex_turn(["codex"], "prompt", out=q, popen=_popen_of(lines, rc))
    items = []
    while not q.empty():
        items.append(q.get())
    return items


def test_run_codex_turn_happy_path():
    items = _collect([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i1","type":"reasoning","text":"pondering"}}',
        '{"type":"item.completed","item":{"id":"i2","type":"command_execution","command":"ls"}}',
        '{"type":"item.completed","item":{"id":"i3","type":"agent_message","text":"hello from codex"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":4}}',
    ])
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert "<think>pondering</think>" in contents        # reasoning -> think
    assert "hello from codex" in contents                # agent_message -> text
    assert "ls" not in "".join(contents)                 # command_execution ignored
    finishes = [c.choices[0].finish_reason for c in items if getattr(c, "choices", None) and c.choices]
    assert "stop" in finishes
    usage = next(c.usage for c in items if getattr(c, "usage", None) is not None)
    assert usage.prompt_tokens == 10 and usage.completion_tokens == 4 and usage.total_tokens == 14


def test_run_codex_turn_turn_failed_errors():
    items = _collect([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"error","message":"Reconnecting... 1/5"}',   # transient -> ignored
        '{"type":"turn.failed","error":{"message":"401 Unauthorized"}}',
    ])
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "401 Unauthorized" in str(errs[0][1])
    # a transient top-level error must NOT have produced a chunk
    assert not any(getattr(it, "choices", None) for it in items)


def test_run_codex_turn_ignores_non_json_lines():
    items = _collect([
        "2026-07-11T04:51:22Z ERROR codex_api: some rust log noise",   # non-JSON
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"ok"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
    ])
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["ok"]


def test_run_codex_turn_crash_without_terminal_event():
    items = _collect(['{"type":"turn.started"}'], rc=1)   # process died, rc!=0
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "exited with code 1" in str(errs[0][1])


# --- client path (fake _codex_path + Popen) ----------------------------------


def test_codex_client_stream(monkeypatch):
    monkeypatch.setattr(cx, "_codex_path", lambda: "codex")
    monkeypatch.setattr(cx.subprocess, "Popen", _popen_of([
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"answer"}}',
        '{"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":1}}',
    ]))
    client = cx.CodexAgentClient()
    chunks = list(client.chat.completions.create(
        model="gpt-5-codex", messages=[{"role": "user", "content": "q"}], stream=True))
    text = "".join(c.choices[0].delta.content for c in chunks
                   if c.choices and c.choices[0].delta and c.choices[0].delta.content)
    assert text == "answer"
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)


# --- backend registration ----------------------------------------------------


def test_backend_registered():
    from evi.backends.codex_agent import CodexAgentBackend
    from evi.backends.factory import KNOWN_BACKENDS, default_base_url

    assert KNOWN_BACKENDS["codex"] is CodexAgentBackend
    assert default_base_url("codex") == ""


def test_get_backend_returns_codex():
    from evi.backends import get_backend

    settings = SimpleNamespace(backend="codex", base_url="", api_key="", request_timeout=120.0)
    assert type(get_backend(settings)).__name__ == "CodexAgentBackend"


def test_backend_list_models():
    from evi.backends.codex_agent import CodexAgentBackend

    b = CodexAgentBackend()
    assert [m.id for m in b.list_models()] == ["gpt-5-codex", "gpt-5"]
    assert all(m.backend == "codex" for m in b.list_models())
    assert b.supports_pull() is False


def test_unavailable_when_codex_missing(monkeypatch):
    monkeypatch.setattr(cx.shutil, "which", lambda _name: None)
    with pytest.raises(cx.CodexUnavailable):
        cx._codex_path()
