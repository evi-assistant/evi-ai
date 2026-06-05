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
from dataclasses import dataclass
from pathlib import Path

from evi.config import SKILL_DIR


_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    path: Path  # SKILL.md file


class SkillStore:
    """Read-only loader for `~/.evi/skills/`.

    Cheap to construct. Every public call rescans the directory so freshly
    added skills appear without an Evi restart (relevant for the long-lived
    web/desktop processes).
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else SKILL_DIR

    # --- public API ------------------------------------------------------

    def list(self) -> list[SkillEntry]:
        if not self.root.is_dir():
            return []
        entries: list[SkillEntry] = []
        for sub in sorted(p for p in self.root.iterdir() if p.is_dir()):
            skill_file = sub / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                meta, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = (meta.get("name") or sub.name).strip()
            if not _NAME_RE.match(name):
                continue
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
