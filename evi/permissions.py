"""Permission policy — decide allow / deny / ask for a tool call.

Layered on top of eVi's existing per-category auto-approve:

0. **Hard-deny** (`auto.hard_deny`), then **protected paths**
   (`auto.protected_paths`): a call that would write a protected file (eVi's
   instruction / config / hook / task files, CI, git hooks, shell start-up and
   secret files; see `DEFAULT_PROTECTED_PATHS`) asks, in every mode, ``yolo``
   included, and no allow rule clears it (a deny rule still denies). Paths are
   the call's path arguments (`evi.tools.base.Tool.path_args`) and, for a
   shell line, its output redirects and the files of a few writing commands
   (``tee``, ``cp``, ``Set-Content`` ...), matched as written and on their
   real path, so a symlink or junction to a protected file is caught. This is
   a guardrail, not a sandbox: code a tool runs (``run_python``, a script) can
   still write anywhere.
1. **Modes** (`auto.mode`): ``yolo`` allows everything, ``plan`` denies every
   tool (read-only planning), ``accept_edits`` auto-allows file edits and
   code reading (fs, code_read; never running code), ``ask`` (default) is the
   normal behaviour.
2. **Rules** (`auto.rules`): a first-match allow/deny list. Each rule is
   ``<allow|deny> <tool-glob> [arg-glob]`` — e.g. ``deny shell rm*``,
   ``allow web``, ``deny fs *.env``. The tool-glob matches the tool's name,
   its category or the call's own category (`classify_call`: ``shell`` for
   any call that runs a shell line, so ``monitor`` kind=command too;
   ``code_read`` for one that only reads code), though an allow rule never
   approves a shell line by its tool's category (``allow code`` doesn't cover
   ``monitor`` kind=command) or running code by ``code_read``
   (`allow_rule_keys`); the arg-glob (fnmatch) is
   checked against the tool call's string arguments. For a call that runs a
   shell command line (``run_command``, ``monitor`` kind=command) the line is
   split into segments (`evi.shellsplit`) and the rules are matched per
   segment: an allow rule approves only if every segment is allowed, the line
   parses and every redirect stays inside the working folder (see
   `shell_rule_action`). For
   such a call a specific allow rule's arg-glob is matched against the command
   segments only, never the other arguments: ``allow run_command C:\\proj*``
   no longer approves every command run with ``cwd`` in that folder. A deny
   rule still matches any string argument, ``cwd`` included.
   Then **secret reads**: an fs/code/code_read call whose path argument
   matches a protected pattern other than the write-only defaults (``.env``,
   ``*.pem``, ``id_rsa`` ...; `protected_read`) asks, so auto-approve never
   pulls a secret into the model's context silently. An explicit allow rule
   above still wins.
3. **Auto-approve categories** (`auto.auto_approve`), matched against the
   call's own category: ``code`` is running code (``run_python``, a linter
   that runs project code) and also covers ``code_read`` (``python_symbols``,
   ``monitor`` tailing a file, ``check_file`` with ruff); a shell line is
   ``shell`` whatever tool runs it. In a run a **remote trigger** drives
   (federation, A2A, a routine, a channel push: ``remote_trigger``) a call
   that runs code (`runs_code`) is never approved by a category, by
   accept_edits or by a trusted dir: only an explicit allow rule lets it
   through, else the run's "deny" policy refuses it. A local unattended run
   (the scheduler, ``evi run``, a teammate, a Telegram turn) goes by the list
   the user wrote, ``shell`` or ``code`` included. Then **trusted
   scopes**: an fs/code_read call whose path arguments ALL resolve (against
   the working folder, on their real path) under a `trusted_dirs` entry, and
   whose glob argument (`find_files`, `search_files`) can't climb out with
   ``..``, or a web call to a `trusted_domains` host. A call with no path
   argument (``run_python``) is never approved by a trusted dir.
4. Otherwise → **ask** (prompt, if there's a UI to prompt with).

Pure + side-effect-free so it's easy to test; the Agent calls `decide()`.
"""

from __future__ import annotations

import fnmatch
import glob as _glob
import json
import os
import re
import socket
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from evi import shellsplit
from evi import workdir as _workdir
from evi.tools.base import CODE_READ_TOOLS, READ_ONLY_TOOLS, glob_values, path_values

