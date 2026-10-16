"""The agent loop: streams chat completions from LM Studio and dispatches tool calls.

Events yielded by `Agent.chat`:
- TextDelta(text)        — incremental assistant text
- ToolCall(name, args)   — model requested a tool
- ToolResult(name, out)  — tool finished, output appended to history
- Done(reason)           — turn complete (stop, length, ...)
- Error(message)         — recoverable error to surface to the UI
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

from openai import OpenAI

from evi.citations import Citation, ToolOutput
from evi.config import Config
from evi.debug import dlog
from evi.hooks import HookRegistry

if TYPE_CHECKING:
    from evi.guardrails import Guardrails
from evi import hooks as hooks_mod
from evi import otel
from evi import skillscope
from evi import workdir
from evi.memory import MemoryStore
from evi.project import ProjectContext
from evi.skills import SkillStore
from evi.tools.base import (
    REGISTRY,
    Tool,
    ToolContext,
    active_context,
    bind_context,
    unbind_context,
)
from evi.transcripts import TranscriptStore
from evi.audio_input import (
    build_audio_content,
    model_supports_audio,
    transcribe_for_fallback,
)
from evi.reasoning import reasoning_extra_body
from evi.vision import build_image_content, describe_for_fallback, model_supports_vision


# Signature: (tool_name, args_json, category) -> bool. True = approve.
PermissionCallback = Callable[[str, str, str], bool]

# Batched variant: given a list of (tool_name, args_json, category) for the
# calls that need a human decision, return a parallel list of bools. Lets a
# frontend prompt once for a whole multi-tool turn instead of N times.
BatchPermissionCallback = Callable[[list[tuple[str, str, str]]], list[bool]]

# What a tool call that would ask does when nobody can be asked (no permission
# callback: a scheduled task, a subagent, a workflow step, an SDK embed):
# "deny" refuses it, "allow" runs it ([auto] unattended_policy, the explicit
# escape hatch). Any other value fails closed to "deny".
UNATTENDED_POLICIES = ("deny", "allow")

# How a tool result written for a call the permission checks refused starts
# (`Agent._denial_message`); a hook veto ("BLOCKED BY HOOK") is not one.
_DENIAL_PREFIXES = ("PERMISSION DENIED", "BLOCKED by the destructive-command guard")


def unattended_policy_name(value: object) -> str:
    """`value` as an unattended policy: "allow" only when it says exactly
    that (any case), "deny" for anything else."""
    return "allow" if str(value or "").strip().lower() == "allow" else "deny"


def is_permission_denial(text: str) -> bool:
    """Whether a tool result is the text of a call the permission checks
    refused (the policy, an unanswered or refused prompt, the guard)."""
    return str(text or "").startswith(_DENIAL_PREFIXES)


def policy_label(agent: Any) -> str:
    """How `agent` settles a tool call that would ask, for a run's header or
    log: "allow-all" (`/auto on`, `-y`), "interactive" (a permission callback
    decides), else its unattended policy ("deny" | "allow"). An [auto] mode
    that lets calls through without asking is named too, since it applies
    before the policy: "allow-all (mode=yolo)", "deny (mode=accept_edits: file
    edits run)". A remote trigger (`remote_unattended`) ignores the mode."""
    if getattr(agent, "auto_all", False):
        return "allow-all"
    if (
        getattr(agent, "permission_callback", None) is not None
        or getattr(agent, "permission_batch_callback", None) is not None
    ):
        return "interactive"
    effective = getattr(agent, "effective_unattended_policy", None)
    if callable(effective):
        policy = effective()
    else:
        policy = unattended_policy_name(getattr(agent, "unattended_policy", None))
    if getattr(agent, "remote_trigger", False):
        return policy
    mode = getattr(getattr(getattr(agent, "config", None), "auto", None), "mode", "ask")
    if mode == "yolo":
        return "allow-all (mode=yolo)"
    if mode == "accept_edits" and policy == "deny":
        return "deny (mode=accept_edits: file edits run)"
    return policy


def config_policy_label(config: Any) -> str:
    """The `policy_label` of an unattended agent built on `config` that has no
    policy of its own: [auto] unattended_policy, and an [auto] mode that lets
    calls run unasked (a run's header before its agents exist)."""
    from types import SimpleNamespace

    auto = getattr(config, "auto", None)
    return policy_label(SimpleNamespace(
        config=config, unattended_policy=getattr(auto, "unattended_policy", "deny"),
    ))


def make_unattended(agent: Any, policy: str | None = None) -> str:
    """Run `agent` with nobody to ask: drop its permission callbacks, so a
    call that would ask follows the unattended policy — `policy` ("deny" |
    "allow") when given, else the agent's own ([auto] unattended_policy).
    Pre-approved categories still run; protected paths and the destructive
    guard still deny. Works on any agent-like object. Returns the run's
    `policy_label`."""
    agent.permission_callback = None
    agent.permission_batch_callback = None
    if policy is not None:
        agent.unattended_policy = unattended_policy_name(policy)
    return policy_label(agent)


# What `remote_unattended` changes on an agent, and puts back afterwards.
_REMOTE_STATE = (
    "permission_callback", "permission_batch_callback", "unattended_policy",
    "auto_all", "auto_approve_categories", "remote_trigger",
)


@contextlib.contextmanager
def remote_unattended(
    agent: Any, deny_callback: PermissionCallback | None = None
) -> Iterator[str]:
    """Drive `agent` for a remote trigger (a federation peer, an A2A client, a
    routine webhook, a channel push), then put it back as it was.

    Nobody can be asked, and nothing the desk user turned on for themselves
    applies: the permission callbacks are dropped, the unattended policy is
    "deny" whatever [auto] unattended_policy says, `/auto on` is off, the
    auto-approve set is [auto] auto_approve again (no session additions), and
    [auto] mode yolo / accept_edits is ignored (`Agent.remote_trigger`, which
    a subagent inherits). `deny_callback`, a callback that refuses every call
    (the web's `_deny_unattended`), stays attached during the run instead of
    none, for a session that must never be left without one. Yields the run's
    policy, "deny". Works on any agent-like object."""
    saved = {name: getattr(agent, name) for name in _REMOTE_STATE if hasattr(agent, name)}
    try:
        make_unattended(agent, "deny")
        agent.permission_callback = deny_callback
        agent.auto_all = False
        auto = getattr(getattr(agent, "config", None), "auto", None)
        agent.auto_approve_categories = set(getattr(auto, "auto_approve", []) or [])
        agent.remote_trigger = True
        yield "deny"
    finally:
        for name in _REMOTE_STATE:
            if name in saved:
                setattr(agent, name, saved[name])
            else:
                with contextlib.suppress(AttributeError):
                    delattr(agent, name)


def _generate_session_id() -> str:
    return secrets.token_hex(6)


def _find_json_blobs(text: str) -> list[str]:
    """Return top-level balanced ``{...}`` / ``[...]`` spans in ``text``
    (string-aware, so braces inside quotes don't confuse the scan)."""
    blobs: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] in "{[":
            depth, in_str, esc, j = 0, False, False, i
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        blobs.append(text[i : j + 1])
                        break
                j += 1
            i = j + 1
        else:
            i += 1
    return blobs


def _loads_tolerant(blob: str):
    """Parse a JSON-ish object, tolerating the malformed output local models
    often emit: single-quoted strings and Python literals. Returns the parsed
    value or None. (Local models sometimes wrap values in single quotes when the
    value itself contains a double quote, producing invalid JSON.)"""
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        pass
    import ast

    try:  # handles single-quoted strings + True/False/None
        return ast.literal_eval(blob)
    except (ValueError, SyntaxError):
        pass
    # Normalize JSON literals to Python, then retry (e.g. lowercase `false`).
    import re as _re

    norm = _re.sub(r"\bfalse\b", "False", blob)
    norm = _re.sub(r"\btrue\b", "True", norm)
    norm = _re.sub(r"\bnull\b", "None", norm)
    try:
        return ast.literal_eval(norm)
    except (ValueError, SyntaxError):
        return None


def recover_text_tool_calls(text: str, known: set[str]) -> list[dict[str, str]]:
    """Recover tool calls that a model emitted as TEXT instead of via the
    structured ``tool_calls`` field — a common local-model (e.g. qwen via
    Ollama) behaviour where the assistant prints a ``{"name": ..., "arguments":
    ...}`` JSON object (often fenced) as content.

    Returns ``[{"name", "arguments"(json str)}, …]`` for blobs whose ``name`` is
    a known tool. Only the first blob that yields calls is used. Returns ``[]``
    when nothing matches.
    """
    if not text or "{" not in text:
        return []
    for blob in _find_json_blobs(text):
        obj = _loads_tolerant(blob)
        if obj is None:
            continue
        items = obj if isinstance(obj, list) else [obj]
        out: list[dict[str, str]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            fn = it.get("function") if isinstance(it.get("function"), dict) else {}
            name = it.get("name") or fn.get("name")
            args = it.get("arguments")
            if args is None:
                args = fn.get("arguments")
            if args is None:
                args = it.get("parameters")
            if name in known and args is not None:
                args_str = args if isinstance(args, str) else json.dumps(args)
                out.append({"name": str(name), "arguments": args_str})
        if out:
            return out
    return []


def _sanitized_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``history`` safe to send to any OpenAI-compat backend.

    Ollama's endpoint rejects a message whose ``content`` is JSON null with
    ``invalid message content type: <nil>`` — and a SINGLE such message
    poisons the whole session: it sits in ``history`` and every later turn
    re-sends it, so the chat 400s on every turn until the session is reset.
    A null can arrive from many places (a tool that returned ``None``, an MCP
    result, a revived transcript entry, a plain assistant/user turn), so we
    guard at the one choke point where the full history is sent rather than at
    each source. OpenAI tolerates null content; Ollama does not. Coerce any
    ``None`` content to ``""`` and leave string / multimodal-list content as-is.
    This copy is send-only — it never mutates the stored history.
    """
    out: list[dict[str, Any]] = []
    for m in history:
        if m.get("content") is None:
            m = {**m, "content": ""}
        out.append(m)
    return out


def _approx_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count without a tokenizer.

    The English-text rule of thumb is ~4 chars/token. For multipart vision
    content the image_url data URL would distort the estimate, so we only
    count the textual parts. Good enough for "how full is my context"
    reporting; not a substitute for the real tokenizer.
    """
    char_count = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            char_count += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    char_count += len(part.get("text") or "")
        # tool_calls payload contributes too (function name + arguments json)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            char_count += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return char_count // 4


class _ThinkParser:
    """Streaming parser that splits assistant text into visible vs thinking.

    Models emit chunks of arbitrary length; the `<think>` and `</think>`
    boundaries can land mid-token. We hold a small buffer of the trailing
    characters so we never emit half a tag as visible text.

    `feed(chunk)` returns `(visible, thinking)` — either may be empty.
    Call `flush()` at end-of-stream to drain whatever's still buffered.
    """

    OPEN = "<think>"
    CLOSE = "</think>"
    _MAX_BUF = max(len(OPEN), len(CLOSE)) - 1

    def __init__(self) -> None:
        self.in_think = False
        self._buf = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        visible: list[str] = []
        thinking: list[str] = []
        # Combine carryover with new chunk so a tag straddling chunks resolves.
        data = self._buf + chunk
        self._buf = ""

        i = 0
        while i < len(data):
            if self.in_think:
                idx = data.find(self.CLOSE, i)
                if idx == -1:
                    # No close tag yet. Keep all but the last few chars to
                    # cover a `</think>` that hasn't fully arrived.
                    safe_end = max(i, len(data) - self._MAX_BUF)
                    thinking.append(data[i:safe_end])
                    self._buf = data[safe_end:]
                    return "".join(visible), "".join(thinking)
                thinking.append(data[i:idx])
                i = idx + len(self.CLOSE)
                self.in_think = False
            else:
                idx = data.find(self.OPEN, i)
                if idx == -1:
                    safe_end = max(i, len(data) - self._MAX_BUF)
                    visible.append(data[i:safe_end])
                    self._buf = data[safe_end:]
                    return "".join(visible), "".join(thinking)
                visible.append(data[i:idx])
                i = idx + len(self.OPEN)
                self.in_think = True

        return "".join(visible), "".join(thinking)

    def flush(self) -> tuple[str, str]:
        """Drain remaining buffer. Anything still inside an open <think> is
        emitted as thinking (we never saw the close tag)."""
        if not self._buf:
            return "", ""
        if self.in_think:
            out = ("", self._buf)
        else:
            out = (self._buf, "")
        self._buf = ""
        return out


def _render_for_summary(messages: list[dict[str, Any]]) -> str:
    """Flatten a slice of Agent.history into readable plaintext for a summary
    request. Tool messages get a label so the model knows what's what."""
    out: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "") or ""
        if role == "tool":
            tname = m.get("name", "tool")
            preview = content[:400].replace("\n", " ")
            out.append(f"[TOOL {tname}] {preview}")
        elif role in ("user", "assistant"):
            out.append(f"[{role.upper()}] {content}")
        else:
            out.append(f"[{role.upper()}] {content[:300]}")
    return "\n\n".join(out)


