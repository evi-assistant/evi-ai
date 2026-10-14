"""GitHub Copilot CLI driver — Claude-Code-style backend for the local ``copilot``
CLI (GitHub's agentic coding tool, ``@github/copilot``), presented to eVi as an
OpenAI ``chat.completions`` client via the shared shim in :mod:`evi.llm.cli_agent`.

Auth is the local ``copilot`` login (a GitHub Copilot subscription — ``copilot
login`` / ``/login``, or an existing GitHub credential) with no separate model API
key. Like ``codex``/``gemini``/``amp``/``qwen`` it's a **chat / delegate** provider:
Copilot is an autonomous agent that runs its own tools, so eVi's tools don't route
through it. This driver does NOT pass ``--allow-all-tools`` — in non-interactive
mode unapproved tool calls are auto-denied rather than prompting, so a chat turn
stays answer-only (configure permissions / ``COPILOT_ALLOW_ALL`` yourself for more).

Mechanism: the rendered conversation is PIPED to ``copilot --output-format text
-s`` on stdin, which runs one non-interactive turn and prints ONLY the agent's
response to stdout (``-s``). There is deliberately no ``-p``: copilot ignores
piped input when ``-p`` is given, and putting the conversation in argv would
send it through ``cmd.exe`` (the npm ``.cmd`` shim on Windows). Its
``--output-format json`` uses a Copilot-specific ``session.*`` event vocabulary
(not the Claude-Code schema), so we take the plain silent text instead. Errors
go to stderr with a non-zero exit. Optional flags are checked against
``copilot --help`` once per binary. Every turn is wall-clock bounded and a hung
CLI is killed with its whole process tree. Requires the ``copilot`` binary on
PATH (``npm i -g @github/copilot``), logged in.
"""

from __future__ import annotations

import shutil
import subprocess

from evi.llm import cli_agent
from evi.llm.cli_agent import (
    CliAgentClient,
    CliUnavailable,
    delta_chunk,
    render_transcript,
    run_cli,
    safe_arg,
    usage_chunk,
)

_TURN_TIMEOUT = 180.0  # wall-clock seconds; hang backstop

_INSTALL = cli_agent.CLI_AGENTS["copilot"][1]


class CopilotUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``copilot`` CLI isn't on PATH or a
    turn timed out."""


def _copilot_path() -> str:
    path = shutil.which("copilot")
    if not path:
        raise CopilotUnavailable(
            f"The copilot backend needs the GitHub Copilot CLI. Install it with "
            f"`{_INSTALL}`, then run `copilot` and `/login` (or "
            "`copilot login`) with your GitHub Copilot subscription."
        )
    return path


def _error_message(stderr: str, rc: int) -> str:
    """Pull a human error out of copilot's stderr — it leads with an ``Error: …``
    line — falling back to the first non-empty line / the exit code."""
    lines = [ln.strip() for ln in (stderr or "").splitlines()]
    for ln in lines:
        if ln.startswith("Error"):
            return ln[:300]
    for ln in lines:
        if ln:
            return ln[:300]
    return f"copilot exited with code {rc}"


def explain_error(msg: str) -> str:
    """Add what to do to the copilot failures a user can fix; anything else is
    returned unchanged."""
    low = (msg or "").lower()
    if "raw mode" in low or "not a tty" in low or "stdin" in low:
        return (f"This copilot CLI didn't take the prompt from stdin, which eVi needs. "
                f"Update it with `{_INSTALL}@latest`. (copilot: {msg})")
    if ("not logged in" in low or "no authentication" in low or "authenticat" in low
            or "login" in low):
        return ("The copilot CLI isn't signed in. Run `copilot` and `/login` with a "
                f"GitHub Copilot subscription. (copilot: {msg})")
    if "unknown option" in low or "unknown argument" in low:
        return (f"This copilot CLI doesn't accept a flag eVi passes; update it with "
                f"`{_INSTALL}@latest`. (copilot: {msg})")
    return msg


def run_copilot_turn(argv: list[str], prompt: str = "", *, out, run=None,
                     timeout: float | None = None) -> None:
    """Run one ``copilot --output-format text -s`` process (non-streaming) with
    `prompt` on stdin and put its response onto `out` as OpenAI chunks. A turn
    longer than `timeout` (default ``_TURN_TIMEOUT``) is killed with its process
    tree. `run` is injectable for testing (defaults to :func:`cli_agent.run_cli`,
    resolved at call time so tests can monkeypatch it)."""
    run = run or run_cli
    timeout = _TURN_TIMEOUT if timeout is None else timeout
    try:
        res = run(argv, input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        out.put(cli_agent.error(CopilotUnavailable(
            f"copilot turn exceeded {int(timeout)}s and was terminated."
        )))
        return

    stdout = (res.stdout or "").strip()
    if stdout:
        out.put(delta_chunk(content=stdout))
        out.put(delta_chunk(finish_reason="stop"))
        out.put(usage_chunk(0, 0))  # silent text mode carries no token counts
        return

    # No response text → surface the error (from stderr / exit code).
    out.put(cli_agent.error(RuntimeError(
        explain_error(_error_message(res.stderr, getattr(res, "returncode", 1)))
    )))


def _flags(path: str) -> list[str]:
    """Output flags, with the optional ones checked against ``copilot --help``
    (kept when the probe fails, as before)."""
    text = cli_agent.help_text(path)
    flags = []
    if not text or "--output-format" in text:
        flags += ["--output-format", "text"]
    flags += ["-s", "--no-color"]
    if "--no-ask-user" in text:
        flags.append("--no-ask-user")  # nobody is there to answer a question
    return flags


class _CopilotDriver:
    """cli_agent driver: render the conversation and run one ``copilot`` turn."""

    def __init__(self):
        self._copilot = _copilot_path()  # fail fast if the CLI is missing
        self._flags: list[str] | None = None

    def run_turn(self, *, model, messages, tools, out):
        if self._flags is None:
            self._flags = _flags(self._copilot)
        prompt = render_transcript(messages or [])
        argv = [self._copilot, *self._flags]
        if model:
            argv += ["--model", safe_arg(model)]
        # No -p: the conversation is piped on stdin (copilot ignores stdin with -p).
        run_copilot_turn(argv, prompt, out=out, timeout=_TURN_TIMEOUT)


class CopilotAgentClient(CliAgentClient):
    """OpenAI-client-shaped Copilot backend over the local ``copilot`` CLI."""

    kind = "copilot"

    def __init__(self, model: str = ""):
        super().__init__(_CopilotDriver(), model)
