"""Tests for the read-only git tools against an ephemeral repo."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evi.tools.base import REGISTRY
import evi.tools.git  # noqa: F401  register tools


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    _git("-c", "init.defaultBranch=main", "init", cwd=d)
    _git("config", "user.email", "evi@test.local", cwd=d)
    _git("config", "user.name", "evi", cwd=d)
    (d / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=d)
    _git("commit", "-m", "init", cwd=d)
    (d / "README.md").write_text("hello\nworld\n")
    monkeypatch.chdir(d)
    return d


def test_git_status(repo: Path) -> None:
    out = REGISTRY["git_status"].call(json.dumps({"path": str(repo)}))
    assert "README.md" in out


def test_git_diff(repo: Path) -> None:
    out = REGISTRY["git_diff"].call(json.dumps({"path": str(repo)}))
    assert "+hello" in out or "hello" in out


def test_git_log_default_limit(repo: Path) -> None:
    out = REGISTRY["git_log"].call(json.dumps({"path": str(repo)}))
    assert "init" in out


def test_git_show_head(repo: Path) -> None:
    out = REGISTRY["git_show"].call(json.dumps({"ref": "HEAD", "path": str(repo)}))
    assert "README.md" in out


def test_git_blame(repo: Path) -> None:
    # Commit the change first so blame has something to attribute.
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "update", cwd=repo)
    out = REGISTRY["git_blame"].call(json.dumps({
        "file_path": "README.md", "path": str(repo)
    }))
    assert "hello" in out
    assert "evi" in out  # author name


def test_git_info(repo: Path) -> None:
    out = REGISTRY["git_info"].call(json.dumps({"path": str(repo)}))
    data = json.loads(out)
    assert data["branch"] == "main"
    assert isinstance(data["head"], str) and len(data["head"]) >= 7
    assert data["dirty_files"] >= 0


def test_git_diff_with_ref(repo: Path) -> None:
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "update", cwd=repo)
    out = REGISTRY["git_diff"].call(json.dumps({
        "ref": "HEAD~1..HEAD", "path": str(repo)
    }))
    assert "README" in out
