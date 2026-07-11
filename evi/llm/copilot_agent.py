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

Mechanism: ``copilot -p "<prompt>" --output-format text -s`` runs one
non-interactive turn and prints ONLY the agent's response to stdout (``-s``); its
``--output-format json`` uses a Copilot-specific ``session.*`` event vocabulary
(not the Claude-Code schema), so we take the plain silent text instead. Errors go
to stderr with a non-zero exit. A ``subprocess.run`` timeout is the hang backstop.
Requires the ``copilot`` binary on PATH (``npm i -g @github/copilot``), logged in.
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
    usage_chunk,
)

_TURN_TIMEOUT = 180.0  # wall-clock seconds; hang backstop


class CopilotUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``copilot`` CLI isn't on PATH."""


def _copilot_path() -> str:
    path = shutil.which("copilot")
    if not path:
        raise CopilotUnavailable(
            "The copilot backend needs the GitHub Copilot CLI. Install it with "
            "`npm i -g @github/copilot`, then run `copilot` and `/login` (or "
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


def run_copilot_turn(argv: list[str], *, out, run=None, timeout: float = _TURN_TIMEOUT) -> None:
    """Run one ``copilot -p … --output-format text -s`` process (non-streaming) and
    put its response onto `out` as OpenAI chunks. stdin is closed empty (the prompt
    rides in `argv` as ``-p``). `run` is injectable for testing (defaults to
    ``subprocess.run``, resolved at call time so tests can monkeypatch it)."""
    run = run or subprocess.run
    try:
        res = run(argv, input="", capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=timeout)
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
        _error_message(res.stderr, getattr(res, "returncode", 1))
    )))


class _CopilotDriver:
    """cli_agent driver: render the conversation and run one ``copilot`` turn."""

    def __init__(self):
        self._copilot = _copilot_path()  # fail fast if the CLI is missing

    def run_turn(self, *, model, messages, tools, out):
        prompt = render_transcript(messages or [])
        argv = [self._copilot, "--output-format", "text", "-s", "--no-color"]
        if model:
            argv += ["--model", str(model)]
        argv += ["-p", prompt]          # prompt as arg (non-interactive mode)
        run_copilot_turn(argv, out=out)


class CopilotAgentClient(CliAgentClient):
    """OpenAI-client-shaped Copilot backend over the local ``copilot`` CLI."""

    def __init__(self, model: str = ""):
        super().__init__(_CopilotDriver(), model)
