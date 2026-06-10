"""Guards for the self-build bootstrap.

eVi develops/builds itself off three artifacts: a root EVI.md (project context),
the unified build scripts, and the self-build guide. If any goes missing or stops
loading, these fail loudly.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_root_evi_md_present_and_loads_as_project_context():
    from evi.project import load_project_context

    assert (ROOT / "EVI.md").is_file()
    ctx = load_project_context(ROOT)
    assert ctx is not None
    text = ctx.text if hasattr(ctx, "text") else str(ctx)
    # the bootstrap essentials are present
    assert "build-desktop" in text
    assert ".venv-build" in text  # the two-venv rule


def test_unified_build_scripts_present():
    assert (ROOT / "scripts" / "build-desktop.ps1").is_file()
    assert (ROOT / "scripts" / "build-desktop.sh").is_file()


def test_build_desktop_chains_sidecar_then_tauri():
    sh = (ROOT / "scripts" / "build-desktop.sh").read_text(encoding="utf-8")
    assert "build-sidecar.sh" in sh and "tauri build" in sh
    ps = (ROOT / "scripts" / "build-desktop.ps1").read_text(encoding="utf-8")
    assert "build-sidecar.ps1" in ps and "tauri build" in ps


def test_self_build_guide_present():
    assert (ROOT / "docs" / "self-build.md").is_file()


def test_build_desktop_ps1_is_ascii():
    # PowerShell 5.1 reads scripts as cp1252; non-ASCII (e.g. em-dash) breaks
    # string terminators. Keep the .ps1 ASCII-only.
    raw = (ROOT / "scripts" / "build-desktop.ps1").read_bytes()
    assert raw.decode("ascii")  # raises if any byte is > 0x7F
