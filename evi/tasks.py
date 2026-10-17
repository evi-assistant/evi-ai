"""The task queue: tracked tasks with acceptance criteria and a verify state.

The director files a task with acceptance criteria and the checks that prove
them; eVi claims it and implements it in a git worktree of its own under the
implementer policy (`run_task`, ``evi task start``), runs the checks itself
and stops with a run report; only the director's attestation (`evi.attest`)
marks the task verified or rejected (docs/roadmap.md, Phase 1,
loop-task-queue).

Storage
    ``<repo>/.evi/tasks/<id>.toml``, one task per file, tracked in git (the
    repository's ``.gitignore`` un-ignores ``.evi/tasks/``; the rest of
    ``.evi/`` stays ignored). ``<repo>`` is the main checkout (`repo_root`:
    the repository's main worktree, also from inside a linked one), so
    ``evi task`` run in a task's worktree finds the same queue. A write goes
    to a temporary file under ``.evi/`` and then replaces the task's file
    (tried again for a moment while Windows refuses because a reader has it
    open; a write that can't be made is a TaskError, the old file intact),
    and every read-modify-write (`TaskQueue.update`) holds an exclusive lock
    on ``.evi/tasks.lock``, so of two concurrent claims one wins and the
    other sees the claim. A task id is ``T<n>`` (the next free number) unless
    one is given: ``[A-Za-z0-9_-]{1,64}``, not a Windows device name, unique
    whatever its case. It names the file and the task's branch
    (``task/<id>``).

Fields (`FIELDS`; every value survives the TOML round trip, none is null)
    id, title, status, why, hints (the director's guidance on how to go
    about it, "" for none; `build_prompt` shows it after why),
    acceptance[] (the criteria), files_in_scope[]
    (globs relative to the repository, `evi.policy.validate_scope`; a
    start may write only these, `evi.policy.in_scope`),
    required_checks[] (command lines split the POSIX way, or argv arrays),
    implementer (a hint: evi-local, evi-frontier, director), created,
    attempts, backend, model, worktree, branch, base (the commit the branch
    started from: what a verify reviews against), evidence (the evidence
    bundle's path, "" until there is one, `evi.evidence`), evidence_summary
    (the bundle's hash and numbers, `evi.evidence.summarize`; the bundle
    itself stays under eVi's home), reviewer_notes[] ({at, verdict,
    note}), lease ({holder, expires_at}, empty when unclaimed), last_result
    ({stop_reason, exit_code, report, summary, out_of_scope,
    out_of_scope_more} of the latest start; the summary is the answer's
    first line, `printable`; out_of_scope the files the branch changed
    outside files_in_scope, `out_of_scope`, left out when git couldn't
    tell, and out_of_scope_more how many more there were, left out for
    none) and, on a task the
    director signed, attestation. Unknown fields are kept. A task file may
    come from anyone (a pulled branch), so its paths are never taken as
    they are: the evidence is read only when it is a bundle eVi wrote
    (`evidence_path`), the branch only as a branch (`head_ref`, never a tag
    of its name), and what it says reaches a terminal only `printable`.
    `evi.attest` signs every field but ``attestation``, so nothing writes a
    verified or rejected task but a transition out of it, which drops the
    attestation, and nothing but `evi.attest.sign` sets a verdict
    (`TaskQueue` refuses anything else).

States (pure functions over a record; `IllegalTransition` for anything else)
    open -> claimed (`claim`: a lease of ``LEASE_MINUTES``) -> implemented
    (`finish`: the start's run ended with stop_reason "done" AND the task's
    required checks passed when eVi ran them itself in the worktree) ->
    needs_review (`submit`) -> verified | rejected (the director's
    ``evi task verify|reject``: `for_verdict`, then `evi.attest.sign`).
    `release`: claimed -> open. A start that fails (any other stop_reason, or
    a required check failing) puts the task back to open. Every start counts
    an attempt and records its result. `reopen`: implemented, needs_review,
    verified or rejected -> open, without the attestation. A live lease held
    by someone else refuses a claim or a release (`LeaseHeld`) unless forced;
    an expired one, or one whose expiry can't be read, can be reclaimed.

Running a task (`run_task`, behind ``evi task start``)
    Only in a trusted repository (`evi.trust`): the implementer policy runs no
    command in an untrusted one; and only a task with files_in_scope (a run
    confined to none could change nothing). It claims the task, opens its worktree
    (`evi.worktree.open_isolated`, branch ``task/<id>``, locked while the run
    goes on; a later attempt reuses it, taking over a lock a start on this
    machine left when it died), builds the agent through the
    caller's factory, holds it to the implementer policy
    (`evi.policy.implementer_policy` confined to the task's files_in_scope:
    a write to any other file is denied, with a reason that tells the model
    to list it as a concern instead; and its required checks allowed as
    exact commands) with the verify gate on (the required checks join
    ``[checks] commands`` for the run), and runs the prompt `build_prompt`
    makes from the task (`evi.headless.run_headless`). Then it runs the
    required checks itself in the worktree (`evi.checks`) as ground truth,
    lists what the branch changed outside files_in_scope anyway (a test's
    output, a ruff ``fix`` setting: `out_of_scope`, but for untracked files
    in a cache folder such as ``__pycache__``; at most
    ``_OUT_OF_SCOPE_LISTED`` paths and the count of the rest,
    ``out_of_scope_more``, recorded in the report's task section and
    last_result; ``evi task verify`` refuses to sign over it without the
    director's reason), writes the run report with a ``task`` section to
    ``<eVi home>/tasks/<repo key>/<id>/<attempt>/report.json``
    (`report_path`), and records the worktree, branch, attempt, backend,
    model and result in the task. A delegate CLI-agent backend is refused,
    as the [llm] backend or a [models] one, whatever
    allow_cli_agent_unattended says: it runs its own tools, outside the
    policy. Nothing is merged or committed, and the checkout you work in is
    never touched.

    `run_task` returns the run (`TaskRun`: the task as recorded, the
    `evi.headless.HeadlessResult`, the report's path, the checks it ran), for
    any caller: an evidence collector (the ``evidence`` hook,
    `evi.evidence.collect_task` behind ``evi task start``) records its
    bundle's path and summary in the task, whatever the run's stop_reason.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

TASKS_REL = Path(".evi") / "tasks"
LOCK_NAME = "tasks.lock"
STATUSES = ("open", "claimed", "implemented", "needs_review", "verified", "rejected")
VERDICTS = ("verified", "rejected")
BRANCH_PREFIX = "task/"
# How long a claim holds before someone else may take the task over.
LEASE_MINUTES = 240
# Every field a task has, in the order a task file lists them.
FIELDS = (
    "id", "title", "status", "why", "hints", "acceptance", "files_in_scope",
    "required_checks", "implementer", "created", "attempts", "backend", "model", "worktree",
    "branch", "base", "evidence", "evidence_summary", "reviewer_notes", "lease",
    "last_result", "attestation",
)
# What a new task starts with besides id, title and created.
_DEFAULTS: dict[str, Any] = {
    "status": "open", "why": "", "hints": "", "acceptance": [], "files_in_scope": [],
    "required_checks": [], "implementer": "", "attempts": 0, "backend": "", "model": "",
    "worktree": "", "branch": "", "base": "", "evidence": "", "reviewer_notes": [],
    "lease": {},
}
# A task file `evi task new --from` copies: what the director files. The rest
# is run bookkeeping, which a new task starts without.
SPEC_FIELDS = ("id", "title", "why", "hints", "acceptance", "files_in_scope",
               "required_checks", "implementer")
_BOOKKEEPING = frozenset(FIELDS) - set(SPEC_FIELDS)
ATTESTATION = "attestation"
# What `evi task start` calls itself as an unattended entry point.
ENTRY = "evi task start"

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_AUTO_ID = re.compile(r"T([0-9]{1,9})\Z", re.I)
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
)
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_LOCK_TIMEOUT = 15.0
_SUMMARY_CHARS = 300
# The most out_of_scope paths a start records (the task file is tracked): the
# rest are counted in last_result's out_of_scope_more.
_OUT_OF_SCOPE_LISTED = 200
# The most out-of-scope paths a message or a reviewer note names (`paths_text`).
_OUT_OF_SCOPE_SHOWN = 10
# C0 controls but tab and newline, DEL and C1 controls: what `printable` drops.
_UNPRINTABLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Waits between tries while Windows refuses to replace (or open) a task file
# that another process is reading: Python opens files there without
# FILE_SHARE_DELETE, so `os.replace` onto one fails until the reader is done.
_SHARING_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
# One read-modify-write of a queue at a time in this process (the file lock
# keeps out other processes).
_THREAD_LOCK = threading.Lock()


class TaskError(RuntimeError):
    """A task request that can't go ahead (the CLI exits 2)."""


