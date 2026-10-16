"""Code execution stops riding the "code" auto-approval (Phase 0,
safety-code-exec-parity).

`monitor` kind=command ran a shell line (``shell=True``, no sandbox) under the
"code" category, which every config auto-approved, even with
``[tools] shell = false``; and run_python ran unasked the same way. Now:

- every call is checked under its own category (`Tool.category_for`,
  `permissions.classify_call`): monitor kind=command is "shell", reading code
  (python_symbols, monitor tailing a file, check_file with ruff) is
  "code_read", and "code" means running code (run_python, a linter that runs
  project code), which a sandboxed run_python is not;
- the default auto_approve is fs, code_read, memory, skills, image, so
  run_python asks unless sandboxed, and a list naming "code" still covers
  "code_read";
- an allow rule never approves a shell line by its tool's category (`allow
  code` and monitor kind=command) or running code by "code_read";
- monitor refuses command mode in a session that doesn't offer the shell
  tools, runs it under the OS sandbox when that is on, and a file it tails is
  a path argument (a protected secret asks);
- decided for a remote-triggered run (`decide(remote_trigger=True)`), code
  execution is never approved by a category, accept_edits or a trusted dir;
  a local unattended run (the scheduler, a Telegram turn) goes by the list;
- `evi lint` and `evi doctor` flag a list saved with the old default's "code"
  and give the migrated one;
- the tool list is unchanged by name: the four tools stay in "code".

The agent-loop tests drive the real Agent against the scripted `fake_llm`
client from conftest.py, under a tmp eVi home. Four of them are strict
xfails: they need the agent wiring this item leaves to the director (pass
`category_for` and `remote_trigger` into `decide`, and use them in the
agent's pre-approval check and its prompts); once it lands they pass, and the
markers come off.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import evi.tools.code  # noqa: F401  registers run_python, python_symbols, check_file
import evi.tools.fs  # noqa: F401  registers write_file & co.
import evi.tools.monitor  # noqa: F401  registers monitor
import evi.tools.shell  # noqa: F401  registers run_command
from evi import hooks as hooks_mod
from evi import sandbox
from evi.config import AutoSettings, Config, ToolToggles
from evi.llm.agent import Agent, ToolResult, is_permission_denial, remote_unattended
from evi.permissions import (
    allow_rule_keys,
    approved_by_category,
    approved_categories,
    auto_approve_migration,
    classify_call,
    decide,
    effective_protected_paths,
    rule_keys,
    runs_code,
)
from evi.tools.base import REGISTRY, ToolContext, get_enabled_tools

CODE_TOOLS = ("run_python", "python_symbols", "check_file", "monitor")
DEFAULT = ["fs", "code_read", "memory", "skills", "image"]
LEGACY = ["fs", "code", "memory", "skills", "image"]
# Creates marker.txt in the folder it runs in, in cmd.exe and sh alike.
MARK = "echo hi > marker.txt"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home and config under tmp_path (never ~/.evi), no safe mode, and
    a process cwd of its own (where monitor's commands run)."""
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.guardrails.GUARDRAILS_PATH", evi_home / "guardrails.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    cwd = tmp_path / "process-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return evi_home


def _args(**kw) -> str:
    return json.dumps(kw)


def _mon(target, kind: str = "command", **kw) -> str:
    return json.dumps({"target": target, "kind": kind, **kw})


def _cfg(*, shell: bool = False, sandbox_on: bool = False,
         auto_approve: list[str] | None = None) -> Config:
    cfg = Config()
    cfg.tools.shell = shell
    cfg.tools.sandbox = sandbox_on
    cfg.tools.memory = cfg.tools.skills = cfg.tools.transcripts = False
    if auto_approve is not None:
        cfg.auto.auto_approve = list(auto_approve)
    return cfg


def _ctx(**kw) -> ToolContext:
    return ToolContext(agent=SimpleNamespace(config=_cfg(**kw)))


@pytest.fixture
def no_spawn(monkeypatch) -> list:
    """Fail the test if anything starts a process; returns what tried to."""
    tried: list = []

    def popen(*a, **k):
        tried.append(a)
        raise AssertionError(f"a process was started: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", popen)
    return tried


def _results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


# ---- 1. one call, one category ---------------------------------------------


def test_static_categories_and_the_tool_list_are_unchanged():
    # The four tools stay in the "code" toggle / group, so the [tools] code
    # toggle, the modes and every category list still pick them, by name.
    from evi.modes import mode_tools

    assert {n: REGISTRY[n].category for n in CODE_TOOLS} == dict.fromkeys(CODE_TOOLS, "code")
    on = {t.name for t in get_enabled_tools(asdict(ToolToggles()))}
    assert set(CODE_TOOLS) <= on
    # [tools] shell is off by default: no shell tool is enabled, and monitor,
    # the one enabled tool that can run a shell line, refuses to (section 3).
    assert ToolToggles().shell is False
    assert not [t for t in get_enabled_tools(asdict(ToolToggles())) if t.category == "shell"]
    assert sorted(t.name for t in get_enabled_tools(asdict(ToolToggles()))
                  if t.category == "code") == sorted(CODE_TOOLS)
    assert set(CODE_TOOLS) <= {t.name for t in mode_tools("code")}
    # monitor's ToolContext parameter never reaches the schema
    schema = REGISTRY["monitor"].openai_schema()["function"]
    assert schema["name"] == "monitor"
    assert set(schema["parameters"]["properties"]) == {"target", "kind", "until", "timeout"}
    assert schema["parameters"]["required"] == ["target"]
    assert "[tools] shell" in schema["description"]


