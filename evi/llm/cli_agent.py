"""Shared shim core for CLI-agent backends.

eVi backends must expose an OpenAI ``chat.completions.create(...)`` surface, but
several AI coding CLIs (Claude Code, OpenAI Codex, …) are agent loops reached over
a LOCAL CLI/SDK using a subscription login (no API key) — not OpenAI-compatible.
To present one as an eVi backend we run it and adapt its streamed output into the
chat-completions chunk shape eVi's agent loop consumes. This module holds the
parts that are IDENTICAL across every such CLI:

* OpenAI-shaped duck-typed chunks/responses — only the fields eVi's loop reads
  (``chunk.choices[0].delta.content`` / ``.tool_calls`` / ``.finish_reason`` and a
  final ``chunk.usage``; non-stream ``resp.choices[i].message.content``).
* The async/subprocess → SYNC bridge: run one turn on a background thread that
  pushes chunks onto a queue; a sync generator drains it (eVi's loop is sync).
* The ``.chat.completions.create(...)`` client shell (stream + non-stream + ``n``),
  parameterised by a per-CLI DRIVER.

A per-CLI backend supplies a driver with ``run_turn(*, model, messages, tools,
out)`` that runs ONE turn (spawning its subprocess / SDK query however it likes)
and puts chunk objects on ``out``, finishing with a finish chunk + a usage chunk
(or ``cli_agent.error(exc)``). See ``evi/llm/claude_agent.py`` (claude-agent-sdk
driver) and ``evi/llm/codex_agent.py`` (``codex exec --json`` subprocess driver).

Subprocess drivers share :func:`run_cli` / :class:`Watchdog` /
:func:`read_lines` (bounded turns that kill the whole process tree),
:func:`help_text` (flag probes against the installed CLI's ``--help``) and
:func:`safe_arg`. The conversation always goes to the CLI on stdin, never in
argv: an npm-installed CLI is a ``.cmd`` shim on Windows, and CreateProcess
runs those through ``cmd.exe``, which caps the command line at 8191 chars,
stops at a newline and does not honour the quoting Python applies, so ``&``,
``|``, ``%`` and quotes in argv can run commands.

:func:`unattended` marks a channel / scheduled / webhook run; delegate CLI
agents refuse to run inside one unless ``[llm] allow_cli_agent_unattended`` is
set.
"""

from __future__ import annotations

import contextvars
import os
import queue
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Callable, Iterator

from evi.proc import kill_tree, spawn

_SENTINEL = object()


class CliUnavailable(RuntimeError):
    """A driver's CLI/SDK isn't installed or isn't logged in. Raised lazily at
    call time so eVi runs fine until the backend is actually selected."""


class CliAgentUnattended(CliUnavailable):
    """A delegate CLI agent was asked to run inside an unattended entry point
    (channel, scheduler, routine, workflow) without the user's opt-in."""


# kind -> (binary, install command). One table for the drivers' hints and
# `evi doctor`; the onboarding copy should read from here too.
CLI_AGENTS: dict[str, tuple[str, str]] = {
    "claude_agent": ("claude", "pip install 'evi-assistant[claude-agent]'"),
    "codex": ("codex", "npm i -g @openai/codex"),
    "gemini": ("gemini", "npm i -g @google/gemini-cli"),
    "amp": ("amp", "npm i -g @ampcode/cli"),
    "qwen": ("qwen", "npm i -g @qwen-code/qwen-code"),
    "copilot": ("copilot", "npm i -g @github/copilot"),
}

# Delegate-style backends: the CLI runs its OWN tools (shell, file edits, web
# access) under its own approval rules, so eVi's permission engine, hard_deny
# list and protected paths never see them. claude_agent is not one: Claude
# Code's built-in tools are off (tools=[]) and every tool call comes back to eVi.
DELEGATE_BACKENDS: frozenset[str] = frozenset({"codex", "gemini", "amp", "qwen", "copilot"})


