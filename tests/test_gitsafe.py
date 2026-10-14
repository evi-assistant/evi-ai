"""Hardened internal git calls (evi.gitsafe) — exploit regressions.

- hostile repo config: a working copy whose .git/config wires core.fsmonitor,
  diff.external, a diff driver (command + textconv), a filter driver
  (clean + smudge), a post-checkout hook file and a config-defined hook to a
  marker script must not run it through eVi's git tools, `evi review`,
  worktrees, the status line or the anatomy map;
- ...nor may a submodule's own filter drivers, nor what the repository's
  config names for a trusted Git LFS (an `lfs.extension`, a `core.sshCommand`
  reached by an LFS download), and git-lfs can't install hooks where eVi
  points core.hooksPath;
- a hostile core.worktree (work tree moved outside the repo, or up to an
  ancestor such as the home directory) is refused;
- ref injection: `--output=<file>` / `--no-index` and friends handed to
  git_diff / git_show as a ref are refused (and stay refused over MCP), and
  so are git_diff `paths` that leave the repo (git's implicit --no-index);
- `evi review --staged` in a pre-commit hook of `git commit -a` reviews the
  index being committed (GIT_INDEX_FILE is honoured for shell commands);
- version fallback: `--end-of-options` only on git >= 2.24, validation always.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evi import gitsafe

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _git(*args: str, cwd: Path) -> str:
    """Plain, unhardened git — test setup and the "trap is armed" controls."""
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout


def _make_repo(d: Path, branch: str = "main") -> Path:
    d.mkdir()
    _git("-c", f"init.defaultBranch={branch}", "init", cwd=d)
    _git("config", "user.email", "evi@test.local", cwd=d)
    _git("config", "user.name", "evi", cwd=d)
    (d / "a.txt").write_text("hi\n", encoding="utf-8")
    (d / "b.txt").write_text("stable\n", encoding="utf-8")  # never edited
    _git("add", "a.txt", "b.txt", cwd=d)
    _git("commit", "-m", "init", cwd=d)
    (d / "a.txt").write_text("hi\nmore\n", encoding="utf-8")
    _git("commit", "-am", "second", cwd=d)
    (d / "a.txt").write_text("hi\nmore\nwip\n", encoding="utf-8")
    return d


_bump = [0]


def _touch(f: Path) -> None:
    """New mtime, same content: git must re-read the file to know it is
    unchanged, which is when it runs the clean filter."""
    _bump[0] += 5
    t = time.time() + _bump[0]
    os.utime(f, (t, t))


def _scripts(tmp_path: Path, marker: Path) -> tuple[str, str]:
    """(marker command, pass-through filter command) for repo config."""
    script = tmp_path / "marker.sh"
    script.write_text(
        f'#!/bin/sh\necho "$0 $*" >> "{marker.as_posix()}"\nexit 0\n',
        encoding="utf-8", newline="\n",
    )
    script.chmod(0o755)
    filt = tmp_path / "filter.sh"  # a filter must copy stdin to stdout
    filt.write_text(
        f'#!/bin/sh\necho "filter $*" >> "{marker.as_posix()}"\ncat\n',
        encoding="utf-8", newline="\n",
    )
    filt.chmod(0o755)
    return f'"{script.as_posix()}"', f'"{filt.as_posix()}"'


def _tool(name: str, **kw) -> str:
    from evi.tools.base import REGISTRY
    import evi.tools.git  # noqa: F401  register tools

    return REGISTRY[name].call(json.dumps(kw))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


# --- hostile repo config -----------------------------------------------------


@pytest.fixture
def hostile(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose own config runs a marker script on ordinary reads."""
    repo = _make_repo(tmp_path / "hostile")
    marker = tmp_path / "MARKER"
    cmd, filt = _scripts(tmp_path, marker)
    _git("config", "core.fsmonitor", cmd, cwd=repo)
    _git("config", "diff.external", cmd, cwd=repo)
    _git("config", "diff.evil.command", cmd, cwd=repo)
    _git("config", "diff.evil.textconv", cmd, cwd=repo)
    _git("config", "filter.evil.clean", f"{filt} clean", cwd=repo)
    _git("config", "filter.evil.smudge", f"{filt} smudge", cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(
        "* diff=evil filter=evil\n", encoding="utf-8",
    )
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\necho hook >> "{marker.as_posix()}"\n', encoding="utf-8", newline="\n",
    )
    hook.chmod(0o755)

    # Controls: plain git really does fire the trap, so a clean run below
    # means eVi's hardening stopped it — not that the trap was a dud.
    for args in (["status", "--short"], ["diff"], ["worktree", "add", "-b", "ctl",
                                                   str(tmp_path / "ctl")]):
        _touch(repo / "b.txt")
        _git(*args, cwd=repo)
        assert marker.exists(), f"control: plain `git {args[0]}` should fire the marker"
        marker.unlink()
    # ...and the filter driver alone does too (fsmonitor off): the clean
    # filter on a touched file during status, smudge on checkout.
    empty = tmp_path / "nohooks"
    empty.mkdir()
    _touch(repo / "b.txt")
    _git("-c", "core.fsmonitor=", "status", "--short", cwd=repo)
    assert "filter clean" in marker.read_text(), "control: clean filter should run"
    marker.unlink()
    _git("-c", f"core.hooksPath={empty.as_posix()}", "worktree", "add", "-b", "ctl2",
         str(tmp_path / "ctl2"), cwd=repo)
    assert "filter smudge" in marker.read_text(), "control: smudge filter should run"
    marker.unlink()
    return repo, marker


