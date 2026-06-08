"""Tests for evi.sync — git-backed cross-machine sync of ~/.evi.

Uses real `git` (on PATH) against temp homes + a bare remote. Each repo gets a
local committer identity so the suite doesn't depend on global git config.
"""

from __future__ import annotations

import subprocess

import pytest

from evi import sync


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _identity(root):
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def test_init_writes_gitignore(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    sync.init(root=root)
    assert (root / ".git").is_dir()
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    for p in ("memory", "skills", "routes.json", "mcp.json", "hooks.toml"):
        assert p in gi


def test_gitignore_excludes_secrets_and_config(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "memory").mkdir()
    (root / "memory" / "a.md").write_text("knowledge", encoding="utf-8")
    (root / "config.toml").write_text("[llm]\napi_key = 'secret'", encoding="utf-8")
    (root / "tokens").mkdir()
    (root / "tokens" / "google.json").write_text("oauth-secret", encoding="utf-8")
    sync.init(root=root)
    _identity(root)
    _git(root, "add", "-A")
    tracked = _git(root, "ls-files").stdout.split()
    assert "memory/a.md" in tracked
    assert ".gitignore" in tracked
    assert "config.toml" not in tracked
    assert "tokens/google.json" not in tracked


def test_status_uninitialized_raises(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    with pytest.raises(sync.SyncError):
        sync.status(root=root)


def test_push_pull_roundtrip(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)

    # Machine A: has a memory note + a per-machine config.
    a = tmp_path / "a"
    a.mkdir()
    (a / "memory").mkdir()
    (a / "memory" / "note.md").write_text("hello", encoding="utf-8")
    (a / "config.toml").write_text("backend=ollama", encoding="utf-8")
    sync.init(remote=str(bare), root=a)
    _identity(a)
    assert "pushed" in sync.push(message="init", root=a)

    # Machine B: a different local config that must survive the pull.
    b = tmp_path / "b"
    b.mkdir()
    (b / "config.toml").write_text("backend=lmstudio", encoding="utf-8")
    sync.init(remote=str(bare), root=b)
    _identity(b)
    sync.pull(root=b)

    assert (b / "memory" / "note.md").read_text(encoding="utf-8") == "hello"
    # config.toml is per-machine: neither synced from A nor clobbered on B.
    assert (b / "config.toml").read_text(encoding="utf-8") == "backend=lmstudio"


def test_push_is_idempotent(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
    a = tmp_path / "a"
    a.mkdir()
    (a / "memory").mkdir()
    (a / "memory" / "n.md").write_text("1", encoding="utf-8")
    sync.init(remote=str(bare), root=a)
    _identity(a)
    sync.push(root=a)

    (a / "memory" / "n.md").write_text("2", encoding="utf-8")
    assert "pushed" in sync.push(root=a)
    # No new changes → no-op.
    assert "up to date" in sync.push(root=a)


def test_pull_changes_propagate(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
    a = tmp_path / "a"
    a.mkdir()
    (a / "memory").mkdir()
    (a / "memory" / "n.md").write_text("v1", encoding="utf-8")
    sync.init(remote=str(bare), root=a)
    _identity(a)
    sync.push(root=a)

    b = tmp_path / "b"
    b.mkdir()
    sync.init(remote=str(bare), root=b)
    _identity(b)
    sync.pull(root=b)
    assert (b / "memory" / "n.md").read_text(encoding="utf-8") == "v1"

    # A updates + pushes; B pulls the change.
    (a / "memory" / "n.md").write_text("v2", encoding="utf-8")
    sync.push(root=a)
    sync.pull(root=b)
    assert (b / "memory" / "n.md").read_text(encoding="utf-8") == "v2"