def flatten_content(content) -> str:
    """OpenAI message content (str, or a list of ``{type:text,text}`` parts) → str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    out.append(part["text"])
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return str(content)


def render_transcript(messages: list[dict]) -> str:
    """OpenAI messages → a single text prompt for CLIs that take ONE prompt (Codex,
    Gemini, …). System messages become a preamble; the conversation is a labelled
    transcript with any prior tool activity rendered as text (eVi resends full
    history each turn, so this is stateless)."""
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


# --- OpenAI-shaped duck types (only the fields eVi's agent loop reads) --------


def delta_chunk(content: str | None = None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, logprobs=None)
    return SimpleNamespace(choices=[choice], usage=None)


def usage_chunk(prompt_tokens: int = 0, completion_tokens: int = 0):
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    payload = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    return SimpleNamespace(choices=[], usage=payload)


def tool_call_delta(idx: int, call_id: str, name: str, arguments: str):
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=idx, id=call_id, function=fn, type="function")
    return delta_chunk(tool_calls=[tc])


def error(exc: BaseException):
    """A driver puts this on its queue to surface a fatal turn error to eVi."""
    return ("__error__", exc)


# --- running one CLI process ---------------------------------------------------

_KILL_GRACE = 10.0   # seconds communicate() may outlive the watchdog's kill
_EXIT_GRACE = 2.0    # seconds the pipes may stay open after the CLI itself exits
_POLL = 0.1          # watchdog poll interval, seconds
_HELP_TIMEOUT = 20.0
# A model id / mode is the only config value that reaches argv. Anything outside
# this set could be read by cmd.exe as syntax when the CLI is a .cmd shim.
_SAFE_ARG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]*")


def safe_arg(value, what: str = "model") -> str:
    """Return `value` as a str if it is safe to put in a CLI argv, else raise
    ``ValueError``. Only plain ids pass (letters, digits and ``. _ : / @ + = -``),
    so a hostile or mistyped config value can't smuggle ``&``/``|``/``%``/quotes
    through cmd.exe."""
    text = str(value)
    if len(text) > 200 or not _SAFE_ARG.fullmatch(text):
        raise ValueError(
            f"Refusing {what} {text[:60]!r}: only letters, digits and . _ : / @ + = - "
            "are allowed in a value passed to a CLI agent."
        )
    return text


class Watchdog:
    """Bound one CLI run until :meth:`cancel`. Once `timeout` seconds pass,
    `proc`'s whole process tree is killed and ``fired`` is set. Once `proc`
    itself has exited but the caller still hasn't cancelled `exit_grace`
    seconds later, the caller is stuck on pipes that something the CLI left
    behind (an MCP server, a helper) still holds: the tree is killed then too,
    which reaches that leftover through :func:`evi.proc.spawn`'s job / process
    group, but ``fired`` stays clear because the CLI's answer is complete.
    ``killed`` is set (and ``killed_at`` stamped) after either kill."""

    def __init__(self, proc, timeout: float, *, exit_grace: float = _EXIT_GRACE):
        self.fired = threading.Event()
        self.killed = threading.Event()
        self.killed_at: float | None = None
        self._done = threading.Event()
        self._proc = proc
        self._timeout = max(0.0, float(timeout))
        self._grace = max(0.0, float(exit_grace))
        threading.Thread(target=self._watch, name="cli-agent-watchdog", daemon=True).start()

    def _exited(self) -> bool:
        try:
            return self._proc.poll() is not None
        except Exception:  # noqa: BLE001 — a stand-in without poll(): still running
            return False

    def _watch(self) -> None:
        deadline = time.monotonic() + self._timeout
        exited_at = None
        while not self._done.wait(max(0.0, min(_POLL, deadline - time.monotonic()))):
            now = time.monotonic()
            if exited_at is None and self._exited():
                exited_at = now
            if exited_at is not None and now - exited_at >= self._grace:
                self._kill()   # leftovers holding the pipes
                return
            if now >= deadline:
                if exited_at is None:
                    self.fired.set()
                self._kill()
                return

    def _kill(self) -> None:
        kill_tree(self._proc)
        self.killed_at = time.monotonic()
        self.killed.set()

    def cancel(self) -> None:
        self._done.set()


def read_lines(proc, dog: Watchdog, *, grace: float | None = None) -> Iterator[str]:
    """Yield `proc`'s stdout lines as they arrive, for drivers that stream it.

    A daemon thread does the reading, so the caller can't be stuck on the pipe:
    once `dog` has killed the tree and the pipe still hasn't reached EOF
    `grace` seconds later (default ``_KILL_GRACE``), something the kill
    couldn't reach still holds it (a process that broke away from the job or
    left the process group) and iteration stops. The reader thread is left to
    finish on its own and closes stdout when it does."""
    grace = _KILL_GRACE if grace is None else grace
    lines: queue.Queue = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:  # noqa: BLE001 — a pipe closed under us is just EOF
            pass
        finally:
            lines.put(_SENTINEL)
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=pump, name="cli-agent-stdout", daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=_POLL)
        except queue.Empty:
            # killed_at is stamped before killed is set.
            if dog.killed.is_set() and time.monotonic() - dog.killed_at >= grace:
                return
            continue
        if line is _SENTINEL:
            return
        yield line


def _decode(data) -> str:
    if isinstance(data, str):
        return data
    return (data or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")


def run_cli(argv: list[str], *, input: str = "", timeout: float,
            popen=None) -> subprocess.CompletedProcess:
    """Run one CLI process to completion: `input` goes on stdin as exact UTF-8
    bytes (no newline translation), stdout and stderr come back as text. After
    `timeout` seconds the whole process tree is killed and
    ``subprocess.TimeoutExpired`` is raised.

    ``subprocess.run(timeout=)`` is not enough here: it kills only the direct
    child (``cmd.exe`` for a ``.cmd`` shim), then waits for pipes the orphaned
    ``node`` still holds, and on Windows its stdin write isn't bounded by the
    timeout at all. The watchdog kill closes every pipe end, so both unblock.
    The process is started with :func:`evi.proc.spawn`, so a helper the CLI
    leaves holding the pipes after it exits is killed after a short grace and
    the answer is returned. `popen` is injectable for testing."""
    popen = popen or spawn
    proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dog = Watchdog(proc, timeout)
    try:
        out, err = proc.communicate((input or "").encode("utf-8"),
                                    timeout=timeout + _KILL_GRACE)
    except subprocess.TimeoutExpired:
        # The kill didn't free the pipes (a descendant we couldn't kill).
        kill_tree(proc)
        raise subprocess.TimeoutExpired(argv, timeout) from None
    finally:
        dog.cancel()
    out, err = _decode(out), _decode(err)
    if dog.fired.is_set():
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


_help_cache: dict[tuple[str, float], str] = {}
_help_lock = threading.Lock()


def help_text(path: str, *, run=None) -> str:
    """The installed CLI's ``--help`` output (stdout + stderr), probed once per
    binary version and cached; ``""`` when the probe fails. Drivers check flags
    against it so a vendor CLI that renamed or dropped one gets a clear error
    instead of an "Unknown argument" mid-chat. `run` is injectable for testing."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = (path, 0.0)
    with _help_lock:
        if key in _help_cache:
            return _help_cache[key]
    run = run or run_cli
    try:
        res = run([path, "--help"], input="", timeout=_HELP_TIMEOUT)
        text = f"{res.stdout or ''}\n{res.stderr or ''}".strip()
    except Exception:  # noqa: BLE001 — a failed probe is just "unknown"
        text = ""
    with _help_lock:
        _help_cache[key] = text
    return text


