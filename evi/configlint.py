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


def lint() -> list[Issue]:
    """Lint the user's authored resources under ``~/.evi``. Returns all issues
    (empty = clean). Never raises — a missing resource is simply not checked."""
    from evi.config import (
        AGENTS_CONFIG_PATH,
        COMMANDS_DIR,
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

    return out
