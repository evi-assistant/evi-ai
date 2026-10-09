"""Project context — auto-loaded `EVI.md` from the current working tree.

Mirrors how Claude Code uses `CLAUDE.md`: a markdown file checked into the
project that gives the agent durable, project-specific context (coding
conventions, where things live, terminology). Loaded from the nearest
ancestor of `cwd` that contains one, so you get sensible per-project
behavior just by `cd`ing into the right directory.

The file is read once at agent construction. If you edit `EVI.md` in a
long-running session, `Agent.reset()` re-reads it.

Size cap: 64 KB. Large project docs should be split and referenced; the
goal is "always-on context", not "stuff every README into every turn".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

logger = logging.getLogger(__name__)


# EVI.md is eVi's own; AGENTS.md is the emerging cross-tool standard. EVI.md
# wins when both exist in the same directory.
PROJECT_FILENAMES = ("EVI.md", "evi.md", "AGENTS.md", "agents.md")
PROJECT_CONFIG_FILENAME = ".evi.toml"  # per-project config overlay
_MAX_BYTES = 64 * 1024
# A real .evi.toml is a few hundred bytes; a bigger one is ignored rather than
# parsed on every Config.load.
_MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectContext:
    path: Path
    content: str

    def format_for_prompt(self) -> str:
        """Render as a markdown block to append to the system prompt."""
        return f"## Project context (`{self.path}`)\n\n{self.content.strip()}\n"


def find_project_file(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for EVI.md.

    Returns the first match, or None. Stops at the filesystem root.
    """
    cur: Path | None = (start or Path.cwd()).resolve()
    while cur is not None:
        for name in PROJECT_FILENAMES:
            candidate = cur / name
            if candidate.is_file():
                return candidate
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def find_project_files(start: Path | None = None) -> list[Path]:
    """Every project file from the filesystem root down to `start` (outermost
    first, nearest last), one per directory. Enables monorepo-style layered
    context: a repo-root EVI.md plus a package-level one."""
    cur: Path | None = (start or Path.cwd()).resolve()
    found: list[Path] = []
    while cur is not None:
        for name in PROJECT_FILENAMES:
            candidate = cur / name
            if candidate.is_file():
                found.append(candidate)
                break  # one per directory
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    found.reverse()  # root → nearest, so the most-specific context comes last
    return found


def _find_anatomy(
    project_files: list[Path], start: Path | None
) -> tuple[Path, str] | None:
    """Locate a generated `.evi/anatomy.md` near the project: at the root of the
    outermost project file if any, else at `start`/cwd. Returns (path, text)."""
    from evi.anatomy import anatomy_path

    roots: list[Path] = []
    if project_files:
        roots.append(project_files[0].parent)  # outermost (repo root)
    roots.append((start or Path.cwd()))
    seen: set[Path] = set()
    for root in roots:
        p = anatomy_path(root)
        if p in seen:
            continue
        seen.add(p)
        try:
            if p.is_file():
                return (p, p.read_text(encoding="utf-8"))
        except OSError:
            continue
    return None


def load_project_context(start: Path | None = None) -> ProjectContext | None:
    """Aggregate project files from root → cwd into one context.

    A single file is returned verbatim (backwards compatible). Multiple levels
    are concatenated with per-file headers, outermost first, capped at
    `_MAX_BYTES` total — partial context beats none.

    Safe mode returns None. Gating here (not just at Agent construction) also
    covers the mid-session reloads — `/cd`, `--cwd`, and the web workdir change
    — which would otherwise re-inject EVI.md into a clean-boot session."""
    from evi import safemode

    if safemode.enabled():
        return None
    files = find_project_files(start)
    sections: list[tuple[Path, str]] = []
    for path in files:
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sections.append((path, text))

    # Project anatomy map (.evi/anatomy.md), if it's been generated — gives the
    # model a file/token map so it stops blind full-file reads. Opt-in by virtue
    # of existing (created via `evi anatomy --write`); appended last so the cap
    # truncates the (regenerable) map before any authored context.
    anatomy = _find_anatomy(files, start)
    if anatomy is not None:
        sections.append(anatomy)

    if not sections:
        return None

    nearest = sections[-1][0]
    if len(sections) == 1:
        content = sections[0][1]
    else:
        content = "\n\n".join(f"### {p}\n\n{t.strip()}" for p, t in sections)
    if len(content.encode("utf-8")) > _MAX_BYTES:
        marker = "\n…(project context truncated)"
        budget = _MAX_BYTES - len(marker.encode("utf-8"))
        content = content.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + marker
    return ProjectContext(path=nearest, content=content)


