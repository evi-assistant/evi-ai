"""Permission policy — decide allow / deny / ask for a tool call.

Layered on top of eVi's existing per-category auto-approve:

1. **Modes** (`auto.mode`): ``yolo`` allows everything, ``plan`` denies every
   tool (read-only planning), ``accept_edits`` auto-allows file edits
   (fs/code), ``ask`` (default) is the normal behaviour.
2. **Rules** (`auto.rules`): a first-match allow/deny list. Each rule is
   ``<allow|deny> <tool-glob> [arg-glob]`` — e.g. ``deny shell rm*``,
   ``allow web``, ``deny fs *.env``. The arg-glob (fnmatch) is checked against
   the tool call's string arguments.
3. **Auto-approve categories** (`auto.auto_approve`): the existing per-category
   allowlist.
4. Otherwise → **ask** (prompt, if there's a UI to prompt with).

Pure + side-effect-free so it's easy to test; the Agent calls `decide()`.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Iterable

_EDIT_CATEGORIES = ("fs", "code")
VALID_MODES = ("ask", "accept_edits", "plan", "yolo")


def _arg_values(tool_args: str | dict) -> list[str]:
    """The string-valued arguments of a tool call (for arg-glob matching)."""
    try:
        data = json.loads(tool_args) if isinstance(tool_args, str) else dict(tool_args)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [str(tool_args)]
    if not isinstance(data, dict):
        return []
    return [v for v in data.values() if isinstance(v, str)]


def _rule_action(rule: str, tool_name: str, values: list[str]) -> str | None:
    """Return 'allow'/'deny' if `rule` matches this call, else None."""
    parts = rule.split(None, 2)
    if len(parts) < 2:
        return None
    action = parts[0].lower()
    if action not in ("allow", "deny"):
        return None
    tool_glob = parts[1]
    if not fnmatch.fnmatch(tool_name, tool_glob):
        return None
    if len(parts) > 2:
        arg_glob = parts[2]
        if not any(fnmatch.fnmatch(v, arg_glob) for v in values):
            return None
    return action


def decide(
    mode: str,
    auto_approve: Iterable[str],
    rules: Iterable[str],
    tool_name: str,
    tool_category: str,
    tool_args: str | dict,
) -> str:
    """Return 'allow', 'deny', or 'ask' for one tool call."""
    if mode == "yolo":
        return "allow"
    if mode == "plan":
        return "deny"

    values = _arg_values(tool_args)
    for rule in rules or ():
        action = _rule_action(rule, tool_name, values)
        if action:  # first match wins (deny or allow)
            return action

    if mode == "accept_edits" and tool_category in _EDIT_CATEGORIES:
        return "allow"
    if tool_category in set(auto_approve or ()):
        return "allow"
    return "ask"
