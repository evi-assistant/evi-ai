"""The agent loop: streams chat completions from LM Studio and dispatches tool calls.

Events yielded by `Agent.chat`:
- TextDelta(text)        — incremental assistant text
- ToolCall(name, args)   — model requested a tool
- ToolResult(name, out)  — tool finished, output appended to history
- Done(reason)           — turn complete (stop, length, ...)
- Error(message)         — recoverable error to surface to the UI
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from openai import OpenAI

from evi.citations import Citation, ToolOutput
from evi.config import Config
from evi.debug import dlog
from evi.hooks import HookRegistry

if TYPE_CHECKING:
    from evi.guardrails import Guardrails
from evi import otel
from evi.memory import MemoryStore
from evi.project import ProjectContext
from evi.skills import SkillStore
from evi.tools.base import Tool
from evi.transcripts import TranscriptStore
from evi.audio_input import (
    build_audio_content,
    model_supports_audio,
    transcribe_for_fallback,
)
from evi.vision import build_image_content, model_supports_vision


# Signature: (tool_name, args_json, category) -> bool. True = approve.
PermissionCallback = Callable[[str, str, str], bool]

# Batched variant: given a list of (tool_name, args_json, category) for the
# calls that need a human decision, return a parallel list of bools. Lets a
# frontend prompt once for a whole multi-tool turn instead of N times.
BatchPermissionCallback = Callable[[list[tuple[str, str, str]]], list[bool]]


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


Event = (
    TextDelta | ThinkingDelta | ToolCall | ToolResult | ToolProgress | UsageStats
    | LogProbs | Guardrail | RouteInfo | Done | Error
)

# Seconds between ToolProgress heartbeats while tools are still running.
PROGRESS_INTERVAL = 2.0


# --- Agent ---------------------------------------------------------------


DEFAULT_SYSTEM_PROMPT = (
    "You are eVi, a personal AI assistant running locally. "
    "You have access to tools — call them when they would help the user, "
    "but answer directly when you don't need them. Be concise."
)


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
    ) -> None:
        self.client = client
        self.config = config
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.memory = memory
        self.skills = skills
        self.project = project
        self.hooks = hooks
        self.permission_callback = permission_callback
        self.permission_batch_callback = permission_batch_callback
        self.transcripts = transcripts
        self.guardrails = guardrails
        self.session_id = session_id or _generate_session_id()
        # Categories that never prompt. Populated from config.auto.auto_approve.
        self.auto_approve_categories: set[str] = set(
            getattr(getattr(config, "auto", None), "auto_approve", []) or []
        )
        self.auto_all: bool = False  # set via /auto on for the session
        self.goal: str | None = None
        self.plan_mode_once: bool = False
        self._base_system_prompt = system_prompt
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": self._compose_system_prompt()}
        ]
        # Last per-turn model decision from the router; reused by
        # continue_chat() so re-rolls stay on the same model.
        self._last_route_model: str | None = None
        self._last_route_name: str | None = None

    # --- prompt composition ---------------------------------------------

    def _compose_system_prompt(self) -> str:
        """Stitch base + style + memory + skills + project context together."""
        parts = [self._base_system_prompt]
        # Output style (response persona), if one is selected.
        try:
            from evi.styles import style_text

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

    def _is_pre_approved(self, tool: Tool) -> bool:
        return self.auto_all or tool.category in self.auto_approve_categories

    def _permission_decision(self, tool: Tool, args_json: str) -> str:
        """'allow' | 'deny' | 'ask' for a tool call, via the permission policy
        (mode + rules + auto-approve categories). `/auto on` forces allow."""
        if self.auto_all:
            return "allow"
        from evi.permissions import decide

        auto = getattr(self.config, "auto", None)
        return decide(
            getattr(auto, "mode", "ask"),
            self.auto_approve_categories,
            getattr(auto, "rules", []) or [],
            tool.name,
            tool.category,
            args_json,
            getattr(auto, "trusted_dirs", []) or [],
            getattr(auto, "trusted_domains", []) or [],
        )

    def _ask_permission(self, tool: Tool, args_json: str) -> bool:
        """Return True iff the tool call is allowed to proceed."""
        if self._is_pre_approved(tool):
            return True
        if self.permission_callback is None:
            return True  # no UI to ask; default-allow (web/scheduler mode)
        try:
            return bool(self.permission_callback(tool.name, args_json, tool.category))
        except Exception:
            return False

    def reset(self, system_prompt: str | None = None) -> None:
        if system_prompt is not None:
            self._base_system_prompt = system_prompt
        self.history = [
            {"role": "system", "content": self._compose_system_prompt()}
        ]

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
        # Input guardrails run on the raw user text, before any composition.
        if self.guardrails is not None:
            _jf = self._guardrail_judge_fn() if self.guardrails.judge_rules else None
            _cf = self._guardrail_classify_fn() if self.guardrails.classifier_rules else None
            gres = self.guardrails.check(user_msg, "input", judge_fn=_jf, classify_fn=_cf)
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
        plan_only = self.plan_mode_once
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
            # No vision support — surface paths so the agent can decide.
            joined = ", ".join(images)
            composed = (
                f"{composed}\n\n[attached files (no vision in current "
                f"model): {joined}]"
            )
            user_content = composed
            transcript_summary = composed
        else:
            user_content = composed
            transcript_summary = composed

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
        # Lifecycle hook: the turn finished (notification; veto ignored).
        self._fire_lifecycle("stop")

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

    def _fire_lifecycle(self, event: str, payload: str = "") -> bool:
        """Run lifecycle hooks for `event`. Returns True if a hook vetoed.
        No-op (returns False) when no hooks are configured or scanning fails."""
        if self.hooks is None:
            return False
        try:
            _results, veto = self.hooks.run_lifecycle(event, payload=payload)  # type: ignore[arg-type]
            return veto is not None
        except Exception:
            return False

    def continue_chat(self, max_turns: int = 6) -> Iterator[Event]:
        """Resume inference against the current `history` without appending a
        new user message. Caller is responsible for setting history up — the
        typical use is re-roll: pop the last assistant message (and any
        intervening tool messages), then invoke this to regenerate."""
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
        # forwards it as a top-level field without erroring.
        extra_body: dict[str, Any] = {}
        effort = (self.config.llm.reasoning_effort or "").strip().lower()
        if effort and effort != "medium":
            extra_body["reasoning_effort"] = effort
        # KV-cache prompt reuse hint (llama.cpp honours it; others ignore).
        if self.config.llm.cache_prompt:
            extra_body["cache_prompt"] = True

        for turn_idx in range(max_turns):
            # Recompute each round so tools surfaced mid-turn by `search_tools`
            # (deferred tool-search-at-scale) become available immediately.
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
                    "messages": self.history,
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
                if (_os.environ.get("EVI_LLM_API") or self.config.llm.api) == "responses":
                    from evi.llm.responses import stream_chat_via_responses
                    stream = stream_chat_via_responses(
                        self.client,
                        builtin_tools=self.config.llm.responses_tools,
                        **create_kwargs,
                    )
                else:
                    stream = self.client.chat.completions.create(**create_kwargs)
            except Exception as e:  # network / model not loaded / etc.
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

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if text_buf:
                assistant_msg["content"] = "".join(text_buf)
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
                ores = self.guardrails.check(
                    assistant_msg["content"], "output", judge_fn=_ojf, classify_fn=_ocf
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
                yield Done(finish_reason or "stop")
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

            # 1) Permission for all calls at once (batched when possible).
            self._gate_permissions(calls_meta)
            # 2) Per-call: unknown-tool error → permission denial → hook veto.
            for m in calls_meta:
                if m["tool"] is None:
                    m["blocked"] = f"ERROR: unknown tool '{m['name']}'"
                elif not m["perm"]:
                    m["blocked"] = (
                        f"PERMISSION DENIED: user did not approve "
                        f"{m['name']}({m['args']})"
                    )
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
                    future_to_idx = {
                        ex.submit(
                            self._dispatch_run, m["tool"], m["name"], m["args"]
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
                        "content": out_obj.text,
                    }
                )
                self._log_to_transcript("tool", out_obj.text, tool_name=fname)

        yield Done("max_turns_reached")

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
            _results, veto = self.hooks.run_before(name, args_json)
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
            return f"PERMISSION DENIED: user did not approve {name}({args_json})"
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
            elif self.permission_callback is None and self.permission_batch_callback is None:
                m["perm"] = True  # 'ask' but no UI to ask (web/scheduler) → default allow
            else:
                m["perm"] = None  # undecided — needs a prompt
                need_prompt.append(m)

        if not need_prompt:
            return

        if self.permission_batch_callback is not None and len(need_prompt) >= 2:
            triples = [
                (m["tool"].name, m["args"], m["tool"].category) for m in need_prompt
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

    def _dispatch_run(self, tool: Tool, name: str, args_json: str) -> ToolOutput:
        """Execute the tool body + run after-hooks. Safe to call in parallel
        across multiple tools — each tool gets its own thread of execution
        and its own subprocess for any hook commands.

        Returns a `ToolOutput` carrying both visible text and any citations
        the tool produced.
        """
        dlog("tool.call", {"name": name, "args": args_json})
        _t0 = time.monotonic()
        with otel.span("evi.tool", **{"tool.name": name}):
            try:
                output = tool.call_rich(args_json)
            except Exception as exc:
                # Belt-and-braces: Tool.call_rich already catches most exceptions.
                output = ToolOutput(text=f"ERROR: {type(exc).__name__}: {exc}")
        otel.record_tool(
            name,
            ok=not output.text.startswith("ERROR:"),
            duration_ms=(time.monotonic() - _t0) * 1000.0,
        )
        dlog("tool.result", {"name": name, "output": output.text})
        if self.hooks is not None:
            self.hooks.run_after(name, args_json, output.text)
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
