"""Repairs for the tool calls small models get wrong.

Arguments that aren't strict JSON, cut off mid-object, mistyped or misnamed
are read and fitted to the tool where that is unambiguous; each repair is
named, so the model (and the log) can see what eVi changed.
"""

from __future__ import annotations

import ast
import difflib
import inspect
import json
import math
import re
from typing import Any, Callable, Iterable

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


# Argument names models use for one the tool calls something else.
ALIASES = {"file_path": "path", "filepath": "path", "filename": "path", "file": "path",
           "cmd": "command"}

_INT = re.compile(r"[+-]?\d+")
_TRUE, _FALSE = {"true", "yes"}, {"false", "no"}
_EXAMPLE = {"string": "...", "integer": 1, "number": 1, "boolean": True, "array": [],
            "object": {}}


def _coerce(value: Any, schema: dict[str, Any]) -> tuple[Any, bool]:
    """(`value` as `schema`'s type, True) when the conversion is unambiguous;
    (`value`, False) when it already fits or can't be converted."""
    kind = schema.get("type")
    if kind == "integer":
        if isinstance(value, str) and _INT.fullmatch(value.strip()):
            return int(value.strip()), True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
    elif kind == "number" and isinstance(value, str):
        text = value.strip()
        if _INT.fullmatch(text):
            return int(text), True
        try:
            num = float(text)
        except ValueError:
            return value, False
        if math.isfinite(num):
            return num, True
    elif kind == "boolean" and isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE | _FALSE:
            return word in _TRUE, True
    elif kind == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value), True
    elif kind == "array":
        if isinstance(value, str):
            parsed = loads_tolerant(value)
            return (parsed if isinstance(parsed, list) else [value]), True
        if value is not None and not isinstance(value, (list, dict)):
            return [value], True
    elif kind == "object" and isinstance(value, str):
        parsed = loads_tolerant(value)
        if isinstance(parsed, dict):
            return parsed, True
    return value, False


def fit_arguments(
    args: dict[str, Any], schema: dict[str, Any], func: Callable[..., Any]
) -> tuple[dict[str, Any], list[str]]:
    """`args` fitted to the tool: an alias renamed to the name the schema
    uses (unless that name is given too), each value coerced to its schema
    type (`_coerce`), and an argument `func` doesn't take dropped (none is
    dropped for a function that takes **kwargs, such as an MCP tool's).
    Returns the new dict and a note for each change."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    props = props if isinstance(props, dict) else {}
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        params = {}
    takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    out: dict[str, Any] = {}
    notes: list[str] = []
    for key, value in args.items():
        target = ALIASES.get(key)
        if key not in props and target in props and target not in args and target not in out:
            notes.append(f"{key} -> {target}")
            key = target
        out[key] = value
    for key, value in list(out.items()):
        sub = props.get(key)
        if isinstance(sub, dict):
            new, changed = _coerce(value, sub)
            if changed:
                out[key] = new
                notes.append(f"{key}: {json.dumps(value)} -> {json.dumps(new)}")
    if params and not takes_any:
        for key in [k for k in out if k not in params or k == "ctx"]:
            del out[key]
            notes.append(f"dropped unknown argument {key}")
    return out, notes


def missing_required(args: dict[str, Any], func: Callable[..., Any]) -> list[str]:
    """The arguments `func` needs (no default) that `args` lacks."""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return []
    named = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    return [n for n, p in params.items()
            if n != "ctx" and p.kind in named and p.default is inspect.Parameter.empty
            and n not in args]


def usage(name: str, schema: dict[str, Any]) -> str:
    """How to call a tool: its arguments and their types, the required ones
    first and the others marked '?', then an example with the required ones,
    e.g. 'read_file(path: string, offset?: integer), e.g. {"path": "..."}'."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    props = props if isinstance(props, dict) else {}
    required = [r for r in (schema.get("required") or []) if r in props]

    def kind(key: str) -> str:
        sub = props.get(key)
        return str(sub.get("type", "any")) if isinstance(sub, dict) else "any"

    parts = [f"{k}: {kind(k)}" for k in required]
    parts += [f"{k}?: {kind(k)}" for k in props if k not in required]
    example = {k: _EXAMPLE.get(kind(k), "...") for k in required}
    return f"{name}({', '.join(parts)}), e.g. {json.dumps(example)}"


def unknown_tool_message(name: str, known: Iterable[str]) -> str:
    """What to tell a model that called a tool there is none of: the close
    names, else how to find one (search_tools, when tools load on demand),
    else the tools there are."""
    names = sorted(set(known))
    msg = f"ERROR: unknown tool '{name}'"
    close = difflib.get_close_matches(str(name), names, n=3, cutoff=0.6)
    if close:
        return f"{msg}. Did you mean: {', '.join(close)}?"
    if "search_tools" in names:
        return f"{msg}. Use search_tools to find a tool by what it does."
    if names:
        more = ", ..." if len(names) > 40 else ""
        return f"{msg}. The tools are: {', '.join(names[:40])}{more}"
    return msg