# A call's own category (`classify_call`, `Tool.category_for`) when it only
# reads code: auto-approved by default, unlike "code", which runs it.
CODE_READ = "code_read"
# Call categories that run code: "code" (run_python, a linter that runs
# project code) and "shell" (a shell line). In a remote-triggered run no
# category approves one (`runs_code`).
EXEC_CATEGORIES = frozenset({"code", "shell"})
# Tools that run arbitrary code whatever category one call of theirs has: a
# sandboxed run_python is "code_read", but still runs code, so a
# remote-triggered run never approves it by category, and an `allow
# code_read` rule never approves it (`allow_rule_keys`).
CODE_EXEC_TOOLS = frozenset({"run_python"})
# Linters `check_file` may run that read only the file and their own static
# config, so a call that can only reach them is "code_read". eslint loads the
# project's eslint.config.js, cargo clippy builds the crate (build.rs, proc
# macros) and go vet may run cgo or fetch a toolchain: those run project code,
# so such a call stays "code" and asks (`classify_call`).
READ_ONLY_LINTERS = frozenset({"ruff", "pyflakes"})
# Every call category a rule's tool-glob or auto_approve can name beyond the
# tools' own categories (for a config linter).
CALL_CATEGORIES = frozenset({CODE_READ, "shell"})
# Calls whose path arguments are files the tool reads or writes: a protected
# secret among them asks (`protected_read`).
_FILE_CATEGORIES = ("fs", "code", CODE_READ)
# What accept_edits allows unasked, and what a trusted dir approves: file
# edits and code reading, never running code.
_EDIT_CATEGORIES = ("fs", CODE_READ)
# The protected paths every config gets (`effective_protected_paths`). Patterns
# without a "/" match a file's name; ones with a "/" match its path (a
# relative one at any depth); "~" is the home folder. Matching ignores case.
#
# The stock `[auto] protected_paths` list (`evi.config.AutoSettings`): secret
# and shell start-up files. Guarded against writes by any tool and reads by an
# fs/code tool (`protected_read`); a user opts out of one by deleting it from
# their list. `evi.commands` also refuses to inline these into a prompt.
SECRET_PROTECTED_PATHS: tuple[str, ...] = (
    ".env", "*.env", ".env.*", ".npmrc", ".gitconfig", ".pypirc",
    ".bashrc", ".zshrc", ".bash_profile", ".profile",
    "*.pem", "id_rsa", "id_ed25519",
)
# The instruction, config, hook, task and CI files an injected agent could
# rewrite to persist. Guarded against writes only (reading EVI.md is normal),
# and added to every saved list unless named in `protected_paths_remove`.
# Keep them OUT of the stock `protected_paths` default: `evi.commands` treats
# that list as "never inline", which would break `@AGENTS.md` in a template.
WRITE_PROTECTED_PATHS: tuple[str, ...] = (
    ".evi.toml", "EVI.md", "AGENTS.md", ".evi/**", "~/.evi/**",
    ".git/hooks/**", ".git/config", ".git/modules/**/hooks/**", ".git/modules/**/config",
    ".github/workflows/**", ".vscode/tasks.json",
)
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = SECRET_PROTECTED_PATHS + WRITE_PROTECTED_PATHS
_ABS_PATTERN = re.compile(r"^(?:/|[a-z]:/)")
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


def _base_dir(workdir: str | Path | None) -> Path:
    """The folder relative path arguments resolve against (`evi.workdir`)."""
    p = Path(workdir or _workdir.get_cwd())
    try:
        return p.expanduser()
    except RuntimeError:  # a "~name" pathlib can't expand: use it as written
        return p


def _is_network_path(p: Path) -> bool:
    """A UNC (``//host/share``) or device (``//?/``) path, Windows only. A
    model-chosen one is never resolved: resolving opens it, which already
    connects (and authenticates) to that host before anyone approved the call.
    Run a value through `_local_spelling` first so a ``\\\\?\\C:\\`` spelling of
    a local path is treated as the local path it is."""
    return p.drive.startswith(("\\\\", "//"))


_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_DRIVE_PATH = re.compile(r"[A-Za-z]:(?:\\|\Z)")
# \\host\X$\rest: a drive's administrative share.
_ADMIN_SHARE = re.compile(r"\\\\([^\\]+)\\([A-Za-z])\$(\\.*)?\Z", re.DOTALL)
_LOOPBACK_V4 = re.compile(r"127(?:\.\d{1,3}){3}\Z")


@lru_cache(maxsize=1)
def _own_host_names() -> frozenset[str]:
    """This machine's names for a UNC path to itself (no lookup, no I/O)."""
    names = {"localhost", "::1", "0--1.ipv6-literal.net"}
    try:
        names.add(socket.gethostname().lower())
    except OSError:
        pass
    names.add(os.environ.get("COMPUTERNAME", "").lower())
    return frozenset(n for n in names if n)


def _local_spelling(value: str) -> str | None:
    """Windows: `value` with a ``\\\\?\\`` / ``\\\\.\\`` / ``\\??\\`` device
    prefix, or as a loopback administrative share (``\\\\localhost\\C$\\x``),
    rewritten lexically (no I/O) to the plain drive path it opens, so it is
    matched and resolved like one; ``\\\\?\\UNC\\host\\share`` becomes
    ``\\\\host\\share``. None when what the path opens can't be told without
    opening it: a device path naming no drive (``\\\\?\\GLOBALROOT\\...``,
    ``\\\\?\\Volume{...}``, a pipe) or another host's ``X$`` share (which may
    be this machine under another name). Elsewhere `value` is returned as is."""
    if os.name != "nt":
        return value
    s = value.replace("/", "\\")
    changed = False
    if s.startswith(_DEVICE_PREFIXES):
        rest = s[4:]
        if _DRIVE_PATH.match(rest):
            return rest if len(rest) > 2 else rest + "\\"
        if rest[:4].lower() != "unc\\":
            return None
        s, changed = "\\\\" + rest[4:], True
    m = _ADMIN_SHARE.match(s)
    if m:
        host = m.group(1).lower()
        if host not in _own_host_names() and not _LOOPBACK_V4.match(host):
            return None
        return f"{m.group(2)}:" + (m.group(3) or "\\")
    return s if changed else value