def test_hostile_config_never_fires_through_evi(hostile, monkeypatch) -> None:
    from evi import anatomy, review, statusline, worktree

    repo, marker = hostile
    monkeypatch.chdir(repo)
    calls = {
        "git_status": lambda: _tool("git_status", path=str(repo)),
        "git_diff": lambda: _tool("git_diff", path=str(repo)),
        "git_diff paths": lambda: _tool("git_diff", paths="a.txt b.txt", path=str(repo)),
        "git_diff range": lambda: _tool("git_diff", ref="HEAD~1..HEAD", path=str(repo)),
        "git_diff staged": lambda: _tool("git_diff", ref="--staged", path=str(repo)),
        "git_show": lambda: _tool("git_show", ref="HEAD", path=str(repo)),
        "git_log": lambda: _tool("git_log", path=str(repo)),
        "git_blame": lambda: _tool("git_blame", file_path="b.txt", path=str(repo)),
        "git_info": lambda: _tool("git_info", path=str(repo)),
        "review default": lambda: review.get_diff(cwd=repo),
        "review shell": lambda: review.get_diff(),  # cwd=None: inherits git env
        "review staged": lambda: review.get_diff(staged=True, cwd=repo),
        "review range": lambda: review.get_diff(range="HEAD~1..HEAD", cwd=repo),
        "review branch": lambda: review.get_diff(branch="main", cwd=repo),
        "review file": lambda: review.get_diff(file="b.txt", cwd=repo),
        "worktree list": lambda: worktree.list_worktrees(repo),
        "worktree create": lambda: worktree.create_worktree("feat", start=repo),
        "worktree dirty": lambda: worktree.dirty_files(repo),
        "statusline": lambda: statusline.git_branch(repo),
        "anatomy": lambda: anatomy._git_files(repo),
    }
    results = {}
    for label, call in calls.items():
        _touch(repo / "b.txt")  # stat-dirty, so a clean filter would run
        results[label] = call()
        assert not marker.exists(), f"{label} ran repo-configured code: {marker.read_text()}"

    # ...and the hardened calls still did their job.
    assert "+wip" in results["git_diff"] and "+wip" in results["git_diff paths"]
    assert "+more" in results["git_diff range"] and "+more" in results["review range"]
    assert "a.txt" in results["git_status"] and "+wip" in results["review default"]
    assert "b.txt" not in results["git_status"]  # unchanged, not "modified"
    assert "+wip" in results["review shell"]
    assert "init" in results["git_log"] and "a.txt" in results["git_show"]
    assert "stable" in results["git_blame"]
    assert json.loads(results["git_info"])["branch"] == "main"
    assert {e.branch for e in results["worktree list"]} >= {"main", "ctl"}
    assert results["worktree create"].is_dir()
    assert (results["worktree create"] / "b.txt").read_text(encoding="utf-8") == "stable\n"
    assert results["statusline"] == "main"
    assert any(p.name == "a.txt" for p in results["anatomy"])


def test_repo_config_hooks_never_fire(tmp_path, monkeypatch) -> None:
    """`hook.<name>.command` + `.event` in .git/config is a hook too, and
    core.hooksPath doesn't cover it."""
    from evi import worktree

    repo = _make_repo(tmp_path / "repo")
    marker = tmp_path / "MARKER"
    cmd, _ = _scripts(tmp_path, marker)
    _git("config", "hook.evil.command", f"{cmd} config-hook", cwd=repo)
    _git("config", "hook.evil.event", "post-checkout", cwd=repo)
    empty = tmp_path / "nohooks"
    empty.mkdir()
    _git("-c", f"core.hooksPath={empty.as_posix()}", "worktree", "add", "-b", "ctl",
         str(tmp_path / "ctl"), cwd=repo)
    if not marker.exists():
        pytest.skip("this git has no config-defined hooks")
    marker.unlink()

    monkeypatch.chdir(repo)
    _git("branch", "other", cwd=repo)
    assert worktree.create_worktree("feat", start=repo).is_dir()
    assert worktree.create_worktree("other", start=repo, create_branch=False).is_dir()
    assert not marker.exists(), marker.read_text()
    assert "hook.evil.enabled=false" in gitsafe.repo_program_overrides(["worktree"], repo)


def test_global_filters_and_hooks_are_left_alone(tmp_path, monkeypatch) -> None:
    """Only the repository's own drivers are switched off: one defined in the
    user's global config (Git LFS, say) still runs."""
    if (gitsafe.git_version() or (0, 0, 0)) < (2, 32):
        pytest.skip("GIT_CONFIG_GLOBAL needs git 2.32")
    repo = _make_repo(tmp_path / "repo")
    marker = tmp_path / "MARKER"
    cmd, filt = _scripts(tmp_path, marker)
    glob = tmp_path / "global.gitconfig"
    glob.write_text(
        f'[filter "mine"]\n\tclean = {filt} mine-clean\n'
        f'[hook "mine"]\n\tcommand = {cmd} mine-hook\n\tevent = post-checkout\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    _git("config", "filter.evil.clean", f"{filt} evil-clean", cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(
        "b.txt filter=mine\na.txt filter=evil\n", encoding="utf-8",
    )
    overrides = gitsafe.repo_program_overrides(["status"], repo)
    assert "filter.evil.clean=" in overrides
    assert not any(".mine." in o for o in overrides)

    (repo / "a.txt").write_text("hi\nmore\n", encoding="utf-8")  # as committed
    _touch(repo / "a.txt")
    _touch(repo / "b.txt")
    r = gitsafe.run_git(["status", "--porcelain"], cwd=repo)
    assert r.returncode == 0 and r.stdout == ""
    fired = marker.read_text() if marker.exists() else ""
    assert "mine-clean" in fired and "evil-clean" not in fired


