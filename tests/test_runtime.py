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
              "server_running", "runtime_installed", "supported", "active_model_id"):
        assert k in s


def test_starter_model_path_under_models_dir(monkeypatch, tmp_path):
    from evi.runtime import catalog as cat

    monkeypatch.setattr(cat, "MODELS_DIR", tmp_path)
    p = rt_setup.starter_model_path()
    assert p.parent.parent == tmp_path
    assert p.name.endswith(".gguf")


def test_catalog_entries_wellformed():
    from evi.runtime import catalog as cat

    models = cat.catalog()
    assert len(models) >= 6
    ids = [m["id"] for m in models]
    assert cat.STARTER_ID in ids
    assert len(ids) == len(set(ids))  # unique ids
    for m in models:
        for k in ("id", "name", "hf_repo", "filename", "quant", "size_gb",
                  "params_b", "min_ram_gb", "min_vram_gb", "license"):
            assert k in m, f"{m['id']} missing {k}"
        assert m["filename"].endswith(".gguf")


def test_get_unknown_returns_none():
    from evi.runtime import catalog as cat

    assert cat.get("nope") is None


def test_recommended_scales_with_memory(monkeypatch):
    from evi.runtime import catalog as cat

    monkeypatch.setattr(cat, "_memory_gb", lambda: (2.0, 0.0))
    small = cat.get(cat.recommended_id())
    assert "coding" not in small.get("tags", [])  # never recommend a specialist

    monkeypatch.setattr(cat, "_memory_gb", lambda: (64.0, 0.0))
    big = cat.get(cat.recommended_id())
    assert big["params_b"] >= small["params_b"]  # more memory -> at least as large


def test_cuda_build_table():
    v = rt_bin.LLAMACPP_VERSION  # noqa: F841 — referenced for clarity
    assert rt_bin._cuda_build_for("12.0") == "13.3"   # Blackwell sm_120 needs >=12.8
    assert rt_bin._cuda_build_for("12.5") == "13.3"
    assert rt_bin._cuda_build_for("8.9") == "12.4"    # Ada
    assert rt_bin._cuda_build_for("6.1") == "12.4"
    assert rt_bin._cuda_build_for("3.5") is None      # pre-Maxwell, too old
    assert rt_bin._cuda_build_for(None) is None
    assert rt_bin._cuda_build_for("garbage") is None


def test_gpu_plan_per_platform():
    assert rt_bin.gpu_plan(system="Darwin", machine="arm64", compute_cap=None) == {"mode": "metal"}
    p = rt_bin.gpu_plan(system="Windows", machine="AMD64", compute_cap="12.0")
    assert p["mode"] == "cuda" and p["build"] == "13.3" and len(p["assets"]) == 2
    assert rt_bin.gpu_plan(system="Windows", machine="AMD64", compute_cap="8.9")["build"] == "12.4"
    assert rt_bin.gpu_plan(system="Windows", machine="AMD64", compute_cap=None) is None
    assert rt_bin.gpu_plan(system="Linux", machine="x86_64", compute_cap="8.9") is None


def test_gpu_root_under_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(rt_bin, "RUNTIME_DIR", tmp_path)
    assert rt_bin.gpu_root() == rt_bin.runtime_root() / "gpu"
    assert rt_bin.gpu_server_path() is None
    assert rt_bin.gpu_installed() is False


def test_ensure_running_noop_when_not_llamacpp(monkeypatch):
    from evi.config import Config

    c = Config()
    c.llm.backend = "ollama"
    monkeypatch.setattr(rt_setup, "Config", type("C", (), {"load": staticmethod(lambda: c)}))
    # backend != llamacpp -> returns immediately, never probes/spawns anything.
    assert rt_setup.ensure_running() is False
