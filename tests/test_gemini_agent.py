"""gemini backend + driver: `gemini -o json` parsing (success + the stderr-error
path), token extraction, registration. Uses a fake `subprocess.run` so these run
without the `gemini` CLI installed (as in CI)."""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest

from evi.llm import gemini_agent as gm
from evi.llm.gemini_agent import _extract_tokens, run_gemini_turn


def _run_of(stdout="", stderr="", rc=0):
    def _run(argv, **kw):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)
    return _run


def _collect(stdout="", stderr="", rc=0):
    q: queue.Queue = queue.Queue()
    run_gemini_turn(["gemini", "-o", "json", "-p", "x"], out=q, run=_run_of(stdout, stderr, rc))
    items = []
    while not q.empty():
        items.append(q.get())
    return items


# --- run_gemini_turn ---------------------------------------------------------


def test_gemini_success_response_and_usage():
    stdout = json.dumps({
        "response": "hello from gemini",
        "stats": {"models": {"gemini-2.5-pro": {"tokens": {"prompt": 12, "candidates": 5, "total": 17}}}},
    })
    items = _collect(stdout=stdout, rc=0)
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["hello from gemini"]
    assert any(getattr(c, "choices", None) and c.choices and c.choices[0].finish_reason == "stop" for c in items)
    usage = next(c.usage for c in items if getattr(c, "usage", None) is not None)
    assert usage.prompt_tokens == 12 and usage.completion_tokens == 5 and usage.total_tokens == 17


def test_gemini_error_from_stderr_json():
    stderr = json.dumps({"session_id": "s1", "error": {"type": "Error", "message": "Please set an Auth method", "code": 41}})
    items = _collect(stdout="", stderr=stderr, rc=41)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "Please set an Auth method" in str(errs[0][1])
    assert not any(getattr(it, "choices", None) for it in items)


def test_gemini_error_from_realistic_multiline_stderr():
    # The real failure shape: log lines, then a pretty-printed JSON error object.
    stderr = (
        'YOLO mode is enabled. All tool calls will be automatically approved.\n'
        'Approval mode overridden to "default" because the current folder is not trusted.\n'
        '{\n'
        '  "session_id": "abc",\n'
        '  "error": {\n'
        '    "type": "Error",\n'
        '    "message": "Please set an Auth method",\n'
        '    "code": 41\n'
        '  }\n'
        '}\n'
    )
    items = _collect(stdout="", stderr=stderr, rc=41)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and str(errs[0][1]) == "Please set an Auth method"


def test_gemini_error_from_plain_stderr():
    items = _collect(stdout="", stderr="YOLO mode is enabled.\nsomething broke\n", rc=1)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1
    # noise lines (YOLO/Approval) are skipped in favour of the real message
    assert "something broke" in str(errs[0][1])


def test_gemini_response_with_error_field_errors():
    stdout = json.dumps({"response": "", "error": {"message": "quota exceeded"}})
    items = _collect(stdout=stdout, rc=0)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "quota exceeded" in str(errs[0][1])


def test_extract_tokens_nested_and_missing():
    assert _extract_tokens({"models": {"m": {"tokens": {"prompt": 3, "candidates": 2}}}}) == (3, 2)
    assert _extract_tokens({"promptTokenCount": 7, "candidatesTokenCount": 1}) == (7, 1)
    assert _extract_tokens({"nothing": "here"}) == (0, 0)
    assert _extract_tokens(None) == (0, 0)


# --- client path (fake _gemini_path + subprocess.run) ------------------------


def test_gemini_client_stream(monkeypatch):
    monkeypatch.setattr(gm, "_gemini_path", lambda: "gemini")
    monkeypatch.setattr(gm.subprocess, "run",
                        _run_of(stdout=json.dumps({"response": "answer", "stats": {}}), rc=0))
    client = gm.GeminiAgentClient()
    chunks = list(client.chat.completions.create(
        model="gemini-2.5-pro", messages=[{"role": "user", "content": "q"}], stream=True))
    text = "".join(c.choices[0].delta.content for c in chunks
                   if c.choices and c.choices[0].delta and c.choices[0].delta.content)
    assert text == "answer"
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)


# --- backend registration ----------------------------------------------------


def test_backend_registered():
    from evi.backends.factory import KNOWN_BACKENDS, default_base_url
    from evi.backends.gemini_agent import GeminiAgentBackend

    assert KNOWN_BACKENDS["gemini"] is GeminiAgentBackend
    assert default_base_url("gemini") == ""


def test_get_backend_returns_gemini():
    from evi.backends import get_backend

    settings = SimpleNamespace(backend="gemini", base_url="", api_key="", request_timeout=120.0)
    assert type(get_backend(settings)).__name__ == "GeminiAgentBackend"


def test_backend_list_models():
    from evi.backends.gemini_agent import GeminiAgentBackend

    b = GeminiAgentBackend()
    assert [m.id for m in b.list_models()] == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert all(m.backend == "gemini" and m.family == "gemini" for m in b.list_models())
    assert b.supports_pull() is False


def test_unavailable_when_gemini_missing(monkeypatch):
    monkeypatch.setattr(gm.shutil, "which", lambda _name: None)
    with pytest.raises(gm.GeminiUnavailable):
        gm._gemini_path()
