"""amp backend + driver: `amp -x --stream-json` Claude-Code JSONL parsing, the
login guard (fail fast, no hang), and backend registration. Uses a FAKE subprocess
so these run without the `amp` CLI installed (as in CI)."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from evi.llm import amp_agent as am
from evi.llm.amp_agent import run_amp_turn


# --- fake subprocess ---------------------------------------------------------


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
        self.pid = 4321
        self._rc = rc

    def wait(self):
        return self._rc


def _popen_of(lines, rc=0):
    def _popen(argv, **kw):
        return _FakeProc(lines, rc)
    return _popen


def _collect(lines, rc=0):
    q: queue.Queue = queue.Queue()
    run_amp_turn(["amp"], "prompt", out=q, popen=_popen_of(lines, rc))
    items = []
    while not q.empty():
        items.append(q.get())
    return items


# --- run_amp_turn (JSONL parser) ---------------------------------------------


def test_amp_happy_path_streams_text_and_usage():
    items = _collect([
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hello from amp"},{"type":"tool_use","name":"bash","input":{}}]}}',
        '{"type":"result","subtype":"success","usage":{"input_tokens":10,"cache_read_input_tokens":2,"output_tokens":4},"result":"hello from amp"}',
    ])
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["hello from amp"]                 # tool_use ignored; result text not doubled
    assert any(getattr(c, "choices", None) and c.choices and c.choices[0].finish_reason == "stop" for c in items)
    usage = next(c.usage for c in items if getattr(c, "usage", None) is not None)
    assert usage.prompt_tokens == 12 and usage.completion_tokens == 4   # 10 + 2 cache


def test_amp_result_only_emits_final_text():
    items = _collect([
        '{"type":"result","subtype":"success","usage":{"output_tokens":3},"result":"just the result"}',
    ])
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["just the result"]


def test_amp_result_error():
    items = _collect([
        '{"type":"result","subtype":"error_during_execution","is_error":true,"error":{"message":"boom"},"usage":{}}',
    ])
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "boom" in str(errs[0][1])


def test_amp_ignores_non_json_lines():
    items = _collect([
        "some progress noise (not json)",
        '{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}',
        '{"type":"result","subtype":"success","usage":{"input_tokens":1,"output_tokens":1}}',
    ])
    contents = [c.choices[0].delta.content for c in items
                if getattr(c, "choices", None) and c.choices[0].delta and c.choices[0].delta.content]
    assert contents == ["ok"]


def test_amp_crash_without_result_errors():
    items = _collect(['{"type":"system","subtype":"init"}'], rc=1)
    errs = [it for it in items if isinstance(it, tuple) and it and it[0] == "__error__"]
    assert len(errs) == 1 and "exited with code 1" in str(errs[0][1])


# --- login guard (_require_auth) ---------------------------------------------


def test_require_auth_raises_when_unauthenticated(monkeypatch):
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    monkeypatch.setattr(am.os.path, "isfile", lambda _p: False)
    with pytest.raises(am.AmpUnavailable):
        am._require_auth()


def test_require_auth_ok_with_env_key(monkeypatch):
    monkeypatch.setenv("AMP_API_KEY", "sgamp_xxx")
    monkeypatch.setattr(am.os.path, "isfile", lambda _p: False)
    assert am._require_auth() is None


def test_require_auth_ok_with_settings_file(monkeypatch):
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    monkeypatch.setattr(am.os.path, "isfile", lambda _p: True)
    assert am._require_auth() is None


# --- client path (fake _amp_path + _require_auth + Popen) --------------------


def test_amp_client_stream(monkeypatch):
    monkeypatch.setattr(am, "_amp_path", lambda: "amp")
    monkeypatch.setattr(am, "_require_auth", lambda: None)
    monkeypatch.setattr(am.subprocess, "Popen", _popen_of([
        '{"type":"assistant","message":{"content":[{"type":"text","text":"answer"}]}}',
        '{"type":"result","subtype":"success","usage":{"input_tokens":2,"output_tokens":1}}',
    ]))
    client = am.AmpAgentClient()
    chunks = list(client.chat.completions.create(
        model="medium", messages=[{"role": "user", "content": "q"}], stream=True))
    text = "".join(c.choices[0].delta.content for c in chunks
                   if c.choices and c.choices[0].delta and c.choices[0].delta.content)
    assert text == "answer"
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)


# --- backend registration ----------------------------------------------------


def test_backend_registered():
    from evi.backends.amp_agent import AmpAgentBackend
    from evi.backends.factory import KNOWN_BACKENDS, default_base_url

    assert KNOWN_BACKENDS["amp"] is AmpAgentBackend
    assert default_base_url("amp") == ""


def test_get_backend_returns_amp():
    from evi.backends import get_backend

    settings = SimpleNamespace(backend="amp", base_url="", api_key="", request_timeout=120.0)
    assert type(get_backend(settings)).__name__ == "AmpAgentBackend"


def test_backend_list_models():
    from evi.backends.amp_agent import AmpAgentBackend

    b = AmpAgentBackend()
    assert [m.id for m in b.list_models()] == ["medium", "low", "high"]
    assert all(m.backend == "amp" and m.family == "amp" for m in b.list_models())
    assert b.supports_pull() is False


def test_unavailable_when_amp_missing(monkeypatch):
    monkeypatch.setattr(am.shutil, "which", lambda _name: None)
    with pytest.raises(am.AmpUnavailable):
        am._amp_path()
