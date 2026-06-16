"""Filesystem tools."""

from __future__ import annotations

import re
from pathlib import Path

from evi import workdir
from evi.citations import Citation, ToolOutput, trim_excerpt
from evi.tools.base import tool


_MAX_READ_BYTES = 256 * 1024  # 256 KB cap so we don't dump huge files into context

# Directories never worth walking for find_files / search_files.
_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", ".venv-build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox",
})
_MAX_GLOB_RESULTS = 200
_MAX_SEARCH_MATCHES = 200
_MAX_SEARCH_FILES = 5000
_MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MB when grepping

# Content cache keyed by resolved path → (mtime_ns, size, ToolOutput).
# The agent often re-reads the same file across turns; when it hasn't
# changed on disk we return the prior result instead of touching the disk
# and rebuilding the citation. mtime_ns + size together invalidate on any
# edit (mtime alone can miss same-second writes; size catches most of those
# and the pair is what editors actually bump).
_READ_CACHE: dict[str, tuple[int, int, ToolOutput]] = {}
_READ_CACHE_MAX = 128


def clear_read_cache() -> None:
    """Drop the read_file content cache. Used by tests and could be wired to
    a future `/reload`-style invalidation."""
    _READ_CACHE.clear()


@tool(
    description=(
        "Read a UTF-8 text file from disk. For large files, paginate with "
        "offset (1-based line to start at) and limit (max lines) to read just a "
        "slice. Returns content or an error string."
    ),
    category="fs",
)
def read_file(path: str, offset: int = 0, limit: int = 0) -> ToolOutput | str:
    p = workdir.resolve(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    # Slice mode: any non-default offset/limit. Streams lines so it can read a
    # window of a file far larger than the whole-file byte cap.
    if offset > 1 or limit > 0:
        return _read_slice(p, max(offset, 1), limit)
    return _read_whole(p)


def _read_whole(p: Path) -> ToolOutput | str:
    try:
        st = p.stat()
        key = str(p.resolve())
    except OSError as exc:
        return f"ERROR: cannot stat {p}: {exc}"

    cached = _READ_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    if st.st_size > _MAX_READ_BYTES:
        return (
            f"ERROR: file too large ({st.st_size} bytes, max {_MAX_READ_BYTES}); "
            "read a slice with offset/limit"
        )
    data = p.read_bytes()
    if len(data) > _MAX_READ_BYTES:
        return f"ERROR: file too large ({len(data)} bytes, max {_MAX_READ_BYTES})"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"ERROR: not utf-8 text: {p}"
    # Surface a citation so the web UI can render a chip pointing back to
    # the file. `start`/`end` are 1-indexed line numbers covering the
    # whole file.
    line_count = text.count("\n") + 1
    citation = Citation(
        id="1",
        source_type="file",
        source_id=str(p),
        excerpt=trim_excerpt(text),
        start=1,
        end=line_count,
    )
    output = ToolOutput(text=text, citations=[citation])

    # Cache, evicting an arbitrary (oldest-inserted) entry when full.
    if len(_READ_CACHE) >= _READ_CACHE_MAX:
        _READ_CACHE.pop(next(iter(_READ_CACHE)))
    _READ_CACHE[key] = (st.st_mtime_ns, st.st_size, output)
    return output


def _read_slice(p: Path, start_line: int, limit: int) -> ToolOutput | str:
    """Return lines [start_line, start_line+limit) (or to EOF when limit<=0).
    Raw content (no line-number prefixes, so the slice round-trips through
    edit_file); the line range rides on the citation. Capped by byte budget."""
    end_line = start_line + limit - 1 if limit > 0 else None
    out: list[str] = []
    nbytes = 0
    truncated = False
    try:
        with p.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                if i < start_line:
                    continue
                if end_line is not None and i > end_line:
                    break
                nbytes += len(line.encode("utf-8"))
                if nbytes > _MAX_READ_BYTES:
                    truncated = True
                    break
                out.append(line)
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: cannot read {p}: {exc}"
    if not out:
        return f"ERROR: no lines at offset {start_line} in {p}"
    text = "".join(out)
    last = start_line + len(out) - 1
    note = f"\n... [truncated at {_MAX_READ_BYTES} bytes]" if truncated else ""
    citation = Citation(
        id="1",
        source_type="file",
        source_id=str(p),
        excerpt=trim_excerpt(text),
        start=start_line,
        end=last,
    )
    return ToolOutput(text=text + note, citations=[citation])


@tool(
    description="Write UTF-8 text to a file, creating parent dirs if needed. Overwrites.",
    category="fs",
)
def write_file(path: str, content: str) -> str:
    p = workdir.resolve(path)
    # Snapshot the prior state so `evi rewind` / `/rewind` can undo this write.
    # Best-effort — a checkpoint failure must never block the write itself.
    try:
        from evi.checkpoints import record_before_write

        record_before_write(p)
    except Exception:  # noqa: BLE001
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so we write the content's own line endings verbatim (avoid
    # Windows turning \n into \r\n and flipping a whole file to CRLF).
    p.write_text(content, encoding="utf-8", newline="")
    _READ_CACHE.pop(str(p.resolve()), None)
    return f"wrote {len(content)} chars to {p}"


@tool(
    description=(
        "Make a surgical edit to a text file: replace the exact substring "
        "old_string with new_string. old_string must match the file exactly "
        "(including whitespace) and appear exactly once, unless replace_all is "
        "true. Prefer this over write_file for small changes — it's safer and "
        "far cheaper than rewriting the whole file."
    ),
    category="fs",
)
def edit_file(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str:
    p = workdir.resolve(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    if old_string == new_string:
        return "ERROR: old_string and new_string are identical"
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: cannot read {p}: {exc}"
    count = text.count(old_string)
    if count == 0:
        return f"ERROR: old_string not found in {p}"
    if count > 1 and not replace_all:
        return (
            f"ERROR: old_string appears {count} times in {p}; pass "
            "replace_all=true or include more surrounding context to make it unique"
        )
    new_text = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    # Snapshot for `evi rewind` (best-effort; never block the edit).
    try:
        from evi.checkpoints import record_before_write

        record_before_write(p)
    except Exception:  # noqa: BLE001
        pass
    p.write_text(new_text, encoding="utf-8", newline="")  # preserve LF/CRLF
    _READ_CACHE.pop(str(p.resolve()), None)  # so a later read sees the edit
    return f"edited {p}: {count if replace_all else 1} replacement(s)"


_PATCH_RE = re.compile(
    r"<{3,}\s*SEARCH[^\n]*\n(.*?)\n?={3,}[^\n]*\n(.*?)\n?>{3,}\s*REPLACE",
    re.DOTALL,
)


def _parse_patch(patch: str) -> list[tuple[str, str]]:
    """Parse SEARCH/REPLACE blocks into (old, new) pairs."""
    return [(m.group(1), m.group(2)) for m in _PATCH_RE.finditer(patch or "")]


@tool(
    description=(
        "Apply several edits to ONE file in a single call. `patch` holds one or "
        "more search/replace blocks in this exact format:\n"
        "<<<<<<< SEARCH\n<existing text>\n=======\n<replacement>\n>>>>>>> REPLACE\n"
        "Each SEARCH must match the current file exactly and uniquely; blocks "
        "apply in order. Cheaper/safer than rewriting the whole file for "
        "multi-spot changes. Use edit_file for a single replacement."
    ),
    category="fs",
)
def apply_patch(path: str, patch: str) -> str:
    p = workdir.resolve(path)
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    blocks = _parse_patch(patch)
    if not blocks:
        return (
            "ERROR: no SEARCH/REPLACE blocks found — use\n"
            "<<<<<<< SEARCH\\n<old>\\n=======\\n<new>\\n>>>>>>> REPLACE"
        )
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: cannot read {p}: {exc}"
    new_text = text
    for i, (old, new) in enumerate(blocks, 1):
        if old == "":
            return f"ERROR: block {i} has an empty SEARCH section"
        n = new_text.count(old)
        if n == 0:
            return f"ERROR: block {i} SEARCH not found in {p}"
        if n > 1:
            return (
                f"ERROR: block {i} SEARCH matches {n} times in {p} — "
                "add surrounding context to make it unique"
            )
        new_text = new_text.replace(old, new, 1)
    if new_text == text:
        return "ERROR: patch made no changes"
    try:
        from evi.checkpoints import record_before_write

        record_before_write(p)
    except Exception:  # noqa: BLE001
        pass
    p.write_text(new_text, encoding="utf-8", newline="")
    _READ_CACHE.pop(str(p.resolve()), None)
    return f"applied {len(blocks)} hunk(s) to {p}"


@tool(
    description="List entries in a directory. Returns one path per line.",
    category="fs",
)
def list_dir(path: str = ".") -> str:
    p = workdir.resolve(path)
    if not p.is_dir():
        return f"ERROR: not a directory: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    return "\n".join(f"{'D' if e.is_dir() else 'F'} {e.name}" for e in entries)


def _is_ignored(p: Path, base: Path) -> bool:
    """True if any path component between base and p is a noise dir."""
    try:
        rel_parts = p.relative_to(base).parts
    except ValueError:
        rel_parts = p.parts
    return any(part in _IGNORE_DIRS for part in rel_parts)


@tool(
    description=(
        "Find files by glob pattern (e.g. '**/*.py', 'src/*.ts'). Literal "
        "name matching, not semantic. Returns matching paths, one per line. "
        "Skips noise dirs (.git, node_modules, .venv, …)."
    ),
    category="fs",
)
def find_files(pattern: str, path: str = ".") -> str:
    base = workdir.resolve(path)
    if not base.is_dir():
        return f"ERROR: not a directory: {base}"
    try:
        matches = sorted(
            str(m) for m in base.glob(pattern)
            if m.is_file() and not _is_ignored(m, base)
        )
    except (ValueError, OSError) as exc:
        return f"ERROR: bad glob {pattern!r}: {exc}"
    if not matches:
        return f"(no files match {pattern!r} under {base})"
    head = matches[:_MAX_GLOB_RESULTS]
    extra = len(matches) - len(head)
    suffix = f"\n... ({extra} more)" if extra > 0 else ""
    return "\n".join(head) + suffix


@tool(
    description=(
        "Search file CONTENTS by regular expression (literal grep, not "
        "semantic — use find_in_project for meaning-based search). Returns "
        "'path:line: text' matches. Narrow with the glob arg (e.g. '*.py'). "
        "Set ignore_case=true for case-insensitive."
    ),
    category="fs",
)
def search_files(
    pattern: str, path: str = ".", glob: str = "", ignore_case: bool = False
) -> str:
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"ERROR: bad regex {pattern!r}: {exc}"
    base = workdir.resolve(path)
    if base.is_file():
        files: list[Path] = [base]
        base_dir = base.parent
    elif base.is_dir():
        base_dir = base
        files = []
        for m in base.rglob(glob or "*"):
            if len(files) >= _MAX_SEARCH_FILES:
                break
            if m.is_file() and not _is_ignored(m, base):
                files.append(m)
    else:
        return f"ERROR: no such path: {base}"

    matches: list[str] = []
    scanned = 0
    for fp in files:
        if len(matches) >= _MAX_SEARCH_MATCHES:
            break
        try:
            if fp.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                continue
            with fp.open("r", encoding="utf-8") as fh:
                for i, line in enumerate(fh, start=1):
                    if rx.search(line):
                        rel = fp.relative_to(base_dir) if base.is_dir() else fp
                        matches.append(f"{rel}:{i}: {line.rstrip(chr(10))[:300]}")
                        if len(matches) >= _MAX_SEARCH_MATCHES:
                            break
        except (OSError, UnicodeDecodeError):
            continue  # binary / unreadable — skip
        scanned += 1

    if not matches:
        return f"(no matches for {pattern!r} in {scanned} files under {base})"
    capped = "" if len(matches) < _MAX_SEARCH_MATCHES else f"\n... (capped at {_MAX_SEARCH_MATCHES} matches)"
    return "\n".join(matches) + capped