def _glob_escapes(pattern: str) -> bool:
    """True if a glob argument can select paths outside the folder it is
    applied to: an absolute, drive- or home-relative one, or one with a
    ``..`` part (pathlib's glob follows ``..``)."""
    s = pattern.replace("\\", "/")
    return s.startswith(("/", "~")) or bool(re.match(r"[A-Za-z]:", s)) or ".." in s.split("/")


def _under_trusted_dir(
    paths: list[str], trusted_dirs: Iterable[str], workdir: str | Path | None = None
) -> bool:
    """True if there is at least one path argument and EVERY one resolves (on
    its real path, relative ones against the working folder) inside a trusted
    directory. Only path arguments count: a code or content body never does.
    A network or opaque device path is never trusted (nor resolved)."""
    roots: list[Path] = []
    for d in trusted_dirs or ():
        local = _local_spelling(str(d)) if str(d).strip() else None
        if local is None:
            continue
        try:
            r = Path(local).expanduser()
            if _is_network_path(r):
                continue  # a network path argument is never trusted anyway
            roots.append(r.resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    if not roots or not paths:
        return False
    base = _base_dir(workdir)
    for v in paths:
        local = _local_spelling(v)
        if local is None:
            return False
        try:
            p = Path(local).expanduser()
            p = p if p.is_absolute() else base / p
            if _is_network_path(p):
                return False
            p = p.resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if not any(p == r or p.is_relative_to(r) for r in roots):
            return False
    return True


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


def _rule_action(
    rule: str, tool_name: str, values: list[str], tool_category: str = ""
) -> str | None:
    """Return 'allow'/'deny' if `rule` matches this call, else None. The
    rule's tool-glob matches the tool name or, when given, its category, so
    ``deny shell rm*`` covers ``run_command`` and ``allow web`` ``web_fetch``."""
    parts = rule.split(None, 2)
    if len(parts) < 2:
        return None
    action = parts[0].lower()
    if action not in ("allow", "deny"):
        return None
    tool_glob = parts[1]
    if not fnmatch.fnmatch(tool_name, tool_glob) and not (
        tool_category and fnmatch.fnmatch(tool_category, tool_glob)
    ):
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


def unmatched_rules(
    rules: Iterable[str], tool_names: Iterable[str], categories: Iterable[str]
) -> list[str]:
    """The `rules` (or `hard_deny` entries, with or without a leading action)
    whose tool-glob matches none of `tool_names` and none of `categories`, so
    they can never apply. For a config linter; a malformed rule is left out."""
    keys = [k for k in (*tool_names, *categories) if k]
    out: list[str] = []
    for rule in rules or ():
        parts = str(rule).split()
        if parts and parts[0].lower() in ("allow", "deny"):
            parts = parts[1:]
        if parts and not any(fnmatch.fnmatch(k, parts[0]) for k in keys):
            out.append(rule)
    return out


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


def rule_keys(
    tool_name: str, tool_category: str, command: str | None, call_category: str = ""
) -> tuple[str, ...]:
    """What a rule's tool-glob is matched against: the tool's name, its
    category, the call's own category (`classify_call`) and ``shell`` when the
    call runs a shell line (`command`, see `shell_command_of`): ``monitor``
    kind=command is category "code" but runs a shell line, so ``hard_deny =
    ["shell rm -rf*"]`` must cover it too."""
    keys = (tool_name, tool_category, call_category, "shell" if command is not None else "")
    return tuple(dict.fromkeys(k for k in keys if k))


def _runs_shell(tool_name: str, tool_category: str, data: dict | None) -> bool:
    """True if the call runs a shell line, even one `shell_command_of` can't
    read: a ``monitor`` kind=command call whose target isn't a string, or
    arguments that don't parse, still count (fail closed)."""
    if tool_category == "shell":
        return True
    if tool_name not in SHELL_EXEC_TOOLS:
        return False
    if tool_name == "monitor":
        return data is None or data.get("kind") == "command"
    return True


def _lints_read_only(data: dict | None) -> bool:
    """True if a ``check_file`` call can only reach a linter that runs no
    project code (`READ_ONLY_LINTERS`), or none: its file's type has no
    linter (`evi.codeintel`), or only ruff / pyflakes. Decided by the file's
    extension, not by what is installed, so it is the same everywhere."""
    path = (data or {}).get("path")
    if not isinstance(path, str):
        return False
    from evi import codeintel

    cmds = codeintel._LINTERS.get(Path(path).suffix.lower())
    return not cmds or all(c[0] in READ_ONLY_LINTERS for c in cmds)


def classify_call(tool_name: str, tool_category: str, tool_args: str | dict) -> str:
    """The category the policy checks one call under, from what the call does
    rather than the toggle its tool is in: ``"shell"`` when it runs a shell
    line (``run_command``, ``monitor`` kind=command), ``"code_read"`` for a
    "code" tool that only reads code (`evi.tools.base.CODE_READ_TOOLS`, and
    ``check_file`` on a file whose linters run no project code), else
    `tool_category`. Pure; `evi.tools.base.Tool.category_for` adds what only
    the tool knows (``run_python`` while a sandbox is active)."""
    data = _arg_dict(tool_args)
    if _runs_shell(tool_name, tool_category, data):
        return "shell"
    if tool_category == "code" and (
        tool_name in CODE_READ_TOOLS or (tool_name == "check_file" and _lints_read_only(data))
    ):
        return CODE_READ
    return tool_category


def runs_code(tool_name: str, tool_category: str, call_category: str) -> bool:
    """True if the call runs code: its own category is "code" or "shell", or
    the tool is one of `CODE_EXEC_TOOLS` (sandboxed or not)."""
    return call_category in EXEC_CATEGORIES or (
        tool_name in CODE_EXEC_TOOLS and tool_category == "code"
    )


def allow_rule_keys(
    tool_name: str, tool_category: str, command: str | None, call_category: str = ""
) -> tuple[str, ...]:
    """The `rule_keys` an allow rule's tool-glob may match; a deny rule (and
    hard_deny) matches every one, so it never gets looser. A call that runs a
    shell line is allowed only by its tool's name or ``shell``, never by the
    toggle category its tool is in, so ``allow code`` approves ``monitor``
    kind=command no more than it does ``run_command``. A call that runs code
    (`runs_code`) is never allowed by ``code_read``, the category a sandboxed
    run_python has, so ``allow code_read`` stays a rule about reading code."""
    keys = rule_keys(tool_name, tool_category, command, call_category)
    if command is not None or call_category == "shell":
        return tuple(k for k in keys if k in (tool_name, "shell"))
    if runs_code(tool_name, tool_category, call_category):
        return tuple(k for k in keys if k != CODE_READ)
    return keys


def approved_categories(auto_approve: Iterable[str]) -> set[str]:
    """The call categories `[auto] auto_approve` lets run unasked: the list,
    plus "code_read" when it names "code" (running code covers reading it, so
    a list saved before "code_read" existed keeps python_symbols unasked)."""
    out = {str(c).strip() for c in auto_approve or () if str(c).strip()}
    if "code" in out:
        out.add(CODE_READ)
    return out


def _call_category(
    tool_name: str, tool_category: str, tool_args: str | dict, call_category: str | None
) -> str:
    """`call_category` as given (`Tool.category_for`), else `classify_call`;
    "shell" for a call that runs a shell line whatever the caller passed."""
    own = classify_call(tool_name, tool_category, tool_args)
    if own == "shell":
        return own
    return call_category or own


def approved_by_category(
    auto_approve: Iterable[str],
    tool_name: str,
    tool_category: str,
    tool_args: str | dict,
    *,
    call_category: str | None = None,
    remote_trigger: bool = False,
) -> bool:
    """True if `[auto] auto_approve` alone lets this call run unasked: its
    own category (`call_category`, default `classify_call`) is approved
    (`approved_categories`), and it doesn't run code in a run a remote
    trigger drives (`remote_trigger`). The category step of `decide`, for a
    caller that short-cuts it (the agent's pre-approval check); protected
    paths and rules aren't looked at."""
    cat = _call_category(tool_name, tool_category, tool_args, call_category)
    if remote_trigger and runs_code(tool_name, tool_category, cat):
        return False
    return cat in approved_categories(auto_approve)


def auto_approve_migration(auto_approve: Iterable[str]) -> list[str] | None:
    """`[auto] auto_approve` with "code" replaced by "code_read", every other
    entry kept in order, or None when it needs no change. Lists written before
    "code_read" existed hold the old default's "code", which now means running
    code (run_python) unasked; one that names "code_read" beside "code" keeps
    that on purpose, so it is left alone."""
    cats = [str(c).strip() for c in auto_approve or () if str(c).strip()]
    if "code" not in cats or CODE_READ in cats:
        return None
    out: list[str] = []
    for c in cats:
        c = CODE_READ if c == "code" else c
        if c not in out:
            out.append(c)
    return out


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
    line, makes the path unknowable, which counts as outside.

    Relative targets (and a relative `cwd`) are joined to the working folder
    as given, not to its resolved form: on a mapped drive that resolves to a
    ``\\\\host\\share`` path, and a relative target there stays on the
    working folder's own share, so it is resolved. Only an absolute target or
    cwd naming a network path is never resolved (the model chose that host)."""
    if not sp.redirects:
        return True
    try:
        start = Path(workdir or _workdir.get_cwd()).expanduser()
        root = start.resolve()
        base: Path | None = start
        if cwd:
            c = Path(cwd).expanduser()
            if c.is_absolute():
                base = None if _is_network_path(c) else c
            else:
                base = start / c
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
            if p.is_absolute():
                if _is_network_path(p):
                    return False
            else:
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
    allow_keys: Iterable[str] | None = None,
) -> str | None:
    """First-match rule decision for a call that runs `command` through a shell.

    The line is split (`evi.shellsplit`) and each command segment is matched
    against the rules on its own; a rule's tool-glob is tried against each of
    `tool_keys` (the tool name first, then optionally its category), an allow
    rule's only against those also in `allow_keys` (default: the tool name and
    ``shell``, as `allow_rule_keys` gives for a shell line, so an ``allow code
    git*`` rule clears the destructive guard for ``monitor`` kind=command no
    more than for ``run_command``). The result is:

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
    allowing = set([*keys[:1], "shell"] if allow_keys is None else allow_keys)

    def action(rule: str, vals: list[str]) -> str | None:
        for key in keys:
            act = _rule_action(rule, key, vals)
            if act == "allow" and key not in allowing:
                continue  # this key can't allow the call; another one may
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


