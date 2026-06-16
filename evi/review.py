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

import re
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
    "  - a severity tag: [error] (must fix), [warn] (should fix), or [info] (nit)\n"
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


# Verdict keywords the single-pass review prompt is told to end with.
_VERDICTS = ("REQUEST_CHANGES", "NEEDS_DISCUSSION", "APPROVE")
_FILE_LINE_RE = re.compile(r"[\w./\\-]+:\d+")


def parse_verdict(text: str) -> str:
    """Extract the review's final verdict keyword, or "" if none is present.

    The rightmost match wins (the verdict line is at the end of the report)."""
    up = text.upper()
    best, best_at = "", -1
    for v in _VERDICTS:
        at = up.rfind(v)
        if at > best_at:
            best, best_at = v, at
    return best


def review_exit_code(text: str) -> int:
    """Map a review report to a process exit code for CI gating (`--exit-code`).

    APPROVE → 0; REQUEST_CHANGES / NEEDS_DISCUSSION → 1. When there's no
    explicit verdict (e.g. a multi-lens report), gate on whether any concrete
    `file:line` issue was reported. Mirrors `/ultrareview` used as a gate."""
    v = parse_verdict(text)
    if v == "APPROVE":
        return 0
    if v in ("REQUEST_CHANGES", "NEEDS_DISCUSSION"):
        return 1
    return 1 if _FILE_LINE_RE.search(text) else 0


# Per-repo review context + learned rules — eVi's local take on Bugbot's
# BUGBOT.md + "@cursor remember". All under the repo's .evi/ dir, fully local.
_REVIEW_CONTEXT_FILES = ("BUGBOT.md", "REVIEW.md")
_REVIEW_RULES_FILE = "review-rules.md"


def _review_dir(start: Path | None = None) -> Path:
    return (start or Path.cwd()) / ".evi"


def load_review_context(start: Path | None = None) -> str:
    """Repo-scoped review context: `.evi/BUGBOT.md` (or REVIEW.md) plus learned
    rules in `.evi/review-rules.md`. Returns "" when none exist."""
    d = _review_dir(start)
    parts: list[str] = []
    for name in _REVIEW_CONTEXT_FILES:
        f = d / name
        if f.is_file():
            try:
                parts.append(f.read_text(encoding="utf-8").strip())
            except OSError:
                pass
            break  # first one wins
    rules = d / _REVIEW_RULES_FILE
    if rules.is_file():
        try:
            txt = rules.read_text(encoding="utf-8").strip()
            if txt:
                parts.append("Learned review rules:\n" + txt)
        except OSError:
            pass
    return "\n\n".join(p for p in parts if p)


def remember_review_rule(text: str, start: Path | None = None) -> Path:
    """Append a learned review rule to `.evi/review-rules.md`. Returns the path."""
    d = _review_dir(start)
    d.mkdir(parents=True, exist_ok=True)
    f = d / _REVIEW_RULES_FILE
    line = "- " + text.strip() + "\n"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return f


def review_prompt(diff: str, *, label: str = "diff", context: str = "") -> str:
    """Compose the user message wrapping the diff in fences, with optional
    repo-scoped review context (BUGBOT.md + learned rules) prepended."""
    body, truncated = truncate_diff(diff)
    note = "" if not truncated else (
        f"\n(NOTE: diff was truncated at {_MAX_DIFF_BYTES // 1024} KB)"
    )
    head = ""
    if context.strip():
        head = (
            "Project-specific review guidance (follow it):\n"
            f"{context.strip()}\n\n"
        )
    return f"{head}Please review the following {label}:{note}\n\n```diff\n{body}\n```"


# --- multi-agent review (Phase 70) --------------------------------------
#
# Fan out one focused reviewer per lens (in parallel), then combine. Each lens
# is baked into the task message (run_subagents_parallel shares one system
# prompt across tasks), so a single generic reviewer prompt drives them all.

MULTI_REVIEW_SYSTEM_PROMPT = (
    "You are a senior engineer reviewing a code diff through ONE specific lens "
    "(given in the task). Stay strictly within that lens. For each issue: the "
    "file + line range (e.g. `foo.py:42-50`), a one-line problem, and a brief "
    "fix. Skip stylistic nits. If nothing in your lens applies, say so plainly "
    "— don't invent issues. You have read-only access to the working tree to "
    "pull surrounding context."
)

REVIEW_LENSES: dict[str, str] = {
    "correctness": (
        "Lens: CORRECTNESS. Find logic bugs only — off-by-one, null/undefined, "
        "race conditions, unhandled exceptions, wrong edge cases, broken control flow."
    ),
    "security": (
        "Lens: SECURITY. Find vulnerabilities only — injection, auth bypass, secrets "
        "in code/logs, TOCTOU, path traversal, unsafe deserialization, SSRF."
    ),
    "performance": (
        "Lens: PERFORMANCE. Find efficiency problems only — N+1 patterns, unbounded "
        "loops, I/O in hot paths, needless allocations, quadratic blow-ups."
    ),
    "tests": (
        "Lens: TESTS. Assess test coverage only — is the new/changed behaviour tested? "
        "Which edge cases or failure paths are missing? Suggest concrete test cases."
    ),
}


def multi_review(
    diff: str,
    lenses: list[str] | None = None,
    tool_categories: tuple[str, ...] = ("fs", "git", "index"),
    context: str = "",
) -> str:
    """Run one reviewer per lens in parallel and combine into one report.
    `context` is optional repo-scoped review guidance (BUGBOT.md + rules)."""
    from evi.llm.subagent import run_subagents_parallel

    chosen = lenses or list(REVIEW_LENSES)
    body, truncated = truncate_diff(diff)
    note = "" if not truncated else f"\n(NOTE: diff truncated at {_MAX_DIFF_BYTES // 1024} KB)"
    ctx = f"Project review guidance (follow it):\n{context.strip()}\n\n" if context.strip() else ""
    tasks = [
        f"{ctx}{REVIEW_LENSES[lens]}{note}\n\nReview this diff:\n```diff\n{body}\n```"
        for lens in chosen
        if lens in REVIEW_LENSES
    ]
    results = run_subagents_parallel(
        tasks,
        system_prompt=MULTI_REVIEW_SYSTEM_PROMPT,
        tool_categories=tool_categories,
    )
    blocks = [
        f"## {lens.title()}\n\n{findings}"
        for lens, (_, findings) in zip(chosen, results)
    ]
    return "# Multi-agent review\n\n" + "\n\n".join(blocks)
