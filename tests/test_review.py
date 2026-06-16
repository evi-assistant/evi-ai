"""Tests for evi/review.py — git diff dispatch + prompt assembly."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from evi.review import (
    REVIEW_SYSTEM_PROMPT,
    ReviewError,
    _MAX_DIFF_BYTES,
    get_diff,
    parse_verdict,
    review_exit_code,
    review_prompt,
    truncate_diff,
)


def _have_git() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=True, timeout=5,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


needs_git = pytest.mark.skipif(not _have_git(), reason="git not installed")


# ----- verdict parsing / exit codes -----------------------------------------


def test_parse_verdict_picks_keyword() -> None:
    assert parse_verdict("looks good.\n\nVerdict: APPROVE") == "APPROVE"
    assert parse_verdict("issues found\nREQUEST_CHANGES") == "REQUEST_CHANGES"
    assert parse_verdict("let's talk\nNEEDS_DISCUSSION") == "NEEDS_DISCUSSION"
    assert parse_verdict("no verdict line here") == ""


def test_parse_verdict_rightmost_wins() -> None:
    # An early mention of one keyword, then the real final verdict.
    text = "I considered REQUEST_CHANGES but ultimately APPROVE"
    assert parse_verdict(text) == "APPROVE"


def test_review_exit_code_from_verdict() -> None:
    assert review_exit_code("all good\nAPPROVE") == 0
    assert review_exit_code("nope\nREQUEST_CHANGES") == 1
    assert review_exit_code("hmm\nNEEDS_DISCUSSION") == 1


def test_review_exit_code_falls_back_to_file_line_issues() -> None:
    # No verdict line (multi-lens style): concrete issues -> gate fails.
    assert review_exit_code("## Correctness\n- foo.py:42 off-by-one") == 1
    # No verdict and no concrete issues -> pass.
    assert review_exit_code("Everything looks clean, no issues.") == 0


# ----- truncate_diff ---------------------------------------------------------


def test_truncate_diff_short_passes_through() -> None:
    body, truncated = truncate_diff("small diff", max_bytes=100)
    assert body == "small diff"
    assert truncated is False


def test_truncate_diff_long_gets_marker() -> None:
    body, truncated = truncate_diff("x" * 5000, max_bytes=100)
    assert truncated is True
    assert "…(diff truncated)" in body
    # Body before the marker is ≤ max_bytes worth of utf-8; the marker
    # itself adds the encoded length of "\n…(diff truncated)".
    marker_bytes = len("\n…(diff truncated)".encode("utf-8"))
    assert len(body.encode("utf-8")) <= 100 + marker_bytes


def test_truncate_diff_unicode_boundary() -> None:
    """Don't split multi-byte chars across the boundary."""
    body, _ = truncate_diff("ä" * 100, max_bytes=20)
    # ä is 2 bytes in UTF-8; trimming should land on a clean boundary.
    assert "�" not in body  # no replacement char


# ----- review_prompt --------------------------------------------------------


def test_review_prompt_wraps_in_fence() -> None:
    p = review_prompt("- foo\n+ bar")
    assert "```diff" in p
    assert "+ bar" in p


def test_review_prompt_warns_on_truncation() -> None:
    huge = "x" * (_MAX_DIFF_BYTES + 5_000)
    p = review_prompt(huge)
    assert "truncated" in p
    assert "KB" in p


# ----- get_diff: error paths ------------------------------------------------


def test_get_diff_missing_diff_file(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="not found"):
        get_diff(diff_file=str(tmp_path / "missing.patch"))


def test_get_diff_reads_diff_file(tmp_path: Path) -> None:
    p = tmp_path / "x.patch"
    p.write_text("diff body here", encoding="utf-8")
    assert get_diff(diff_file=str(p)) == "diff body here"


# ----- get_diff: integration against an ephemeral repo ---------------------


@needs_git
def test_get_diff_default_against_ephemeral_repo(tmp_path: Path) -> None:
    """Spin up a tiny repo, make a commit, change a file, verify the diff
    surfaces. Heavy but real — catches the actual git-flag surface."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@x",
    })

    def run(*args, **kw):
        return subprocess.run(
            args, cwd=repo, env=env, check=True, capture_output=True, **kw,
        )

    # Older git versions don't accept `-b` on init; use config flag for compat.
    run("git", "-c", "init.defaultBranch=main", "init")
    (repo / "foo.txt").write_text("hello\n", encoding="utf-8")
    run("git", "add", "foo.txt")
    run("git", "commit", "-m", "init")
    # Modify the file.
    (repo / "foo.txt").write_text("hello\nworld\n", encoding="utf-8")

    diff = get_diff(cwd=repo)
    assert "+world" in diff


@needs_git
def test_get_diff_staged_against_ephemeral_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@x",
    })

    def run(*args):
        return subprocess.run(
            args, cwd=repo, env=env, check=True, capture_output=True,
        )

    run("git", "-c", "init.defaultBranch=main", "init")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    run("git", "add", "a.txt")
    run("git", "commit", "-m", "init")
    # Stage a change.
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    run("git", "add", "a.txt")

    staged = get_diff(staged=True, cwd=repo)
    assert "+two" in staged
    # `get_diff()` (no args) uses `git diff HEAD` — that shows ALL
    # working-tree changes vs HEAD, INCLUDING staged ones. So a staged
    # change still appears here. Verify that's the case.
    wt = get_diff(cwd=repo)
    assert "+two" in wt


@needs_git
def test_get_diff_range_against_ephemeral_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@x",
    })

    def run(*args):
        return subprocess.run(
            args, cwd=repo, env=env, check=True, capture_output=True,
        )

    run("git", "-c", "init.defaultBranch=main", "init")
    (repo / "c.txt").write_text("a\n", encoding="utf-8")
    run("git", "add", "c.txt")
    run("git", "commit", "-m", "c1")
    (repo / "c.txt").write_text("a\nb\n", encoding="utf-8")
    run("git", "add", "c.txt")
    run("git", "commit", "-m", "c2")

    out = get_diff(range="HEAD~1..HEAD", cwd=repo)
    assert "+b" in out


@needs_git
def test_get_diff_nonexistent_branch_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@x",
    })
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=repo, env=env, check=True, capture_output=True,
    )
    (repo / "x.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "x.txt"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, env=env, check=True, capture_output=True)
    with pytest.raises(ReviewError):
        get_diff(branch="nope-not-here", cwd=repo)


# ----- system prompt sanity --------------------------------------------------


def test_review_system_prompt_mentions_security_and_verdict() -> None:
    # Sanity check that the prompt asks for what we'd want a reviewer to do.
    assert "Security" in REVIEW_SYSTEM_PROMPT
    assert "APPROVE" in REVIEW_SYSTEM_PROMPT