def _evi_home_pattern() -> str:
    """``<EVI_HOME>/**`` when ``EVI_HOME`` moves eVi's home, else "". The
    folder's own glob characters are escaped, so a home named ``a[1]`` still
    matches itself."""
    home = os.environ.get("EVI_HOME", "").strip()
    if not home:
        return ""
    path = Path(home)
    try:
        path = path.expanduser()
    except RuntimeError:  # a "~name" pathlib can't expand: use it as written
        pass
    return _glob.escape(str(path).rstrip("/\\")) + "/**"


def effective_protected_paths(
    configured: Iterable[str] | None = None, removed: Iterable[str] = ()
) -> list[str]:
    """The protected patterns in force: the configured ones (None: the stock
    `SECRET_PROTECTED_PATHS`) plus every `WRITE_PROTECTED_PATHS` entry, so a
    config saved before those were added still gets them, minus the entries
    listed in `removed` (``[auto] protected_paths_remove``). A stock secret
    pattern a saved list leaves out stays out: deleting it from
    `protected_paths` has always been how a user opts out of one. With
    ``EVI_HOME`` set, eVi's home is protected along with ``~/.evi/**``."""
    drop = {str(r).strip() for r in removed or () if str(r).strip()}
    mine = SECRET_PROTECTED_PATHS if configured is None else configured
    out: list[str] = []
    for pat in (*mine, *WRITE_PROTECTED_PATHS):
        pat = str(pat).strip()
        if pat and pat not in drop and pat not in out:
            out.append(pat)
    home = _evi_home_pattern()
    if home and "~/.evi/**" not in drop and home not in out:
        out.append(home)
    return out


