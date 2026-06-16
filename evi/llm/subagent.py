"""Subagent runner — spin up a scoped `Agent` for delegated work and drain it.

A subagent shares the same `Agent` class as the main loop but is built with
a focused system prompt and a restricted tool list. Callers hand in a `task`
string, we run the agent to completion, and return the concatenated final
assistant text. Used by `evi.tools.subagent` to back `delegate_explore`,
`delegate_plan`, …
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from evi.config import Config
from evi.llm.agent import Agent, Done, Error, TextDelta, ToolResult
from evi.llm.client import make_client
from evi.tools.base import REGISTRY, Tool


def _tools_in_categories(categories: Iterable[str]) -> list[Tool]:
    allowed = set(categories)
    return [t for t in REGISTRY.values() if t.category in allowed]


def run_subagent(
    *,
    system_prompt: str,
    task: str,
    tool_categories: Iterable[str] = (),
    max_turns: int = 6,
) -> str:
    """Run a one-shot scoped Agent and return its final assistant text.

    Pulls the same LLM client/config the parent uses; respects the parent's
    tool *category* filter but ignores per-tool toggles, so a subagent can
    use read-only filesystem tools even if `fs` is otherwise on.
    """
    config = Config.load()
    client = make_client(config.llm)
    tools = _tools_in_categories(tool_categories)
    agent = Agent(
        client=client,
        config=config,
        tools=tools,
        system_prompt=system_prompt,
    )

    text_parts: list[str] = []
    tool_trace: list[str] = []
    error: str | None = None

    for event in agent.chat(task, max_turns=max_turns):
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolResult):
            # Keep a short trace so the caller can see what the sub-agent did.
            preview = event.output[:200].replace("\n", " ")
            tool_trace.append(f"{event.name}: {preview}")
        elif isinstance(event, Error):
            error = event.message
            break
        elif isinstance(event, Done):
            break

    if error:
        return f"ERROR: subagent failed: {error}"

    result = "".join(text_parts).strip()
    if not result:
        result = "(subagent produced no text)"
    if tool_trace:
        trace = "\n".join(f"  - {t}" for t in tool_trace)
        result = f"{result}\n\n[trace]\n{trace}"
    return result


def run_subagents_parallel(
    tasks: list[str],
    *,
    system_prompt: str,
    tool_categories: Iterable[str] = (),
    max_turns: int = 6,
    max_workers: int = 4,
) -> list[tuple[str, str]]:
    """Run several subagents concurrently and return [(task, result), …] in the
    original order.

    Each task gets its own scoped Agent via `run_subagent`. Wall-clock wins come
    from overlapping the orchestration + tool calls; note that a single local
    backend serialises the actual model inference (one model, one GPU), so the
    big speedups are on tool-heavy work or a remote / multi-GPU backend.
    """
    import concurrent.futures as _futures

    if not tasks:
        return []
    results: list[tuple[str, str]] = [(t, "") for t in tasks]
    workers = min(max_workers, len(tasks)) or 1
    with _futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fut_to_i = {
            ex.submit(
                run_subagent,
                system_prompt=system_prompt,
                task=task,
                tool_categories=tool_categories,
                max_turns=max_turns,
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
        "tool_categories": ("fs",),
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
    it can't shadow a built-in. Malformed files/entries are skipped.

        [[agent]]
        name = "security"
        system_prompt = "You are a security reviewer…"
        tools = ["fs"]          # tool *categories* the subagent may use
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
                out[f"{pd.name}:{name}"] = {"system_prompt": sp, "tool_categories": cats}
    except Exception:  # plugin scanning must never break core subagents
        pass
    return out


def _user_profiles_path():
    from evi.config import AGENTS_CONFIG_PATH

    return AGENTS_CONFIG_PATH


def load_user_profiles(path: Path | None = None) -> dict[str, dict[str, object]]:
    """User-defined subagent profiles from ``~/.evi/agents.toml`` (same
    ``[[agent]]`` schema as a plugin's agents.toml, but referenced by bare name).
    Missing file = {}; malformed file/entries are skipped."""
    f = path or _user_profiles_path()
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
        out[name] = {"system_prompt": sp, "tool_categories": cats}
    return out


def add_user_profile(
    name: str,
    system_prompt: str,
    tool_categories: tuple[str, ...] = (),
    *,
    path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Append (or overwrite) a profile in ``~/.evi/agents.toml`` and return the
    file path. Raises ValueError on a bad name or an existing name without
    ``overwrite``. Used by ``evi agents new``."""
    slug = name.strip()
    if not slug or ":" in slug or any(c.isspace() for c in slug):
        raise ValueError("agent name must be non-empty with no spaces or ':'")
    if slug in SUBAGENT_PROFILES:
        raise ValueError(f"'{slug}' is a built-in profile; pick another name")
    f = path or _user_profiles_path()
    existing = load_user_profiles(f)
    if slug in existing and not overwrite:
        raise ValueError(f"profile '{slug}' already exists (use --force to overwrite)")
    existing[slug] = {
        "system_prompt": system_prompt.strip(),
        "tool_categories": tuple(tool_categories),
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
