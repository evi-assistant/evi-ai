"""Subagent runner — spin up a scoped `Agent` for delegated work and drain it.

A subagent shares the same `Agent` class as the main loop but is built with
a focused system prompt and a restricted tool list. Callers hand in a `task`
string, we run the agent to completion, and return the concatenated final
assistant text. Used by `evi.tools.subagent` to back `delegate_explore`,
`delegate_plan`, …

A subagent runs under the same session state as the agent that spawned it: its
tool hooks (so a parent's veto guard also binds the subagent), its working
folder and its active skill scope (a floor the subagent can only narrow). All
three are read from the spawning tool call's context (`evi.hooks.active()`,
`evi.workdir`, `evi.skillscope.inherited()`) unless the caller passes them.
Lifecycle hooks (`user_prompt_submit`, `stop`, …) are not inherited: a
delegated task is not a prompt the user typed, and a subagent finishing is not
the end of the user's turn.

Permissions follow the spawning agent too: a subagent asks through its parent's
permission callbacks (in the web UI, the session's pending-decision prompt with
its deadline), one prompt at a time however many subagents run at once, and
inherits `/auto on`, the parent's unattended policy and a remote trigger's
restrictions. With no parent UI (dream, `evi team`, a scheduled task) it runs
unattended: a call that would ask follows ``[auto] unattended_policy``, deny
by default. A profile's tool categories are intersected with the ``[tools]``
toggles (and safe mode): a profile can never hand a subagent a tool the user
switched off.

``isolate="worktree"`` (a profile's ``isolate = "worktree"``) runs the subagent
in a git worktree of its own, a fresh ``fix-<task>`` branch under
``<repo>/.worktrees/`` of the repository it would otherwise work in, and its
report ends with where that work is. Nothing is merged back. A worktree that
can't be opened fails the delegation: it never runs unisolated.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable

import tomllib

from evi import hooks as hooks_mod
from evi import skillscope
from evi import workdir
from evi.config import Config
from evi.hooks import HookRegistry
from evi.llm.agent import Done, Error, TextDelta, ToolResult
from evi.llm.client import make_client
from evi.memory import MemoryStore
from evi.redact import Redactor
from evi.tools.base import REGISTRY, Tool, active_context

logger = logging.getLogger(__name__)

# Default for `hooks=` / `skill_scope=`: take the spawning agent's (None outside
# a tool call).
INHERIT = object()

# What `isolate` (a profile's `isolate = ...`) may say.
ISOLATE_MODES: tuple[str, ...] = ("worktree",)

# Pinned tool-NAME lists for agents that must not change anything on disk.
# Names, not categories: the `fs` category also holds write_file, edit_file
# and apply_patch. tests/test_unattended_policy.py pins these lists and the
# sets of fs and memory tools, so a new one fails it until it is sorted in or
# out. (Phase 3 replaces these with tool side-effect annotations.)
READ_ONLY_FS_TOOLS: tuple[str, ...] = ("read_file", "list_dir", "find_files", "search_files")
# The memory tools (evi/tools/memory.py) that only read ~/.evi/memory, and the
# ones that change it. Not the bug-ledger tools that share the category.
MEMORY_READ_TOOLS: tuple[str, ...] = ("recall", "list_memories", "recall_by_tag")
MEMORY_WRITE_TOOLS: tuple[str, ...] = ("remember", "forget")
MEMORY_TOOLS: tuple[str, ...] = MEMORY_READ_TOOLS + MEMORY_WRITE_TOOLS
# The Explore profile: look around, change nothing.
EXPLORE_TOOLS: tuple[str, ...] = READ_ONLY_FS_TOOLS
# The dream agent (evi/dream.py): read the files and memories the transcripts
# mention, change nothing. The transcripts hold tool output anyone could have
# written, so it proposes memory changes for the user instead of making them:
# no remember / forget, fs writes, shell, code, web or computer control.
DREAM_TOOLS: tuple[str, ...] = READ_ONLY_FS_TOOLS + MEMORY_READ_TOOLS


def _tools_in_categories(categories: Iterable[str], pool: Iterable[Tool]) -> list[Tool]:
    """Tools of `pool` whose category — or, for a pinned list such as
    `EXPLORE_TOOLS`, whose name — is in `categories`."""
    allowed = set(categories)
    return [t for t in pool if t.category in allowed or t.name in allowed]


def _toggled_on(tool: Tool, toggles: dict[str, Any]) -> bool:
    """Whether the ``[tools]`` toggles enable `tool`: its category's toggle,
    or on for a category that has none (an SDK ``@tool``'s, a plugin's)."""
    value = toggles.get(tool.category)
    return value if isinstance(value, bool) else True


def select_tools(
    config: Config,
    *,
    tool_categories: Iterable[str] = (),
    tools: Iterable[str] | None = None,
) -> list[Tool]:
    """A subagent's tools.

    `tools` (a pinned name list the calling code chose: dream, multi-review,
    an SDK caller's own tools) gives exactly those registered tools, whatever
    the ``[tools]`` toggles say; only safe mode still drops memory, skills and
    MCP tools. Otherwise the tools whose category or name is in
    `tool_categories` (a profile's list), and only those the toggles enable,
    safe mode applied (`evi.sdk.builder.tool_toggles`). A category with no
    toggle (a custom ``@tool``'s) counts as enabled."""
    from evi import safemode
    from evi.sdk.builder import SAFE_MODE_OFF_CATEGORIES, tool_toggles
    from evi.tools import register_builtin_tools

    register_builtin_tools()
    if tools is not None:
        wanted = set(tools)
        off = set(SAFE_MODE_OFF_CATEGORIES) if safemode.enabled() else set()
        return [t for t in REGISTRY.values() if t.name in wanted and t.category not in off]
    toggles = tool_toggles(config)
    pool = [t for t in REGISTRY.values() if _toggled_on(t, toggles)]
    return _tools_in_categories(tool_categories, pool)


# One permission prompt at a time per parent agent: parallel subagents
# (delegate calls in one turn, parallel_research) ask from tool threads, and a
# terminal prompt can't take two answers at once.
_PROMPT_LOCKS: "weakref.WeakKeyDictionary[Any, threading.RLock]" = weakref.WeakKeyDictionary()
_PROMPT_LOCKS_GUARD = threading.Lock()
_FALLBACK_PROMPT_LOCK = threading.RLock()


def _prompt_lock(parent: Any) -> threading.RLock:
    with _PROMPT_LOCKS_GUARD:
        try:
            lock = _PROMPT_LOCKS.get(parent)
            if lock is None:
                lock = _PROMPT_LOCKS[parent] = threading.RLock()
            return lock
        except TypeError:  # not weak-referenceable: share one lock
            return _FALLBACK_PROMPT_LOCK


def _one_at_a_time(
    ask: Callable[..., Any] | None, parent: Any, child: list[Any]
) -> Callable[..., Any] | None:
    """`ask` (a parent's permission callback) serialized per parent. An answer
    that turned the parent's `/auto on` on ("allow all this session") covers
    the subagent (`child[0]`) from then on too."""
    if ask is None:
        return None
    lock = _prompt_lock(parent)

    def serialized(*args: Any) -> Any:
        with lock:
            answer = ask(*args)
        if getattr(parent, "auto_all", False) and child and not getattr(child[0], "auto_all", True):
            child[0].enable_auto_all()
        return answer

    return serialized


def run_subagent(
    *,
    system_prompt: str,
    task: str,
    tool_categories: Iterable[str] = (),
    max_turns: int = 6,
    hooks: HookRegistry | None | object = INHERIT,
    cwd: str | None = None,
    skill_scope: skillscope.Scope | None | object = INHERIT,
    tools: Iterable[str] | None = None,
    unattended_policy: str | None = None,
    memory: MemoryStore | None = None,
    isolate: str | None = None,
    worktree: Any = None,
) -> str:
    """Run a one-shot scoped Agent and return its final assistant text.

    Pulls the same LLM client/config the parent uses. Its tools are those
    whose category (or name) is in `tool_categories`, cut to what the
    ``[tools]`` toggles enable, or exactly the registered tools named in
    `tools` when given (`select_tools`).

    `hooks` defaults to the tool hooks of the spawning agent
    (`evi.hooks.active()`); pass a registry (used whole, lifecycle hooks
    included), or None for no hooks. `cwd` defaults to the caller's session
    working folder ("" when there is none: the process cwd). `skill_scope`
    defaults to the spawning agent's active skill scope; the subagent's own
    turns can narrow it but never clear it. `memory` is the store its memory
    tools work on (default: the spawning session's, else ~/.evi/memory).

    Permission prompts go through the spawning agent's callbacks, one at a
    time. With no parent UI the run is unattended: a call that would ask
    follows `unattended_policy` ("deny" | "allow"; None: the parent's, else
    ``[auto] unattended_policy``, deny by default), and a delegate CLI-agent
    backend is refused unless ``[llm] allow_cli_agent_unattended`` is set.
    Only pass "allow" with a pinned read-only `tools` list.

    `isolate="worktree"` runs it in a fresh git worktree (`fix-<task>`) of the
    repository at its working folder instead (`_open_worktree`), locked
    while it runs, and appends a ``[worktree]`` line saying where the work
    is; nothing is merged. `worktree` (an `evi.worktree.IsolatedWorktree`
    the caller opened, and releases: a team task's) runs it in that one.
    Either way its folder is the worktree's, and the tools that work in the
    process's folder instead (`evi.worktree.confined_tools`) are left out.
    """
    from evi.llm.cli_agent import CliAgentUnattended, unattended
    from evi.sdk.builder import build_agent

    if hooks is INHERIT:
        parent_hooks = hooks_mod.active()
        hooks = parent_hooks.tool_hooks() if parent_hooks is not None else None
    if skill_scope is INHERIT:
        skill_scope = skillscope.inherited()
    # The agent whose tool call spawned this one, if any: its permission UI
    # (or its unattended policy) is the subagent's.
    ctx = active_context()
    parent = ctx.agent if ctx is not None else None
    child: list[Any] = []  # the subagent, once built (_one_at_a_time)
    permission_callback = _one_at_a_time(
        getattr(parent, "permission_callback", None), parent, child
    )
    permission_batch_callback = _one_at_a_time(
        getattr(parent, "permission_batch_callback", None), parent, child
    )
    attended = permission_callback is not None or permission_batch_callback is not None

    config = Config.load()
    if isolate and worktree is not None:
        raise ValueError("pass isolate or worktree, not both")
    iso = None
    if isolate:
        from evi.worktree import WorktreeError

        try:
            iso = _open_worktree(isolate, task, cwd, config)
        except WorktreeError as exc:
            return f"ERROR: subagent failed: worktree isolation: {exc}"
    held = iso if iso is not None else worktree
    if held is not None:
        cwd = str(held.path)
    text_parts: list[str] = []
    tool_trace: list[str] = []
    error: str | None = None
    # The trace is cut short, so it is redacted first: a key straddling the
    # cut would leave a prefix no later redaction (a display's) can match.
    red = Redactor()
    try:
        # Nobody can approve anything a delegate CLI agent's own tools do.
        gate = contextlib.nullcontext() if attended else unattended("subagent", config)
        with gate:
            agent = build_agent(
                config=config,
                client=make_client(config.llm),
                tools=_held_tools(
                    select_tools(config, tool_categories=tool_categories, tools=tools), held,
                ),
                system_prompt=system_prompt,
                enable_memory=False,
                enable_skills=False,
                enable_project=False,
                enable_hooks=False,
                enable_guardrails=False,
                permission_callback=permission_callback,
                permission_batch_callback=permission_batch_callback,
                unattended_policy=(
                    unattended_policy if unattended_policy is not None
                    else getattr(parent, "unattended_policy", None)
                ),
            )
            child.append(agent)
            agent.hooks = hooks  # type: ignore[assignment]
            # The store its memory tools use (_store_for): the one given, else the
            # spawning agent's. Never fall through to the import-time default
            # store while a parent has its own (that wrote into ~/.evi/memory).
            store = memory if memory is not None else getattr(parent, "memory", None)
            if isinstance(store, MemoryStore):
                agent.memory = store
            if getattr(parent, "auto_all", False):
                agent.enable_auto_all()  # the user's `/auto on` covers the session
            # A remote trigger's run stays one: no desk-user yolo / accept_edits.
            agent.remote_trigger = bool(getattr(parent, "remote_trigger", False))
            # Same folder as the parent: tools resolve against it, and so do the
            # permission checks (protected paths, trusted dirs) that read agent.cwd.
            agent.cwd = workdir.session_cwd() if cwd is None else cwd
            agent.inherited_skill_scope = skill_scope  # type: ignore[assignment]

            for event in agent.chat(task, max_turns=max_turns):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                elif isinstance(event, ToolResult):
                    # Keep a short trace so the caller can see what the sub-agent did.
                    preview = red(event.output)[:200].replace("\n", " ")
                    tool_trace.append(f"{event.name}: {preview}")
                elif isinstance(event, Error):
                    error = event.message
                    break
                elif isinstance(event, Done):
                    break
    except CliAgentUnattended as exc:
        error = str(exc)
    finally:
        where = ""
        if iso is not None:
            from evi import worktree as wt

            try:  # what it left there, then the run's lock comes off
                where = f"\n\n[worktree] {wt.summary_line(wt.worktree_report(iso))}"
            finally:
                wt.release(iso)
    if error:
        return f"ERROR: subagent failed: {error}{where}"

    result = "".join(text_parts).strip()
    if not result:
        result = "(subagent produced no text)"
    if tool_trace:
        trace = "\n".join(f"  - {t}" for t in tool_trace)
        result = f"{result}\n\n[trace]\n{trace}"
    return result + where


def _held_tools(tools: list[Tool], held: Any) -> list[Tool]:
    """A subagent's tools, without the ones that work in the process's folder
    when it is held to a worktree (`evi.worktree.confined_tools`)."""
    if held is None:
        return tools
    from evi.worktree import confined_tools

    return confined_tools(tools)


def _open_worktree(isolate: str, task: str, cwd: str | None, config: Config):
    """The worktree a `run_subagent(isolate=...)` works in: a fresh
    `fix-<task>` branch of the repository at its working folder (`cwd`, else
    the session's, else the process's), from HEAD. Raises WorktreeError for
    an unknown mode, a folder outside git, or a backend that can't be held to
    the worktree (evi.worktree.check_thread_isolation)."""
    from evi import worktree as wt

    if isolate not in ISOLATE_MODES:
        raise wt.WorktreeError(
            f"unknown isolate {isolate!r} (only {', '.join(ISOLATE_MODES)})"
        )
    wt.check_thread_isolation(config)
    start = Path(cwd or workdir.session_cwd() or os.getcwd())
    return wt.open_isolated(wt.AUTO, task=task, start=start)


def run_subagents_parallel(
    tasks: list[str],
    *,
    system_prompt: str,
    tool_categories: Iterable[str] = (),
    max_turns: int = 6,
    max_workers: int = 4,
    hooks: HookRegistry | None | object = INHERIT,
    cwd: str | None = None,
    skill_scope: skillscope.Scope | None | object = INHERIT,
    tools: Iterable[str] | None = None,
    unattended_policy: str | None = None,
    isolate: str | None = None,
) -> list[tuple[str, str]]:
    """Run several subagents concurrently and return [(task, result), …] in the
    original order.

    Each task gets its own scoped Agent via `run_subagent`. Wall-clock wins come
    from overlapping the orchestration + tool calls; note that a single local
    backend serialises the actual model inference (one model, one GPU), so the
    big speedups are on tool-heavy work or a remote / multi-GPU backend.
    `hooks` / `cwd` / `skill_scope` / `tools` / `unattended_policy` /
    `isolate` are as for `run_subagent` (isolated, each task gets a worktree
    of its own); each worker starts from a copy of the caller's context, so
    the defaults (and the permission UI) inherit from the caller.
    """
    import concurrent.futures as _futures

    if not tasks:
        return []
    results: list[tuple[str, str]] = [(t, "") for t in tasks]
    workers = min(max_workers, len(tasks)) or 1
    with _futures.ThreadPoolExecutor(max_workers=workers) as ex:
        # A fresh context copy per task: one Context can't be entered by two
        # threads at once, and pool threads otherwise start with an empty one.
        fut_to_i = {
            ex.submit(
                contextvars.copy_context().run,
                run_subagent,
                system_prompt=system_prompt,
                task=task,
                tool_categories=tool_categories,
                max_turns=max_turns,
                hooks=hooks,
                cwd=cwd,
                skill_scope=skill_scope,
                # Only when set, so a stand-in runner without them still works.
                **({"tools": tools} if tools is not None else {}),
                **(
                    {"unattended_policy": unattended_policy}
                    if unattended_policy is not None else {}
                ),
                **({"isolate": isolate} if isolate else {}),
            ): i
            for i, task in enumerate(tasks)
        }
        for fut in _futures.as_completed(fut_to_i):
            i = fut_to_i[fut]
            try:
                results[i] = (tasks[i], fut.result())
            except Exception as exc:  # noqa: BLE001
                results[i] = (tasks[i], f"ERROR: {type(exc).__name__}: {exc}")
    return results


# Pre-baked subagent personalities. New ones can be added without changing
# the tool-layer dispatch — see evi/tools/subagent.py.
SUBAGENT_PROFILES: dict[str, dict[str, object]] = {
    "explore": {
        "system_prompt": (
            "You are an Explore subagent. Your job is to investigate a "
            "codebase or filesystem and report findings concisely. You may "
            "use read-only filesystem tools. Do not modify anything. End "
            "with a short bulleted summary of what you found."
        ),
        # Tool names, not the fs category (which can write): read-only for real.
        "tool_categories": EXPLORE_TOOLS,
    },
    "plan": {
        "system_prompt": (
            "You are a Plan subagent. Given a task, produce a step-by-step "
            "implementation plan as a numbered list. Identify critical files, "
            "trade-offs, and risks. Do not write code. Do not call tools."
        ),
        "tool_categories": (),
    },
}


def load_plugin_profiles(root: Path | None = None) -> dict[str, dict[str, object]]:
    """Subagent profiles bundled by installed plugins, from
    ``<plugin>/agents.toml``. Each profile is namespaced ``<plugin>:<name>`` so
    it can't shadow a built-in. Malformed files/entries are skipped (an
    unknown ``isolate`` too: that profile would run unisolated).

        [[agent]]
        name = "security"
        system_prompt = "You are a security reviewer…"
        tools = ["fs"]          # tool categories (or tool names) the subagent may use
        isolate = "worktree"    # optional: work in a git worktree of its own

    Whatever a profile lists, the subagent only gets tools the ``[tools]``
    toggles enable (`select_tools`).
    """
    out: dict[str, dict[str, object]] = {}
    try:
        from evi.plugins import plugin_dirs

        for pd in plugin_dirs(root):
            f = pd / "agents.toml"
            if not f.is_file():
                continue
            try:
                data = tomllib.loads(f.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            for entry in data.get("agent", []) or []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                sp = str(entry.get("system_prompt") or "").strip()
                if not name or not sp:
                    continue
                raw = entry.get("tools", entry.get("tool_categories", [])) or []
                cats = tuple(str(c) for c in raw) if isinstance(raw, list) else ()
                profile = _profile(sp, cats, entry, where=f"{pd.name}:{name}")
                if profile is not None:
                    out[f"{pd.name}:{name}"] = profile
    except Exception:  # plugin scanning must never break core subagents
        pass
    return out


def _user_profiles_path():
    from evi.config import AGENTS_CONFIG_PATH

    return AGENTS_CONFIG_PATH


def load_user_profiles(path: Path | None = None) -> dict[str, dict[str, object]]:
    """User-defined subagent profiles from ``~/.evi/agents.toml`` (same
    ``[[agent]]`` schema as a plugin's agents.toml, ``isolate`` included, but
    referenced by bare name). Missing file = {}; malformed file/entries are
    skipped (an unknown ``isolate`` with a warning)."""
    return _read_user_profiles(path or _user_profiles_path())


def _read_user_profiles(f: Path, *, keep_unknown_isolate: bool = False) -> dict[str, dict[str, object]]:
    """`load_user_profiles`; with `keep_unknown_isolate` a profile whose
    ``isolate`` eVi doesn't know is kept too, with that value as written (so
    `add_user_profile` writes it back rather than drop it)."""
    out: dict[str, dict[str, object]] = {}
    try:
        if not f.is_file():
            return out
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return out
    for entry in data.get("agent", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        sp = str(entry.get("system_prompt") or "").strip()
        if not name or not sp:
            continue
        raw = entry.get("tools", entry.get("tool_categories", [])) or []
        cats = tuple(str(c) for c in raw) if isinstance(raw, list) else ()
        profile = _profile(sp, cats, entry, where=name, keep_unknown_isolate=keep_unknown_isolate)
        if profile is not None:
            out[name] = profile
    return out


def _profile(
    system_prompt: str, cats: tuple[str, ...], entry: dict, *, where: str = "",
    keep_unknown_isolate: bool = False,
) -> dict[str, object] | None:
    """A profile from an ``[[agent]]`` entry; None (skipped, with a warning)
    when its ``isolate`` is not one eVi knows: run unisolated, it could edit
    the very checkout it was meant to keep away from. `keep_unknown_isolate`
    keeps it, with that value as written, for writing the file back."""
    profile: dict[str, object] = {"system_prompt": system_prompt, "tool_categories": cats}
    written = str(entry.get("isolate") or "").strip()
    isolate = written.lower()
    if isolate:
        if isolate not in ISOLATE_MODES:
            if keep_unknown_isolate:
                profile["isolate"] = written
                return profile
            logger.warning(
                "subagent profile %r skipped: isolate = %r is not one of %s",
                where, written, ", ".join(ISOLATE_MODES),
            )
            return None
        profile["isolate"] = isolate
    return profile


def add_user_profile(
    name: str,
    system_prompt: str,
    tool_categories: tuple[str, ...] = (),
    *,
    path: Path | None = None,
    overwrite: bool = False,
    isolate: str = "",
) -> Path:
    """Append (or overwrite) a profile in ``~/.evi/agents.toml`` and return the
    file path. Raises ValueError on a bad name, an unknown ``isolate`` or an
    existing name without ``overwrite``. Used by ``evi agents new``. The other
    profiles are written back as they were, ``isolate`` included: also one eVi
    doesn't know, so that profile stays (skipped until it is fixed) rather
    than vanish."""
    slug = name.strip()
    if not slug or ":" in slug or any(c.isspace() for c in slug):
        raise ValueError("agent name must be non-empty with no spaces or ':'")
    if slug in SUBAGENT_PROFILES:
        raise ValueError(f"'{slug}' is a built-in profile; pick another name")
    isolate = isolate.strip().lower()
    if isolate and isolate not in ISOLATE_MODES:
        raise ValueError(f"isolate must be one of: {', '.join(ISOLATE_MODES)}")
    f = path or _user_profiles_path()
    existing = _read_user_profiles(f, keep_unknown_isolate=True)
    if slug in existing and not overwrite:
        raise ValueError(f"profile '{slug}' already exists (use --force to overwrite)")
    existing[slug] = {
        "system_prompt": system_prompt.strip(),
        "tool_categories": tuple(tool_categories),
        **({"isolate": isolate} if isolate else {}),
    }
    lines: list[str] = [
        "# eVi user subagent profiles. Reference one with the `delegate` tool",
        "# (delegate(profile=\"<name>\", task=...)) or `evi agents`.",
        "",
    ]
    for nm, prof in existing.items():
        cats = list(prof.get("tool_categories") or ())
        sp = str(prof.get("system_prompt") or "")
        lines.append("[[agent]]")
        lines.append(f"name = {_toml_str(nm)}")
        lines.append(f"system_prompt = {_toml_str(sp)}")
        lines.append(f"tools = {_toml_str_list(cats)}")
        if prof.get("isolate"):
            lines.append(f"isolate = {_toml_str(str(prof['isolate']))}")
        lines.append("")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def _toml_str(s: str) -> str:
    """A TOML basic string with the few characters that need escaping handled."""
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{esc}"'


def _toml_str_list(items: list[str]) -> str:
    return "[" + ", ".join(_toml_str(i) for i in items) + "]"


def all_profiles(root: Path | None = None) -> dict[str, dict[str, object]]:
    """Built-in profiles + user-defined (~/.evi/agents.toml) + plugin-supplied.
    Built-ins always win; user profiles win over plugin ones."""
    merged: dict[str, dict[str, object]] = dict(SUBAGENT_PROFILES)
    for k, v in load_user_profiles().items():
        merged.setdefault(k, v)
    for k, v in load_plugin_profiles(root).items():
        merged.setdefault(k, v)
    return merged


def get_profile(name: str, root: Path | None = None) -> dict[str, object] | None:
    return all_profiles(root).get(name)