@pytest.mark.parametrize("name, args, expected", [
    ("monitor", _mon("app.log", "file"), "code_read"),
    ("monitor", _mon("make build"), "shell"),
    # a command call the shell parser can't read is still a shell call
    ("monitor", _mon(["rm -rf ~"]), "shell"),
    ("monitor", "not json", "shell"),
    ("monitor", _mon("x", "Command"), "code_read"),  # not a command: refused by the tool
    ("python_symbols", _args(path="a.py"), "code_read"),
    ("check_file", _args(path="a.py"), "code_read"),   # ruff / pyflakes
    ("check_file", _args(path="notes.txt"), "code_read"),  # no linter runs
    ("check_file", _args(path="a.js"), "code"),       # eslint loads eslint.config.js
    ("check_file", _args(path="lib.rs"), "code"),     # cargo clippy runs build.rs
    ("check_file", _args(path="main.go"), "code"),
    ("check_file", _args(), "code"),
    ("run_python", _args(code="print(1)"), "code"),
    ("run_command", _args(command="ls"), "shell"),
    ("write_file", _args(path="a", content="b"), "fs"),
])
def test_category_for(home, name, args, expected):
    assert REGISTRY[name].category_for(args) == expected


def test_classify_call_is_the_pure_part():
    # decide() falls back to it when the caller passes no call category: a
    # shell line, the always-read-only tools and check_file (by the file's
    # extension) are classified; only a hook's answer (a sandboxed run_python,
    # which reads the config) is not.
    assert classify_call("monitor", "code", _mon("x", "file")) == "code_read"
    assert classify_call("monitor", "code", _mon("ls")) == "shell"
    assert classify_call("python_symbols", "code", _args(path="a.py")) == "code_read"
    assert classify_call("check_file", "code", _args(path="a.py")) == "code_read"
    assert classify_call("check_file", "code", _args(path="pkg/a.PY")) == "code_read"
    assert classify_call("check_file", "code", _args(path="a.js")) == "code"
    assert classify_call("check_file", "code", _args(path=["a.py"])) == "code"
    assert classify_call("check_file", "code", "not json") == "code"
    assert classify_call("run_python", "code", _args(code="1")) == "code"
    # a same-named tool in another category is left alone
    assert classify_call("python_symbols", "mcp", "{}") == "mcp"


def test_run_python_is_code_read_only_while_a_sandbox_is_active(home, monkeypatch):
    rp = REGISTRY["run_python"]
    (home / "config.toml").write_text("[tools]\nsandbox = true\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "available", lambda: False)
    assert sandbox.requested() and not sandbox.active()
    assert rp.category_for(_args(code="1")) == "code"  # requested but no sandboxer
    monkeypatch.setattr(sandbox, "available", lambda: True)
    assert sandbox.active() and rp.category_for(_args(code="1")) == "code_read"
    (home / "config.toml").write_text("[tools]\nsandbox = false\n", encoding="utf-8")
    assert not sandbox.active() and rp.category_for(_args(code="1")) == "code"
    # an explicit config wins over the file
    assert sandbox.active(SimpleNamespace(tools=SimpleNamespace(sandbox=True)))
    assert not sandbox.requested(SimpleNamespace(tools=None))


def test_run_python_still_runs_sandboxed(home, monkeypatch):
    # The sandbox check moved to sandbox.requested(): run_python still wraps.
    (home / "config.toml").write_text("[tools]\nsandbox = true\n", encoding="utf-8")
    seen: list = []

    def wrap(argv, workdir, allow_network=False):
        seen.append((list(argv), allow_network))
        return [sys.executable, "-c", "print('sandboxed')"]

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "wrap", wrap)
    out = REGISTRY["run_python"].call(_args(code="print('plain')"))
    assert "sandboxed" in out and seen and seen[0][1] is False


# ---- 2. the policy ---------------------------------------------------------


def _d(name: str, args: str, auto: list[str] | None = None, *, mode: str = "ask",
       rules: list[str] | None = None, hook: bool = True, **kw) -> str:
    """decide() for a registered tool, passing its call category the way the
    agent does (`Tool.category_for`) unless hook=False."""
    tool = REGISTRY[name]
    if hook:
        kw.setdefault("call_category", tool.category_for(args))
    return decide(mode, DEFAULT if auto is None else auto, rules or [], name, tool.category,
                  args, path_args=tool.path_args(args), read_only=tool.read_only,
                  dialect="bash", **kw)


def test_default_auto_approve():
    assert AutoSettings().auto_approve == DEFAULT
    assert Config().auto.auto_approve == DEFAULT


