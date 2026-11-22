"""A shell line that commits, pushes or opens a pull request asks first when
what it would send holds a secret (`permissions.shell_secret_send`)."""

from __future__ import annotations

import json
import subprocess

import pytest

from evi import permissions, redact, secretscan
from evi.permissions import decide, shell_secret_send

GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # a GitHub token's shape


@pytest.fixture(autouse=True)
def no_known_values(monkeypatch):
    monkeypatch.setattr(redact, "_known", lambda: redact._compile({}))


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "app.py")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _send(command, repo):
    return shell_secret_send("run_command", "shell", json.dumps({"command": command}),
                             workdir=repo)


def _decide(command, repo, mode="ask", rules=()):
    return decide(mode, ["shell"], list(rules), "run_command", "shell",
                  json.dumps({"command": command}), workdir=repo)


def test_a_commit_with_a_token_in_the_change_is_named_without_the_token(repo):
    (repo / "app.py").write_text(f"x = 1\ntoken = '{GH}'\n", encoding="utf-8")
    hit = _send("git commit -am wip", repo)
    assert hit == "git commit would send a github_token (app.py:2)"
    assert GH not in hit


def test_a_clean_commit_and_lines_that_send_nothing_pass(repo):
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert _send("git commit -am wip", repo) is None
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    for line in ("git status", "git diff", "git add app.py", "echo git commit", "ls"):
        assert _send(line, repo) is None, line


