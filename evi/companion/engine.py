"""The Companion engine: check-ins, the judgment layer, the brief, proposals.

A **check-in** (`Companion.check_in`) builds a one-shot agent through
`evi.sdk.build_agent` (hooks and guardrails apply; no project context,
skills or memory index) whose tools are only the configured read-only
sources, under the ``watcher`` policy (`evi.policy.watcher_policy`): every
other call is denied, whatever [auto] says. Its client refuses a delegate
CLI-agent backend (`evi.llm.cli_agent.unattended`, never allowed here: such
a backend runs its own tools). Before the model is asked, the sources that
take no arguments are read directly in code and their output fingerprinted
together with the memory folder's index (names, sizes, times) and the
owner's instructions: when nothing changed since the last check-in, the
model is not called at all. A source that can't be read that way (an MCP
tool that needs arguments) makes every check-in run. The data read goes
into the message as untrusted blocks (`evi.companion.prompts`), and each
source tool's results reach the model the same way.

The model's final answer must be one JSON object (`parse_answer`); an answer
that isn't is discarded and audited, as is any item in it that breaks the
schema. Then the **judgment layer**, plain code (`Companion._judge`): an
item whose dedupe_key was seen in the last `dedupe_days` is dropped; the
rest go to the feed, and only those at or above `notify_min_priority`,
outside `quiet_hours` (an urgent one too, unless `quiet_hours_urgent`) and
under `max_notifications_per_day` are notified (`deliver`). One held back
for quiet hours or the daily limit waits and goes out at the first check-in
or brief that may send it (`_release_held`), most urgent first.

The **brief** (`Companion.brief`) is made in code, no model: today's
calendar (when a calendar source is configured), the open needs_you items
of the last `dedupe_days` and the pending proposals. It goes out once a day
at `brief_at`; a brief missed then (the machine was asleep) goes out at the
next check-in that day, never twice (the day is claimed before it is made).

A **proposal** names a tool from `allowed_actions`, its exact arguments and
a one-line summary. It is refused when made, and again when approved, for a
tool that is not listed, whose name or category says payment, transfer,
trading or deletion (`money_or_delete`, whatever the list says), that runs a
model, inline code or the screen, whose command line the destructive guard
flags or that deletes, makes a network request or overwrites through a
redirect, whose SQL writes, that overwrites or empties a file
(`_content_refusal`), whose call [auto] hard_deny, a deny rule or the
director's ground refuses (`evi.permissions.decide`), or that writes a
protected path. `approve` checks it is pending, not expired, still allowed
and its arguments unchanged (their hash), marks it running, runs the
owner's before_tool_call hooks (a veto refuses it) and then calls exactly
that tool once with the stored arguments, no model in the loop, recording
the redacted result in the feed (after_tool_call hooks see it). Approval is
local only: nothing here reads a reply from Telegram.

Times are local (`now` may be given; tests do). Everything is audited
(`evi.companion.store`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from evi.companion import prompts
from evi.companion.prompts import KINDS, PRIORITIES
from evi.companion.store import CompanionStore

logger = logging.getLogger(__name__)

DEFAULTS = {
    "check_every_hours": 3, "brief_at": "07:30", "quiet_hours": "22:00-07:00",
    "notify_min_priority": "high", "max_notifications_per_day": 6, "dedupe_days": 7,
    "proposal_ttl_minutes": 1440, "max_turns": 8,
}
PROPOSAL_STATES = ("pending", "running", "executed", "failed", "rejected", "expired", "refused")
# Words in a tool's name or category that mark it as moving money or deleting
# something: such a tool is never proposed, listed in allowed_actions or not.
MONEY_OR_DELETE_WORDS = frozenset({
    "pay", "pays", "payment", "payments", "payout", "paypal", "venmo", "zelle", "transfer",
    "transfers", "wire", "remit", "remittance", "money", "bank", "banking", "fund", "funds",
    "trade", "trades", "trading", "buy", "sell", "order", "orders", "purchase", "checkout",
    "invoice", "charge", "refund", "stock", "stocks", "crypto", "bitcoin", "btc", "eth",
    "wallet", "withdraw", "withdrawal", "deposit", "swap", "brokerage", "broker",
    "cash", "payroll", "bid", "bids", "bet", "bets", "wager", "donate", "donation", "tip",
    "tips", "xfer",
    "delete", "del", "remove", "rm", "rmdir", "unlink", "erase", "purge", "destroy", "drop",
    "wipe", "trash", "forget", "truncate", "shred", "uninstall", "cancel", "archive",
    "revoke", "overwrite", "reset",
})
# The same roots inside a run-together name ("banktransfer", "deleteall").
_MONEY_OR_DELETE_ROOTS = ("payment", "transfer", "withdraw", "wallet", "crypto", "trading",
                          "payroll", "donat", "xfer", "wager",
                          "delete", "purge", "erase", "destroy", "wipe", "cancel", "archive",
                          "revoke", "overwrite")
# Never proposed, whatever allowed_actions says: they put a model back in the
# loop (subagents, skills, other agents), run code the arguments don't spell
# out or drive the screen, so the stored call can't say what it will do. A
# shell line (run_command, monitor kind=command, any "shell" tool) is refused
# too (`_content_refusal`): no word list can tell whether one deletes, empties
# a file or posts money (`copy /y nul f`, `powershell -Command Set-Content`).
_NEVER_PROPOSED = frozenset({"run_python", "parallel_research", "invoke_skill"})
_NEVER_PROPOSED_CATEGORIES = frozenset({"subagent", "federation", "skills", "computer"})
_SQL_WRITE_WORDS = frozenset({"insert", "update", "delete", "replace", "drop", "alter",
                              "create", "attach", "detach", "vacuum", "reindex"})
# A source read in advance whose output covers another's.
_COVERED_BY = {"calendar_today": "calendar_week", "calendar_next": "calendar_week",
               "calendar_search": "calendar_week"}
RESULT_MAX = 2000
# Items held back for these reasons wait in state.json's "held" (at most
# HELD_MAX, for at most HELD_MAX_AGE seconds) and go out once they may.
_WAITS = ("quiet hours", "daily limit reached")
HELD_MAX = 50
HELD_MAX_AGE = 2 * 86400
NOTIFY_BODY_MAX = 600
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class ProposalError(ValueError):
    """A proposal that can't be approved or rejected (and why)."""