def test_default_config_reads_code_unasked_and_asks_to_run_it(home, tmp_path):
    assert _d("python_symbols", _args(path="a.py")) == "allow"
    assert _d("check_file", _args(path="a.py")) == "allow"
    assert _d("monitor", _mon("app.log", "file")) == "allow"  # auto-approvable as before
    assert _d("check_file", _args(path="a.js")) == "ask"
    assert _d("run_python", _args(code="print(1)")) == "ask"
    assert _d("monitor", _mon("npm test")) == "ask"
    # without the per-call category (no agent wiring) the policy classifies
    # all but a sandboxed run_python itself, which then asks (fails closed)
    assert _d("python_symbols", _args(path="a.py"), hook=False) == "allow"
    assert _d("monitor", _mon("app.log", "file"), hook=False) == "allow"
    assert _d("check_file", _args(path="a.py"), hook=False) == "allow"
    assert _d("check_file", _args(path="a.js"), hook=False) == "ask"


def test_a_sandboxed_run_python_is_approved_like_reading_code(home):
    assert _d("run_python", _args(code="1"), call_category="code_read") == "allow"
    assert _d("run_python", _args(code="1"), [], call_category="code_read") == "ask"


def test_monitor_command_is_a_shell_call_even_with_code_approved(home):
    for auto in (DEFAULT, LEGACY, ["code", "code_read"]):
        assert _d("monitor", _mon("npm test"), auto) == "ask"
        # whatever category a caller claims for it
        assert _d("monitor", _mon("npm test"), auto, call_category="code_read") == "ask"
    assert _d("monitor", _mon("npm test"), [*DEFAULT, "shell"]) == "allow"
    assert _d("monitor", _mon("npm test"), mode="accept_edits") == "ask"


def test_code_still_covers_code_read(home):
    # A list saved with the old default keeps reading code unasked, and (at the
    # desk) running it too: the user listed it.
    assert approved_categories(LEGACY) >= {"code", "code_read"}
    assert approved_categories(DEFAULT) == set(DEFAULT)
    assert _d("python_symbols", _args(path="a.py"), LEGACY) == "allow"
    assert _d("monitor", _mon("app.log", "file"), LEGACY) == "allow"
    assert _d("run_python", _args(code="1"), LEGACY) == "allow"


def test_a_remote_trigger_never_approves_running_code_by_category(home):
    u = {"remote_trigger": True}
    for name, args in (
        ("run_python", _args(code="1")),
        ("check_file", _args(path="a.js")),
        ("monitor", _mon("npm test")),
        ("run_command", _args(command="npm test")),
    ):
        assert _d(name, args, [*LEGACY, "shell"], **u) == "ask", name
        assert _d(name, args, mode="accept_edits", **u) == "ask", name
    # sandboxed or not, and whatever rule names "code_read"
    assert _d("run_python", _args(code="1"), call_category="code_read", **u) == "ask"
    assert _d("run_python", _args(code="1"), rules=["allow code_read"],
              call_category="code_read", **u) == "ask"
    # reading code still runs, under "code" or "code_read"
    for auto in (DEFAULT, LEGACY):
        assert _d("python_symbols", _args(path="a.py"), auto, **u) == "allow"
        assert _d("monitor", _mon("app.log", "file"), auto, **u) == "allow"
        assert _d("check_file", _args(path="a.py"), auto, **u) == "allow"
    # an explicit allow rule is the user's own call (and yolo, which the agent
    # never gives a remote-triggered run)
    assert _d("run_python", _args(code="1"), rules=["allow run_python"], **u) == "allow"
    assert _d("run_python", _args(code="1"), mode="yolo", **u) == "allow"
    # and a deny still denies
    assert _d("run_python", _args(code="1"), LEGACY, rules=["deny code"], **u) == "deny"


def test_a_local_unattended_run_goes_by_the_list(home):
    # Not a remote trigger (the scheduler, `evi run`, a teammate, a Telegram
    # turn): the categories the user listed approve what they name, "shell"
    # and "code" included; the default list approves no code execution.
    assert _d("run_command", _args(command="npm test"), [*DEFAULT, "shell"]) == "allow"
    assert _d("monitor", _mon("npm test"), [*DEFAULT, "shell"]) == "allow"
    assert _d("run_python", _args(code="1"), ["code"]) == "allow"
    assert _d("run_python", _args(code="1"), LEGACY) == "allow"  # until migrated
    assert _d("run_python", _args(code="1")) == "ask"  # the default
    for auto in ([*DEFAULT, "shell"], ["code"], LEGACY):
        assert approved_by_category(auto, "run_command", "shell", _args(command="x")) == (
            "shell" in auto)
        assert approved_by_category(auto, "run_command", "shell", _args(command="x"),
                                    remote_trigger=True) is False


def test_runs_code():
    assert runs_code("run_python", "code", "code")
    assert runs_code("run_python", "code", "code_read")  # sandboxed: still code
    assert runs_code("monitor", "code", "shell") and runs_code("check_file", "code", "code")
    assert not runs_code("python_symbols", "code", "code_read")
    assert not runs_code("monitor", "code", "code_read")
    assert not runs_code("run_python", "mcp", "mcp")  # an MCP tool of that name


