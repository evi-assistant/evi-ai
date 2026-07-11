"""OpenAI Codex CLI driver — Claude-Code-style backend for the local ``codex``
CLI, presented to eVi as an OpenAI ``chat.completions`` client via the shared shim
in :mod:`evi.llm.cli_agent`.

Auth is the local ``codex login`` (ChatGPT Plus/Pro/Business subscription) — no
``ANTHROPIC``/``OPENAI`` API key. Unlike ``claude_agent`` (where eVi drives its own
tools via a ``can_use_tool`` interceptor), Codex is an **autonomous** agent that
runs its OWN tools inside its sandbox; ``codex exec`` streams events and prints the
final agent message. So this backend is a **chat / delegate** provider: eVi's tools
do NOT route through it (they're ignored), and eVi streams Codex's answer. Run it
read-only by default so a chat turn can't edit files.

Mechanism: spawn ``codex exec --json`` (JSONL events on stdout, Rust logs on
stderr), feed the rendered conversation on stdin, and map events →  OpenAI chunks:
``item.completed`` with ``item.type == "agent_message"`` → text; ``reasoning`` →
``<think>``; ``turn.completed.usage`` → token counts; ``turn.failed`` → error.
Transient top-level ``error`` events are retries and are ignored. Requires the
``codex`` binary on PATH (``npm i -g @openai/codex``), logged in.
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
    flatten_content,
    usage_chunk,
)


class CodexUnavailable(CliUnavailable):
    """Raised (lazily, at call time) when the ``codex`` CLI isn't on PATH."""


def _codex_path() -> str:
    path = shutil.which("codex")
    if not path:
        raise CodexUnavailable(
            "The codex backend needs the OpenAI Codex CLI. Install it with "
            "`npm i -g @openai/codex` (or brew install --cask codex) and run "
            "`codex login` with your ChatGPT Plus/Pro plan."
        )
    return path


def render_prompt(messages: list[dict]) -> str:
    """OpenAI messages → a single text prompt for ``codex exec`` (Codex takes one
    prompt; eVi resends full history each turn, so this is stateless). System
    messages become a preamble; the conversation is a labelled transcript with any
    prior tool activity rendered as text."""
    system: list[str] = []
    convo: list[str] = []
    id_to_name: dict[str, str] = {}
    for m in messages:
        role = m.get("role")
        content = flatten_content(m.get("content"))
        if role == "system":
            if content:
                system.append(content)
        elif role == "tool":
            name = id_to_name.get(m.get("tool_call_id") or "", "tool")
            convo.append(f"[tool {name} returned: {content}]")
        elif role == "assistant":
            parts = [content] if content else []
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                nm = fn.get("name") or "tool"
                id_to_name[tc.get("id") or ""] = nm
                parts.append(f"[called {nm}({fn.get('arguments') or '{}'})]")
            if parts:
                convo.append("Assistant: " + "\n".join(parts))
        elif content:  # user (default)
            convo.append("User: " + content)
    text = ("\n\n".join(system) + "\n\n") if system else ""
    return (text + "\n".join(convo)).strip()


# Item types Codex emits for its OWN agentic activity — surfaced neither as the
# reply nor as reasoning (eVi just wants the final answer).
_ACTIVITY_ITEMS = {"command_execution", "file_changes", "mcp_tool_calls",
                   "web_search", "plan_updates", "todo_list", "error"}


def run_codex_turn(argv: list[str], prompt: str, *, out, popen=None) -> None:
    """Spawn one ``codex exec --json`` process, stream its stdout JSONL onto
    `out` as OpenAI chunks. `popen` is injectable for testing (defaults to
    ``subprocess.Popen``, resolved at call time so tests can monkeypatch it)."""
    popen = popen or subprocess.Popen
    proc = popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # Rust tracing noise; the fatal error rides turn.failed
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    finished = False
    prompt_toks = comp_toks = 0
    try:
        if proc.stdin is not None:
            proc.stdin.write(prompt)
            proc.stdin.close()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # non-JSON stray line — ignore defensively
            etype = ev.get("type")
            if etype == "item.completed":
                item = ev.get("item") or {}
                itype = item.get("type")
                text = item.get("text") or ""
                if itype == "agent_message" and text:
                    out.put(delta_chunk(content=text))
                elif itype == "reasoning" and text:
                    out.put(delta_chunk(content=f"<think>{text}</think>"))
                # _ACTIVITY_ITEMS and anything else: ignore (Codex's own tools).
            elif etype == "turn.completed":
                u = ev.get("usage") or {}
                prompt_toks = u.get("input_tokens", 0) or 0
                comp_toks = u.get("output_tokens", 0) or 0
                out.put(delta_chunk(finish_reason="stop"))
                out.put(usage_chunk(prompt_toks, comp_toks))
                finished = True
            elif etype == "turn.failed":
                msg = (ev.get("error") or {}).get("message") or "codex turn failed"
                out.put(cli_agent.error(RuntimeError(msg)))
                finished = True
                return
            # thread.started / turn.started / item.started / item.updated /
            # transient top-level "error" (retries): ignore.
    finally:
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        rc = proc.wait()

    if not finished:
        # Process ended without turn.completed/turn.failed (crash / killed).
        if rc:
            out.put(cli_agent.error(RuntimeError(f"codex exited with code {rc}")))
        else:
            out.put(delta_chunk(finish_reason="stop"))
            out.put(usage_chunk(0, 0))


class _CodexDriver:
    """cli_agent driver: render the conversation and run one ``codex exec`` turn."""

    def __init__(self):
        self._codex = _codex_path()  # fail fast if the CLI is missing

    def run_turn(self, *, model, messages, tools, out):
        prompt = render_prompt(messages or [])
        argv = [
            self._codex, "exec", "--json",
            "--skip-git-repo-check",   # run anywhere, not just inside a git repo
            "--color", "never",
            "-s", "read-only",         # a chat turn must not edit files
            "--ephemeral",             # don't persist session files
        ]
        if model:
            argv += ["-m", str(model)]
        argv.append("-")               # read the prompt from stdin
        run_codex_turn(argv, prompt, out=out)


class CodexAgentClient(CliAgentClient):
    """OpenAI-client-shaped Codex backend over the local ``codex`` CLI."""

    def __init__(self, model: str = ""):
        super().__init__(_CodexDriver(), model)
