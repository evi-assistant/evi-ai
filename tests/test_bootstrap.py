"""Guards for the self-build bootstrap.

eVi develops/builds itself off three artifacts: a root EVI.md (project context),
the unified build scripts, and the self-build guide. If any goes missing or stops
loading, these fail loudly.

EVI.md is loaded into eVi's own prompt and the implementer follows it, so it is
also kept short and free of facts that rot: a hand-typed test count, a paused
release, an old checkout path. Facts belong in commands (`pytest --collect-only`,
`evi doctor --dev`), not in the file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVI_MD = ROOT / "EVI.md"
SELF_BUILD = ROOT / "docs" / "self-build.md"
EVI_MD_BUDGET = 4096  # bytes

# (what, pattern) pairs that must never appear in EVI.md or docs/self-build.md.
_STALE = [
    ("a paused or held release",
     re.compile(r"\bpaus(?:e|ed|es|ing)\b|\bon\s+hold\b", re.IGNORECASE)),
    ("a frozen release",
     re.compile(r"\breleases?\b[^.\n]*\b(?:frozen|blocked|halted|suspended)\b",
                re.IGNORECASE)),
    # A number up to three words before "tests" / "cases" / "passing":
    # "980+ passing", "1788 e2e and unit tests", "~1.8k tests", "(1,788 cases)".
    # (?!/): `--timeout=180 tests/e2e` is a path. A number ends in a digit, so a
    # numbered step ("3. eVi runs the tests") is not a count.
    ("a hand-typed test count",
     re.compile(r"\d(?:[\d,.]*\d)?\s*k?\+?\s+(?:[\w-]+\s+){0,3}?(?:tests?|cases|passing|passed)\b(?!/)",
                re.IGNORECASE)),
    ("a hand-typed test count", re.compile(r"\btest\s+count\b\W{0,3}\d", re.IGNORECASE)),
    ("the old C:/evi checkout path", re.compile(r"\bC:[\\/]+evi\b", re.IGNORECASE)),
    ("manual wiki edits",
     re.compile(r"\b(?:mirror|cop(?:y|ies|ied|ying)|sync|push|upload)\w*\b[^.\n]*\bwiki\b"
                r"|\bwiki\b[^.\n]*\b(?:by hand|manually)\b", re.IGNORECASE)),
]
# A version number goes stale with the next release; EVI.md must not carry one.
_VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")
# Nor any other big number, however it is phrased: a count of three or more
# digits ("1788", "1,788") or thousands ("1.8k"). Flags with values are fine.
_BIG_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{3,}\b|\b\d+(?:\.\d+)?k\b", re.IGNORECASE)
_NUMBER_ALLOWED = re.compile(r"--timeout=\d+")


def stale_facts(text: str) -> list[str]:
    return [f"{what}: {m.group(0)!r}" for what, rx in _STALE for m in rx.finditer(text)]


def big_numbers(text: str) -> list[str]:
    return _BIG_NUMBER.findall(_NUMBER_ALLOWED.sub("", text))


def test_root_evi_md_present_and_loads_as_project_context():
    from evi.project import load_project_context

    assert EVI_MD.is_file()
    ctx = load_project_context(ROOT)
    assert ctx is not None
    text = ctx.text if hasattr(ctx, "text") else str(ctx)
    # the bootstrap essentials are present
    assert "build-desktop" in text
    assert ".venv-build" in text  # the two-venv rule


def test_evi_md_fits_its_budget():
    size = len(EVI_MD.read_bytes())
    assert size <= EVI_MD_BUDGET, (
        f"EVI.md is {size} bytes, over the {EVI_MD_BUDGET}-byte budget: it is loaded "
        "into every prompt. Move procedures to docs/self-build.md."
    )


@pytest.mark.parametrize("path", [EVI_MD, SELF_BUILD], ids=["EVI.md", "self-build.md"])
def test_no_stale_facts(path):
    assert stale_facts(path.read_text(encoding="utf-8")) == []


def test_evi_md_has_no_version_numbers():
    assert _VERSION.findall(EVI_MD.read_text(encoding="utf-8")) == []


def test_evi_md_has_no_big_numbers():
    # Structural, so a rewording the patterns above miss still fails here.
    assert big_numbers(EVI_MD.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("sample", [
    "Releases are **PAUSED** on a billing block.",
    "The test suite is green (`pytest -q` ≈ 980+ passing).",
    "Verified: 2465 unit passed",
    "fast (~1,300+ tests)",
    r"You are working on eVi itself, at `C:\evi`.",
    "run eVi from C:/evi",
    "mirror the touched files to the evi-assistant/evi-ai wiki",
    # rewordings the first version of the detector let through
    "Test count: 1788.",
    "The suite has 1788 e2e and unit tests.",
    "~1.8k tests pass",
    "(1,788 cases)",
    "Releases are on hold until billing is fixed.",
    "releases PAUSE until further notice",
    "copy the docs to the GitHub wiki by hand",
    "update the wiki manually after merging",
    "Releases are frozen until the audit ends.",
])
def test_stale_fact_detector_catches(sample):
    assert stale_facts(sample)


@pytest.mark.parametrize("sample", [
    "Test count: 1788.", "The suite has 1788 e2e and unit tests.", "~1.8k tests pass",
    "(1,788 cases)", "about 2,465 of them",
])
def test_big_number_detector_catches(sample):
    assert big_numbers(sample)


@pytest.mark.parametrize("sample", [
    "Python 3.13 in a uv-managed venv", "under 4 KB", "python -m pytest -q -n auto",
    "the test count: `python -m pytest --collect-only -q` (last line)",
    "python -m pytest -q -m e2e --timeout=180 tests/cli_e2e",
    "docs publish to the wiki automatically from main",
    "Run the subset covering the files you touched.",
    "nobody edits the wiki directly",
])
def test_stale_fact_detector_allows(sample):
    assert stale_facts(sample) == []
    assert big_numbers(sample) == []


def test_evi_md_is_the_implementer_contract():
    text = EVI_MD.read_text(encoding="utf-8")
    for section in ("## Never", "## Workflow", "## Facts are commands",
                    "## Commit message", "## CHANGELOG voice", "## Line endings"):
        assert section in text, section
    for rule in ("Commit to `main`", "tag, release", "set a task's status",
                 ".github/workflows/", "~/.evi", "record_fix", "Hand-off",
                 "dev-bootstrap --worktree", "evi doctor --dev", "Agent.chat"):
        assert rule in text, rule


def test_unified_build_scripts_present():
    assert (ROOT / "scripts" / "build-desktop.ps1").is_file()
    assert (ROOT / "scripts" / "build-desktop.sh").is_file()


def test_build_desktop_chains_sidecar_then_tauri():
    sh = (ROOT / "scripts" / "build-desktop.sh").read_text(encoding="utf-8")
    assert "build-sidecar.sh" in sh and "tauri build" in sh
    ps = (ROOT / "scripts" / "build-desktop.ps1").read_text(encoding="utf-8")
    assert "build-sidecar.ps1" in ps and "tauri build" in ps


def test_self_build_guide_present():
    assert SELF_BUILD.is_file()


@pytest.mark.parametrize("name", ["build-desktop.ps1", "dev-bootstrap.ps1"])
def test_ps1_scripts_are_ascii(name):
    # PowerShell 5.1 reads scripts as cp1252; non-ASCII (e.g. em-dash) breaks
    # string terminators. Keep the .ps1 ASCII-only.
    raw = (ROOT / "scripts" / name).read_bytes()
    assert raw.decode("ascii")  # raises if any byte is > 0x7F


def test_dev_bootstrap_scripts_offer_the_same_options():
    sh = (ROOT / "scripts" / "dev-bootstrap.sh").read_text(encoding="utf-8")
    ps = (ROOT / "scripts" / "dev-bootstrap.ps1").read_text(encoding="utf-8")
    for opt in ("venv", "python", "extras", "recreate", "reinstall", "playwright",
                "worktree", "base", "repo", "dry-run"):
        assert f"--{opt}" in sh, opt
        ps_name = "DryRun" if opt == "dry-run" else opt.capitalize()
        assert f"${ps_name}" in ps, ps_name
    for s in (sh, ps):
        assert "evi doctor --dev" in s and "--strict" in s
        assert "pytest-xdist" in s
        assert ".worktrees" in s and "info" in s and "exclude" in s
    assert b"\r" not in (ROOT / "scripts" / "dev-bootstrap.sh").read_bytes()
