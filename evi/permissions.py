"""Permission policy — decide allow / deny / ask for a tool call.

Layered on top of eVi's existing per-category auto-approve:

1. **Modes** (`auto.mode`): ``yolo`` allows everything, ``plan`` denies every
   tool (read-only planning), ``accept_edits`` auto-allows file edits
   (fs/code), ``ask`` (default) is the normal behaviour.
2. **Rules** (`auto.rules`): a first-match allow/deny list. Each rule is
   ``<allow|deny> <tool-glob> [arg-glob]`` — e.g. ``deny shell rm*``,
   ``allow web``, ``deny fs *.env``. The arg-glob (fnmatch) is checked against
   the tool call's string arguments. For a call that runs a shell command line
   (``run_command``, ``monitor`` kind=command) the line is split into segments
   (`evi.shellsplit`) and the rules are matched per segment: an allow rule
   approves only if every segment is allowed, the line parses and every
   redirect stays inside the working folder (see `shell_rule_action`). For
   such a call a specific allow rule's arg-glob is matched against the command
   segments only, never the other arguments: ``allow run_command C:\\proj*``
   no longer approves every command run with ``cwd`` in that folder. A deny
   rule still matches any string argument, ``cwd`` included.
3. **Auto-approve categories** (`auto.auto_approve`): the existing per-category
   allowlist.
4. Otherwise → **ask** (prompt, if there's a UI to prompt with).

Pure + side-effect-free so it's easy to test; the Agent calls `decide()`.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from evi import shellsplit
from evi import workdir as _workdir

_EDIT_CATEGORIES = ("fs", "code")
VALID_MODES = ("ask", "accept_edits", "plan", "yolo")
# Tools that run an argument through a shell, and the argument holding the
# command line; any category-"shell" tool runs its "command" argument. The one
# map for both decide() and the agent's destructive guard (`shell_command_of`).
# `run_python` is intentionally excluded: arbitrary Python is a broader threat
# surface than a shell-command check can secure (it has its own toggle).
SHELL_EXEC_TOOLS = {"run_command": "command", "monitor": "target"}
# Redirect targets that are not files: always fine.
_NULL_SINKS = frozenset(
    {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "nul", "nul:", "con", "$null"}
)


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


def _is_broad(rule: str) -> bool:
    """True for a rule with no real arg-glob (none, or only ``*``/``?``)."""
    parts = rule.split(None, 2)
    return len(parts) < 3 or not parts[2].strip().strip("*? ")


def _arg_dict(tool_args: str | dict) -> dict | None:
    try:
        data = json.loads(tool_args) if isinstance(tool_args, str) else dict(tool_args)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def shell_command_of(tool_name: str, tool_category: str, tool_args: str | dict) -> str | None:
    """The command line a tool call would run through a shell, or None when it
    runs none (not a shell tool, or `monitor` tailing a file)."""
    field = SHELL_EXEC_TOOLS.get(tool_name)
    if field is None:
        if tool_category != "shell":
            return None
        field = "command"
    data = _arg_dict(tool_args)
    if data is None:
        return None
    if tool_name == "monitor" and data.get("kind") != "command":
        return None
    value = data.get(field)
    return value if isinstance(value, str) else None


def _cwd_arg(tool_args: str | dict) -> str:
    """The call's own ``cwd`` argument (run_command), or ""."""
    value = (_arg_dict(tool_args) or {}).get("cwd")
    return value if isinstance(value, str) else ""


def exec_cwd_of(tool_name: str, tool_args: str | dict) -> str | None:
    """The folder a shell call's command line runs in, for resolving relative
    redirect targets: ``""`` for the working folder, a path (relative to it, or
    absolute), or None when it is unknown.

    ``run_command`` runs in its ``cwd`` argument (default: the working folder).
    ``monitor`` passes no ``cwd=`` to its Popen, so its command runs in the eVi
    process's own cwd, which a web session's working folder need not be.
    """
    if tool_name == "monitor":
        try:
            return os.getcwd()
        except OSError:
            return None
    return _cwd_arg(tool_args)


