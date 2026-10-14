"""Qwen Code CLI driver — Claude-Code-style backend for the local ``qwen`` CLI
(Alibaba's gemini-cli fork), presented to eVi as an OpenAI ``chat.completions``
client via the shared shim in :mod:`evi.llm.cli_agent`.

Auth is the local ``qwen`` login — a **free** Qwen OAuth (sign in with a qwen.ai /
Alibaba account for a generous free tier, ~2000 req/day) with NO API key. Like
``codex``/``gemini``/``amp`` it's a **chat / delegate** provider: Qwen Code is an
autonomous agent that runs its own tools, so eVi's tools don't route through it.

Mechanism: ``qwen -o json --approval-mode default -p "<fixed instruction>"`` with
the rendered conversation on STDIN (qwen puts stdin first and appends ``-p``),
never in argv, because an npm ``.cmd`` shim on Windows runs argv through
``cmd.exe``. It returns a JSON ARRAY of Claude-Code-style events on stdout
(``{"type":"assistant","message":{content:[…]}}`` for text, a terminal
``{"type":"result", usage, result, error}``), which goes to the shared
Claude-Code parser. ``--approval-mode default`` (checked against ``qwen --help``)
makes headless qwen deny its shell and file-write tools, whatever qwen's own
settings default to. Qwen Code **fails fast** when unauthenticated (a ``result``
event with ``error.message`` — no interactive hang); the turn timeout, which
kills the whole process tree, is the backstop. Requires the ``qwen`` binary on
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
    run_cli,
    safe_arg,
    usage_chunk,
)

_TURN_TIMEOUT = 180.0  # wall-clock seconds; backstop only (qwen fails fast on auth)

# Fixed -p text. qwen reads stdin first and appends this, so argv never carries
# the conversation. Plain words only: it passes through cmd.exe.
_HEADLESS_PROMPT = "Reply to the last User message in the conversation above."

_INSTALL = cli_agent.CLI_AGENTS["qwen"][1]


class QwenUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``qwen`` CLI isn't on PATH, is too
    old, or a turn timed out."""


def _qwen_path() -> str:
    path = shutil.which("qwen")
    if not path:
        raise QwenUnavailable(
            f"The qwen backend needs the Qwen Code CLI. Install it with "
            f"`{_INSTALL}`, then run `qwen` once and choose "
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
            if "unknown argument" in ln.lower():
                return (f"This qwen CLI doesn't accept a flag eVi passes; update it with "
                        f"`{_INSTALL}@latest`. (qwen: {ln[:300]})")
            return ln[:300]
    return f"qwen exited with code {rc}"


def run_qwen_turn(argv: list[str], prompt: str = "", *, out, run=None,
                  timeout: float | None = None) -> None:
    """Run one ``qwen -o json`` process (non-streaming) with `prompt` on stdin
    and put the parsed reply onto `out` as OpenAI chunks (via the shared
    Claude-Code parser). A turn longer than `timeout` (default
    ``_TURN_TIMEOUT``) is killed with its process tree. `run` is injectable for
    testing (defaults to :func:`cli_agent.run_cli`, resolved at call time so
    tests can monkeypatch it)."""
    run = run or run_cli
    timeout = _TURN_TIMEOUT if timeout is None else timeout
    try:
        res = run(argv, input=prompt, timeout=timeout)
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


def _mode_flags(path: str) -> list[str]:
    """Flags checked against the installed CLI's ``--help``. A failed probe
    still gets ``--approval-mode default``: an unknown flag fails loudly, a
    missing one could leave qwen's own settings default (even yolo) in charge."""
    text = cli_agent.help_text(path)
    if text and "--output-format" not in text:
        raise QwenUnavailable(
            f"This qwen CLI is too old for eVi (no --output-format). Update it "
            f"with `{_INSTALL}@latest`."
        )
    if text and "--approval-mode" not in text:
        return []
    return ["--approval-mode", "default"]


class _QwenDriver:
    """cli_agent driver: render the conversation and run one ``qwen`` turn."""

    def __init__(self):
        self._qwen = _qwen_path()  # fail fast if the CLI is missing
        self._flags: list[str] | None = None

    def run_turn(self, *, model, messages, tools, out):
        if self._flags is None:
            self._flags = _mode_flags(self._qwen)
        prompt = render_transcript(messages or [])
        argv = [self._qwen, "-o", "json", *self._flags]
        if model:
            argv += ["-m", safe_arg(model)]
        argv += ["-p", _HEADLESS_PROMPT]   # the conversation itself goes on stdin
        run_qwen_turn(argv, prompt, out=out, timeout=_TURN_TIMEOUT)


class QwenAgentClient(CliAgentClient):
    """OpenAI-client-shaped Qwen Code backend over the local ``qwen`` CLI."""

    kind = "qwen"

    def __init__(self, model: str = ""):
        super().__init__(_QwenDriver(), model)
