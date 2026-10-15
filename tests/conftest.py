"""Root test conftest.

Keep the e2e suite (tests/e2e/, Playwright-driven) out of the normal unit run
when the `e2e` extra isn't installed — otherwise collecting it would error in
the default CI job (which installs no Playwright). When Playwright IS present,
the e2e tests are still excluded by default via the `e2e` marker + the
`-m "not e2e"` addopts; run them explicitly with `pytest tests/e2e -m e2e`.
"""

collect_ignore_glob: list[str] = []
try:
    import playwright  # noqa: F401
except ImportError:
    collect_ignore_glob += ["e2e/*"]

# Same guard for the native-desktop suite (tests/desktop_e2e/, Selenium +
# tauri-driver). It's opt-in via the `desktop` marker and Windows-only, but if
# Selenium isn't installed even collecting it would error, so keep it out of the
# default unit run. Run it with `pytest tests/desktop_e2e -m desktop`.
try:
    import selenium  # noqa: F401
except ImportError:
    collect_ignore_glob += ["desktop_e2e/*"]

# Python's http.server calls socket.getfqdn() during server_bind, which does a
# reverse-DNS lookup that hangs for 30s+ on macOS CI runners — enough to trip
# pytest --timeout and fail the whole run (test_a2a / test_federation / test_hooks
# all spin up a throwaway HTTPServer). No eVi code uses getfqdn, so short-circuit
# it in the test process to the passed host (localhost). Harmless everywhere;
# only macOS actually stalled.
import socket as _socket  # noqa: E402

_socket.getfqdn = lambda name="": name or "localhost"


# Session state that tool bodies read lives in ContextVars (the session working
# folder, the hooks and skill scope of the agent whose tool is running). A test that runs a CLI
# command in-process can set one in this very thread and never reset it, and the
# next test on the same xdist worker then resolves paths against a stale folder.
# Give every test a clean slate and restore afterwards. The process-wide hook
# registry a server shares is dropped too, so no test sees another's hooks.
import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any, Iterator  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_session_contextvars(monkeypatch):
    from evi import hooks, skillscope, workdir

    monkeypatch.setattr(hooks, "_SHARED", None)
    cwd_token = workdir.set_cwd(None)
    hooks_token = hooks.bind(None)
    scope_token = skillscope.bind(None)
    try:
        yield
    finally:
        skillscope.unbind(scope_token)
        hooks.unbind(hooks_token)
        workdir.reset(cwd_token)


# ---- fake_llm: a scripted OpenAI-compatible streaming client -----------------
#
# Every agent-loop test drives Agent.chat against this instead of a model. A
# script is a list of turns, one per chat.completions.create() call; each turn
# is a list of stream chunks. Build turns with `fake_llm.text("…")` and
# `fake_llm.call(("write_file", {"path": "x"}), …)`, then
# `client = fake_llm([turn, turn, …])`. `client.requests` records every create()
# kwargs dict (e.g. to check which tool schemas the model was offered).


@dataclass
class _FnDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ToolCallDelta:
    index: int
    id: str | None = None
    function: _FnDelta | None = None


@dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[_ToolCallDelta] | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Chunk:
    choices: list[_Choice]


class _FakeCompletions:
    def __init__(self, scripts: list[list[_Chunk]]) -> None:
        self._scripts = scripts
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Iterator[_Chunk]:
        self.requests.append(kwargs)
        if self.calls >= len(self._scripts):
            raise AssertionError(
                f"fake_llm: script exhausted after {len(self._scripts)} turn(s)"
            )
        script = self._scripts[self.calls]
        self.calls += 1
        return iter(script)


class _FakeClient:
    def __init__(self, scripts: list[list[_Chunk]]) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletions(scripts)})()

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.chat.completions.requests


def _text_chunk(t: str, finish: str | None = None) -> _Chunk:
    return _Chunk([_Choice(_Delta(content=t), finish)])


def llm_text(text: str) -> list[_Chunk]:
    """One model turn that replies with plain text and stops."""
    return [_text_chunk(text, finish="stop")]


def llm_call(*calls: tuple[str, dict | str]) -> list[_Chunk]:
    """One model turn that requests the tool calls given as (name, args), where
    args is a dict or a JSON string."""
    deltas = [
        _ToolCallDelta(
            index=i,
            id=f"call_{i}",
            function=_FnDelta(
                name=name, arguments=args if isinstance(args, str) else json.dumps(args)
            ),
        )
        for i, (name, args) in enumerate(calls)
    ]
    return [_Chunk([_Choice(_Delta(tool_calls=deltas), finish_reason="tool_calls")])]


class _FakeLLM:
    """What the `fake_llm` fixture returns: call it with a script for a client."""

    text = staticmethod(llm_text)
    call = staticmethod(llm_call)

    def __call__(self, script: list[list[_Chunk]]) -> _FakeClient:
        return _FakeClient(script)


@pytest.fixture
def fake_llm() -> _FakeLLM:
    return _FakeLLM()