def test_approved_by_category_is_decides_category_step(home):
    rp, mon = REGISTRY["run_python"], REGISTRY["monitor"]
    assert approved_by_category(LEGACY, "run_python", "code", _args(code="1"))
    assert not approved_by_category(DEFAULT, "run_python", "code", _args(code="1"))
    assert approved_by_category(DEFAULT, "run_python", "code", _args(code="1"),
                                call_category="code_read")
    assert not approved_by_category(LEGACY, "run_python", "code", _args(code="1"),
                                    remote_trigger=True)
    assert not approved_by_category(DEFAULT, "run_python", "code", _args(code="1"),
                                    call_category="code_read", remote_trigger=True)
    assert not approved_by_category(LEGACY, "monitor", "code", _mon("ls"),
                                    call_category="code_read")
    assert approved_by_category(DEFAULT, mon.name, mon.category, _mon("a.log", "file"),
                                call_category=mon.category_for(_mon("a.log", "file")))
    assert rp.category == "code"


def test_accept_edits_allows_edits_and_reading_code_not_running_it(home):
    assert _d("write_file", _args(path="a.txt", content="x"), [], mode="accept_edits") == "allow"
    assert _d("python_symbols", _args(path="a.py"), [], mode="accept_edits") == "allow"
    assert _d("monitor", _mon("a.log", "file"), [], mode="accept_edits") == "allow"
    assert _d("run_python", _args(code="1"), [], mode="accept_edits") == "ask"
    assert _d("check_file", _args(path="a.js"), [], mode="accept_edits") == "ask"