def test_driver_name_git_cannot_override_is_refused(tmp_path, monkeypatch) -> None:
    """`-c` splits at the first "=", so a driver called "a=b" can't be blanked:
    eVi refuses to run git there instead of running it."""
    from evi import review, worktree

    repo = _make_repo(tmp_path / "repo")
    marker = tmp_path / "MARKER"
    _, filt = _scripts(tmp_path, marker)
    _git("config", "filter.a=b.clean", f"{filt} clean", cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=a=b\n", encoding="utf-8")
    _touch(repo / "b.txt")
    _git("status", "--short", cwd=repo)
    assert marker.exists(), "control: plain git runs the 'a=b' filter"
    marker.unlink()

    _touch(repo / "b.txt")
    with pytest.raises(gitsafe.UnsafeRepoError, match="a=b"):
        gitsafe.run_git(["status", "--short"], cwd=repo)
    assert _tool("git_status", path=str(repo)).startswith("ERROR")
    assert _tool("git_diff", path=str(repo)).startswith("ERROR")
    with pytest.raises(review.ReviewError):
        review.get_diff(cwd=repo)
    with pytest.raises(worktree.WorktreeError):
        worktree.create_worktree("feat", start=repo)
    assert not marker.exists()


def test_git_without_show_scope_switches_off_every_driver(tmp_path, monkeypatch) -> None:
    """Older gits (< 2.26) can't say where a key came from: fail closed."""
    repo = _make_repo(tmp_path / "repo")
    marker = tmp_path / "MARKER"
    _, filt = _scripts(tmp_path, marker)
    _git("config", "filter.evil.clean", f"{filt} clean", cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n", encoding="utf-8")

    real = subprocess.run
    seen: list[list[str]] = []

    def old_git(argv, *a, **kw):
        if "--show-scope" in argv:
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 129, b"", b"error: unknown option")
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitsafe.subprocess, "run", old_git)
    overrides = gitsafe.repo_program_overrides(["status"], repo)
    assert seen and "filter.evil.clean=" in overrides
    # Without scopes even a system/global driver (Git for Windows ships
    # filter.lfs) is switched off.
    keys = gitsafe._neutralising_config([(None, "filter.lfs.clean")])
    assert "filter.lfs.clean=" in keys
    _touch(repo / "b.txt")
    assert gitsafe.run_git(["status", "--short"], cwd=repo).returncode == 0
    assert not marker.exists()


# --- Git LFS: a trusted driver that reads the repository's config -----------


def _py_marker(tmp_path: Path, marker: Path, name: str, *, ssh: bool = False) -> str:
    """Command line for a Python script that logs its argv to `marker` and
    copies stdin to stdout, as a filter / LFS extension must. As an ssh
    (`ssh=True`) it exits 1 at once instead — git-lfs keeps its stdin open —
    and is quoted with forward slashes: git-lfs runs ssh through a shell,
    extension commands without one."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import shutil, sys\n"
        f"open({str(marker)!r}, 'a').write({name!r} + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        + ("sys.exit(1)\n" if ssh else
           "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n"),
        encoding="utf-8",
    )
    if ssh:
        return f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    return f"{sys.executable} {script}"


_LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\noid sha256:" + "b" * 64 + "\nsize 12\n"
)


@pytest.fixture
def lfs_repo(tmp_path, monkeypatch) -> Path:
    """A repo tracking *.bin with Git LFS, the filter defined in the user's
    global config as `git lfs install` does — trusted, so it stays on."""
    if shutil.which("git-lfs") is None:
        pytest.skip("git-lfs not installed")
    if (gitsafe.git_version() or (0, 0, 0)) < (2, 32):
        pytest.skip("GIT_CONFIG_GLOBAL needs git 2.32")
    glob = tmp_path / "global.gitconfig"
    glob.write_text(
        '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
        "\tprocess = git-lfs filter-process\n\trequired = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    repo = _make_repo(tmp_path / "lfsrepo")
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8",
    )
    (repo / "data.bin").write_text("hello data\n", encoding="utf-8")
    _git("add", ".gitattributes", "data.bin", cwd=repo)
    # ...and a pointer whose object was never fetched: checking it out downloads.
    (repo / "big.bin").write_bytes(_LFS_POINTER.encode())  # LF: a canonical pointer
    _git("-c", "filter.lfs.clean=", "-c", "filter.lfs.process=",
         "-c", "filter.lfs.required=false", "add", "big.bin", cwd=repo)
    _git("commit", "-m", "lfs", cwd=repo)
    return repo


def test_repo_lfs_extension_never_runs(lfs_repo, tmp_path) -> None:
    """git-lfs runs `lfs.extension.<n>.clean` from the repository's own
    .git/config on every clean — through the global LFS driver eVi keeps."""
    from evi import review, worktree

    marker = tmp_path / "MARKER"
    ext = _py_marker(tmp_path, marker, "ext")
    _git("config", "lfs.extension.evil.clean", f"{ext} clean %f", cwd=lfs_repo)
    _git("config", "lfs.extension.evil.smudge", f"{ext} smudge %f", cwd=lfs_repo)
    _git("config", "lfs.extension.evil.priority", "0", cwd=lfs_repo)
    _touch(lfs_repo / "data.bin")
    subprocess.run(["git", "status", "--short"], cwd=lfs_repo, capture_output=True)
    if not marker.exists():
        pytest.skip("this git-lfs does not run repository-config extensions")
    marker.unlink()

    for label, call in {
        "git_status": lambda: _tool("git_status", path=str(lfs_repo)),
        "git_diff": lambda: _tool("git_diff", path=str(lfs_repo)),
        "review": lambda: review.get_diff(cwd=lfs_repo),
        "dirty_files": lambda: worktree.dirty_files(lfs_repo),
        "worktree create": lambda: worktree.create_worktree("feat", start=lfs_repo),
    }.items():
        _touch(lfs_repo / "data.bin")
        try:
            call()  # failing closed (git-lfs refuses the emptied extension) is fine
        except (review.ReviewError, worktree.WorktreeError):
            pass
        assert not marker.exists(), f"{label} ran the repo's LFS extension: {marker.read_text()}"
    assert "lfs.extension.evil.clean=" in gitsafe.repo_program_overrides(["status"], lfs_repo)


def test_create_worktree_never_downloads_lfs_objects(lfs_repo, tmp_path) -> None:
    """Checking out an LFS pointer downloads it, and git-lfs then runs the
    repository's own core.sshCommand (or credential helper, transfer agent)."""
    from evi import worktree

    marker = tmp_path / "MARKER"
    ssh = _py_marker(tmp_path, marker, "ssh", ssh=True)
    _git("config", "lfs.url", "ssh://git@lfs.example.invalid/x.git", cwd=lfs_repo)
    _git("config", "core.sshCommand", ssh, cwd=lfs_repo)
    subprocess.run(["git", "worktree", "add", "-b", "ctl", str(tmp_path / "ctl")],
                   cwd=lfs_repo, capture_output=True, timeout=300)
    if not marker.exists():
        pytest.skip("this git-lfs does not reach ssh here")
    marker.unlink()

    assert "core.sshcommand=" in gitsafe.repo_program_overrides(["worktree"], lfs_repo)
    wt = worktree.create_worktree("feat", start=lfs_repo)
    assert not marker.exists(), marker.read_text()
    # LFS files come out as pointer files: nothing was downloaded or smudged.
    assert (wt / "big.bin").read_text(encoding="utf-8") == _LFS_POINTER
    assert (wt / "data.bin").read_text(encoding="utf-8").startswith("version https://git-lfs")
    assert worktree.dirty_files(wt) == []  # ...and git sees them as unmodified


