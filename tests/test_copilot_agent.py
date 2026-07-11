"""copilot backend + driver: `copilot -p … --output-format text -s` silent-text
handling, the stderr error path, the timeout backstop, and backend registration.
Uses a fake `subprocess.run` so these run without the `copilot` CLI (as in CI)."""

from __future__ import annotations

import queue
import subprocess
from types import SimpleNamespace

import pytest

from evi.llm import copilot_agent as co
from evi.llm.copilot_agent import _error_message, run_copilot_turn


def _run_of(stdout="", stderr="", rc=0):
    def _run(argv, **kw):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)
    return _run


def _collect(stdout="", stderr="", rc=0, run=None):
    q: queue.Queue = queue.Queue()
    run_copilot_turn(["copilot", "-p", "x"], out=q, run=run or _run_of(stdout, stderr, rc))
    items = []
    while not q.empty():
        items.append(q.get())
    return items


# --- run_copilot_turn --------------------------------------------------------


def test_copilot_success_silent_text():
    items = _collect(stdout="hello from copilot\n", rc=0)
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["hello from copilot"]
    assert any(getattr(c, "choices", None) and c.choices and c.choices[0].finish_reason == "stop" for c in items)
    usage = next(c.usage for c in items if getattr(c, "usage", None) is not None)
    assert usage.prompt_tokens == 0 and usage.completion_tokens == 0   # silent text has no counts


def test_copilot_error_prefers_error_line():
    stderr = (
        "Error: Access denied by policy settings (Request ID: ABC:123)\n"
        "\n"
        "Your Copilot CLI policy setting may be preventing access.\n"
    )
    items = _collect(stdout="", stderr=stderr, rc=1)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and str(errs[0][1]).startswith("Error: Access denied by policy settings")


def test_copilot_error_falls_back_to_first_line():
    items = _collect(stdout="", stderr="something failed badly", rc=1)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "something failed badly" in str(errs[0][1])


def test_copilot_timeout_errors():
    def _raise(argv, **kw):
        raise subprocess.TimeoutExpired(cmd="copilot", timeout=1)
    items = _collect(run=_raise)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and isinstance(errs[0][1], co.CopilotUnavailable)


def test_error_message_helper():
    assert _error_message("Error: nope\nmore", 1) == "Error: nope"
    assert _error_message("plain first\nsecond", 3) == "plain first"
    assert _error_message("", 2) == "copilot exited with code 2"


# --- client path (fake _copilot_path + subprocess.run) -----------------------


def test_copilot_client_stream(monkeypatch):
    monkeypatch.setattr(co, "_copilot_path", lambda: "copilot")
    monkeypatch.setattr(co.subprocess, "run", _run_of(stdout="answer", rc=0))
    client = co.CopilotAgentClient()
    chunks = list(client.chat.completions.create(
        model="auto", messages=[{"role": "user", "content": "q"}], stream=True))
    text = "".join(c.choices[0].delta.content for c in chunks
                   if c.choices and c.choices[0].delta and c.choices[0].delta.content)
    assert text == "answer"
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)


# --- backend registration ----------------------------------------------------


def test_backend_registered():
    from evi.backends.copilot_agent import CopilotAgentBackend
    from evi.backends.factory import KNOWN_BACKENDS, default_base_url

    assert KNOWN_BACKENDS["copilot"] is CopilotAgentBackend
    assert default_base_url("copilot") == ""


def test_get_backend_returns_copilot():
    from evi.backends import get_backend

    settings = SimpleNamespace(backend="copilot", base_url="", api_key="", request_timeout=120.0)
    assert type(get_backend(settings)).__name__ == "CopilotAgentBackend"


def test_backend_list_models():
    from evi.backends.copilot_agent import CopilotAgentBackend

    b = CopilotAgentBackend()
    assert [m.id for m in b.list_models()] == ["auto", "claude-sonnet-4.5", "gpt-5"]
    assert all(m.backend == "copilot" and m.family == "copilot" for m in b.list_models())
    assert b.supports_pull() is False


def test_unavailable_when_copilot_missing(monkeypatch):
    monkeypatch.setattr(co.shutil, "which", lambda _name: None)
    with pytest.raises(co.CopilotUnavailable):
        co._copilot_path()