def test_a_trusted_dir_approves_reading_code_not_running_it(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    td = {"trusted_dirs": [str(proj)], "workdir": proj}
    assert _d("python_symbols", _args(path=str(proj / "a.py")), [], **td) == "allow"
    assert _d("check_file", _args(path=str(proj / "a.py")), [], **td) == "allow"
    assert _d("check_file", _args(path=str(proj / "a.js")), [], **td) == "ask"


def test_reading_a_secret_with_code_read_still_asks(home):
    pp = {"protected_paths": [".env"]}
    assert _d("python_symbols", _args(path=".env"), **pp) == "ask"
    assert _d("check_file", _args(path="x/.env"), LEGACY, **pp) == "ask"


def test_monitor_tailing_a_secret_asks(home):
    # The file monitor tails is its path argument (`Tool.path_args`), so the
    # secret-read check sees it, as it does python_symbols' path.
    mon = REGISTRY["monitor"]
    assert mon.path_args(_mon("app.log", "file")) == ["app.log"]
    assert mon.path_args(json.dumps({"target": "app.log"})) == ["app.log"]  # kind=file default
    assert mon.path_args(_mon("cat .env")) == []  # a command line is no path
    pp = {"protected_paths": effective_protected_paths()}
    for target in (".env", "sub/.env", "~/.ssh/id_rsa", "certs/key.pem"):
        for auto in (DEFAULT, LEGACY):
            assert _d("monitor", _mon(target, "file"), auto, **pp) == "ask", target
            assert _d("monitor", _mon(target, "file"), auto, hook=False, **pp) == "ask", target
    # an explicit allow rule still wins, as for any secret read
    assert _d("monitor", _mon(".env", "file"), rules=["allow monitor"], **pp) == "allow"
    # tailing a write-protected file (eVi's own log) only reads it: no prompt
    log = str(home / "logs" / "web.log")
    assert _d("monitor", _mon(log, "file"), **pp) == "allow"
    assert _d("monitor", _mon("AGENTS.md", "file"), **pp) == "allow"
    assert _d("monitor", _mon("app.log", "file"), **pp) == "allow"


def test_rules_match_the_call_category(home):
    assert _d("monitor", _mon("a.log", "file"), [], rules=["allow code_read"]) == "allow"
    assert _d("python_symbols", _args(path="a.py"), rules=["deny code_read"]) == "deny"
    assert _d("monitor", _mon("npm test"), [*DEFAULT, "shell"], rules=["deny shell"]) == "deny"
    # hard_deny "shell" covers a command call the shell parser can't read
    assert _d("monitor", _mon(["rm -rf ~"]), mode="yolo", hard_deny=["shell"]) == "deny"
    # a rule naming the tool's own category still applies
    assert _d("python_symbols", _args(path="a.py"), rules=["deny code"]) == "deny"
    assert rule_keys("monitor", "code", None, "code_read") == ("monitor", "code", "code_read")
    assert rule_keys("monitor", "code", "ls", "shell") == ("monitor", "code", "shell")


@pytest.mark.parametrize("rule", ["allow code", "allow code npm*", "allow c*"])
def test_allow_code_never_approves_a_shell_line(home, rule):
    # `allow code` (written to keep run_python unasked after the migration)
    # must not approve shell lines through monitor: run_command asks for it.
    for auto in (DEFAULT, LEGACY):
        assert _d("monitor", _mon("npm test"), auto, rules=[rule]) == "ask"
        assert _d("run_command", _args(command="npm test"), auto, rules=[rule]) == "ask"
    # a shell line the parser can't read too
    assert _d("monitor", _mon(["npm test"]), rules=[rule]) == "ask"
    # it still covers running and reading code
    if rule != "allow code npm*":
        assert _d("run_python", _args(code="1"), [], rules=[rule]) == "allow"
        assert _d("python_symbols", _args(path="a.py"), [], rules=[rule]) == "allow"


def test_rules_for_a_shell_line_name_the_tool_or_shell(home):
    for rule in ("allow monitor", "allow shell", "allow monitor npm*", "allow shell npm*"):
        assert _d("monitor", _mon("npm test"), rules=[rule]) == "allow", rule
    # a deny rule still matches the tool's own category: it never loosens
    assert _d("monitor", _mon("npm test"), [*DEFAULT, "shell"], rules=["deny code"]) == "deny"
    assert _d("monitor", _mon("npm test"), mode="yolo", hard_deny=["code npm*"]) == "deny"


def test_the_guards_rule_check_never_allows_by_the_tools_category(home):
    # The destructive guard (`Agent._destructive_decision`) asks
    # shell_rule_action with only `rule_keys`: `allow code git*` must clear a
    # guard prompt for monitor kind=command no more than for run_command.
    from evi.permissions import shell_rule_action

    cmd = "git reset --hard"

    def act(rule, name="monitor", category="code"):
        return shell_rule_action([rule], rule_keys(name, category, cmd), [cmd], cmd,
                                 broad=False)

    assert act("allow code git*") is None
    assert act("allow code git*", "run_command", "shell") is None
    for rule in ("allow monitor git*", "allow shell git*"):
        assert act(rule) == "allow", rule
    assert act("allow shell git*", "run_command", "shell") == "allow"
    # a deny rule still matches the tool's category
    assert act("deny code git*") == "deny"


def test_allow_code_read_never_approves_running_code(home):
    sandboxed = {"call_category": "code_read"}
    assert _d("run_python", _args(code="1"), [], rules=["allow code_read"], **sandboxed) == "ask"
    assert _d("run_python", _args(code="1"), [], rules=["allow code"], **sandboxed) == "allow"
    assert _d("python_symbols", _args(path="a.py"), [], rules=["allow code_read"]) == "allow"
    # a deny rule naming code_read still denies a sandboxed run
    assert _d("run_python", _args(code="1"), rules=["deny code_read"], **sandboxed) == "deny"


def test_allow_rule_keys():
    assert allow_rule_keys("monitor", "code", "ls", "shell") == ("monitor", "shell")
    assert allow_rule_keys("monitor", "code", None, "shell") == ("monitor", "shell")
    assert allow_rule_keys("run_command", "shell", "ls", "shell") == ("run_command", "shell")
    assert allow_rule_keys("run_python", "code", None, "code_read") == ("run_python", "code")
    assert allow_rule_keys("run_python", "code", None, "code") == ("run_python", "code")
    assert allow_rule_keys("monitor", "code", None, "code_read") == (
        "monitor", "code", "code_read")


# ---- 3. the monitor tool ----------------------------------------------------


def test_monitor_refuses_command_mode_while_the_shell_is_off(home, no_spawn):
    mon = REGISTRY["monitor"]
    out = mon.call(_mon(MARK), _ctx(shell=False))
    assert out.startswith("ERROR: kind='command'") and "[tools] shell" in out
    # no session: the config file's [tools] shell (off by default)
    assert mon.call(_mon(MARK)).startswith("ERROR: kind='command'")
    assert not no_spawn and not (Path.cwd() / "marker.txt").exists()


def test_monitor_reads_the_sessions_shell_toggle(home, monkeypatch):
    # The session's own config decides, not whatever config.toml says now.
    (home / "config.toml").write_text("[tools]\nshell = true\n", encoding="utf-8")
    mon = REGISTRY["monitor"]
    assert mon.call(_mon(MARK), _ctx(shell=False)).startswith("ERROR: kind='command'")
    out = mon.call(_mon(f'"{sys.executable}" -c "print(42)"', timeout=15), _ctx(shell=True))
    assert "42" in out and "exited 0" in out
    # no session: config.toml's value
    out = mon.call(_mon(f'"{sys.executable}" -c "print(43)"', timeout=15))
    assert "43" in out


def _session(tools: list[str], **cfg) -> ToolContext:
    return ToolContext(agent=SimpleNamespace(
        config=_cfg(**cfg), tools={n: REGISTRY[n] for n in tools}))


def test_monitor_runs_a_command_exactly_where_run_command_is_offered(home, monkeypatch):
    # A session's tool set says whether it offers the shell tools: [tools]
    # shell put run_command there, or a mode that turns them on did (code
    # mode, whatever [tools] shell says). One without them (a teammate given
    # fs + code) gets no command mode, even with [tools] shell on.
    from evi.modes import mode_tools

    mon = REGISTRY["monitor"]
    cmd = _mon(f'"{sys.executable}" -c "print(44)"', timeout=15)
    out = mon.call(cmd, _session(["monitor", "run_command"], shell=False))
    assert "44" in out and "exited 0" in out
    code_mode = ToolContext(agent=SimpleNamespace(
        config=_cfg(shell=False), tools={t.name: t for t in mode_tools("code")}))
    assert "44" in mon.call(cmd, code_mode)
    spawned: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    for tools in (["monitor", "run_python", "write_file"], ["monitor"], []):
        out = mon.call(_mon(MARK), _session(tools, shell=True))
        assert out.startswith("ERROR: kind='command'") and "shell tools" in out
    assert not spawned
    # tailing a file needs no shell tool
    log = Path.cwd() / "app.log"
    log.write_text("", encoding="utf-8")
    assert "no new lines" in mon.call(_mon(str(log), "file", timeout=1), _session(["monitor"]))


@pytest.mark.parametrize("args, msg", [
    (_mon("x", "Command"), "ERROR: kind must be"),
    (_mon("x", "cmd"), "ERROR: kind must be"),
    (_mon(["rm -rf ~"]), "ERROR: target must be"),
    (_mon("   "), "ERROR: target must be"),
])
def test_monitor_rejects_what_it_cant_classify(home, no_spawn, args, msg):
    assert REGISTRY["monitor"].call(args, _ctx(shell=True)).startswith(msg)
    assert not no_spawn


def test_monitor_command_runs_under_the_sandbox(home, monkeypatch):
    seen: list = []

    def wrap(argv, workdir, allow_network=False):
        seen.append((list(argv), str(workdir), allow_network))
        return [sys.executable, "-c", "print('in the sandbox')"]

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "wrap", wrap)
    out = REGISTRY["monitor"].call(_mon("make build", timeout=15),
                                   _ctx(shell=True, sandbox_on=True))
    assert "in the sandbox" in out
    assert seen == [(["/bin/sh", "-c", "make build"], str(Path.cwd()), False)]
    # requested, but no sandboxer: it says so
    monkeypatch.setattr(sandbox, "available", lambda: False)
    out = REGISTRY["monitor"].call(_mon(f'"{sys.executable}" -c "print(7)"', timeout=15),
                                   _ctx(shell=True, sandbox_on=True))
    assert out.startswith("(sandbox requested but no sandboxer") and "7" in out


