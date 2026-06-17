"""Project anatomy map — a cheap, token-estimated file index for orientation.

A local riff on OpenWolf-style "project intelligence": instead of the agent
blindly `read_file`-ing whole files to find its way around, eVi can inject a
compact map of the repo — every file with a rough token estimate, grouped by
directory — so the model knows *where* things are before spending context
reading them.

Built from `git ls-files` when the root is a git repo (so it honours
`.gitignore`), else a plain walk with a small ignore set. Token estimate is the
same char//4 heuristic eVi uses elsewhere — rough but enough to budget reads.
Written to ``<root>/.evi/anatomy.md`` (refreshable); auto-injected into project
context when present, and viewable via the ``evi anatomy`` CLI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ANATOMY_REL = Path(".evi") / "anatomy.md"

_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".venv313", ".venv-build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".idea", ".vscode", "target", ".evi",
}
_IGNORE_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".o", ".a", ".class", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".bin", ".lock", ".woff", ".woff2", ".ttf", ".mp4", ".mp3",
}
_MAX_FILES = 600       # cap the map so a huge monorepo doesn't blow context
_MAX_CHARS = 60_000    # hard cap on the rendered markdown


def _est_tokens(n_bytes: int) -> int:
    return max(1, n_bytes // 4)


def _keep(rel: Path) -> bool:
    """Shared filter for both enumerators: drop ignored DIRECTORIES (check only
    ancestor components, so a file merely *named* like an ignore token survives)
    and ignored suffixes."""
    if set(rel.parts[:-1]) & _IGNORE_DIRS:
        return False
    return rel.suffix.lower() not in _IGNORE_SUFFIXES


def _git_files(root: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-ignored files via git, or None if not a repo."""
    try:
        res = subprocess.run(
            # core.quotepath=false keeps non-ASCII paths raw (not octal-escaped +
            # quoted), so files like résumé.txt resolve instead of vanishing.
            ["git", "-C", str(root), "-c", "core.quotepath=false", "ls-files",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    out = []
    for line in res.stdout.splitlines():
        rel = line.strip()
        # Apply the same ignore filter the walk uses — git only honours
        # .gitignore, which may not exclude .evi/ or binary blobs.
        if rel and _keep(Path(rel)):
            out.append(root / rel)
    return out


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and _keep(p.relative_to(root)):
            out.append(p)
    return out


def _root(root: str | Path | None) -> Path:
    """Resolve the project root: the given path, else the per-session working
    folder (honours `evi chat --cwd` / `/cd`), else cwd. Mirrors bugledger."""
    if root is not None:
        return Path(root).resolve()
    try:
        from evi import workdir

        return Path(workdir.get_cwd())
    except Exception:
        return Path.cwd()


def build_anatomy(root: str | Path | None = None) -> str:
    """Return a Markdown map of `root` (default the session cwd): files grouped
    by directory, each with an estimated token count, plus subtotals."""
    base = _root(root)
    files = _git_files(base)
    if files is None:
        files = _walk_files(base)
    # Keep only files that still exist and are readable-size.
    sized: list[tuple[Path, int]] = []
    for f in files:
        try:
            sized.append((f, f.stat().st_size))
        except OSError:
            continue
    sized.sort(key=lambda t: str(t[0]).lower())
    truncated = len(sized) > _MAX_FILES
    sized = sized[:_MAX_FILES]

    # Group by parent directory (relative to root).
    by_dir: dict[str, list[tuple[str, int]]] = {}
    total_tokens = 0
    for f, size in sized:
        rel = f.relative_to(base)
        d = str(rel.parent).replace("\\", "/")
        d = "." if d == "." else d
        tok = _est_tokens(size)
        total_tokens += tok
        by_dir.setdefault(d, []).append((rel.name, tok))

    # Render the directory body first so we know if the char cap will also fire.
    body: list[str] = []
    for d in sorted(by_dir):
        entries = by_dir[d]
        dtok = sum(t for _, t in entries)
        body.append(f"## {d}/  _(~{dtok:,} tok)_")
        for name, tok in sorted(entries):
            body.append(f"- {name} — ~{tok:,}")
        body.append("")
    body_md = "\n".join(body)
    char_truncated = len(body_md) > _MAX_CHARS
    if char_truncated:
        body_md = body_md[:_MAX_CHARS] + "\n…(map truncated)"

    header = [
        f"# Project map — {base.name}",
        "",
        f"_{len(sized)} files · ~{total_tokens:,} tokens (est.)_"
        + ("  ⚠ truncated" if (truncated or char_truncated) else ""),
        "",
        "File sizes are rough token estimates (chars/4) — use them to budget "
        "reads; read only the slices you need.",
        "",
    ]
    return "\n".join(header) + "\n" + body_md


def anatomy_path(root: str | Path | None = None) -> Path:
    return _root(root) / ANATOMY_REL


def write_anatomy(root: str | Path | None = None) -> Path:
    """Build + write the map to ``<root>/.evi/anatomy.md`` and return its path."""
    p = anatomy_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_anatomy(root), encoding="utf-8")
    return p


def load_anatomy(root: str | Path | None = None) -> str | None:
    """Read a previously-written ``.evi/anatomy.md`` (None when absent)."""
    p = anatomy_path(root)
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None