class TaskNotFound(TaskError):
    """No task of that id in the queue."""


class IllegalTransition(TaskError):
    """The task's status doesn't allow that step."""


class LeaseHeld(TaskError):
    """Someone else holds a live lease on the task."""


# ---- small helpers ---------------------------------------------------------------


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def format_time(when: _dt.datetime) -> str:
    """`when` as a task stores it: UTC, to the second, ``...Z``."""
    return when.astimezone(_dt.timezone.utc).strftime(_TIME_FORMAT)


def parse_time(text: Any) -> _dt.datetime | None:
    """A time `format_time` wrote, or None when it can't be read."""
    try:
        return _dt.datetime.strptime(str(text), _TIME_FORMAT).replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def validate_id(task_id: Any) -> str:
    """`task_id` if it may name a task (see the module doc), else TaskError."""
    text = str(task_id if task_id is not None else "")
    if not _ID.match(text):
        raise TaskError(f"not a task id: {text[:80]!r} (1 to 64 letters, digits, '_' or '-')")
    if text.lower() in _DEVICE_NAMES:
        raise TaskError(f"not a task id: {text!r} is a reserved file name on Windows")
    return text


def branch_for(task_id: str) -> str:
    """The branch a task's work goes on."""
    return BRANCH_PREFIX + task_id


def head_ref(branch: str) -> str:
    """The full ref of a task's branch, for reading its head: git resolves a
    bare ``task/T1`` to the tag ``refs/tags/task/T1`` before the branch, so a
    tag of the same name would answer for a branch that moved on."""
    return "refs/heads/" + str(branch).strip()


def printable(value: Any) -> str:
    """`value` as text with the control characters dropped (tab and newline
    stay): a task file is tracked and a summary is the model's, and neither
    may move the cursor or repaint the terminal that shows them."""
    return _UNPRINTABLE.sub("", str(value))


def _storable(text: str) -> str:
    """`text` as it can be written as UTF-8: a lone surrogate (a model's
    broken emoji, say) becomes its ``\\uXXXX`` escape."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def default_holder() -> str:
    """Who claims, when nobody says: ``<user>@<host>``."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — no user name to be had
        user = "someone"
    try:
        host = socket.gethostname()
    except OSError:
        host = "localhost"
    return f"{user}@{host}"


def status_of(record: Mapping[str, Any]) -> str:
    raw = record.get("status")
    return str(raw if raw is not None else "").strip()


def check_argv(entry: Any) -> tuple[str, ...]:
    """A required check as argv: a list as it is, a string split the POSIX
    way (use ``/`` in paths, or an array). TaskError when it gives none."""
    if isinstance(entry, (list, tuple)):
        words = tuple(str(w) for w in entry if str(w))
    elif isinstance(entry, str):
        try:
            words = tuple(shlex.split(entry, posix=True))
        except ValueError as exc:
            raise TaskError(f"required check {entry!r} doesn't parse: {exc}") from exc
    else:
        raise TaskError(f"a required check is a command line or an argv array, not {entry!r}")
    if not words:
        raise TaskError(f"required check {entry!r} is empty")
    return words


