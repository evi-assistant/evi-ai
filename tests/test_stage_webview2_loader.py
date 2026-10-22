"""scripts/stage_webview2_loader.py: a Windows GNU-toolchain desktop build
ships WebView2Loader.dll next to the exe; MSVC and other OSes need nothing."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "stage_webview2_loader.py"
TAURI = ROOT / "desktop" / "src-tauri"


def _load():
    spec = importlib.util.spec_from_file_location("stage_webview2_loader", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


@pytest.mark.parametrize(("triple", "needed"), [
    ("x86_64-pc-windows-gnu", True),
    ("i686-pc-windows-gnu", True),
    ("aarch64-pc-windows-gnullvm", True),
    ("x86_64-pc-windows-msvc", False),
    ("aarch64-apple-darwin", False),
    ("x86_64-unknown-linux-gnu", False),  # gnu, but not Windows
    (None, False),
])
def test_only_windows_gnu_targets_need_the_loader(mod, triple, needed):
    assert mod.needs_loader(triple) is needed


def test_msvc_prints_nothing_and_stages_nothing(mod, monkeypatch, capsys):
    monkeypatch.setattr(mod, "crate_dir", lambda *a, **k: pytest.fail("looked for the crate"))
    assert mod.main(["--target", "x86_64-pc-windows-msvc"]) == 0
    assert capsys.readouterr().out == ""


def test_gnu_stages_the_arch_matching_dll_and_prints_the_overlay(mod, monkeypatch, tmp_path,
                                                                 capsys):
    crate = tmp_path / "webview2-com-sys-0.39.1"
    for arch in ("x64", "x86", "arm64"):
        (crate / arch).mkdir(parents=True)
        (crate / arch / "WebView2Loader.dll").write_bytes(arch.encode())
    dest = tmp_path / "binaries" / "WebView2Loader.dll"
    monkeypatch.setattr(mod, "crate_dir", lambda *a, **k: crate)
    monkeypatch.setattr(mod, "DEST", dest)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    assert mod.main(["--target", "x86_64-pc-windows-gnu"]) == 0
    assert dest.read_bytes() == b"x64"
    assert capsys.readouterr().out.strip() == mod.OVERLAY
    assert mod.main(["--target", "aarch64-pc-windows-gnullvm"]) == 0
    assert dest.read_bytes() == b"arm64"


def test_a_missing_dll_fails_loudly(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "crate_dir", lambda *a, **k: tmp_path / "empty")
    monkeypatch.setattr(mod, "DEST", tmp_path / "out" / "WebView2Loader.dll")
    assert mod.main(["--target", "x86_64-pc-windows-gnu"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "can't stage WebView2Loader.dll" in captured.err
    assert not (tmp_path / "out" / "WebView2Loader.dll").exists()


def test_the_overlay_bundles_the_staged_dll_at_the_install_root(mod):
    overlay = json.loads((ROOT / "desktop" / mod.OVERLAY).read_text(encoding="utf-8"))
    assert overlay["bundle"]["resources"] == {"binaries/WebView2Loader.dll": "WebView2Loader.dll"}
    # The staged path is that resource's source, relative to src-tauri.
    assert mod.DEST == TAURI / "binaries" / "WebView2Loader.dll"
    # binaries/ is gitignored, so a staged DLL never shows up as a change.
    assert "desktop/src-tauri/binaries/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_the_build_scripts_pass_the_overlay_to_tauri():
    for name in ("build-desktop.ps1", "build-desktop.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "stage_webview2_loader.py" in text, name
        assert "tauri.standalone.conf.json" in text, name