# --- Claude-Code stream-json events (Amp, Qwen Code, `claude -p --stream-json`) --
#
# Several CLIs emit the SAME event schema Claude Code's `--output-format stream-json`
# uses: `{"type":"assistant","message":{"content":[{"type":"text","text":…}, …]}}`
# for the agent's messages and a terminal `{"type":"result", subtype, is_error,
# usage:{input_tokens,output_tokens,…}, result:"final text", error:{message}}`. The
# parsing below is identical for all of them, so it lives here.


def cc_usage(u) -> tuple[int, int]:
    """(prompt, completion) tokens from a Claude-Code-style ``usage`` object
    (Anthropic field names, with cache buckets); (0, 0) when absent."""
    u = u or {}
    prompt = (
        (u.get("input_tokens", 0) or 0)
        + (u.get("cache_read_input_tokens", 0) or 0)
        + (u.get("cache_creation_input_tokens", 0) or 0)
    )
    if not prompt:
        prompt = u.get("prompt_tokens", 0) or 0
    completion = (u.get("output_tokens", 0) or 0) or (u.get("completion_tokens", 0) or 0)
    return int(prompt), int(completion)


def cc_error_message(ev: dict) -> str:
    """Human error string from a Claude-Code ``result`` error event."""
    err = ev.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if isinstance(err, str) and err:
        return err
    res = ev.get("result")
    if isinstance(res, str) and res:
        return res
    return "turn failed"