def _canon(path: str) -> str:
    """A lower-case, ``/``-separated spelling of `path` for protected-path
    matching. Windows drops trailing dots and spaces and an ``:stream`` suffix
    from a name (``EVI.md.`` and ``EVI.md::$DATA`` both write EVI.md), so those
    are dropped here too."""
    parts = path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if i == 0 and len(part) == 2 and part[1] == ":":
            continue  # a drive
        part = part.split(":", 1)[0]
        parts[i] = part if part in (".", "..") else part.rstrip(". ")
    return "/".join(parts).lower()


def _path_forms(value: str, base: Path) -> set[str] | None:
    """Every spelling of a path argument to match: as written, made absolute
    (relative to `base`) and normalised, and its real path (symlinks and
    junctions followed). A device-path spelling is read as the drive path it
    opens (`_local_spelling`); None when what it opens can't be told."""
    local = _local_spelling(value)
    if local is None:
        return None
    forms = {_canon(value), _canon(local)}
    try:
        p = Path(local).expanduser()
        if not p.is_absolute():
            p = base / p
        forms.add(_canon(os.path.normpath(str(p))))
        if not _is_network_path(p):
            forms.add(_canon(str(p.resolve())))
    except (OSError, RuntimeError, ValueError):
        pass
    return forms


def _pattern_forms(pattern: str) -> set[str]:
    """`pattern` canonicalised, ``~`` expanded; an absolute pattern also with
    its fixed leading folder resolved (a short or linked home folder). A
    network folder is never resolved: patterns can come from a repo's
    ``.evi.toml``, and resolving ``//host/share`` connects (and authenticates)
    to that host, on every tool call."""
    pat = pattern.strip()
    if pat.startswith("~"):
        pat = os.path.expanduser(pat)
    pat = _local_spelling(pat) or pat
    forms = {_canon(pat)}
    if _ABS_PATTERN.match(_canon(pat)):
        glob_at = min([pat.find(c) for c in "*?[" if c in pat] or [len(pat)])
        cut = max(pat.rfind("/", 0, glob_at), pat.rfind("\\", 0, glob_at))
        head = Path(pat[:cut]) if cut > 0 else None
        if head is not None and not _is_network_path(head):
            try:
                resolved = str(head.resolve())
            except (OSError, RuntimeError, ValueError):
                resolved = pat[:cut]
            forms.add(_canon(resolved + "/" + pat[cut + 1:]))
    return forms


def _matches(form: str, pat: str) -> bool:
    if "/" not in pat:
        name = form.rstrip("/").rsplit("/", 1)[-1]
        return fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(form, pat)
    if _ABS_PATTERN.match(pat):
        return fnmatch.fnmatchcase(form, pat)
    return fnmatch.fnmatchcase(form, pat) or fnmatch.fnmatchcase(form, "*/" + pat)


def _first_protected(
    targets: list[tuple[str, Path, str]], protected_paths: Iterable[str]
) -> str | None:
    """The first `(value, base, shown)` target matching a protected pattern,
    as `shown` (the path as written, for a message), or None. A target whose
    path can't be told (`_path_forms` gives None) counts as protected: fail
    closed."""
    if not targets:
        return None
    pats = [f for p in (protected_paths or ()) if str(p).strip() for f in _pattern_forms(str(p))]
    if not pats:
        return None
    for value, base, shown in targets:
        forms = _path_forms(value, base)
        if forms is None or any(_matches(f, p) for f in forms for p in pats):
            return shown
    return None