# ---- settings ---------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """[companion] as the engine uses it: every value checked, a bad one its
    default (`settings`)."""

    enabled: bool = False
    check_every_hours: int = 3
    brief_at: tuple[int, int] = (7, 30)
    quiet_hours: tuple[int, int] | None = (22 * 60, 7 * 60)
    quiet_hours_urgent: bool = False
    notify_min_priority: str = "high"
    max_notifications_per_day: int = 6
    dedupe_days: int = 7
    sources: tuple[str, ...] = ("calendar", "memory")
    allowed_actions: tuple[str, ...] = ()
    proposal_ttl_minutes: int = 1440
    max_turns: int = 8
    telegram: bool = False
    telegram_chat: str = ""
    backend: str = ""
    model: str = ""


def _hhmm(text: Any) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def _quiet(text: Any) -> tuple[int, int] | None | bool:
    """quiet_hours as (start, end) minutes, None for none, False if bad."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    parts = raw.split("-")
    if len(parts) != 2:
        return False
    a, b = _hhmm(parts[0]), _hhmm(parts[1])
    if a is None or b is None:
        return False
    return a[0] * 60 + a[1], b[0] * 60 + b[1]


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    v = int(value)
    return v if lo <= v <= hi else default


def _strs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))


def user_enabled(config: Any) -> bool:
    """Whether [companion] enabled is true in the USER layer (config.toml)
    and still in the merged config: a profile can switch the Companion off,
    never on, and a repository's .evi.toml can't touch it (evi/project.py).
    A Config built in code (no layers) goes by its own value."""
    section = getattr(config, "companion", None)
    if getattr(section, "enabled", False) is not True:
        return False
    raw = getattr(config, "_user_raw", None)
    if raw is None:
        return True
    body = raw.get("companion") if isinstance(raw, dict) else None
    return isinstance(body, dict) and body.get("enabled") is True


def settings(config: Any) -> Settings:
    """[companion] of `config` checked (see `Settings`)."""
    c = getattr(config, "companion", None)
    get = (lambda k, d=None: getattr(c, k, d))  # noqa: E731
    quiet = _quiet(get("quiet_hours", DEFAULTS["quiet_hours"]))
    if quiet is False:
        logger.warning("companion: [companion] quiet_hours %r is not HH:MM-HH:MM; using %s",
                       get("quiet_hours"), DEFAULTS["quiet_hours"])
        quiet = _quiet(DEFAULTS["quiet_hours"])
    level = str(get("notify_min_priority", "high") or "").strip().lower()
    return Settings(
        enabled=user_enabled(config),
        check_every_hours=_int(get("check_every_hours"), 3, 1, 168),
        brief_at=_hhmm(get("brief_at")) or (7, 30),
        quiet_hours=quiet,  # type: ignore[arg-type]
        quiet_hours_urgent=get("quiet_hours_urgent") is True,
        notify_min_priority=level if level in PRIORITIES else "high",
        max_notifications_per_day=_int(get("max_notifications_per_day"), 6, 0, 1000),
        dedupe_days=_int(get("dedupe_days"), 7, 0, 365),
        sources=_strs(get("sources", ["calendar", "memory"])),
        allowed_actions=_strs(get("allowed_actions", [])),
        proposal_ttl_minutes=_int(get("proposal_ttl_minutes"), 1440, 1, 60 * 24 * 30),
        max_turns=_int(get("max_turns"), 8, 1, 50),
        telegram=get("telegram") is True,
        telegram_chat=str(get("telegram_chat", "") or "").strip(),
        backend=str(get("backend", "") or "").strip(),
        model=str(get("model", "") or "").strip(),
    )


# ---- time ---------------------------------------------------------------------------


def local(now: datetime | None = None) -> datetime:
    """`now` (default: this moment) as an aware local time; a naive `now`
    is read as local."""
    return (now or datetime.now()).astimezone()


def in_quiet_hours(now: datetime, quiet: tuple[int, int] | None) -> bool:
    """Whether local `now` falls in `quiet` ((start, end) minutes; it may
    wrap past midnight)."""
    if not quiet:
        return False
    start, end = quiet
    m = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= m < end
    return m >= start or m < end


def _rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else -1


# ---- the model's answer ---------------------------------------------------------------


def _text_ok(value: Any, limit: int, *, required: bool, multiline: bool = False) -> bool:
    if not isinstance(value, str) or len(value) > limit:
        return False
    if required and not value.strip():
        return False
    allowed = "\n\t" if multiline else ""
    return all(c.isprintable() or c in allowed for c in value)


def _item_errors(item: Any) -> str | None:
    """Why `item` breaks the schema (`prompts.OUTPUT_SCHEMA`), or None."""
    if not isinstance(item, dict):
        return "an item is not an object"
    required = {"kind", "title", "body", "priority", "why_now", "dedupe_key"}
    extra = set(item) - required - {"proposal"}
    if extra:
        return f"unknown keys {sorted(extra)[:5]}"
    missing = required - set(item)
    if missing:
        return f"missing {sorted(missing)}"
    if item["kind"] not in KINDS:
        return "bad kind"
    if item["priority"] not in PRIORITIES:
        return "bad priority"
    if not _text_ok(item["title"], prompts.TITLE_MAX, required=True):
        return "bad title"
    if not _text_ok(item["body"], prompts.BODY_MAX, required=False, multiline=True):
        return "bad body"
    if not _text_ok(item["why_now"], prompts.WHY_MAX, required=False, multiline=True):
        return "bad why_now"
    if not _text_ok(item["dedupe_key"], prompts.KEY_MAX, required=True):
        return "bad dedupe_key"
    prop = item.get("proposal")
    if prop is None:
        return None
    if not isinstance(prop, dict) or set(prop) != {"tool", "args", "summary"}:
        return "bad proposal"
    if not _text_ok(prop["tool"], prompts.TOOL_MAX, required=True):
        return "bad proposal tool"
    if not isinstance(prop["args"], dict):
        return "proposal args are not an object"
    try:
        if len(json.dumps(prop["args"])) > prompts.ARGS_MAX_CHARS:
            return "proposal args too long"
    except (TypeError, ValueError, RecursionError):
        return "proposal args are not JSON"
    if not _text_ok(prop["summary"], prompts.SUMMARY_MAX, required=True):
        return "bad proposal summary"
    return None