def test_lfs_cannot_install_hooks_into_the_hooks_path(lfs_repo, tmp_path) -> None:
    """git-lfs installs its hooks into whatever hooks directory git reports;
    an empty directory there would fill up (and its post-checkout run)."""
    from evi import worktree

    empty = tmp_path / "nohooks"
    empty.mkdir()
    _touch(lfs_repo / "data.bin")
    _git("-c", f"core.hooksPath={empty.as_posix()}", "status", "--short", cwd=lfs_repo)
    if not any(empty.iterdir()):
        pytest.skip("this git-lfs does not install hooks while filtering")

    before = gitsafe.no_hooks_path()
    _touch(lfs_repo / "data.bin")
    assert gitsafe.run_git(["status", "--porcelain"], cwd=lfs_repo).returncode == 0
    assert worktree.create_worktree("feat", start=lfs_repo).is_dir()
    # Still the same, still empty: nothing was written, so no new temp path
    # was needed (one leaked per call before).
    assert gitsafe.no_hooks_path() == before
    assert os.path.isfile(before) and os.path.getsize(before) == 0


def test_repo_program_settings_are_blanked_only_from_repo_scopes() -> None:
    keys = [
        ("local", "lfs.extension.evil.clean"),
        ("local", "lfs.Extension.Evil2.smudge"),   # git-lfs lowercases it
        ("local", "lfs.customtransfer.agent.path"),
        ("local", "lfs.standalonetransferagent"),
        ("local", "core.sshcommand"),
        ("worktree", "core.askpass"),
        ("local", "credential.https://x.example/?a=b.helper"),
        ("local", "lfs.url"),                       # not a program
        ("local", "lfs.customtransfer.agent.args"),
        ("local", "credential.username"),
        ("global", "lfs.customtransfer.mine.path"),  # the user's own
        ("system", "credential.helper"),
        ("global", "core.sshcommand"),
    ]
    cfg = gitsafe._neutralising_config(keys)
    assert cfg[0::2] == ["-c"] * (len(cfg) // 2)
    assert set(cfg[1::2]) == {
        "lfs.extension.evil.clean=", "lfs.Extension.Evil2.smudge=",
        "lfs.customtransfer.agent.path=", "lfs.standalonetransferagent=",
        "core.sshcommand=", "core.askpass=", "credential.helper=",
    }
    assert gitsafe._neutralising_config(
        [("global", "credential.helper"), ("system", "core.sshcommand")]
    ) == []
    with pytest.raises(gitsafe.UnsafeRepoError, match="a=b"):
        gitsafe._neutralising_config([("local", "lfs.extension.a=b.clean")])


def test_lfs_downloads_skipped_except_for_clone_and_evis_own_repo(
    repo, tmp_path, monkeypatch,
) -> None:
    real = subprocess.run
    envs: list[tuple[list[str], dict]] = []

    def spy(argv, *a, **kw):
        envs.append((list(argv), kw.get("env") or {}))
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitsafe.subprocess, "run", spy)

    def skip_smudge(args, **kw) -> str | None:
        envs.clear()
        gitsafe.run_git(args, **kw)
        return envs[-1][1].get("GIT_LFS_SKIP_SMUDGE")

    assert skip_smudge(["worktree", "list"], cwd=repo) == "1"
    assert skip_smudge(["status"], cwd=repo) == "1"
    assert skip_smudge(["status"], cwd=repo, run_hooks=True) is None
    assert skip_smudge(["clone", "--", str(repo), str(tmp_path / "c")]) is None


# --- submodules: a git started inside one reads that one's config ----------


