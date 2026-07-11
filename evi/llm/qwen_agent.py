"""Qwen Code CLI driver — Claude-Code-style backend for the local ``qwen`` CLI
(Alibaba's gemini-cli fork), presented to eVi as an OpenAI ``chat.completions``
client via the shared shim in :mod:`evi.llm.cli_agent`.

Auth is the local ``qwen`` login — a **free** Qwen OAuth (sign in with a qwen.ai /
Alibaba account for a generous free tier, ~2000 req/day) with NO API key. Like
``codex``/``gemini``/``amp`` it's a **chat / delegate** provider: Qwen Code is an
autonomous agent that runs its own tools, so eVi's tools don't route through it.

Mechanism: ``qwen -p "<prompt>" -o json`` returns a JSON ARRAY of Claude-Code-style
events on stdout (``{"type":"assistant","message":{content:[…]}}`` for text, a
terminal ``{"type":"result", usage, result, error}``), so we run it with
``subprocess.run`` and hand the decoded array to the shared Claude-Code parser.
Qwen Code **fails fast** when unauthenticated (a ``result`` event with
``error.message`` — no interactive hang), so no login guard is needed; a
``subprocess.run`` timeout is the only backstop. Requires the ``qwen`` binary on
PATH (``npm i -g @qwen-code/qwen-code``), logged in.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from evi.llm import cli_agent
from evi.llm.cli_agent import (
    CliAgentClient,
    CliUnavailable,
    delta_chunk,
    emit_claude_events,
    render_transcript,
    usage_chunk,
)

_TURN_TIMEOUT = 180.0  # wall-clock seconds; backstop only (qwen fails fast on auth)


class QwenUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``qwen`` CLI isn't on PATH."""


def _qwen_path() -> str:
    path = shutil.which("qwen")
    if not path:
        raise QwenUnavailable(
            "The qwen backend needs the Qwen Code CLI. Install it with "
            "`npm i -g @qwen-code/qwen-code`, then run `qwen` once and choose "
            "'Qwen' to sign in free with your qwen.ai account (~2000 req/day), "
            "or set OPENAI_API_KEY / --auth-type for another provider."
        )
    return path


def _stderr_error(stderr: str, rc: int) -> str:
    """Fallback error when qwen produced no usable stdout events (last non-empty
    stderr line, else the exit code)."""
    for ln in reversed((stderr or "").splitlines()):
        ln = ln.strip()
        if ln:
            return ln[:300]
    return f"qwen exited with code {rc}"


def run_qwen_turn(argv: list[str], *, out, run=None, timeout: float = _TURN_TIMEOUT) -> None:
    """Run one ``qwen -o json`` process (non-streaming) and put the parsed reply
    onto `out` as OpenAI chunks (via the shared Claude-Code parser). stdin is
    closed empty so qwen doesn't block on it. `run` is injectable for testing
    (defaults to ``subprocess.run``, resolved at call time so tests can
    monkeypatch it)."""
    run = run or subprocess.run
    try:
        res = run(argv, input="", capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        out.put(cli_agent.error(QwenUnavailable(
            f"qwen turn exceeded {int(timeout)}s and was terminated."
        )))
        return

    stdout = (res.stdout or "").strip()
    events = None
    if stdout:
        try:
            data = json.loads(stdout)
            events = data if isinstance(data, list) else [data]
        except ValueError:
            events = None

    if events:
        saw_result, error_msg, prompt_toks, comp_toks = emit_claude_events(events, out)
        if error_msg:
            out.put(cli_agent.error(RuntimeError(str(error_msg))))
            return
        if saw_result:
            out.put(delta_chunk(finish_reason="stop"))
            out.put(usage_chunk(prompt_toks, comp_toks))
            return

    # No usable events → surface the error (from stderr / exit code).
    out.put(cli_agent.error(RuntimeError(
        _stderr_error(res.stderr, getattr(res, "returncode", 1))
    )))


class _QwenDriver:
    """cli_agent driver: render the conversation and run one ``qwen`` turn."""

    def __init__(self):
        self._qwen = _qwen_path()  # fail fast if the CLI is missing

    def run_turn(self, *, model, messages, tools, out):
        prompt = render_transcript(messages or [])
        argv = [self._qwen, "-o", "json"]
        if model:
            argv += ["-m", str(model)]
        argv += ["-p", prompt]          # prompt as arg (non-interactive mode)
        run_qwen_turn(argv, out=out)


class QwenAgentClient(CliAgentClient):
    """OpenAI-client-shaped Qwen Code backend over the local ``qwen`` CLI."""

    def __init__(self, model: str = ""):
        super().__init__(_QwenDriver(), model)
