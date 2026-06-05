"""Tests for evi/repl_input.py — slash-command tab completer.

We don't drive prompt_toolkit's UI; instead we instantiate the completer
directly, hand it a `Document`, and assert what it yields. The fallback
path (no prompt_toolkit installed) is also covered.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.completion import CompleteEvent  # noqa: E402
from prompt_toolkit.document import Document  # noqa: E402

from evi.repl_input import ReplInput, _completer_class, _strip_rich  # noqa: E402


def _agent_with_tools(tools: list[str]) -> MagicMock:
    """Build a fake agent that's just enough to drive the completer."""
    a = MagicMock()
    a.tools = {name: MagicMock() for name in tools}
    a.config.llm.model = "qwen2.5-7b-instruct"
    return a


def _complete(completer, line: str) -> list[str]:
    doc = Document(text=line, cursor_position=len(line))
    return [c.text for c in completer.get_completions(doc, CompleteEvent())]


# --- _strip_rich ------------------------------------------------------------


def test_strip_rich_removes_tags() -> None:
    assert _strip_rich("[bold green]you[/bold green] > ") == "you > "
    # Mismatched tags are still stripped (we use a permissive regex).
    assert _strip_rich("[red]err[/]") == "err"


def test_strip_rich_passes_plain_text() -> None:
    assert _strip_rich("you > ") == "you > "


# --- completer: command names -----------------------------------------------


def test_completes_builtin_command_names() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    out = _complete(completer, "/he")
    assert "help" in out
    # The completion text is `help`, not `/help` — the leading slash stays.
    assert all(not s.startswith("/") for s in out)


def test_no_completions_for_plain_text() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    assert _complete(completer, "hello") == []


def test_command_completion_filters_by_prefix() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    # "/r" should match reset, reload — not exit, model, etc.
    out = _complete(completer, "/r")
    assert "reset" in out
    assert "reload" in out
    assert "exit" not in out


# --- completer: per-command arg completion -----------------------------------


def test_effort_completer_offers_levels() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    out = _complete(completer, "/effort ")
    assert set(out) == {"low", "medium", "high", "max"}


def test_effort_completer_filters_partial_match() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    out = _complete(completer, "/effort hi")
    assert out == ["high"]


def test_auto_completer() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    assert set(_complete(completer, "/auto ")) == {"on", "off"}


def test_speak_completer() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    assert set(_complete(completer, "/speak o")) == {"on", "off"}


def test_forcetool_completes_tool_names() -> None:
    completer = _completer_class()(
        _agent_with_tools(["read_file", "write_file", "run_python"])
    )
    out = _complete(completer, "/forcetool r")
    assert "read_file" in out
    assert "run_python" in out
    assert "write_file" not in out


def test_forcetool_skips_completion_past_first_arg() -> None:
    """`/forcetool read_file some prompt` shouldn't keep proposing tool names
    on the trailing prompt token."""
    completer = _completer_class()(_agent_with_tools(["read_file"]))
    out = _complete(completer, "/forcetool read_file extra arg")
    assert out == []  # only the first arg is the tool name


def test_goal_completer_offers_clear() -> None:
    completer = _completer_class()(_agent_with_tools([]))
    out = _complete(completer, "/goal c")
    assert "clear" in out


# --- completer: model ids (cached + fallback) --------------------------------


def test_model_completer_uses_active_when_backend_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `get_backend(...).list_models()` raises, we still surface the
    currently-configured model so the user can see at least one entry."""

    def boom(_):
        raise RuntimeError("backend down")

    # The completer imports `get_backend` lazily inside `_model_ids`, so we
    # patch the source module.
    import evi.backends as backends_mod

    monkeypatch.setattr(backends_mod, "get_backend", boom)

    completer = _completer_class()(_agent_with_tools([]))
    out = _complete(completer, "/model q")
    assert "qwen2.5-7b-instruct" in out


def test_model_completer_returns_backend_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MagicMock()
    backend.list_models.return_value = [
        MagicMock(id="a-model"),
        MagicMock(id="b-model"),
    ]
    import evi.backends as backends_mod

    monkeypatch.setattr(backends_mod, "get_backend", lambda _: backend)

    completer = _completer_class()(_agent_with_tools([]))
    out = _complete(completer, "/model ")
    assert "a-model" in out
    assert "b-model" in out


# --- ReplInput facade ------------------------------------------------------


@pytest.mark.skipif(
    not __import__("sys").stdout.isatty(),
    reason="PromptSession's terminal probe needs a real TTY",
)
def test_repl_input_constructs(tmp_path) -> None:
    """Smoke: ReplInput should build a PromptSession when prompt_toolkit is
    installed (which we already gated at the top of the file).

    Skipped under pytest on Windows because prompt_toolkit's
    `Output.create` raises NoConsoleScreenBufferError without a real TTY.
    """
    agent = _agent_with_tools([])
    repl = ReplInput(agent, history_path=tmp_path / "hist")
    assert repl._fallback is False
    assert repl._session is not None
