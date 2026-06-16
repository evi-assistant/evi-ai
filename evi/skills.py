"""Skills — markdown instruction packets the model loads on demand.

Each skill lives at `~/.evi/skills/<name>/SKILL.md` with optional YAML-style
frontmatter:

    ---
    name: code-review
    description: Review a diff for correctness and style issues.
    ---

    # Body
    Step 1 …

The directory layout (one folder per skill) leaves room for skill-local
assets like helper scripts or example data without colliding with the file
that holds the instructions.

We deliberately do NOT auto-fire skills based on keyword matches. Instead
the agent sees a one-line index of every skill in its system prompt and
calls `invoke_skill(name)` when it wants the full body. That keeps the
context window cheap and the model's behavior debuggable.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from evi.config import SKILL_DIR


_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class SkillError(Exception):
    """A skill can't be imported (missing SKILL.md, bad name, already exists)."""


def _slug(name: str) -> str:
    """Filesystem-safe skill folder name. Takes the last path component, drops a
    trailing .md, and collapses anything outside [A-Za-z0-9_-] to a hyphen."""
    base = Path(str(name)).name
    if base.endswith(".md"):
        base = base[:-3]
    return re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-")


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    path: Path  # SKILL.md file


class SkillStore:
    """Read-only loader for `~/.evi/skills/`.

    Cheap to construct. Every public call rescans the directory so freshly
    added skills appear without an eVi restart (relevant for the long-lived
    web/desktop processes).
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else SKILL_DIR

    # --- public API ------------------------------------------------------

    def _scan_roots(self) -> list[tuple[str, Path]]:
        """[(name-prefix, dir)] — the user's skills dir plus each installed
        plugin's skills/ (~/.evi/plugins/<name>/skills/, exposed as
        `<plugin>:<skill>`)."""
        roots: list[tuple[str, Path]] = []
        if self.root.is_dir():
            roots.append(("", self.root))
        plugins = self.root.parent / "plugins"
        if plugins.is_dir():
            for pd in sorted(plugins.iterdir()):
                sdir = pd / "skills"
                if sdir.is_dir() and _NAME_RE.match(pd.name):
                    roots.append((pd.name + ":", sdir))
        return roots

    def list(self) -> list[SkillEntry]:
        entries: list[SkillEntry] = []
        seen: set[str] = set()
        for prefix, root in self._scan_roots():
            # Recursive: any directory holding a SKILL.md is a skill, so skills
            # can be organised in subfolders (`skills/web/auth/SKILL.md`), not
            # just immediate children. The invocation name still comes from the
            # frontmatter `name` (else the skill dir's own name).
            for skill_file in sorted(root.rglob("SKILL.md")):
                if not skill_file.is_file():
                    continue
                try:
                    meta, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
                except OSError:
                    continue
                local = (meta.get("name") or skill_file.parent.name).strip()
                if not _NAME_RE.match(local):
                    continue
                name = prefix + local
                if name in seen:  # first match wins (deterministic via sort)
                    continue
                seen.add(name)
                desc = (meta.get("description") or "").strip() or "(no description)"
                entries.append(SkillEntry(name=name, description=desc, path=skill_file))
        return entries

    def read(self, name: str) -> str:
        """Return the SKILL.md body (frontmatter stripped) for a given skill."""
        entry = self._find(name)
        if entry is None:
            raise KeyError(name)
        _, body = _split_frontmatter(entry.path.read_text(encoding="utf-8"))
        return body.strip()

    def load(self, name: str) -> tuple[str, Path, list[Path]]:
        """Return ``(body, skill_dir, companion_files)`` for a skill.

        ``companion_files`` are the absolute paths of every file bundled
        alongside ``SKILL.md`` (recursively), excluding ``SKILL.md`` itself —
        so a caller can tell the model what bundled references/scripts it may
        read. Raises ``KeyError`` if the skill doesn't exist.
        """
        entry = self._find(name)
        if entry is None:
            raise KeyError(name)
        _, body = _split_frontmatter(entry.path.read_text(encoding="utf-8"))
        skill_dir = entry.path.parent
        resources = sorted(
            p for p in skill_dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        return body.strip(), skill_dir, resources

    def format_for_prompt(self) -> str:
        """Render the skill index as a markdown block for the system prompt.

        Empty string when no skills are installed, so callers can append
        unconditionally.
        """
        entries = self.list()
        if not entries:
            return ""
        lines = ["## Available skills", ""]
        for e in entries:
            lines.append(f"- **{e.name}** — {e.description}")
        lines.append("")
        lines.append("Call `invoke_skill(name)` to load the full instructions.")
        return "\n".join(lines)

    # --- internals -------------------------------------------------------

    def _find(self, name: str) -> SkillEntry | None:
        for e in self.list():
            if e.name == name:
                return e
        return None


# --- frontmatter parser --------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (metadata_dict, body) from a markdown file.

    We accept a lightweight subset: a leading `---` line, then `key: value`
    pairs (one per line, no nested structures), then a closing `---`. If no
    frontmatter is present, returns ({}, text).

    PyYAML would be more correct, but we don't want another dependency for
    something this simple — skills are personal-use and authors can stick
    to the obvious format.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            body = "\n".join(lines[i + 1 :])
            return meta, body
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
        i += 1
    # Unterminated frontmatter — treat the whole thing as body.
    return {}, text


# --- importing external skills (e.g. Claude Agent Skills) -----------------


def _rewrite_refs(skill_dir: Path) -> int:
    """Rewrite relative references to bundled files in SKILL.md to absolute
    installed paths, so the model can read them regardless of its cwd. Only
    standalone tokens that exactly match an existing companion file are touched.
    Returns the number of replacements made."""
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return 0
    companions = sorted(
        (p.relative_to(skill_dir).as_posix()
         for p in skill_dir.rglob("*")
         if p.is_file() and p.name != "SKILL.md"),
        key=len, reverse=True,  # longest paths first so nested refs win
    )
    total = 0
    for rel in companions:
        absp = (skill_dir / rel).as_posix()
        # Match `rel` only when it isn't already part of a longer path/word.
        pattern = re.compile(r"(?<![\w./-])" + re.escape(rel) + r"(?![\w/-])")
        text, n = pattern.subn(absp, text)
        total += n
    if total:
        skill_md.write_text(text, encoding="utf-8")
    return total


def import_skill(
    source: str,
    *,
    name: str | None = None,
    rewrite_paths: bool = False,
    overwrite: bool = False,
    root: Path | None = None,
) -> str:
    """Import a skill directory (e.g. a Claude Agent Skill) into the skills dir.

    ``source`` may be a skill directory containing ``SKILL.md`` or the
    ``SKILL.md`` file itself. The installed name comes from ``name``, else the
    SKILL.md frontmatter ``name``, else the source folder name (slugified).
    With ``rewrite_paths`` the SKILL.md's relative references to bundled files
    are rewritten to absolute installed paths. Returns the installed name.
    """
    src = Path(source).expanduser()
    if src.is_file() and src.name == "SKILL.md":
        src = src.parent
    if not src.is_dir():
        raise SkillError(f"not a skill directory: {source}")
    skill_md = src / "SKILL.md"
    if not skill_md.is_file():
        raise SkillError(f"no SKILL.md in {src}")

    meta, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    slug = _slug(name or meta.get("name") or src.name)
    if not slug or not _NAME_RE.match(slug):
        raise SkillError(f"invalid skill name {slug!r} (use --name)")

    dest_root = root if root is not None else SKILL_DIR
    dest = dest_root / slug
    if dest.exists():
        if not overwrite:
            raise SkillError(f"skill exists: {slug} (pass overwrite=True / --force)")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    if rewrite_paths:
        _rewrite_refs(dest)
    return slug


def remove(name: str, root: Path | None = None) -> bool:
    """Delete a user skill directory (``~/.evi/skills/<name>/``).

    Returns True if it was removed, False if no such user skill exists.
    Plugin skills (``<plugin>:<skill>``) are owned by their plugin and can't be
    removed here — that raises ``SkillError``.
    """
    if ":" in name:
        raise SkillError(
            f"{name!r} is a plugin skill — remove its plugin with `evi plugin remove`"
        )
    dest_root = root if root is not None else SKILL_DIR
    dest = dest_root / _slug(name)
    if not dest.is_dir():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    return True
