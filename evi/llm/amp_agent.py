"""Sourcegraph Amp CLI driver — Claude-Code-style backend for the local ``amp``
CLI, presented to eVi as an OpenAI ``chat.completions`` client via the shared shim
in :mod:`evi.llm.cli_agent`.

Auth is the local ``amp login`` (an Amp subscription / credit balance) or an
``AMP_API_KEY`` access token — NOT a per-token model API key (Amp does its own
multi-model orchestration behind your account). Like ``codex``/``gemini`` it's a
**chat / delegate** provider: Amp is an autonomous agent that runs its OWN tools
per your configured ``amp permissions``, so eVi's tools don't route through it,
and (unlike ``codex`` read-only) a turn *can* use tools including file edits —
configure ``amp permissions`` to restrict it.

Mechanism: ``amp -x --stream-json`` reads the prompt on stdin and streams
Claude-Code-compatible JSONL on stdout — ``{"type":"assistant","message":{...}}``
carries the agent's text blocks, ``{"type":"result",...}`` carries the final text
+ token usage. Amp's model/behaviour is chosen by
``-m/--mode {low,medium,high,ultra}`` (there is no ``-m <model-id>``).

Login guard: an UNAUTHENTICATED ``amp`` opens an interactive browser login and
blocks forever — which would hang a chat turn. So this driver (a) refuses to spawn
without evidence of auth (``AMP_API_KEY`` or a saved settings file), and (b)
wall-clock bounds every turn with a watchdog that kills the process tree. Requires
the ``amp`` binary on PATH (``npm i -g @ampcode/cli``, the package that replaced
``@sourcegraph/amp``, or Amp's own installer).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from evi.llm import cli_agent
from evi.llm.cli_agent import (
    CliAgentClient,
    CliUnavailable,
    Watchdog,
    delta_chunk,
    emit_claude_events,
    read_lines,
    render_transcript,
    usage_chunk,
)
from evi.proc import kill_tree, spawn

_TURN_TIMEOUT = 180.0  # wall-clock seconds; a stalled login/turn can't hang eVi

_MODES = {"low", "medium", "high", "ultra"}

_INSTALL = cli_agent.CLI_AGENTS["amp"][1]


class AmpUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when ``amp`` is missing or unauthenticated."""


def _amp_path() -> str:
    path = shutil.which("amp")
    if not path:
        raise AmpUnavailable(
            f"The amp backend needs the Amp CLI. Install it with `{_INSTALL}` "
            "(it replaced @sourcegraph/amp) or Amp's installer, then run `amp login` "
            "(Amp subscription) or set AMP_API_KEY (token from "
            "https://ampcode.com/settings)."
        )
    return path


def _settings_file() -> str:
    return os.environ.get("AMP_SETTINGS_FILE") or os.path.join(
        os.path.expanduser("~"), ".config", "amp", "settings.json"
    )


def _require_auth() -> None:
    """Amp opens an interactive browser login when unauthenticated, which would
    block a chat turn indefinitely. Refuse to spawn without evidence of auth —
    ``AMP_API_KEY`` in the env, or a saved settings file from ``amp login``. (Each
    turn is ALSO wall-clock bounded, as a backstop.)"""
    if os.environ.get("AMP_API_KEY"):
        return
    if os.path.isfile(_settings_file()):
        return
    raise AmpUnavailable(
        "The amp backend isn't authenticated. Run `amp login` (Amp subscription) "
        "or set AMP_API_KEY (token from https://ampcode.com/settings). eVi won't "
        "start Amp unauthenticated because its login flow would block the turn."
    )


def run_amp_turn(argv: list[str], prompt: str, *, out, popen=None,
                 timeout: float | None = None) -> None:
    """Spawn one ``amp -x --stream-json`` process, stream its assistant text onto
    `out` as OpenAI chunks (via the shared Claude-Code parser). A watchdog kills
    the process tree after `timeout` so a stalled login can't hang eVi. `popen` is
    injectable for testing (defaults to :func:`evi.proc.spawn`, which resolves
    ``subprocess.Popen`` at call time so tests can monkeypatch it). The process
    tree is killed through :func:`evi.proc.kill_tree` (a ``.cmd`` shim's
    ``node`` child included, and a helper left holding the pipes after amp
    exits)."""
    popen = popen or spawn
    timeout = _TURN_TIMEOUT if timeout is None else timeout
    proc = popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # login prompts / progress noise; errors ride the result event
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    watchdog = Watchdog(proc, timeout)

    def _events():
        """Parse amp's NDJSON stdout lazily so text streams as it arrives.
        Bounded even if the tree kill can't free stdout (see read_lines)."""
        for line in read_lines(proc, watchdog):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue  # non-JSON stray line — ignore defensively

    got_result = False
    prompt_toks = comp_toks = 0
    error_msg = None
    try:
        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except Exception:  # noqa: BLE001 — broken pipe if amp exited early
                pass
        got_result, error_msg, prompt_toks, comp_toks = emit_claude_events(_events(), out)
    except BaseException:
        kill_tree(proc)  # don't leave amp running behind a failed turn
        raise
    finally:
        # read_lines' reader thread closes stdout once it reaches EOF.
        rc = proc.wait()  # still bounded: the watchdog stays armed until amp exits
        watchdog.cancel()

    if watchdog.fired.is_set():
        out.put(cli_agent.error(AmpUnavailable(
            f"amp turn exceeded {int(timeout)}s and was terminated (possible "
            "login/auth stall — run `amp login` or set AMP_API_KEY)."
        )))
        return
    if error_msg:
        out.put(cli_agent.error(RuntimeError(str(error_msg))))
        return
    if not got_result and rc:
        out.put(cli_agent.error(RuntimeError(f"amp exited with code {rc}")))
        return
    out.put(delta_chunk(finish_reason="stop"))
    out.put(usage_chunk(prompt_toks, comp_toks))


class _AmpDriver:
    """cli_agent driver: render the conversation and run one ``amp -x`` turn."""

    def __init__(self):
        self._amp = _amp_path()  # fail fast if the CLI is missing

    def run_turn(self, *, model, messages, tools, out):
        _require_auth()  # fail fast (no hang) if not logged in
        prompt = render_transcript(messages or [])
        argv = [self._amp, "-x", "--stream-json", "--no-color"]
        mode = str(model or "").strip().lower()
        if mode in _MODES:
            argv += ["--mode", mode]
        run_amp_turn(argv, prompt, out=out, timeout=_TURN_TIMEOUT)


class AmpAgentClient(CliAgentClient):
    """OpenAI-client-shaped Amp backend over the local ``amp`` CLI."""

    kind = "amp"

    def __init__(self, model: str = ""):
        super().__init__(_AmpDriver(), model)