def test_submodule_filter_drivers_never_fire(tmp_path, monkeypatch) -> None:
    from evi import review, worktree

    marker = tmp_path / "MARKER"
    _, filt = _scripts(tmp_path, marker)
    src = _make_repo(tmp_path / "src")
    sup = _make_repo(tmp_path / "super")
    _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "sub",
         cwd=sup)
    _git("commit", "-m", "add sub", cwd=sup)
    # A repository committed inside (a gitlink with no .gitmodules entry) too.
    inner = _make_repo(sup / "inner")
    _git("add", "inner", cwd=sup)
    _git("commit", "-m", "embed", cwd=sup)
    for d, name in ((sup / "sub", "subevil"), (inner, "innerevil")):
        _git("config", f"filter.{name}.clean", f"{filt} {name}", cwd=d)
        info = Path(_git("rev-parse", "--absolute-git-dir", cwd=d).strip()) / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text(f"* filter={name}\n", encoding="utf-8")

    def arm() -> None:
        _touch(sup / "sub" / "b.txt")
        _touch(inner / "b.txt")

    arm()
    _git("status", "--short", cwd=sup)
    fired = marker.read_text(encoding="utf-8")
    assert "subevil" in fired and "innerevil" in fired, "control: plain git status fires both"
    marker.unlink()

    results = {}
    for label, call in {
        "git_status": lambda: _tool("git_status", path=str(sup)),
        "git_diff": lambda: _tool("git_diff", path=str(sup)),
        "review": lambda: review.get_diff(cwd=sup),
        "dirty_files": lambda: worktree.dirty_files(sup),
        "git_status in sub": lambda: _tool("git_status", path=str(sup / "sub")),
    }.items():
        arm()
        results[label] = call()
        assert not marker.exists(), f"{label} ran a submodule's filter: {marker.read_text()}"
    # Still reported: inner's uncommitted edit, and a submodule is no
    # foreign work tree to the checks in front of these tools.
    assert "inner" in results["git_status"]
    assert any("inner" in line for line in results["dirty_files"])
    assert not results["git_status in sub"].startswith("ERROR"), results["git_status in sub"]
    overrides = gitsafe.repo_program_overrides(["status"], sup)
    assert "filter.subevil.clean=" in overrides and "filter.innerevil.clean=" in overrides


def test_lookup_only_for_subcommands_that_can_run_drivers(repo, monkeypatch) -> None:
    real = subprocess.run
    spawned: list[list[str]] = []

    def spy(argv, *a, **kw):
        spawned.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(gitsafe.subprocess, "run", spy)
    gitsafe.run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)  # status line
    assert len(spawned) == 1
    spawned.clear()
    gitsafe.run_git(["status", "--short"], cwd=repo)
    # the config lookup, the submodule listing, then status itself
    assert len(spawned) == 3 and "config" in spawned[0] and "ls-files" in spawned[1]
    spawned.clear()
    gitsafe.run_git(["-C", str(repo), "status"], run_hooks=True)  # eVi's own repo
    assert len(spawned) == 1


def test_git_argv_carries_the_hardening() -> None:
    argv = gitsafe.git_argv(["diff", "HEAD"])
    assert argv[0] == gitsafe.git_executable()
    cfg = dict(
        argv[i + 1].split("=", 1) for i in range(len(argv) - 1) if argv[i] == "-c"
    )
    assert cfg["core.fsmonitor"] == ""
    assert cfg["diff.external"] == ""
    assert cfg["core.pager"] == "cat"
    assert cfg["log.showSignature"] == "false"
    assert cfg["diff.submodule"] == "short"
    # An empty FILE: no hooks in it, and none can be installed into it.
    hooks = Path(cfg["core.hooksPath"])
    assert hooks.is_file() and hooks.stat().st_size == 0
    assert argv[-4:] == ["diff", "--no-ext-diff", "--no-textconv", "HEAD"]

    # Subcommand found after leading global options; blame gets --no-textconv.
    tail = ["-C", "x", "-c", "core.quotepath=false", "blame", "--", "f"]
    assert gitsafe.git_argv(tail)[-8:] == [
        "-C", "x", "-c", "core.quotepath=false", "blame", "--no-textconv", "--", "f",
    ]
    assert gitsafe.git_argv(["-C", "x", "ls-files"])[-3:] == ["-C", "x", "ls-files"]
    # Only eVi's own sync repo keeps hooks.
    assert not any(a.startswith("core.hooksPath=")
                   for a in gitsafe.git_argv(["status"], run_hooks=True))


def test_git_env_scrubs_redirects_and_disables_prompts(monkeypatch) -> None:
    scrubbed = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EXEC_PATH")
    for key in scrubbed:
        monkeypatch.setenv(key, "planted")
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    env = gitsafe.git_env()
    assert not set(scrubbed) & {k.upper() for k in env}
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_TERMINAL_PROMPT" not in gitsafe.git_env(prompt=True)


def test_inherited_git_dir_does_not_redirect_tools(repo, tmp_path, monkeypatch) -> None:
    # e.g. eVi launched from a git hook of ANOTHER repo exports GIT_DIR.
    other = _make_repo(tmp_path / "other", branch="elsewhere")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert json.loads(_tool("git_info", path=str(repo)))["branch"] == "main"


def test_shell_commands_can_keep_the_repo_env(monkeypatch) -> None:
    for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_EXTERNAL_DIFF",
                "GIT_PAGER", "GIT_EXEC_PATH"):
        monkeypatch.setenv(key, "x")
    env = {k.upper() for k in gitsafe.git_env(inherit_repo_env=True)}
    assert {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"} <= env
    assert not {"GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EXEC_PATH"} & env


def test_review_staged_in_a_pre_commit_hook_sees_the_index_being_committed(
    repo, tmp_path,
) -> None:
    """`git commit -a` runs pre-commit with GIT_INDEX_FILE pointing at the
    index it is about to commit; `evi review --staged --exit-code` used as
    that hook must review that index, not the (unchanged) default one."""
    import evi

    out = tmp_path / "reviewed.diff"
    runner = tmp_path / "hook_review.py"
    root = Path(evi.__file__).resolve().parent.parent
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from evi import review\n"
        f"open({str(out)!r}, 'w', encoding='utf-8').write(review.get_diff(staged=True))\n",
        encoding="utf-8",
    )
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\n"{Path(sys.executable).as_posix()}" "{runner.as_posix()}"\n'
        "exit 1\n",  # abort the commit either way
        encoding="utf-8", newline="\n",
    )
    hook.chmod(0o755)
    # a.txt carries an unstaged "wip" line; only `commit -a` stages it.
    assert _git("diff", "--cached", cwd=repo) == ""
    r = subprocess.run(["git", "commit", "-a", "-m", "x"], cwd=repo,
                       capture_output=True, text=True)
    assert out.exists(), r.stderr
    assert "+wip" in out.read_text(encoding="utf-8"), r.stderr


