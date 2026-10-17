"""Headless (non-interactive) single-shot runs.

Backs `evi run "<prompt>"` — drain one agent turn-loop to completion and return
a structured result, for scripts / CI / cron. The CLI builds the agent (and
sets a non-interactive permission policy); this module just drains + formats, so
it's testable with a fake agent. The result names the policy the run had
(`evi.llm.agent.policy_label`: "deny", "allow", "allow-all" or "interactive")
and why the turn stopped (`stop_reason`: the turn's Done reason, e.g. "stop",
or "interrupted" when it was cancelled).

A tool's output in the result is a display of it, so it is redacted
(`evi.redact`) before it is cut short.

With ``cancel_on_sigint`` (what `evi run` asks for), Ctrl+C / SIGINT cancels
the turn (`Agent.cancel`) instead of raising KeyboardInterrupt through it: the
turn ends with Done("interrupted"), its commands' process trees are killed,
and the result comes back as usual with ``stop_reason="interrupted"``. A
second SIGINT stops waiting for the turn to wind down (the result is still
returned, never a traceback).
"""

from __future__ import annotations

import contextlib
import json
import signal
from dataclasses import dataclass, field

from evi.llm.agent import (
    INTERRUPTED_REASON,
    Done,
    Error,
    TextDelta,
    ToolResult,
    UsageStats,
    policy_label,
)

# What a SIGINT-cancelled turn tells its notes (Agent.cancel's reason).
SIGINT_REASON = "the run was interrupted (Ctrl+C / SIGINT)"


@dataclass
class HeadlessResult:
    text: str = ""
    tools: list[dict] = field(default_factory=list)
    usage: dict | None = None
    error: str | None = None
    # How a tool call that would ask was settled in this run (policy_label).
    policy: str | None = None
    # Why the turn ended: its Done reason ("stop", "max_turns_reached", ...),
    # "interrupted" when it was cancelled; None if it never said.
    stop_reason: str | None = None

    @property
    def interrupted(self) -> bool:
        return self.stop_reason == INTERRUPTED_REASON


class _SigintCancels:
    """While installed, SIGINT cancels `agent`'s turn instead of raising
    KeyboardInterrupt; a second SIGINT raises it (to stop waiting). Only on
    the main thread, where Python runs signal handlers: elsewhere (a web
    request's thread, a batch worker) SIGINT is left as it is. The handler
    only sets the agent's cancel token, which is safe from a signal handler
    on the turn's own thread (Agent.cancel)."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.pressed = False
        self._previous: object = None
        self._installed = False

    def __enter__(self) -> "_SigintCancels":
        try:
            self._previous = signal.signal(signal.SIGINT, self._on_sigint)
            self._installed = True
        except ValueError:  # not the main thread
            pass
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._installed:
            previous = signal.default_int_handler if self._previous is None else self._previous
            signal.signal(signal.SIGINT, previous)
            self._installed = False

    def cancel(self) -> bool:
        """Cancel the turn; False when the agent can't be cancelled."""
        cancel = getattr(self.agent, "cancel", None)
        if not callable(cancel):
            return False
        cancel(SIGINT_REASON)
        return True

    def _on_sigint(self, _signum: int, _frame: object) -> None:
        if self.pressed or not self.cancel():
            raise KeyboardInterrupt
        self.pressed = True


def run_headless(
    agent,
    prompt: str,
    max_turns: int = 12,
    response_format: dict | None = None,
    *,
    cancel_on_sigint: bool = False,
) -> HeadlessResult:
    """Run one prompt through `agent` to completion and collect the result.

    `response_format` (e.g. a json_schema wrapper) is forwarded to the backend
    for Structured Outputs when the agent supports it. `cancel_on_sigint`: a
    Ctrl+C cancels the turn and the result says so (see the module doc).
    """
    from evi.redact import Redactor

    parts: list[str] = []
    res = HeadlessResult(policy=policy_label(agent))
    chat_kwargs = {"max_turns": max_turns}
    if response_format is not None:
        chat_kwargs["response_format"] = response_format
    red = Redactor()
    guard = _SigintCancels(agent) if cancel_on_sigint else contextlib.nullcontext()
    events = None
    try:
        with guard:
            try:
                events = agent.chat(prompt, **chat_kwargs)
                # A Ctrl+C that came before the turn existed went to no turn.
                if cancel_on_sigint and guard.pressed:
                    guard.cancel()
                for event in events:
                    if isinstance(event, TextDelta):
                        parts.append(event.text)
                    elif isinstance(event, ToolResult):
                        output = red(event.output) if isinstance(event.output, str) else event.output
                        res.tools.append({"name": event.name, "output": output[:2000]})
                    elif isinstance(event, UsageStats):
                        res.usage = {
                            "prompt": event.prompt_tokens,
                            "completion": event.completion_tokens,
                            "total": event.total_tokens,
                        }
                    elif isinstance(event, Error):
                        res.error = event.message
                    elif isinstance(event, Done):
                        res.stop_reason = event.reason
                        break
            finally:
                # Still under the guard: a turn left mid-tools stops its calls
                # (Agent._run_calls), and a Ctrl+C meanwhile is handled below.
                close = getattr(events, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
    except KeyboardInterrupt:
        if not cancel_on_sigint:
            raise
        # A second Ctrl+C: stop waiting for the turn to wind down.
        res.stop_reason = INTERRUPTED_REASON
    res.text = "".join(parts).strip()
    return res


def to_json(res: HeadlessResult) -> str:
    out = {"text": res.text, "tools": res.tools, "usage": res.usage, "error": res.error}
    if res.policy is not None:
        out["policy"] = res.policy
    if res.stop_reason is not None:
        out["stop_reason"] = res.stop_reason
    return json.dumps(out)