# ---- 4. the agent loop -------------------------------------------------------


def _agent(client, tools, *, cfg: Config, cwd: Path, **kw) -> Agent:
    agent = Agent(client=client, config=cfg, tools=[REGISTRY[t] for t in tools], **kw)
    agent.cwd = str(cwd)
    return agent


def _write(tmp_path: Path) -> str:
    """Python that leaves tmp_path/ran.txt behind if it runs."""
    return f"open({str(tmp_path / 'ran.txt')!r}, 'w').write('x')"


def _proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "app.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    (proj / "app.log").write_text("", encoding="utf-8")
    return proj


def test_default_config_prompts_only_for_running_code(home, tmp_path, fake_llm):
    proj = _proj(tmp_path)
    asked: list = []

    def ask(name, args, category):
        asked.append(name)
        return False

    client = fake_llm([
        fake_llm.call(("python_symbols", {"path": "app.py"}),
                      ("monitor", {"target": str(proj / "app.log"), "kind": "file",
                                   "timeout": 1}),
                      ("check_file", {"path": "app.py"}),
                      ("run_python", {"code": _write(tmp_path)})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["python_symbols", "monitor", "check_file", "run_python"],
                   cfg=_cfg(), cwd=proj, permission_callback=ask)
    res = {r.name: r.output for r in _results(agent.chat("go"))}

    assert asked == ["run_python"]
    assert not is_permission_denial(res["check_file"])
    assert "def f(x)" in res["python_symbols"]
    assert "no new lines" in res["monitor"]
    assert is_permission_denial(res["run_python"])


def test_shell_off_means_no_shell_even_with_everything_approved(home, tmp_path, fake_llm):
    # AC: with tools.shell=false no tool spawns a shell, monitor included,
    # under `/auto on` (yolo) and with "shell" auto-approved.
    proj = _proj(tmp_path)
    client = fake_llm([fake_llm.call(("monitor", {"target": MARK, "kind": "command"})),
                       fake_llm.text("done")])
    agent = _agent(client, ["monitor"], cwd=proj,
                   cfg=_cfg(shell=False, auto_approve=[*LEGACY, "shell"]))
    agent.enable_auto_all()
    [res] = _results(agent.chat("go"))
    assert res.output.startswith("ERROR: kind='command'")
    assert not (Path.cwd() / "marker.txt").exists() and not (proj / "marker.txt").exists()


def test_monitor_command_asks_like_run_command(home, tmp_path, fake_llm):
    proj = _proj(tmp_path)
    asked: list = []

    def ask(name, args, category):
        asked.append((name, json.loads(args)["kind"]))
        return False

    client = fake_llm([fake_llm.call(("monitor", {"target": MARK, "kind": "command"})),
                       fake_llm.text("done")])
    agent = _agent(client, ["monitor", "run_command"], cfg=_cfg(shell=True), cwd=proj,
                   permission_callback=ask)
    [res] = _results(agent.chat("go"))
    assert asked == [("monitor", "command")]
    assert is_permission_denial(res.output) and not (Path.cwd() / "marker.txt").exists()


def test_allow_code_never_clears_the_guard_for_a_monitor_command(
    home, tmp_path, fake_llm, no_spawn
):
    # With "shell" auto-approved, a specific `allow code git*` rule used to
    # clear the destructive guard for monitor's `git reset --hard` (a rule
    # matched on monitor's toggle category), so it ran unasked; run_command
    # asks for the same line.
    proj = _proj(tmp_path)
    asked: list = []
    cfg = _cfg(shell=True, auto_approve=[*DEFAULT, "shell"])
    cfg.auto.rules = ["allow code git*"]
    client = fake_llm([fake_llm.call(("monitor", {"target": "git reset --hard",
                                                  "kind": "command"})),
                       fake_llm.text("done")])
    agent = _agent(client, ["monitor", "run_command"], cfg=cfg, cwd=proj,
                   permission_callback=lambda *a: asked.append(a[0]) or False)
    [res] = _results(agent.chat("go"))
    assert asked == ["monitor"]
    assert is_permission_denial(res.output) and not no_spawn


def test_unattended_reads_code_but_never_runs_it(home, tmp_path, fake_llm):
    proj = _proj(tmp_path)
    client = fake_llm([
        fake_llm.call(("python_symbols", {"path": "app.py"}),
                      ("run_python", {"code": _write(tmp_path)}),
                      ("monitor", {"target": MARK, "kind": "command"})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["python_symbols", "run_python", "monitor", "run_command"],
                   cwd=proj, cfg=_cfg(shell=True))
    res = {r.name: r.output for r in _results(agent.chat("go"))}
    assert "def f(x)" in res["python_symbols"]
    assert is_permission_denial(res["run_python"]) and is_permission_denial(res["monitor"])
    assert not (tmp_path / "ran.txt").exists() and not (Path.cwd() / "marker.txt").exists()


@pytest.mark.parametrize("deny_callback", [None, lambda *_: False])
def test_a_remote_trigger_gets_no_code_execution(home, tmp_path, fake_llm, deny_callback):
    # Notes from the unattended-default-deny review: a remote peer (federation,
    # A2A, a routine, a channel push) got run_python and monitor kind=command
    # through the default "code" auto-approval.
    proj = _proj(tmp_path)
    client = fake_llm([
        fake_llm.call(("run_python", {"code": _write(tmp_path)}),
                      ("monitor", {"target": MARK, "kind": "command"}),
                      ("python_symbols", {"path": "app.py"})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["run_python", "monitor", "python_symbols", "run_command"],
                   cwd=proj, cfg=_cfg(shell=True), permission_callback=lambda *_: True)
    with remote_unattended(agent, deny_callback):
        res = {r.name: r.output for r in _results(agent.chat("go"))}
    assert is_permission_denial(res["run_python"]) and is_permission_denial(res["monitor"])
    assert "def f(x)" in res["python_symbols"]
    assert not (tmp_path / "ran.txt").exists() and not (Path.cwd() / "marker.txt").exists()


def test_code_mode_monitor_command_asks_like_run_command(home, tmp_path, fake_llm):
    # Code mode offers run_command whatever [tools] shell says, so monitor's
    # command mode is there too, and asks like it (no refusal to relay).
    from evi.modes import mode_tools

    proj = _proj(tmp_path)
    asked: list = []
    cmd = f'"{sys.executable}" -c "print(45)"'
    client = fake_llm([fake_llm.call(("monitor", {"target": cmd, "kind": "command",
                                                  "timeout": 15})),
                       fake_llm.text("done")])
    agent = Agent(client=client, config=_cfg(shell=False), tools=mode_tools("code"),
                  permission_callback=lambda *a: asked.append(a[0]) or True)
    agent.cwd = str(proj)
    [res] = _results(agent.chat("go"))
    assert asked == ["monitor"]
    assert "45" in res.output and "exited 0" in res.output


def test_monitor_tailing_a_secret_is_confirmed(home, tmp_path, fake_llm):
    proj = _proj(tmp_path)
    (proj / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    asked: list = []
    client = fake_llm([
        fake_llm.call(("monitor", {"target": str(proj / ".env"), "kind": "file",
                                   "timeout": 1}),
                      ("monitor", {"target": str(proj / "app.log"), "kind": "file",
                                   "timeout": 1})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["monitor"], cfg=_cfg(), cwd=proj,
                   permission_callback=lambda name, args, cat: asked.append(
                       json.loads(args)["target"]) or False)
    first, second = _results(agent.chat("go"))
    assert asked == [str(proj / ".env")]
    assert is_permission_denial(first.output) and "no new lines" in second.output


def test_a_local_unattended_run_honours_shell_when_listed(home, tmp_path, fake_llm):
    # Nobody to ask, no remote trigger (the scheduler, `evi run`): "shell" in
    # the user's own list still lets run_command run, and no category runs
    # code under the default list.
    proj = _proj(tmp_path)
    client = fake_llm([
        fake_llm.call(("run_command", {"command": f'"{sys.executable}" -c "print(46)"'}),
                      ("run_python", {"code": _write(tmp_path)})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["run_command", "run_python"], cwd=proj,
                   cfg=_cfg(shell=True, auto_approve=[*DEFAULT, "shell"]))
    res = {r.name: r.output for r in _results(agent.chat("go"))}
    assert not is_permission_denial(res["run_command"]) and "46" in res["run_command"]
    assert is_permission_denial(res["run_python"]) and not (tmp_path / "ran.txt").exists()


@pytest.mark.parametrize("deny_callback", [None, lambda *_: False])
def test_a_remote_trigger_gets_no_code_execution_from_a_legacy_list(
    home, tmp_path, fake_llm, deny_callback
):
    proj = _proj(tmp_path)
    client = fake_llm([
        fake_llm.call(("run_python", {"code": _write(tmp_path)})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["run_python"], cwd=proj, cfg=_cfg(auto_approve=LEGACY))
    with remote_unattended(agent, deny_callback):
        [res] = _results(agent.chat("go"))
    assert is_permission_denial(res.output) and not (tmp_path / "ran.txt").exists()


def test_a_legacy_list_doesnt_approve_monitor_commands(home, tmp_path, fake_llm):
    proj = _proj(tmp_path)
    asked: list = []
    client = fake_llm([fake_llm.call(("monitor", {"target": MARK, "kind": "command"})),
                       fake_llm.text("done")])
    agent = _agent(client, ["monitor", "run_command"], cwd=proj,
                   cfg=_cfg(shell=True, auto_approve=LEGACY),
                   permission_callback=lambda *a: asked.append(a[2]) or False)
    [res] = _results(agent.chat("go"))
    assert asked == ["shell"]  # and the prompt shows the call's category
    assert is_permission_denial(res.output) and not (Path.cwd() / "marker.txt").exists()


def test_a_sandboxed_run_python_runs_unasked(home, tmp_path, fake_llm, monkeypatch):
    (home / "config.toml").write_text("[tools]\nsandbox = true\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox, "wrap", lambda argv, wd, allow_network=False: list(argv))
    proj = _proj(tmp_path)
    asked: list = []
    client = fake_llm([fake_llm.call(("run_python", {"code": "print(6 * 7)"})),
                       fake_llm.text("done")])
    agent = _agent(client, ["run_python"], cwd=proj, cfg=_cfg(sandbox_on=True),
                   permission_callback=lambda *a: asked.append(a) or False)
    [res] = _results(agent.chat("go"))
    assert not asked and "42" in res.output


# ---- 5. the migration: evi lint and evi doctor ------------------------------


def test_auto_approve_migration_keeps_the_users_choices():
    assert auto_approve_migration(LEGACY) == DEFAULT
    assert auto_approve_migration(["web", "code", "shell", "code"]) == ["web", "code_read", "shell"]
    assert auto_approve_migration(DEFAULT) is None
    assert auto_approve_migration(["fs", "code", "code_read"]) is None  # kept on purpose
    assert auto_approve_migration([]) is None


def test_configlint_flags_the_old_default(monkeypatch):
    from evi import configlint

    monkeypatch.setattr(configlint, "_mcp_server_names", lambda: [])
    [issue] = configlint.lint_auto({"auto_approve": [*LEGACY, "web"]})
    assert issue.resource == "config.toml [auto] auto_approve" and issue.level == "warn"
    assert ('auto_approve = ["fs", "code_read", "memory", "skills", "image", "web"]'
            in issue.message)
    for clean in ({"auto_approve": DEFAULT}, {"auto_approve": ["code", "code_read"]}, {}):
        assert configlint.lint_auto(clean) == []
    # rules may name the call categories
    assert configlint.lint_auto({"rules": ["allow code_read", "deny shell rm*"]}) == []


def test_configlint_reads_config_toml(home, monkeypatch):
    import evi.config as config_mod
    from evi import configlint

    for name in ("AGENTS_CONFIG_PATH", "COMMANDS_DIR", "HOOKS_CONFIG_PATH", "SKILL_DIR"):
        monkeypatch.setattr(config_mod, name, home / "missing" / name)
    monkeypatch.setattr(configlint, "_mcp_server_names", lambda: [])
    (home / "config.toml").write_text(
        '[auto]\nauto_approve = ["fs", "code", "memory", "skills", "image"]\n', encoding="utf-8"
    )
    issues = [i for i in configlint.lint() if i.resource.startswith("config.toml")]
    assert [i.resource for i in issues] == ["config.toml [auto] auto_approve"]


def test_doctor_offers_the_migrated_list(home, monkeypatch):
    from evi import doctor

    [check] = doctor._check_auto_approve()  # no config.toml: the default
    assert check.status == "ok" and "asks unless sandboxed" in check.detail
    assert not (home / "config.toml").exists()  # doctor never writes it
    (home / "config.toml").write_text(
        '[auto]\nauto_approve = ["fs", "code", "memory", "skills", "image", "web"]\n',
        encoding="utf-8",
    )
    [check] = doctor._check_auto_approve()
    assert check.status == "warn" and "run_python runs unasked" in check.detail
    assert check.fix == ('in config.toml [auto]: auto_approve = '
                         '["fs", "code_read", "memory", "skills", "image", "web"]',)
    (home / "config.toml").write_text(
        '[auto]\nauto_approve = ["fs", "code", "code_read"]\n', encoding="utf-8"
    )
    [check] = doctor._check_auto_approve()
    assert check.status == "ok" and "by choice" in check.detail and not check.fix
    for slow in ("_check_backend", "_check_hardware", "_check_binaries",
                 "_check_optional_deps", "_check_federation", "_check_web_access"):
        monkeypatch.setattr(doctor, slow, lambda: [])
    assert "code execution approval" in {c.name for c in doctor.run_checks()}
