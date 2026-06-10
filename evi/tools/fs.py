"""Filesystem tools."""

from __future__ import annotations

from pathlib import Path

from evi.citations import Citation, ToolOutput, trim_excerpt
from evi.tools.base import tool


_MAX_READ_BYTES = 256 * 1024  # 256 KB cap so we don't dump huge files into context

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
    description="Read a UTF-8 text file from disk. Returns content or an error string.",
    category="fs",
)
def read_file(path: str) -> ToolOutput | str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    try:
        st = p.stat()
        key = str(p.resolve())
    except OSError as exc:
        return f"ERROR: cannot stat {p}: {exc}"

    cached = _READ_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    if st.st_size > _MAX_READ_BYTES:
        return f"ERROR: file too large ({st.st_size} bytes, max {_MAX_READ_BYTES})"
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


@tool(
    description="Write UTF-8 text to a file, creating parent dirs if needed. Overwrites.",
    category="fs",
)
def write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
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
    p = Path(path).expanduser()
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


@tool(
    description="List entries in a directory. Returns one path per line.",
    category="fs",
)
def list_dir(path: str = ".") -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"ERROR: not a directory: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    return "\n".join(f"{'D' if e.is_dir() else 'F'} {e.name}" for e in entries)