# --- per-project config overlay (Phase 74) ------------------------------


def find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) for a `.evi.toml` project config."""
    cur: Path | None = (start or Path.cwd()).resolve()
    while cur is not None:
        candidate = cur / PROJECT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def load_project_config_overlay(start: Path | None = None) -> dict:
    """Return the project `.evi.toml` parsed to a dict (empty if none).

    This is the RAW, untrusted file. Config.load passes it through
    merge_project_overlay (allowlist + tighten-only) before it touches the
    user config — never merge it directly.

    Never raises. Callers such as the run_python sandbox check and the MCP
    destructive-command guard treat a Config.load error as "setting off", so a
    repo file that made Config.load raise would switch the user's OWN
    protections off. A file that can't be read or parsed (invalid UTF-8,
    nesting deep enough to exhaust the stack, oversized) is ignored as a
    whole — fail closed — and reported once."""
    try:
        path = find_project_config(start)
    except OSError:
        return {}
    if path is None:
        return {}
    try:
        with path.open("rb") as f:
            raw = f.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) <= _MAX_CONFIG_BYTES:
            data = tomllib.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        reason = f"larger than {_MAX_CONFIG_BYTES // 1024} KB"
    except OSError:
        reason = "could not be read"
    except (ValueError, RecursionError, MemoryError):
        # ValueError covers TOMLDecodeError and UnicodeDecodeError.
        reason = "not valid UTF-8 TOML, or nested too deeply"
    warn_ignored_project_keys([f"the whole file ({reason})"], path)
    return {}


# --- project config trust policy (1.0.53 security hotfix) ---------------
#
# A `.evi.toml` ships with whatever repository you cloned, so it is UNTRUSTED
# input. Merged verbatim (as before 1.0.53) it could set [statusline] command
# (run through the shell on every REPL prompt), [llm] base_url / api_key /
# backend (ship your prompts, files and keys to another host), [auto] mode =
# "yolo", [tools] shell = true, [runtime] server_path (an executable eVi
# launches), and so on.
#
# So a project overlay is filtered through an explicit ALLOWLIST before it is
# merged — fail closed: any section or key not named below (including every
# field added to Config in future) is ignored and reported once. The user's
# own profile overlay (~/.evi/profiles/, user-owned) is trusted and NOT
# filtered.

# Benign per-repo preferences, applied as-is (the project value wins). Only
# model-behaviour knobs: nothing here is a URL / host / port, credential, path,
# command or listener, or picks which backend receives your data. Deliberately
# absent from [llm]: backend, base_url, api_key, api, responses_tools
# (server-side tools executed by the model host); and fast_mode, fast_model,
# fallback_models, router_enabled, router_model, request_timeout — each lets
# the turn run on a different model (or deadline) from the [[judge]]
# guardrail, which uses `model` and fails OPEN on any error, so a repo could
# switch the user's guardrails off while chat kept working. Every other
# section — statusline, runtime, web, federation, channels, notify,
# telemetry, plugins, desktop, google, microsoft, obsidian, comfy, voice,
# models, ultracode — is ignored outright.
_PROJECT_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "llm": frozenset({
        "model", "embed_model", "embed_dimensions",
        "temperature", "top_p", "presence_penalty", "frequency_penalty", "seed",
        "stop_sequences", "logit_bias", "max_tokens", "max_completion_tokens",
        "context_size", "compact_after_messages", "compact_keep_recent",
        "compact_when_pct", "reasoning_effort", "parallel_tool_calls",
        "cache_prompt", "logprobs", "top_logprobs", "output_style",
    }),
    "worktree": frozenset({"base_ref"}),
}

# Allowed values that are NAMES eVi hands to another program: model ids become
# `codex -m <model>` / `gemini -m <model>` argv (often through a Windows .cmd
# shim that cmd.exe re-parses) and base_ref becomes a `git worktree add` arg.
# They must look like a plain name — no leading "-" (option injection, e.g.
# "--yolo") and no quotes / shell metacharacters. Single spaces are allowed
# inside a name (the managed runtime's catalog names are "Qwen2.5 7B
# Instruct", ...), but not around it and never before a "-", so no word of
# the name can be read as an option even if it gets split on spaces.
_NAME_KEYS = frozenset({
    ("llm", "model"), ("llm", "embed_model"), ("llm", "output_style"),
    ("worktree", "base_ref"),
})
_SAFE_NAME = re.compile(
    r"[A-Za-z0-9_.:/@+](?:[A-Za-z0-9_.:/@+-]| (?=[A-Za-z0-9_.:/@+])){0,199}"
)
# base_ref may also be a revspec (HEAD~1, main^). git gets it as a plain argv
# entry (evi/worktree.py, no shell), so the leading-"-" guard is what matters.
_SAFE_REF = re.compile(r"[A-Za-z0-9_.:/@+][A-Za-z0-9_.:/@+~^-]{0,199}")

# reasoning_effort is an enum, not free text: it is exported to hooks as
# $EVI_EFFORT (which a cmd.exe hook re-parses) and printed by /reload. These
# are the levels /effort accepts plus the "none" alias of evi/reasoning.py.
_EFFORT_LEVELS = frozenset({"off", "none", "low", "medium", "high", "max"})

# stop_sequences go to the backend; keep them few and short. Newlines and tabs
# are normal in a stop sequence, other control characters are not.
_MAX_STOP_SEQUENCES = 16
_MAX_STOP_LEN = 256

# [llm] numbers must lie in these inclusive ranges. TOML allows nan / inf and
# arbitrarily long (hex) integers; accepted as-is they broke `evi config show`,
# Config.save and every request body carrying them. A plain comparison also
# rejects nan and never overflows. A number key missing here is rejected.
_I32_MAX = 2**31 - 1
_NUMBER_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "presence_penalty": (-2.0, 2.0),
    "frequency_penalty": (-2.0, 2.0),
    "seed": (-(2**63), 2**63 - 1),
    "max_tokens": (1, _I32_MAX),
    "max_completion_tokens": (0, _I32_MAX),  # 0 = unset
    "context_size": (0, _I32_MAX),  # 0 = unknown
    "compact_after_messages": (0, _I32_MAX),  # 0 = off
    "compact_keep_recent": (0, _I32_MAX),
    "compact_when_pct": (1, 100),
    "embed_dimensions": (1, 65536),
    "top_logprobs": (0, 20),
}

# [tools]: a repo may only switch capabilities OFF (`<category> = false`;
# `true` is ignored)...
_TOOLS_OFF_ONLY = frozenset({
    "fs", "code", "shell", "gmail", "outlook", "image", "memory", "subagent",
    "mcp", "skills", "web", "voice", "computer", "pdf", "sqlite", "index",
    "git", "federation", "ocr", "calendar", "ask", "vision",
    "format_on_edit", "check_on_edit",  # these run repo-configured formatters
})
# ...and the run_python / run_command OS sandbox ON (its safe direction is
# `true`). Every other [tools] key — transcripts (not a tool: turning it off
# would stop eVi recording what happened in the repo), mcp_allow,
# tool_search*, mcp_max_output_chars, cleanup_period_days (deletes
# transcripts), search_backend, searxng_url — is ignored.
_TOOLS_ON_ONLY = frozenset({"sandbox"})

# [auto]: only these permission keys, and only in the tightening direction —
# merge_project_overlay combines them with the user's own values:
#   mode              honoured only when STRICTER than the user's mode
#   rules             only `deny ...` rules, evaluated before the user's rules
#                     (rules are skipped in yolo — hard_deny is what holds in
#                     every mode)
#   hard_deny         unioned with the user's list (never replaced / shrunk)
#   protected_paths   unioned with the user's list (never replaced / shrunk)
#   block_destructive honoured only when true
# auto_approve, trusted_dirs, trusted_domains, destructive_allow,
# destructive_disable_rules, ultracode and anything else in [auto] is ignored.
_AUTO_LIST_KEYS = ("rules", "hard_deny", "protected_paths")

# Permission modes, loosest → strictest (see evi/permissions.py: yolo allows
# everything, accept_edits auto-allows file edits, ask prompts, plan denies
# every tool).
_MODE_STRICTNESS = ("yolo", "accept_edits", "ask", "plan")

# (path or cwd, ignored keys) already reported. Config.load runs on every web
# request, subagent and scheduler tick, so each finding is logged once per
# process.
_WARNED: set[tuple[str, tuple[str, ...]]] = set()
_MAX_LISTED = 20  # ignored keys named in one warning; the rest are counted

# Key names printed verbatim in the warning. TOML quoted keys can hold ANY
# character (terminal escape sequences, CR/LF), so anything else is shown as
# its escaped repr — a repo must not be able to rewrite or forge the warning.
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _key_label(*parts: Any) -> str:
    """Dotted key name that is safe to print (see _PLAIN_KEY)."""
    out = []
    for part in map(str, parts):
        if _PLAIN_KEY.fullmatch(part):
            out.append(part)
        else:
            out.append(repr(part[:40]) + ("..." if len(part) > 40 else ""))
    return ".".join(out)


def _printable(text: str) -> str:
    """`text`, or its escaped repr if it has control / format characters."""
    return text if text.isprintable() else repr(text)


def _plain_text(value: Any, allowed: str = "") -> bool:
    """No control / format characters (escape sequences, CR, bidi overrides)
    in a string or list of strings, apart from `allowed`. Project values are
    echoed to the terminal (`evi config show`, /reload, the REPL prompt)."""
    for s in value if isinstance(value, list) else [value]:
        if isinstance(s, str) and not s.isprintable():
            if not all(c.isprintable() or c in allowed for c in s):
                return False
    return True


def _valid_logit_bias(raw: str) -> bool:
    """`logit_bias` is a JSON object of token id → number in [-100, 100] (the
    range LLMSettings clamps to), or empty."""
    if not raw.strip():
        return True
    if len(raw) > 16 * 1024:
        return False
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    # A comparison, not math.isfinite (OverflowError on a huge int); it also
    # rejects nan and inf.
    return isinstance(parsed, dict) and all(
        k.isascii() and k.isdigit()
        and isinstance(v, (int, float)) and not isinstance(v, bool)
        and -100 <= v <= 100
        for k, v in parsed.items()
    )


def _valid_value(section: str, key: str, value: Any) -> bool:
    """Content checks for an allowlisted value whose type already matches."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        bounds = _NUMBER_RANGES.get(key)
        return bounds is not None and bounds[0] <= value <= bounds[1]
    if (section, key) in _NAME_KEYS:
        return _safe_names(value, _SAFE_REF if key == "base_ref" else _SAFE_NAME)
    if (section, key) == ("llm", "reasoning_effort"):
        return value.strip().lower() in _EFFORT_LEVELS
    if (section, key) == ("llm", "logit_bias"):
        return _valid_logit_bias(value)
    if (section, key) == ("llm", "stop_sequences"):
        return len(value) <= _MAX_STOP_SEQUENCES and all(
            0 < len(s) <= _MAX_STOP_LEN for s in value
        ) and _plain_text(value, "\t\r\n")
    return _plain_text(value)


