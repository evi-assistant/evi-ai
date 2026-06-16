"""Tests for the per-session working folder (evi.workdir + fs resolution)."""

from __future__ import annotations

from pathlib import Path

from evi import workdir


def test_resolve_absolute_passes_through(tmp_path):
    p = tmp_path / "a.txt"
    assert workdir.resolve(str(p)) == p


def test_resolve_relative_against_session_cwd(tmp_path):
    tok = workdir.set_cwd(tmp_path)
    try:
        assert workdir.resolve("sub/x.txt") == tmp_path / "sub" / "x.txt"
        assert workdir.get_cwd() == tmp_path
    finally:
        workdir.reset(tok)


def test_resolve_falls_back_to_process_cwd():
    # No session cwd set -> process cwd.
    assert workdir.get_cwd() == Path.cwd()
    assert workdir.resolve("rel.txt") == Path.cwd() / "rel.txt"


def test_read_file_uses_session_cwd(tmp_path):
    from evi.tools.fs import read_file

    (tmp_path / "hello.txt").write_text("hi there", encoding="utf-8")
    tok = workdir.set_cwd(tmp_path)
    try:
        out = read_file("hello.txt")  # relative — resolves under tmp_path
    finally:
        workdir.reset(tok)
    text = out.text if hasattr(out, "text") else out
    assert "hi there" in text


def test_write_file_uses_session_cwd(tmp_path):
    from evi.tools.fs import write_file

    tok = workdir.set_cwd(tmp_path)
    try:
        msg = write_file("out/note.md", "content")
    finally:
        workdir.reset(tok)
    assert (tmp_path / "out" / "note.md").read_text(encoding="utf-8") == "content"
    assert "note.md" in msg


def test_handle_cd_sets_agent_cwd(tmp_path, monkeypatch):
    from evi.apps.cli.main import _handle_cd

    class _A:
        cwd = ""
        history = []
        project = None

    agent = _A()
    target = tmp_path / "proj"
    target.mkdir()
    _handle_cd(agent, str(target), None)
    assert agent.cwd == str(target.resolve())
    # cleanup the contextvar the handler set
    workdir.set_cwd("")


def test_handle_cd_rejects_nonexistent(tmp_path):
    from evi.apps.cli.main import _handle_cd

    class _A:
        cwd = ""
        history = []
        project = None

    agent = _A()
    _handle_cd(agent, str(tmp_path / "nope"), None)
    assert agent.cwd == ""
