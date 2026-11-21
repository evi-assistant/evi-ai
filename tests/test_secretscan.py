"""`evi secrets scan`: secrets in files, a work tree or a diff, never shown."""

from __future__ import annotations

import subprocess

import pytest

from evi import redact, secretscan

GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # a GitHub token's shape
KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAx4f0Zt1n4bH0aK7Q2pL9vR3sT5uW7yZ\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def no_known_values(monkeypatch):
    """Only shapes count unless a test names a known value: the machine's own
    keys (env, ~/.evi) stay out of it."""
    monkeypatch.setattr(redact, "_known", lambda: redact._compile({}))


def test_a_token_is_found_on_its_line_and_not_shown():
    [f] = secretscan.scan_text(f"a = 1\ntoken = '{GH}'\n", "cfg.py")
    assert f == secretscan.Finding("cfg.py", 2, "github_token")
    assert GH not in repr(f)


def test_a_private_key_block_counts_on_its_first_line():
    assert secretscan.scan_text("x\n" + KEY) == [secretscan.Finding("", 2, "private_key")]


def test_a_known_value_is_found_by_its_value(monkeypatch):
    monkeypatch.setattr(redact, "_known", lambda: redact._compile({"hunter2-value-xyz": "llm"}))
    assert secretscan.scan_text("pw: hunter2-value-xyz") == [secretscan.Finding("", 1, "llm")]


def test_clean_text_has_no_findings():
    assert secretscan.scan_text("nothing to see\nhere = 'sk-'") == []


def test_scan_paths_walks_folders_and_skips_binaries_and_dot_git(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\0" + GH.encode())
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(GH, encoding="utf-8")
    found = secretscan.scan_paths(["."], tmp_path)
    assert found == [secretscan.Finding("src/a.py", 1, "github_token")]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "old.txt").write_text(f"old = '{GH}'\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_scan_tree_covers_tracked_and_untracked_but_not_ignored(repo):
    (repo / "new.txt").write_text(GH, encoding="utf-8")
    (repo / "ignored.txt").write_text(GH, encoding="utf-8")
    assert {f.path for f in secretscan.scan_tree(repo)} == {"old.txt", "new.txt"}


def test_scan_diff_sees_only_added_lines_on_their_new_line_numbers(repo):
    (repo / "old.txt").write_text(f"one\ntwo\nold = '{GH}'\nfresh = '{GH}x'\n",
                                  encoding="utf-8")
    assert secretscan.scan_diff(repo, "HEAD") == [
        secretscan.Finding("old.txt", 4, "github_token")]


def test_scan_diff_refuses_an_option_as_ref(repo):
    with pytest.raises(ValueError):
        secretscan.scan_diff(repo, "--output=x")
