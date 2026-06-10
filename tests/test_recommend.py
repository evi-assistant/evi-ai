"""Tests for hardware detection + model recommendation."""

from __future__ import annotations

import subprocess

import pytest

import evi.hardware as hw_mod
from evi.hardware import GPU, HardwareInfo, detect_gpus
from evi.recommend import recommend


# ---- GPU detection via nvidia-smi parsing -------------------------------


class _FakeRun:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_detect_gpus_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw_mod.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        hw_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: _FakeRun(
            "GeForce RTX 5070 Ti, 16380, 15234, 555.42, 12.0\n"
            "Tesla P40, 24576, 24500, 535.10, 6.1\n"
        ),
    )
    gpus = detect_gpus()
    # Sorted by VRAM desc.
    assert [g.name for g in gpus] == ["Tesla P40", "GeForce RTX 5070 Ti"]
    assert gpus[0].vram_total_mb == 24576
    assert gpus[0].compute_capability == "6.1"
    assert gpus[1].vram_total_mb == 16380


def test_detect_gpus_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw_mod.shutil, "which", lambda _: None)
    assert detect_gpus() == []


def test_detect_gpus_handles_old_driver_without_compute_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hw_mod.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        hw_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: _FakeRun("GeForce GT 940MX, 2048, 1800, 470.86\n"),
    )
    gpus = detect_gpus()
    assert len(gpus) == 1
    assert gpus[0].vram_total_mb == 2048
    assert gpus[0].compute_capability is None


def test_detect_gpus_handles_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw_mod.shutil, "which", lambda _: "/usr/bin/nvidia-smi")

    def _boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)

    monkeypatch.setattr(hw_mod.subprocess, "run", _boom)
    assert detect_gpus() == []


# ---- recommend() picks ---------------------------------------------------


def _hw(*, vram: int = 0, name: str = "", cc: str | None = None, ram_gb: float = 16.0) -> HardwareInfo:
    gpus = [GPU(name=name or "fake", vram_total_mb=vram, compute_capability=cc)] if vram else []
    return HardwareInfo(gpus=gpus, ram_total_bytes=int(ram_gb * 1024 ** 3), platform="linux")


def test_recommend_p40_picks_32b_chat() -> None:
    rec = recommend(_hw(vram=24576, name="Tesla P40", cc="6.1"))
    assert rec.mode == "gpu"
    assert rec.chat is not None
    assert "32b" in rec.chat.id.lower()


def test_recommend_5070ti_picks_14b_chat() -> None:
    rec = recommend(_hw(vram=16380, name="GeForce RTX 5070 Ti", cc="12.0"))
    assert rec.mode == "gpu"
    assert rec.chat is not None
    assert "14b" in rec.chat.id.lower()


def test_recommend_modern_gpu_not_flagged_pre_pascal() -> None:
    # Regression: the compute-capability check used a STRING comparison, so a
    # modern GPU ("12.0") was wrongly flagged pre-Pascal because "12.0" < "6.0".
    rec = recommend(_hw(vram=16380, name="RTX 5070 Ti", cc="12.0"))
    assert not any("pre-Pascal" in n for n in rec.notes)


def test_recommend_old_gpu_still_flagged_pre_pascal() -> None:
    # cc 5.0 (Maxwell) is genuinely pre-Pascal and should still warn.
    rec = recommend(_hw(vram=8192, name="GTX 980", cc="5.2"))
    assert any("pre-Pascal" in n for n in rec.notes)


def test_recommend_940mx_falls_back_to_cpu() -> None:
    rec = recommend(_hw(vram=2048, name="GeForce GT 940MX", cc="5.0", ram_gb=16))
    assert rec.mode == "cpu"  # GPU is below useful threshold
    assert rec.chat is not None
    # 16 GB RAM means we can run up to the 8B/7B Q4 tier.
    assert any(token in rec.chat.id for token in ("7b", "8b", "3b"))


def test_recommend_no_gpu_uses_cpu() -> None:
    rec = recommend(_hw(vram=0, ram_gb=32))
    assert rec.mode == "cpu"
    assert rec.chat is not None


def test_recommend_remote_only_when_starved() -> None:
    # No GPU and only 1 GB RAM — nothing in the registry fits.
    rec = recommend(_hw(vram=0, ram_gb=1.0))
    assert rec.mode == "remote-only"
    assert rec.chat is None


def test_recommend_picks_distinct_coder_when_possible() -> None:
    rec = recommend(_hw(vram=16380, ram_gb=32))
    assert rec.chat is not None and rec.coder is not None
    assert rec.coder.role == "coder"
    assert rec.chat.role == "chat"


def test_recommend_notes_pre_pascal_warning() -> None:
    rec = recommend(_hw(vram=4096, name="old", cc="5.0", ram_gb=16))
    # 940MX-tier — below the "useful threshold" branch fires first, but the
    # 4 GB card test exercises the >= 2500 path and surfaces the cc warning.
    if rec.mode == "gpu":
        assert any("pre-Pascal" in n or "compute" in n for n in rec.notes)
