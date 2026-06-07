"""First-run helpers: get a brand-new user from zero to first chat.

A fresh eVi install has no LLM backend, so the web/desktop UI shows a
"no backend" banner. This module powers the one-click setup path behind it:
install Ollama (per-OS, package-manager-first), then auto-pull a small default
model. We deliberately do NOT bundle a runtime — the multi-GB *model* download
is the real first-run cost, and bundling would balloon the installer 5–10x for
no real gain (see docs/roadmap.md, Phase 50).

Everything here is dependency-light and the platform detection is injectable so
the install logic is unit-testable without actually installing anything.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field

MANUAL_DOWNLOAD_URL = "https://ollama.com/download"


@dataclass(frozen=True)
class OllamaInstallPlan:
    """How (or whether) we can install Ollama on this machine, unattended."""

    available: bool            # can we run an unattended install here?
    method: str                # "winget" | "brew" | "install.sh" | "manual" | "unsupported"
    command: list[str] = field(default_factory=list)  # argv for the install
    manual_url: str = MANUAL_DOWNLOAD_URL
    note: str = ""


def ollama_install_plan(
    *,
    system: str | None = None,
    which=shutil.which,
) -> OllamaInstallPlan:
    """Decide how to install Ollama on this OS, package-manager-first.

    `system` / `which` are injectable for testing (default to the real
    platform + PATH lookup). Falls back to a manual-download plan when no
    unattended path exists (e.g. Windows without winget, macOS without brew).
    """
    sys_name = (system or platform.system()).lower()

    if sys_name == "windows":
        if which("winget"):
            return OllamaInstallPlan(
                available=True,
                method="winget",
                command=[
                    "winget", "install", "--id", "Ollama.Ollama", "-e",
                    "--silent",
                    "--accept-package-agreements", "--accept-source-agreements",
                ],
                note="Installing Ollama via winget.",
            )
        return OllamaInstallPlan(
            available=False, method="manual",
            note="winget isn't available — download the Ollama installer manually.",
        )

    if sys_name == "darwin":
        if which("brew"):
            return OllamaInstallPlan(
                available=True,
                method="brew",
                command=["brew", "install", "--cask", "ollama"],
                note="Installing Ollama via Homebrew.",
            )
        return OllamaInstallPlan(
            available=False, method="manual",
            note="Homebrew isn't available — download the Ollama app manually.",
        )

    if sys_name == "linux":
        if which("curl"):
            return OllamaInstallPlan(
                available=True,
                method="install.sh",
                command=["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                note="Installing Ollama via the official install script.",
            )
        if which("wget"):
            return OllamaInstallPlan(
                available=True,
                method="install.sh",
                command=["sh", "-c", "wget -qO- https://ollama.com/install.sh | sh"],
                note="Installing Ollama via the official install script.",
            )
        return OllamaInstallPlan(
            available=False, method="manual",
            note="Neither curl nor wget is available — install Ollama manually.",
        )

    return OllamaInstallPlan(
        available=False, method="unsupported",
        note=f"Automatic Ollama install isn't supported on {sys_name!r}.",
    )


def install_ollama(*, timeout: float = 600.0, dry_run: bool = False) -> dict:
    """Run the unattended Ollama install for this OS.

    Returns a JSON-able dict: always has `ok` (bool) and `method`; on failure
    also `message` and (when applicable) `needs_manual` + `manual_url`. With
    `dry_run=True` it reports the command it *would* run without executing it —
    handy for tests and `--dry-run` style callers.
    """
    plan = ollama_install_plan()

    if not plan.available:
        return {
            "ok": False,
            "needs_manual": True,
            "method": plan.method,
            "manual_url": plan.manual_url,
            "message": plan.note,
        }

    if dry_run:
        return {"ok": True, "dry_run": True, "method": plan.method, "command": plan.command}

    try:
        proc = subprocess.run(
            plan.command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {
            "ok": False, "needs_manual": True, "method": plan.method,
            "manual_url": plan.manual_url,
            "message": f"{plan.command[0]} not found on PATH.",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "method": plan.method,
            "message": f"Ollama install timed out after {int(timeout)}s.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "method": plan.method, "message": f"install failed: {exc}"}

    if proc.returncode == 0:
        return {"ok": True, "method": plan.method, "message": "Ollama installed."}
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    return {
        "ok": False,
        "method": plan.method,
        "returncode": proc.returncode,
        "manual_url": plan.manual_url,
        "message": "Ollama install failed: " + " ".join(tail) if tail
                   else f"Ollama install exited {proc.returncode}.",
    }


def recommended_model() -> str:
    """The model to auto-pull on first run, chosen for the detected hardware."""
    from evi.hardware import detect
    from evi.recommend import first_run_model

    return first_run_model(detect())