def test_git_executable_ignores_cwd_and_relative_path_entries(tmp_path, monkeypatch) -> None:
    real = gitsafe.git_executable()
    assert real and os.path.isabs(real)
    plant = tmp_path / "plant"
    plant.mkdir()
    fake = plant / ("git.exe" if os.name == "nt" else "git")
    fake.write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.chdir(plant)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "plant", os.path.dirname(real)]))
    assert gitsafe.git_executable() == real
    monkeypatch.setenv("PATH", os.pathsep.join([".", ""]))
    assert gitsafe.git_executable() is None
    with pytest.raises(gitsafe.GitNotFoundError):
        gitsafe.run_git(["--version"])


def test_hooks_path_replaced_when_not_a_plain_file(tmp_path, monkeypatch) -> None:
    mine = tmp_path / "hooks"
    mine.write_text("", encoding="utf-8")
    monkeypatch.setattr(gitsafe, "_hooks_path", str(mine))
    assert gitsafe.no_hooks_path() == str(mine)
    # A temp cleaner removed it, and someone made a directory of hooks there.
    mine.unlink()
    mine.mkdir()
    (mine / "post-checkout").write_text("#!/bin/sh\n", encoding="utf-8")
    fresh = gitsafe.no_hooks_path()
    assert fresh != str(mine)
    assert os.path.isfile(fresh) and os.path.getsize(fresh) == 0


def test_sync_keeps_user_hooks_and_prompts(tmp_path, monkeypatch) -> None:
    from evi import sync

    seen: list[dict] = []
    real = gitsafe.run_git

    def spy(args, cwd=None, timeout=None, **kw):
        seen.append(kw)
        return real(args, cwd, timeout, **kw)

    monkeypatch.setattr(gitsafe, "run_git", spy)
    sync.current_branch(tmp_path)
    assert seen == [{"prompt": True, "run_hooks": True}]


def test_plugin_clone_goes_through_gitsafe(tmp_path, monkeypatch) -> None:
    from evi import plugins

    src = tmp_path / "src"
    src.mkdir()
    _git("-c", "init.defaultBranch=main", "init", cwd=src)
    _git("config", "user.email", "evi@test.local", cwd=src)
    _git("config", "user.name", "evi", cwd=src)
    (src / "plugin.toml").write_text('name = "plug"\n', encoding="utf-8")
    _git("add", "plugin.toml", cwd=src)
    _git("commit", "-m", "plugin", cwd=src)
    bare = tmp_path / "plug.git"
    _git("clone", "--bare", str(src), str(bare), cwd=tmp_path)

    seen: list[list[str]] = []
    real = gitsafe.run_git

    def spy(args, cwd=None, timeout=None, **kw):
        seen.append(list(args))
        return real(args, cwd, timeout, **kw)

    monkeypatch.setattr(gitsafe, "run_git", spy)
    assert plugins.install(str(bare), root=tmp_path / "home") == "plug"
    assert seen and seen[0][0] == "clone"
    assert (tmp_path / "home" / "plugins" / "plug" / "plugin.toml").is_file()


# --- ref injection -----------------------------------------------------------

_BAD_REFS = [
    "--output={out}",
    "  --output={out}",
    "\t--output={out}",
    "--output",
    "--no-index",
    "--ext-diff",
    "--textconv",
    "-p",
    "-",
    "--",
    "HEAD\n--output={out}",
    "HEAD\x00",
]


@pytest.fixture(params=["current", "pre-2.24"])
def git_mode(request, monkeypatch) -> str:
    """Run each case with --end-of-options and with the old-git fallback."""
    if request.param == "pre-2.24":
        monkeypatch.setattr(gitsafe, "git_version", lambda: (2, 20, 0))
    return request.param


@pytest.mark.parametrize("tool", ["git_diff", "git_show"])
@pytest.mark.parametrize("template", _BAD_REFS)
def test_option_like_refs_are_refused(repo, tmp_path, git_mode, tool, template) -> None:
    out = tmp_path / "pwned.txt"
    result = _tool(tool, ref=template.format(out=out), path=str(repo))
    assert result.startswith("ERROR"), result
    assert not out.exists()


def test_git_diff_output_ref_creates_no_file(repo, tmp_path, git_mode) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    for target in (tmp_path / "new.txt", victim):
        assert _tool("git_diff", ref=f"--output={target}", path=str(repo)).startswith("ERROR")
    assert not (tmp_path / "new.txt").exists()
    assert victim.read_text(encoding="utf-8") == "keep me\n"  # not truncated


def test_git_diff_no_index_cannot_read_arbitrary_files(repo, tmp_path, git_mode) -> None:
    secret = tmp_path / "credentials"
    secret.write_text("TOPSECRET\n", encoding="utf-8")
    out = _tool(
        "git_diff", ref="--no-index", paths=f"{repo / 'a.txt'} {secret}", path=str(repo),
    )
    assert out.startswith("ERROR")
    assert "TOPSECRET" not in out


@pytest.fixture
def outside(tmp_path: Path) -> tuple[Path, Path]:
    """(a secret file next to the repo, a non-repo directory holding it)."""
    d = tmp_path / "outside"
    d.mkdir()
    secret = d / "credentials"
    secret.write_text("aws_secret_access_key=TOPSECRET\n", encoding="utf-8")
    (d / "empty").write_text("", encoding="utf-8")
    return secret, d