# Commands that write the files their arguments name (`protected_write`).
# Every plain argument counts, a copy's source too: asking about a read is the
# safe side. `sed` / `perl` count only with an in-place flag, `dd` only its of=.
_WRITING_COMMANDS = frozenset({
    "tee", "cp", "mv", "install", "ln", "copy", "move", "xcopy", "robocopy", "ren",
    "rename", "tee-object", "out-file", "set-content", "add-content", "new-item",
    "copy-item", "move-item", "rename-item", "sc", "ac", "ni", "cpi", "mi",
})
_IN_PLACE_EDITORS = frozenset({"sed", "perl"})
_CHDIR_COMMANDS = frozenset({"cd", "chdir", "pushd", "set-location", "sl", "push-location"})
_POP_COMMANDS = frozenset({"popd", "pop-location"})
# A shell expansion (or a cmd %VAR%): the word's value isn't known statically.
_EXPANSION = re.compile(r"[$%`]")


def _program(seg: shellsplit.Segment) -> tuple[int, str] | None:
    """The index and lower-cased base name of the program `seg` runs
    (leading ``NAME=value`` words and wrappers such as ``sudo`` skipped)."""
    ci = shellsplit._command_index(seg.words)
    return None if ci is None else (ci, shellsplit._base(seg.words[ci]))


def _in_place(args: tuple[str, ...]) -> bool:
    """True if `args` hold ``sed`` / ``perl``'s in-place flag (``-i``,
    ``-i.bak``, ``-pi``, ``--in-place``)."""
    return any(
        a == "--in-place" or a.startswith("--in-place=")
        or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
        for a in args
    )


def _option_value(arg: str) -> str:
    """The value an option word carries inline (pwsh ``-Path:x``, GNU
    ``--target-directory=x``), or "" for an option without one."""
    if arg.startswith("--"):
        return arg.split("=", 1)[1] if "=" in arg else ""
    return arg.split(":", 1)[1] if ":" in arg else ""


def _leaf(path: str) -> str:
    """The last name in `path` ("" for ``.``, ``..`` or a bare separator)."""
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return "" if name in (".", "..") else name


# A name joined to a writer's argument to ask "is this a folder whose files
# are protected?": `cp x .git/hooks` writes `.git/hooks/<something>`.
_DIR_PROBE = "evi-dir-probe"
# Past this many arguments only the probe is joined to each (the cost is
# every argument times every other argument's name).
_MAX_NAMED_ARGS = 8


def _written_by_commands(sp: shellsplit.Split) -> list[tuple[str, str]]:
    """The files a writing command in the line (`_WRITING_COMMANDS`, ``sed
    -i``, ``dd of=``) may write, as ``(path, shown)`` pairs: its plain
    arguments and inline option values (``-FilePath:x``), options and cmd
    switches left out. An argument holding an expansion is skipped, like a
    dynamic redirect. A copy, move, link or new item may name the FOLDER it
    writes into (``cp pre-commit .git/hooks``), so each argument is also
    checked as one: with every other argument's name in it, a file under
    that (a recursive copy), and any file in it."""
    out: list[tuple[str, str]] = []
    for seg in sp.commands:
        prog = _program(seg)
        if prog is None:
            continue
        args = seg.words[prog[0] + 1:]
        if prog[1] == "dd":
            out += [(a[3:], a[3:]) for a in args
                    if a.startswith("of=") and a[3:] and not _EXPANSION.search(a)]
            continue
        in_place = prog[1] in _IN_PLACE_EDITORS
        if not (prog[1] in _WRITING_COMMANDS or (in_place and _in_place(args))):
            continue
        files: list[str] = []
        for a in args:
            if a.startswith("-"):
                a = _option_value(a)
            elif sp.dialect == "cmd" and a.startswith("/"):
                continue  # a switch: copy /Y, robocopy /MIR
            if a and not _EXPANSION.search(a):
                files.append(a)
        out += [(f, f) for f in files]
        if in_place:
            continue  # sed -i edits the files it names, never into a folder
        names = [n for n in dict.fromkeys(map(_leaf, files)) if n]
        if len(files) > _MAX_NAMED_ARGS:
            names = []
        for f in files:
            d = f.replace("\\", "/").rstrip("/")
            if not d:
                continue
            for n in names:
                if n != _leaf(f):
                    out += [(f"{d}/{n}", f"{d}/{n}"), (f"{d}/{n}/{_DIR_PROBE}", f"{d}/{n}/*")]
            out.append((f"{d}/{_DIR_PROBE}", f"{d}/*"))
    return out


