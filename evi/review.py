"""`evi review` — git-aware code review.

Wraps `git diff` invocations to assemble the change set, then hands the
diff + a review-focused system prompt to the LLM. The agent gets
read-only access to the working tree (fs + git + index tools) so it can
pull surrounding context as needed.

Diff sources, in priority order:
1. `--diff-file <path>` — read an existing patch from disk.
2. `--file <path>` — diff just one file (working tree vs HEAD).
3. `--branch <name>` — `<name>...HEAD` (current branch's commits not on `<name>`).
4. Positional `HEAD~3..` / `<commit>..<commit>` — explicit range.
5. `--staged` — `git diff --cached`.
6. Default (no flags) — `git diff HEAD` (working tree vs last commit).

Output is streamed to console using the same Rich rendering the chat
REPL uses; we deliberately re-use that event handler so /goal, /plan,
permission prompts etc. all work the same.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


_MAX_DIFF_BYTES = 64 * 1024  # cap so we don't blow the context window


class ReviewError(RuntimeError):
    """Raised when the diff source is unreachable or unrecognised."""


def _git(*args: str, cwd: str | Path | None = None, timeout: float = 30.0) -> str:
    """Run a git command and return stdout. Raises ReviewError on non-zero."""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise ReviewError("git not on PATH — install git to use `evi review`") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"git timed out: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        raise ReviewError(f"git failed (exit {proc.returncode}): {stderr}")
    return proc.stdout


def get_diff(
    *,
    range: str | None = None,
    staged: bool = False,
    branch: str | None = None,
    file: str | None = None,
    diff_file: str | None = None,
    cwd: str | Path | None = None,
) -> str:
    """Resolve the requested diff source to a unified-diff string."""
    if diff_file:
        p = Path(diff_file).expanduser()
        if not p.is_file():
            raise ReviewError(f"diff file not found: {p}")
        return p.read_text(encoding="utf-8", errors="replace")
    if file:
        return _git("diff", "HEAD", "--", file, cwd=cwd)
    if branch:
        # Three-dot: changes on HEAD that aren't on `branch` (typical PR view).
        return _git("diff", f"{branch}...HEAD", cwd=cwd)
    if range:
        return _git("diff", range, cwd=cwd)
    if staged:
        return _git("diff", "--cached", cwd=cwd)
    # Default: working tree vs last commit.
    return _git("diff", "HEAD", cwd=cwd)


def truncate_diff(diff: str, *, max_bytes: int = _MAX_DIFF_BYTES) -> tuple[str, bool]:
    """Cap the diff at `max_bytes`. Returns `(text, was_truncated)`."""
    encoded = diff.encode("utf-8")
    if len(encoded) <= max_bytes:
        return diff, False
    # Truncate at a UTF-8 boundary and append a marker.
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return (truncated + "\n…(diff truncated)", True)


REVIEW_SYSTEM_PROMPT = (
    "You are a senior engineer reviewing a code diff. Be specific and concise.\n"
    "\n"
    "For each issue you find, output:\n"
    "  - the file + line range (e.g. `foo.py:42-50`)\n"
    "  - a one-line summary of the problem\n"
    "  - a brief fix suggestion (one or two sentences)\n"
    "\n"
    "Focus on:\n"
    "  1. **Bugs**: off-by-one, null/undefined, race conditions, "
    "exception handling, edge cases.\n"
    "  2. **Security**: injection, auth bypass, secrets in logs, "
    "TOCTOU, path traversal.\n"
    "  3. **API misuse / breaking changes**: public contract changes "
    "that weren't called out.\n"
    "  4. **Performance gotchas**: N+1 patterns, unbounded loops, "
    "I/O in hot paths.\n"
    "  5. **Missing tests** for the new behaviour.\n"
    "\n"
    "Skip purely stylistic nits unless they cause real harm. If the "
    "diff looks good, say so — don't invent issues. End with a one-line "
    "verdict: APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION."
)


def review_prompt(diff: str, *, label: str = "diff") -> str:
    """Compose the user message wrapping the diff in fences."""
    body, truncated = truncate_diff(diff)
    note = "" if not truncated else (
        f"\n(NOTE: diff was truncated at {_MAX_DIFF_BYTES // 1024} KB)"
    )
    return f"Please review the following {label}:{note}\n\n```diff\n{body}\n```"