def test_an_untracked_file_counts_only_when_the_line_adds(repo):
    (repo / "new.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    assert _send("git commit -m wip", repo) is None
    assert _send("git add -A && git commit -m wip", repo) == (
        "git commit would send a github_token (new.py:1)")


def test_a_staged_new_file_counts(repo):
    (repo / "new.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    _git(repo, "add", "new.py")
    assert "github_token (new.py:1)" in _send("git commit -m wip", repo)


def test_git_dash_c_and_a_cd_find_the_work_tree(repo, tmp_path):
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    assert _send(f"git -C {repo.name} commit -am wip", tmp_path) is not None
    assert _send(f"cd {repo.name} && git commit -am wip", tmp_path) is not None
    assert _send("git commit -am wip", tmp_path) is None  # not a work tree: git fails itself


def test_a_push_sends_the_commits_since_the_upstream(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "HEAD")
    assert _send("git push", repo) is None
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    assert _send("git push", repo) is None  # not committed: a push doesn't send it
    assert _send("git commit -am wip && git push", repo) == (
        "git push would send a github_token (app.py:1)")
    _git(repo, "commit", "-q", "-am", "leak")
    assert _send("git push", repo) == "git push would send a github_token (app.py:1)"
    assert _send("gh pr create --fill", repo) is not None


def test_a_push_with_nothing_on_a_remote_sends_every_tracked_file(repo):
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "leak")
    assert _send("git push -u origin HEAD", repo) is not None


def test_decide_asks_even_under_an_allow_rule_and_a_deny_rule_still_denies(repo):
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    assert _decide("git commit -am wip", repo, rules=["allow run_command git *"]) == "ask"
    assert _decide("git commit -am wip", repo, rules=["deny run_command git commit*"]) == "deny"
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    assert _decide("git commit -am wip", repo, rules=["allow run_command git *"]) == "allow"


def test_a_scan_failure_reads_as_a_finding_in_the_agent(monkeypatch, repo):
    """The agent's wrapper fails closed: a check that can't run asks."""
    from types import SimpleNamespace

    from evi.llm.agent import Agent

    def boom(*a, **k):
        raise RuntimeError("scan broke")

    monkeypatch.setattr(permissions, "shell_secret_send", boom)
    fake = SimpleNamespace(cwd=str(repo))
    tool = SimpleNamespace(name="run_command", category="shell")
    hit = Agent._secret_send_hit(fake, tool, json.dumps({"command": "git commit -m x"}))
    assert hit is not None


def test_scan_outgoing_refuses_a_folder_that_is_not_a_work_tree(tmp_path):
    with pytest.raises(secretscan.ScanError):
        secretscan.scan_outgoing(tmp_path, commit=True, push=False)


def _agent(repo, fake_llm, command, *, rules=()):
    import evi.tools.shell  # noqa: F401  registers run_command
    from evi.config import Config
    from evi.llm.agent import Agent
    from evi.tools.base import REGISTRY

    cfg = Config()
    cfg.tools.shell = True
    cfg.tools.memory = cfg.tools.skills = cfg.tools.transcripts = False
    cfg.auto.rules = list(rules)
    client = fake_llm([fake_llm.call(("run_command", {"command": command})),
                       fake_llm.text("done")])
    agent = Agent(client=client, config=cfg, tools=[REGISTRY["run_command"]])
    agent.cwd = str(repo)
    return agent


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout


def test_an_unattended_run_refuses_the_commit_and_says_why(repo, fake_llm):
    from evi.llm.agent import ToolResult

    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    before = _head(repo)
    agent = _agent(repo, fake_llm, "git commit -am leak", rules=["allow run_command *"])
    agent.enable_auto_all()  # `/auto on` doesn't clear it either
    [res] = [e for e in agent.chat("go") if isinstance(e, ToolResult)]
    assert "git commit would send a github_token (app.py:1)" in res.output
    assert GH not in res.output and _head(repo) == before


def test_with_a_ui_it_asks_even_under_auto_on(repo, fake_llm):
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    before = _head(repo)
    asked = []
    agent = _agent(repo, fake_llm, "git commit -am leak", rules=["allow run_command *"])
    agent.permission_callback = lambda name, args, cat: asked.append(args) or False
    agent.enable_auto_all()
    list(agent.chat("go"))
    assert len(asked) == 1 and _head(repo) == before


def test_yolo_asks_and_plan_mode_denies(repo):
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    assert _decide("git commit -am wip", repo, mode="yolo") == "ask"
    assert _decide("git commit -am wip", repo, mode="plan") == "deny"
    (repo / "app.py").write_text("x = 4\n", encoding="utf-8")
    assert _decide("git commit -am wip", repo, mode="yolo") == "allow"


@pytest.mark.parametrize(("line", "dialect"), [
    ('bash -c "git commit -am x"', "bash"),
    ('pwsh -c "git commit -am x"', "pwsh"),
    ("cmd /c git commit -am x", "cmd"),
    ("git.exe commit -am x", "pwsh"),
    ("& git commit -am x", "pwsh"),
    ("FOO=1 timeout 5 git --no-pager -c core.x=1 commit -am x", "bash"),
])
def test_wrapped_and_inline_commits_are_seen(repo, line, dialect):
    (repo / "app.py").write_text(f"t = '{GH}'\n", encoding="utf-8")
    hit = shell_secret_send("run_command", "shell", json.dumps({"command": line}),
                            workdir=repo, dialect=dialect)
    assert hit is not None and "github_token (app.py:1)" in hit, (line, hit)


# --- What an independent review found (each was a miss before) ---

def _token(repo, name="app.py", line=f"t = '{GH}'\n"):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(line, encoding="utf-8")


def test_a_network_folder_is_not_scanned_and_asks(repo, monkeypatch):
    called = []
    monkeypatch.setattr(secretscan, "scan_outgoing", lambda *a, **k: called.append(a) or [])
    for line, args in [("git -C //attacker.invalid/share commit -am x", {}),
                       ("git --work-tree=//host/s commit -am x", {}),
                       ("git commit -am x", {"cwd": "//host/share"})]:
        hit = shell_secret_send("run_command", "shell", json.dumps({"command": line, **args}),
                                workdir=repo, dialect="bash")
        if permissions.os.name == "nt":
            assert hit is not None and "network folder" in hit, line
    if permissions.os.name == "nt":
        assert called == []


@pytest.mark.parametrize("setting", ["diff.mnemonicPrefix", "diff.noprefix", "diff.relative"])
def test_diff_settings_in_the_repository_config_change_nothing(repo, setting):
    _git(repo, "config", setting, "true")
    (repo / "sub").mkdir()
    _token(repo)
    assert _send("git commit -am x", repo) == "git commit would send a github_token (app.py:1)"
    assert _send("git commit -am x", repo / "sub") is not None


def test_a_staged_secret_counts_after_the_file_is_cleaned(repo):
    _token(repo)
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert _send("git commit -m x", repo) is not None


def test_a_staged_secret_counts_in_a_repository_with_no_commit(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    _git(r, "init", "-q")
    _token(r)
    _git(r, "add", "app.py")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert _send("git commit -m x", r) is not None


@pytest.fixture
def pushed(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "HEAD")
    return repo


def test_a_push_finds_a_secret_a_later_commit_removed(pushed):
    _token(pushed)
    _git(pushed, "commit", "-q", "-am", "add")
    (pushed / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(pushed, "commit", "-q", "-am", "remove")
    assert _send("git push", pushed) is not None


def test_a_new_branch_push_scans_only_its_own_commits(pushed):
    _token(pushed, "fixture.py")
    _git(pushed, "add", "fixture.py")
    _git(pushed, "commit", "-q", "-m", "fixture")
    _git(pushed, "push", "-q")
    _git(pushed, "checkout", "-q", "-b", "feat")
    (pushed / "app.py").write_text("x = 5\n", encoding="utf-8")
    _git(pushed, "commit", "-q", "-am", "clean")
    assert _send("git push -u origin feat", pushed) is None


def test_odd_file_names_are_scanned_and_shown_plainly(repo):
    for name in ("café.py", "a b.py"):
        (repo / name).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "names")
    for name in ("café.py", "a b.py"):
        _token(repo, name)
        assert _send("git commit -am x", repo) == f"git commit would send a github_token ({name}:1)"
        (repo / name).write_text("x = 1\n", encoding="utf-8")


def test_an_added_line_that_reads_like_a_header_hides_nothing(repo):
    (repo / "app.py").write_text(f"x = 1\n++ note\nt = '{GH}'\n", encoding="utf-8")
    assert _send("git commit -am x", repo) == "git commit would send a github_token (app.py:3)"


@pytest.mark.parametrize("line", ["if true; then git commit -am x; fi",
                                  "for i in 1; do git commit -am x; done",
                                  "! git commit -am x"])
def test_shell_keywords_before_the_command_hide_nothing(repo, line):
    _token(repo)
    assert _send(line, repo) is not None


def test_a_folder_eVi_cant_follow_asks(repo, tmp_path):
    _token(repo)
    for line in (f"D={repo.name}; cd $D && git commit -am x", 'git -C "$D" commit -am x'):
        hit = _send(line, tmp_path)
        assert hit is not None and "can't tell which work tree" in hit, line


def test_gh_pr_new_counts_like_create(repo):
    _token(repo)
    _git(repo, "commit", "-q", "-am", "leak")
    assert _send("gh pr new --fill", repo) is not None


def test_a_commit_from_a_subfolder_scans_the_whole_work_tree(repo):
    (repo / "sub").mkdir()
    _token(repo, "root.py")
    assert _send("git add -A && git commit -m x", repo / "sub") is not None