def emit_claude_events(events, out) -> tuple[bool, str | None, int, int]:
    """Drive `out` from Claude-Code-style stream events (an iterable of parsed
    dicts — a live generator for a streaming CLI, or a decoded array for a
    batch one). Emits each ``assistant`` message's text blocks as delta chunks
    and, if nothing streamed, the terminal ``result``'s final text. Returns
    ``(saw_result, error_message, prompt_tokens, completion_tokens)``; the caller
    adds the finish + usage chunks, or surfaces the error."""
    streamed_any = False
    saw_result = False
    prompt_toks = comp_toks = 0
    error_msg = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "assistant":
            msg = ev.get("message") or {}
            for block in (msg.get("content") or []):
                if (isinstance(block, dict) and block.get("type") == "text"
                        and block.get("text")):
                    out.put(delta_chunk(content=block["text"]))
                    streamed_any = True
            # tool_use blocks: the CLI's own tools — ignore.
        elif etype == "result":
            saw_result = True
            prompt_toks, comp_toks = cc_usage(ev.get("usage"))
            is_err = bool(ev.get("is_error")) or (ev.get("subtype") not in (None, "success"))
            if is_err:
                error_msg = cc_error_message(ev)
            elif not streamed_any and isinstance(ev.get("result"), str) and ev.get("result"):
                out.put(delta_chunk(content=ev["result"]))
                streamed_any = True
        # system/init, user (tool results), and anything else: ignore.
    return saw_result, error_msg, prompt_toks, comp_toks


# --- async / subprocess -> sync bridge ---------------------------------------


def spawn_turn(run_turn: Callable[[queue.Queue], None]) -> queue.Queue:
    """Run ``run_turn(out)`` on a daemon thread; return the queue it fills. The
    sentinel + error tuple are handled by :func:`drain`."""
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            run_turn(q)
        except Exception as exc:  # noqa: BLE001 — a turn failure can't crash eVi
            q.put(("__error__", exc))
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=worker, name="cli-agent-turn", daemon=True).start()
    return q


def drain(q: queue.Queue):
    """Yield chunk objects off the queue until the sentinel; re-raise a driver
    error tuple so it propagates like an OpenAI SDK exception."""
    while True:
        item = q.get()
        if item is _SENTINEL:
            return
        if isinstance(item, tuple) and item and item[0] == "__error__":
            raise item[1]
        yield item


# --- unattended entry points --------------------------------------------------
#
# With nobody watching (a channel message, a scheduled task, a routine webhook,
# a workflow step) a delegate CLI agent's own tools would run with no approval
# eVi can enforce, so entry points wrap their run in ``unattended(...)``. The
# check is made up front (fail before any agent work, covering the [llm]
# backend and every [models] *_backend a tool may route to) and by every
# delegate client at call time: one called in the block's context, or built
# inside the block and called from any thread (a backend switched mid-run, or
# an agent drained on a worker thread, is still caught). The opt-in is a user
# config edit, ``[llm] allow_cli_agent_unattended = true``.
#
# Wrapped in this block: scheduled tasks (evi/scheduler.py), subagents with no
# parent permission UI and so dream and `evi team` (evi/llm/subagent.py),
# ultracode stages (evi/ultracode.py), `evi run` / `evi batch`, and the web's
# workflow, recipe, routine, ultracode, channel push, federation and A2A runs.

_UNATTENDED: contextvars.ContextVar[tuple[str, bool] | None] = contextvars.ContextVar(
    "evi_cli_agent_unattended", default=None
)


def unattended_refusal(kind: str, entry: str, setting: str = "[llm] backend") -> str:
    """The message a refused unattended run reports. `setting` names the config
    key that selected the backend."""
    return (
        f"The {kind} backend is a CLI agent that runs its own tools (shell, file "
        "edits, web access) outside eVi's permission checks, so eVi won't use it "
        f"for an unattended {entry} run. Point {setting} at a local or API "
        "backend for this, or set `allow_cli_agent_unattended = true` under [llm] "
        "in ~/.evi/config.toml to accept that risk."
    )


def _allowed_unattended(settings) -> bool:
    settings = getattr(settings, "llm", settings)
    return getattr(settings, "allow_cli_agent_unattended", False) is True


def _configured_backends(settings) -> list[tuple[str, str]]:
    """``(setting, kind)`` for the [llm] backend and, given a ``Config``, every
    non-empty ``[models] *_backend`` (a specialty client a tool builds on its
    own thread)."""
    llm = getattr(settings, "llm", settings)
    found = [("[llm] backend", str(getattr(llm, "backend", "") or ""))]
    models = getattr(settings, "models", None)
    for key, value in sorted(getattr(models, "__dict__", {}).items()):
        if key.endswith("_backend") and isinstance(value, str) and value.strip():
            found.append((f"[models] {key}", value))
    return [(setting, kind.strip().lower()) for setting, kind in found]


