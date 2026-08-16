"""Managed llama.cpp runtime — asset resolution, setup state, autostart guard.

Pure logic: no network, no subprocess, injectable platform.
"""

from __future__ import annotations

import pytest

from evi.runtime import llamacpp_runtime as rt_bin
from evi.runtime import setup as rt_setup


def test_cpu_asset_per_platform():
    v = rt_bin.LLAMACPP_VERSION
    assert rt_bin.cpu_asset("Windows", "AMD64") == f"llama-{v}-bin-win-cpu-x64.zip"
    assert rt_bin.cpu_asset("Windows", "ARM64") == f"llama-{v}-bin-win-cpu-arm64.zip"
    assert rt_bin.cpu_asset("Linux", "x86_64") == f"llama-{v}-bin-ubuntu-x64.tar.gz"
    assert rt_bin.cpu_asset("Linux", "aarch64") == f"llama-{v}-bin-ubuntu-arm64.tar.gz"
    assert rt_bin.cpu_asset("Darwin", "arm64") == f"llama-{v}-bin-macos-arm64.tar.gz"
    assert rt_bin.cpu_asset("Darwin", "x86_64") == f"llama-{v}-bin-macos-x64.tar.gz"
    # arch aliases normalize
    assert rt_bin.cpu_asset("linux", "X86_64") == rt_bin.cpu_asset("Linux", "amd64")


def test_cpu_asset_unsupported():
    assert rt_bin.cpu_asset("Plan9", "riscv") is None
    assert rt_bin.cpu_asset("Linux", "s390x") is None


def test_runtime_root_and_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    assert rt_bin.runtime_root() == tmp_path / "llama" / rt_bin.LLAMACPP_VERSION
    assert rt_bin.server_path() is None
    assert rt_bin.is_installed() is False


def test_ensure_runtime_unsupported_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt_bin.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(rt_bin.platform, "machine", lambda: "riscv")
    with pytest.raises(RuntimeError):
        rt_bin.ensure_runtime()


def test_setup_status_shape():
    s = rt_setup.status()
    for k in ("stage", "pct", "message", "running", "done", "error",
              "server_running", "runtime_installed", "model_present", "supported"):
        assert k in s


def test_starter_model_path_under_models_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(rt_setup, "MODELS_DIR", tmp_path)
    p = rt_setup.starter_model_path()
    assert p.parent.parent == tmp_path
    assert p.name.endswith(".gguf")


def test_ensure_running_noop_when_not_llamacpp(monkeypatch):
    from evi.config import Config

    c = Config()
    c.llm.backend = "ollama"
    monkeypatch.setattr(rt_setup, "Config", type("C", (), {"load": staticmethod(lambda: c)}))
    # backend != llamacpp -> returns immediately, never probes/spawns anything.
    assert rt_setup.ensure_running() is False
