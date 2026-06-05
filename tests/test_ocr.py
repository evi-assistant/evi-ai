"""Tests for the OCR tool (Tesseract subprocess mocked)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import evi.tools.ocr as ocr_mod
from evi.tools.base import REGISTRY


def test_missing_tesseract_returns_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: None)
    # Make the env/home fallbacks resolve to nothing.
    monkeypatch.delenv("EVI_TESSERACT_CMD", raising=False)
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    fake = Path("/tmp/fake.png")
    out = REGISTRY["ocr_image"].call(json.dumps({"path": str(fake)}))
    assert out.startswith("ERROR:")
    assert "tesseract" in out.lower()
    assert "install" in out.lower()


def test_missing_image_file_returns_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")
    out = REGISTRY["ocr_image"].call(
        json.dumps({"path": str(tmp_path / "nope.png")})
    )
    assert out.startswith("ERROR:")
    assert "no such file" in out.lower()


def test_happy_path_returns_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG-fake")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")

    def fake_run(cmd, **kwargs):
        # Validate the command shape so we know we're invoking tesseract the
        # way we mean to (file, "stdout", "-l", language).
        assert cmd[0].endswith("tesseract")
        assert cmd[1] == str(img)
        assert cmd[2] == "stdout"
        assert cmd[3] == "-l"
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout="Hello, world!\nSecond line.\n",
            stderr="",
        )

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)
    out = REGISTRY["ocr_image"].call(json.dumps({"path": str(img)}))
    assert "Hello, world!" in out
    assert "Second line." in out


def test_language_arg_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    img = tmp_path / "page.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)
    REGISTRY["ocr_image"].call(json.dumps({"path": str(img), "language": "deu"}))
    assert captured["cmd"][-1] == "deu"


def test_nonzero_exit_returns_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    img = tmp_path / "page.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="couldn't open image",
        )

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)
    out = REGISTRY["ocr_image"].call(json.dumps({"path": str(img)}))
    assert "ERROR" in out
    assert "couldn't open image" in out


def test_timeout_returns_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    img = tmp_path / "page.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=ocr_mod._TIMEOUT_SECONDS)

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)
    out = REGISTRY["ocr_image"].call(json.dumps({"path": str(img)}))
    assert "timed out" in out.lower()


def test_tesseract_cmd_prefers_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """$EVI_TESSERACT_CMD (pointing at a real file) wins over PATH."""
    fake_bin = tmp_path / "tess-bin"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("EVI_TESSERACT_CMD", str(fake_bin))
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")
    assert ocr_mod._tesseract_cmd() == str(fake_bin)


def test_tesseract_cmd_env_ignored_when_not_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVI_TESSERACT_CMD", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")
    # Falls through to PATH.
    assert ocr_mod._tesseract_cmd() == "/usr/bin/tesseract"


def test_tesseract_cmd_finds_evi_tools_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A binary dropped by `evi-tools install tesseract` is found without PATH."""
    monkeypatch.delenv("EVI_TESSERACT_CMD", raising=False)
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    exe = "tesseract.exe" if ocr_mod.os.name == "nt" else "tesseract"
    bindir = tmp_path / "tools" / "bin"
    bindir.mkdir(parents=True)
    (bindir / exe).write_text("x", encoding="utf-8")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: None)
    assert ocr_mod._tesseract_cmd() == str(bindir / exe)


def test_empty_output_returns_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    img = tmp_path / "page.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(
        ocr_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    out = REGISTRY["ocr_image"].call(json.dumps({"path": str(img)}))
    assert "no text" in out.lower()
