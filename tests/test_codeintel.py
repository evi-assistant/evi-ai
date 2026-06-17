"""Tests for local code intelligence (formatters/linters) + format-on-edit."""

from __future__ import annotations

import evi.codeintel as codeintel
from evi import workdir


def test_format_file_no_formatter_for_unknown_ext(tmp_path):
    f = tmp_path / "x.unknownext"
    f.write_text("data", encoding="utf-8")
    assert codeintel.format_file(f) == (False, "")


def test_format_file_skips_when_tool_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(codeintel.shutil, "which", lambda _name: None)
    f = tmp_path / "a.py"
    f.write_text("x=1", encoding="utf-8")
    ran, _tool = codeintel.format_file(f)
    assert ran is False


def test_format_file_runs_available_tool(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(codeintel.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(argv, **kw):
        calls["argv"] = argv

        class R:
            pass

        return R()

    monkeypatch.setattr(codeintel.subprocess, "run", fake_run)
    f = tmp_path / "a.py"
    f.write_text("x=1", encoding="utf-8")
    ran, tool = codeintel.format_file(f)
    assert ran is True and tool == "ruff"
    assert calls["argv"][:2] == ["ruff", "format"]


def test_diagnose_no_linter_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(codeintel.shutil, "which", lambda _name: None)
    f = tmp_path / "a.py"
    f.write_text("x=1", encoding="utf-8")
    out = codeintel.diagnose(f)
    assert "no linter installed" in out


def test_diagnose_reports_output(tmp_path, monkeypatch):
    monkeypatch.setattr(codeintel.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(argv, **kw):
        class R:
            returncode = 1  # linters exit non-zero when they find issues
            stdout = "a.py:1:1 F401 unused import"
            stderr = ""

        return R()

    monkeypatch.setattr(codeintel.subprocess, "run", fake_run)
    f = tmp_path / "a.py"
    f.write_text("import os", encoding="utf-8")
    assert "F401" in codeintel.diagnose(f)


def test_format_on_edit_hook(tmp_path, monkeypatch):
    import evi.config as config_mod
    from evi.tools.fs import write_file

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[tools]\nformat_on_edit = true\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(codeintel, "format_file", lambda p: (True, "ruff"))

    tok = workdir.set_cwd(tmp_path)
    try:
        out = write_file("a.py", "x=1\n")
    finally:
        workdir.reset(tok)
    assert "formatted with ruff" in out


def test_check_file_tool(tmp_path, monkeypatch):
    from evi.tools.code import check_file

    monkeypatch.setattr(codeintel, "diagnose", lambda p: "clean")
    f = tmp_path / "a.py"
    f.write_text("x=1", encoding="utf-8")
    assert check_file(str(f)) == "clean"
    assert check_file(str(tmp_path / "nope.py")).startswith("ERROR")