def _implicit_no_index_payloads(repo: Path, secret: Path, outside_dir: Path):
    sub = repo / "sub"
    sub.mkdir(exist_ok=True)
    rel = os.path.relpath(secret, repo).replace("\\", "/")
    return [
        # git turns "diff -- a b" with a or b outside the repo into --no-index
        {"paths": f"{repo / 'a.txt'} {secret}", "path": str(repo)},
        {"paths": f"{secret} {repo / 'a.txt'}", "path": str(repo)},
        {"paths": f"a.txt {rel}", "path": str(repo)},
        {"paths": f"a.txt sub/../{rel}", "path": str(repo)},
        {"paths": f"../a.txt ../{rel}", "path": str(sub)},
        {"paths": f"a.txt {secret}", "path": str(repo), "ref": "HEAD"},
        # ...and outside any repository every diff is --no-index
        {"paths": f"{outside_dir / 'empty'} {secret}", "path": str(outside_dir)},
        {"paths": "empty credentials", "path": str(outside_dir)},
    ]


def test_git_diff_paths_cannot_leave_the_repo(repo, outside) -> None:
    secret, outside_dir = outside
    # Control: plain git really does leak through this shape of call.
    plain = subprocess.run(["git", "diff", "--", str(repo / "a.txt"), str(secret)],
                           cwd=repo, capture_output=True, text=True)
    assert "TOPSECRET" in plain.stdout
    for kw in _implicit_no_index_payloads(repo, secret, outside_dir):
        out = _tool("git_diff", **kw)
        assert out.startswith("ERROR"), (kw, out)
        assert "TOPSECRET" not in out, kw


def test_git_diff_paths_inside_the_repo_still_work(repo) -> None:
    (repo / "sub").mkdir()
    for kw in (
        {"paths": "a.txt b.txt"},  # two paths: the shape git prescans
        {"paths": f"{repo / 'a.txt'} {repo / 'b.txt'}"},
        {"paths": "a.txt"},
        {"paths": "../a.txt ../b.txt", "path": str(repo / "sub")},
        {"paths": ":(glob)*.txt"},
        {"paths": "a.txt", "ref": "HEAD"},
    ):
        out = _tool("git_diff", **{"path": str(repo), **kw})
        assert "+wip" in out, (kw, out)


def test_hostile_core_worktree_is_refused(repo, tmp_path, monkeypatch) -> None:
    """A shipped .git/config with core.worktree=<elsewhere> (plus an index
    entry for a file there) would make status / diff / blame read files
    outside the repository."""
    from evi import review, worktree

    victim = tmp_path / "victim_home"
    victim.mkdir()
    (victim / "id_rsa").write_text("PRIVATEKEY\n", encoding="utf-8")
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input="x\n",
                          capture_output=True, text=True).stdout.strip()
    _git("update-index", "--add", "--cacheinfo", f"100644,{blob},id_rsa", cwd=repo)
    _git("config", "core.worktree", str(victim), cwd=repo)
    assert "PRIVATEKEY" in _git("diff", cwd=repo), "control: plain git reads the victim"

    for name, kw in (("git_status", {}), ("git_diff", {}), ("git_diff", {"ref": "HEAD"}),
                     ("git_diff", {"paths": "id_rsa"}), ("git_info", {}),
                     ("git_blame", {"file_path": "id_rsa"})):
        out = _tool(name, path=str(repo), **kw)
        assert out.startswith("ERROR") and "work tree" in out, (name, kw, out)
        assert "PRIVATEKEY" not in out
    with pytest.raises(review.ReviewError, match="work tree"):
        review.get_diff(cwd=repo)
    monkeypatch.chdir(repo)
    with pytest.raises(review.ReviewError, match="work tree"):
        review.get_diff(staged=True)
    with pytest.raises(worktree.WorktreeError, match="work tree"):
        worktree.create_worktree("feat", start=repo)
    assert not (victim / ".worktrees").exists()


def test_core_worktree_at_an_ancestor_is_refused(tmp_path, monkeypatch) -> None:
    """core.worktree naming an ANCESTOR (a downloaded repo pointing at the
    home directory): the repo directory is inside that work tree, so the
    "inside the work tree" check alone passed and diff read ~/.ssh/id_rsa."""
    from evi import review, worktree

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATEKEY-ANCESTOR\n", encoding="utf-8")
    (home / "Downloads").mkdir()
    repo = _make_repo(home / "Downloads" / "evil")
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input="x\n",
                          capture_output=True, text=True).stdout.strip()
    _git("config", "core.worktree", "../../..", cwd=repo)  # .git -> evil -> Downloads -> home
    _git("update-index", "--add", "--cacheinfo", f"100644,{blob},.ssh/id_rsa", cwd=repo)
    assert "PRIVATEKEY-ANCESTOR" in _git("diff", cwd=repo), "control: plain git reads it"

    for name, kw in (("git_status", {}), ("git_diff", {}), ("git_diff", {"ref": "HEAD"}),
                     ("git_info", {}), ("git_blame", {"file_path": "a.txt"})):
        out = _tool(name, path=str(repo), **kw)
        assert out.startswith("ERROR") and "work tree" in out, (name, kw, out)
        assert "PRIVATEKEY" not in out
    with pytest.raises(review.ReviewError, match="work tree"):
        review.get_diff(cwd=repo)
    monkeypatch.chdir(repo)
    with pytest.raises(review.ReviewError, match="work tree"):
        review.get_diff()
    with pytest.raises(worktree.WorktreeError, match="work tree"):
        worktree.create_worktree("feat", start=repo)
    assert not (home / ".worktrees").exists()


