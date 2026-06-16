"""ask_user tool — let the agent ask the human a quick question mid-task.

eVi's analogue of Claude Code's AskUserQuestion: when the model needs a
decision it can't make alone (which approach, which file, yes/no), it calls
``ask_user`` and gets the human's answer back as the tool result.

eVi tools are synchronous (run between model turns), so this blocks for input
— which is fine in the interactive REPL but must NEVER hang a web/headless
run. We therefore only prompt when the session is genuinely interactive
(``EVI_INTERACTIVE`` set by the REPL **and** a real TTY on stdin); otherwise
we return a clear note telling the model to just ask in its reply instead.
"""

from __future__ import annotations

import os
import sys

from evi.tools.base import tool

_MAX_OPTIONS = 12


def _interactive() -> bool:
    if os.environ.get("EVI_INTERACTIVE") != "1":
        return False
    try:
        return sys.stdin.isatty()
    except (ValueError, OSError):
        return False


@tool(
    description=(
        "Ask the user a clarifying question and wait for their answer. Use when "
        "you genuinely need a decision only the user can make (which approach, "
        "which option, confirm/deny). `options` is an optional list of choices "
        "(comma- or newline-separated); the user may also type a free-form "
        "answer. Returns the user's response as text."
    ),
    category="ask",
)
def ask_user(question: str, options: str = "") -> str:
    """Prompt the user with `question` (+ optional `options`) and return their answer."""
    question = (question or "").strip()
    if not question:
        return "ERROR: question is required"

    choices = [c.strip() for c in options.replace("\n", ",").split(",") if c.strip()]
    choices = choices[:_MAX_OPTIONS]

    if not _interactive():
        # Non-interactive (web / headless / print mode): don't block — tell the
        # model to fold the question into its reply instead.
        opts = f" Options: {', '.join(choices)}." if choices else ""
        return (
            "Interactive prompting isn't available in this session. Ask the user "
            f"this directly in your reply instead: {question}{opts}"
        )

    print()
    print(f"  ? {question}")
    for i, c in enumerate(choices, 1):
        print(f"    {i}. {c}")
    try:
        raw = input("  your answer: ").strip()
    except (EOFError, KeyboardInterrupt):
        return "(user declined to answer)"
    if not raw:
        return "(user gave no answer)"
    # If they typed a number that indexes a listed option, return that option.
    if choices and raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(choices):
            return choices[n - 1]
    return raw
