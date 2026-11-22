"""The verify gate's secret scan (safety-secret-scan-cli): a secret the turn
added to a file fails the round like a check, in any folder (it runs no
code), and the model is told where and what kind, never the secret. A secret
the file already held (committed, or there when the turn started) is not the
turn's. [checks] secrets = false turns it off.

The projects here are untrusted, so no project check runs: only the scan.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import evi.config as config_mod
from evi import secretscan
from evi.headless import run_headless
from evi.history import is_note
from tests.conftest import llm_call, llm_text
from tests.test_verify_rounds import _agent, _config, _validate_report, home  # noqa: F401

# Split so no scanner reads the test as holding a token.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
TOKEN2 = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
pytestmark = pytest.mark.usefixtures("home")

LEAKY = f'API_TOKEN = "{TOKEN}"\n'
CLEAN = 'import os\n\nAPI_TOKEN = os.environ["API_TOKEN"]\n'


def _folder(tmp_path: Path, *, git: bool = False, files: dict[str, str] | None = None,
            commit: bool = True) -> Path:
    work = (tmp_path / "proj").resolve()
    work.mkdir()
    for name, text in (files or {}).items():
        (work / name).write_text(text, encoding="utf-8")
    if git:
        def g(*args: str) -> None:
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test",
                            "-c", "commit.gpgsign=false", *args],
                           cwd=work, check=True, capture_output=True)

        g("init", "-q")
        if commit and files:
            g("add", "-A")
            g("commit", "-q", "-m", "start")
    return work


def _checks(res) -> list[tuple]:
    return [(c["name"], c["round"], c["exit"], c["source"]) for c in res.checks]


def test_a_secret_the_turn_wrote_fails_the_round_then_the_fix_passes(tmp_path, fake_llm):
    work = _folder(tmp_path)
    agent = _agent(fake_llm([
        llm_call(("write_file", {"path": "settings.py", "content": LEAKY})),
        llm_text("added the token"),
        llm_call(("read_file", {"path": "settings.py"})),
        llm_call(("write_file", {"path": "settings.py", "content": CLEAN})),
        llm_text("reads it from the environment now"),
    ]), work)
    res = run_headless(agent, "add the API token")
    assert (res.stop_reason, res.exit_code) == ("done", 0)
    assert _checks(res) == [("secrets", 1, 1, "evi"), ("secrets", 2, 0, "evi")]
    assert res.checks[0]["tail"].startswith("settings.py:1: github_token")
    assert res.checks[0]["cmd"] == "evi secrets scan"
    assert "not a trusted folder" in res.checks_skipped  # the project's checks didn't run
    [note] = [m for m in agent.history if is_note(m)]
    assert "settings.py:1: github_token" in note["content"] and "round 1 of 2" in note["content"]
    assert TOKEN not in note["content"] and TOKEN not in str(res.to_dict())
    _validate_report(res.to_dict())


def test_a_secret_the_model_keeps_ends_the_turn_with_checks_failed(tmp_path, fake_llm):
    work = _folder(tmp_path)
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "settings.py", "content": LEAKY})),
        llm_text("done"),
        llm_text("still done"),
    ]), work), "go")
    assert (res.stop_reason, res.exit_code) == ("checks_failed", 5)
    assert _checks(res) == [("secrets", 1, 1, "evi"), ("secrets", 2, 1, "evi")]


def test_a_committed_secret_the_turn_kept_is_not_flagged(tmp_path, fake_llm):
    work = _folder(tmp_path, git=True, files={"settings.py": LEAKY})
    res = run_headless(_agent(fake_llm([
        llm_call(("read_file", {"path": "settings.py"})),
        llm_call(("write_file", {"path": "settings.py",
                                 "content": "# settings\n\n" + LEAKY + "DEBUG = False\n"})),
        llm_text("done"),
    ]), work), "go")
    assert res.stop_reason == "done" and res.checks == []


def test_a_secret_added_beside_a_committed_one_is_flagged_alone(tmp_path, fake_llm):
    work = _folder(tmp_path, git=True, files={"settings.py": LEAKY})
    res = run_headless(_agent(fake_llm([
        llm_call(("read_file", {"path": "settings.py"})),
        llm_call(("write_file", {"path": "settings.py",
                                 "content": LEAKY + f'OTHER = "{TOKEN2}"\n'})),
        llm_text("done"),
        llm_text("done"),
    ]), work), "go")
    assert res.stop_reason == "checks_failed"
    assert res.checks[0]["tail"].splitlines()[0] == "settings.py:2: github_token"
    assert "settings.py:1:" not in res.checks[0]["tail"]


def test_a_secret_already_in_a_dirty_file_at_the_start_is_not_the_turns(tmp_path, fake_llm):
    work = _folder(tmp_path, git=True, files={"settings.py": "DEBUG = False\n"})
    (work / "settings.py").write_text("DEBUG = False\n" + LEAKY, encoding="utf-8")  # the user's
    (work / "notes.txt").write_text(LEAKY, encoding="utf-8")  # untracked, the user's
    res = run_headless(_agent(fake_llm([
        llm_call(("read_file", {"path": "settings.py"})),
        llm_call(("write_file", {"path": "settings.py", "content": LEAKY + "DEBUG = True\n"})),
        llm_call(("read_file", {"path": "notes.txt"})),
        llm_call(("write_file", {"path": "notes.txt", "content": "todo\n" + LEAKY})),
        llm_text("done"),
    ]), work), "go")
    assert res.stop_reason == "done" and res.checks == []


def test_a_new_file_in_a_repository_counts_whole(tmp_path, fake_llm):
    work = _folder(tmp_path, git=True, files={"README.md": "x\n"})
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "deploy.sh", "content": f"export T={TOKEN}\n"})),
        llm_text("done"),
        llm_text("done"),
    ]), work), "go")
    assert res.stop_reason == "checks_failed"
    assert res.checks[0]["tail"].startswith("deploy.sh:1: github_token")


def test_the_scan_is_off_with_checks_secrets_false(tmp_path, fake_llm):
    _config(config_mod.HOME, "[checks]\nsecrets = false\n")
    work = _folder(tmp_path)
    res = run_headless(_agent(fake_llm([
        llm_call(("write_file", {"path": "settings.py", "content": LEAKY})),
        llm_text("done"),
    ]), work), "go")
    assert res.stop_reason == "done" and res.checks == []


# ---- evi.secretscan.added -----------------------------------------------------------


def test_added_knows_a_line_that_moved_and_skips_a_file_past_the_baseline(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n" + LEAKY, encoding="utf-8")
    (tmp_path / "b.py").write_text(LEAKY, encoding="utf-8")
    base = {"a.py": secretscan.secret_lines(LEAKY), "b.py": None}
    assert secretscan.added(tmp_path, ["a.py", "b.py"], base=base) == []
    assert secretscan.added(tmp_path, ["a.py", "b.py"]) == [
        secretscan.Finding("a.py", 2, "github_token"), secretscan.Finding("b.py", 1, "github_token")]
    assert secretscan.added(tmp_path, ["gone.py"]) == []


def test_baseline_reads_at_most_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(secretscan, "MAX_BASELINE_FILES", 1)
    (tmp_path / "a.py").write_text(LEAKY, encoding="utf-8")
    assert secretscan.baseline(tmp_path, ["a.py", "b.py"]) == {
        "a.py": secretscan.secret_lines(LEAKY), "b.py": None}


@pytest.mark.parametrize("text, want", [
    ("", frozenset()),
    (LEAKY, frozenset({("github_token", LEAKY.strip())})),
])
def test_secret_lines(text, want):
    assert secretscan.secret_lines(text) == want
