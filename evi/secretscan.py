"""Secret scanning: `evi secrets scan`.

Finds what `evi.redact` would redact — a token, key or private-key block by
its shape (`evi.redact.PATTERNS`), or a secret this machine holds (an
`[llm]` api_key, a ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` variable, the
director key) by its value — in files or in the lines a git diff adds. A
finding names the file, the line and the kind of secret, never the secret
itself.

Pure apart from reading files and running git (`evi.gitsafe.run_git`, so a
repository's own config runs no program).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Files bigger than this are skipped (a build artifact, a database), and so
# is a file whose first 8 KB hold a NUL byte (a binary).
MAX_BYTES = 1_000_000


class ScanError(RuntimeError):
    """The scan could not run: no git, not a repository, a bad ref."""


@dataclass(frozen=True)
class Finding:
    path: str   # as given, or relative to the repository (/-separated)
    line: int   # 1-based
    kind: str   # the redaction kind: "github_token", "private_key", ...


def scan_text(text: str, path: str = "") -> list[Finding]:
    """The secrets in `text`, one Finding per (line, kind), in line order.
    A multi-line match (a private-key block) counts on the line it starts."""
    from evi.redact import PATTERNS, _known

    hits: set[tuple[int, str]] = set()

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    known = _known()
    if known.rx is not None:
        for m in known.rx.finditer(text):
            hits.add((line_of(m.start()), known.kinds.get(m.group(0), "secret")))
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            hits.add((line_of(m.start("s")), kind))
    return [Finding(path, line, kind) for line, kind in sorted(hits)]


def _readable(p: Path) -> str | None:
    """`p`'s text, or None for one that is too big, binary or unreadable."""
    try:
        if p.stat().st_size > MAX_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _git(args: list[str], repo: Path) -> str:
    from evi.gitsafe import run_git

    try:
        r = run_git(args, cwd=repo, timeout=60)
    except Exception as exc:  # noqa: BLE001 — no git, an unsafe repo, a timeout
        raise ScanError(f"git {args[0]}: {exc}") from exc
    if r.returncode != 0:
        raise ScanError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def scan_paths(paths: Iterable[str | Path], root: str | Path = ".") -> list[Finding]:
    """The secrets in `paths` (files, or folders walked; a .git folder is
    skipped), each shown relative to `root` when it is under it."""
    base = Path(root)
    out: list[Finding] = []
    for given in paths:
        p = Path(given)
        if not p.is_absolute():
            p = base / p
        files = sorted(f for f in p.rglob("*") if f.is_file() and ".git" not in f.parts) \
            if p.is_dir() else [p]
        for f in files:
            text = _readable(f)
            if text is None:
                continue
            try:
                shown = f.relative_to(base).as_posix()
            except ValueError:
                shown = str(f)
            out += scan_text(text, shown)
    return out


def scan_tree(repo: str | Path = ".") -> list[Finding]:
    """The secrets in the work tree of the git repository `repo`: its
    tracked files and the untracked ones git doesn't ignore."""
    root = Path(repo)
    names = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    return scan_paths([n for n in sorted(set(names.split("\0"))) if n], root)


def scan_diff(repo: str | Path = ".", base: str = "HEAD") -> list[Finding]:
    """The secrets in the lines `git diff <base>` adds in `repo` (the work
    tree against `base`), each on its line in the new file."""
    from evi.gitsafe import validate_ref

    root = Path(repo)
    diff = _git(["diff", "--no-color", "--no-ext-diff", "--unified=0",
                 validate_ref(base), "--"], root)
    out: list[Finding] = []
    path, line = "", 0
    added: list[str] = []
    first = 0

    def flush() -> None:
        if path and added:
            for f in scan_text("\n".join(added), path):
                out.append(Finding(path, first + f.line - 1, f.kind))

    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            flush()
            added, name = [], raw[4:]
            path = name[2:] if name.startswith("b/") else ""
        elif raw.startswith("@@"):
            flush()
            added = []
            new = raw.split("+", 1)[1].split(" ", 1)[0]
            line = first = int(new.split(",", 1)[0] or 0)
        elif raw.startswith("+") and path:
            if not added:
                first = line
            added.append(raw[1:])
            line += 1
    flush()
    return out
