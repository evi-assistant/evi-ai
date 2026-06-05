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
