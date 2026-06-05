"""Tests for scripts/evi_tools.py — the binary provisioner.

It lives in scripts/ (outside the package), so we load it by path. We only
exercise the pure planning functions; nothing here installs anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evi_tools.py"


def _load():
    spec = importlib.util.spec_from_file_location("evi_tools", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


et = _load()


def test_tools_bin_dir_honours_evi_home(monkeypatch, tmp_path):
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    assert et.tools_bin_dir() == tmp_path / "tools" / "bin"


def test_pkg_install_command_winget_tesseract():
    argv = et.pkg_install_command("tesseract", "winget")
    assert argv[:2] == ["winget", "install"]
    assert "UB-Mannheim.TesseractOCR" in argv


def test_pkg_install_command_apt_ffmpeg():
    argv = et.pkg_install_command("ffmpeg", "apt-get")
    assert argv == ["sudo", "apt-get", "install", "-y", "ffmpeg"]


def test_pkg_install_command_brew_ollama():
    argv = et.pkg_install_command("ollama", "brew")
    assert argv == ["brew", "install", "ollama"]


def test_pkg_install_command_none_when_not_packaged():
    # Ollama is not in apt → None (falls back to the official script).
    assert et.pkg_install_command("ollama", "apt-get") is None


def test_plan_install_prefers_pkg_when_available():
    kind, detail = et.plan_install("ffmpeg", "brew")
    assert kind == "pkg"
    assert detail == ["brew", "install", "ffmpeg"]


def test_plan_install_ollama_linux_script_when_no_pkg(monkeypatch):
    monkeypatch.setattr(et, "os_kind", lambda: "linux")
    kind, detail = et.plan_install("ollama", "apt-get")
    assert kind == "script"
    assert "ollama.com/install.sh" in detail


def test_plan_install_ffmpeg_download_when_no_pm(monkeypatch):
    monkeypatch.setattr(et, "os_kind", lambda: "linux")
    kind, detail = et.plan_install("ffmpeg", None)
    assert kind == "download"
    assert detail.endswith(".tar.xz")


def test_plan_install_tesseract_manual_when_no_pm(monkeypatch):
    monkeypatch.setattr(et, "os_kind", lambda: "linux")
    kind, detail = et.plan_install("tesseract", None)
    assert kind == "manual"
    assert detail.startswith("https://")


def test_known_tools_have_required_fields():
    for name, info in et.TOOLS.items():
        assert "binary" in info and "purpose" in info and "pkg" in info


def test_main_list_runs(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    rc = et.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tesseract" in out and "ffmpeg" in out


def test_main_install_dry_run_no_side_effects(capsys, monkeypatch):
    monkeypatch.setattr(et, "detect_package_manager", lambda: "brew")
    monkeypatch.setattr(et, "_found", lambda b: None)  # pretend not installed
    rc = et.main(["install", "ffmpeg", "--dry-run"])
    assert rc == 0
    assert "brew install ffmpeg" in capsys.readouterr().out
