"""`evi run` prints an answer holding a lone surrogate (polish-small-fixes,
part 3).

A model's broken emoji reaches eVi as a lone surrogate (``"\\ud83d"``), which
no encoding can write. `evi run --format text` printed the hand-off with a
plain print(res.text), so after the run had finished it raised
UnicodeEncodeError: the answer never reached stdout and the process exited 1
with a traceback instead of the run's exit code. Now the answer goes out with
what can't be encoded escaped (``\\ud83d``), as `evi task start` already did,
and the exit code is the run's. The JSON formats were already safe (json.dumps
escapes it); the tests hold them to that.

Agents run against the shared fake_llm client (tests/conftest.py) through
CliRunner, whose stdout is strict UTF-8 like a real UTF-8 terminal; eVi's
home, config and transcripts are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import json

import pytest

from evi import hooks as hooks_mod
from evi.config import Config
from evi.llm.agent import Agent
from evi.tools.base import Tool
from tests.conftest import _Chunk, _Choice, _Delta, llm_call, llm_text

ANSWER = "Fixed the parser \ud83d and the tests pass"
ESCAPED = "Fixed the parser \\ud83d and the tests pass"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, transcripts, backends and trust list under
    tmp_path, no safe mode, and a process cwd of its own."""
    import evi.config as config_mod
    import evi.sessions as sessions_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.setattr(sessions_mod, "TRANSCRIPTS_DIR", evi_home / "transcripts")
    monkeypatch.setattr(sessions_mod, "HANDOFF_DIR", evi_home / "handoff")
    monkeypatch.setattr("evi.backends.registry.BACKENDS_PATH", evi_home / "backends.json")
    (evi_home / "config.toml").write_text(
        "[tools]\nmemory = false\nskills = false\ntranscripts = false\n", encoding="utf-8")
    elsewhere = tmp_path / "process-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return evi_home


def _agent(client, tools=()) -> Agent:
    return Agent(client=client, config=Config.load(), tools=list(tools))


def _echo() -> Tool:
    return Tool(name="echo", description="echo its argument", category="fs",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                func=lambda x="": f"echo:{x}")


def _run(monkeypatch, agent: Agent, *args: str):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    monkeypatch.setattr(cli_main, "_build_agent", lambda *_a, **_k: agent)
    return CliRunner().invoke(cli_main.app, ["run", *args])


def test_text_format_prints_the_answer_escaped_and_exits_0(home, monkeypatch, fake_llm) -> None:
    r = _run(monkeypatch, _agent(fake_llm([llm_text(ANSWER)])), "fix the parser")
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)
    assert r.exit_code == 0, r.output
    r.stdout_bytes.decode("utf-8")  # strict: what went out is UTF-8
    assert r.stdout == ESCAPED + "\n"  # the whole answer, on stdout, once
    assert "UnicodeEncodeError" not in r.output


def test_text_format_keeps_the_runs_exit_code(home, monkeypatch, fake_llm) -> None:
    """A run that stops on max_turns (exit 3) after replying with a surrogate:
    its text is printed, escaped, and the exit code is still 3, not 1."""
    said = [_Chunk([_Choice(_Delta(content="Looking \ud83d first"))]),
            *llm_call(("echo", {"x": "1"}))]
    agent = _agent(fake_llm([said, llm_call(("echo", {"x": "2"}))]), [_echo()])
    r = _run(monkeypatch, agent, "--max-turns", "2", "loop")
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)
    assert r.exit_code == 3, r.output
    assert r.stdout == "Looking \\ud83d first\n"
    assert "max-turns" in r.stderr


def test_an_answer_stdout_can_encode_is_printed_as_it_is(home, monkeypatch, fake_llm) -> None:
    """Nothing changes for an answer without one: a real emoji goes out as
    itself, not escaped."""
    r = _run(monkeypatch, _agent(fake_llm([llm_text("Done \U0001f600 ok")])), "go")
    assert r.exit_code == 0, r.output
    assert r.stdout == "Done \U0001f600 ok\n"


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_json_formats_stay_valid_json(home, monkeypatch, fake_llm, fmt) -> None:
    r = _run(monkeypatch, _agent(fake_llm([llm_text(ANSWER)])), "--format", fmt, "fix it")
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)
    assert r.exit_code == 0, r.output
    raw = r.stdout_bytes.decode("utf-8")
    lines = [json.loads(line) for line in raw.splitlines()]  # every line parses
    report = lines[-1]
    assert report["stop_reason"] == "done"
    assert report["text"] == ANSWER  # the escape reads back as the same string
    if fmt == "json":
        assert len(lines) == 1
    else:
        assert report["kind"] == "RunResult"
        streamed = "".join(f.get("text", "") for f in lines if f["kind"] == "TextDelta")
        assert streamed == ANSWER


def test_print_answer_escapes_for_a_narrow_stdout(monkeypatch) -> None:
    """The helper on a stdout whose encoding lacks a character (a Windows
    console in cp1252): that character is escaped too, the rest kept."""
    import io
    import sys

    import evi.apps.cli.main as cli_main

    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", out)
    cli_main._print_answer("café ☃ \ud83d done")
    out.flush()
    assert buf.getvalue().decode("cp1252") == "café \\u2603 \\ud83d done\n"


@pytest.mark.parametrize(("answer", "printed"), [
    ("skull \udc80 here \U0001f600", "skull \\udc80 here \U0001f600"),
    ("\udcff\udc80 twice, café", "\\udcff\\udc80 twice, café"),
])
def test_print_answer_escapes_on_a_surrogateescape_stdout(monkeypatch, answer, printed) -> None:
    """In UTF-8 mode (PYTHONUTF8=1) or a POSIX C locale stdout's error handler
    is surrogateescape, which raises for nothing in U+DC80-U+DCFF: it writes
    the raw byte 0x80-0xFF instead. The low half of a broken 💀 (U+DC80) must
    still go out escaped, so stdout stays valid UTF-8."""
    import io
    import sys

    import evi.apps.cli.main as cli_main

    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="utf-8", errors="surrogateescape", newline="\n")
    monkeypatch.setattr(sys, "stdout", out)
    cli_main._print_answer(answer)
    out.flush()
    assert buf.getvalue().decode("utf-8") == printed + "\n"  # strict: no raw 0x80 byte


def test_print_answer_on_a_stdout_naming_an_unknown_codec(monkeypatch) -> None:
    """A stand-in stdout whose `encoding` names no codec Python knows: the
    answer is held to UTF-8 rather than lost to a LookupError."""
    import io
    import sys

    import evi.apps.cli.main as cli_main

    class _Out(io.StringIO):
        encoding = "no-such-codec"

    out = _Out()
    monkeypatch.setattr(sys, "stdout", out)
    cli_main._print_answer("ok \udc80 \U0001f600")
    assert out.getvalue() == "ok \\udc80 \U0001f600\n"
