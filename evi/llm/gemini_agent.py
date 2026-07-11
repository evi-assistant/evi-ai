"""Google Gemini CLI driver — Claude-Code-style backend for the local ``gemini``
CLI, presented to eVi as an OpenAI ``chat.completions`` client via the shared shim
in :mod:`evi.llm.cli_agent`.

Auth is the local ``gemini`` login (a Google account gives a generous free tier —
~1000 req/day — with no API key). Like ``codex`` it's a **chat / delegate**
provider: Gemini is an autonomous agent that runs its own tools, so eVi's tools
don't route through it.

Mechanism: ``gemini -p "<prompt>" -o json`` returns a SINGLE JSON object on stdout
(``{"response": "...", "stats": {...}}``) — non-streaming, so we use
``subprocess.run`` (no deadlock, capture both streams). On failure gemini writes
nothing to stdout and an ``{"error": {"message", "code"}}`` object to stderr with a
non-zero exit, so the driver reads stderr for the error. ``--approval-mode yolo``
keeps a chat turn from blocking on a tool-approval prompt. Requires the ``gemini``
binary on PATH (``npm i -g @google/gemini-cli``), logged in.
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
    render_transcript,
    usage_chunk,
)


class GeminiUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``gemini`` CLI isn't on PATH."""


def _gemini_path() -> str:
    path = shutil.which("gemini")
    if not path:
        raise GeminiUnavailable(
            "The gemini backend needs the Google Gemini CLI. Install it with "
            "`npm i -g @google/gemini-cli` and run `gemini` once to log in with "
            "your Google account (free tier), or set GEMINI_API_KEY."
        )
    return path


def _extract_tokens(stats) -> tuple[int, int]:
    """Best-effort (prompt, completion) token counts from gemini's ``stats`` (its
    exact shape varies by version); returns (0, 0) when not found."""
    def walk(o):
        if isinstance(o, dict):
            p = o.get("prompt") or o.get("promptTokenCount") or o.get("input")
            c = (o.get("candidates") or o.get("candidatesTokenCount")
                 or o.get("output") or o.get("completion"))
            if isinstance(p, int) or isinstance(c, int):
                return (p if isinstance(p, int) else 0, c if isinstance(c, int) else 0)
            for v in o.values():
                r = walk(v)
                if r != (0, 0):
                    return r
        return (0, 0)
    return walk(stats or {})


def _error_message(stderr: str, rc: int) -> str:
    """Pull a human error out of gemini's stderr (a JSON ``{error:{message}}`` on
    failure), falling back to the raw text / exit code."""
    stderr = stderr or ""
    # On failure gemini prints a (multi-line, pretty) {"error": {"message": …}}
    # object, usually after a couple of log lines — extract from the first `{`.
    brace = stderr.find("{")
    if brace != -1:
        try:
            obj = json.loads(stderr[brace:])
            msg = (obj.get("error") or {}).get("message")
            if msg:
                return msg
        except (ValueError, AttributeError):
            pass
    # Not JSON — last meaningful, non-noise stderr line.
    for ln in reversed(stderr.splitlines()):
        ln = ln.strip()
        if ln and ln not in "{}" and "YOLO mode" not in ln and "Approval mode" not in ln:
            return ln[:300]
    return f"gemini exited with code {rc}"


def run_gemini_turn(argv: list[str], *, out, run=None) -> None:
    """Run one ``gemini -o json`` process (non-streaming) and put the parsed reply
    onto `out` as OpenAI chunks. The prompt is already in `argv` (as ``-p``);
    stdin is closed empty so gemini doesn't append to or block on it. `run` is
    injectable for testing (defaults to ``subprocess.run``, resolved at call
    time so tests can monkeypatch it)."""
    run = run or subprocess.run
    res = run(argv, input="", capture_output=True, text=True,
              encoding="utf-8", errors="replace")
    stdout = (res.stdout or "").strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None

    if isinstance(data, dict) and isinstance(data.get("response"), str):
        if data.get("error"):
            out.put(cli_agent.error(RuntimeError((data["error"] or {}).get("message", "gemini error"))))
            return
        prompt_toks, comp_toks = _extract_tokens(data.get("stats"))
        if data["response"]:
            out.put(delta_chunk(content=data["response"]))
        out.put(delta_chunk(finish_reason="stop"))
        out.put(usage_chunk(prompt_toks, comp_toks))
        return

    # No usable stdout response → surface the error (from stderr / exit code).
    out.put(cli_agent.error(RuntimeError(_error_message(res.stderr, getattr(res, "returncode", 1)))))


class _GeminiDriver:
    """cli_agent driver: render the conversation and run one ``gemini`` turn."""

    def __init__(self):
        self._gemini = _gemini_path()  # fail fast if the CLI is missing

    def run_turn(self, *, model, messages, tools, out):
        prompt = render_transcript(messages or [])
        argv = [
            self._gemini,
            "-o", "json",
            "--approval-mode", "yolo",  # don't block a chat turn on tool approval
        ]
        if model:
            argv += ["-m", str(model)]
        argv += ["-p", prompt]         # prompt as arg (headless mode)
        run_gemini_turn(argv, out=out)


class GeminiAgentClient(CliAgentClient):
    """OpenAI-client-shaped Gemini backend over the local ``gemini`` CLI."""

    def __init__(self, model: str = ""):
        super().__init__(_GeminiDriver(), model)
