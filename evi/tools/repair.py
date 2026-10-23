"""Repairs for the tool calls small models get wrong.

Arguments that aren't strict JSON, cut off mid-object, mistyped or misnamed
are read and fitted to the tool where that is unambiguous; each repair is
named, so the model (and the log) can see what eVi changed.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

# A quoted string (kept as it is), or a bare JSON word to spell as Python.
_JSON_WORD = re.compile(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\b(true|false|null)\b""")
_PYTHON_WORD = {"true": "True", "false": "False", "null": "None"}


def loads_tolerant(blob: str) -> Any:
    """`blob` parsed as JSON, or failing that as the Python-literal forms
    local models write (single-quoted strings, True/None, and lowercase
    true/false/null beside them). None when nothing parses."""
    try:
        return json.loads(blob)
    except (ValueError, TypeError, RecursionError):
        pass
    try:
        return ast.literal_eval(blob)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        pass
    norm = _JSON_WORD.sub(lambda m: m.group(1) or _PYTHON_WORD[m.group(2)], blob)
    try:
        return ast.literal_eval(norm)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def close_truncated(blob: str) -> str | None:
    """`blob` with what a cut-off JSON value lacks appended: a closing quote
    for an open string (a dangling backslash dropped first), else a trailing
    comma or colon dropped, then a closer for each open { or [. None when
    nothing is open."""
    stack: list[str] = []
    in_str = esc = False
    for ch in blob:
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
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if not stack and not in_str:
        return None
    out = blob
    if in_str:
        if esc:
            out = out[:-1]
        out += '"'
    else:
        out = out.rstrip()
        while out.endswith((",", ":")):
            out = out[:-1].rstrip()
    return out + "".join(reversed(stack))


def parse_arguments(raw: str, *, allow_truncated: bool) -> tuple[dict[str, Any] | None, list[str]]:
    """A tool call's arguments as a dict, and a note for each repair made to
    read them; (None, []) when they aren't a JSON object even after repair.
    Strict JSON first, then the forms `loads_tolerant` reads. A cut-off
    object (a reply that hit its token limit) is closed only when
    `allow_truncated`: a tool that writes would write a cut-off argument as
    if it were whole."""
    text = (raw or "").strip() or "{}"
    notes: list[str] = []
    try:
        value = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        value = loads_tolerant(text)
        notes = ["read arguments that were not strict JSON"]
        if value is None and allow_truncated:
            closed = close_truncated(text)
            if closed is not None:
                value = loads_tolerant(closed)
                notes = ["closed arguments that were cut off"]
    if not isinstance(value, dict):
        return None, []
    try:  # a Python literal can hold what JSON can't (a set, bytes)
        json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return None, []
    return value, notes