def _dir_changes(sp: shellsplit.Split, start: Path) -> list[Path] | None:
    """The folders a relative path in `sp` may resolve against: `start` and
    each folder a ``cd`` / ``pushd`` / ``Set-Location`` in the line moves to
    (in order, each from the one before; ``popd`` and ``cd -`` go back to one
    already listed, a bare ``cd`` to the home folder). None when a move can't
    be followed: an expansion or a wildcard in its target, or a drive switch
    (``D:``, which lands in that drive's own current folder)."""
    bases = [start]
    cur = start
    for seg in sp.commands:
        prog = _program(seg)
        if prog is None:
            continue
        ci, name = prog
        if re.fullmatch(r"[A-Za-z]:", seg.words[ci]):
            return None
        if name in _POP_COMMANDS or name not in _CHDIR_COMMANDS:
            continue
        rest = seg.words[ci + 1:]
        if "-" in rest:
            continue
        args = [
            a for a in rest
            if a and not a.startswith("-") and not (sp.dialect == "cmd" and a.lower() == "/d")
        ]
        if not args:
            home = Path(os.path.expanduser("~"))
            bases.append(home)
            if sp.dialect != "cmd":  # cmd's bare `cd` only prints the folder
                cur = home
            continue
        target = args[0]
        local = _local_spelling(target)
        if local is None or _EXPANSION.search(target) or any(c in target for c in "*?["):
            return None
        try:
            cur = cur / Path(local).expanduser()  # an absolute target replaces cur
        except (OSError, RuntimeError, ValueError):
            return None  # `cd ~nosuchuser`: pathlib can't expand it, so can't follow
        bases.append(cur)
    return bases


def _is_absolute(value: str) -> bool:
    """True for a path that doesn't depend on the current folder (a rooted
    ``/x`` too, and an opaque device path, which is refused as written). A
    ``~name`` that can't be expanded counts as relative: its folder is
    unknown."""
    local = _local_spelling(value)
    if local is None:
        return True
    if local.startswith(("/", "\\")):
        return True
    try:
        return Path(local).expanduser().is_absolute()
    except (OSError, RuntimeError, ValueError):
        return False


def protected_write(
    tool_name: str,
    tool_category: str,
    tool_args: str | dict,
    protected_paths: Iterable[str],
    *,
    workdir: str | Path | None = None,
    path_args: list[str] | None = None,
    read_only: bool | None = None,
    dialect: str | None = None,
    split: shellsplit.Split | None = None,
) -> str | None:
    """The first path this call would write that matches a protected pattern
    (as written), or None.

    The paths are the call's path arguments, unless the tool only reads
    (`read_only`, default: a built-in in `evi.tools.base.READ_ONLY_TOOLS`) or
    the call does (`classify_call` is "code_read"), and for a shell line its
    output redirect targets and the files its writing commands name (``tee``,
    ``cp``, ``Set-Content`` ...;
    `_written_by_commands`). `path_args` defaults to the arguments named like
    paths (`evi.tools.base.path_values`). Relative paths resolve against
    `workdir`, a shell line's against its cwd and every folder a ``cd`` in it
    moves to; after a ``cd`` that can't be followed a relative one counts as
    protected. Each is matched as written, normalised and on its real path.
    Any category counts, not only fs/code. A path whose target can't be told
    (an opaque device path) counts as protected.
    """
    base = _base_dir(workdir)
    targets: list[tuple[str, Path, str]] = []
    if read_only is None:
        read_only = tool_name in READ_ONLY_TOOLS
    # A call that only reads code (monitor tailing a file) writes none of its
    # path arguments either.
    if not read_only and classify_call(tool_name, tool_category, tool_args) != CODE_READ:
        if path_args is None:
            path_args = path_values(_arg_dict(tool_args) or {}, tool_category)
        targets += [(v, base, v) for v in path_args if v]
    command = shell_command_of(tool_name, tool_category, tool_args)
    if command is not None:
        sp = split if split is not None else shellsplit.split(command, dialect)
        cwd = exec_cwd_of(tool_name, tool_args) or ""
        try:
            rbase = base / Path(cwd).expanduser()  # an absolute cwd replaces base
        except (OSError, RuntimeError, ValueError):
            rbase = base
        writes = [
            (r.text, r.text) for r in sp.redirects
            if ">" in r.op and r.text and not r.dynamic and r.text.lower() not in _NULL_SINKS
        ] + _written_by_commands(sp)
        bases = _dir_changes(sp, rbase) if writes and sp.changes_dir else [rbase]
        for w, shown in writes:
            if bases is None and not _is_absolute(w):
                if any(str(p).strip() for p in protected_paths or ()):
                    # It lands after a `cd` we can't follow, so it may be a
                    # protected file; say so, as the path isn't known to be one.
                    return f"{shown} (after a cd eVi can't follow, so where it lands is unknown)"
                continue
            targets += [(w, b, shown) for b in (bases or [rbase])]
    return _first_protected(targets, protected_paths)


