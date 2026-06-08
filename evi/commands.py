"""User-defined slash commands — saved markdown prompt templates.

Drop a file at ``~/.evi/commands/<name>.md``; typing ``/<name>`` in the REPL or
web chat sends its (expanded) content as the next user message. Modelled on
Claude Code's custom commands:

- **Frontmatter** (optional YAML-ish block at the top)::

      ---
      description: Draft a conventional-commit message
      argument-hint: [scope]
      model: qwen2.5-coder:14b-instruct-q4_K_M
      ---

  `description` is shown in `/help`; `argument-hint` documents expected args;
  `model` is surfaced for callers that want a per-command model override.
- **Arguments**: ``$ARGUMENTS`` (everything after the name), positional
  ``$1``..``$9`` (shlex-split), and the legacy ``{args}`` (== ``$ARGUMENTS``).
- **File references**: ``@path/to/file`` inlines that file's contents (fenced),
  if it exists — otherwise the token is left untouched.
- **Namespacing**: subdirectories become ``:`` names, e.g.
  ``commands/git/commit.md`` → ``/git:commit``.

We deliberately do **not** execute ``!bash`` blocks (Claude Code gates those
behind allowed-tools; auto-running shell on expansion is too sharp an edge for
eVi's permission model). If you want triggering metadata or tool gating, that's
a Skill (`evi/skills.py`).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from evi.config import COMMANDS_DIR

# Each path segment must be a safe identifier (also blocks traversal).
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_POSITIONAL_RE = re.compile(r"\$([1-9])")
# `@path` not preceded by a non-space char (so emails like a@b aren't matched).
_FILE_REF_RE = re.compile(r"(?<!\S)@([^\s]+)")
_MAX_INLINE_BYTES = 16_000


@dataclass(frozen=True)
class SlashCommandEntry:
    name: str            # "commit" or namespaced "git:commit"
    path: Path
    summary: str         # for /help — frontmatter description, else first line
    description: str = ""
    argument_hint: str = ""
    model: str = ""


class CommandStore:
    """Loader for ``~/.evi/commands/**/*.md``. Stateless; every call rescans."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else COMMANDS_DIR

    def _scan_roots(self) -> list[tuple[str, Path]]:
        """[(name-prefix, dir)] — the user's commands dir plus each installed
        plugin's commands/ (~/.evi/plugins/<name>/commands/, exposed as
        `<plugin>:<cmd>`)."""
        roots: list[tuple[str, Path]] = []
        if self.root.is_dir():
            roots.append(("", self.root))
        plugins = self.root.parent / "plugins"
        if plugins.is_dir():
            for pd in sorted(plugins.iterdir()):
                cdir = pd / "commands"
                if cdir.is_dir() and _NAME_RE.match(pd.name):
                    roots.append((pd.name + ":", cdir))
        return roots

    def list(self) -> list[SlashCommandEntry]:
        out: list[SlashCommandEntry] = []
        for prefix, root in self._scan_roots():
            for p in sorted(root.rglob("*.md")):
                rel = p.relative_to(root).with_suffix("")
                if not all(_NAME_RE.match(part) for part in rel.parts):
                    continue
                out.append(self._entry(prefix + ":".join(rel.parts), p))
        return out

    def get(self, name: str) -> SlashCommandEntry | None:
        # Linear scan over a small set; also keeps path traversal impossible
        # (only files surfaced by list() — already name-validated — can match).
        for entry in self.list():
            if entry.name == name:
                return entry
        return None

    def expand(self, name: str, args: str = "") -> str | None:
        """Return the command body (frontmatter stripped) with arguments and
        file references substituted, or None if the command doesn't exist."""
        entry = self.get(name)
        if entry is None:
            return None
        _, body = _split_frontmatter(entry.path.read_text(encoding="utf-8"))
        return _substitute(body, args).strip()

    # --- internals -------------------------------------------------------

    def _entry(self, name: str, path: Path) -> SlashCommandEntry:
        meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        desc = meta.get("description", "")
        return SlashCommandEntry(
            name=name,
            path=path,
            summary=desc or _first_line(body),
            description=desc,
            argument_hint=meta.get("argument-hint", ""),
            model=meta.get("model", ""),
        )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body). No frontmatter → ({}, text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip().lower()] = val.strip().strip("'\"")
    return meta, text[m.end():]


def _substitute(body: str, args: str) -> str:
    parts = shlex.split(args) if args.strip() else []
    body = body.replace("$ARGUMENTS", args).replace("{args}", args)
    body = _POSITIONAL_RE.sub(
        lambda m: parts[int(m.group(1)) - 1] if int(m.group(1)) <= len(parts) else "",
        body,
    )
    return _inline_files(body)


def _inline_files(body: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        try:
            text = Path(m.group(1)).expanduser().read_text(encoding="utf-8")[:_MAX_INLINE_BYTES]
        except OSError:
            return m.group(0)  # not a readable file → leave the @token as-is
        return f"\n```\n{text}\n```\n"

    return _FILE_REF_RE.sub(repl, body)


def _first_line(text: str) -> str:
    """First prose line of the body (markdown header as fallback)."""
    header: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if header is None:
                header = stripped.lstrip("#").strip()
            continue
        return stripped[:160]
    return header[:160] if header else "(no summary)"
