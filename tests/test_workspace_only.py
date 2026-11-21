"""`[auto] workspace_only`: no file reached outside the workspace.

"writes" refuses a call that writes outside the working folder, the trusted
dirs and the temp folder; "all" refuses reads there too. It holds in every
mode, yolo included, and over allow rules; symlinks and junctions are
followed; a repo can only raise it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import evi.tools.fs  # noqa: F401  registers write_file & co.
import evi.tools.shell  # noqa: F401  registers run_command
from evi import workspace
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.permissions import decide
from evi.tools.base import REGISTRY


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace folder, a folder outside it, and eVi's home elsewhere."""
    import evi.config as config_mod

    proj = tmp_path / "proj"
    out = tmp_path / "outside"
    home = tmp_path / "evi-home"
    for d in (proj, out, home):
        d.mkdir()
    monkeypatch.setattr(config_mod, "HOME", home)
    # Keep the temp folder out of the picture unless a test wants it.
    monkeypatch.setattr(workspace.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    return proj, out, home


def _d(tool: str, args: dict, proj: Path, level: str, *, mode: str = "yolo",
       rules=(), trusted=(), auto=("fs", "code_read")) -> str:
    t = REGISTRY[tool]
    a = json.dumps(args)
    return decide(mode, list(auto), list(rules), tool, t.category, a, list(trusted),
                  workdir=str(proj), path_args=t.path_args(a), read_only=t.read_only,
                  call_category=t.category_for(a), dialect="bash", workspace_only=level)


def test_off_changes_nothing(ws):
    proj, out, _ = ws
    assert _d("write_file", {"path": str(out / "x.txt"), "content": "x"}, proj, "off") == "allow"


@pytest.mark.parametrize("level", ["writes", "all"])
def test_a_write_outside_is_denied_in_yolo(ws, level):
    proj, out, _ = ws
    assert _d("write_file", {"path": str(out / "x.txt"), "content": "x"}, proj, level) == "deny"
    assert _d("write_file", {"path": "../outside/x.txt", "content": "x"}, proj, level) == "deny"
    assert _d("write_file", {"path": "sub/x.txt", "content": "x"}, proj, level) == "allow"


def test_an_allow_rule_does_not_clear_it(ws):
    proj, out, _ = ws
    args = {"path": str(out / "x.txt"), "content": "x"}
    assert _d("write_file", args, proj, "writes", mode="ask", rules=["allow write_file"]) == "deny"


def test_reads_only_with_all(ws):
    proj, out, _ = ws
    (out / "notes.txt").write_text("n", encoding="utf-8")
    args = {"path": str(out / "notes.txt")}
    assert _d("read_file", args, proj, "writes") == "allow"
    assert _d("read_file", args, proj, "all") == "deny"
    assert _d("read_file", {"path": "inside.txt"}, proj, "all") == "allow"


def test_trusted_dirs_and_temp_are_workspace(ws, tmp_path, monkeypatch):
    proj, out, _ = ws
    args = {"path": str(out / "x.txt"), "content": "x"}
    assert _d("write_file", args, proj, "all", trusted=[str(out)]) == "allow"
    monkeypatch.setattr(workspace.tempfile, "gettempdir", lambda: str(out))
    assert _d("write_file", args, proj, "all") == "allow"


def test_temp_can_be_left_out(ws, monkeypatch):
    proj, out, _ = ws
    monkeypatch.setattr(workspace.tempfile, "gettempdir", lambda: str(out))
    t = REGISTRY["write_file"]
    a = json.dumps({"path": str(out / "x.txt"), "content": "x"})
    assert decide("yolo", [], [], "write_file", t.category, a, workdir=str(proj),
                  path_args=t.path_args(a), workspace_only="writes",
                  workspace_allow_temp=False) == "deny"


def test_evi_own_read_folders_stay_readable_with_all(ws):
    proj, _, home = ws
    spill = home / "tool-output" / "s1" / "c1.txt"
    spill.parent.mkdir(parents=True)
    spill.write_text("spilled", encoding="utf-8")
    assert _d("read_file", {"path": str(spill)}, proj, "all") == "allow"
    assert _d("read_file", {"path": str(home / "config.toml")}, proj, "all") == "deny"
    # ...readable, not writable.
    assert _d("write_file", {"path": str(spill), "content": "x"}, proj, "all") == "deny"


def test_a_glob_that_climbs_out_is_denied_with_all(ws):
    proj, _, _ = ws
    assert _d("find_files", {"pattern": "../**/*.txt"}, proj, "all") == "deny"
    assert _d("find_files", {"pattern": "**/*.py"}, proj, "all") == "allow"


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # No symlink privilege on Windows: a junction needs none.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)


def test_a_link_inside_that_leads_out_is_outside(ws):
    proj, out, _ = ws
    _link(proj / "escape", out)
    assert _d("write_file", {"path": "escape/x.txt", "content": "x"}, proj, "writes") == "deny"
    assert _d("read_file", {"path": "escape/x.txt"}, proj, "all") == "deny"


@pytest.mark.parametrize("line", [
    "echo x > ../outside/y.txt",
    "echo x | tee ../outside/y.txt",
    "cp a.txt ../outside/y.txt",
    "cd .. && echo x > outside/y.txt",
])
def test_shell_writes_outside_are_denied(ws, line):
    proj, _, _ = ws
    assert _d("run_command", {"command": line}, proj, "writes", auto=("shell",)) == "deny"


def test_shell_writes_inside_pass(ws):
    proj, _, _ = ws
    assert _d("run_command", {"command": "echo x > y.txt && ls"}, proj, "writes",
              auto=("shell",)) == "allow"


def test_powershell_set_content_outside_is_denied(ws):
    proj, out, _ = ws
    t = REGISTRY["run_command"]
    a = json.dumps({"command": f"Set-Content -Path {out / 'y.txt'} -Value x"})
    assert decide("yolo", ["shell"], [], "run_command", t.category, a, workdir=str(proj),
                  path_args=t.path_args(a), dialect="pwsh",
                  workspace_only="writes") == "deny"


def test_a_relative_write_after_an_unknown_cd_is_outside(ws):
    proj, _, _ = ws
    assert _d("run_command", {"command": "cd $SOMEWHERE && echo x > y.txt"}, proj, "writes",
              auto=("shell",)) == "deny"


def test_an_unknown_level_is_the_strictest():
    assert workspace.level_of("wrties") == "all"
    assert workspace.level_of(None) == "off" and workspace.level_of(" Writes ") == "writes"


# ---- the agent loop ----------------------------------------------------------


def test_agent_denies_in_yolo_and_says_why(ws, fake_llm):
    proj, out, _ = ws
    cfg = Config()
    cfg.auto.workspace_only = "writes"
    cfg.tools.memory = cfg.tools.skills = cfg.tools.transcripts = False
    client = fake_llm([
        fake_llm.call(("write_file", {"path": str(out / "x.txt"), "content": "x"}),
                      ("write_file", {"path": "ok.txt", "content": "ok"})),
        fake_llm.text("done"),
    ])
    agent = Agent(client=client, config=cfg, tools=[REGISTRY["write_file"]])
    agent.cwd = str(proj)
    agent.enable_auto_all()
    results = [e for e in agent.chat("go") if isinstance(e, ToolResult)]
    assert "outside the workspace" in results[0].output
    assert 'workspace_only = "writes"' in results[0].output
    assert not (out / "x.txt").exists()
    assert (proj / "ok.txt").read_text(encoding="utf-8") == "ok"


# ---- project config -------------------------------------------------------------


def test_a_project_can_raise_it_but_not_lower_it():
    from evi.project import merge_project_overlay

    raised, ignored = merge_project_overlay({"auto": {"workspace_only": "writes"}},
                                            {"auto": {"workspace_only": "all"}})
    assert raised["auto"]["workspace_only"] == "all" and not ignored
    kept, ignored = merge_project_overlay({"auto": {"workspace_only": "all"}},
                                          {"auto": {"workspace_only": "off"}})
    assert kept["auto"]["workspace_only"] == "all" and "auto.workspace_only" in ignored
    temp, _ = merge_project_overlay({}, {"auto": {"workspace_allow_temp": False}})
    assert temp["auto"]["workspace_allow_temp"] is False
    _, ignored = merge_project_overlay({}, {"auto": {"workspace_allow_temp": True}})
    assert "auto.workspace_allow_temp" in ignored


def test_lint_flags_an_unknown_level():
    from evi.configlint import lint_auto

    [issue] = lint_auto({"workspace_only": "wrties"})
    assert "workspace_only" in issue.resource and "'all'" in issue.message