def _type_matches(default: Any, value: Any) -> bool:
    """Whether `value` has the TOML type of the field's `default`."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return isinstance(default, float) or isinstance(value, int)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return False


def _safe_names(value: Any, pattern: re.Pattern[str] = _SAFE_NAME) -> bool:
    names = value if isinstance(value, list) else [value]
    return all(n == "" or pattern.fullmatch(n) for n in names)


def _is_deny_rule(rule: str) -> bool:
    parts = rule.split(None, 2)
    return len(parts) >= 2 and parts[0].lower() == "deny"


def filter_project_overlay(overlay: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split a raw project `.evi.toml` dict into (safe_overlay, ignored_keys).

    `safe_overlay` keeps only allowlisted keys, the [tools] toggles that
    tighten, and the [auto] keys that can only tighten (still to be combined
    with the user's values — see merge_project_overlay). `ignored_keys` are
    sorted dotted names such as "llm.base_url" or "statusline.command", safe
    to print (odd key names are shown escaped). Pure."""
    from evi.config import LLMSettings, WorktreeSettings

    defaults = {"llm": LLMSettings(), "worktree": WorktreeSettings()}
    safe: dict[str, Any] = {}
    ignored: list[str] = []
    for section, body in overlay.items():
        if not isinstance(body, dict):
            ignored.append(_key_label(section))
            continue
        kept: dict[str, Any] = {}
        for key, value in body.items():
            name = _key_label(section, key)
            if section in _PROJECT_ALLOWED_KEYS:
                allowed = key in _PROJECT_ALLOWED_KEYS[section]
                ok = (
                    allowed
                    and _type_matches(getattr(defaults[section], key), value)
                    and _valid_value(section, key, value)
                )
                if allowed and not ok:
                    # A key a repo MAY set — say it was the value that failed.
                    name += " (invalid value)"
                if ok and (section, key) == ("llm", "reasoning_effort"):
                    value = value.strip().lower()
            elif section == "tools":
                ok = (value is False and key in _TOOLS_OFF_ONLY) or (
                    value is True and key in _TOOLS_ON_ONLY
                )
            elif section == "auto" and key in _AUTO_LIST_KEYS:
                ok = isinstance(value, list) and all(
                    isinstance(v, str) and v.isprintable() for v in value
                )
                if ok:
                    value = [v for v in value if v.strip()]
                    if key == "rules":
                        denies = [r for r in value if _is_deny_rule(r)]
                        if denies and len(denies) < len(value):
                            # The deny entries still apply; say what didn't.
                            ignored.append(f"{name} (non-deny entries)")
                        value = denies
                    # Empty = an attempt to CLEAR the user's list; refused.
                    ok = bool(value)
            elif section == "auto" and key == "mode":
                ok = value in _MODE_STRICTNESS
            elif section == "auto" and key == "block_destructive":
                ok = value is True
            else:
                ok = False
            if ok:
                kept[key] = value
            else:
                ignored.append(name)
        if kept:
            safe[section] = kept
    return safe, sorted(set(ignored))


