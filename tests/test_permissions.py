"""Tests for the permission policy (Phase 66)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evi.config import AutoSettings, Config
from evi.llm.agent import Agent
from evi.permissions import decide
from evi.shellsplit import DIALECTS
from evi.tools.base import Tool


# --- pure policy --------------------------------------------------------


def test_modes():
    assert decide("yolo", [], [], "run_shell", "shell", "{}") == "allow"
    assert decide("plan", ["fs"], [], "write_file", "fs", "{}") == "deny"
    assert decide("accept_edits", [], [], "write_file", "fs", "{}") == "allow"
    assert decide("accept_edits", [], [], "run_shell", "shell", "{}") == "ask"


def test_auto_approve_category():
    assert decide("ask", ["fs"], [], "write_file", "fs", "{}") == "allow"
    assert decide("ask", [], [], "write_file", "fs", "{}") == "ask"


def test_rule_deny_with_arg_glob():
    rules = ["deny write_file *.env"]
    assert decide("ask", ["fs"], rules, "write_file", "fs",
                  '{"path": "/x/.env", "content": "y"}') == "deny"
    # a non-matching path still uses the category allow
    assert decide("ask", ["fs"], rules, "write_file", "fs",
                  '{"path": "/x/readme.md"}') == "allow"


def test_rule_allow_overrides_ask():
    assert decide("ask", [], ["allow web_search"], "web_search", "web", "{}") == "allow"


def test_rule_first_match_wins():
    rules = ["deny run_shell rm*", "allow run_shell *"]
    assert decide("ask", [], rules, "run_shell", "shell", '{"command": "rm -rf /"}') == "deny"
    assert decide("ask", [], rules, "run_shell", "shell", '{"command": "ls"}') == "allow"


def test_hard_deny_beats_everything():
    # hard_deny is evaluated before yolo AND before allow rules — unoverridable.
    assert decide("yolo", ["fs"], ["allow run_shell *"], "run_shell", "shell",
                  '{"command": "rm -rf /"}', hard_deny=["run_shell rm*"]) == "deny"
    # a leading "deny" in the entry is accepted too
    assert decide("ask", [], [], "write_file", "fs", '{"path": "/x/secrets"}',
                  hard_deny=["deny write_file *secrets*"]) == "deny"
    # non-matching hard_deny doesn't block
    assert decide("yolo", [], [], "read_file", "fs", "{}", hard_deny=["run_shell *"]) == "allow"


def test_protected_paths_force_ask():
    pp = [".env", "*.pem", ".gitconfig"]
    # accept_edits would allow a write, but a protected path forces a prompt
    assert decide("accept_edits", [], [], "write_file", "fs",
                  '{"path": "/proj/.env"}', protected_paths=pp) == "ask"
    assert decide("accept_edits", [], [], "write_file", "fs",
                  '{"path": "/proj/key.pem"}', protected_paths=pp) == "ask"
    # an auto-approved category is overridden too
    assert decide("ask", ["fs"], [], "write_file", "fs",
                  '{"path": "/home/u/.gitconfig"}', protected_paths=pp) == "ask"
    # a normal file under accept_edits is still allowed
    assert decide("accept_edits", [], [], "write_file", "fs",
                  '{"path": "/proj/main.py"}', protected_paths=pp) == "allow"
    # an EXPLICIT allow rule still wins (honour user intent over protection)
    assert decide("ask", ["fs"], ["allow write_file *.env"], "write_file", "fs",
                  '{"path": "/proj/.env"}', protected_paths=pp) == "allow"


def test_tool_glob():
    assert decide("ask", [], ["deny delegate_*"], "delegate_explore", "subagent", "{}") == "deny"


# --- agent integration --------------------------------------------------


def _agent(auto: AutoSettings) -> Agent:
    cfg = Config()
    cfg.auto = auto
    return Agent(client=object(), config=cfg, tools=[])


def _tool(name="run_shell", category="shell") -> Tool:
    return Tool(name=name, description="", parameters={"type": "object", "properties": {}},
                func=lambda: "x", category=category)


def test_agent_permission_decision():
    assert _agent(AutoSettings(mode="yolo"))._permission_decision(_tool(), "{}") == "allow"
    assert _agent(AutoSettings(mode="plan"))._permission_decision(_tool(), "{}") == "deny"
    a = _agent(AutoSettings(mode="ask", rules=["deny run_shell rm*"]))
    assert a._permission_decision(_tool(), '{"command": "rm x"}') == "deny"
    assert a._permission_decision(_tool(), '{"command": "ls"}') == "ask"


def test_agent_auto_all_overrides_plan():
    a = _agent(AutoSettings(mode="plan"))
    a.enable_auto_all()
    assert a._permission_decision(_tool(), "{}") == "allow"


# --- trusted dirs + domains (Phase 77) ----------------------------------


def test_trusted_dir(tmp_path):
    inside = tmp_path / "proj" / "src"
    inside.mkdir(parents=True)
    trusted = [str(tmp_path / "proj")]
    args_in = json.dumps({"path": str(inside / "f.py"), "content": "x"})
    args_out = json.dumps({"path": str(tmp_path / "other" / "f.py")})
    assert decide("ask", [], [], "write_file", "fs", args_in, trusted_dirs=trusted) == "allow"
    assert decide("ask", [], [], "write_file", "fs", args_out, trusted_dirs=trusted) == "ask"


def test_trusted_domain():
    trusted = ["docs.python.org"]
    fetch = json.dumps({"url": "https://docs.python.org/3/library/os.html"})
    other = json.dumps({"url": "https://evil.example.com/x"})
    assert decide("ask", [], [], "web_fetch", "web", fetch, trusted_domains=trusted) == "allow"
    assert decide("ask", [], [], "web_fetch", "web", other, trusted_domains=trusted) == "ask"
    # subdomain matches
    sub = json.dumps({"url": "https://api.docs.python.org/x"})
    assert decide("ask", [], [], "web_fetch", "web", sub, trusted_domains=trusted) == "allow"


def test_deny_rule_beats_trusted_dir(tmp_path):
    trusted = [str(tmp_path)]
    args = json.dumps({"path": str(tmp_path / "secret.env")})
    # explicit deny rule wins over the trusted dir
    assert decide("ask", [], ["deny write_file *.env"], "write_file", "fs", args,
                  trusted_dirs=trusted) == "deny"


def test_agent_uses_trusted_dirs(tmp_path):
    a = _agent(AutoSettings(mode="ask", trusted_dirs=[str(tmp_path)]))
    args = json.dumps({"path": str(tmp_path / "x.py"), "content": "y"})
    tool = _tool(name="write_file", category="fs")
    assert a._permission_decision(tool, args) == "allow"


# --- destructive-command guard integration ------------------------------

_DESTRUCTIVE = '{"command": "git push --force origin main"}'
_SAFE = '{"command": "ls -la"}'


def _agent_cb(auto: AutoSettings, cb) -> Agent:
    cfg = Config()
    cfg.auto = auto
    return Agent(client=object(), config=cfg, tools=[], permission_callback=cb)


def test_guard_headless_denies_even_under_yolo():
    # No permission UI (scheduler/web-without-prompt): a destructive shell
    # command is DENIED even in yolo, while a safe command is still allowed.
    a = _agent(AutoSettings(mode="yolo"))
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "deny"
    assert a._permission_decision(_tool(), _SAFE) == "allow"


def test_guard_prompts_when_ui_exists():
    seen = []
    a = _agent_cb(AutoSettings(mode="yolo"), lambda n, args, cat: seen.append(args) or False)
    # With a UI, destructive → "ask" (must confirm), never silent-allow.
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "ask"
    # _ask_permission routes it to the callback even though yolo/pre-approval
    # would otherwise short-circuit.
    assert a._ask_permission(_tool(), _DESTRUCTIVE) is False
    assert seen, "callback must be consulted for a destructive command"


def test_guard_overrides_auto_approve_and_auto_all():
    a = _agent(AutoSettings(mode="ask", auto_approve=["shell"]))
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "deny"
    a.enable_auto_all()  # `/auto on` still can't run it silently
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "deny"
    assert a._ask_permission(_tool(), _DESTRUCTIVE) is False


def test_guard_can_be_disabled():
    a = _agent(AutoSettings(mode="yolo", block_destructive=False))
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "allow"


def test_guard_allow_exemption():
    a = _agent(AutoSettings(mode="yolo", destructive_allow=["*--force-with-lease*"]))
    args = '{"command": "git push --force-with-lease origin main"}'
    assert a._permission_decision(_tool(), args) == "allow"


def test_guard_specific_allow_rule_clears_but_broad_does_not():
    specific = _agent(AutoSettings(mode="yolo", rules=["allow run_shell *--force*"]))
    assert specific._permission_decision(_tool(), _DESTRUCTIVE) == "allow"
    broad = _agent(AutoSettings(mode="yolo", rules=["allow run_shell"]))
    assert broad._permission_decision(_tool(), _DESTRUCTIVE) == "deny"


def test_guard_only_applies_to_shell_category():
    a = _agent(AutoSettings(mode="yolo"))
    fs = _tool(name="write_file", category="fs")
    # a non-shell tool carrying scary-looking text is unaffected
    assert a._permission_decision(fs, '{"path": "git push --force"}') == "allow"


def test_guard_disable_rules():
    a = _agent(AutoSettings(mode="yolo", destructive_disable_rules=["git-force-push"]))
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "allow"


def test_guard_covers_monitor_command_tool():
    # `monitor` (category "code") runs its `target` as a shell command; the guard
    # must cover it too, not only category "shell".
    a = _agent(AutoSettings(mode="yolo"))
    mon = _tool(name="monitor", category="code")
    assert a._permission_decision(mon, '{"target": "rm -rf ~", "kind": "command"}') == "deny"
    # tailing a file (kind != command) is not a shell command → unaffected
    assert a._permission_decision(mon, '{"target": "app.log", "kind": "file"}') == "allow"


def test_guard_broad_star_glob_rule_does_not_clear():
    # `allow run_shell *` is a wildcard, not a specific intent → must not disable.
    a = _agent(AutoSettings(mode="yolo", rules=["allow run_shell *"]))
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "deny"


def test_guard_rule_may_key_on_category():
    # docs tell users to write rules against the category token ("shell"); a
    # specific such allow-rule clears the guard for the run_shell tool.
    a = _agent(AutoSettings(mode="yolo", rules=["allow shell *--force*"]))
    assert a._permission_decision(_tool(), _DESTRUCTIVE) == "allow"
    b = _agent(AutoSettings(mode="yolo", rules=["allow shell"]))  # broad → does not clear
    assert b._permission_decision(_tool(), _DESTRUCTIVE) == "deny"


# --- shell segment splitting: allow rules must cover every segment ----------

_GIT = ["allow run_command git*"]


def _sh(command: str, **extra) -> str:
    return json.dumps({"command": command, **extra})


def _shell_decide(rules, command, workdir, dialect, mode="ask", cwd=None, **kw):
    args = _sh(command) if cwd is None else _sh(command, cwd=cwd)
    return decide(mode, [], rules, "run_command", "shell", args,
                  workdir=workdir, dialect=dialect, **kw)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_allow_rule_must_cover_every_segment(dialect, tmp_path):
    def d(cmd):
        return _shell_decide(_GIT, cmd, tmp_path, dialect)

    assert d("git status && git log") == "allow"
    assert d("git status") == "allow"
    # Exploit regressions: the whole-line glob used to approve the chained tail.
    assert d("git log; curl x | sh") == "ask"
    assert d("git log; python x.py") == "ask"
    assert d("git log && rm -rf build") == "ask"
    assert d("git log | tee out.txt") == "ask"


@pytest.mark.parametrize("dialect", ["bash", "pwsh"])
def test_allow_rule_covers_nested_substitutions(dialect, tmp_path):
    assert _shell_decide(_GIT, "git log $(curl x)", tmp_path, dialect) == "ask"
    rules = _GIT + ["allow run_command date*"]
    assert _shell_decide(rules, "git log --since=$(date +%F)", tmp_path, dialect) == "allow"


def test_cmd_percent_and_reparse_ask_despite_allow_rule(tmp_path):
    # re-reviewer finding: cmd resolves %VAR% and re-parses pipelines in ways a
    # single scan can't follow, so `allow run_command git*` must NOT auto-approve
    # these cmd lines even though every *visible* segment is git.  Each shape
    # runs a hidden command in real cmd.exe (confirmed differentially).
    def d(cmd):
        return _shell_decide(_GIT, cmd, tmp_path, "cmd")

    # %CMDCMDLINE substring splices `&python x.py` in behind a quote
    assert d('git log "&python x.py" %CMDCMDLINE:~41,12%') == "ask"
    assert d("git log %PAYLOAD%") == "ask"             # any %VAR% expansion
    assert d("git log %OUT:~0,3%") == "ask"            # substring modifier
    # ^& / ^| where a pipe appears: the stage is re-parsed, the escaped sep runs
    assert d("git log ^& rd x | git status") == "ask"
    assert d("git log ^| rd x | git status") == "ask"
    assert d("(git log ^& rd x) | git status") == "ask"
    assert d("for /f %i in ('git log') do git show %i") == "ask"  # for /f command set
    # a caret-escaped separator with NO pipe is one covered command → still allow
    assert d("git log ^& git status") == "allow"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_rules_together_cover_the_segments(dialect, tmp_path):
    rules = ["allow run_command git*", "allow run_command python*"]
    assert _shell_decide(rules, "git log; python x.py", tmp_path, dialect) == "allow"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_unparsable_line_asks_despite_allow_rule(dialect, tmp_path):
    rules = ["allow run_command bash*", "allow run_command git*", "allow run_command cmd*",
             "allow run_command powershell*"]
    assert _shell_decide(rules, "bash script.sh", tmp_path, dialect) == "allow"
    assert _shell_decide(rules, 'bash -c "echo hi"', tmp_path, dialect) == "ask"
    assert _shell_decide(rules, "bash -lc 'git log'", tmp_path, dialect) == "ask"
    assert _shell_decide(rules, 'git log "unbalanced', tmp_path, dialect) == "ask"
    assert _shell_decide(rules, "cmd /c git log", tmp_path, dialect) == "ask"
    assert _shell_decide(rules, "powershell -Command git log", tmp_path, dialect) == "ask"


@pytest.mark.parametrize("dialect", ["bash", "pwsh"])
def test_unbalanced_single_quote_asks(dialect, tmp_path):
    rules = ["allow run_command echo*"]
    assert _shell_decide(rules, "echo 'balanced'", tmp_path, dialect) == "allow"
    assert _shell_decide(rules, "echo 'unbalanced", tmp_path, dialect) == "ask"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_broad_allow_still_covers_any_line(dialect, tmp_path):
    # `allow run_command` / `allow run_command *` is the user allowing every
    # command: unparsable lines and outside redirects included (as before).
    for rules in (["allow run_command"], ["allow run_command *"]):
        assert _shell_decide(rules, 'bash -c "x"', tmp_path, dialect) == "allow"
        assert _shell_decide(rules, "git log; rm -rf build", tmp_path, dialect) == "allow"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_redirect_outside_workdir_asks(dialect, tmp_path):
    work = tmp_path / "proj"
    work.mkdir()
    outside = tmp_path / "elsewhere.txt"
    var = {"bash": "$OUT", "pwsh": "$env:OUT", "cmd": "%OUT%"}[dialect]
    null = {"bash": "/dev/null", "pwsh": "$null", "cmd": "nul"}[dialect]

    def d(cmd, **kw):
        return _shell_decide(_GIT, cmd, work, dialect, **kw)

    assert d("git log > log.txt") == "allow"
    assert d("git log >> sub/log.txt 2>&1") == "allow"
    assert d(f"git log 2> {null}") == "allow"
    assert d(f'git log > "{outside}"') == "ask"
    assert d("git log > ../elsewhere.txt") == "ask"
    assert d("git log > ~/x.txt") == "ask"
    assert d(f"git log > {var}") == "ask"  # unknown path
    # relative targets resolve against the call's cwd
    assert d("git log > log.txt", cwd=str(tmp_path)) == "ask"
    assert d("git log > log.txt", cwd="sub") == "allow"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_redirect_after_cd_asks(dialect, tmp_path):
    rules = _GIT + ["allow run_command cd*"]
    assert _shell_decide(rules, "cd sub && git log", tmp_path, dialect) == "allow"
    assert _shell_decide(rules, "cd .. && git log > log.txt", tmp_path, dialect) == "ask"


def test_redirect_symlink_out_of_workdir_asks(tmp_path):
    work = tmp_path / "proj"
    work.mkdir()
    (tmp_path / "target").mkdir()
    try:
        (work / "link").symlink_to(tmp_path / "target", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert _shell_decide(_GIT, "git log > link/x.txt", work, "bash") == "ask"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_deny_rule_matches_any_segment(dialect, tmp_path):
    rules = ["deny run_command rm*", "allow run_command *"]
    assert _shell_decide(rules, "ls && rm -rf build", tmp_path, dialect) == "deny"
    assert _shell_decide(rules, "ls && echo hi", tmp_path, dialect) == "allow"
    # ...even when the line is unparsable after the denied segment
    assert _shell_decide(rules, 'rm -rf build && bash -c "x"', tmp_path, dialect) == "deny"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_hard_deny_matches_any_segment(dialect, tmp_path):
    hd = ["run_command rm -rf*"]
    assert _shell_decide([], "ls && rm -rf /tmp/x", tmp_path, dialect,
                         mode="yolo", hard_deny=hd) == "deny"
    assert _shell_decide([], "ls && echo rm -rf", tmp_path, dialect,
                         mode="yolo", hard_deny=hd) == "allow"


def test_monitor_command_is_split_too(tmp_path):
    rules = ["allow monitor git*"]

    def d(args):
        return decide("ask", [], rules, "monitor", "code", json.dumps(args),
                      workdir=tmp_path, dialect="bash")

    assert d({"target": "git log", "kind": "command"}) == "allow"
    assert d({"target": "git log; rm x", "kind": "command"}) == "ask"
    # tailing a file is not a shell command: the glob matches the path as before
    assert d({"target": "git.log", "kind": "file"}) == "allow"


def test_non_shell_tools_keep_whole_argument_matching():
    assert decide("ask", [], ["allow write_file *; x"], "write_file", "fs",
                  '{"path": "a; x"}') == "allow"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_allow_rule_on_cwd_argument_no_longer_approves_shell_lines(dialect, tmp_path):
    # Intended change (review round 1): for a shell call an allow rule's
    # arg-glob must cover the command segments. A glob that only matches the
    # `cwd` argument used to approve ANY line run there, chains included.
    rules = [f"allow run_command {tmp_path}*"]
    for cmd in ("npm test", "git log; rm -rf build"):
        assert _shell_decide(rules, cmd, tmp_path, dialect, cwd=str(tmp_path)) == "ask"
    # ...while a deny rule still matches any string argument, cwd included.
    deny = [f"deny run_command {tmp_path}*", "allow run_command *"]
    assert _shell_decide(deny, "npm test", tmp_path, dialect, cwd=str(tmp_path)) == "deny"


def _monitor_args(target: str) -> str:
    return json.dumps({"target": target, "kind": "command"})


@pytest.mark.parametrize("dialect", DIALECTS)
def test_monitor_redirect_resolves_against_the_process_cwd(dialect, tmp_path, monkeypatch):
    # monitor's Popen passes no cwd=, so `> log.txt` lands in the eVi process
    # cwd, not the session working folder: judge the redirect where it lands.
    work = tmp_path / "proj"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    def d():
        return decide("ask", [], ["allow monitor echo*"], "monitor", "code",
                      _monitor_args("echo hi > log.txt"), workdir=work, dialect=dialect)

    monkeypatch.chdir(other)
    assert d() == "ask"
    monkeypatch.chdir(work)
    assert d() == "allow"


def test_guard_monitor_redirect_resolves_against_the_process_cwd(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    a = _agent(AutoSettings(mode="yolo", rules=["allow monitor git*"]))
    a.cwd = str(work)
    mon = _tool(name="monitor", category="code")
    args = _monitor_args("git push --force origin main > push.log")
    monkeypatch.chdir(other)
    assert a._permission_decision(mon, args) == "deny"  # headless: guard holds
    monkeypatch.chdir(work)
    assert a._permission_decision(mon, args) == "allow"


def test_guard_and_rules_share_one_shell_tool_map(tmp_path, monkeypatch):
    # A new shell-running tool registered in permissions.SHELL_EXEC_TOOLS is
    # guarded AND segment-matched; neither path can fall back to treating its
    # command line as an opaque string.
    import evi.permissions as permissions

    monkeypatch.setitem(permissions.SHELL_EXEC_TOOLS, "run_script", "line")
    tool = _tool(name="run_script", category="code")
    a = _agent(AutoSettings(mode="yolo"))
    a.cwd = str(tmp_path)
    assert a._permission_decision(tool, json.dumps({"line": "rm -rf ~"})) == "deny"
    assert a._permission_decision(tool, json.dumps({"line": "ls"})) == "allow"
    b = _agent(AutoSettings(mode="ask", rules=["allow run_script git*"], auto_approve=[]))
    b.cwd = str(tmp_path)
    assert b._permission_decision(tool, json.dumps({"line": "git log"})) == "allow"
    assert b._permission_decision(tool, json.dumps({"line": "git log; python x.py"})) == "ask"


# --- the destructive guard: an allow rule clears it only segment by segment ---


def test_guard_allow_rule_must_cover_every_segment(tmp_path):
    # Exploit regression (verifier finding): `allow run_command git*` cleared
    # the guard for `git log; rm -rf ~`, and decide() then auto-approved it.
    run = _tool(name="run_command")
    a = _agent(AutoSettings(mode="ask", rules=list(_GIT)))
    a.cwd = str(tmp_path)
    assert a._permission_decision(run, _sh("git log; rm -rf ~")) == "deny"  # headless
    assert a._ask_permission(run, _sh("git log; rm -rf ~")) is False
    assert a._permission_decision(run, _sh("git status && git log")) == "allow"
    ui = _agent_cb(AutoSettings(mode="ask", rules=list(_GIT)), lambda n, args, cat: False)
    ui.cwd = str(tmp_path)
    assert ui._permission_decision(run, _sh("git log; rm -rf ~")) == "ask"


def test_guard_category_rule_must_cover_every_segment(tmp_path):
    a = _agent(AutoSettings(mode="yolo", rules=["allow shell git*"]))
    a.cwd = str(tmp_path)
    run = _tool(name="run_command")
    assert a._permission_decision(run, _sh("git log; rm -rf ~")) == "deny"
    assert a._permission_decision(run, _sh("git push --force origin main")) == "allow"


def test_guard_not_cleared_for_unparsable_or_outside_redirect(tmp_path):
    work = tmp_path / "proj"
    work.mkdir()
    run = _tool(name="run_command")
    a = _agent(AutoSettings(mode="ask", rules=["allow run_command bash*",
                                                "allow run_command git*"]))
    a.cwd = str(work)
    assert a._permission_decision(run, _sh('bash -c "rm -rf ~"')) == "deny"
    assert a._permission_decision(run, _sh('git push --force origin main "oops')) == "deny"
    outside = tmp_path / "push.log"
    assert a._permission_decision(
        run, _sh(f'git push --force origin main > "{outside}"')) == "deny"
    assert a._permission_decision(run, _sh("git push --force origin main > push.log")) == "allow"


def test_guard_deny_rule_matches_a_segment(tmp_path):
    a = _agent_cb(AutoSettings(mode="yolo", rules=["deny run_command rm*"]),
                  lambda n, args, cat: True)
    a.cwd = str(tmp_path)
    assert a._permission_decision(_tool(name="run_command"), _sh("ls; rm -rf ~")) == "deny"


def test_guard_not_cleared_for_cmd_percent_or_reparse(tmp_path, monkeypatch):
    # re-reviewer finding, guard path: on Windows the guard splits with the cmd
    # dialect (shellsplit.default_dialect()); force it so this runs everywhere.
    # `allow run_command git*` must NOT clear the destructive guard for a cmd
    # line that hides a destructive command behind %VAR% or a caret re-parse,
    # and destructive_allow globs can't exempt one either.
    import evi.shellsplit as ss
    monkeypatch.setattr(ss, "default_dialect", lambda: "cmd")
    run = _tool(name="run_command")

    a = _agent(AutoSettings(mode="ask", rules=list(_GIT)))  # headless (no UI)
    a.cwd = str(tmp_path)
    pct = 'git log "&rd /s /q C:\\\\x" %CMDCMDLINE:~41,12%'
    assert a._permission_decision(run, _sh(pct)) == "deny"
    reparse = "git log ^& rd /s /q C:\\x | git status"
    assert a._permission_decision(run, _sh(reparse)) == "deny"

    b = _agent(AutoSettings(mode="yolo",
                            destructive_allow=["git push --force-with-lease*"]))
    b.cwd = str(tmp_path)
    exempt = 'git push --force-with-lease "&rd /s /q C:\\\\x" %CMDCMDLINE:~61,12%'
    assert b._permission_decision(run, _sh(exempt)) == "deny"


# --- the same through Agent.chat (scripted fake LLM stream) ------------------


def _tool_call_turn(call_id: str, name: str, args: str) -> list:
    fn = SimpleNamespace(name=name, arguments=args)
    delta = SimpleNamespace(content=None, tool_calls=[
        SimpleNamespace(index=0, id=call_id, function=fn)])
    return [SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="tool_calls")])]


def _text_turn(text: str) -> list:
    delta = SimpleNamespace(content=text, tool_calls=None)
    return [SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")])]


def _scripted_client(turns: list[list]) -> SimpleNamespace:
    it = iter(turns)
    completions = SimpleNamespace(create=lambda **_: iter(next(it)))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


@pytest.mark.parametrize("command,prompted,ran", [
    ("git status && git log", False, True),  # every segment covered: no prompt
    ("git log; python x.py", True, False),  # uncovered tail: ask (user says no)
    ("git log; rm -rf ~", True, False),  # destructive tail: the guard holds
])
def test_chat_allow_rule_covers_every_segment(tmp_path, command, prompted, ran):
    ran_cmds: list[str] = []
    prompts: list[str] = []
    run_command = Tool(
        name="run_command", description="",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        func=lambda command: ran_cmds.append(command) or "exit 0", category="shell",
    )
    cfg = Config()
    cfg.auto = AutoSettings(mode="ask", rules=list(_GIT))
    agent = Agent(
        client=_scripted_client([_tool_call_turn("c1", "run_command", _sh(command)),
                                 _text_turn("done")]),
        config=cfg, tools=[run_command],
        permission_callback=lambda name, args, cat: prompts.append(args) or False,
    )
    agent.cwd = str(tmp_path)
    list(agent.chat("go"))
    assert bool(prompts) is prompted
    assert (ran_cmds == [command]) is ran