def test_own_work_trees_still_pass(repo, tmp_path, monkeypatch) -> None:
    """Subdirectories, linked worktrees (a .git file) and an inherited
    GIT_DIR / GIT_WORK_TREE chosen by the user are all the repo's own."""
    from evi import review

    (repo / "sub").mkdir()
    (repo / "sub" / "c.txt").write_text("c\n", encoding="utf-8")
    wt = tmp_path / "linked"
    _git("worktree", "add", "-b", "linked", str(wt), cwd=repo)
    (wt / "a.txt").write_text("linked edit\n", encoding="utf-8")
    out = _tool("git_status", path=str(repo / "sub"))
    assert "?? ./" in out and "../a.txt" in out, out
    assert "a.txt" in _tool("git_status", path=str(wt))
    assert "+linked edit" in review.get_diff(cwd=wt)
    assert gitsafe.is_own_work_tree(repo / "sub", "sub/")
    assert not gitsafe.is_own_work_tree(repo / "sub", "")
    assert not gitsafe.is_own_work_tree(repo, "x/")
    # The user's own GIT_DIR / GIT_WORK_TREE (a dotfiles repo, say) is theirs.
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    assert gitsafe.is_own_work_tree(tmp_path, "", inherit_repo_env=True)
    assert not gitsafe.is_own_work_tree(tmp_path, "")


def test_valid_refs_still_work(repo, git_mode) -> None:
    assert "+more" in _tool("git_diff", ref="HEAD~1..HEAD", path=str(repo))
    assert "+more" in _tool("git_diff", ref="  HEAD~1  ", path=str(repo))
    assert "+wip" in _tool("git_diff", ref="HEAD", path=str(repo))
    assert not _tool("git_diff", ref="--staged", path=str(repo)).startswith("ERROR")
    assert "second" in _tool("git_show", ref="HEAD", path=str(repo))
    assert "init" in _tool("git_show", ref=":/init", path=str(repo))


def test_review_refuses_option_like_range_and_branch(repo, tmp_path, git_mode) -> None:
    from evi.review import ReviewError, get_diff

    out = tmp_path / "pwned.txt"
    for kw in ({"range": f"--output={out}"}, {"range": "--no-index"},
               {"branch": f"--output={out}"}, {"branch": "-p"}):
        with pytest.raises(ReviewError, match="option"):
            get_diff(cwd=repo, **kw)
    assert not out.exists()
    assert "+more" in get_diff(range="HEAD~1..HEAD", cwd=repo)


def test_mcp_served_git_tools_inherit_validation(repo, tmp_path, outside) -> None:
    from evi.mcp import publish

    by_name = {t.name: t for t in publish.selected_tools(publish.DEFAULT_CATEGORIES)}
    assert {"git_diff", "git_show"} <= set(by_name)
    out = tmp_path / "mcp-pwned.txt"
    secret, outside_dir = outside
    results = [
        publish.dispatch(by_name, "git_diff", {"ref": f"--output={out}", "path": str(repo)}),
        publish.dispatch(by_name, "git_show", {"ref": f"--output={out}", "path": str(repo)}),
        publish.dispatch(by_name, "git_diff", {
            "ref": "--no-index", "paths": f"{repo / 'a.txt'} {secret}", "path": str(repo),
        }),
        # implicit --no-index: no ref at all
        *(publish.dispatch(by_name, "git_diff", kw)
          for kw in _implicit_no_index_payloads(repo, secret, outside_dir)),
    ]
    assert all(r.startswith("ERROR") for r in results), results
    assert not out.exists()
    assert not any("TOPSECRET" in r for r in results)
    # The same served tool still answers a legitimate call.
    assert "+more" in publish.dispatch(
        by_name, "git_diff", {"ref": "HEAD~1..HEAD", "path": str(repo)}
    )


# --- validation + version fallback -------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("git version 2.55.0.windows.5\n", (2, 55, 0)),
    ("git version 2.24.0", (2, 24, 0)),
    ("git version 2.17", (2, 17, 0)),
    ("git version 1.8.3.1", (1, 8, 3)),
    ("not git", None),
])
def test_parse_version(text, expected) -> None:
    assert gitsafe.parse_version(text) == expected


def test_real_git_version_is_known() -> None:
    v = gitsafe.git_version()
    assert v is not None and v >= (1, 0, 0)


@pytest.mark.parametrize("version,marker", [
    (None, False), ((2, 17, 0), False), ((2, 23, 9), False),
    ((2, 24, 0), True), ((2, 55, 0), True),
])
def test_end_of_options_only_when_supported(monkeypatch, version, marker) -> None:
    monkeypatch.setattr(gitsafe, "git_version", lambda: version)
    expected = ["--end-of-options", "HEAD"] if marker else ["HEAD"]
    assert gitsafe.rev_args("HEAD") == expected
    assert gitsafe.rev_args() == []
    # The fallback leans on validation, which applies either way.
    with pytest.raises(gitsafe.UnsafeRefError):
        gitsafe.rev_args("--output=x")


@pytest.mark.parametrize("ref", ["HEAD", "HEAD~3", "main..topic", "a...b", "v1.2^{}",
                                 ":/fix bug", "@{u}", "origin/main", "abc1234"])
def test_validate_ref_accepts_revisions(ref) -> None:
    assert gitsafe.validate_ref(f" {ref} ") == ref


@pytest.mark.parametrize("ref", ["", "   ", "-", "--x", "-p", "--output=f",
                                 "HEAD\n-p", "a\x00b", "a\x7fb"])
def test_validate_ref_rejects(ref) -> None:
    with pytest.raises(gitsafe.UnsafeRefError):
        gitsafe.validate_ref(ref)


@pytest.mark.parametrize("name", ["topic", "feature/x", "fix-1.2", "user/a_b", "a@b", "v2"])
def test_validate_branch_name_accepts(name) -> None:
    assert gitsafe.validate_branch_name(name) == name


@pytest.mark.parametrize("name", [
    "", "-b", "--orphan", "HEAD", "@", "a..b", "a b", "a~1", "a^", "a:b", "a?", "a*",
    "a[", "a\\b", "/a", "a/", "a.", ".a", "a/.b", "a.lock", "a/b.lock/c", "a//b",
    "a@{1}", "a\x01",
])
def test_validate_branch_name_rejects(name) -> None:
    with pytest.raises(gitsafe.UnsafeRefError):
        gitsafe.validate_branch_name(name)
