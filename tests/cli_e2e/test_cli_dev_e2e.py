"""CLI e2e for the self-build entry points: `evi doctor --dev`, `evi chat --mode`.

Runs the real `python -m evi` against throwaway git checkouts under tmp_path.
The interactive REPL needs a console and a backend, so `chat` is covered here
only up to its option checks; tests/test_self_build_commands.py runs a whole
`evi chat --mode code` session against a fake backend.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(shutil.which("git") is None, reason="needs git")]

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def _checkout(root: Path) -> Path:
    """A checkout with one file that is CRLF on disk while .gitattributes and
    the index say LF, plus a conftest.py that must never run."""
    root.mkdir()
    subprocess.run([*GIT, "-C", str(root), "init", "-q"], check=True)
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    (root / "a.py").write_bytes(b"x = 1\n")
    (root / "conftest.py").write_bytes(
        f"open({str(root.parent / 'conftest-ran')!r}, 'w').close()\n".encode())
    subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", "init"], check=True)
    (root / "a.py").write_bytes(b"x = 1\r\n")
    subprocess.run([*GIT, "-C", str(root), "add", "a.py"], check=True)
    return root


def test_doctor_dev_reports_eol_mismatch_with_its_fix(evi_cli, tmp_path):
    repo = _checkout(tmp_path / "checkout")
    r = evi_cli("doctor", "--dev", "--repo", str(repo), timeout=120)
    flat = " ".join(r.out.split())  # Rich wraps the details at the console width
    assert "line endings" in flat and "a.py is CRLF, wants LF" in flat
    fix = ("git --literal-pathspecs rm --cached -q -- a.py; "
           "git --literal-pathspecs checkout HEAD -- a.py")
    assert fix in [ln.strip() for ln in r.out.splitlines()]
    assert (repo / "a.py").read_bytes() == b"x = 1\r\n"  # printed, never applied
    # Not the eVi checkout: collection, which would import conftest.py, is skipped.
    assert "pytest --collect-only" in flat and "skipped" in flat
    assert not (tmp_path / "conftest-ran").exists()


def test_doctor_dev_strict_fails_on_a_foreign_venv(evi_cli, tmp_path):
    repo = _checkout(tmp_path / "checkout")
    venv = tmp_path / "venvs" / "foreign"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {tmp_path / 'other-pc' / 'Python313'}\nversion = 3.13.1\n", encoding="utf-8")
    r = evi_cli("doctor", "--dev", "--repo", str(repo), "--venv", str(venv), "--no-collect",
                "--strict", check=False, timeout=120)
    assert r.code == 1, r.out
    flat = " ".join(r.out.split())
    assert "foreign" in flat and "dev-bootstrap" in flat


def test_chat_rejects_an_unknown_mode(evi_cli):
    r = evi_cli("chat", "--mode", "turbo", check=False, timeout=60)
    assert r.code == 2
    assert "unknown mode" in r.out and "code" in r.out