def required_argvs(record: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """The task's required checks as argv lists (TaskError for a bad one)."""
    checks = record.get("required_checks") or []
    if not isinstance(checks, list):
        raise TaskError("required_checks must be a list")
    return [check_argv(c) for c in checks]


def scope_of(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The task's files_in_scope, validated (`evi.policy.validate_scope`)."""
    from evi import policy

    scope = record.get("files_in_scope") or []
    if not isinstance(scope, list):
        raise TaskError("files_in_scope must be a list")
    try:
        return policy.validate_scope(str(s) for s in scope)
    except policy.PolicyError as exc:
        raise TaskError(f"files_in_scope: {exc}") from exc


def _text_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TaskError(f"{what} must be a list of strings")
    return [str(v) for v in value]


def hints_text(value: Any) -> str:
    """A task's hints as stored: a string as it is, or a list of strings
    (``evi task new --hint`` is repeatable, a ``--from`` file may list them)
    one per line. TaskError for anything else."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return "\n".join(v.strip() for v in value if v.strip())
    raise TaskError("hints must be a string or a list of strings")


def _ordered(record: Mapping[str, Any]) -> dict[str, Any]:
    """`record` with the known fields first, in `FIELDS` order."""
    out = {k: record[k] for k in FIELDS if k in record}
    out.update({k: v for k, v in record.items() if k not in out})
    return out


def _without_attestation(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != ATTESTATION}


def task_home(root: str | os.PathLike[str], task_id: str) -> Path:
    """The task's folder under eVi's home, ``<eVi home>/tasks/<repo key>/<id>``
    (its run reports, `report_path`), the key a hash of the main checkout's
    path."""
    from evi import config

    key = hashlib.sha256(
        os.path.normcase(os.path.realpath(root)).encode("utf-8", "surrogateescape")
    ).hexdigest()[:16]
    return Path(config.HOME) / "tasks" / key / validate_id(task_id)


def _inside(path: str, base: str | os.PathLike[str]) -> bool:
    """`path` (absolute and normalised) is `base` or below it, and nothing
    below `base` on the way is a link, a junction or a Windows device name.
    Decided on the names alone, but for an lstat of each step (never one
    that follows a link)."""
    top = os.path.abspath(base)
    try:
        if os.path.commonpath([os.path.normcase(path), os.path.normcase(top)]) != (
                os.path.normcase(top)):
            return False
        rel = os.path.relpath(path, top)
    except ValueError:  # another drive
        return False
    isjunction = getattr(os.path, "isjunction", lambda _p: False)
    here = Path(top)
    for part in Path(rel).parts if rel != os.curdir else ():
        if part == os.pardir or part.split(".")[0].rstrip(" ").lower() in _DEVICE_NAMES:
            return False
        here = here / part
        if os.path.islink(here) or isjunction(here):
            return False
    return True


def evidence_path(record: Mapping[str, Any], root: str | os.PathLike[str]) -> str:
    """The task's evidence bundle as an absolute path ("" for none): a
    relative one is read against the repository (`evi.attest`'s contract).
    The path comes from a tracked file, so it is never taken as it is:
    TaskError unless it is a bundle eVi wrote (`evi.evidence.acceptable`: a
    folder holding report.json under the repository's folder of
    ``<eVi home>/evidence/``, through no link, or exactly a folder ``evi run
    --evidence`` / ``evi evidence collect --out`` recorded there), and isn't
    a UNC or device path (only stat'ing ``\\\\host\\share`` makes Windows log
    on to that host). Anything else (".", the repository, its .git, any
    folder elsewhere) is never hashed: callers read it as no evidence that
    matches (a task list, a show) or refuse (a verify). Nothing is expanded
    (``~`` is a folder of the repository)."""
    from evi import evidence

    raw = str(record.get("evidence") or "").strip()
    if not raw:
        return ""
    task_id = str(record.get("id") or "")
    unc = raw[:1] in ("\\", "/") and raw[1:2] in ("\\", "/")  # also \\?\ and \\.\ paths
    if "\0" not in raw and not unc:
        p = Path(raw)
        full = os.path.abspath(p if p.is_absolute() else Path(root) / p)
        if evidence.acceptable(full, root):
            return full
    raise TaskError(f"task {task_id}: its evidence path {printable(raw)[:200]!r} is not an "
                    "evidence bundle eVi wrote for this repository (under "
                    f"{evidence.repo_folder(root)}, or a folder `evi run --evidence` or `evi "
                    "evidence collect --out` recorded), so it is not read")


# ---- records -----------------------------------------------------------------------


def new_task(
    task_id: str,
    *,
    title: str,
    why: str = "",
    acceptance: Sequence[str] = (),
    files_in_scope: Sequence[str] = (),
    required_checks: Sequence[Any] = (),
    implementer: str = "",
    hints: str | Sequence[str] = "",
    extra: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """A new open task (an empty `task_id`: `TaskQueue.create` gives it the
    next T<n>). `hints`: how to go about it (a string, or strings joined one
    per line, `hints_text`). `extra`: more fields to keep (run bookkeeping
    and an attestation in it are dropped). TaskError for a bad id, an empty
    title, a scope entry outside the repository, a check that doesn't parse
    or hints that are no text."""
    title = str(title or "").strip()
    if not title:
        raise TaskError("a task needs a title")
    if required_checks is not None and (isinstance(required_checks, (str, bytes))
                                        or not isinstance(required_checks, (list, tuple))):
        # A string would be one check per character.
        raise TaskError("required_checks must be a list of command lines or argv arrays")
    record: dict[str, Any] = {
        k: v for k, v in (extra or {}).items() if k not in _BOOKKEEPING and k not in SPEC_FIELDS
    }
    record.update({
        "id": validate_id(task_id) if task_id else "", "title": title, **_DEFAULTS,
        "why": str(why or "").strip(),
        "hints": hints_text(hints),
        "acceptance": _text_list(acceptance, "acceptance"),
        "files_in_scope": _text_list(files_in_scope, "files_in_scope"),
        "required_checks": [list(c) if isinstance(c, (list, tuple)) else c
                            for c in (required_checks or [])],
        "implementer": str(implementer or "").strip(),
        "created": format_time(now or now_utc()),
    })
    scope_of(record)
    required_argvs(record)
    return _ordered(record)


def spec_from_toml(text: str) -> dict[str, Any]:
    """The task a ``--from`` file describes: its fields as parsed (run
    bookkeeping is dropped by `new_task`). TaskError when it isn't TOML."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TaskError(f"not a TOML file: {exc}") from exc
    return data


def lease_of(record: Mapping[str, Any]) -> dict[str, Any]:
    lease = record.get("lease")
    return dict(lease) if isinstance(lease, Mapping) else {}


def lease_live(record: Mapping[str, Any], now: _dt.datetime | None = None) -> bool:
    """A claim someone holds and that hasn't run out (an expiry that can't
    be read has run out)."""
    lease = lease_of(record)
    expires = parse_time(lease.get("expires_at"))
    return bool(str(lease.get("holder") or "").strip()) and expires is not None and (
        expires > (now or now_utc()))


def _who(record: Mapping[str, Any]) -> str:
    lease = lease_of(record)
    return printable(f"{lease.get('holder') or 'someone'} (until "
                     f"{lease.get('expires_at') or '?'})")


def claim(
    record: Mapping[str, Any],
    *,
    holder: str,
    now: _dt.datetime | None = None,
    ttl_minutes: int = LEASE_MINUTES,
    force: bool = False,
) -> dict[str, Any]:
    """open -> claimed, with a lease for `holder` of `ttl_minutes`. A claimed
    task whose lease ran out (or is `holder`'s own) is claimed again; one
    another holder's live lease covers raises LeaseHeld unless `force`."""
    now = now or now_utc()
    holder = str(holder or "").strip()
    if not holder:
        raise TaskError("a claim needs a holder")
    status = status_of(record)
    task_id = record.get("id")
    if status == "claimed":
        held_by = str(lease_of(record).get("holder") or "")
        if lease_live(record, now) and held_by != holder and not force:
            raise LeaseHeld(f"task {task_id} is claimed by {_who(record)}; wait for the lease "
                            "to run out, or pass --force to take it over")
    elif status != "open":
        raise IllegalTransition(f"task {task_id} is {status or '(no status)'!r}: only an open "
                                "task (or a claimed one whose lease ran out) can be claimed")
    out = _without_attestation(record)
    out["status"] = "claimed"
    out["lease"] = {"holder": holder,
                    "expires_at": format_time(now + _dt.timedelta(minutes=max(1, int(ttl_minutes))))}
    return _ordered(out)


def release(
    record: Mapping[str, Any], *, holder: str, now: _dt.datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """claimed -> open. Another holder's live lease raises LeaseHeld unless
    `force`."""
    status = status_of(record)
    task_id = record.get("id")
    if status != "claimed":
        raise IllegalTransition(f"task {task_id} is {status or '(no status)'!r}: only a claimed "
                                "task can be released")
    held_by = str(lease_of(record).get("holder") or "")
    if lease_live(record, now) and held_by != holder and not force:
        raise LeaseHeld(f"task {task_id} is claimed by {_who(record)}, not {holder}; pass "
                        "--force to release it anyway")
    out = _without_attestation(record)
    out["status"] = "open"
    out["lease"] = {}
    return _ordered(out)


def _holding(record: Mapping[str, Any], holder: str) -> None:
    """Raise unless `record` is claimed by `holder` (whatever the expiry: a
    lease that ran out still counts until someone else takes it)."""
    task_id = record.get("id")
    if status_of(record) != "claimed":
        raise IllegalTransition(f"task {task_id} is no longer claimed (it is "
                                f"{status_of(record) or '(no status)'!r})")
    if str(lease_of(record).get("holder") or "") != holder:
        raise LeaseHeld(f"task {task_id} is now claimed by {_who(record)}")


def annotate(record: Mapping[str, Any], *, holder: str, **fields: Any) -> dict[str, Any]:
    """Set `fields` (worktree, branch, base ...) on a task `holder` claims."""
    _holding(record, holder)
    out = _without_attestation(record)
    out.update({k: v for k, v in fields.items() if v is not None})
    return _ordered(out)


def finish(
    record: Mapping[str, Any],
    *,
    holder: str,
    implemented: bool,
    stop_reason: str,
    exit_code: int,
    report: str = "",
    summary: str = "",
    fields: Mapping[str, Any] | None = None,
    out_of_scope: Sequence[str] | None = None,
    out_of_scope_more: int = 0,
) -> dict[str, Any]:
    """The end of a start `holder` claimed the task for: implemented when
    `implemented` (the run was done and the required checks passed), else
    back to open. Either way the attempt is counted and its result recorded
    (`last_result`, with `out_of_scope` unless it is None: git couldn't
    tell, and `out_of_scope_more`, how many paths it leaves out, unless 0),
    with `fields` (worktree, branch, backend, model ...)."""
    _holding(record, holder)
    out = _without_attestation(record)
    out.update({k: v for k, v in (fields or {}).items() if v is not None})
    try:
        attempts = int(record.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    out["attempts"] = attempts + 1
    out["status"] = "implemented" if implemented else "open"
    out["lease"] = {}
    out["last_result"] = {"stop_reason": str(stop_reason), "exit_code": int(exit_code),
                          "report": str(report or ""), "summary": str(summary or "")}
    if out_of_scope is not None:
        out["last_result"]["out_of_scope"] = [str(p) for p in out_of_scope]
        if out_of_scope_more:
            out["last_result"]["out_of_scope_more"] = int(out_of_scope_more)
    return _ordered(out)


def submit(record: Mapping[str, Any]) -> dict[str, Any]:
    """implemented -> needs_review."""
    status = status_of(record)
    if status != "implemented":
        raise IllegalTransition(f"task {record.get('id')} is {status or '(no status)'!r}: only an "
                                "implemented task can be submitted for review")
    out = _without_attestation(record)
    out["status"] = "needs_review"
    return _ordered(out)


def _note(record: Mapping[str, Any], verdict: str, note: str, now: _dt.datetime | None) -> list:
    notes = [dict(n) if isinstance(n, Mapping) else n
             for n in (record.get("reviewer_notes") or [])]
    if str(note or "").strip():
        notes.append({"at": format_time(now or now_utc()), "verdict": verdict,
                      "note": str(note).strip()})
    return notes


def reopen(
    record: Mapping[str, Any], *, note: str = "", now: _dt.datetime | None = None
) -> dict[str, Any]:
    """implemented, needs_review, verified or rejected -> open, dropping the
    attestation (`evi.attest.drop_attestation`: the old signature no longer
    applies) and the lease; `note` joins the reviewer notes."""
    from evi import attest

    status = status_of(record)
    if status not in ("implemented", "needs_review") and not attest.claims_verdict(status):
        raise IllegalTransition(f"task {record.get('id')} is {status or '(no status)'!r}: only an "
                                "implemented, needs_review, verified or rejected task can be "
                                "reopened (release a claimed one)")
    out = attest.drop_attestation(record)
    out["status"] = "open"
    out["lease"] = {}
    out["reviewer_notes"] = _note(record, "reopened", note, now)
    return _ordered(out)


def for_verdict(
    record: Mapping[str, Any], *, verdict: str, note: str = "", now: _dt.datetime | None = None
) -> dict[str, Any]:
    """The record the director signs `verdict` for (`evi.attest.sign` sets
    the status and adds the attestation): a needs_review task, with `note`
    in its reviewer notes (so it is signed too)."""
    if verdict not in VERDICTS:
        raise TaskError(f"unknown verdict {verdict!r}")
    status = status_of(record)
    if status != "needs_review":
        raise IllegalTransition(f"task {record.get('id')} is {status or '(no status)'!r}: only a "
                                f"task in needs_review can be {verdict} (`evi task submit` it, "
                                "or `evi task reopen` a decided one first)")
    out = _without_attestation(record)
    out["reviewer_notes"] = _note(record, verdict, note, now)
    return _ordered(out)


def allow_out_of_scope(
    record: Mapping[str, Any], files: Sequence[str], reason: str,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """`record` (the body `for_verdict` made) with the director's `reason`
    for signing although the branch changed `files` outside files_in_scope
    in its reviewer notes (verdict ``out_of_scope_allowed``; the files as
    `paths_text` names them, the task file being tracked), so the
    attestation signs it too. TaskError for an empty reason."""
    reason = str(reason or "").strip()
    if not reason:
        raise TaskError("--allow-out-of-scope needs a reason")
    out = _without_attestation(record)
    out["reviewer_notes"] = _note(
        record, "out_of_scope_allowed",
        f"{reason} (changed outside files_in_scope: {paths_text(files)})", now)
    return _ordered(out)


def paths_text(paths: Sequence[str], more: int = 0) -> str:
    """`paths` as a message or a note names them: the first
    `_OUT_OF_SCOPE_SHOWN`, joined with commas, then how many more there are
    (counting `more` paths not in the list)."""
    shown = ", ".join(map(str, paths[:_OUT_OF_SCOPE_SHOWN]))
    rest = max(len(paths) - _OUT_OF_SCOPE_SHOWN, 0) + max(int(more or 0), 0)
    return shown + (f" and {rest} more" if rest else "")


# ---- the queue -----------------------------------------------------------------------


def repo_root(start: str | os.PathLike[str] | None = None) -> Path:
    """The main checkout of the repository `start` (default: here) is in:
    the folder holding its git common dir, also from inside a linked
    worktree (`evi.worktree.managed_root`). TaskError outside git."""
    from evi import worktree

    try:
        return worktree.managed_root(Path(start) if start is not None else None).resolve()
    except worktree.WorktreeError as exc:
        raise TaskError(f"the task queue lives in a git repository: {exc}") from exc


def dumps(record: Mapping[str, Any]) -> str:
    """`record` as a task file (TOML). TaskError for a value TOML can't hold
    as it is (a None, say): a task is read back exactly as written, or its
    attestation wouldn't check."""
    from evi.config import _to_toml

    ordered = _ordered(record)
    try:
        text = _to_toml(ordered)
        text.encode("utf-8")  # a lone surrogate parses back, but can't go in the file
        back = tomllib.loads(text)
    except (TypeError, ValueError, tomllib.TOMLDecodeError) as exc:  # UnicodeError is one
        raise TaskError(f"task {record.get('id')!r} can't be written as TOML: {exc}") from exc
    if back != ordered:
        raise TaskError(f"task {record.get('id')!r} can't be written as TOML as it is "
                        "(a value would change on the way back)")
    return text if text.endswith("\n") else text + "\n"


def _lock(fd: int, *, release: bool = False) -> bool:
    """Lock the first byte of the open file `fd` without waiting, or let it
    go; False when another open file holds it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _sharing_retry(op: Callable[..., Any], *args: Any) -> Any:
    """`op(*args)`, tried again after a short wait while it raises
    PermissionError: on Windows that is a sharing violation while another
    process (``evi task list``, a status poll) has the task file open, and
    it passes in milliseconds. The last PermissionError is raised."""
    for delay in _SHARING_DELAYS:
        try:
            return op(*args)
        except PermissionError:
            time.sleep(delay)
    return op(*args)


def _sort_key(task_id: str) -> tuple:
    m = _AUTO_ID.match(task_id)
    return (0, int(m.group(1)), task_id) if m else (1, 0, task_id.lower())


class TaskQueue:
    """The tasks of the repository whose main checkout is `root` (see the
    module doc). `at()` finds it from any folder of the repository."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.folder = self.root / TASKS_REL

    @classmethod
    def at(cls, start: str | os.PathLike[str] | None = None) -> "TaskQueue":
        return cls(repo_root(start))

    def path(self, task_id: str) -> Path:
        return self.folder / f"{validate_id(task_id)}.toml"

    # -- reading --

    def ids(self) -> list[str]:
        """The ids of the task files, T<n> first by number."""
        try:
            names = [p.stem for p in self.folder.glob("*.toml") if p.is_file()]
        except OSError:
            return []
        return sorted((n for n in names if _ID.match(n)), key=_sort_key)

    def _find(self, task_id: str) -> Path | None:
        """The file of `task_id` under the name it has on disk: matched
        exactly, else ignoring case (the file system may or may not)."""
        validate_id(task_id)
        names = self.ids()
        if task_id not in names:
            folded = task_id.casefold()
            task_id = next((n for n in names if n.casefold() == folded), "")
        return self.folder / f"{task_id}.toml" if task_id else None

    def read(self, task_id: str) -> dict[str, Any]:
        """The task `task_id` as stored; TaskNotFound when there is none,
        TaskError when its file can't be read as a task."""
        validate_id(task_id)
        path = self._find(task_id)
        if path is None:
            raise TaskNotFound(f"no task {task_id} in {self.folder}")
        try:
            record = tomllib.loads(_sharing_retry(path.read_bytes).decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise TaskError(f"{path} can't be read as a task: {exc}") from exc
        if str(record.get("id") or "") != path.stem:
            raise TaskError(f"{path} names task {record.get('id')!r}, not {path.stem!r}")
        if not isinstance(record.get("status", ""), str):
            raise TaskError(f"{path}: status is not a string")
        return record

    def all(self) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Every task, and (id, problem) for each file that can't be read."""
        found: list[dict[str, Any]] = []
        problems: list[tuple[str, str]] = []
        for task_id in self.ids():
            try:
                found.append(self.read(task_id))
            except TaskError as exc:
                problems.append((task_id, str(exc)))
        return found, problems

    def next_id(self) -> str:
        """``T<n>``: one past the highest number in use."""
        numbers = [int(m.group(1)) for m in (_AUTO_ID.match(i) for i in self.ids()) if m]
        return f"T{max(numbers, default=0) + 1}"

    # -- writing --

    @contextlib.contextmanager
    def locked(self, timeout: float = _LOCK_TIMEOUT) -> Iterator[None]:
        """Hold the queue's lock (``.evi/tasks.lock``): one writer at a time,
        across processes. TaskError after `timeout` seconds, or when the lock
        file can't be opened."""
        folder = self.root / ".evi"
        with _THREAD_LOCK:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                fd = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                raise TaskError(f"can't lock the task queue ({folder / LOCK_NAME}): {exc}") from exc
            locked = False
            try:
                deadline = time.monotonic() + timeout
                while not (locked := _lock(fd)):
                    if time.monotonic() >= deadline:
                        raise TaskError(f"another eVi is changing the task queue "
                                        f"({folder / LOCK_NAME}); try again")
                    time.sleep(0.05)
                yield
            finally:
                if locked:
                    _lock(fd, release=True)
                os.close(fd)

    def _write(self, record: Mapping[str, Any], path: Path | None = None) -> dict[str, Any]:
        """Write `record` to its file atomically (caller holds the lock);
        TaskError when it can't be, the old file left as it was."""
        text = dumps(record)
        path = path or self.path(str(record.get("id")))
        # Under .evi/ (ignored), not .evi/tasks/ (tracked): a crash leaves no
        # stray file for `git status` to list.
        tmp = self.root / ".evi" / f".task-{path.stem}.{secrets.token_hex(4)}.tmp"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            _sharing_retry(os.replace, tmp, path)
        except BaseException as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            if isinstance(exc, OSError):
                raise TaskError(f"can't write {path}: {exc}") from exc
            raise
        return tomllib.loads(text)

    @staticmethod
    def _guard(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> None:
        """Refuse a write no transition makes: a verdict nothing signed, or
        any change to a verified / rejected task but dropping its
        attestation (`evi.attest`'s contract)."""
        from evi import attest

        if attest.claims_verdict(status_of(new)) and ATTESTATION not in new:
            raise TaskError(f"task {new.get('id')}: only the director's attestation "
                            "(`evi task verify|reject`) sets a verdict")
        if old is not None and attest.claims_verdict(status_of(old)) and (
                ATTESTATION in new or attest.claims_verdict(status_of(new))):
            raise TaskError(f"task {new.get('id')} is {status_of(old)!r}: nothing changes it "
                            "but `evi task reopen` (its attestation covers every field)")

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Add a new task (`new_task`); an empty id takes `next_id`. TaskError
        when the id is taken, whatever its case."""
        with self.locked():
            body = dict(record)
            if not str(body.get("id") or "").strip():
                body["id"] = self.next_id()
            task_id = validate_id(body["id"])
            if self._find(task_id) is not None:
                raise TaskError(f"task {task_id} already exists in {self.folder}")
            self._guard(None, body)
            return self._write(_ordered(body), self.path(task_id))

    def update(
        self, task_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        """Read the task, apply `change` (a transition) and write the result,
        all under the lock; returns what was written."""
        with self.locked():
            old = self.read(task_id)
            path = self._find(task_id) or self.path(task_id)
            new = change(dict(old))
            if str(new.get("id") or "") != str(old.get("id") or ""):
                raise TaskError("a transition can't change the task's id")
            self._guard(old, new)
            return self._write(new, path)

    def replace(self, expected: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
        """Write `new` over the task only while it is still `expected` (it
        was read before a long step, a review say); TaskError otherwise."""
        task_id = str(expected.get("id") or "")

        def change(current: dict[str, Any]) -> dict[str, Any]:
            if current != dict(expected):
                raise TaskError(f"task {task_id} changed meanwhile; run the command again")
            return dict(new)

        return self.update(task_id, change)


# ---- running a task ------------------------------------------------------------------


def report_path(root: str | os.PathLike[str], task_id: str, attempt: int) -> Path:
    """Where a start's run report goes: ``<eVi home>/tasks/<repo key>/<id>/
    <attempt>/report.json`` (`task_home`)."""
    return task_home(root, task_id) / str(int(attempt)) / "report.json"


def _git_names(worktree: str | os.PathLike[str], args: Sequence[str]) -> list[str]:
    """The NUL-separated paths `git <args>` prints in `worktree`
    (`evi.gitsafe`); TaskError when git can't answer."""
    import subprocess

    from evi import gitsafe

    try:
        proc = gitsafe.run_git(list(args), cwd=worktree, timeout=60)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TaskError(f"git {args[0]} could not run in {worktree}: {exc}") from exc
    if proc.returncode != 0:
        raise TaskError(f"git {args[0]} failed in {worktree}: "
                        f"{(proc.stderr or proc.stdout or '').strip()[:200]}")
    return [p for p in proc.stdout.split("\0") if p]


def changed_files(worktree: str | os.PathLike[str], base: str, *,
                  caches: bool = True) -> list[str]:
    """The repository-relative files the branch checked out in `worktree`
    changed against `base`, committed or not: ``git diff --name-only
    base...HEAD`` plus ``git status --porcelain`` there (untracked files one
    by one, ignored ones left out; a rename as both its names), sorted.
    Without `caches`, an untracked file in a cache or dependency folder
    (``__pycache__``, ``.pytest_cache``: `evi.checks`' skipped parts, as a
    run's own tests leave them in a repository that doesn't ignore them) is
    left out too. TaskError when git can't tell (or there is no base)."""
    from evi import checks, gitsafe

    base = str(base or "").strip()
    if not base:
        raise TaskError("the task has no base to compare its branch with")
    try:
        revs = gitsafe.rev_args(f"{base}...HEAD", what="base")
    except gitsafe.UnsafeRefError as exc:
        raise TaskError(str(exc)) from exc
    names = set(_git_names(worktree, ["diff", "--name-only", "-z", "--no-renames",
                                      "--no-ext-diff", "--ignore-submodules=none", *revs, "--"]))
    status = _git_names(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                   "--ignore-submodules=none", "--no-renames"])
    i = 0
    while i < len(status):
        entry = status[i]
        if caches or entry[:2] != "??" or not checks._SKIP_PARTS.intersection(
                entry[3:].split("/")):
            names.add(entry[3:])
        if entry[:1] in ("R", "C"):  # a rename's old name follows (should --no-renames slip)
            i += 1
            if i < len(status):
                names.add(status[i])
        i += 1
    return sorted(n for n in names if n)


def out_of_scope(worktree: str | os.PathLike[str], base: str, scope: Sequence[str], *,
                 caches: bool = True) -> list[str]:
    """The files the branch in `worktree` changed against `base` (committed
    or not, `changed_files`, `caches` as it takes them) that no `scope`
    entry covers (`evi.policy.in_scope`): what a run changed outside its
    files_in_scope, whatever wrote it. TaskError when git can't tell."""
    from evi import policy

    return [p for p in changed_files(worktree, base, caches=caches)
            if not policy.in_scope(scope, p)]


def build_prompt(record: Mapping[str, Any], worktree: str | os.PathLike[str]) -> str:
    """What the implementer is asked: the task's title, why, the director's
    hints ("How to approach it"), acceptance criteria, files in scope and
    required checks, and the EVI.md contract."""
    task_id = record.get("id")
    parts = [f"Implement task {task_id} in this repository: {record.get('title') or ''}".strip()]
    why = str(record.get("why") or "").strip()
    if why:
        parts.append(f"Why:\n{why}")
    hints = record.get("hints")
    hints = hints.strip() if isinstance(hints, str) else ""
    if hints:
        parts.append(f"How to approach it:\n{hints}")
    criteria = [str(c) for c in record.get("acceptance") or []]
    if criteria:
        parts.append("Acceptance criteria (each must be met, and shown by a test or a check):\n"
                     + "\n".join(f"{n}. {c}" for n, c in enumerate(criteria, 1)))
    scope = [str(s) for s in record.get("files_in_scope") or []]
    if scope:
        parts.append("Files in scope (change only these: a write to any other file is refused; "
                     "anything else goes in your hand-off as a concern):\n"
                     + "\n".join(f"- {s}" for s in scope))
    else:
        parts.append("Files in scope: none (the task names no files_in_scope, so every write is "
                     "refused): put what you would change in your hand-off as a concern.")
    checks = record.get("required_checks") or []
    if checks:
        from evi.checks import display

        lines = []
        for c in checks:
            try:
                lines.append(f"- {display(check_argv(c))}")
            except TaskError:
                lines.append(f"- {c}")
        parts.append("Required checks (eVi runs them in this folder when you finish; the task "
                     "counts as implemented only if they pass):\n" + "\n".join(lines))
    parts.append(
        "Follow the implementer contract in EVI.md (loaded as project context). Work only in "
        f"this worktree ({worktree}). Never commit, push or change the task's status: the "
        "director reviews your work and verifies it. When you are done, reply with the "
        "hand-off: the files you changed; each acceptance criterion, met or not, with its "
        "evidence; the commands you ran and their results; and any concerns as file:line."
    )
    return "\n\n".join(parts)


@dataclass
class TaskRun:
    """What `run_task` did. `record` is the task as recorded after the run
    (`recorded` False: it changed hands meanwhile, and is as last read)."""

    record: dict[str, Any]
    result: Any  # evi.headless.HeadlessResult
    report_path: Path | None
    exit_code: int
    implemented: bool
    stop_reason: str  # the task's: the run's, or "checks_failed" for a failed required check
    checks: list[Any] = field(default_factory=list)  # evi.checks.CheckRun, the required checks
    worktree: Any = None  # evi.worktree.IsolatedWorktree
    recorded: bool = True
    note: str = ""


def _summary(res: Any, stop_reason: str, runs: Sequence[Any]) -> str:
    """The run's outcome in one line for ``last_result.summary``: printable
    and writable whatever the model answered (`printable`, `_storable`)."""
    def line(text: Any) -> str:
        first = printable(_storable(str(text or ""))).strip().split("\n", 1)[0]
        return first.replace("\t", " ").strip()[:_SUMMARY_CHARS]

    if stop_reason == "checks_failed" and runs:
        bad = next((r for r in runs if not r.ok), None)
        if bad is not None:
            status = f"exit {bad.exit}" if bad.exit is not None else (bad.note or "did not finish")
            return line(f"required check failed: {bad.check.cmd} ({status})")
    if stop_reason == "error" and getattr(res, "error", None):
        return line(res.error) or stop_reason
    return line(getattr(res, "text", "")) or stop_reason


def _backend_of(agent: Any) -> str:
    llm = getattr(getattr(agent, "config", None), "llm", None)
    return str(getattr(llm, "backend", "") or "").strip().lower()


def _configure(agent: Any, wt: Any, argvs: list[tuple[str, ...]],
               scope: tuple[str, ...]) -> None:
    """Hold `agent` to the task: its worktree, nobody to ask, the implementer
    policy with the required checks allowed, and the verify gate on with
    them. TaskError for a backend the policy can't hold."""
    from evi import checks, policy
    from evi.llm.agent import make_unattended
    from evi.llm.cli_agent import DELEGATE_BACKENDS, _configured_backends

    kind = _backend_of(agent)
    if kind in DELEGATE_BACKENDS or getattr(getattr(agent, "client", None), "delegate", False):
        raise TaskError(f"the {kind or 'configured'} backend is a CLI agent that runs its own "
                        "tools, outside the implementer policy a task runs under; use a local "
                        "or API backend (--backend)")
    config = getattr(agent, "config", None)
    # Every [models] *_backend too (a tool builds that client on its own), and
    # whatever allow_cli_agent_unattended says: that setting is for runs
    # without a policy, and none of these clients would keep to this one.
    for setting, other in _configured_backends(config) if config is not None else ():
        if other in DELEGATE_BACKENDS:
            raise TaskError(f"{setting} = {other!r} is a CLI agent that runs its own tools, "
                            "outside the implementer policy a task runs under; clear it, or "
                            "use a local or API backend, for `evi task start`")
    agent.cwd = str(wt.path)
    make_unattended(agent)
    if hasattr(agent, "auto_all"):
        agent.auto_all = False
    cs = getattr(config, "checks", None)
    if cs is not None:
        if str(getattr(cs, "verify", "auto") or "auto").strip().lower() == "off":
            cs.verify = "auto"  # the verify gate stays on for a task (on under a policy)
        known = [checks._argv_of(c) for c in (cs.commands or [])]
        cs.commands = [*(cs.commands or []),
                       *(list(a) for a in argvs if a not in known)]
    trusted = policy.folder_trusted(wt.path)
    allowed = [*argvs, *checks.allowlist(wt.path, config, trusted)]
    agent.policy = policy.implementer_policy(wt.path, files_in_scope=scope, checks=allowed,
                                             trusted=trusted, confine=True)


def _write_report(path: Path, report: dict[str, Any]) -> Path | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A lone surrogate in the model's text goes out as its \uXXXX escape:
        # still JSON, and it reads back as the same string.
        with open(path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return path
    except (OSError, ValueError):
        return None


def run_task(
    queue: TaskQueue,
    task_id: str,
    make_agent: Callable[[Any], Any],
    *,
    holder: str | None = None,
    base: str | None = None,
    max_turns: int = 30,
    ttl_minutes: int = LEASE_MINUTES,
    force: bool = False,
    backend: str = "",
    on_event: Callable[[Any], None] | None = None,
    cancel_on_sigint: bool = False,
    evidence: Callable[[TaskRun], str | Mapping[str, Any] | None] | None = None,
    cancel_token: threading.Event | None = None,
) -> TaskRun:
    """Claim task `task_id`, run it in its worktree under the implementer
    policy and record the outcome (see the module doc). `make_agent(wt)`
    builds the agent for the worktree `wt` (an `evi.worktree.IsolatedWorktree`);
    this function holds it to the task. `evidence(run)`, when given, collects
    the run's evidence and returns its path, recorded as the task's evidence,
    or the fields to record (`evi.evidence.record_fields`: ``evidence`` and
    ``evidence_summary``); when it raises, the run is kept, the note says
    why, and an earlier attempt's evidence is cleared.

    `cancel_token`, once set, cancels what runs after the agent's turn (the
    caller cancels the turn itself, `Agent.cancel`): no further required
    check starts, the running one is stopped (`evi.checks.run_check`), and
    the run ends "interrupted", the task back to open, as a cancelled turn
    does.

    TaskError (nothing ran, and the task is as it was) for an untrusted
    repository, a bad task or one without files_in_scope (the confined run
    could change nothing), someone else's live lease, a worktree that can't
    be opened, or a backend the policy can't hold. Any other failure,
    building the agent included, is the run's (stop_reason "error"): the
    task goes back to open with the attempt counted."""
    from evi import checks, headless, trust
    from evi import worktree as _wt
    from evi.llm.cli_agent import unattended

    holder = holder or default_holder()
    task_id = validate_id(task_id)
    try:
        repo_trusted = trust.status(queue.root).trusted
    except Exception:  # noqa: BLE001 — fail closed
        repo_trusted = False
    if not repo_trusted:
        raise TaskError(f"{queue.root} is not a trusted folder, so a task can't run there (the "
                        "implementer policy runs no tests in it); run `evi trust` there first")
    record = queue.read(task_id)
    argvs = required_argvs(record)
    scope = scope_of(record)
    if not scope:
        # Confined to no file, the run could change nothing (and leave no
        # work to verify): refused before it claims anything.
        raise TaskError(f"task {record['id']} names no files_in_scope, and a start may change "
                        "only the files they cover: give it some (files_in_scope in its task "
                        "file, `evi task new --scope`) first")
    branch = branch_for(str(record["id"]))

    before: dict[str, Any] = {}

    def take(current: dict[str, Any]) -> dict[str, Any]:
        before.clear()
        before.update(current)
        return claim(current, holder=holder, ttl_minutes=ttl_minutes, force=force)

    claimed = queue.update(task_id, take)

    def undo() -> None:
        """Put the task back as it was before this start claimed it."""
        def back(current: dict[str, Any]) -> dict[str, Any]:
            if current != claimed:
                raise TaskError("changed meanwhile")
            return dict(before)

        with contextlib.suppress(TaskError, OSError):
            queue.update(task_id, back)

    from evi import attest

    try:
        attest.branch_head(head_ref(branch), cwd=queue.root)
        branch_existed = True
    except attest.AttestError:
        branch_existed = False
    try:
        # A lock a start on this machine left behind when it died is taken
        # over (the claim above is this one's now); a live run's is not.
        wt = _wt.open_isolated(branch, start=queue.root, base=base or None, take_stale=True)
    except _wt.WorktreeError as exc:
        undo()
        raise TaskError(f"can't open the task's worktree: {exc}") from exc

    res = None
    runs: list[Any] = []
    try:
        start_base = wt.base if not branch_existed else (str(claimed.get("base") or "") or wt.base)
        try:
            claimed = queue.update(task_id, lambda r: annotate(
                r, holder=holder, worktree=str(wt.path), branch=wt.branch, base=start_base))
        except TaskError:
            undo()
            raise
        agent = None
        try:
            agent = make_agent(wt)
            _configure(agent, wt, argvs, scope)
        except TaskError:
            undo()
            raise
        except KeyboardInterrupt:
            res = headless.stopped_result(agent, "interrupted", max_turns=max_turns)
        except Exception as exc:  # noqa: BLE001 — a failed build is the run's error
            res = headless.stopped_result(agent, "error", error=f"{type(exc).__name__}: {exc}",
                                          max_turns=max_turns)
        if res is None:
            prompt = build_prompt(claimed, wt.path)
            with unattended(ENTRY, getattr(agent, "config", None)):
                res = headless.run_headless(
                    agent, prompt, max_turns=max_turns, cancel_on_sigint=cancel_on_sigint,
                    on_event=on_event, track_files=wt.path, report_errors=True,
                )
            with contextlib.suppress(Exception):
                res.trust = trust.status(wt.path).state
        try:
            res.worktree = _wt.worktree_report(wt)
        except Exception:  # noqa: BLE001 — the report says less rather than fail
            res.worktree = None
        run_stop = res.stop_reason or ("error" if res.error else "done")
        if run_stop == "done":
            cs = checks.settings(getattr(agent, "config", None))
            for argv in argvs:
                if cancel_token is not None and cancel_token.is_set():
                    break
                name = Path(argv[0]).name or argv[0]
                runs.append(checks.run_check(checks.Check(name, argv, "test", "task"), wt.path,
                                             timeout=cs.timeout, tail_lines=cs.tail_lines,
                                             cancel_token=cancel_token))
            if cancel_token is not None and cancel_token.is_set():
                run_stop = "interrupted"  # cancelled after its turn: its checks prove nothing
        implemented = run_stop == "done" and all(r.ok for r in runs)
        stop_reason = run_stop if run_stop != "done" else ("done" if implemented else "checks_failed")
        exit_code = headless.exit_code(stop_reason)
        attempt = int(claimed.get("attempts") or 0) + 1
        run = TaskRun(record=claimed, result=res, report_path=None, exit_code=exit_code,
                      implemented=implemented, stop_reason=stop_reason, checks=runs, worktree=wt)
        # The backstop for what the policy can't see: a test's output, a ruff
        # fix setting (not the caches the run's own tests leave untracked).
        # Recorded, never fatal; `evi task verify` checks again, strictly.
        outside: list[str] | None
        more: int | None = None
        try:
            outside = out_of_scope(wt.path, start_base, scope, caches=False)
        except TaskError as exc:
            outside = None
            run.note = printable(f"what the branch changed outside files_in_scope can't be "
                                 f"told: {exc}")
        if outside is not None:
            more = max(len(outside) - _OUT_OF_SCOPE_LISTED, 0)
            outside = outside[:_OUT_OF_SCOPE_LISTED]
        report = {
            **res.to_dict(),
            "task": {
                "id": str(claimed["id"]), "attempt": attempt, "repo": str(queue.root),
                "branch": wt.branch, "base": start_base,
                "outcome": "implemented" if implemented else "open",
                "stop_reason": stop_reason, "exit_code": exit_code,
                "required_checks": [r.record() for r in runs],
                "files_in_scope": list(scope), "out_of_scope": outside,
                "out_of_scope_more": more,
            },
        }
        run.report_path = _write_report(report_path(queue.root, str(claimed["id"]), attempt),
                                        report)
        evidence_at: Any = None
        if evidence is not None:
            try:
                evidence_at = evidence(run)
            except Exception as exc:  # noqa: BLE001 — evidence never loses the run
                run.note = "; ".join(filter(None, [run.note, printable(
                    f"evidence was not collected: {type(exc).__name__}: {exc}")]))
                # An earlier attempt's bundle is not this attempt's evidence.
                evidence_at = {"evidence": "", "evidence_summary": {}}
        fields: dict[str, Any] = {
            "worktree": str(wt.path), "branch": wt.branch, "base": start_base,
            "backend": backend or _backend_of(agent) or str(res.backend or ""),
            "model": str(res.model or ""),
        }
        if isinstance(evidence_at, Mapping):  # {"evidence": path, "evidence_summary": {...}}
            fields.update({k: v for k, v in evidence_at.items()
                           if k in ("evidence", "evidence_summary") and v is not None})
        elif evidence_at:
            fields["evidence"] = str(evidence_at)
        try:
            run.record = queue.update(task_id, lambda r: finish(
                r, holder=holder, implemented=implemented, stop_reason=stop_reason,
                exit_code=exit_code, report=str(run.report_path or ""),
                summary=_summary(res, stop_reason, runs), fields=fields, out_of_scope=outside,
                out_of_scope_more=more or 0))
        except (TaskError, OSError, ValueError) as exc:  # never lose the run to a write
            run.recorded = False
            run.note = (f"the outcome was not recorded: {exc}"
                        + (f"; {run.note}" if run.note else ""))
            with contextlib.suppress(TaskError):
                run.record = queue.read(task_id)
            current = run.record
            if status_of(current) == "claimed" and lease_of(current).get("holder") == holder:
                run.note += (f"; task {task_id} is still claimed by {holder}: "
                             f"`evi task release {task_id}` puts it back to open")
        return run
    finally:
        _wt.release(wt)