def _redirects_inside(
    sp: shellsplit.Split, workdir: str | Path | None, cwd: str | None
) -> bool:
    """True if every redirect of `sp` targets a null sink or a path inside the
    working folder. Relative targets resolve against `cwd` (see `exec_cwd_of`;
    None = unknown); an expansion in a target, or a ``cd`` anywhere in the
    line, makes the path unknowable, which counts as outside."""
    if not sp.redirects:
        return True
    try:
        root = Path(workdir or _workdir.get_cwd()).expanduser().resolve()
        base: Path | None = root
        if cwd:
            c = Path(cwd).expanduser()
            base = (c if c.is_absolute() else root / c).resolve()
        elif cwd is None:
            base = None
    except (OSError, RuntimeError, ValueError):
        return False
    for r in sp.redirects:
        if r.text.lower() in _NULL_SINKS:
            continue
        if r.dynamic or not r.text:
            return False
        try:
            p = Path(r.text).expanduser()
            if not p.is_absolute():
                if sp.changes_dir or base is None:
                    return False
                p = base / p
            p = p.resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if not (p == root or p.is_relative_to(root)):
            return False
    return True


def shell_rule_action(
    rules: Iterable[str],
    tool_keys: Iterable[str],
    values: list[str],
    command: str,
    *,
    workdir: str | Path | None = None,
    cwd: str | None = "",
    dialect: str | None = None,
    broad: bool = True,
    split: shellsplit.Split | None = None,
) -> str | None:
    """First-match rule decision for a call that runs `command` through a shell.

    The line is split (`evi.shellsplit`) and each command segment is matched
    against the rules on its own; a rule's tool-glob is tried against each of
    `tool_keys` (the tool name, and optionally its category). The result is:

    - ``"deny"`` if the first rule matching the call as a whole (`values`, every
      string argument) is a deny, or the first rule matching ANY segment is;
    - ``"allow"`` if every segment's first matching rule is an allow, the line
      parses, and every redirect stays inside `workdir`. So ``allow run_command
      git*`` approves ``git status && git log`` but not ``git log; python x.py``,
      ``bash -c "..."``, an unbalanced quote or ``git log > /elsewhere``;
    - ``None`` otherwise: no rule decides, and the caller falls through.

    A *broad* allow (no arg-glob, or only ``*``/``?``) is the user allowing every
    command, so it covers any segment, an unparsable line and any redirect. With
    ``broad=False`` (the destructive guard) broad allow rules are ignored, so a
    wildcard can never clear the guard.
    """
    rules = list(rules or ())
    keys = [k for k in tool_keys if k]

    def action(rule: str, vals: list[str]) -> str | None:
        for key in keys:
            act = _rule_action(rule, key, vals)
            if act == "allow" and not broad and _is_broad(rule):
                return None
            if act:
                return act
        return None

    def first(vals: list[str]) -> str | None:
        for rule in rules:
            act = action(rule, vals)
            if act:
                return act
        return None

    if first(values) == "deny":
        return "deny"
    sp = split if split is not None else shellsplit.split(command, dialect)
    covered = bool(sp.commands)
    for seg in sp.commands:  # an unparsable line still yields its leading segments
        act = first([seg.text])
        if act == "deny":
            return "deny"
        if act != "allow":
            covered = False
    if broad and any(_is_broad(r) and action(r, values) == "allow" for r in rules):
        return "allow"
    if not covered or not sp.ok or not _redirects_inside(sp, workdir, cwd):
        return None
    return "allow"


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
    *,
    workdir: str | Path | None = None,
    dialect: str | None = None,
) -> str:
    """Return 'allow', 'deny', or 'ask' for one tool call.

    `workdir` is the session working folder that shell redirects must stay
    inside for an allow rule to apply (default: `evi.workdir.get_cwd()`), and
    `dialect` the shell the command line runs in (default: this platform's).
    """
    values = _arg_values(tool_args)
    command = shell_command_of(tool_name, tool_category, tool_args)
    sp = shellsplit.split(command, dialect) if command is not None else None

    # 0. Hard-deny: unconditional, evaluated before EVERYTHING (even yolo) so an
    #    allow rule or yolo can't override it.
    for rule in hard_deny or ():
        # hard_deny entries are deny-only — accept "<tool-glob> [arg-glob]" with
        # or without a leading "deny".
        norm = rule if rule.split(None, 1)[:1] == ["deny"] else f"deny {rule}"
        if _rule_action(norm, tool_name, values) == "deny":
            return "deny"
        # ...and against each segment of a shell line, so `ls && rm -rf x`
        # can't hide `rm -rf x` from a `run_command rm -rf*` entry.
        if sp is not None and any(
            _rule_action(norm, tool_name, [seg.text]) == "deny" for seg in sp.commands
        ):
            return "deny"

    if mode == "yolo":
        return "allow"
    if mode == "plan":
        return "deny"

    if command is not None:
        action = shell_rule_action(
            rules, (tool_name,), values, command,
            workdir=workdir, cwd=exec_cwd_of(tool_name, tool_args), split=sp,
        )
        if action:
            return action
    else:
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
