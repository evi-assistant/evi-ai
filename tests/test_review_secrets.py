"""Reviews run the secret scan (safety-secret-scan-cli): `evi review` (one
pass and --multi), the REPL's /review and the task review gate scan the lines
their diff adds. The model reads the diff with its secrets redacted, the
report starts with what the scan found (path:line: kind, never the secret),
and a finding fails the gate whatever the reviewers say."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evi import attest, redact, review, secretscan

# Split so no scanner reads the test as holding a token.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
OLD_TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


@pytest.fixture(autouse=True)
def no_known_secrets(monkeypatch):
    for name in list(os.environ):
        if redact._SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    redact.refresh()
    yield
    redact.refresh()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    (r / "x.py").write_text(f'a = 1\nOLD = "{OLD_TOKEN}"\nb = 2\nc = 3\n', encoding="utf-8")
    _git(r, "add", "x.py")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "checkout", "-q", "-b", "evi/T1")
    (r / "x.py").write_text(f'a = 1\nb = 2\nc = 3\nKEY = "{TOKEN}"\n', encoding="utf-8")
    _git(r, "commit", "-q", "-am", "change")
    return r


# ---- the scan -------------------------------------------------------------------


def test_scan_patch_counts_context_lines_toward_line_numbers():
    patch = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,5 @@\n"
        " a = 1\n+b = 2\n c = 3\n d = 4\n" f'+KEY = "{TOKEN}"\n'
    )
    assert secretscan.scan_patch(patch) == [secretscan.Finding("x.py", 5, "github_token")]


def test_secret_scan_redacts_every_secret_and_reports_the_added_ones(repo):
    diff = review.get_diff(branch="main", cwd=repo)
    scan = review.secret_scan(diff, "guidance")
    assert TOKEN not in scan.diff and OLD_TOKEN not in scan.diff  # a removed one too
    assert scan.findings == (secretscan.Finding("x.py", 4, "github_token"),)
    assert scan.context.startswith("guidance") and review.SECRETS_CONTEXT in scan.context
    report = scan.report("fine. APPROVE")
    assert report.startswith(review.SECRETS_HEADING)
    assert "BLOCKING: x.py:4: github_token" in report and TOKEN not in report
    assert review.review_exit_code(report) == 1  # the verdict doesn't clear it
    assert review.review_exit_code("fine. APPROVE") == 0


def test_a_diff_without_secrets_is_sent_unchanged():
    diff = "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -0,0 +1 @@\n+hello\n"
    scan = review.secret_scan(diff, "ctx")
    assert (scan.diff, scan.findings, scan.context) == (diff, (), "ctx")
    assert scan.report("r") == "r" and scan.section == ""


def test_a_removed_secret_alone_is_redacted_and_noted_but_not_blocking():
    diff = ("diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n"
            f'-T = "{TOKEN}"\n' '+T = os.environ["T"]\n')
    scan = review.secret_scan(diff)
    assert TOKEN not in scan.diff and scan.findings == ()
    assert scan.context == review.SECRETS_CONTEXT
    assert review.review_exit_code(scan.report("fine. APPROVE")) == 0


def test_only_the_scan_section_with_a_finding_blocks():
    assert review.review_exit_code("## Secret scan\n\nnothing here\n\nAPPROVE") == 0
    assert review.review_exit_code("see ## Secret scan\n- BLOCKING: x\nAPPROVE") == 0


# ---- the task review gate ---------------------------------------------------------


def test_the_review_gate_blocks_on_a_secret_whatever_the_reviewer_says(repo):
    seen: list[tuple[str, str]] = []

    def approving(diff, *, context=""):
        seen.append((diff, context))
        return "## Correctness\n\nSUGGESTION: none. APPROVE"

    res = attest.review_gate("main", cwd=repo, reviewer=approving)
    assert res.blocking and res.exit_code == 1
    assert res.report.startswith(review.SECRETS_HEADING) and TOKEN not in res.report
    diff, context = seen[0]
    assert TOKEN not in diff and "[REDACTED:github_token]" in diff
    assert review.SECRETS_CONTEXT in context


# ---- the CLI --------------------------------------------------------------------------


def test_evi_review_multi_json_reports_the_secrets(repo, monkeypatch):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    seen: list[str] = []

    def fake_multi(diff, tool_categories=(), context="", cwd=None):
        seen.append(diff)
        return "## Correctness\n\nSUGGESTION: none"

    monkeypatch.setattr(review, "multi_review", fake_multi)
    monkeypatch.chdir(repo)
    r = CliRunner().invoke(cli_main.app, ["review", "--branch", "main", "--multi", "--json",
                                          "--exit-code"])
    assert r.exit_code == 1, r.output
    assert TOKEN not in r.output and TOKEN not in seen[0]
    out = json.loads(r.output)
    assert out["secrets"] == [{"path": "x.py", "line": 4, "kind": "github_token"}]
    assert out["exit_code"] == 1 and out["review"].startswith(review.SECRETS_HEADING)


def test_the_repl_review_shows_the_secrets_first(repo, monkeypatch, capsys):
    import evi.apps.cli.main as cli_main

    monkeypatch.setattr(review, "multi_review",
                        lambda diff, context="", **_k: "## Correctness\n\nSUGGESTION: none")
    monkeypatch.setattr(cli_main.console, "width", 10_000)
    monkeypatch.chdir(repo)
    assert cli_main._handle_review(None, "--branch main", None) == "continue"
    out = capsys.readouterr().out
    assert "Secret scan" in out and "x.py:4: github_token" in out and TOKEN not in out
    assert "verdict: FAIL" in out
