"""scripts/bundle_vc_runtime.py: the frozen sidecar carries the VC++ runtime
its native modules need, from a Microsoft-signed copy, or the build fails."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "bundle_vc_runtime.py"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("bundle_vc_runtime", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize(("name", "ok"), [
    ("msvcp140.dll", True),
    ("MSVCP140.dll", True),
    ("msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll", True),  # numpy.libs' copy
    ("msvcp140_1.dll", False),  # a different DLL
    ("msvcp140_codecvt_ids.dll", False),
    ("msvcp140.dll.bak", False),
])
def test_a_wheel_mangled_copy_matches_but_sibling_dlls_dont(mod, name, ok):
    assert bool(mod._mangled("MSVCP140.dll").fullmatch(name)) is ok


def _sig(table):
    return lambda paths: {str(p): table[p.name] for p in paths if p.name in table}


def test_pick_takes_the_newest_microsoft_signed_copy(mod, monkeypatch, tmp_path):
    old, new, forged, unsigned = (tmp_path / n for n in ("a.dll", "b.dll", "c.dll", "d.dll"))
    monkeypatch.setattr(mod, "candidates", lambda name: [old, new, forged, unsigned])
    table = {
        "a.dll": {"valid": True, "microsoft": True, "version": "14.29.30133.0"},
        "b.dll": {"valid": True, "microsoft": True, "version": "14.40.33810.0"},
        "c.dll": {"valid": True, "microsoft": False, "version": "99.0"},  # someone else's
        "d.dll": {"valid": False, "microsoft": True, "version": "98.0"},  # tampered
    }
    assert mod.pick("MSVCP140.dll", check=_sig(table)) == new
    table["b.dll"]["valid"] = False
    assert mod.pick("MSVCP140.dll", check=_sig(table)) == old
    assert mod.pick("MSVCP140.dll", check=lambda paths: {}) is None


@pytest.mark.skipif(os.name != "nt", reason="the runtime is only bundled on Windows")
def test_main_fills_only_what_is_missing_and_fails_without_a_source(mod, monkeypatch, tmp_path,
                                                                   capsys):
    internal = tmp_path / "evi-server" / "_internal"
    internal.mkdir(parents=True)
    (internal / "vcruntime140.dll").write_bytes(b"have")  # case-insensitive match
    (internal / "VCRUNTIME140_1.dll").write_bytes(b"have")
    src = tmp_path / "numpy.libs" / "msvcp140-abc123.dll"
    src.parent.mkdir()
    src.write_bytes(b"msvcp")
    asked = []
    monkeypatch.setattr(mod, "pick", lambda name: asked.append(name) or src)
    assert mod.main([str(tmp_path / "evi-server")]) == 0
    assert asked == ["MSVCP140.dll"]
    assert (internal / "MSVCP140.dll").read_bytes() == b"msvcp"
    # Nothing to find: the build fails and says how to fix it.
    (internal / "MSVCP140.dll").unlink()
    monkeypatch.setattr(mod, "pick", lambda name: None)
    assert mod.main([str(tmp_path / "evi-server")]) == 1
    assert "Visual C++ Redistributable" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="the runtime is only bundled on Windows")
def test_main_refuses_a_folder_that_is_not_a_pyinstaller_onedir(mod, tmp_path):
    assert mod.main([str(tmp_path)]) == 2


def test_the_sidecar_builds_bundle_the_runtime_and_gate_on_the_self_check():
    for name in ("build-sidecar.ps1", "build-sidecar.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "bundle_vc_runtime.py" in text, name
        assert "--check" in text, name
        # Bundled before the gate, and both before staging.
        assert text.index("bundle_vc_runtime.py") < text.index("--check") < text.index(
            "Stage the whole onedir folder"), name


@pytest.mark.skipif(os.name != "nt", reason="Authenticode is Windows-only")
def test_the_real_signature_check_reads_microsoft_dlls_and_rejects_others(mod, tmp_path):
    import sys

    runtime = Path(sys.base_prefix) / "vcruntime140.dll"  # ships with Python
    fake = tmp_path / "msvcp140.dll"
    fake.write_bytes(b"MZ not really a dll")
    info = mod.signature([runtime, fake])
    assert info[str(runtime)]["valid"] and info[str(runtime)]["microsoft"]
    assert info[str(runtime)]["version"].startswith("14.")
    assert not info[str(fake)]["valid"]


def test_a_failing_signature_check_is_reported_not_raised(mod, monkeypatch, tmp_path, capsys):
    import subprocess

    def boom(paths):
        raise subprocess.CalledProcessError(1, "powershell", stderr="no powershell here")

    monkeypatch.setattr(mod, "candidates", lambda name: [tmp_path / "x.dll"])
    assert mod.pick("MSVCP140.dll", check=boom) is None
    assert "no powershell here" in capsys.readouterr().err


def test_the_same_file_found_twice_is_one_candidate(mod, monkeypatch, tmp_path):
    sp = tmp_path / "site-packages"
    (sp / "numpy.libs").mkdir(parents=True)
    (sp / "numpy.libs" / "msvcp140-abc.dll").write_bytes(b"x")
    monkeypatch.setattr(mod.site, "getsitepackages", lambda: [str(sp), str(sp) + os.sep])
    monkeypatch.setattr(mod.site, "getusersitepackages", lambda: str(tmp_path / "none"))
    monkeypatch.setattr(mod.sys, "base_prefix", str(tmp_path / "py"))
    monkeypatch.setattr(mod.sys, "prefix", str(tmp_path / "py"))
    monkeypatch.setenv("SystemRoot", str(tmp_path / "win"))
    assert mod.candidates("MSVCP140.dll") == [sp / "numpy.libs" / "msvcp140-abc.dll"]