def merge_project_overlay(
    base: dict[str, Any], overlay: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Apply a raw project overlay on top of `base` (user config + profile).

    Filters it with filter_project_overlay, merges the allowlisted keys as
    usual (project wins), and combines the [auto] keys tighten-only against
    `base`'s effective values (the AutoSettings defaults where `base` doesn't
    set them, so e.g. the default protected_paths are never lost). Returns
    (merged, ignored_keys).

    Never raises: if anything goes wrong the whole overlay is dropped and
    `base` is returned unchanged (see load_project_config_overlay for why a
    Config.load error must not be possible from a repo file)."""
    try:
        return _merge_project_overlay(base, overlay)
    except Exception:  # noqa: BLE001 — fail closed: keep the user's config
        logger.debug("project config overlay could not be applied", exc_info=True)
        return base, ["the whole file (could not be applied)"]


def _merge_project_overlay(
    base: dict[str, Any], overlay: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    from evi.config import AutoSettings
    from evi.profiles import merge_overlay

    safe, ignored = filter_project_overlay(overlay)
    project_auto = safe.pop("auto", {})
    merged = merge_overlay(base, safe)
    if not project_auto:
        return merged, ignored

    cur = merged.get("auto")
    auto: dict[str, Any] = dict(cur) if isinstance(cur, dict) else {}
    defaults = AutoSettings()

    def _mine(key: str) -> list:
        value = auto.get(key, getattr(defaults, key))
        return list(value) if isinstance(value, list) else []

    if "mode" in project_auto:
        rank = {m: i for i, m in enumerate(_MODE_STRICTNESS)}
        # An unrecognised user mode behaves like "ask" in permissions.decide.
        mine = rank.get(auto.get("mode", defaults.mode), rank["ask"])
        theirs = rank[project_auto["mode"]]
        if theirs > mine:
            auto["mode"] = project_auto["mode"]
        elif theirs < mine:
            ignored = sorted({*ignored, "auto.mode"})
    for key in ("hard_deny", "protected_paths"):
        if key in project_auto:
            mine = _mine(key)
            auto[key] = mine + [v for v in project_auto[key] if v not in mine]
    if "rules" in project_auto:
        # Rules are first-match, so an appended deny would be shadowed by a
        # broader user allow ("allow shell *"). The project's denies go first;
        # a deny can only ever turn an allow/ask into a deny.
        theirs = project_auto["rules"]
        auto["rules"] = theirs + [r for r in _mine("rules") if r not in theirs]
    if project_auto.get("block_destructive") is True:
        auto["block_destructive"] = True
    merged["auto"] = auto
    return merged, ignored


def warn_ignored_project_keys(ignored: list[str], path: Path | None = None) -> None:
    """Log (once per process per file + key set) which `.evi.toml` keys were
    ignored. Goes through `logging`, so the CLI prints it to stderr.

    Config.load calls this on every load without a `path`, so the dedupe is
    keyed on the working directory and checked BEFORE locating the file —
    find_project_config stats every ancestor directory, which is slow on a
    network share."""
    if not ignored:
        return
    if path is not None:
        where_key = str(path)
    else:
        try:
            where_key = "cwd:" + os.getcwd()
        except OSError:
            where_key = "cwd:?"
    key = (where_key, tuple(ignored))
    if key in _WARNED:
        return
    _WARNED.add(key)
    if path is None:
        try:
            path = find_project_config()
        except OSError:
            path = None
    listed = ", ".join(_printable(k) for k in ignored[:_MAX_LISTED])
    if len(ignored) > _MAX_LISTED:
        listed += f" (+{len(ignored) - _MAX_LISTED} more)"
    logger.warning(
        "eVi: ignoring these settings from project config %s: %s. A repo's "
        ".evi.toml may only set model/sampling preferences (to valid values) "
        "and tighten tools/permissions (see 'Per-project config' in the eVi "
        "configuration docs).",
        _printable(str(path)) if path is not None else PROJECT_CONFIG_FILENAME,
        listed,
    )
