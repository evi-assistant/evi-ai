"""Tests for OS sandbox wrapping (Phase 67).

We don't execute a real sandbox — we mock platform.system + shutil.which and
assert the wrapped argv is shaped correctly per OS.
"""

from __future__ import annotations

from pathlib import Path

import evi.sandbox as sandbox

# wrap() normalises the workdir via Path(); on Windows "/work" -> "\\work", so
# compare against the host-normalised form rather than the literal.
WD = str(Path("/work"))


def _patch(monkeypatch, system, has):
    monkeypatch.setattr(sandbox, "_system", lambda: system)
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: ("/usr/bin/" + n) if n in has else None)


def test_linux_bwrap_wrap(monkeypatch):
    _patch(monkeypatch, "Linux", {"bwrap"})
    out = sandbox.wrap(["python", "x.py"], "/work", allow_network=False)
    assert out[0] == "bwrap"
    assert "--unshare-net" in out
    assert "--bind" in out and WD in out
    assert out[-2:] == ["python", "x.py"]


def test_linux_allow_network_omits_unshare(monkeypatch):
    _patch(monkeypatch, "Linux", {"bwrap"})
    out = sandbox.wrap(["python", "x.py"], "/work", allow_network=True)
    assert "--unshare-net" not in out


def test_macos_sandbox_exec(monkeypatch):
    _patch(monkeypatch, "Darwin", {"sandbox-exec"})
    out = sandbox.wrap(["python", "x.py"], "/work")
    assert out[0] == "sandbox-exec" and out[1] == "-p"
    assert "deny network*" in out[2]
    assert WD in out[2]
    assert out[-2:] == ["python", "x.py"]


def test_no_launcher_returns_argv_unchanged(monkeypatch):
    _patch(monkeypatch, "Linux", set())  # bwrap not present
    argv = ["python", "x.py"]
    assert sandbox.wrap(argv, "/work") == argv


def test_windows_not_available(monkeypatch):
    _patch(monkeypatch, "Windows", {"bwrap", "sandbox-exec"})
    assert sandbox.available() is False
    assert sandbox.wrap(["python", "x.py"], "/work") == ["python", "x.py"]


def test_available_and_status(monkeypatch):
    _patch(monkeypatch, "Linux", {"bwrap"})
    assert sandbox.available() is True
    st = sandbox.status()
    assert st["platform"] == "Linux" and st["launcher"] == "bwrap" and st["available"]