def parse_answer(text: str) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """The model's final answer as items: (items, problems). items is None
    when the answer as a whole is not one JSON object ``{"items": [...]}``
    (a ```json fence around it is the only wrapping taken); otherwise the
    items that meet the schema, and `problems` says why each other one was
    dropped."""
    raw = str(text or "").strip()
    fenced = _FENCE.fullmatch(raw)
    if fenced:
        raw = fenced.group(1)
    if len(raw) > 200_000:
        return None, ["answer too long"]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None, ["not JSON"]
    if not isinstance(data, dict) or set(data) != {"items"} or not isinstance(data["items"], list):
        return None, ['not an object {"items": [...]}']
    if len(data["items"]) > prompts.MAX_ITEMS:
        return None, [f"more than {prompts.MAX_ITEMS} items"]
    items: list[dict[str, Any]] = []
    problems: list[str] = []
    for i, item in enumerate(data["items"]):
        why = _item_errors(item)
        if why is None:
            items.append(item)
        else:
            problems.append(f"item {i}: {why}")
    return items, problems


# ---- actions ---------------------------------------------------------------------------


def _words(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    return {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w}


def money_or_delete(name: str, category: str = "") -> bool:
    """Whether a tool's name or category says it moves money (payments,
    transfers, trading) or deletes something (`MONEY_OR_DELETE_WORDS`)."""
    words = _words(name) | _words(category)
    if words & MONEY_OR_DELETE_WORDS:
        return True
    flat = re.sub(r"[^a-z0-9]", "", f"{name} {category}".lower())
    return any(root in flat for root in _MONEY_OR_DELETE_ROOTS)


def args_hash(args: Any) -> str:
    """The hash a proposal's arguments are pinned by (canonical JSON)."""
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _registry() -> dict[str, Any]:
    from evi.tools import register_builtin_tools

    return register_builtin_tools()


def _empties_a_file(path: str, old_new: list[tuple[str, str]], replace_all: bool) -> bool:
    """Whether replacing `old_new` in the file at `path` leaves it blank (a
    file that can't be read, or isn't there, says no: the tool fails then)."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not text.strip():
        return False
    new = text
    for old, rep in old_new:
        if not isinstance(old, str) or not isinstance(rep, str) or not old:
            return False
        new = new.replace(old, rep) if replace_all else new.replace(old, rep, 1)
    return not new.strip()


def _content_refusal(name: str, category: str, args: dict[str, Any]) -> str | None:
    """Why a proposed call deletes or moves money through a general tool
    (`action_refusal`), or None."""
    from evi import permissions

    if name in _NEVER_PROPOSED or category in _NEVER_PROPOSED_CATEGORIES or (
            name.startswith("delegate")):
        return (f"{name} runs a model, code or the screen whose effect its arguments don't "
                "spell out: the Companion never proposes it")
    if permissions._runs_shell(name, category, args):
        return (f"{name} runs a shell line, which can delete, empty a file or move money "
                "in ways its words don't show: the Companion never proposes one")
    if name == "sqlite_query":
        from evi.tools.sqlite import _ALLOWED_FIRST_TOKEN, _first_token

        sql = str(args.get("sql") or "")
        if (_first_token(sql) not in _ALLOWED_FIRST_TOKEN or ";" in sql.strip().rstrip(";")
                or _words(sql) & _SQL_WRITE_WORDS):
            return "its SQL is not a plain read"
    if name == "web_fetch" and money_or_delete(str(args.get("url") or "")):
        return "its URL looks like a payment, transfer or deletion endpoint"
    path = args.get("path")
    if name == "write_file" and isinstance(path, str) and path:
        target = Path(path).expanduser()
        if target.is_absolute() and target.exists():
            return f"it would overwrite the existing file {path!r} (edit_file changes one)"
    if name == "edit_file" and isinstance(path, str) and _empties_a_file(
            path, [(args.get("old_string"), args.get("new_string"))],
            args.get("replace_all") is True):
        return f"it would empty the file {path!r}"
    if name == "apply_patch" and isinstance(path, str):
        from evi.tools.fs import _parse_patch

        blocks = _parse_patch(str(args.get("patch") or ""))
        if blocks and _empties_a_file(path, blocks, False):
            return f"it would empty the file {path!r}"
    return None


def action_refusal(name: str, args: Any, config: Any, allowed: Iterable[str], *,
                   need_tool: bool) -> str | None:
    """Why the action `name` with `args` may not be proposed (or approved),
    or None. `need_tool`: the tool must be loaded in this process (at
    approval; an MCP tool only exists where its server runs)."""
    from evi import permissions
    from evi.shell_guard import destructive_hit

    if name not in set(allowed):
        return f"{name} is not in [companion] allowed_actions"
    if not isinstance(args, dict):
        return "its arguments are not an object"
    tool = _registry().get(name)
    if tool is None and need_tool:
        return f"{name} is not a tool eVi has loaded here"
    category = str(getattr(tool, "category", "") or ("mcp" if "." in name else ""))
    if money_or_delete(name, category):
        return (f"{name} looks like a payment, transfer, trade or deletion tool: the "
                "Companion never proposes one")
    command = permissions.shell_command_of(name, category, args)
    if command is not None:
        hit = destructive_hit(command)
        if hit is not None:
            return f"the destructive-command guard: {hit.reason}"
    why = _content_refusal(name, category, args)
    if why is not None:
        return why
    auto = getattr(config, "auto", None)
    protected = permissions.effective_protected_paths(getattr(auto, "protected_paths", None))
    path_args = (tool.path_args(args) if tool is not None
                 else permissions.path_values(args, category))
    for value in path_args:
        # Checked here and run later, possibly in another process: a relative
        # path (or none: run_command's cwd left out) could mean two different
        # folders.
        if not str(value).strip():
            return "a path argument (path or cwd) is left empty: it must be an absolute path"
        if not Path(str(value)).expanduser().is_absolute():
            return f"its path argument {value!r} is not absolute"
    decision = permissions.decide(
        "ask", (), getattr(auto, "rules", []) or [], name, category, args,
        hard_deny=getattr(auto, "hard_deny", []) or [], protected_paths=protected,
        path_args=path_args, read_only=getattr(tool, "read_only", None),
    )
    if decision == "deny":
        why = permissions.director_ground(name, category, args, path_args=path_args)
        return why or "an [auto] hard_deny or deny rule refuses it"
    hit_path = permissions.protected_write(
        name, category, args, protected, path_args=path_args,
        read_only=getattr(tool, "read_only", None))
    if hit_path is not None:
        return f"it writes the protected path {hit_path!r}"
    return None


# ---- delivery --------------------------------------------------------------------------


def deliver(title: str, body: str, config: Any, s: Settings) -> list[str]:
    """Send one notification: through `evi.notify` with [notify]'s sound,
    desktop and url channels, and to the owner's Telegram chat when
    `s.telegram` (outbound text only). Returns the channels that delivered
    (no desktop toast on Windows: `evi.notify` leaves that to the UI)."""
    from evi import notify as _notify

    n = getattr(config, "notify", None)
    sound = getattr(n, "sound", True) is True
    desktop = getattr(n, "desktop", True) is True
    url = str(getattr(n, "url", "") or "").strip()
    used = [str(c) for c in (_notify.notify(title, body, sound=sound, desktop=desktop, url=url)
                             or [])]
    if s.telegram:
        from evi.channels import telegram

        if telegram.send_to_owner(f"{title}\n\n{body}", config, s.telegram_chat):
            used.append("telegram")
    return used


# ---- the engine ------------------------------------------------------------------------


@dataclass
class CheckIn:
    """What one check-in did (also the audit record's content)."""

    ran: bool = False
    skipped: str | None = None
    sources: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    denials: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    notified: int = 0
    error: str | None = None
    brief: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Companion:
    """The engine over one store. `config`: default `Config.load()`.
    `client`: the model client a check-in's agent uses (default: built from
    the config). `notifier(title, body) -> channels`: default `deliver`."""

    def __init__(self, config: Any = None, store: CompanionStore | None = None, *,
                 client: Any = None,
                 notifier: Callable[[str, str], list[str]] | None = None) -> None:
        if config is None:
            from evi.config import Config

            config = Config.load()
        self.config = config
        self.settings = settings(config)
        self.store = store or CompanionStore()
        self.client = client
        self._notifier = notifier

    # -- entry points --------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> CheckIn | None:
        """The scheduler's job: a check-in when one is due (`check_every_hours`
        since the last), else the brief when it is due. Never raises."""
        now = local(now)
        try:
            if not self._may_run():
                return None
            state = self.store.load_state()
            last = state.get("last_check")
            # A last check in the future (the clock was ahead once) is due too.
            due = not isinstance(last, (int, float)) or last > now.timestamp() + 60 or (
                now.timestamp() - last >= self.settings.check_every_hours * 3600 - 60)
            if due:
                return self.check_in(now)
            self.brief(now)
        except Exception as exc:  # noqa: BLE001 — a scheduled job must not die
            logger.warning("companion: tick failed: %s", exc)
            self.store.audit("error", where="tick", error=f"{type(exc).__name__}: {exc}")
        return None

    def check_in(self, now: datetime | None = None) -> CheckIn:
        """One check-in now (then the brief, if it is due and not yet out
        today). Skipped when the Companion is off, safe mode is on or
        another check-in is running."""
        now = local(now)
        result = CheckIn()
        if not self._may_run():
            result.skipped = "disabled"
            return result
        with self.store.run_lock() as got:
            if not got:
                result.skipped = "busy"
                self.store.audit("check_in", skipped="busy")
                return result
            self.expire(now)
            # What waited (quiet hours, yesterday's limit) goes first.
            try:
                self._release_held(now)
            except Exception as exc:  # noqa: BLE001
                self.store.audit("error", where="held", error=f"{type(exc).__name__}: {exc}")
            try:
                self._check_in(now, result)
            except Exception as exc:  # noqa: BLE001 — record it, never crash the caller
                logger.warning("companion: check-in failed: %s", exc)
                result.error = f"{type(exc).__name__}: {exc}"
                self.store.audit("check_in", error=result.error, sources=result.sources)
                with contextlib.suppress(Exception):
                    self._mark_checked(now)
            try:
                result.brief = self._brief(now, force=False)
            except Exception as exc:  # noqa: BLE001
                self.store.audit("error", where="brief", error=f"{type(exc).__name__}: {exc}")
        return result

    def brief(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any] | None:
        """The morning brief when it is due (`brief_at` passed today and no
        brief out yet today), or now whatever the time with `force`. Returns
        the feed item, or None when none went out."""
        now = local(now)
        if not self._may_run():
            return None
        with self.store.run_lock() as got:
            if not got:
                return None
            try:
                self._release_held(now)
            except Exception as exc:  # noqa: BLE001
                self.store.audit("error", where="held", error=f"{type(exc).__name__}: {exc}")
            return self._brief(now, force=force)

    def expire(self, now: datetime | None = None) -> list[str]:
        """Mark pending proposals past their expiry expired (audited);
        returns their ids."""
        ts = local(now).timestamp()
        gone: list[str] = []
        with self.store.lock():
            props = self.store.load_proposals()
            for pid, p in props.items():
                if p.get("state") == "pending" and float(p.get("expires_at") or 0) <= ts:
                    p["state"] = "expired"
                    p["decided_at"] = ts
                    gone.append(pid)
            if gone:
                self.store.save_proposals(props)
        for pid in gone:
            self.store.audit("expire", proposal=pid, tool=props[pid].get("tool"))
        return gone

    def approve(self, proposal_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Run the pending proposal `proposal_id` once, as stored (see the
        module doc). Returns it with its new state ("executed" or "failed")
        and result. Raises KeyError for an unknown id, ProposalError when
        it can't run (the reason; a refused or expired one is recorded)."""
        ts = local(now).timestamp()
        with self.store.lock():
            props = self.store.load_proposals()
            p = props.get(proposal_id)
            if p is None:
                raise KeyError(proposal_id)
            if p.get("state") != "pending":
                raise ProposalError(f"proposal {proposal_id} is {p.get('state')}, not pending")
            if float(p.get("expires_at") or 0) <= ts:
                p.update(state="expired", decided_at=ts)
                self.store.save_proposals(props)
                self.store.audit("expire", proposal=proposal_id, tool=p.get("tool"))
                raise ProposalError(f"proposal {proposal_id} has expired")
            if not self.settings.enabled:
                raise ProposalError("the Companion is off ([companion] enabled)")
            from evi import safemode

            if safemode.enabled():  # no hooks there: their vetoes couldn't run
                raise ProposalError("safe mode is on: no Companion action runs")
            why: str | None
            if args_hash(p.get("args")) != p.get("args_hash"):
                why = "its arguments changed since it was proposed"
            else:
                why = action_refusal(str(p.get("tool") or ""), p.get("args"), self.config,
                                     self.settings.allowed_actions, need_tool=True)
            if why is not None:
                p.update(state="refused", decided_at=ts, reason=why)
                self.store.save_proposals(props)
                self.store.audit("approve_refused", proposal=proposal_id, tool=p.get("tool"),
                                 reason=why)
                raise ProposalError(f"proposal {proposal_id} refused: {why}")
            p.update(state="running", decided_at=ts)
            self.store.save_proposals(props)
            tool_name, args = str(p["tool"]), dict(p["args"])
        self.store.audit("approve", proposal=proposal_id, tool=tool_name, args=args)
        hooks, args_json = self._hooks(), json.dumps(args)
        veto = self._hook_veto(hooks, tool_name, args_json)
        if veto is not None:
            with self.store.lock():
                props = self.store.load_proposals()
                p = props.get(proposal_id) or p
                p.update(state="refused", reason=veto, finished_at=time.time())
                props[proposal_id] = p
                self.store.save_proposals(props)
            self.store.audit("approve_refused", proposal=proposal_id, tool=tool_name,
                             reason=veto)
            raise ProposalError(f"proposal {proposal_id} refused: {veto}")
        output = self._execute(tool_name, args)
        if hooks is not None:
            try:
                hooks.run_after(tool_name, args_json, output)
            except Exception:  # noqa: BLE001 — an observer must not replace the result
                pass
        from evi.redact import redact

        text = redact(output)
        cut = text if len(text) <= RESULT_MAX else text[:RESULT_MAX] + " [... cut]"
        failed = text.startswith(("ERROR", "PERMISSION DENIED", "BLOCKED"))
        with self.store.lock():
            props = self.store.load_proposals()
            p = props.get(proposal_id) or p
            p.update(state="failed" if failed else "executed", result=cut,
                     finished_at=time.time())
            props[proposal_id] = p
            self.store.save_proposals(props)
            self.store.append_feed([self._entry(
                kind="result",
                title=("Failed: " if failed else "Done: ") + str(p.get("summary")),
                body=cut, priority="low", why_now="you approved it",
                dedupe_key=f"result:{proposal_id}", source="approval", ts=time.time(),
                proposal_id=proposal_id)])
        self.store.audit("execute", proposal=proposal_id, tool=tool_name,
                         state=p["state"], result=cut)
        return dict(p)

    def reject(self, proposal_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Reject the pending proposal `proposal_id` (recorded). KeyError for
        an unknown id, ProposalError when it isn't pending."""
        ts = local(now).timestamp()
        with self.store.lock():
            props = self.store.load_proposals()
            p = props.get(proposal_id)
            if p is None:
                raise KeyError(proposal_id)
            if p.get("state") != "pending":
                raise ProposalError(f"proposal {proposal_id} is {p.get('state')}, not pending")
            p.update(state="rejected", decided_at=ts)
            self.store.save_proposals(props)
        self.store.audit("reject", proposal=proposal_id, tool=p.get("tool"))
        return dict(p)

    def proposals(self, state: str | None = None,
                  now: datetime | None = None) -> list[dict[str, Any]]:
        """Proposals (oldest first), only those in `state` when given; the
        expired ones are marked first."""
        self.expire(now)
        rows = [{"id": pid, **p} for pid, p in self.store.load_proposals().items()]
        if state:
            rows = [r for r in rows if r.get("state") == state]
        return sorted(rows, key=lambda r: float(r.get("created_at") or 0))

    def feed(self, limit: int = 50, since: float | datetime | None = None) -> list[dict[str, Any]]:
        """The newest `limit` feed items (oldest first), only those after
        `since` (a time or epoch seconds) when given."""
        items = self.store.read_feed()
        if since is not None:
            cut = since.timestamp() if isinstance(since, datetime) else float(since)
            items = [i for i in items if float(i.get("ts") or 0) > cut]
        return items[-max(int(limit), 0):] if limit else []

    def status(self) -> dict[str, Any]:
        """What the Companion is set to and where it stands."""
        s = self.settings
        state = self.store.load_state()
        last = state.get("last_check")
        pending = [p for p in self.store.load_proposals().values() if p.get("state") == "pending"]
        day = local().date().isoformat()
        notified = state.get("notified") or {}
        return {
            "enabled": s.enabled,
            "root": str(self.store.root),
            "sources": list(s.sources),
            "allowed_actions": list(s.allowed_actions),
            "check_every_hours": s.check_every_hours,
            "brief_at": "%02d:%02d" % s.brief_at,
            "last_check": last,
            "next_check": (last + s.check_every_hours * 3600)
            if isinstance(last, (int, float)) else None,
            "brief_day": state.get("brief_day"),
            "pending_proposals": len(pending),
            "notifications_today": int(notified.get("count") or 0)
            if notified.get("day") == day else 0,
        }

    # -- internals: the check-in ---------------------------------------------------

    def _may_run(self) -> bool:
        from evi import safemode

        return self.settings.enabled and not safemode.enabled()

    def _check_in(self, now: datetime, result: CheckIn) -> None:
        from evi.policy import PolicyError, watcher_policy

        s = self.settings
        try:
            policy = watcher_policy(s.sources, self.store.root)
        except PolicyError as exc:
            result.error = str(exc)
            self._mark_checked(now)  # asked again next interval, not every tick
            self.store.audit("check_in", error=result.error)
            return
        tools, result.unavailable = self._source_tools(policy)
        result.sources = [t.name for t in tools]
        if not tools:
            result.skipped = "no sources"
            self._mark_checked(now)
            self.store.audit("check_in", skipped="no sources", unavailable=result.unavailable)
            return
        nonce = secrets.token_hex(8)
        data, complete = self._prefetch(tools)
        instructions = self.store.read_instructions()
        fingerprint = self._fingerprint(data, instructions, tools) if complete else None
        with self.store.lock():
            state = self.store.load_state()
            if fingerprint is not None and fingerprint == state.get("fingerprint"):
                state["last_check"] = now.timestamp()
                self.store.save_state(state)
                result.skipped = "unchanged"
        if result.skipped:
            self.store.audit("check_in", skipped="unchanged", sources=result.sources,
                             unavailable=result.unavailable)
            return
        recent = [i for i in self.store.read_feed()[-30:] if i.get("source") == "check_in"]
        pending = [p for p in self.store.load_proposals().values() if p.get("state") == "pending"]
        message = prompts.check_in_message(
            now_text=now.strftime("%A %Y-%m-%d %H:%M %Z").strip(),
            sources=result.sources, actions=s.allowed_actions, instructions=instructions,
            recent=recent, pending=pending, data=data, nonce=nonce)
        answer, report = self._ask(tools, policy, nonce, message)
        allowed = {t.name for t in tools}
        for call in report.tool_calls if report is not None else []:
            if call.get("status") == "denied" or call.get("name") not in allowed:
                result.denials.append({"tool": call.get("name"), "status": call.get("status"),
                                       "reason": str(call.get("reason") or call.get("output")
                                                     or "")[:300]})
        if report is not None and report.error:
            result.error = str(report.error)[:300]
        result.ran = True
        items, problems = parse_answer(answer)
        result.dropped = problems
        if items is None:
            from evi.redact import redact

            # The model was asked: the next one waits its turn, but the
            # fingerprint isn't kept, so that one asks again.
            self._mark_checked(now)
            self.store.audit("check_in", ran=True, rejected_answer=problems,
                             answer=redact(answer)[:300], denials=result.denials,
                             sources=result.sources, error=result.error)
            return
        with self.store.lock():
            state = self.store.load_state()
            to_send = self._judge(items, now, state, result, source="check_in")
            state["last_check"] = now.timestamp()
            if fingerprint is not None:
                state["fingerprint"] = fingerprint
            self.store.save_state(state)
        for entry in to_send:  # outside the lock: a slow channel holds up nobody
            self._notify(entry)
        self.store.audit("check_in", ran=True, sources=result.sources,
                         unavailable=result.unavailable,
                         items=[{"kind": i["kind"], "title": i["title"],
                                 "dedupe_key": i["dedupe_key"]} for i in result.items],
                         dropped=problems, denials=result.denials, proposals=result.proposals,
                         refused=result.refused, notified=result.notified, error=result.error)

    def _mark_checked(self, now: datetime) -> None:
        """Record a check-in at `now` (the next one waits `check_every_hours`)."""
        with self.store.lock():
            state = self.store.load_state()
            state["last_check"] = now.timestamp()
            self.store.save_state(state)

    def _source_tools(self, policy: Any) -> tuple[list[Any], list[str]]:
        """The policy's source tools as loaded here, wrapped so their output
        reaches the model as untrusted data; and the ones not loaded."""
        reg = _registry()
        tools: list[Any] = []
        missing: list[str] = []
        for name, category in policy.tools:
            tool = reg.get(name)
            if tool is None or getattr(tool, "category", "") != category:
                missing.append(name)
                continue
            tools.append(tool)
        return tools, missing

    def _wrap(self, tool: Any, nonce: str) -> Any:
        from evi.tools.base import Tool, ToolContext

        if tool.category == "mcp":
            def call(**kwargs: Any) -> str:
                return prompts.envelope(tool.name, tool.call_rich(kwargs).text, nonce)
        else:
            def call(ctx: ToolContext | None = None, **kwargs: Any) -> str:  # type: ignore[misc]
                return prompts.envelope(tool.name, tool.call_rich(kwargs, ctx).text, nonce)

        return Tool(name=tool.name, description=tool.description, parameters=tool.parameters,
                    func=call, category=tool.category, path_params=tool.path_params,
                    read_only=True, trust="untrusted")

    def _prefetch(self, tools: list[Any]) -> tuple[list[tuple[str, str]], bool]:
        """Read the sources that take no arguments now, in code: (source,
        text) pairs, and whether what every source reads is covered so (else
        the fingerprint can't vouch for an unchanged world). A source that
        needs arguments is covered by another's read (`_COVERED_BY`:
        calendar_search by calendar_week) or by the memory index (the
        memory tools); an MCP one never is."""
        names = {t.name for t in tools}
        data: list[tuple[str, str]] = []
        complete = True
        for tool in tools:
            required = (tool.parameters or {}).get("required") or []
            if required:
                if tool.category != "memory" and _COVERED_BY.get(tool.name) not in names:
                    complete = False
                continue
            if _COVERED_BY.get(tool.name) in names:
                continue
            try:
                text = tool.call_rich({}).text
            except Exception as exc:  # noqa: BLE001
                text = f"ERROR: {type(exc).__name__}: {exc}"
            data.append((tool.name, text))
        return data, complete

    def _fingerprint(self, data: list[tuple[str, str]], instructions: str,
                     tools: list[Any]) -> str:
        h = hashlib.sha256()
        for name, text in data:
            h.update(f"{name}\0{text}\0".encode("utf-8", "replace"))
        if any(t.category == "memory" for t in tools):
            h.update(_memory_index().encode())
        h.update(instructions.encode("utf-8", "replace"))
        h.update(json.dumps([sorted(t.name for t in tools),
                             list(self.settings.allowed_actions)]).encode())
        return h.hexdigest()

    def _ask(self, tools: list[Any], policy: Any, nonce: str,
             message: str) -> tuple[str, Any]:
        """Run the check-in agent: (its final text, the run report)."""
        from evi import workdir
        from evi.llm.agent import TextDelta, ToolCall, ToolResult

        cfg = self.config
        # No verify checks (nothing is written) and never a delegate CLI agent.
        cfg = dataclasses.replace(
            cfg,
            checks=dataclasses.replace(cfg.checks, verify="off"),
            llm=dataclasses.replace(cfg.llm, allow_cli_agent_unattended=False),
        )
        final: list[str] = []

        def on_event(event: Any) -> None:
            if isinstance(event, (ToolCall, ToolResult)):
                final.clear()  # only the text after the last tool round is the answer
            elif isinstance(event, TextDelta):
                final.append(event.text)

        # The turn sets this thread's working folder; put it back afterwards.
        token = workdir.set_cwd(None)
        try:
            return self._run(cfg, tools, policy, nonce, message, on_event, final)
        finally:
            workdir.reset(token)

    def _run(self, cfg: Any, tools: list[Any], policy: Any, nonce: str, message: str,
             on_event: Callable[[Any], None], final: list[str]) -> tuple[str, Any]:
        """Build the check-in agent (watcher policy, unattended) and run it."""
        from evi.headless import run_headless
        from evi.llm.agent import make_unattended
        from evi.llm.cli_agent import unattended
        from evi.sdk.builder import build_agent

        s = self.settings
        with unattended("companion check-in", cfg):
            agent = build_agent(
                config=cfg, system_prompt=prompts.SYSTEM_PROMPT,
                tools=[self._wrap(t, nonce) for t in tools], client=self.client,
                backend=s.backend or None, model=s.model or None, enable_project=False,
                enable_memory=False, enable_skills=False, unattended_policy="deny",
            )
            agent.cwd = str(self.store.root)
            agent.policy = policy
            make_unattended(agent, "deny")
            report = run_headless(agent, message, max_turns=s.max_turns, on_event=on_event,
                                  report_errors=True)
        return "".join(final), report

    # -- internals: judgment -----------------------------------------------------------

    def _judge(self, items: list[dict[str, Any]], now: datetime, state: dict[str, Any],
               result: CheckIn, *, source: str) -> list[dict[str, Any]]:
        """Dedupe, store and propose; decide which items are notified and
        count them. Returns those (the caller holds the lock, saves `state`,
        then sends them with `_notify`)."""
        s = self.settings
        ts = now.timestamp()
        window = s.dedupe_days * 86400
        # A time in the future (the clock was ahead once) vouches for nothing.
        seen = {k: v for k, v in (state.get("seen") or {}).items()
                if isinstance(v, (int, float)) and 0 <= ts - v < window}
        entries = []
        to_send = []
        for item in items:
            key = item["dedupe_key"]
            if key in seen:
                result.dropped.append(f"duplicate {key}")
                continue
            seen[key] = ts
            entry = self._entry(source=source, ts=ts, **{k: item[k] for k in (
                "kind", "title", "body", "priority", "why_now", "dedupe_key")})
            prop = item.get("proposal")
            if prop is not None:
                why = action_refusal(prop["tool"], prop["args"], self.config,
                                     s.allowed_actions, need_tool=False)
                if why is None:
                    pid = self._propose(prop, ts, key)
                    entry["proposal_id"] = pid
                    result.proposals.append(pid)
                else:
                    entry["proposal_refused"] = {"tool": prop["tool"], "reason": why}
                    result.refused.append({"tool": prop["tool"], "reason": why})
                    self.store.audit("proposal_refused", tool=prop["tool"], args=prop["args"],
                                     reason=why, dedupe_key=key)
            ok, why_not = self._may_notify(item["priority"], now, state)
            if ok:
                self._count(now, state)
                entry["notified"] = True
                to_send.append(entry)
                result.notified += 1
            else:
                entry["held"] = why_not
                if why_not in _WAITS:  # sent later (`_release_held`)
                    state["held"] = [*(state.get("held") or []), entry][-HELD_MAX:]
            entries.append(entry)
            result.items.append(entry)
        state["seen"] = seen
        self.store.append_feed(entries)
        return to_send

    def _entry(self, **fields: Any) -> dict[str, Any]:
        ts = float(fields.get("ts") or time.time())
        return {"id": secrets.token_hex(6), "at": datetime.fromtimestamp(ts).astimezone()
                .isoformat(timespec="seconds"), "notified": False, "held": None, **fields}

    def _propose(self, prop: dict[str, Any], ts: float, key: str) -> str:
        pid = secrets.token_hex(6)
        record = {
            "tool": prop["tool"], "args": prop["args"], "args_hash": args_hash(prop["args"]),
            "summary": prop["summary"], "dedupe_key": key, "state": "pending",
            "created_at": ts, "expires_at": ts + self.settings.proposal_ttl_minutes * 60,
        }
        props = self.store.load_proposals()
        props[pid] = record
        self.store.save_proposals(props)
        self.store.audit("proposal", proposal=pid, tool=record["tool"], args=record["args"],
                         summary=record["summary"], expires_at=record["expires_at"])
        return pid

    def _may_notify(self, priority: str, now: datetime, state: dict[str, Any], *,
                    threshold: bool = True) -> tuple[bool, str]:
        s = self.settings
        if state.get("quiet") or self.store.state_corrupt:
            return False, "state unreadable: notifying nothing this time"
        if threshold and _rank(priority) < _rank(s.notify_min_priority):
            return False, f"below {s.notify_min_priority}"
        if in_quiet_hours(now, s.quiet_hours) and not (
                priority == "urgent" and s.quiet_hours_urgent):
            return False, "quiet hours"
        day = now.date().isoformat()
        n = state.get("notified") or {}
        count = int(n.get("count") or 0) if n.get("day") == day else 0
        if count >= s.max_notifications_per_day:
            return False, "daily limit reached"
        return True, ""

    def _release_held(self, now: datetime) -> list[dict[str, Any]]:
        """Notify the items held for quiet hours or the daily limit that may
        go now (most urgent first, still under the limit); drop those held
        longer than `HELD_MAX_AGE` or now below the threshold. Returns the
        ones sent."""
        ts = now.timestamp()
        send: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        with self.store.lock():
            state = self.store.load_state()
            held = [h for h in state.get("held") or [] if isinstance(h, dict)]
            if not held:
                return []
            keep: list[dict[str, Any]] = []
            for h in sorted(held, key=lambda h: (-_rank(str(h.get("priority"))),
                                                 float(h.get("ts") or 0))):
                age = ts - float(h.get("ts") or 0)
                if abs(age) > HELD_MAX_AGE:
                    dropped.append(h)
                    continue
                ok, why_not = self._may_notify(str(h.get("priority")), now, state)
                if ok:
                    self._count(now, state)
                    send.append(h)
                elif why_not in _WAITS:
                    keep.append(h)
                else:
                    dropped.append(h)
            state["held"] = sorted(keep, key=lambda h: float(h.get("ts") or 0))
            self.store.save_state(state)
        for h in dropped:
            self.store.audit("held_dropped", item=h.get("id"), title=h.get("title"),
                             held=h.get("held"))
        for h in send:
            self._notify(h, released=True)
        return send

    def _count(self, now: datetime, state: dict[str, Any]) -> None:
        day = now.date().isoformat()
        n = state.get("notified") or {}
        count = int(n.get("count") or 0) if n.get("day") == day else 0
        state["notified"] = {"day": day, "count": count + 1}

    def _notify(self, entry: dict[str, Any], *, released: bool = False) -> list[str]:
        from evi.redact import redact

        title = redact(f"eVi: {entry['title']}")[:200]
        body = str(entry.get("body") or "")
        if len(body) > NOTIFY_BODY_MAX:
            body = body[:NOTIFY_BODY_MAX] + " ..."
        if entry.get("proposal_id"):
            body += "\n\nA proposed action is waiting: approve or reject it in eVi on this machine."
        body = redact(body)
        try:
            if self._notifier is not None:
                used = list(self._notifier(title, body) or [])
            else:
                used = deliver(title, body, self.config, self.settings)
        except Exception as exc:  # noqa: BLE001 — a failed ping is recorded, not fatal
            used = []
            self.store.audit("notify_failed", title=title, error=f"{type(exc).__name__}: {exc}")
        self.store.audit("notify", item=entry.get("id"), title=title, channels=used,
                         priority=entry.get("priority"),
                         **({"held": entry.get("held")} if released else {}))
        return used

    def _hooks(self) -> Any:
        """The owner's hooks (hooks.toml and plugins); None when they can't
        be loaded (then `_hook_veto` fails closed)."""
        from evi.hooks import load_hooks

        try:
            return load_hooks()
        except Exception as exc:  # noqa: BLE001
            logger.warning("companion: hooks could not be loaded: %s", exc)
            return None

    def _hook_veto(self, hooks: Any, name: str, args_json: str) -> str | None:
        """Run the before_tool_call hooks for an approved call as the agent
        does: why one vetoed it, or None. Hooks that can't be loaded or run
        block it (fail closed)."""
        if hooks is None:
            return "BLOCKED BY HOOK: the hooks could not be loaded"
        try:
            _results, veto = hooks.run_before(name, args_json)
        except Exception as exc:  # noqa: BLE001
            try:
                guarded = hooks.can_veto("before_tool_call", name, args_json)
            except Exception:  # noqa: BLE001
                guarded = True
            if guarded:
                return f"BLOCKED BY HOOK: the guard hooks failed to run ({type(exc).__name__})"
            return None
        if veto is None:
            return None
        msg = (veto.stderr.strip() or veto.stdout.strip() or "(no message)")[:300]
        return f"BLOCKED BY HOOK {veto.hook_name!r}: {msg}"

    def _execute(self, name: str, args: dict[str, Any]) -> str:
        tool = _registry().get(name)
        if tool is None:
            return f"ERROR: {name} is not loaded"
        try:
            return tool.call_rich(dict(args)).text
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {type(exc).__name__}: {exc}"

    # -- internals: the brief ----------------------------------------------------------------

    def _brief(self, now: datetime, *, force: bool) -> dict[str, Any] | None:
        s = self.settings
        day = now.date().isoformat()
        with self.store.lock():
            state = self.store.load_state()
            if not force:
                if state.get("brief_day") == day:
                    return None
                if (now.hour, now.minute) < s.brief_at:
                    return None
            # Claimed before it is made: a brief that fails to build is lost
            # for the day rather than sent twice.
            state["brief_day"] = day
            self.store.save_state(state)
        body = self._brief_body(now)
        entry = self._entry(kind="brief", title=f"Your brief for {now.strftime('%A %d %B')}",
                            body=body, priority="medium", why_now="the morning brief",
                            dedupe_key=f"brief:{day}", source="brief", ts=now.timestamp())
        with self.store.lock():
            state = self.store.load_state()
            ok, why_not = self._may_notify("medium", now, state, threshold=False)
            if ok:
                self._count(now, state)
                self.store.save_state(state)
                entry["notified"] = True
            else:
                entry["held"] = why_not
            self.store.append_feed([entry])
        if entry["notified"]:
            self._notify(entry)
        self.store.audit("brief", day=day, notified=entry["notified"], held=entry["held"],
                         forced=force)
        return entry

    def _brief_body(self, now: datetime) -> str:
        from evi.policy import PolicyError, watcher_sources

        try:
            names = watcher_sources(self.settings.sources)
        except PolicyError:
            names = {}
        parts: list[str] = []
        # Any calendar source: today's part of the calendar the owner shares.
        if any(cat == "calendar" for cat in names.values()):
            tool = _registry().get("calendar_today")
            text = "(the calendar can't be read here)"
            if tool is not None and tool.category == "calendar":
                try:
                    text = tool.call_rich({}).text
                except Exception as exc:  # noqa: BLE001
                    text = f"(the calendar failed: {type(exc).__name__})"
            parts.append("Today:\n" + (text if len(text) <= 1500 else text[:1500] + " ..."))
        cutoff = now.timestamp() - self.settings.dedupe_days * 86400
        needs = [i for i in self.store.read_feed()
                 if i.get("kind") == "needs_you" and float(i.get("ts") or 0) >= cutoff][-10:]
        parts.append("Needs you:\n" + ("\n".join(f"- {i.get('title')}" for i in needs)
                                       if needs else "- nothing"))
        pending = [p for p in self.store.load_proposals().values()
                   if p.get("state") == "pending"
                   and float(p.get("expires_at") or 0) > now.timestamp()]
        parts.append("Waiting for your approval:\n" + (
            "\n".join(f"- {p.get('summary')} ({p.get('tool')})" for p in pending)
            if pending else "- nothing"))
        return "\n\n".join(parts)


def _memory_index() -> str:
    """The memory folder's notes as names, sizes and times (what changes
    when a note does)."""
    try:
        from evi.tools.memory import _store

        root = Path(_store.root)
        rows = sorted(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}"
                      for p in root.glob("*.md"))
        return "\n".join(rows)
    except Exception:  # noqa: BLE001 — unknown: changes every time
        return secrets.token_hex(8)
