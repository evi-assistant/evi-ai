"""Tests for the first-run setup helpers (Phase 50): per-OS Ollama install
planning, the install runner, and the first-run default-model picker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evi import firstrun  # noqa: E402
from evi.recommend import first_run_model  # noqa: E402


def _hw(*, vram_mb=None, ram_gb=16):
    """Minimal HardwareInfo stand-in (only the attrs recommend() reads)."""
    gpu = SimpleNamespace(vram_total_mb=vram_mb, name="gpu", compute_capability=None) if vram_mb else None
    return SimpleNamespace(primary_gpu=gpu, ram_total_bytes=ram_gb * 1024 * 1024 * 1024)


# --- install plan per OS -------------------------------------------------


def test_plan_windows_winget():
    plan = firstrun.ollama_install_plan(
        system="Windows", which=lambda n: "C:/winget.exe" if n == "winget" else None
    )
    assert plan.available and plan.method == "winget"
    assert plan.command[:2] == ["winget", "install"]
    assert "Ollama.Ollama" in plan.command


def test_plan_windows_no_winget_is_manual():
    plan = firstrun.ollama_install_plan(system="Windows", which=lambda n: None)
    assert plan.available is False
    assert plan.method == "manual"
    assert plan.manual_url


def test_plan_macos_brew():
    plan = firstrun.ollama_install_plan(
        system="Darwin", which=lambda n: "/opt/homebrew/bin/brew" if n == "brew" else None
    )
    assert plan.available and plan.method == "brew"
    assert plan.command == ["brew", "install", "--cask", "ollama"]


def test_plan_macos_no_brew_is_manual():
    plan = firstrun.ollama_install_plan(system="Darwin", which=lambda n: None)
    assert plan.available is False and plan.method == "manual"


def test_plan_linux_curl():
    plan = firstrun.ollama_install_plan(
        system="Linux", which=lambda n: "/usr/bin/curl" if n == "curl" else None
    )
    assert plan.available and plan.method == "install.sh"
    assert "curl -fsSL https://ollama.com/install.sh" in plan.command[-1]


def test_plan_linux_wget_fallback():
    plan = firstrun.ollama_install_plan(
        system="Linux", which=lambda n: "/usr/bin/wget" if n == "wget" else None
    )
    assert plan.available and "wget" in plan.command[-1]


def test_plan_linux_no_downloader_is_manual():
    plan = firstrun.ollama_install_plan(system="Linux", which=lambda n: None)
    assert plan.available is False and plan.method == "manual"


def test_plan_unsupported_os():
    plan = firstrun.ollama_install_plan(system="Plan9", which=lambda n: None)
    assert plan.available is False and plan.method == "unsupported"


# --- install_ollama ------------------------------------------------------


def test_install_dry_run_reports_command(monkeypatch):
    monkeypatch.setattr(
        firstrun, "ollama_install_plan",
        lambda: firstrun.OllamaInstallPlan(available=True, method="winget", command=["winget", "x"]),
    )
    res = firstrun.install_ollama(dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    assert res["command"] == ["winget", "x"]


def test_install_needs_manual_when_no_plan(monkeypatch):
    monkeypatch.setattr(
        firstrun, "ollama_install_plan",
        lambda: firstrun.OllamaInstallPlan(available=False, method="manual", note="no winget"),
    )
    res = firstrun.install_ollama()
    assert res["ok"] is False and res["needs_manual"] is True
    assert res["manual_url"]


def test_install_runs_command_on_success(monkeypatch):
    monkeypatch.setattr(
        firstrun, "ollama_install_plan",
        lambda: firstrun.OllamaInstallPlan(available=True, method="brew", command=["brew", "install", "--cask", "ollama"]),
    )
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = firstrun.install_ollama()
    assert res["ok"] is True
    assert captured["cmd"] == ["brew", "install", "--cask", "ollama"]


def test_install_reports_failure_tail(monkeypatch):
    monkeypatch.setattr(
        firstrun, "ollama_install_plan",
        lambda: firstrun.OllamaInstallPlan(available=True, method="brew", command=["brew", "x"]),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="kaboom"),
    )
    res = firstrun.install_ollama()
    assert res["ok"] is False and "kaboom" in res["message"]


def test_install_handles_timeout(monkeypatch):
    monkeypatch.setattr(
        firstrun, "ollama_install_plan",
        lambda: firstrun.OllamaInstallPlan(available=True, method="brew", command=["brew", "x"]),
    )

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", boom)
    res = firstrun.install_ollama(timeout=1)
    assert res["ok"] is False and "timed out" in res["message"]


# --- first_run_model selection ------------------------------------------


def test_first_run_model_caps_small_on_big_gpu():
    # A 24 GB GPU *could* run 32B, but the first pull should be small + fast.
    assert first_run_model(_hw(vram_mb=24000)) == "qwen2.5:3b-instruct-q4_K_M"


def test_first_run_model_cpu_box_picks_3b():
    assert first_run_model(_hw(vram_mb=None, ram_gb=16)) == "qwen2.5:3b-instruct-q4_K_M"


def test_first_run_model_tiny_box_drops_down():
    m = first_run_model(_hw(vram_mb=None, ram_gb=2))
    assert m in ("qwen2.5:1.5b-instruct-q4_K_M", "llama3.2:1b-instruct-q4_K_M")