def protected_read(
    tool_category: str,
    tool_args: str | dict,
    protected_paths: Iterable[str],
    *,
    workdir: str | Path | None = None,
    path_args: list[str] | None = None,
) -> str | None:
    """The first path argument of an fs/code/code_read call that matches a
    protected pattern other than the write-only ones (`WRITE_PROTECTED_PATHS`
    and eVi's home), or None: a secret or start-up file (``.env``, ``id_rsa``
    ...) the call could pull into the model's context. `path_args` defaults to
    the arguments named like paths."""
    if tool_category not in _FILE_CATEGORIES:
        return None
    if path_args is None:
        path_args = path_values(_arg_dict(tool_args) or {}, tool_category)
    skip = {*WRITE_PROTECTED_PATHS, _evi_home_pattern()}
    pats = [p for p in (protected_paths or ()) if str(p).strip() not in skip]
    base = _base_dir(workdir)
    return _first_protected([(v, base, v) for v in path_args if v], pats)


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
    path_args: list[str] | None = None,
    read_only: bool | None = None,
    call_category: str | None = None,
    remote_trigger: bool = False,
) -> str:
    """Return 'allow', 'deny', or 'ask' for one tool call.

    `workdir` is the session working folder: relative path arguments resolve
    against it, and shell redirects must stay inside it for an allow rule to
    apply (default: `evi.workdir.get_cwd()`). `dialect` is the shell the
    command line runs in (default: this platform's). `path_args` are the
    call's path arguments (`evi.tools.base.Tool.path_args`; default: the
    arguments named like paths) and `read_only` whether the tool only reads
    them (default: `evi.tools.base.READ_ONLY_TOOLS`). `protected_paths` is
    used as given: `effective_protected_paths` adds the defaults.

    `tool_category` is the tool's own (toggle) category and `call_category`
    this call's (`evi.tools.base.Tool.category_for`; default:
    `classify_call`), which auto_approve, accept_edits and trusted dirs go by;
    a call that runs a shell line is "shell" whatever is passed. An allow
    rule matches only `allow_rule_keys`. `remote_trigger` is True when a
    remote peer drives the run (federation, A2A, a routine, a channel push):
    a call that runs code (`runs_code`) is then never approved by a category,
    accept_edits or a trusted dir, only by an explicit allow rule (or yolo,
    which such a run never gets). A local unattended run passes False: the
    user's own list decides there, "shell" or "code" included.
    """
    values = _arg_values(tool_args)
    data = _arg_dict(tool_args) or {}
    command = shell_command_of(tool_name, tool_category, tool_args)
    cat = _call_category(tool_name, tool_category, tool_args, call_category)
    exec_held = remote_trigger and runs_code(tool_name, tool_category, cat)
    keys = rule_keys(tool_name, tool_category, command, cat)
    allow_keys = allow_rule_keys(tool_name, tool_category, command, cat)
    sp = shellsplit.split(command, dialect) if command is not None else None
    if path_args is None:
        path_args = path_values(data, tool_category)

    # 0. Hard-deny: unconditional, evaluated before EVERYTHING (even yolo) so an
    #    allow rule or yolo can't override it.
    for rule in hard_deny or ():
        # hard_deny entries are deny-only — accept "<tool-glob> [arg-glob]" with
        # or without a leading "deny".
        norm = rule if rule.split(None, 1)[:1] == ["deny"] else f"deny {rule}"
        if any(_rule_action(norm, k, values) == "deny" for k in keys):
            return "deny"
        # ...and against each segment of a shell line, so `ls && rm -rf x`
        # can't hide `rm -rf x` from a `shell rm -rf*` entry.
        if sp is not None and any(
            _rule_action(norm, k, [seg.text]) == "deny" for seg in sp.commands for k in keys
        ):
            return "deny"

    def rule_action() -> str | None:
        if command is not None:
            return shell_rule_action(
                rules, keys, values, command,
                workdir=workdir, cwd=exec_cwd_of(tool_name, tool_args), split=sp,
                allow_keys=allow_keys,
            )
        for rule in rules or ():
            # first match wins (deny or allow); explicit deny beats trust
            action = next(
                (a for k in keys
                 if (a := _rule_action(rule, k, values)) and (a == "deny" or k in allow_keys)),
                None,
            )
            if action:
                return action
        return None

    # 1. Protected paths: a write to an instruction / config / hook / CI /
    #    secret file is never silent, in any mode (yolo included) and whatever
    #    allow rule matches; plan mode or a deny rule still denies it.
    if protected_write(
        tool_name, tool_category, tool_args, protected_paths, workdir=workdir,
        path_args=path_args, read_only=read_only, split=sp,
    ) is not None:
        return "deny" if mode == "plan" or rule_action() == "deny" else "ask"

    if mode == "yolo":
        return "allow"
    if mode == "plan":
        return "deny"

    action = rule_action()
    if action:
        return action

    # Reading a secret file (.env, a key) is never silent either, unless an
    # explicit allow rule above said so.
    if protected_read(
        cat if cat in _FILE_CATEGORIES else tool_category, tool_args, protected_paths,
        workdir=workdir, path_args=path_args,
    ) is not None:
        return "ask"
    # Driven by a remote trigger, code execution goes no further: it asks,
    # and the run's "deny" policy refuses it.
    if exec_held:
        return "ask"
    if mode == "accept_edits" and cat in _EDIT_CATEGORIES:
        return "allow"
    if cat in approved_categories(auto_approve):
        return "allow"
    # Trusted scopes: files under a trusted dir (with no glob argument that
    # climbs out of it), or web fetches to a trusted host.
    if (
        cat in _EDIT_CATEGORIES
        and not any(_glob_escapes(g) for g in glob_values(tool_name, data))
        and _under_trusted_dir(path_args, trusted_dirs, workdir)
    ):
        return "allow"
    if tool_category == "web" and _host_trusted(values, trusted_domains):
        return "allow"
    return "ask"