def check_unattended(settings, entry: str) -> None:
    """Raise :class:`CliAgentUnattended` when `settings` (an ``LLMSettings`` or a
    ``Config``) selects a delegate CLI-agent backend, as the [llm] backend or a
    ``[models] *_backend``, and the user hasn't set
    ``allow_cli_agent_unattended``."""
    if _allowed_unattended(settings):
        return
    for setting, kind in _configured_backends(settings):
        if kind in DELEGATE_BACKENDS:
            raise CliAgentUnattended(unattended_refusal(kind, entry, setting))


@contextmanager
def unattended(entry: str, settings=None) -> Iterator[None]:
    """Mark the enclosed run as the unattended entry point `entry` (e.g.
    ``"telegram"``, ``"scheduled task"``). With `settings`, a refused backend
    raises right here; inside the block every delegate client refuses to run
    unless `settings` allowed it. Build the agent inside the block: its client
    then refuses on any thread. A client built outside the block is only
    refused when called in this thread or a copied context."""
    if settings is not None:
        check_unattended(settings, entry)
    token = _UNATTENDED.set((entry, _allowed_unattended(settings)))
    try:
        yield
    finally:
        _UNATTENDED.reset(token)


def current_unattended() -> tuple[str, bool] | None:
    """``(entry, allowed)`` when running inside :func:`unattended`, else None."""
    return _UNATTENDED.get()


# --- the OpenAI-client shell --------------------------------------------------


class _Completions:
    def __init__(self, client: "CliAgentClient"):
        self._client = client

    def create(self, *, model=None, messages=None, tools=None, stream=False,
               n=1, **_ignored):
        """OpenAI-compatible entry point. Extra kwargs (temperature, top_p,
        tool_choice, stream_options, …) are accepted and ignored — a CLI agent
        doesn't take them."""
        client = self._client
        if client.delegate:
            # The caller's context, then the block the client was built in.
            for marker in (current_unattended(), getattr(client, "_built_unattended", None)):
                if marker is not None and not marker[1]:
                    raise CliAgentUnattended(unattended_refusal(client.kind, marker[0]))
        driver = client._driver
        model = model or client.model
        msgs = messages or []

        def run(out):
            driver.run_turn(model=model, messages=msgs, tools=tools, out=out)

        if stream:
            return drain(spawn_turn(run))

        # Non-streaming: run n times, collect text + tool_calls into n choices.
        choices = []
        usage_payload = None
        for i in range(max(1, int(n or 1))):
            text_parts: list[str] = []
            tool_calls = []
            for chunk in drain(spawn_turn(run)):
                if getattr(chunk, "usage", None) is not None:
                    usage_payload = chunk.usage
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                if d and d.content:
                    text_parts.append(d.content)
                if d and d.tool_calls:
                    for tc in d.tool_calls:
                        tool_calls.append(SimpleNamespace(
                            id=tc.id, type="function",
                            function=SimpleNamespace(name=tc.function.name,
                                                     arguments=tc.function.arguments),
                        ))
            message = SimpleNamespace(
                role="assistant",
                content="".join(text_parts) or None,
                tool_calls=tool_calls or None,
            )
            choices.append(SimpleNamespace(index=i, message=message, finish_reason="stop"))
        return SimpleNamespace(choices=choices, usage=usage_payload)


class _Chat:
    def __init__(self, client: "CliAgentClient"):
        self.completions = _Completions(client)


class CliAgentClient:
    """OpenAI-``client``-shaped object backed by a per-CLI ``driver``. Only the
    surface eVi's agent loop touches is implemented: ``.chat.completions.create``.
    ``model`` is the default model id when a call omits one. ``kind`` names the
    backend in messages; ``delegate`` is True when the CLI runs its own tools,
    which makes the client refuse to run inside :func:`unattended`, and always
    when it was built inside one (whatever thread later calls it)."""

    kind = "cli-agent"
    delegate = True

    def __init__(self, driver, default_model: str = ""):
        self._driver = driver
        self.model = default_model or ""
        self._built_unattended = current_unattended()
        self.chat = _Chat(self)
