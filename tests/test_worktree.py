"""Tests for the git worktree wrapper — uses a real ephemeral git repo."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialize a minimal git repo with one commit and return its path."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # `-c init.defaultBranch=main` is supported by older gits than `git init -b`.
    _git("-c", "init.defaultBranch=main", "init", cwd=repo_dir)
    _git("config", "user.email", "evi@test.local", cwd=repo_dir)
    _git("config", "user.name", "evi-test", cwd=repo_dir)
    (repo_dir / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=repo_dir)
    _git("commit", "-m", "init", cwd=repo_dir)
    monkeypatch.chdir(repo_dir)
    return repo_dir


def test_repo_root_finds_top(repo: Path) -> None:
    from evi.worktree import repo_root

    assert repo_root().resolve() == repo.resolve()


def test_create_and_list(repo: Path) -> None:
    from evi.worktree import create_worktree, list_worktrees

    path = create_worktree("feature/x")
    assert path.is_dir()
    assert path.parent.name == ".worktrees"
    # Branch with / is flattened.
    assert path.name == "feature__x"

    entries = list_worktrees()
    branches = {e.branch for e in entries}
    assert "feature/x" in branches


def test_find_worktree_for(repo: Path) -> None:
    from evi.worktree import create_worktree, find_worktree_for

    created = create_worktree("topic")
    assert find_worktree_for("topic") == created
    assert find_worktree_for("nope") is None


def test_remove(repo: Path) -> None:
    from evi.worktree import create_worktree, find_worktree_for, remove_worktree

    create_worktree("temp")
    assert find_worktree_for("temp") is not None
    remove_worktree("temp")
    assert find_worktree_for("temp") is None


def test_create_fails_if_path_exists(repo: Path) -> None:
    from evi.worktree import WorktreeError, create_worktree

    create_worktree("dup")
    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree("dup")


# --- MSYS/Cygwin git path normalization -------------------------------------
#
# A git that prints POSIX paths on Windows (msys2, Cygwin, Git Bash, devkitPro)
# used to produce WindowsPath('/c/proj'), which blows up as a subprocess cwd
# with NotADirectoryError (WinError 267).


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/c/evi", r"C:\evi"),
        ("/c/Users/me/proj", r"C:\Users\me\proj"),
        ("/d/work", r"D:\work"),
        ("/cygdrive/c/evi", r"C:\evi"),          # Cygwin prefix
        ("/c", "C:\\"),                          # repo at a drive root
        ("/c/evi\n", r"C:\evi"),                 # trailing newline from git
        (r"C:\already\native", r"C:\already\native"),
        ("C:/forward/slashes", r"C:\forward\slashes"),
    ],
)
def test_git_path_normalizes_msys_on_windows(raw, expected, monkeypatch):
    from evi import worktree

    monkeypatch.setattr(worktree.os, "name", "nt")
    assert str(worktree._git_path(raw)) == expected


def test_git_path_leaves_posix_alone_off_windows(monkeypatch):
    from evi import worktree

    monkeypatch.setattr(worktree.os, "name", "posix")
    # "/c/foo" is a perfectly legitimate path on Linux — must NOT be rewritten.
    assert str(worktree._git_path("/c/foo")) == "/c/foo"
    assert str(worktree._git_path("/home/u/proj")) == "/home/u/proj"


def test_repo_root_survives_msys_style_git(repo: Path, monkeypatch) -> None:
    """repo_root must return a usable path even when git speaks MSYS."""
    from evi import worktree

    real_git = worktree._git

    def fake_git(*args: str, **kw):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            # What an msys2 git actually prints for C:\... paths.
            drive, rest = str(repo)[0], str(repo)[3:].replace("\\", "/")
            return f"/{drive.lower()}/{rest}\n"
        return real_git(*args, **kw)

    monkeypatch.setattr(worktree, "_git", fake_git)
    monkeypatch.setattr(worktree.os, "name", "nt")

    root = worktree.repo_root()
    assert str(root)[1:3] == ":\\", f"not a native path: {root}"
    assert root.is_dir(), f"repo_root returned an unusable path: {root}"


def test_repo_root_survives_unmappable_msys_mount(repo: Path, monkeypatch) -> None:
    """The case string-rewriting CANNOT fix.

    msys maps drives through a user-editable mount table, so C:\\Users\\me\\p
    prints as /home/me/p — there is no drive letter to recover. repo_root must
    still return a usable path by walking up for .git.
    """
    from evi import worktree

    real_git = worktree._git

    def fake_git(*args: str, **kw):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return "/home/someone/totally/unmappable\n"
        return real_git(*args, **kw)

    monkeypatch.setattr(worktree, "_git", fake_git)
    monkeypatch.setattr(worktree.os, "name", "nt")

    root = worktree.repo_root()
    assert root.resolve() == repo.resolve()
    assert root.is_dir()


def test_repo_root_error_names_the_likely_cause(tmp_path: Path, monkeypatch) -> None:
    from evi import worktree

    monkeypatch.setattr(
        worktree, "_git", lambda *a, **k: "/home/nobody/nowhere\n"
    )
    monkeypatch.setattr(worktree.os, "name", "nt")
    # tmp_path has no .git anywhere up the tree inside the temp area.
    with pytest.raises(worktree.WorktreeError, match="shadowing"):
        worktree.repo_root(tmp_path / "no-repo-here")


# --- dirty detection + the CLI confirmation it drives -----------------------


def test_resolve_worktree_path(repo: Path) -> None:
    from evi.worktree import resolve_worktree_path

    assert resolve_worktree_path("topic") == repo / ".worktrees" / "topic"
    # slashes in a branch name become __ so the path stays one level deep
    assert resolve_worktree_path("feat/x") == repo / ".worktrees" / "feat__x"
    # an absolute path is taken as-is
    assert resolve_worktree_path(str(repo / "elsewhere")) == repo / "elsewhere"


def test_dirty_files_clean_and_dirty(repo: Path) -> None:
    from evi.worktree import create_worktree, dirty_files

    wt = create_worktree("dirt")
    assert dirty_files(wt) == []            # freshly created -> clean

    (wt / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    dirty = dirty_files(wt)
    assert dirty and any("scratch.txt" in ln for ln in dirty)


def test_dirty_files_undetermined_is_none(tmp_path: Path) -> None:
    from evi.worktree import dirty_files

    # Missing directory -> None ("can't tell"), which the CLI treats as
    # "there may be work at risk" and prompts rather than assuming clean.
    assert dirty_files(tmp_path / "does-not-exist") is None


def test_cli_remove_refuses_dirty_worktree_non_interactively(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import typer

    import evi.apps.cli.main as cli
    from evi.worktree import create_worktree, find_worktree_for

    wt = create_worktree("risky")
    (wt / "unsaved.txt").write_text("work in progress\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    with pytest.raises(typer.Exit):
        cli.worktree_remove("risky", yes=False)

    # Refused, so the worktree and its uncommitted file must still be there.
    assert find_worktree_for("risky") is not None
    assert (wt / "unsaved.txt").is_file()


def test_cli_remove_clean_worktree_needs_no_prompt(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evi.apps.cli.main as cli
    from evi.worktree import create_worktree, find_worktree_for

    create_worktree("tidy")
    # No TTY and no --yes: a CLEAN worktree must still remove, or the prompt
    # would fire on routine cleanup and get itself worked around with --yes.
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        "typer.confirm", lambda *a, **k: pytest.fail("should not prompt when clean")
    )
    cli.worktree_remove("tidy", yes=False)
    assert find_worktree_for("tidy") is None


def test_cli_remove_yes_skips_prompt_on_dirty(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evi.apps.cli.main as cli
    from evi.worktree import create_worktree, find_worktree_for

    wt = create_worktree("forced")
    (wt / "unsaved.txt").write_text("bye\n", encoding="utf-8")

    monkeypatch.setattr(
        "typer.confirm", lambda *a, **k: pytest.fail("--yes must not prompt")
    )
    cli.worktree_remove("forced", yes=True)
    assert find_worktree_for("forced") is None
