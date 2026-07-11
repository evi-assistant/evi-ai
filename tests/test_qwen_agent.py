"""qwen backend + driver: `qwen -o json` (Claude-Code event array) parsing, the
timeout backstop, and backend registration. Uses a fake `subprocess.run` so these
run without the `qwen` CLI installed (as in CI)."""

from __future__ import annotations

import queue
import subprocess
from types import SimpleNamespace

import pytest

from evi.llm import qwen_agent as qw
from evi.llm.qwen_agent import run_qwen_turn


def _run_of(stdout="", stderr="", rc=0):
    def _run(argv, **kw):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)
    return _run


def _collect(stdout="", stderr="", rc=0, run=None):
    q: queue.Queue = queue.Queue()
    run_qwen_turn(["qwen", "-o", "json", "-p", "x"], out=q,
                  run=run or _run_of(stdout, stderr, rc))
    items = []
    while not q.empty():
        items.append(q.get())
    return items


# --- run_qwen_turn -----------------------------------------------------------


def test_qwen_success_array_and_usage():
    stdout = (
        '[{"type":"assistant","message":{"content":[{"type":"text","text":"hi from qwen"}]}},'
        '{"type":"result","subtype":"success","usage":{"input_tokens":5,"output_tokens":2},"result":"hi from qwen"}]'
    )
    items = _collect(stdout=stdout, rc=0)
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["hi from qwen"]
    assert any(getattr(c, "choices", None) and c.choices and c.choices[0].finish_reason == "stop" for c in items)
    usage = next(c.usage for c in items if getattr(c, "usage", None) is not None)
    assert usage.prompt_tokens == 5 and usage.completion_tokens == 2


def test_qwen_error_result_event():
    stdout = ('[{"type":"result","subtype":"error_during_execution","is_error":true,'
              '"usage":{"input_tokens":0,"output_tokens":0},'
              '"error":{"message":"No auth type is selected"}}]')
    items = _collect(stdout=stdout, rc=1)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "No auth type is selected" in str(errs[0][1])
    assert not any(getattr(it, "choices", None) for it in items)


def test_qwen_no_stdout_falls_back_to_stderr():
    items = _collect(stdout="", stderr="qwen: fatal startup error", rc=1)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "fatal startup error" in str(errs[0][1])


def test_qwen_timeout_errors():
    def _raise(argv, **kw):
        raise subprocess.TimeoutExpired(cmd="qwen", timeout=1)
    items = _collect(run=_raise)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and isinstance(errs[0][1], qw.QwenUnavailable)


# --- client path (fake _qwen_path + subprocess.run) --------------------------


def test_qwen_client_stream(monkeypatch):
    monkeypatch.setattr(qw, "_qwen_path", lambda: "qwen")
    monkeypatch.setattr(qw.subprocess, "run", _run_of(
        stdout='[{"type":"assistant","message":{"content":[{"type":"text","text":"answer"}]}},'
               '{"type":"result","subtype":"success","usage":{"input_tokens":1,"output_tokens":1}}]',
        rc=0))
    client = qw.QwenAgentClient()
    chunks = list(client.chat.completions.create(
        model="qwen3-coder-plus", messages=[{"role": "user", "content": "q"}], stream=True))
    text = "".join(c.choices[0].delta.content for c in chunks
                   if c.choices and c.choices[0].delta and c.choices[0].delta.content)
    assert text == "answer"
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)


# --- backend registration ----------------------------------------------------


def test_backend_registered():
    from evi.backends.factory import KNOWN_BACKENDS, default_base_url
    from evi.backends.qwen_agent import QwenAgentBackend

    assert KNOWN_BACKENDS["qwen"] is QwenAgentBackend
    assert default_base_url("qwen") == ""


def test_get_backend_returns_qwen():
    from evi.backends import get_backend

    settings = SimpleNamespace(backend="qwen", base_url="", api_key="", request_timeout=120.0)
    assert type(get_backend(settings)).__name__ == "QwenAgentBackend"


def test_backend_list_models():
    from evi.backends.qwen_agent import QwenAgentBackend

    b = QwenAgentBackend()
    assert [m.id for m in b.list_models()] == ["qwen3-coder-plus", "qwen3-coder-flash"]
    assert all(m.backend == "qwen" and m.family == "qwen3-coder" for m in b.list_models())
    assert b.supports_pull() is False


def test_unavailable_when_qwen_missing(monkeypatch):
    monkeypatch.setattr(qw.shutil, "which", lambda _name: None)
    with pytest.raises(qw.QwenUnavailable):
        qw._qwen_path()
