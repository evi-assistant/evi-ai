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
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

_EDIT_CATEGORIES = ("fs", "code")
VALID_MODES = ("ask", "accept_edits", "plan", "yolo")


def _under_trusted_dir(values: list[str], trusted_dirs: Iterable[str]) -> bool:
    """True if any string arg resolves to a path inside a trusted directory."""
    roots: list[Path] = []
    for d in trusted_dirs or ():
        try:
            roots.append(Path(d).expanduser().resolve())
        except (OSError, ValueError):
            continue
    if not roots:
        return False
    for v in values:
        try:
            p = Path(v).expanduser().resolve()
        except (OSError, ValueError):
            continue
        for r in roots:
            try:
                if p == r or p.is_relative_to(r):
                    return True
            except (OSError, ValueError):
                continue
    return False


def _host_trusted(values: list[str], trusted_domains: Iterable[str]) -> bool:
    """True if any string arg is a URL whose host matches a trusted domain
    (exact or a subdomain of it)."""
    domains = [d.lower().lstrip(".") for d in (trusted_domains or ()) if d.strip()]
    if not domains:
        return False
    for v in values:
        host = (urlparse(v).hostname or "").lower()
        if not host:
            continue
        for d in domains:
            if host == d or host.endswith("." + d):
                return True
    return False


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


def _is_protected(values: list[str], protected_paths: Iterable[str]) -> bool:
    """True if any path arg matches a protected pattern (full path or basename)."""
    pats = [p for p in (protected_paths or ()) if p]
    if not pats:
        return False
    for v in values:
        base = Path(v).name
        for pat in pats:
            if fnmatch.fnmatch(v, pat) or fnmatch.fnmatch(base, pat):
                return True
    return False


def decide(
    mode: str,
    auto_approve: Iterable[str],
    rules: Iterable[str],
    tool_name: str,
    tool_category: str,
    tool_args: str | dict,
    trusted_dirs: Iterable[str] = (),
    trusted_domains: Iterable[str] = (),
    hard_deny: Iterable[str] = (),
    protected_paths: Iterable[str] = (),
) -> str:
    """Return 'allow', 'deny', or 'ask' for one tool call."""
    values = _arg_values(tool_args)

    # 0. Hard-deny: unconditional, evaluated before EVERYTHING (even yolo) so an
    #    allow rule or yolo can't override it.
    for rule in hard_deny or ():
        # hard_deny entries are deny-only — accept "<tool-glob> [arg-glob]" with
        # or without a leading "deny".
        norm = rule if rule.split(None, 1)[:1] == ["deny"] else f"deny {rule}"
        if _rule_action(norm, tool_name, values) == "deny":
            return "deny"

    if mode == "yolo":
        return "allow"
    if mode == "plan":
        return "deny"

    for rule in rules or ():
        action = _rule_action(rule, tool_name, values)
        if action:  # first match wins (deny or allow); explicit deny beats trust
            return action

    # Protected paths: a fs/code write to a sensitive file must never be silently
    # auto-approved by accept_edits / auto-approve / trusted-dir — force a prompt.
    # (An explicit allow rule above already returned, honouring user intent.)
    if tool_category in _EDIT_CATEGORIES and _is_protected(values, protected_paths):
        return "ask"

    if mode == "accept_edits" and tool_category in _EDIT_CATEGORIES:
        return "allow"
    if tool_category in set(auto_approve or ()):
        return "allow"
    # Trusted scopes: files under a trusted dir, or web fetches to a trusted host.
    if tool_category in _EDIT_CATEGORIES and _under_trusted_dir(values, trusted_dirs):
        return "allow"
    if tool_category == "web" and _host_trusted(values, trusted_domains):
        return "allow"
    return "ask"
