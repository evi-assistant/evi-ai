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


# The empty tree: what a repository with no commit yet is diffed against.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# A push with more unsent commits than this is scanned by what HEAD holds,
# not commit by commit (a first push of a long history).
MAX_PUSH_COMMITS = 300
# Fixed whatever the user's or the repository's config says: a/ b/ prefixes
# (diff.noprefix, diff.mnemonicPrefix), paths from the top (diff.relative),
# names unquoted (core.quotePath), no context lines.
_PATCH_FLAGS = ["--no-color", "--no-ext-diff", "--unified=0", "--src-prefix=a/",
                "--dst-prefix=b/", "--no-relative"]
_QUOTE_PATH = ["-c", "core.quotePath=false"]


def _header_path(name: str) -> str:
    """The file a ``+++ `` header names ("" for /dev/null): git's trailing tab
    (a name with a space) dropped, C-style quoting undone, the b/ cut."""
    import codecs

    name = name.rstrip("\r\n").rstrip("\t")
    if name == "/dev/null":
        return ""
    if len(name) >= 2 and name[0] == name[-1] == '"':
        raw = codecs.escape_decode(name[1:-1].encode("utf-8", "backslashreplace"))[0]
        name = raw.decode("utf-8", errors="replace")
    return name[2:] if name.startswith("b/") else name


def _scan_patch(patch: str) -> list[Finding]:
    """The secrets on the lines a patch (``git diff`` / ``git log -p`` with
    `_PATCH_FLAGS`) adds, each on its line in the new file. A hunk's lines
    are counted from its ``@@ -a,b +c,d @@`` header, so an added line that
    reads like a header (``+++ x``) is still content."""
    out: list[Finding] = []
    path, first = "", 0
    old_left = new_left = 0
    added: list[str] = []

    def flush() -> None:
        if added:
            shown = path or "(unknown file)"
            for f in scan_text("\n".join(added), shown):
                out.append(Finding(shown, first + f.line - 1, f.kind))
            added.clear()

    for raw in patch.splitlines():
        if old_left > 0 or new_left > 0:
            if raw.startswith("+"):
                added.append(raw[1:])
                new_left -= 1
                continue
            if raw.startswith("-"):
                old_left -= 1
                continue
            if raw.startswith(" "):
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if raw.startswith("\\"):
                continue
            old_left = new_left = 0  # the hunk ended early: read as a header
        if raw.startswith("\\"):
            continue
        flush()
        if raw.startswith("diff "):
            path = ""
        elif raw.startswith("+++ "):
            path = _header_path(raw[4:])
        elif raw.startswith("@@"):
            try:
                old, new = raw.split("@@")[1].split()[:2]
                o = old.lstrip("-").split(",")
                n = new.lstrip("+").split(",")
                old_left = int(o[1]) if len(o) > 1 else 1
                new_left = int(n[1]) if len(n) > 1 else 1
                first = int(n[0])
            except (IndexError, ValueError):
                old_left = new_left = 0
    flush()
    return out


def scan_diff(repo: str | Path = ".", base: str = "HEAD", head: str | None = None) -> list[Finding]:
    """The secrets in the lines `git diff <base>` adds in `repo` (the work
    tree against `base`; with `head`, that commit against `base`), each on
    its line in the new file, paths from the repository's top."""
    from evi.gitsafe import validate_ref

    revs = [validate_ref(base)] + ([validate_ref(head)] if head else [])
    return _scan_patch(_git([*_QUOTE_PATH, "diff", *_PATCH_FLAGS, *revs, "--"], Path(repo)))


def _try_git(args: list[str], root: Path) -> str | None:
    try:
        return _git(args, root).strip() or None
    except ScanError:
        return None


def scan_outgoing(repo: str | Path, *, commit: bool, push: bool, adds: bool = False
                  ) -> list[Finding]:
    """The secrets in what a `git commit` (`commit`), and a `git push` or
    `gh pr create` of the current branch (`push`), would send from the git
    work tree `repo` is in (scanned from its top folder).

    A commit sends what is staged and, as `git commit -a` would, the work
    tree's changes to tracked files: both are scanned. When the same line
    runs `git add` (`adds`), the untracked files git doesn't ignore count
    too. A push sends every commit on HEAD that is on no remote yet: each is
    scanned, so a secret added and then removed again is still found (past
    MAX_PUSH_COMMITS, what HEAD holds is scanned instead).
    ScanError when `repo` is not in a git work tree."""
    top_s = _try_git(["rev-parse", "--show-toplevel"], Path(repo))
    if not top_s:
        raise ScanError(f"not a git work tree: {repo}")
    top = Path(top_s)
    has_head = _try_git(["rev-parse", "--verify", "--quiet", "HEAD"], top) is not None
    base = "HEAD" if has_head else EMPTY_TREE
    out: list[Finding] = []

    def patch(*args: str) -> list[Finding]:
        return _scan_patch(_git([*_QUOTE_PATH, *args], top))

    if commit:
        out += patch("diff", *_PATCH_FLAGS, "--cached", base, "--")
        out += patch("diff", *_PATCH_FLAGS, base, "--")
        if adds:
            names = _git(["ls-files", "-z", "--others", "--exclude-standard"], top)
            out += scan_paths([n for n in names.split("\0") if n and not n.endswith("/")], top)
    if push and has_head:
        remote = _try_git(["for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes"], top)
        revs = ["HEAD", "--not", "--remotes"] if remote else ["HEAD"]
        count = int(_git(["rev-list", "--count", *revs], top).strip() or 0)
        if count > MAX_PUSH_COMMITS:
            out += patch("diff", *_PATCH_FLAGS, EMPTY_TREE, "HEAD", "--")
        elif count:
            out += patch("log", "-p", "-m", "--format=", *_PATCH_FLAGS, *revs, "--")
    return sorted(set(out), key=lambda f: (f.path, f.line, f.kind))