# --- Event types ----------------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingDelta:
    """Incremental text from inside a <think>…</think> block.

    Reasoning models (DeepSeek-R1, QwQ, Llama-Nemotron-Reasoning, …) wrap
    their chain-of-thought in `<think>` tags. We strip those out of the
    visible TextDelta stream and route the inner content here so the UI
    can render it dim or behind a "show thinking" toggle.
    """

    text: str


@dataclass
class ToolCall:
    name: str
    arguments: str


@dataclass
class ToolResult:
    name: str
    output: str
    # Structured source excerpts the tool emitted alongside the visible
    # text. Empty list for tools that don't produce citations (the common
    # case). The web UI renders these as a "Sources" footer.
    citations: list[Citation] = field(default_factory=list)


@dataclass
class ToolProgress:
    """Heartbeat emitted while one or more tools are still running, so the CLI
    and web UIs show live status instead of appearing to hang during a slow
    call (index build, web fetch, model pull, …). `names` are the tools still
    running; `elapsed` is seconds since this batch started."""

    names: list[str]
    elapsed: float


@dataclass
class UsageStats:
    """Real token counts from the backend, when available.

    Emitted once per LLM round-trip after the streaming response closes
    (we ask for it via `stream_options.include_usage`). Backends that
    don't report usage simply never emit this event.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class LogProbs:
    """Per-token log-probabilities for the assistant's visible output.

    Emitted once per turn (before `Done`) when `config.llm.logprobs` is on
    and the backend reports them. `tokens` is a list of `{token, logprob}`
    dicts (capped). `avg_logprob` / `min_logprob` summarise confidence;
    `low_count` is how many tokens fell below `low_threshold` (more = more
    hedging / potential hallucination).
    """

    tokens: list[dict[str, Any]]
    avg_logprob: float
    min_logprob: float
    low_count: int
    low_threshold: float = -2.0


@dataclass
class Guardrail:
    """A guardrail rule matched on input or output content.

    `direction` is "input" or "output". `blocked` is True when a block rule
    fired (input → the turn was refused; output → the stored reply was
    replaced). `redacted_by` lists redaction rules that rewrote the text.
    """

    direction: str
    blocked: bool
    blocked_by: list[str]
    redacted_by: list[str]
    message: str


@dataclass
class Done:
    reason: str


@dataclass
class Error:
    message: str


@dataclass
class RouteInfo:
    """Which model/route this turn resolved to (multi-model routing). `route`
    is the route name, or "default"/"fast" when no route matched."""

    model: str
    route: str


@dataclass
class Steering:
    """A follow-up the user sent while a turn was already running.

    eVi applies it at the next step rather than interrupting the current one, so
    a mid-turn correction lands without killing work in flight. `applied` is
    False when the turn ended before there was a next step — the caller is then
    responsible for sending it as a normal message, so the text is never lost.
    """

    text: str
    applied: bool = True


Event = (
    TextDelta | ThinkingDelta | ToolCall | ToolResult | ToolProgress | UsageStats
    | LogProbs | Guardrail | RouteInfo | Steering | Done | Error
)

# Seconds between ToolProgress heartbeats while tools are still running.
PROGRESS_INTERVAL = 2.0


# --- Agent ---------------------------------------------------------------


DEFAULT_SYSTEM_PROMPT = (
    "You are eVi, a personal AI assistant running locally. "
    "You have access to tools — call them when they would help the user, "
    "but answer directly when you don't need them. Be concise."
)


# User-facing capabilities worth naming when the model is asked "what can you
# do?" — maps a [tools] category flag to a plain-language name (with the key
# precondition where an external helper is involved). Internal / niche
# categories (fs, code, memory, skills, subagent, federation, ask,
# transcripts) are deliberately omitted: they aren't what a user means by
# "a capability", and every entry here rides on the prompt each turn.
_GROUNDING_CAPABILITIES: dict[str, str] = {
    "image": "image generation (local, via ComfyUI)",
    "web": "web search + page fetch",
    "ocr": "OCR — read text from images",
    "pdf": "reading PDFs",
    "sqlite": "querying SQLite databases",
    "git": "inspecting git repositories",
    "voice": "voice — transcribe audio / speak replies",
    "vision": "describing images",
    "calendar": "reading your calendar",
    "mcp": "tools from external MCP servers",
    "computer": "on-screen computer control",
    "index": "semantic search over your files",
}


class Agent:
    """Stateful chat session against a tool-capable LLM.

    History is held in-memory; build a new Agent per conversation. Tool calls
    are executed synchronously inside `chat` between LLM turns.
    """

    def __init__(
        self,
        client: OpenAI,
        config: Config,
        tools: list[Tool],
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        memory: MemoryStore | None = None,
        skills: SkillStore | None = None,
        project: ProjectContext | None = None,
        hooks: HookRegistry | None = None,
        permission_callback: PermissionCallback | None = None,
        permission_batch_callback: BatchPermissionCallback | None = None,
        transcripts: TranscriptStore | None = None,
        session_id: str | None = None,
        guardrails: "Guardrails | None" = None,
        frontend: str = "cli",
        user_root: Path | str | None = None,
        unattended_policy: str | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.memory = memory
        self.skills = skills
        self.project = project
        # Session working folder ("" = process cwd). File tools resolve relative
        # paths against this via evi.workdir; set by /cd, --cwd, or the web UI.
        # It starts as the folder active where the agent is built, so an agent
        # built for a session's run (an /ultra stage, a workflow step inside
        # fan_out) works in that session's folder, not the process cwd.
        self.cwd: str = workdir.session_cwd()
        # The active skill's tool scope for this turn (evi.skillscope). Agent
        # state, never a ContextVar: invoke_skill runs on a tool thread, so it
        # returns the scope and the loop applies it here. Cleared each turn.
        self.active_skill_scope: skillscope.Scope | None = None
        # The scope of the agent that spawned this one (a subagent started
        # while a skill was active). A floor: never cleared, only narrowed.
        self.inherited_skill_scope: skillscope.Scope | None = None
        self.hooks = hooks
        self.permission_callback = permission_callback
        self.permission_batch_callback = permission_batch_callback
        # With neither callback set nobody can be asked, and a call that would
        # ask follows this policy ("deny" | "allow"). None: [auto]
        # unattended_policy, read live (effective_unattended_policy).
        self.unattended_policy: str | None = (
            None if unattended_policy is None else unattended_policy_name(unattended_policy)
        )
        # A remote trigger drives this run (`remote_unattended`): [auto] mode
        # yolo / accept_edits is the desk user's and does not apply.
        self.remote_trigger: bool = False
        self.transcripts = transcripts
        self.guardrails = guardrails
        # Which surface the user is on ("cli" | "web"; the desktop app is "web").
        # Only affects prose that names WHERE to change a setting — a terminal
        # user edits config, not a Settings GUI.
        self.frontend = (frontend or "cli").strip().lower()
        self.session_id = session_id or _generate_session_id()
        # The tool call this agent is built inside (a subagent, a workflow
        # step), None for a top-level agent: ToolContext.parent of its calls.
        self.parent_context: ToolContext | None = active_context()
        # The multi-user web login's data dir (HOME/users/<name>), None in
        # single-user mode; tools reach it as ToolContext.user_root. An agent
        # built inside a tool call works for the same user as the agent whose
        # tool built it.
        if user_root is None and self.parent_context is not None:
            user_root = self.parent_context.user_root
        self.user_root: Path | None = Path(user_root) if user_root is not None else None
        # User turns so far (ToolContext.turn_index): chat() counts each one,
        # continuing a revived session's count (_sync_turn_index).
        self.turn_index: int = 0
        self._turns_of: str | None = None  # the session_id turn_index counts for
        # ToolContext.cancel_token. Nothing sets it yet (Agent cancel will).
        self.cancel_token = threading.Event()
        # This agent's bindings of tools that have a `binder`, made at each
        # tool's first call (`_session_tool`): unbound tool's id → (that tool,
        # kept alive so the id stays unique; its binding; the session bound for).
        self._bound_lock = threading.Lock()
        self._bound: dict[int, tuple[Tool, Tool, str]] = {}
        # Categories that never prompt. Populated from config.auto.auto_approve.
        self.auto_approve_categories: set[str] = set(
            getattr(getattr(config, "auto", None), "auto_approve", []) or []
        )
        self.auto_all: bool = False  # set via /auto on for the session
        # Non-interrupting steering: follow-ups the user sends WHILE a turn is
        # running. They're queued here and applied between steps rather than
        # interrupting the current one. Written from the web request thread and
        # read from the worker thread running chat(), hence the lock.
        self._steer_lock = threading.Lock()
        self._steer_queue: list[str] = []
        self.goal: str | None = None
        self.plan_mode_once: bool = False
        self.plan_mode: bool = False  # persistent plan/read-only mode (/plan on)
        self._base_system_prompt = system_prompt
        # Computed ONCE per session (not per turn): local models have no clock,
        # but a date that changed every turn would needlessly bust the KV-cache
        # prefix local backends reuse. refresh_prompt() keeps this stable date.
        from datetime import datetime as _dt

        self._session_date = _dt.now().strftime("%A, %Y-%m-%d")
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": self._compose_system_prompt()}
        ]
        # Last per-turn model decision from the router; reused by
        # continue_chat() so re-rolls stay on the same model.
        self._last_route_model: str | None = None
        self._last_route_name: str | None = None

    # --- prompt composition ---------------------------------------------

    # --- non-interrupting steering ----------------------------------------

    def steer(self, text: str) -> bool:
        """Queue a follow-up to apply at the next step of a running turn.

        Safe to call from another thread. Returns False for empty text so a
        caller can fall back to sending a normal message.
        """
        text = (text or "").strip()
        if not text:
            return False
        with self._steer_lock:
            self._steer_queue.append(text)
        return True

    def pending_steering(self) -> int:
        with self._steer_lock:
            return len(self._steer_queue)

    def take_pending_steering(self) -> list[str]:
        """Remove and return anything still queued.

        Used after a turn ends: whatever never reached a next step has to be
        handed back so the caller can send it normally. Dropping it silently
        would be worse than refusing the message in the first place.
        """
        with self._steer_lock:
            out, self._steer_queue = self._steer_queue, []
        return out

    def _compose_system_prompt(self) -> str:
        """Stitch base + style + memory + skills + project context together."""
        parts = [self._base_system_prompt]
        # Model identity. The tool-discipline sentence (shared by both branches)
        # curbs a common small-model failure — firing a tool on a bare "hello".
        model_id = (getattr(self.config.llm, "model", "") or "").strip()
        backend_kind = (getattr(self.config.llm, "backend", "") or "").strip().lower()
        # Backends that serve a LOCAL, typically open-weight model (the user runs
        # the weights). openai_compat and the CLI-agent backends are NOT here:
        # they may genuinely be Claude/GPT/Gemini via the user's key or CLI login.
        _LOCAL_MODEL_KINDS = {"lmstudio", "ollama", "llamacpp"}
        _tool_discipline = (
            " Do not call tools for greetings, small talk, or anything you can "
            "answer directly — only call a tool when it is actually needed."
        )
        # Small models read "if the user asks, tell them X" as a standing order
        # and prefix EVERY reply with it — including after being asked to stop.
        # Make the "only when asked" half explicit rather than implied. Kept
        # terse deliberately: this rides on every turn, and a local model's
        # context is small.
        _identity_discipline = (
            " Only state the model when asked — never volunteer it or prefix a "
            "reply with it."
        )
        # Backend/model is switchable mid-conversation (picker, /model, `evi
        # backend use`), so earlier assistant turns can still name the OLD model
        # and a model that imitates its own transcript keeps quoting it.
        # Scoped to the assistant's OWN replies: talking about other models is
        # routine here (model picker, six CLI-agent backends, /model), so a blanket
        # "any earlier mention is stale" would also discredit the user's messages
        # and compaction summaries.
        _identity_staleness = (
            " If an earlier reply of yours named a different model, it predates a"
            " backend switch and is stale."
        )
        if model_id and backend_kind in _LOCAL_MODEL_KINDS:
            # Local open-weight models hallucinate "I'm GPT-4" from their training
            # data when asked what they are — anchor their identity hard.
            parts.append(
                f"You are eVi, running the local open-weight model `{model_id}`. "
                "You are NOT ChatGPT, GPT-4, Claude, or Gemini, and you were not "
                "built by OpenAI, Anthropic, or Google. If the user asks which "
                f"model or company you are, tell them you are eVi running `{model_id}` "
                "locally." + _identity_discipline + _identity_staleness + _tool_discipline
            )
        elif model_id:
            # openai_compat / CLI-agent backends: the model may genuinely BE Claude,
            # GPT, Gemini, etc. (via the user's API key or CLI subscription), so do
            # NOT claim it's open-weight or deny what it is — just anchor the eVi
            # persona and answer honestly about the underlying model.
            parts.append(
                f"You are eVi, a local-first personal assistant. This session is "
                f"powered by the `{model_id}` model through your configured backend. "
                "Identify yourself as eVi; if the user asks which model powers you, "
                f"answer honestly that it is `{model_id}` (don't claim to be an "
                "open-weight or purely local model if you aren't)."
                + _identity_discipline + _identity_staleness + _tool_discipline
            )
        # Session date + a dateless staleness note. Local models have no clock
        # (they invent "today"), and eVi ships a scheduler that rides this same
        # prompt, so an anchored date is load-bearing. No hardcoded cutoff — eVi
        # is model-pluggable (arbitrary local weights + 6 CLI backends) and can't
        # know each model's real cutoff, so cutoff is phrased as behavior.
        parts.append(
            f"Today is {self._session_date}. Your training data may be out of "
            "date; for current events, prices, or recent releases prefer web "
            "search (when it's enabled) over answering from memory, and say so "
            "when you're unsure."
        )
        # eVi's real capabilities + which are currently off, so the model answers
        # "what can you do?" honestly instead of hallucinating that eVi can't do
        # something it can (and points the user at how to enable an off feature).
        parts.append(self._capability_grounding())
        # Output style (response persona), if one is selected. Skipped in safe
        # mode: a user file at ~/.evi/styles/<name>.md overrides the built-in of
        # the same name, so a broken one must not survive a clean boot. (This is
        # inline in the prompt build, so no builder flag reaches it.)
        try:
            from evi import safemode
            from evi.styles import style_text

            if not safemode.enabled():
                st = style_text(getattr(self.config.llm, "output_style", "") or "")
                if st:
                    parts.append(st)
        except Exception:  # noqa: BLE001
            pass
        if self.memory is not None:
            mem = self.memory.format_for_prompt()
            if mem:
                parts.append(mem)
        if self.skills is not None:
            sk = self.skills.format_for_prompt()
            if sk:
                parts.append(sk)
        if self.project is not None:
            parts.append(self.project.format_for_prompt())
        return "\n\n".join(parts)

    def _capability_grounding(self) -> str:
        """A short, honest statement of eVi's real capabilities + which are OFF.

        Fixes a common failure where the model, asked "what can you do?" or "can
        you generate images?", answers from its training data and denies a
        feature eVi actually has. `off` is computed live from [tools] so the list
        always matches the session; when a capability is off, the model is told
        to point the user at how to enable it rather than refusing.
        """
        tools_cfg = getattr(self.config, "tools", None)
        off = [
            label
            for cat, label in _GROUNDING_CAPABILITIES.items()
            if tools_cfg is not None and not getattr(tools_cfg, cat, False)
        ]
        # Where the user turns features on differs by surface: a GUI user opens
        # Settings; a terminal user runs `evi config`. Naming the wrong one sends
        # a third of users to a place that doesn't exist for them.
        where = "Settings → Tools" if self.frontend != "cli" else "`evi config` (the Tools settings)"
        text = (
            "eVi is a local-first assistant with real, local tools — not just a "
            "chat model. When asked what you can do, answer from eVi's actual "
            "capabilities; never tell the user eVi lacks a feature it has. If a "
            "request needs a capability that's off (or an external helper like "
            f"ComfyUI for image generation that isn't running), tell the user what "
            f"to enable in {where} or start — don't just refuse."
        )
        if off:
            text += (
                f" Capabilities eVi supports but that are OFF this session (enable "
                f"in {where}): " + ", ".join(off) + "."
            )
        return text

    # --- goal + plan-mode hooks (consumed by the REPL / programmatic use) -

    def set_goal(self, goal: str) -> None:
        self.goal = goal.strip() or None

    def clear_goal(self) -> None:
        self.goal = None

    def enable_plan_mode(self) -> None:
        """The next chat() turn runs in plan-only mode (no tool calls)."""
        self.plan_mode_once = True

    # --- history compaction ---------------------------------------------

    def _config_logit_bias(self) -> dict | None:
        """Parse `config.llm.logit_bias` (a JSON string) into a dict.

        Returns None on empty/invalid so a malformed config never crashes a
        turn — we'd rather drop the bias than refuse to chat. Values are
        clamped to OpenAI's [-100, 100] range.
        """
        raw = (self.config.llm.logit_bias or "").strip()
        if not raw:
            return None
        import json as _json

        try:
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            dlog("logit_bias.parse_error", {"raw": raw[:120]})
            return None
        if not isinstance(parsed, dict) or not parsed:
            return None
        out: dict[str, float] = {}
        for k, v in parsed.items():
            try:
                out[str(k)] = max(-100.0, min(100.0, float(v)))
            except (ValueError, TypeError):
                continue
        return out or None

    def token_usage(self) -> tuple[int, int]:
        """Return (approx_tokens_in_history, configured_context_size).

        Both numbers are best-effort: the token count is an estimate (see
        `_approx_tokens`) and the ceiling comes from
        `config.llm.context_size` which the user sets per model.
        """
        return _approx_tokens(self.history), int(self.config.llm.context_size or 0)

    def refresh_context_size(self) -> int:
        """Bring the ceiling in step with eVi's managed llama.cpp server when
        this session talks to it, and return it.

        That server's window (-c) IS the ceiling, and a model swap or GPU
        switch relaunches it with a new one after this session loaded its own
        copy of the config. Any other server keeps `config.llm.context_size`.
        No probe — cheap enough to call before every turn."""
        from evi.runtime.llama_server import ctx_for

        window = ctx_for(self.config.llm.base_url)
        if window:
            self.config.llm.context_size = window
        return int(self.config.llm.context_size or 0)

    def _maybe_autocompact(self) -> None:
        """Compact history if either the message-count threshold is exceeded
        OR usage exceeds the configured percentage of the context window."""
        msg_threshold = self.config.llm.compact_after_messages
        if msg_threshold and len(self.history) > msg_threshold:
            self.compact_history()
            return
        used, ceiling = self.token_usage()
        pct = self.config.llm.compact_when_pct
        if ceiling > 0 and pct > 0 and used * 100 >= ceiling * pct:
            self.compact_history()

    def compact_history(self, keep_recent: int | None = None) -> int:
        """Summarise the oldest stretch of history into one system note.

        Keeps the original system prompt (index 0) and the most recent
        `keep_recent` messages verbatim. Everything in between is replaced
        by one `role=system` message holding the summary. Returns the
        number of messages collapsed (0 if nothing to do).

        Internally uses a one-shot LLM call with the same backend the
        agent is already using; if that call fails we leave history
        unchanged so a transient model outage doesn't lose context.
        """
        keep = keep_recent if keep_recent is not None else self.config.llm.compact_keep_recent
        keep = max(2, int(keep))

        # history[0] is always the system prompt; protect it.
        if len(self.history) <= 1 + keep:
            return 0

        head = self.history[0]
        tail = self.history[-keep:]
        middle = self.history[1:-keep]
        if not middle:
            return 0

        # Lifecycle hook: a before_compact hook may veto to keep history intact.
        if self._fire_lifecycle("before_compact", str(len(middle))):
            return 0

        # Render the slice we want to summarise as a plain transcript.
        rendered = _render_for_summary(middle)
        summary_prompt = (
            "Summarise the conversation below into a concise paragraph "
            "preserving any concrete facts, decisions, file paths, "
            "preferences, and unfinished tasks. Aim for under 400 words. "
            "Do not invent details. Conversation:\n\n" + rendered
        )

        try:
            stream = self.client.chat.completions.create(
                model=self.config.llm.model,
                messages=[
                    {"role": "system", "content": "You write conversation summaries."},
                    {"role": "user", "content": summary_prompt},
                ],
                temperature=0.2,
                max_tokens=1024,
                stream=False,
            )
            summary = stream.choices[0].message.content or ""
        except Exception:
            return 0

        if not summary.strip():
            return 0

        note = {
            "role": "system",
            "content": (
                f"[compacted: {len(middle)} earlier messages summarised]\n\n"
                + summary.strip()
            ),
        }
        self.history = [head, note, *tail]
        return len(middle)

    def enable_auto_all(self) -> None:
        """Approve every tool call for the rest of this session."""
        self.auto_all = True

    def disable_auto_all(self) -> None:
        self.auto_all = False

    def _call_category(self, tool: Tool, args_json: str) -> str:
        """The category the permission policy checks this call under
        (`Tool.category_for`: monitor kind=command is "shell", reading code is
        "code_read"), shown in the prompt too; the tool's own category for a
        tool without one."""
        fn = getattr(tool, "category_for", None)
        if callable(fn):
            try:
                return fn(args_json)
            except Exception:  # noqa: BLE001 — fall back to the static category
                pass
        return getattr(tool, "category", "")

    def _is_pre_approved(self, tool: Tool, args_json: str = "{}") -> bool:
        """`/auto on`, or `[auto] auto_approve` approves this call's own
        category (`permissions.approved_by_category`); never code execution
        in a remote-triggered run."""
        if self.auto_all:
            return True
        from evi.permissions import approved_by_category

        return approved_by_category(
            self.auto_approve_categories,
            tool.name,
            getattr(tool, "category", ""),
            args_json,
            call_category=self._call_category(tool, args_json),
            remote_trigger=bool(getattr(self, "remote_trigger", False)),
        )

    def _guarded_command(self, tool: Tool, args_json: str) -> str | None:
        """The shell command a guarded tool would execute, or None if `tool`
        isn't a shell-executing call this invocation.

        The destructive guard inspects every tool in
        `evi.permissions.SHELL_EXEC_TOOLS` (not just category "shell"), so a
        command-runner like `monitor` (category "code", runs its `target` with
        shell=True) can't slip a destructive command past it. That one map also
        drives `permissions.decide`, so the guard and the rules always agree on
        which calls run a shell line."""
        from evi.permissions import shell_command_of

        try:
            return shell_command_of(
                getattr(tool, "name", ""), getattr(tool, "category", ""), args_json
            )
        except Exception:  # noqa: BLE001
            return None

    def _destructive_decision(self, tool: Tool, args_json: str) -> tuple[str, str] | None:
        """Guard curated destructive shell commands so they can never run
        silently. Returns ``(decision, reason)`` — ``"ask"`` when a permission
        UI exists (require confirmation) or ``"deny"`` when there's none
        (headless fail-safe) — or ``None`` when the guard doesn't apply.

        Sits above yolo / accept_edits / auto-approve / `/auto on`. Explicit
        *specific* user allow-rules (naming the tool or ``shell``, never the
        toggle category a command-runner like `monitor` is in, and carrying a
        non-trivial arg-glob) are honoured as intent and clear a
        ``prompt``-severity hit, but only when they cover EVERY segment of the
        command line (`evi.shellsplit`), the line parses, and its redirects stay
        inside the working folder: ``allow run_command git*`` must not clear
        ``git log; rm -rf ~``. A broad ``allow shell`` / ``allow shell *`` never
        clears it, so a wildcard can't silently disable the safety net, and no
        allow rule clears a ``block``-severity hit (``rm -rf ~``, a force-push,
        ``curl … | sh``): only a ``destructive_allow`` glob can exempt one.
        """
        auto = getattr(self.config, "auto", None)
        if not getattr(auto, "block_destructive", True):
            return None
        cmd = self._guarded_command(tool, args_json)
        if cmd is None:
            return None
        from evi.shell_guard import destructive_hit

        hit = destructive_hit(
            cmd,
            disable_rules=getattr(auto, "destructive_disable_rules", []) or [],
            allow=getattr(auto, "destructive_allow", []) or [],
        )
        if hit is None:
            return None
        from evi.permissions import _arg_values, exec_cwd_of, rule_keys, shell_rule_action

        act = shell_rule_action(
            getattr(auto, "rules", []) or [],
            # deny rules keyed on the name, the category and "shell" (monitor
            # too); allow rules on the name and "shell" (`shell_rule_action`)
            rule_keys(tool.name, getattr(tool, "category", ""), cmd),
            _arg_values(args_json),
            cmd,
            workdir=getattr(self, "cwd", "") or None,
            cwd=exec_cwd_of(tool.name, args_json),
            broad=False,  # only a non-trivial arg-glob is a real, specific intent
        )
        if act == "deny":
            return ("deny", hit.reason)
        if act == "allow" and hit.severity != "block":
            return None
        return ("ask" if self._has_permission_ui() else "deny", hit.reason)

    def _has_permission_ui(self) -> bool:
        return (
            self.permission_callback is not None
            or getattr(self, "permission_batch_callback", None) is not None
        )

    def effective_unattended_policy(self) -> str:
        """What a call that would ask does when there is no permission UI:
        "deny" or "allow". The agent's own `unattended_policy` when an entry
        point set one, else [auto] unattended_policy (default "deny")."""
        own = getattr(self, "unattended_policy", None)
        if own is None:
            own = getattr(getattr(self.config, "auto", None), "unattended_policy", "deny")
        return unattended_policy_name(own)

    def _protected_patterns(self) -> list[str]:
        """`[auto] protected_paths` plus the built-in defaults, minus
        `protected_paths_remove` (`permissions.effective_protected_paths`)."""
        from evi.permissions import effective_protected_paths

        auto = getattr(self.config, "auto", None)
        return effective_protected_paths(
            getattr(auto, "protected_paths", None),  # None: the stock list
            getattr(auto, "protected_paths_remove", []) or [],
        )

    @staticmethod
    def _tool_path_args(tool: Tool, args_json: str) -> list[str] | None:
        fn = getattr(tool, "path_args", None)
        return fn(args_json) if callable(fn) else None

    def _protected_hit(self, tool: Tool, args_json: str) -> str | None:
        """The protected path this call would write, or None. Such a call is
        never pre-approved (yolo, auto-approve, `/auto on`): it asks, or is
        denied when there is no UI to ask with."""
        from evi.permissions import protected_write

        try:
            return protected_write(
                tool.name,
                getattr(tool, "category", ""),
                args_json,
                self._protected_patterns(),
                workdir=getattr(self, "cwd", "") or None,
                path_args=self._tool_path_args(tool, args_json),
                read_only=getattr(tool, "read_only", None),
            )
        except Exception:  # noqa: BLE001 — a check that can't run must not allow
            return "(unreadable path arguments)"

    def _secret_read_hit(self, tool: Tool, args_json: str) -> str | None:
        """The protected secret (``.env``, a key) this fs/code call would read,
        or None (`permissions.protected_read`). Such a call is never
        pre-approved by a category: `decide` only lets an explicit allow rule
        or yolo pass it, so here it is confirmed."""
        from evi.permissions import protected_read

        try:
            return protected_read(
                getattr(tool, "category", ""),
                args_json,
                self._protected_patterns(),
                workdir=getattr(self, "cwd", "") or None,
                path_args=self._tool_path_args(tool, args_json),
            )
        except Exception:  # noqa: BLE001 — a check that can't run must not allow
            return "(unreadable path arguments)"

    def _prompt_one(self, tool: Tool, args_json: str) -> bool:
        """Ask the user about one call: the per-call callback, else the batch
        one with a single item. False when there is neither, or it fails."""
        try:
            category = self._call_category(tool, args_json)
            if self.permission_callback is not None:
                return bool(self.permission_callback(tool.name, args_json, category))
            batch = getattr(self, "permission_batch_callback", None)
            if batch is not None:
                answers = list(batch([(tool.name, args_json, category)]))
                return bool(answers[0]) if answers else False
        except Exception:
            return False
        return False

    def _denial_message(self, tool: Tool | None, name: str, args_json: str) -> str:
        """Tool-result text for a blocked call. When the destructive-command
        guard is the reason, tell the model *why* and to prefer a safer path
        (mirrors Claude Code's denial feedback) instead of a bare 'denied'."""
        if tool is not None:
            guard = self._destructive_decision(tool, args_json)
            if guard is not None:
                return (
                    f"BLOCKED by the destructive-command guard: {guard[1]}. Not run — "
                    "find a safer approach; only re-issue if the user explicitly "
                    "asked for this exact command."
                )
            protected = self._protected_hit(tool, args_json)
            if protected is not None:
                return (
                    f"PERMISSION DENIED: {name} writes the protected path {protected!r} "
                    "([auto] protected_paths), which needs the user's approval, and was "
                    "not approved — not run."
                )
            secret = self._secret_read_hit(tool, args_json)
            if secret is not None:
                return (
                    f"PERMISSION DENIED: {name} reads the protected path {secret!r} "
                    "([auto] protected_paths), which needs the user's approval, and was "
                    "not approved — not run."
                )
        if not self._has_permission_ui():
            return (
                f"PERMISSION DENIED: {name}({args_json}) was not run. This run is "
                "unattended (nobody can approve a tool call), so only auto-approved "
                "tool categories run ([auto] auto_approve; [auto] unattended_policy = "
                f'"{self.effective_unattended_policy()}").'
            )
        return f"PERMISSION DENIED: user did not approve {name}({args_json})"

    def _permission_decision(self, tool: Tool, args_json: str) -> str:
        """'allow' | 'deny' | 'ask' for a tool call, via the permission policy
        (mode + rules + auto-approve categories). `/auto on` acts as yolo for
        the session: hard_deny, protected paths and the destructive guard
        still apply. A deny from the policy (hard_deny, plan mode, a deny rule)
        always wins, even over a guard prompt."""
        guard = self._destructive_decision(tool, args_json)
        decision = self._decide(tool, args_json, self._auto_mode())
        if decision == "deny":
            return "deny"
        if guard is not None:
            return guard[0]  # destructive guard overrides auto-approve / yolo / /auto
        if not self._has_permission_ui():
            # Unattended, a protected write is never let through, and neither is
            # a secret read (.env, a key) that no explicit allow rule covers:
            # not by [auto] unattended_policy, yolo or `/auto on` (`-y`).
            if decision == "ask" and self._protected_hit(tool, args_json) is not None:
                return "deny"
            if self._secret_read_hit(tool, args_json) is not None and (
                decision == "ask" or self._decide(tool, args_json, "ask") != "allow"
            ):
                return "deny"
        return decision

    def _auto_mode(self) -> str:
        """The permission mode this agent's calls are decided under: "yolo"
        for `/auto on`, else [auto] mode, except that a remote trigger
        (`remote_trigger`) never gets the desk user's yolo or accept_edits."""
        if self.auto_all:
            return "yolo"
        mode = getattr(getattr(self.config, "auto", None), "mode", "ask")
        if getattr(self, "remote_trigger", False) and mode in ("yolo", "accept_edits"):
            return "ask"
        return mode

    def _decide(self, tool: Tool, args_json: str, mode: str) -> str:
        """`permissions.decide` for this call under `mode`, with this agent's
        auto-approve set and the [auto] rules, trusted scopes and paths."""
        from evi.permissions import decide

        auto = getattr(self.config, "auto", None)
        return decide(
            mode,
            self.auto_approve_categories,
            getattr(auto, "rules", []) or [],
            tool.name,
            tool.category,
            args_json,
            getattr(auto, "trusted_dirs", []) or [],
            getattr(auto, "trusted_domains", []) or [],
            getattr(auto, "hard_deny", []) or [],
            self._protected_patterns(),
            workdir=getattr(self, "cwd", "") or None,
            path_args=self._tool_path_args(tool, args_json),
            read_only=getattr(tool, "read_only", None),
            call_category=self._call_category(tool, args_json),
            remote_trigger=bool(getattr(self, "remote_trigger", False)),
        )

    def _ask_permission(self, tool: Tool, args_json: str) -> bool:
        """Return True iff the tool call is allowed to proceed."""
        guard = self._destructive_decision(tool, args_json)
        if guard is None and (
            self._protected_hit(tool, args_json) is not None
            or self._secret_read_hit(tool, args_json) is not None
        ):
            guard = ("ask", "protected path")  # never pre-approved: confirm it
        if guard is not None:
            if guard[0] == "deny":
                return False  # headless / user-denied destructive command → block
            # 'ask': require confirmation — bypass the pre-approval short-circuit
            # (through the batch UI when that is the only one).
            return self._prompt_one(tool, args_json)
        if self._is_pre_approved(tool):
            return True
        if not self._has_permission_ui():
            # Nobody to ask (scheduler, subagent, SDK embed): the unattended
            # policy decides, and it fails closed.
            return self.effective_unattended_policy() == "allow"
        return self._prompt_one(tool, args_json)

    def reset(self, system_prompt: str | None = None) -> None:
        if system_prompt is not None:
            self._base_system_prompt = system_prompt
        self.history = [
            {"role": "system", "content": self._compose_system_prompt()}
        ]
        # A new conversation: session-bound tools start afresh. turn_index
        # keeps counting, as the session id (and so its journal tags) stays.
        with self._bound_lock:
            self._bound.clear()

    # --- history manipulation (edit / re-roll / branch) ------------------

    def truncate_history(self, after_index: int) -> int:
        """Drop every message past `after_index` (inclusive of system at 0).

        Returns the number of messages removed. Always keeps history[0]
        (the system prompt) regardless of the value passed.
        """
        after_index = max(0, int(after_index))
        # System message at index 0 stays no matter what.
        cutoff = max(1, after_index + 1)
        if cutoff >= len(self.history):
            return 0
        removed = len(self.history) - cutoff
        self.history = self.history[:cutoff]
        return removed

    def edit_message(self, at_index: int, new_content: str) -> bool:
        """Replace history[at_index].content and truncate everything after.

        Refuses to edit the system prompt (index 0). Returns True on success.
        """
        if at_index <= 0 or at_index >= len(self.history):
            return False
        msg = dict(self.history[at_index])
        msg["content"] = new_content
        # Re-shape: keep [0..at_index], replace at_index, drop everything else.
        self.history = self.history[:at_index] + [msg]
        return True

    def rewind_to_last_user(self) -> int:
        """Drop trailing messages until the last entry is a user message.

        Used by re-roll: regenerate the assistant's response without
        appending a new user turn. Returns the number of messages popped.
        """
        popped = 0
        while len(self.history) > 1 and self.history[-1].get("role") != "user":
            self.history.pop()
            popped += 1
        return popped

    def _pick_model_for_turn(self, user_msg: str) -> tuple[str, str]:
        """Resolve the (model_id, route_name) to use for this turn.

        Precedence: router (if enabled and a route matches) > fast_mode >
        configured default. Empty `user_msg` or routing disabled both fall
        through to the fast/default branch.
        """
        if self.config.llm.router_enabled and user_msg.strip():
            try:
                from evi.routing import Router, RouterStore

                routes = RouterStore().load()
            except Exception:
                routes = []
            if routes:
                router = Router(
                    routes,
                    default_model=self.config.llm.model,
                    classifier_model=self.config.llm.router_model,
                    client=self.client,
                )
                decision = router.pick(user_msg)
                if decision.route_name != "default":
                    dlog(
                        "router.pick",
                        {
                            "route": decision.route_name,
                            "model": decision.model,
                            "reason": decision.reason,
                        },
                    )
                    return decision.model, decision.route_name
        if self.config.llm.fast_mode and self.config.llm.fast_model:
            return self.config.llm.fast_model, "fast"
        return self.config.llm.model, "default"

    def refresh_config(self) -> None:
        """Re-read config.toml from disk and apply to this agent in place.

        Picks up changes to model, effort, fast_mode, sampling knobs, etc.
        without restarting the process. The system prompt is also re-composed
        so changes to memory / skills land on the next turn.
        """
        self.config = Config.load()
        # Refresh auto-approval set in case those changed too.
        self.auto_approve_categories = set(
            getattr(getattr(self.config, "auto", None), "auto_approve", []) or []
        )
        self.refresh_prompt()

    def refresh_prompt(self) -> None:
        """Recompose the system prompt (history[0]) in place from current config.

        The system prompt embeds live state — the model identity, memory, skills —
        so it must be re-stitched whenever any of those change mid-session (e.g. a
        model switch via the picker). Callers that mutate ``config.llm.model`` on a
        running agent MUST call this, or the frozen prompt will keep telling the
        model it's the old model (and it'll report the old name)."""
        if self.history:
            self.history[0] = {
                "role": "system",
                "content": self._compose_system_prompt(),
            }

    def chat(
        self,
        user_msg: str,
        max_turns: int = 6,
        images: list[str] | None = None,
        response_format: dict | None = None,
        tool_choice: str | dict | None = None,
        prediction: str | None = None,
        parallel_tool_calls: bool | None = None,
        logit_bias: dict | None = None,
        audio: list[str] | None = None,
    ) -> Iterator[Event]:
        """Send a user message and yield events until the model is done.

        Runs up to `max_turns` LLM round-trips so a runaway tool loop can't
        spin forever. If a goal is set, prepends a reminder to the user
        message. If plan-mode-once is set, tools are withheld for this turn
        and a planning suffix is appended to the user message.

        `images` is an optional list of local file paths. When the
        configured model looks vision-capable, the images are encoded as
        data URLs and attached to the user message as `image_url` content
        parts (OpenAI vision spec). For non-vision models we degrade
        gracefully: the paths are mentioned in the text so the agent can
        still read them via tools (e.g. `read_file` on a PDF or text).

        `prediction` is OpenAI's predicted-outputs hint
        (https://platform.openai.com/docs/guides/predicted-outputs): pass
        the expected output text and supporting backends use speculative
        decoding to verify it token-by-token, often 3-5× faster than
        regenerating from scratch. Killer for edit-like prompts where you
        already know most of the answer (rename, reformat, add docstring).
        Backends that don't support speculative decoding silently drop the
        field — we forward it via `extra_body` to avoid SDK kwarg
        rejection. Only applied to the FIRST LLM round-trip in this turn;
        once any tool runs, the prediction is stale and we let the model
        write freely.
        """
        # Scope file tools to this session's working folder for the turn. Tool
        # bodies run on pool threads, so _dispatch_run sets it there too.
        if getattr(self, "cwd", ""):
            workdir.set_cwd(self.cwd)
        # Reset active-skill tool scope each turn; invoke_skill re-arms it.
        self.active_skill_scope = None
        # Input guardrails run on the raw user text, before any composition.
        if self.guardrails is not None:
            _jf = self._guardrail_judge_fn() if self.guardrails.judge_rules else None
            _cf = self._guardrail_classify_fn() if self.guardrails.classifier_rules else None
            _gf = self._guardrail_guard_fn() if self.guardrails.guard_rules else None
            gres = self.guardrails.check(
                user_msg, "input", judge_fn=_jf, classify_fn=_cf, guard_fn=_gf
            )
            if gres.changed:
                yield Guardrail(
                    direction="input",
                    blocked=not gres.allowed,
                    blocked_by=list(gres.blocked_by),
                    redacted_by=list(gres.redacted_by),
                    message=(
                        (
                            f"input blocked by: {', '.join(gres.blocked_by)}"
                            + (f" ({'; '.join(gres.notes)})" if gres.notes else "")
                        )
                        if gres.blocked_by
                        else f"input redacted by: {', '.join(gres.redacted_by)}"
                    ),
                )
            if not gres.allowed:
                yield Done("guardrail_blocked")
                return
            user_msg = gres.text  # use the redacted text downstream

        # Lifecycle hook: a user_prompt_submit hook may veto (block) the turn
        # before the model ever sees it.
        if self._fire_lifecycle("user_prompt_submit", user_msg):
            yield Done("hook_blocked")
            return

        composed = user_msg
        if self.goal:
            composed = (
                f"[ongoing goal: {self.goal}]\n\n{composed}\n\n"
                "Stay focused on the goal. Say 'goal complete' when you "
                "believe it is satisfied."
            )
        plan_only = self.plan_mode_once or self.plan_mode
        if plan_only:
            composed = (
                f"{composed}\n\n"
                "Plan-only mode: produce a numbered implementation plan. "
                "Do not call any tools. Identify the files you would touch "
                "and the trade-offs."
            )
            self.plan_mode_once = False  # one-shot

        images = images or []
        audio = audio or []
        model_id = self.config.llm.model
        audio_native = bool(audio) and model_supports_audio(model_id)
        vision_native = bool(images) and model_supports_vision(model_id)

        # Non-omni model + audio → transcribe locally and fold into the text
        # so "talk about this clip" still works everywhere.
        if audio and not audio_native:
            transcript = transcribe_for_fallback(audio)
            if transcript:
                composed = f"{composed}\n\n{transcript}"

        user_content: Any
        if audio_native or vision_native:
            # Build a multipart message. Audio first (omni models also handle
            # images, so append image parts after when both are present).
            if audio_native:
                parts = build_audio_content(composed, audio)
                if vision_native:
                    parts += build_image_content("", images)[1:]  # drop dup text
            else:
                parts = build_image_content(composed, images)
            user_content = parts
            bits = []
            if audio_native:
                bits.append(f"+{len(audio)} audio")
            if vision_native:
                bits.append(f"+{len(images)} image")
            transcript_summary = f"{composed}\n\n[{', '.join(bits)} attachment(s)]"
        elif images:
            # Non-vision main model: describe the image(s) locally via the vision
            # specialty (+ OCR when text-heavy) and fold that into the text, so a
            # pasted screenshot still "works" on a text-only model — the visual
            # analogue of the audio-transcribe fallback above. If nothing could
            # be produced (no vision model / OCR), surface the paths as before.
            described = describe_for_fallback(images)
            composed = f"{composed}\n\n{described}" if composed else described
            user_content = composed
            transcript_summary = composed
        else:
            user_content = composed
            transcript_summary = composed

        self._sync_turn_index()
        self.turn_index += 1  # ToolContext.turn_index for this turn's calls
        self.history.append({"role": "user", "content": user_content})
        self._log_to_transcript("user", transcript_summary)
        model_for_turn, route_name = self._pick_model_for_turn(composed)
        self._last_route_model = model_for_turn
        self._last_route_name = route_name
        yield RouteInfo(model=model_for_turn, route=route_name)
        yield from self._run_inference_loop(
            max_turns=max_turns,
            plan_only=plan_only,
            response_format=response_format,
            tool_choice=tool_choice,
            model_override=model_for_turn,
            prediction=prediction,
            parallel_tool_calls=parallel_tool_calls,
            logit_bias=logit_bias,
        )
        # No `stop` hook here: every consumer breaks on Done, which closes this
        # generator before it gets back to this line. The loop fires it.

    def _guardrail_judge_fn(self):
        """A one-shot LLM classifier for semantic ([[judge]]) guardrails.

        Returns judge(policy, text) -> (allowed, reason). Uses the agent's own
        client/model (stays local), temperature 0, no tools/stream. Any error
        returns allowed=True (fail open) so a flaky grader can't block chat.
        """
        def judge(policy: str, text: str) -> tuple[bool, str]:
            prompt = (
                "You are a strict content-safety classifier. A message violates "
                "the POLICY only if it clearly matches it.\n\n"
                f"POLICY (disallowed):\n{policy}\n\n"
                "Reply on the first line with exactly ALLOW or BLOCK, then a "
                f"short reason.\n\nMESSAGE:\n{text}"
            )
            try:
                resp = self.client.chat.completions.create(
                    model=self.config.llm.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=120,
                    stream=False,
                )
                out = (resp.choices[0].message.content or "").strip()
            except Exception:
                return True, ""  # fail open
            first = out.splitlines()[0] if out else ""
            allowed = not first.strip().upper().startswith("BLOCK")
            return allowed, first[:200]

        return judge

    @staticmethod
    def _guardrail_classify_fn():
        """A classify(model_id, text) -> {label: score} backed by a local HF
        moderation model (offline). Used by [[classifier]] guardrail rules; a
        missing dep/model raises, which the guardrail catches and fails open."""
        from evi import moderation

        def classify(model_id: str, text: str) -> dict:
            return moderation.classify(model_id, text)

        return classify

    def _guardrail_guard_fn(self):
        """A guard(model, text, direction) -> (allowed, reason) backed by a
        dedicated safety-guard model (Llama Guard / ShieldGemma). Used by
        [[guard]] rules; the rule's model wins, else [models] guard. Reaches the
        model via the specialty registry so it can live on a separate backend.
        Any failure raises, which the guardrail catches and fails open."""
        from evi import guardmodel
        from evi.llm.specialty import SpecialtyRegistry

        registry = SpecialtyRegistry(self.config.llm, self.config.models)

        def guard(model: str, text: str, direction: str) -> tuple[bool, str]:
            mid = (model or self.config.models.guard or "").strip()
            role = "assistant" if direction == "output" else "user"
            return guardmodel.classify_safety(
                mid, text, role=role, registry=registry, llm=self.config.llm
            )

        return guard

    def _fire_lifecycle(self, event: str, payload: str = "") -> bool:
        """Run lifecycle hooks for `event`. Returns True if a hook vetoed.
        No-op (returns False) when no hooks are configured. If the hooks can't
        be run at all, a vetoing guard for the event still blocks it (fail
        closed); anything else is let through."""
        if self.hooks is None:
            return False
        try:
            _results, veto = self.hooks.run_lifecycle(  # type: ignore[arg-type]
                event, payload=payload, cwd=getattr(self, "cwd", "")
            )
            return veto is not None
        except Exception:
            try:
                return self.hooks.can_veto(event)  # type: ignore[arg-type]
            except Exception:
                return event in hooks_mod.VETO_EVENTS

    def continue_chat(self, max_turns: int = 6) -> Iterator[Event]:
        """Resume inference against the current `history` without appending a
        new user message. Caller is responsible for setting history up — the
        typical use is re-roll: pop the last assistant message (and any
        intervening tool messages), then invoke this to regenerate."""
        # A re-roll answers the last user message afresh: the turn that set
        # the old skill scope was rewound away with it.
        self.active_skill_scope = None
        self._sync_turn_index()  # a revived session re-rolls its last turn
        # Re-use the model the original turn picked, so re-rolls stay on
        # the same expert.
        yield from self._run_inference_loop(
            max_turns=max_turns,
            model_override=self._last_route_model,
        )

    def complete_variants(
        self,
        prompt: str,
        n: int = 3,
        *,
        temperature: float | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> list[str]:
        """Return `n` independent one-shot completions for `prompt`.

        Stateless: does NOT touch `self.history` and never calls tools.
        Built for "give me 3 commit messages / subject lines / variants"
        — a single non-streaming `create(n=...)` call. Backends that
        ignore `n` (most local ones today) return a single choice, so the
        caller may get fewer than `n` results; we return whatever came
        back. A higher `temperature` (default bumps to 0.9 for variety)
        makes the variants actually differ.
        """
        n = max(1, int(n))
        messages = [
            {"role": "system", "content": system or "You produce concise, high-quality variants."},
            {"role": "user", "content": prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else 0.9,
            "n": n,
            "stream": False,
        }
        if self.config.llm.max_completion_tokens:
            kwargs["max_completion_tokens"] = int(self.config.llm.max_completion_tokens)
        else:
            kwargs["max_tokens"] = max_tokens or self.config.llm.max_tokens
        dlog("llm.variants", {"model": kwargs["model"], "n": n})
        resp = self.client.chat.completions.create(**kwargs)
        out: list[str] = []
        for choice in getattr(resp, "choices", []) or []:
            msg = getattr(choice, "message", None)
            text = (getattr(msg, "content", None) or "").strip()
            if text:
                out.append(text)
        return out

    def suggest_title(self, max_words: int = 6) -> str:
        """Return a short title summarising the conversation so far.

        Stateless one-shot — does NOT mutate history. Reads the first few
        user/assistant turns and asks the model for a terse noun-phrase
        title (no trailing punctuation, no quotes). Returns "" on any
        failure so callers can fall back to their own labelling (e.g. the
        first user message). Web tabs / `evi sessions title` use this.
        """
        # Pull the earliest real exchange — skip the system prompt at [0].
        snippets: list[str] = []
        for m in self.history[1:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            snippets.append(f"{role}: {content.strip()[:500]}")
            if len(snippets) >= 4:
                break
        if not snippets:
            return ""
        prompt = (
            f"Summarise this conversation as a title of at most {max_words} "
            "words. Reply with ONLY the title — no quotes, no trailing "
            "punctuation, no preamble.\n\n" + "\n".join(snippets)
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.config.llm.model,
                messages=[
                    {"role": "system", "content": "You write terse conversation titles."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=32,
                stream=False,
            )
            title = (resp.choices[0].message.content or "").strip()
        except Exception:
            return ""
        # Tidy: drop wrapping quotes, collapse whitespace, clamp word count.
        title = title.strip().strip('"').strip("'").strip()
        title = " ".join(title.split())
        if not title:
            return ""
        words = title.split(" ")
        if len(words) > max_words:
            title = " ".join(words[:max_words])
        return title.rstrip(".")

    def _run_inference_loop(
        self,
        *,
        max_turns: int,
        plan_only: bool = False,
        response_format: dict | None = None,
        tool_choice: str | dict | None = None,
        model_override: str | None = None,
        prediction: str | None = None,
        parallel_tool_calls: bool | None = None,
        logit_bias: dict | None = None,
    ) -> Iterator[Event]:
        """The streaming + tool-dispatch loop. Assumes history is exactly
        what the model should see. Shared by `chat` and `continue_chat`."""

        # Effective model: explicit override (routing) wins; else fast-mode;
        # else the configured default.
        if model_override:
            active_model = model_override
        elif self.config.llm.fast_mode and self.config.llm.fast_model:
            active_model = self.config.llm.fast_model
        else:
            active_model = self.config.llm.model
        # `reasoning_effort` is OpenAI-style; for backends that ignore it
        # (most local ones, today) we drop it into `extra_body` so the SDK
        # forwards it as a top-level field without erroring. Only send it to
        # models that actually support thinking — Ollama 400s a non-reasoning
        # model (e.g. qwen2.5) on a thinking request, while DeepSeek-R1 accepts.
        extra_body: dict[str, Any] = {}
        effort = (self.config.llm.reasoning_effort or "").strip().lower()
        extra_body.update(reasoning_extra_body(effort, active_model))
        # KV-cache prompt reuse hint (llama.cpp honours it; others ignore).
        if self.config.llm.cache_prompt:
            extra_body["cache_prompt"] = True

        for turn_idx in range(max_turns):
            # Non-interrupting steering: a follow-up the user sent while this
            # turn was running lands HERE — between steps, once the previous
            # tool round is complete — instead of interrupting work in flight.
            for _steer in self.take_pending_steering():
                self.history.append({"role": "user", "content": _steer})
                self._log_to_transcript("user", _steer)
                yield Steering(_steer, applied=True)

            # Recompute each round so tools surfaced mid-turn by `search_tools`
            # (deferred tool-search-at-scale) become available immediately. An
            # active skill's tool scope does NOT trim this list: a tool list
            # that changes mid-turn makes local backends re-process the whole
            # prompt. The scope is enforced at dispatch below instead.
            tool_schemas = (
                None if plan_only
                else ([t.openai_schema() for t in self.tools.values()] or None)
            )
            dlog(
                "llm.request",
                {
                    "model": active_model,
                    "fast_mode": self.config.llm.fast_mode,
                    "effort": effort or "medium",
                    "n_messages": len(self.history),
                    "n_tools": len(tool_schemas or []),
                    "last_user": (self.history[-1].get("content") if self.history else None),
                    "prediction_bytes": (
                        len(prediction) if (prediction and turn_idx == 0) else 0
                    ),
                },
            )
            try:
                create_kwargs: dict[str, Any] = {
                    "model": active_model,
                    # Send-only sanitize: never let a null-content message reach
                    # the backend (Ollama 400s on <nil> and it poisons the whole
                    # session). Stored history is untouched. See _sanitized_messages.
                    "messages": _sanitized_messages(self.history),
                    "tools": tool_schemas,
                    "temperature": self.config.llm.temperature,
                    "stream": True,
                    # Ask the server to include token usage in the final
                    # streamed chunk (OpenAI: ChatCompletionChunk with usage).
                    "stream_options": {"include_usage": True},
                }
                # Token budget: reasoning models reject `max_tokens` and want
                # `max_completion_tokens`. When the latter is set we send it
                # ALONE; otherwise fall back to the classic `max_tokens`.
                if self.config.llm.max_completion_tokens:
                    create_kwargs["max_completion_tokens"] = int(
                        self.config.llm.max_completion_tokens
                    )
                else:
                    create_kwargs["max_tokens"] = self.config.llm.max_tokens
                # tool_choice: caller-provided per-turn, else default ("auto").
                if tool_choice is not None:
                    if tool_choice == "none":
                        # Explicit "no tools" — drop the schemas too so the
                        # model doesn't see them at all.
                        create_kwargs["tools"] = None
                        create_kwargs.pop("tool_choice", None)
                    else:
                        create_kwargs["tool_choice"] = tool_choice
                # parallel_tool_calls: per-turn override wins over config.
                # Only meaningful (and only forwarded) when False AND tools
                # are actually present in this request.
                ptc = (
                    parallel_tool_calls
                    if parallel_tool_calls is not None
                    else self.config.llm.parallel_tool_calls
                )
                if ptc is False and create_kwargs.get("tools"):
                    create_kwargs["parallel_tool_calls"] = False
                # logit_bias: per-turn dict wins; else parse the config JSON
                # string. Forward only when non-empty.
                bias = logit_bias if logit_bias is not None else self._config_logit_bias()
                if bias:
                    create_kwargs["logit_bias"] = bias
                # response_format: caller-provided per-turn override.
                if response_format is not None:
                    create_kwargs["response_format"] = response_format
                # Sampling knobs — only forward when non-default to avoid
                # surprising local backends that don't speak them.
                if self.config.llm.top_p != 1.0:
                    create_kwargs["top_p"] = self.config.llm.top_p
                if self.config.llm.presence_penalty:
                    create_kwargs["presence_penalty"] = self.config.llm.presence_penalty
                if self.config.llm.frequency_penalty:
                    create_kwargs["frequency_penalty"] = self.config.llm.frequency_penalty
                if self.config.llm.seed:
                    create_kwargs["seed"] = int(self.config.llm.seed)
                if self.config.llm.stop_sequences:
                    create_kwargs["stop"] = list(self.config.llm.stop_sequences)
                # logprobs: ask the backend for per-token confidence. Only on
                # the first round (tool rounds don't need it) to keep the
                # response light.
                if self.config.llm.logprobs and turn_idx == 0:
                    create_kwargs["logprobs"] = True
                    if self.config.llm.top_logprobs:
                        create_kwargs["top_logprobs"] = int(self.config.llm.top_logprobs)
                # Build per-turn extra_body: shared base + first-turn-only
                # `prediction` for speculative decoding. After the first
                # round-trip the conversation has tool outputs woven in,
                # so the user's prediction is stale and we drop it.
                if prediction and turn_idx == 0:
                    turn_extra = dict(extra_body)
                    turn_extra["prediction"] = {
                        "type": "content", "content": prediction,
                    }
                    create_kwargs["extra_body"] = turn_extra
                elif extra_body:
                    create_kwargs["extra_body"] = extra_body
                # Opt-in Responses API path (default "chat" keeps every local
                # backend working). Env override wins for quick experiments.
                import os as _os
                use_responses = (
                    _os.environ.get("EVI_LLM_API") or self.config.llm.api
                ) == "responses"

                def _open(model_id: str):
                    create_kwargs["model"] = model_id
                    if use_responses:
                        from evi.llm.responses import stream_chat_via_responses
                        return stream_chat_via_responses(
                            self.client,
                            builtin_tools=self.config.llm.responses_tools,
                            **create_kwargs,
                        )
                    return self.client.chat.completions.create(**create_kwargs)

                # Model fallback chain: try the primary, then each configured
                # fallback on a setup-time failure (timeout / 5xx / model not
                # loaded). Mirrors Claude Code's fallbackModel. Mid-stream
                # failures aren't retried — output may already be partway out.
                candidates = [create_kwargs["model"]]
                for fb in self.config.llm.fallback_models:
                    if fb and fb not in candidates:
                        candidates.append(fb)
                stream = None
                last_err: Exception | None = None
                for ci, cand in enumerate(candidates):
                    try:
                        stream = _open(cand)
                    except Exception as e:  # network / model not loaded / etc.
                        last_err = e
                        dlog("llm.error",
                             {"type": type(e).__name__, "msg": str(e), "model": cand})
                        continue
                    if ci > 0:  # we fell back — tell the UI which model won
                        yield RouteInfo(model=cand, route="fallback")
                        dlog("llm.fallback", {"to": cand})
                    break
                if stream is None:
                    yield Error(
                        f"LLM request failed: "
                        f"{type(last_err).__name__}: {last_err}"
                    )
                    return
            except Exception as e:  # kwargs building / unexpected
                dlog("llm.error", {"type": type(e).__name__, "msg": str(e)})
                yield Error(f"LLM request failed: {type(e).__name__}: {e}")
                return

            text_buf: list[str] = []
            # tool calls arrive as deltas keyed by index
            tool_buf: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None
            think_parser = _ThinkParser()
            usage_payload: Any = None  # captured from the final chunk
            logprob_tokens: list[dict[str, Any]] = []  # collected when enabled
            # Some local models emit a tool call as a JSON/fenced text blob
            # instead of via structured tool_calls. If the reply *starts* with
            # `{` / `[` / a code fence we can't tell it's a tool call until the
            # stream ends — so hold that text rather than streaming raw JSON to
            # the UI, and flush it only if it turns out NOT to be a tool call.
            hold_text = False
            hold_decided = False

            def _maybe_hold() -> None:
                nonlocal hold_text, hold_decided
                if hold_decided:
                    return
                head = "".join(text_buf).lstrip()
                if head:
                    hold_decided = True
                    hold_text = head[:1] in ("`", "{", "[")

            for chunk in stream:
                # The final chunk in a stream_options.include_usage stream
                # has no choices but carries the usage tally.
                if getattr(chunk, "usage", None) is not None:
                    usage_payload = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                # Collect per-token logprobs when the backend streams them.
                lp = getattr(choice, "logprobs", None)
                if lp is not None:
                    for tok in (getattr(lp, "content", None) or []):
                        token = getattr(tok, "token", None)
                        logprob = getattr(tok, "logprob", None)
                        if token is not None and logprob is not None:
                            logprob_tokens.append({"token": token, "logprob": float(logprob)})
                delta = choice.delta
                if delta is None:
                    continue
                if delta.content:
                    visible, thinking = think_parser.feed(delta.content)
                    if thinking:
                        yield ThinkingDelta(thinking)
                    if visible:
                        text_buf.append(visible)
                        _maybe_hold()
                        if not hold_text:
                            yield TextDelta(visible)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        slot = tool_buf.setdefault(
                            tc.index,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

            # Drain any trailing buffered content (chunk boundaries can leave
            # a partial tag or trailing text in the parser).
            tail_visible, tail_thinking = think_parser.flush()
            if tail_thinking:
                yield ThinkingDelta(tail_thinking)
            if tail_visible:
                text_buf.append(tail_visible)
                _maybe_hold()
                if not hold_text:
                    yield TextDelta(tail_visible)

            # Real token usage from the backend (when supported).
            if usage_payload is not None:
                prompt = int(getattr(usage_payload, "prompt_tokens", 0) or 0)
                completion = int(
                    getattr(usage_payload, "completion_tokens", 0) or 0
                )
                total = int(getattr(usage_payload, "total_tokens", 0) or (prompt + completion))
                dlog(
                    "llm.usage",
                    {"prompt": prompt, "completion": completion, "total": total},
                )
                yield UsageStats(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=total,
                )

            # Confidence summary from collected logprobs.
            if logprob_tokens:
                vals = [t["logprob"] for t in logprob_tokens]
                low_threshold = -2.0
                low_count = sum(1 for v in vals if v < low_threshold)
                yield LogProbs(
                    tokens=logprob_tokens[:500],  # cap payload weight
                    avg_logprob=sum(vals) / len(vals),
                    min_logprob=min(vals),
                    low_count=low_count,
                    low_threshold=low_threshold,
                )

            # Recover tool calls some local models emit as TEXT instead of via
            # the structured tool_calls field (e.g. qwen via Ollama printing a
            # fenced ``{"name": …, "arguments": …}`` block). Only when there are
            # no structured calls AND the reply leads with JSON / a code fence,
            # so we never execute a tool-call example buried in normal prose.
            if not tool_buf and text_buf:
                _stripped = "".join(text_buf).strip()
                if _stripped[:1] in ("`", "{", "["):
                    _recovered = recover_text_tool_calls(_stripped, set(self.tools))
                    if _recovered:
                        for _idx, _rc in enumerate(_recovered):
                            tool_buf[_idx] = {
                                "id": f"call_text_{_idx}",
                                "name": _rc["name"],
                                "arguments": _rc["arguments"],
                            }
                        text_buf = []  # don't store the JSON as visible content
                        dlog("llm.text_tool_call_recovered", {"n": len(_recovered)})

            # Held leading-JSON text that was NOT a recovered tool call is a
            # genuine reply (e.g. the user asked for JSON) — stream it now so it
            # still renders, just non-incrementally.
            if hold_text and text_buf:
                yield TextDelta("".join(text_buf))

            # ALWAYS set `content` (empty string on a tool-calls-only turn).
            # Ollama's OpenAI-compat endpoint rejects an assistant message with
            # absent/null content — "invalid message content type: <nil>" — which
            # then POISONS the session: that message sits in history and every
            # later turn re-sends it and 400s, wedging the chat. OpenAI tolerates
            # null content on a tool-call message; Ollama requires a string.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_buf)}
            if tool_buf:
                assistant_msg["tool_calls"] = [
                    {
                        "id": slot["id"] or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        },
                    }
                    for i, slot in sorted(tool_buf.items())
                ]
            # Output guardrails: scan the assistant text before we store /
            # log it. We can't un-stream what already went to the UI, but we
            # clean the stored copy (so it can't poison later turns or the
            # transcript) and flag it.
            if self.guardrails is not None and assistant_msg.get("content"):
                _ojf = self._guardrail_judge_fn() if self.guardrails.judge_rules else None
                _ocf = self._guardrail_classify_fn() if self.guardrails.classifier_rules else None
                _ogf = self._guardrail_guard_fn() if self.guardrails.guard_rules else None
                ores = self.guardrails.check(
                    assistant_msg["content"], "output",
                    judge_fn=_ojf, classify_fn=_ocf, guard_fn=_ogf
                )
                if ores.blocked_by:
                    assistant_msg["content"] = (
                        f"[output blocked by guardrail: {', '.join(ores.blocked_by)}]"
                    )
                elif ores.redacted_by:
                    assistant_msg["content"] = ores.text
                if ores.changed:
                    yield Guardrail(
                        direction="output",
                        blocked=bool(ores.blocked_by),
                        blocked_by=list(ores.blocked_by),
                        redacted_by=list(ores.redacted_by),
                        message=(
                            f"output blocked by: {', '.join(ores.blocked_by)}"
                            if ores.blocked_by
                            else f"output redacted by: {', '.join(ores.redacted_by)}"
                        ),
                    )

            self.history.append(assistant_msg)
            self._log_to_transcript(
                "assistant",
                assistant_msg.get("content") or "",
                tool_calls=assistant_msg.get("tool_calls"),
            )

            if not tool_buf:
                self._maybe_autocompact()
                yield from self._finish_turn(finish_reason or "stop")
                return

            # Execute the requested tools. Permission is gated in ONE pass
            # (so a multi-call turn can prompt once via the batch callback),
            # before-hooks run per call, then the actual tool bodies run in
            # PARALLEL via a thread pool so independent file reads / web
            # fetches / etc. don't queue up.
            calls_meta: list[dict[str, Any]] = []
            for call in assistant_msg["tool_calls"]:
                fname = call["function"]["name"]
                fargs = call["function"]["arguments"]
                yield ToolCall(fname, fargs)
                calls_meta.append({
                    "call": call,
                    "tool": self.tools.get(fname),
                    "name": fname,
                    "args": fargs,
                })

            # 1) Unknown tools, and the skill scope (allowed/disallowed-tools),
            #    enforced here at dispatch; the tool list stays as is. A skill
            #    invoked in this same batch runs alongside the other calls, so
            #    its scope is read from its arguments up front (skillscope.peek)
            #    and applied to the rest of the batch.
            turn_scope = self._skill_scope()
            batch_scope = turn_scope
            for m in calls_meta:
                m["sets_scope"] = False
                if m["tool"] is not None and skillscope.allows(m["name"], turn_scope):
                    peeked = self._fit_scope(skillscope.peek(m["tool"], m["args"]))
                    if peeked is not None:
                        m["sets_scope"] = True
                        batch_scope = skillscope.narrow(batch_scope, peeked)
            for m in calls_meta:
                scope = turn_scope if m["sets_scope"] else batch_scope
                if m["tool"] is None:
                    m["blocked"] = f"ERROR: unknown tool '{m['name']}'"
                elif not skillscope.allows(m["name"], scope):
                    m["blocked"] = (
                        f"ERROR: tool '{m['name']}' is not allowed by the active skill's scope"
                    )
                else:
                    m["blocked"] = None
            # 2) Permission for the calls still standing, at once (batched when
            #    possible): a call refused above never prompts.
            self._gate_permissions([m for m in calls_meta if m["blocked"] is None])
            # 3) Per-call: permission denial → hook veto.
            for m in calls_meta:
                if m["blocked"] is not None:
                    continue
                if not m["perm"]:
                    m["blocked"] = self._denial_message(m.get("tool"), m["name"], m["args"])
                else:
                    m["blocked"] = self._run_before_hooks(m["name"], m["args"])

            # Each entry is either a `ToolOutput` (success or blocked) or
            # None (slot not filled yet). Permission denials / hook vetoes
            # become text-only ToolOutputs so the downstream loop is uniform.
            outputs: list[ToolOutput | None] = []
            for m in calls_meta:
                if m["blocked"] is None:
                    outputs.append(None)
                else:
                    outputs.append(ToolOutput(text=m["blocked"]))
            runnable = [
                (i, m) for i, m in enumerate(calls_meta) if outputs[i] is None
            ]
            if runnable:
                import concurrent.futures as _futures

                # 1 worker is fine for a single tool call; cap at 4 to avoid
                # thrashing a local LLM box that's also running the model.
                max_workers = min(4, len(runnable)) or 1
                with _futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                    # Each call runs on its own copy of this thread's context
                    # (one Context can't be entered by two threads at once), so
                    # ContextVars set for the turn reach the tool body. Nothing
                    # a tool sets there comes back: results do (skill scope).
                    future_to_idx = {
                        ex.submit(
                            contextvars.copy_context().run,
                            self._dispatch_run, m["tool"], m["name"], m["args"],
                            batch_scope,
                        ): i
                        for i, m in runnable
                    }
                    # Instead of blocking silently until tools finish (the old
                    # `as_completed`), poll with a timeout and emit ToolProgress
                    # heartbeats so a slow tool shows live status. Tools flagged
                    # `long` announce immediately (elapsed 0); any tool still
                    # running past PROGRESS_INTERVAL announces on each tick.
                    start = time.monotonic()
                    long_now = [
                        m["name"] for _, m in runnable
                        if getattr(m["tool"], "long", False)
                    ]
                    if long_now:
                        yield ToolProgress(long_now, 0.0)
                    pending = set(future_to_idx)
                    while pending:
                        done, pending = _futures.wait(
                            pending,
                            timeout=PROGRESS_INTERVAL,
                            return_when=_futures.FIRST_COMPLETED,
                        )
                        for fut in done:
                            i = future_to_idx[fut]
                            try:
                                outputs[i] = fut.result()
                            except Exception as exc:
                                outputs[i] = ToolOutput(
                                    text=f"ERROR: {type(exc).__name__}: {exc}"
                                )
                        if pending:
                            names = [calls_meta[future_to_idx[f]]["name"] for f in pending]
                            yield ToolProgress(names, round(time.monotonic() - start, 1))

            # A skill invoked this round scopes the rounds after it. Applied
            # here, on the loop's thread, in call order (skillscope.narrow).
            for output in outputs:
                scope = self._fit_scope(skillscope.scope_of(output))
                if scope is not None:
                    self.active_skill_scope = skillscope.narrow(self.active_skill_scope, scope)

            # Emit results + append history in the ORIGINAL order so
            # tool_call_id pairing stays consistent.
            for m, output in zip(calls_meta, outputs):
                fname = m["name"]
                call = m["call"]
                out_obj = output or ToolOutput(text="(no output)")
                yield ToolResult(fname, out_obj.text, list(out_obj.citations))
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": fname,
                        # A tool that returned None must not write a null-content
                        # message — Ollama rejects it and it poisons the session.
                        "content": out_obj.text if out_obj.text is not None else "",
                    }
                )
                self._log_to_transcript("tool", out_obj.text, tool_name=fname)

        yield from self._finish_turn("max_turns_reached")

    def _finish_turn(self, reason: str) -> Iterator[Event]:
        """End the turn: fire the `stop` lifecycle hook, then yield Done.

        The hook fires BEFORE the Done event because every consumer (REPL,
        headless, web worker, subagent) breaks on Done and so closes the
        generator: anything after that yield never runs. Once per completed
        inference loop, so a re-roll (continue_chat) fires it once too."""
        self._fire_lifecycle("stop", reason)
        yield Done(reason)

    def _skill_scope(self) -> skillscope.Scope | None:
        """The skill scope this turn's calls run under: the one inherited from
        a spawning agent, narrowed by any skill invoked in this turn."""
        return skillscope.narrow(self.inherited_skill_scope, self.active_skill_scope)

    def _fit_scope(self, scope: skillscope.Scope | None) -> skillscope.Scope | None:
        """A skill's declared scope as eVi understands it (skillscope.fit)."""
        return skillscope.fit(scope, set(REGISTRY) | set(self.tools))

    # --- session-bound tools ----------------------------------------------

    def tool_context(self) -> ToolContext:
        """The ToolContext a tool call made now runs with."""
        return ToolContext(
            agent=self,
            session_id=self.session_id,
            cwd=getattr(self, "cwd", "") or "",
            cancel_token=self.cancel_token,
            turn_index=self.turn_index,
            user_root=self.user_root,
            parent=getattr(self, "parent_context", None),
        )

    def _sync_turn_index(self) -> None:
        """Count turns for the session this agent now holds. A revived or
        resumed one (history restored, or `session_id` set after the agent was
        built) counts on from the user messages already in its history, so a new
        turn never reuses an earlier one's (session, turn) checkpoint tag."""
        if self._turns_of != self.session_id:
            self._turns_of = self.session_id
            self.turn_index = sum(
                1 for m in self.history[1:] if isinstance(m, dict) and m.get("role") == "user"
            )

    def tools_for_session(self, tools: Iterable[Tool] | None = None) -> dict[str, Tool]:
        """This session's own tool map: `tools` (default: this agent's) with
        every tool that has a `binder` bound to this agent now (`Tool.bind`),
        the rest shared as they are. Names, order and schema objects don't
        change, so the tool list the model sees is byte-identical. The agent
        itself binds lazily, at each tool's first call; a tool whose binder
        fails here stays unbound, and its call reports the error."""
        src = list(self.tools.values()) if tools is None else list(tools)
        out: dict[str, Tool] = {}
        for t in src:
            try:
                out[t.name] = self._session_tool(t)
            except Exception:  # noqa: BLE001 — the call surfaces it as ERROR
                out[t.name] = t
        return out

    def _session_tool(self, tool: Tool) -> Tool:
        """`tool` as this agent runs it: a tool with a `binder` is bound for
        this agent's session at its first call, and that binding reused until
        the session id changes or `reset`. A copy bound for another agent (one
        built from that agent's tools) is bound afresh from its source, so the
        two never share state. Other tools are returned as they are."""
        src = getattr(tool, "bound_from", None) or tool
        if getattr(src, "binder", None) is None:
            return tool
        with self._bound_lock:
            bound = self._cached_binding(src)
        if bound is not None:
            return bound
        # The binder runs outside the lock (it may look up another of this
        # session's tools) and on a fully built agent. If two first calls race,
        # the binding stored first is the one both use.
        ctx = self.tool_context()
        fresh = src.bind(ctx)
        with self._bound_lock:
            bound = self._cached_binding(src)
            if bound is None:
                self._bound[id(src)] = (src, fresh, ctx.session_id)
                bound = fresh
        return bound

    def _cached_binding(self, src: Tool) -> Tool | None:
        """This session's binding of `src`, if any. Caller holds _bound_lock."""
        hit = self._bound.get(id(src))
        if hit is not None and hit[0] is src and hit[2] == self.session_id:
            return hit[1]
        return None

    # --- tool dispatch with permission + hooks --------------------------

    # --- transcript writes ----------------------------------------------

    def _log_to_transcript(
        self,
        role: str,
        content: str,
        *,
        tool_name: str | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """Mirror one message to the on-disk transcript, if enabled."""
        if self.transcripts is None:
            return
        try:
            self.transcripts.write_message(
                session=self.session_id,
                role=role,
                content=content or "",
                tool_name=tool_name,
                tool_calls=tool_calls,
            )
        except OSError:
            pass  # never let disk errors break the chat loop

    def _run_before_hooks(self, name: str, args_json: str) -> str | None:
        """Run before-hooks for one call. Returns a blocked message on veto,
        else None. Split out from permission so the batch path can gate
        permission once and still run hooks per call."""
        if self.hooks is not None:
            try:
                _results, veto = self.hooks.run_before(
                    name, args_json, cwd=getattr(self, "cwd", "")
                )
            except Exception as exc:
                # The hooks could not run: a vetoing guard for this call still
                # blocks it (fail closed); an observer is skipped.
                try:
                    guarded = self.hooks.can_veto("before_tool_call", name, args_json)
                except Exception:
                    guarded = True
                if guarded:
                    return f"BLOCKED BY HOOK: the guard hooks failed to run ({type(exc).__name__})"
                return None
            if veto is not None:
                msg = veto.stderr.strip() or veto.stdout.strip() or "(no message)"
                return f"BLOCKED BY HOOK {veto.hook_name!r}: {msg}"
        return None

    def _dispatch_pre(self, tool: Tool | None, name: str, args_json: str) -> str | None:
        """Run pre-dispatch gating (permission + before-hooks).

        Returns an error/blocked message if the call should NOT proceed.
        Returns None if `tool.call(args_json)` is cleared to run.

        Kept serial across multiple tool calls in one turn so the permission
        prompt UX (CLI input or web dialog) handles one decision at a time.
        Used by the single-call `_invoke_tool` path; the chat loop uses
        `_gate_permissions` + `_run_before_hooks` so it can batch prompts.
        """
        if tool is None:
            return f"ERROR: unknown tool '{name}'"
        if not self._ask_permission(tool, args_json):
            return self._denial_message(tool, name, args_json)
        return self._run_before_hooks(name, args_json)

    def _gate_permissions(self, calls_meta: list[dict[str, Any]]) -> None:
        """Decide permission for every call in one pass, writing a bool to
        `m["perm"]`. Pre-approved categories and unknown tools never prompt.

        When 2+ calls need a human decision AND a batch callback is wired,
        ask once via `permission_batch_callback`; otherwise fall back to the
        per-call `permission_callback` (one prompt each). Unknown tools get
        `perm=True` here so `_run_before_hooks`/dispatch surfaces the
        'unknown tool' error uniformly downstream.
        """
        need_prompt: list[dict[str, Any]] = []
        for m in calls_meta:
            tool = m["tool"]
            if tool is None:
                m["perm"] = True  # unknown-tool error raised later, not a denial
                continue
            decision = self._permission_decision(tool, m["args"])
            if decision == "allow":
                m["perm"] = True
            elif decision == "deny":
                m["perm"] = False  # policy/mode/rule blocked it — no prompt
            elif not self._has_permission_ui():
                # 'ask' with nobody to ask (scheduler, subagent, SDK embed): the
                # unattended policy decides, "deny" unless the user opted in.
                m["perm"] = self.effective_unattended_policy() == "allow"
            else:
                m["perm"] = None  # undecided — needs a prompt
                need_prompt.append(m)

        if not need_prompt:
            return

        if self.permission_batch_callback is not None and len(need_prompt) >= 2:
            triples = [
                (m["tool"].name, m["args"], self._call_category(m["tool"], m["args"]))
                for m in need_prompt
            ]
            try:
                decisions = list(self.permission_batch_callback(triples))
            except Exception:
                decisions = [False] * len(need_prompt)
            for i, m in enumerate(need_prompt):
                m["perm"] = bool(decisions[i]) if i < len(decisions) else False
        else:
            for m in need_prompt:
                m["perm"] = self._ask_permission(m["tool"], m["args"])

    def _dispatch_run(
        self,
        tool: Tool,
        name: str,
        args_json: str,
        skill_scope: skillscope.Scope | None = None,
    ) -> ToolOutput:
        """Execute the tool body + run after-hooks. Safe to call in parallel
        across multiple tools — each tool gets its own thread of execution
        and its own subprocess for any hook commands.

        `skill_scope` is the scope the call's batch runs under (None: this
        agent's own), handed to any subagent the tool spawns.

        A tool that declares ``ctx`` gets this session's ToolContext, and one
        with a `binder` runs as this agent's own binding of it (made here at
        its first call; a binder that fails makes the call's ERROR).

        Returns a `ToolOutput` carrying both visible text and any citations
        the tool produced.
        """
        dlog("tool.call", {"name": name, "args": args_json})
        _t0 = time.monotonic()
        ctx = self.tool_context()
        # The tool body sees this session's folder ("" = the process cwd, not
        # whatever folder the calling thread last had), hooks, skill scope and
        # tool context. In the chat loop this runs on a pool thread under a
        # copied context, so these sets stay with this one call; the tokens put
        # things back when called in-thread. (A batch scope of None means the
        # agent's is.)
        cwd_token = workdir.set_cwd(getattr(self, "cwd", "") or None)
        hooks_token = hooks_mod.bind(self.hooks)
        scope_token = skillscope.bind(skill_scope or self._skill_scope())
        ctx_token = bind_context(ctx)
        try:
            with otel.span("evi.tool", **{"tool.name": name}):
                try:
                    run = self._session_tool(tool)
                    # A duck-typed tool (not a Tool) keeps its one-argument call.
                    output = (
                        run.call_rich(args_json, ctx) if isinstance(run, Tool)
                        else run.call_rich(args_json)
                    )
                except Exception as exc:
                    # Belt-and-braces: Tool.call_rich already catches most exceptions.
                    output = ToolOutput(text=f"ERROR: {type(exc).__name__}: {exc}")
        finally:
            unbind_context(ctx_token)
            skillscope.unbind(scope_token)
            hooks_mod.unbind(hooks_token)
            workdir.reset(cwd_token)
        otel.record_tool(
            name,
            ok=not output.text.startswith("ERROR:"),
            duration_ms=(time.monotonic() - _t0) * 1000.0,
        )
        dlog("tool.result", {"name": name, "output": output.text})
        if self.hooks is not None:
            try:
                self.hooks.run_after(
                    name, args_json, output.text, cwd=getattr(self, "cwd", "")
                )
            except Exception:  # noqa: BLE001 — an observer must not replace the result
                pass
        return output

    def _invoke_tool(self, tool: Tool | None, name: str, args_json: str) -> str:
        """Single-tool convenience wrapper used in the non-parallel path
        (and by tests). Returns just the text for back-compat — callers
        wanting citations should drive `_dispatch_run` directly."""
        gate = self._dispatch_pre(tool, name, args_json)
        if gate is not None:
            return gate
        assert tool is not None  # _dispatch_pre would have returned otherwise
        return self._dispatch_run(tool, name, args_json).text
