"""Google Gemini CLI driver — Claude-Code-style backend for the local ``gemini``
CLI, presented to eVi as an OpenAI ``chat.completions`` client via the shared shim
in :mod:`evi.llm.cli_agent`.

Auth is whatever the local ``gemini`` CLI is set up with: a Gemini API key
(``GEMINI_API_KEY``) or Vertex AI (``GOOGLE_GENAI_USE_VERTEXAI=true`` plus a
project/location or ``GOOGLE_API_KEY``). Since 2026-06-18 Google no longer serves
Gemini CLI to free, Google AI Pro and Ultra "Login with Google" accounts; those
moved to the closed Antigravity CLI (``agy``), which eVi doesn't drive. Like
``codex`` it's a **chat / delegate** provider: Gemini is an autonomous agent that
runs its own tools, so eVi's tools don't route through it.

Mechanism: ``gemini -o json --approval-mode default -p "<fixed instruction>"``
with the rendered conversation on STDIN (gemini puts stdin first and appends
``-p``), never in argv, because an npm ``.cmd`` shim on Windows runs argv
through ``cmd.exe``. It returns a SINGLE JSON object on stdout (``{"response":
"...", "stats": {...}}``). On failure gemini writes nothing to stdout and an
``{"error": {"message", "code"}}`` object to stderr with a non-zero exit, so the
driver reads stderr for the error and maps auth/tier failures to actionable
messages. The approval mode is ``default``, never ``yolo``: headless gemini
treats a tool that would need approval (shell, file writes) as denied, which
keeps a chat turn read-only like ``codex -s read-only``. Flags are checked
against ``gemini --help`` once per binary. Every turn is wall-clock bounded and
a hung CLI is killed with its whole process tree. Requires the ``gemini``
binary on PATH (``npm i -g @google/gemini-cli``).
"""

from __future__ import annotations

import json
import re
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

_TURN_TIMEOUT = 180.0  # wall-clock seconds; a hung CLI is killed with its children

# Fixed -p text. gemini reads stdin first and appends this, so argv never
# carries the conversation. Plain words only: it passes through cmd.exe.
_HEADLESS_PROMPT = "Reply to the last User message in the conversation above."

_INSTALL = cli_agent.CLI_AGENTS["gemini"][1]
_AUTH_HELP = (
    "Set GEMINI_API_KEY (a key from https://aistudio.google.com/apikey), or use "
    "Vertex AI (GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT and "
    "GOOGLE_CLOUD_LOCATION, or GOOGLE_API_KEY), and choose that auth method in "
    "gemini's settings."
)
_CONSUMER_ENDED = (
    "Google stopped serving Gemini CLI to free, Google AI Pro and Ultra sign-ins "
    "on 2026-06-18 (those plans moved to the Antigravity CLI, which eVi doesn't "
    "drive). "
)
_SIGN_IN_WORDS = re.compile(
    r"\b(login|log in|sign-in|sign in|oauth|account|individuals|free tier|"
    r"google ai (pro|ultra))\b"
)
# A retired model is reported as "no longer supported" too, sometimes "for your
# account"; naming a model means it isn't the sign-in cut-off.
_MODEL_WORDS = re.compile(r"\bmodels?\b|\bgemini-\d")


class GeminiUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``gemini`` CLI isn't on PATH, is
    too old, or a turn timed out."""


def _gemini_path() -> str:
    path = shutil.which("gemini")
    if not path:
        raise GeminiUnavailable(
            f"The gemini backend needs the Google Gemini CLI. Install it with "
            f"`{_INSTALL}`, then authenticate it with an API key or Vertex AI. "
            + _AUTH_HELP + " " + _CONSUMER_ENDED.strip()
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


def explain_error(msg: str) -> str:
    """Map gemini's auth / tier / quota failures to a message that says what to
    do; anything else is returned unchanged. The original text is kept."""
    low = (msg or "").lower()
    tail = f" (gemini: {msg})"
    if ("antigravity" in low or "code assist for individuals" in low
            or ("no longer supported" in low and _SIGN_IN_WORDS.search(low)
                and not _MODEL_WORDS.search(low))):
        # "no longer supported" alone is also how a retired MODEL is reported;
        # only a sign-in / account / tier context, with no model named, means
        # the consumer cut-off.
        return _CONSUMER_ENDED + _AUTH_HELP + tail
    if "api key not valid" in low or "api_key_invalid" in low or "invalid api key" in low:
        return ("Gemini rejected the API key. Check GEMINI_API_KEY / GOOGLE_API_KEY "
                "or create a new key at https://aistudio.google.com/apikey." + tail)
    if ("auth method" in low or "authentication" in low or "login required" in low
            or "oauth" in low):
        return "The gemini CLI has no usable sign-in for headless use. " + _AUTH_HELP + (
            " " + _CONSUMER_ENDED.strip()) + tail
    if ("resource_exhausted" in low or "quota" in low or "rate limit" in low
            or re.search(r"\b429\b", low)):
        return ("Gemini quota is used up for this key or project (unpaid keys get a "
                "small daily quota). Wait for the reset, or use a paid key or Vertex AI."
                + tail)
    if ("permission_denied" in low or "permission denied" in low
            or re.search(r"\b403\b", low)):
        return ("Gemini refused the request. With Vertex AI, check the project has the "
                "Vertex AI API enabled and your account can use it; otherwise use a "
                "Gemini API key. " + _CONSUMER_ENDED.strip() + tail)
    if "unknown argument" in low:
        return (f"This gemini CLI doesn't accept a flag eVi passes; update it with "
                f"`{_INSTALL}@latest`." + tail)
    return msg


def run_gemini_turn(argv: list[str], prompt: str = "", *, out, run=None,
                    timeout: float | None = None) -> None:
    """Run one ``gemini -o json`` process (non-streaming) with `prompt` on stdin
    and put the parsed reply onto `out` as OpenAI chunks. A turn longer than
    `timeout` (default ``_TURN_TIMEOUT``) is killed with its process tree. `run`
    is injectable for testing (defaults to :func:`cli_agent.run_cli`, resolved
    at call time so tests can monkeypatch it)."""
    run = run or run_cli
    timeout = _TURN_TIMEOUT if timeout is None else timeout
    try:
        res = run(argv, input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        out.put(cli_agent.error(GeminiUnavailable(
            f"gemini turn exceeded {int(timeout)}s and was terminated."
        )))
        return
    stdout = (res.stdout or "").strip()
    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None

    if isinstance(data, dict) and isinstance(data.get("response"), str):
        if data.get("error"):
            err = data["error"]
            msg = err.get("message", "gemini error") if isinstance(err, dict) else str(err)
            out.put(cli_agent.error(RuntimeError(explain_error(msg))))
            return
        prompt_toks, comp_toks = _extract_tokens(data.get("stats"))
        if data["response"]:
            out.put(delta_chunk(content=data["response"]))
        out.put(delta_chunk(finish_reason="stop"))
        out.put(usage_chunk(prompt_toks, comp_toks))
        return

    # No usable stdout response → surface the error (from stderr / exit code).
    msg = _error_message(res.stderr, getattr(res, "returncode", 1))
    out.put(cli_agent.error(RuntimeError(explain_error(msg))))


def _mode_flags(path: str) -> list[str]:
    """Flags checked against the installed CLI's ``--help``. A failed probe
    still gets ``--approval-mode default``: an unknown flag fails loudly, a
    missing one could leave a settings default in charge."""
    text = cli_agent.help_text(path)
    if text and "--output-format" not in text:
        raise GeminiUnavailable(
            f"This gemini CLI is too old for eVi (no --output-format). Update it "
            f"with `{_INSTALL}@latest`."
        )
    if text and "--approval-mode" not in text:
        return []  # predates approval modes; its default asks, never auto-approves
    return ["--approval-mode", "default"]


class _GeminiDriver:
    """cli_agent driver: render the conversation and run one ``gemini`` turn."""

    def __init__(self):
        self._gemini = _gemini_path()  # fail fast if the CLI is missing
        self._flags: list[str] | None = None

    def run_turn(self, *, model, messages, tools, out):
        if self._flags is None:
            self._flags = _mode_flags(self._gemini)
        prompt = render_transcript(messages or [])
        argv = [self._gemini, "-o", "json", *self._flags]
        if model:
            argv += ["-m", safe_arg(model)]
        argv += ["-p", _HEADLESS_PROMPT]   # the conversation itself goes on stdin
        run_gemini_turn(argv, prompt, out=out, timeout=_TURN_TIMEOUT)


class GeminiAgentClient(CliAgentClient):
    """OpenAI-client-shaped Gemini backend over the local ``gemini`` CLI."""

    kind = "gemini"

    def __init__(self, model: str = ""):
        super().__init__(_GeminiDriver(), model)
