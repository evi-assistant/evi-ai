"""Config / authored-resource linter — validate skills, hooks, commands, agents.

`evi doctor` checks the *environment* (home dir, backend, deps, hardware). This
checks the things *you authored* — the resource surface that silently misbehaves
when malformed: a SKILL.md missing its `description` never gets picked by the
model; a typo'd hook event never fires; a command with broken frontmatter is
skipped. All static + offline.

Surfaced via `evi lint`; also the CI gate for the evi-skills repo (point
:func:`lint_path` at a skills checkout). Reuses the existing validators
(`hooks.validate`, `guardrails.validate`, skill frontmatter parsing) so there's
one source of truth per resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# A SKILL.md body much larger than this bloats every turn it's loaded into.
_SKILL_BODY_WARN_CHARS = 8000


@dataclass
class Issue:
    resource: str   # e.g. "skill:pdf-pro", "hooks.toml", "command:deploy"
    level: str      # "error" | "warn"
    message: str


def _lint_skill_md(skill_md: Path, label: str) -> list[Issue]:
    """Validate one SKILL.md: required frontmatter, body size, tool-scope, refs."""
    from evi.skills import _split_frontmatter

    out: list[Issue] = []
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return [Issue(f"skill:{label}", "error", f"unreadable: {exc}")]
    meta, body = _split_frontmatter(text)
    if not meta.get("name", "").strip():
        out.append(Issue(f"skill:{label}", "error", "missing frontmatter 'name'"))
    if not meta.get("description", "").strip():
        out.append(Issue(
            f"skill:{label}", "error",
            "missing frontmatter 'description' — the model can't know when to use it",
        ))
    if len(body) > _SKILL_BODY_WARN_CHARS:
        out.append(Issue(
            f"skill:{label}", "warn",
            f"body is {len(body)} chars (> {_SKILL_BODY_WARN_CHARS}); large skills "
            "bloat every turn they load into — consider trimming or splitting",
        ))
    # allowed-tools / disallowed-tools must be parseable (comma/space lists).
    for key in ("allowed-tools", "allowed_tools", "disallowed-tools", "disallowed_tools"):
        val = meta.get(key, "")
        if val and not any(c.isalnum() for c in val):
            out.append(Issue(f"skill:{label}", "warn", f"'{key}' has no tool names"))
    # Relative file references in the body that don't exist on disk.
    import re

    for m in re.finditer(r"\]\(([^)]+)\)", body):
        ref = m.group(1).strip()
        if ref.startswith(("http://", "https://", "#", "mailto:")) or ref.startswith("/"):
            continue
        if not (skill_md.parent / ref).exists():
            out.append(Issue(f"skill:{label}", "warn", f"broken file reference: {ref}"))
    return out


def lint_path(skills_root: Path) -> list[Issue]:
    """Lint every ``*/SKILL.md`` under a directory (used by the evi-skills CI)."""
    out: list[Issue] = []
    root = Path(skills_root)
    if not root.is_dir():
        return [Issue("skills", "error", f"not a directory: {root}")]
    for skill_md in sorted(root.glob("*/SKILL.md")):
        out += _lint_skill_md(skill_md, skill_md.parent.name)
    # A bare SKILL.md at the root (single-skill repo).
    if (root / "SKILL.md").is_file():
        out += _lint_skill_md(root / "SKILL.md", root.name)
    return out


def _str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _tool_glob(rule: str) -> str:
    """A rule's (or hard_deny entry's) tool-glob: its first word after an
    optional allow/deny."""
    words = rule.split()
    if words and words[0].lower() in ("allow", "deny"):
        words = words[1:]
    return words[0] if words else ""


def _mcp_server_names() -> list[str]:
    try:
        from evi.mcp.servers import load_servers

        return [s.name for s in load_servers()]
    except Exception:  # noqa: BLE001 — an unreadable mcp.json just isn't consulted
        return []


def _lint_rules(auto: dict) -> list[Issue]:
    """`rules` / `hard_deny` entries that can never apply: malformed ones, a
    hard_deny entry that starts with "allow", and any whose tool-glob matches
    no tool name, tool category or configured MCP server (MCP tools are named
    ``<server>.<tool>``; their tool part can't be checked offline)."""
    from fnmatch import fnmatch

    from evi.permissions import CALL_CATEGORIES, unmatched_rules

    out: list[Issue] = []
    rules, hard_deny = _str_list(auto.get("rules")), _str_list(auto.get("hard_deny"))
    well_formed: list[tuple[str, str]] = []
    for r in rules:
        words = r.split()
        if len(words) < 2 or words[0].lower() not in ("allow", "deny"):
            out.append(Issue("config.toml [auto] rules", "warn",
                             f"rule {r!r} is not '<allow|deny> <tool-glob> [arg-glob]' "
                             "and is ignored"))
        else:
            well_formed.append(("rules", r))
    for r in hard_deny:
        if r.split()[:1] and r.split()[0].lower() == "allow":
            out.append(Issue("config.toml [auto] hard_deny", "warn",
                             f"hard_deny entry {r!r} starts with 'allow', which is read as a "
                             "tool name, so it blocks nothing; hard_deny entries only deny"))
        elif r.strip():
            well_formed.append(("hard_deny", r))
    if not well_formed:
        return out
    from evi.tools import register_builtin_tools

    registry = register_builtin_tools()
    servers = _mcp_server_names()
    names = [*registry, *(f"{s}." for s in servers)]
    # A rule also matches a call's own category ("code_read", "shell").
    categories = {t.category for t in registry.values()} | {"mcp"} | CALL_CATEGORIES
    unmatched = set(unmatched_rules([r for _, r in well_formed], names, categories))
    for key, r in well_formed:
        if r not in unmatched:
            continue
        glob = _tool_glob(r)
        if "." in glob and any(fnmatch(s, glob.split(".", 1)[0]) for s in servers):
            continue  # names an MCP server's tool
        out.append(Issue(f"config.toml [auto] {key}", "warn",
                         f"{r!r}: tool-glob {glob!r} matches no tool name, tool category "
                         "or MCP server, so it never applies"))
    return out


def _lint_protected_remove(auto: dict) -> list[Issue]:
    """`protected_paths_remove` entries that remove nothing (not a built-in
    write-protected path nor in `protected_paths`), or less than it seems."""
    from evi.config import AutoSettings
    from evi.permissions import WRITE_PROTECTED_PATHS

    remove = [r.strip() for r in _str_list(auto.get("protected_paths_remove")) if r.strip()]
    if not remove:
        return []
    explicit = isinstance(auto.get("protected_paths"), list)
    mine = [p.strip() for p in (
        _str_list(auto.get("protected_paths")) if explicit else AutoSettings().protected_paths
    )]
    known = {*mine, *WRITE_PROTECTED_PATHS}
    res = "config.toml [auto] protected_paths_remove"
    out: list[Issue] = []
    for r in remove:
        if r not in known:
            out.append(Issue(res, "warn",
                             f"{r!r} is not a protected path (built-in, or in protected_paths), "
                             "so it removes nothing; use the entry's exact text"))
        elif explicit and r in mine:
            out.append(Issue(res, "warn",
                             f"{r!r} is also listed in protected_paths; it is removed"))
    if "~/.evi/**" in remove and ".evi/**" not in remove:
        out.append(Issue(res, "warn",
                         "removing '~/.evi/**' alone leaves your eVi home protected: "
                         "'.evi/**' matches an .evi folder at any depth"))
    return out


def _lint_auto_approve(auto: dict) -> list[Issue]:
    """An `auto_approve` list saved with the old default's "code", which now
    means running code (run_python) unasked, not just reading it. The fix
    keeps every other entry (`evi.permissions.auto_approve_migration`); a list
    naming "code_read" beside "code" keeps running code unasked on purpose."""
    from evi.config import _fmt
    from evi.permissions import auto_approve_migration

    if not isinstance(auto.get("auto_approve"), list):
        return []
    migrated = auto_approve_migration(_str_list(auto.get("auto_approve")))
    if migrated is None:
        return []
    return [Issue(
        "config.toml [auto] auto_approve", "warn",
        "'code' auto-approves running code (run_python, linters that run project "
        "code), not only reading it; reading code is now 'code_read'. To have code "
        f"execution ask, set auto_approve = {_fmt(migrated)}; to keep it unasked on "
        "purpose, add 'code_read' beside 'code'",
    )]


def lint_auto(auto: object) -> list[Issue]:
    """Lint the `[auto]` permission settings as written in config.toml: an
    auto_approve list from before "code_read", rules and hard_deny entries
    that never apply, destructive_allow globs the guard ignores,
    destructive_disable_rules ids that name no guard rule, and
    protected_paths_remove entries that remove nothing."""
    if not isinstance(auto, dict):
        return []
    from evi.shell_guard import RULE_IDS, rejected_allow_globs

    out = _lint_auto_approve(auto) + _lint_rules(auto)
    for g in rejected_allow_globs(_str_list(auto.get("destructive_allow"))):
        out.append(Issue("config.toml [auto] destructive_allow", "warn",
                         f"glob {g!r} has a wildcard in its command word and is ignored; "
                         "name the command, e.g. 'git push --force-with-lease*'"))
    for rid in _str_list(auto.get("destructive_disable_rules")):
        if rid not in RULE_IDS:
            out.append(Issue("config.toml [auto] destructive_disable_rules", "warn",
                             f"{rid!r} is not a destructive-guard rule id, so it disables "
                             "nothing"))
    return out + _lint_protected_remove(auto)


def lint() -> list[Issue]:
    """Lint the user's authored resources under ``~/.evi``. Returns all issues
    (empty = clean). Never raises — a missing resource is simply not checked."""
    from evi.config import (
        AGENTS_CONFIG_PATH,
        COMMANDS_DIR,
        CONFIG_PATH,
        HOOKS_CONFIG_PATH,
        SKILL_DIR,
    )

    out: list[Issue] = []

    # Skills.
    if SKILL_DIR.is_dir():
        out += lint_path(SKILL_DIR)

    # Hooks — reuse the editor validator (rejects typo'd event names etc.).
    if HOOKS_CONFIG_PATH.is_file():
        from evi import hooks

        err = hooks.validate(hooks.read_raw())
        if err:
            out.append(Issue("hooks.toml", "error", err))

    # Guardrails — reuse its validator.
    try:
        from evi.guardrails import GUARDRAILS_PATH, validate as gr_validate

        if GUARDRAILS_PATH.is_file():
            err = gr_validate(GUARDRAILS_PATH.read_text(encoding="utf-8"))
            if err:
                out.append(Issue("guardrails.toml", "error", err))
    except Exception:  # noqa: BLE001
        pass

    # Slash commands — frontmatter description recommended.
    if COMMANDS_DIR.is_dir():
        from evi.skills import _split_frontmatter

        for cmd in sorted(COMMANDS_DIR.rglob("*.md")):
            try:
                meta, _ = _split_frontmatter(cmd.read_text(encoding="utf-8"))
            except OSError as exc:
                out.append(Issue(f"command:{cmd.stem}", "error", f"unreadable: {exc}"))
                continue
            if not meta.get("description", "").strip():
                out.append(Issue(
                    f"command:{cmd.stem}", "warn",
                    "no frontmatter 'description' (shown in the command picker)",
                ))

    # Agent profiles — must parse as TOML.
    if AGENTS_CONFIG_PATH.is_file():
        import tomllib

        try:
            tomllib.loads(AGENTS_CONFIG_PATH.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            out.append(Issue("agents.toml", "error", f"parse error: {exc}"))

    # Permission settings — config.toml's own [auto] (not a profile's or a
    # project's), as written.
    if CONFIG_PATH.is_file():
        import tomllib

        try:
            data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            out.append(Issue("config.toml", "error", f"parse error: {exc}"))
        else:
            try:
                out += lint_auto(data.get("auto"))
            except Exception as exc:  # noqa: BLE001 — lint never raises
                out.append(Issue("config.toml [auto]", "warn",
                                 f"could not be checked: {type(exc).__name__}: {exc}"))

    return out
