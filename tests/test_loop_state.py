"""Session state inside the agent loop.

Tool bodies run on pool threads, which start with an empty context, so state
kept in ContextVars never reached them: the session folder (/cd, --cwd, the web
folder chip) was ignored, a skill's allowed/disallowed-tools were never
enforced, and subagents ran without the parent's hooks. Every consumer also
broke on Done before the `stop` hook could fire.

These tests drive the real consumers (Agent.chat, the REPL, headless, the web
worker, subagents) against the scripted `fake_llm` client from conftest.py.
They also pin how hooks get their input: whole whenever one env variable can
hold it, never cut under a guard, in the session folder, and failing closed
when a guard can't run.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import evi.tools.fs  # noqa: F401  registers write_file & co.
import evi.tools.shell  # noqa: F401  registers run_command
import evi.tools.skills as skills_tools
import evi.tools.subagent  # noqa: F401  registers delegate_*
from evi import hooks as hooks_mod
from evi import workdir
from evi.config import Config
from evi.hooks import Hook, HookRegistry
from evi.llm.agent import Agent, Done, ToolResult
from evi.skills import SkillStore
from evi.tools.base import REGISTRY


# ---- helpers -------------------------------------------------------------


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config and hooks file under tmp_path (never ~/.evi), no safe
    mode, and a process cwd that no tool call should ever land in."""
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    (evi_home / "config.toml").write_text('[llm]\nmodel = "fake"\n', encoding="utf-8")
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    elsewhere = tmp_path / "process-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return evi_home


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _marker_hook(event: str, marker: Path) -> Hook:
    """A hook that appends "<event> <payload>" to `marker` each time it runs."""
    code = (
        "import os; "
        f"open({str(marker)!r}, 'a', encoding='utf-8').write("
        "os.environ['EVI_HOOK_EVENT'] + ' ' + os.environ['EVI_HOOK_ARGS_JSON'] + '\\n')"
    )
    return Hook(name=f"mark-{event}", event=event, match="*", command=_py(code))


def _veto_writes(arg_match: tuple[tuple[str, str], ...] = ()) -> Hook:
    return Hook(
        name="no-writes", event="before_tool_call", match="write_file",
        command=_py("import sys; sys.stderr.write('writes are off here'); sys.exit(1)"),
        veto_on_nonzero=True, arg_match=arg_match,
    )


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _agent(client, tools: list[str], *, hooks=None, cwd: str = "") -> Agent:
    # No permission UI here: the unattended "allow" policy lets shell and
    # subagent calls run, so these tests exercise session state, not the gate
    # (tests/test_unattended_policy.py covers that).
    agent = Agent(
        client=client, config=Config(), tools=[REGISTRY[n] for n in tools], hooks=hooks,
        unattended_policy="allow",
    )
    agent.cwd = cwd
    return agent


def _writer_profile(home: Path, monkeypatch) -> None:
    """A user subagent profile `writer` with the fs category (write_file
    included): the built-in explore profile is read-only."""
    import evi.llm.subagent as subagent_mod

    f = home / "agents.toml"
    f.write_text(
        '[[agent]]\nname = "writer"\nsystem_prompt = "sub"\ntools = ["fs"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(subagent_mod, "_user_profiles_path", lambda: f)


def _results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def _offered(request: dict) -> set[str]:
    return {t["function"]["name"] for t in (request.get("tools") or [])}


def _same_dir(a: str | Path, b: str | Path) -> bool:
    return os.path.samefile(a, b)


# ---- the session folder reaches tool threads -----------------------------


def test_chat_tools_run_in_agent_cwd(home, tmp_path, fake_llm):
    proj = tmp_path / "proj"
    proj.mkdir()
    pwd = "cd" if os.name == "nt" else "pwd"
    client = fake_llm([
        fake_llm.call(
            ("write_file", {"path": "x.txt", "content": "hi"}),
            ("run_command", {"command": pwd}),
        ),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["write_file", "run_command"], cwd=str(proj))

    out = {r.name: r.output for r in _results(agent.chat("go"))}

    assert (proj / "x.txt").read_text(encoding="utf-8") == "hi"
    assert not (Path.cwd() / "x.txt").exists()
    status, reported = out["run_command"].splitlines()[:2]
    assert status == "exit 0"
    assert _same_dir(reported.strip(), proj)


def test_agent_cwd_change_between_turns_moves_tools(home, tmp_path, fake_llm):
    # What /cd and the folder chip do: change agent.cwd on a live session.
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "a.txt", "content": "1"})),
        fake_llm.text("ok"),
        fake_llm.call(("write_file", {"path": "b.txt", "content": "2"})),
        fake_llm.text("ok"),
    ])
    agent = _agent(client, ["write_file"], cwd=str(first))
    list(agent.chat("one"))
    agent.cwd = str(second)
    list(agent.chat("two"))
    assert (first / "a.txt").exists() and not (first / "b.txt").exists()
    assert (second / "b.txt").exists()


def test_repl_cd_moves_tools_and_fires_stop_once_per_turn(
    home, tmp_path, fake_llm, monkeypatch
):
    import evi.apps.cli.main as cli_main
    import evi.repl_input as repl_input
    from evi.commands import CommandStore

    proj = tmp_path / "proj"
    proj.mkdir()
    marker = tmp_path / "stop.log"
    typed = iter([f"/cd {proj}", "write it", "thanks"])

    class _Input:
        def __init__(self, agent):
            pass

        def read(self, prompt):
            try:
                return next(typed)
            except StopIteration:
                raise EOFError

    monkeypatch.setattr(repl_input, "ReplInput", _Input)
    monkeypatch.setattr(
        cli_main, "CommandStore", lambda *a, **k: CommandStore(root=tmp_path / "commands")
    )
    monkeypatch.setattr(cli_main.atexit, "register", lambda *a, **k: None)
    monkeypatch.setenv("EVI_INTERACTIVE", "0")  # restored after; the REPL sets it
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "from the repl"})),
        fake_llm.text("wrote it"),
        fake_llm.text("you're welcome"),
    ])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([_marker_hook("stop", marker)]))

    cli_main._run_repl(agent)

    assert (proj / "x.txt").read_text(encoding="utf-8") == "from the repl"
    assert not (Path.cwd() / "x.txt").exists()
    assert _lines(marker) == ["stop stop", "stop stop"]  # two turns, one stop each


def test_fan_out_drain_team_and_parallel_subagents_carry_the_folder(
    home, tmp_path, fake_llm, monkeypatch
):
    import evi.llm.subagent as subagent_mod
    from evi.teams import TeamStore, drain_team
    from evi.workflows import fan_out

    proj = tmp_path / "proj"
    proj.mkdir()
    reg = HookRegistry([])
    cwd_token = workdir.set_cwd(proj)
    hooks_token = hooks_mod.bind(reg)
    try:
        seen = fan_out(lambda _: (workdir.get_cwd(), hooks_mod.active()), [1, 2, 3, 4])
        assert seen == [(proj, reg)] * 4

        store = TeamStore(tmp_path / "team.json")
        store.add("a")
        store.add("b")
        final = drain_team(store, lambda task: str(workdir.get_cwd()), max_workers=2)
        assert {t.result for t in final} == {str(proj)}

        clients = [
            fake_llm([fake_llm.call(("write_file", {"path": f"{n}.txt", "content": n})),
                      fake_llm.text("ok")])
            for n in ("one", "two")
        ]
        lock = threading.Lock()

        def next_client(*_):
            with lock:
                return clients.pop()

        monkeypatch.setattr(subagent_mod, "make_client", next_client)
        monkeypatch.setattr(subagent_mod, "Config", SimpleNamespace(load=Config))
        subagent_mod.run_subagents_parallel(
            ["first", "second"], system_prompt="sub", tool_categories=("fs",)
        )
    finally:
        hooks_mod.unbind(hooks_token)
        workdir.reset(cwd_token)
    assert (proj / "one.txt").exists() and (proj / "two.txt").exists()
    assert not (Path.cwd() / "one.txt").exists()


def test_agents_built_for_a_run_after_cd_work_in_the_session_folder(
    home, tmp_path, fake_llm
):
    # /cd sets the folder on the REPL thread. /ultra (and /effort ultracode)
    # builds a fresh agent per stage, never setting its cwd, and runs the
    # solvers inside fan_out; a workflow's steps do the same.
    from evi import ultracode as uc
    from evi.workflows import fan_out

    proj = tmp_path / "proj"
    proj.mkdir()
    early = _agent(fake_llm([fake_llm.call(("write_file", {"path": "early.txt",
                                                            "content": "x"})),
                             fake_llm.text("ok")]), ["write_file"])

    def factory(sp, model=None, backend=None, name="stage.txt"):
        client = fake_llm([
            fake_llm.call(("write_file", {"path": name, "content": "x"})),
            fake_llm.text("ok"),
        ])
        return Agent(client=client, config=Config(), tools=[REGISTRY["write_file"]],
                     system_prompt=sp)

    # Ultracode stages only read (a solver keeps just read-only fs tools), so
    # they show their folder by reading a file that is only in the session's.
    (proj / "stage.txt").write_text("in the session folder\n", encoding="utf-8")
    readers: list = []

    def reader(sp, model=None, backend=None):
        client = fake_llm([
            fake_llm.call(("read_file", {"path": "stage.txt"})),
            fake_llm.text("ok"),
        ])
        readers.append(client)
        return Agent(client=client, config=Config(), tools=[REGISTRY["read_file"]],
                     system_prompt=sp)

    token = workdir.set_cwd(str(proj))
    try:
        assert factory("sp").cwd == str(proj)
        uc.run_ultracode(
            "edit it", run_one=uc.make_runner(reader),
            cfg=uc.UltraConfig(breadth=1, rounds=0, mode=""),
        )
        fan_out(lambda n: list(factory("step", name=n).chat("go")), ["a.txt", "b.txt"])
        # An agent's folder is its own, fixed when it is built: one set on the
        # thread afterwards (by /cd or another session's turn) doesn't move it.
        list(early.chat("go"))
    finally:
        workdir.reset(token)
    assert any("in the session folder" in json.dumps(c.requests[-1]["messages"])
               for c in readers)
    assert not (Path.cwd() / "stage.txt").exists()
    assert (proj / "a.txt").exists() and (proj / "b.txt").exists()
    assert (Path.cwd() / "early.txt").exists() and not (proj / "early.txt").exists()


# ---- skill tool scope is enforced in the loop ---------------------------


@pytest.fixture
def skills(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()

    def make(name: str, frontmatter: str = "") -> None:
        d = root / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: t\n{frontmatter}---\nfollow me\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(skills_tools, "_store", SkillStore(root=root))
    return make


def test_disallowed_tool_refused_after_invoke_skill(home, tmp_path, skills, fake_llm):
    skills("careful", "disallowed-tools: write_file\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    client = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "careful"})),
        fake_llm.call(("write_file", {"path": "x.txt", "content": "nope"})),
        fake_llm.text("could not write"),
    ])
    agent = _agent(client, ["invoke_skill", "write_file"], cwd=str(proj))

    out = _results(agent.chat("go"))

    assert "[tools disallowed: write_file]" in out[0].output
    assert "not allowed by the active skill's scope" in out[1].output
    assert not (proj / "x.txt").exists()
    # Enforced at dispatch: the tool list the model sees never changes mid-turn
    # (prefix stability for local backends).
    offered = [_offered(r) for r in client.requests]
    assert offered == [{"invoke_skill", "write_file"}] * 3


def test_allowed_tools_scope_and_reset_next_turn(home, tmp_path, skills, fake_llm):
    skills("reader", "allowed-tools: read_file, invoke_skill\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    client = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "reader"})),
        fake_llm.call(("write_file", {"path": "x.txt", "content": "nope"})),
        fake_llm.text("read-only, sorry"),
        # next turn: the scope is gone
        fake_llm.call(("write_file", {"path": "y.txt", "content": "yes"})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["invoke_skill", "read_file", "write_file"], cwd=str(proj))

    first = _results(agent.chat("look around"))
    assert "[tools scoped to: invoke_skill, read_file]" in first[0].output
    assert "not allowed by the active skill's scope" in first[1].output
    assert not (proj / "x.txt").exists()

    list(agent.chat("now write"))
    assert agent.active_skill_scope is None
    assert (proj / "y.txt").read_text(encoding="utf-8") == "yes"


def test_second_skill_cannot_widen_scope(home, tmp_path, skills, fake_llm):
    skills("careful", "disallowed-tools: write_file\n")
    skills("writer", "allowed-tools: write_file, invoke_skill\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    client = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "careful"})),
        fake_llm.call(("invoke_skill", {"name": "writer"})),
        fake_llm.call(("write_file", {"path": "x.txt", "content": "nope"})),
        fake_llm.text("stuck"),
    ])
    agent = _agent(client, ["invoke_skill", "write_file"], cwd=str(proj))
    out = _results(agent.chat("go"))
    assert "not allowed by the active skill's scope" in out[2].output
    assert not (proj / "x.txt").exists()


def test_reroll_clears_skill_scope(home, tmp_path, skills, fake_llm):
    skills("careful", "disallowed-tools: write_file\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    client = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "careful"})),
        fake_llm.text("ok"),
        # the re-roll answers the same prompt afresh, without the skill
        fake_llm.call(("write_file", {"path": "x.txt", "content": "again"})),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["invoke_skill", "write_file"], cwd=str(proj))
    list(agent.chat("go"))
    assert agent.active_skill_scope is not None
    agent.rewind_to_last_user()
    list(agent.continue_chat())
    assert (proj / "x.txt").read_text(encoding="utf-8") == "again"


def test_same_batch_call_is_scoped_by_the_skill(home, tmp_path, skills, fake_llm):
    # invoke_skill and write_file in ONE assistant message run side by side;
    # the write must still be refused, and the user never asked about it.
    skills("careful", "disallowed-tools: write_file\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    client = fake_llm([
        fake_llm.call(
            ("invoke_skill", {"name": "careful"}),
            ("write_file", {"path": "x.txt", "content": "nope"}),
        ),
        fake_llm.text("could not write"),
    ])
    asked: list[str] = []
    agent = _agent(client, ["invoke_skill", "write_file"], cwd=str(proj))
    agent.auto_approve_categories = set()  # every call asks
    agent.permission_callback = lambda name, args, cat: asked.append(name) or True

    out = _results(agent.chat("go"))

    assert "[tools disallowed: write_file]" in out[0].output
    assert "not allowed by the active skill's scope" in out[1].output
    assert not (proj / "x.txt").exists()
    assert asked == ["invoke_skill"]  # a call the scope refuses never prompts


def test_same_batch_unscoped_skill_leaves_calls_alone(home, tmp_path, skills, fake_llm):
    skills("plain")
    proj = tmp_path / "proj"
    proj.mkdir()
    client = fake_llm([
        fake_llm.call(
            ("invoke_skill", {"name": "plain"}),
            ("write_file", {"path": "x.txt", "content": "yes"}),
        ),
        fake_llm.text("done"),
    ])
    agent = _agent(client, ["invoke_skill", "write_file"], cwd=str(proj))
    list(agent.chat("go"))
    assert (proj / "x.txt").read_text(encoding="utf-8") == "yes"
    assert agent.active_skill_scope is None


def test_delegate_runs_under_the_skill_scope(home, tmp_path, skills, fake_llm, monkeypatch):
    # A subagent can't do what the skill refused the agent that spawned it.
    import evi.llm.subagent as subagent_mod

    skills("careful", "disallowed-tools: write_file\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    sub_client = fake_llm([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "sneaky"})),
        fake_llm.text("tried"),
    ])
    monkeypatch.setattr(subagent_mod, "make_client", lambda *_: sub_client)
    monkeypatch.setattr(subagent_mod, "Config", SimpleNamespace(load=Config))
    _writer_profile(home, monkeypatch)
    parent = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "careful"})),
        fake_llm.call(("delegate", {"profile": "writer", "task": "write x.txt"})),
        fake_llm.text("delegated"),
    ])
    agent = _agent(parent, ["invoke_skill", "write_file", "delegate"], cwd=str(proj))

    out = _results(agent.chat("go"))

    assert "not allowed by the active skill's scope" in out[1].output  # subagent trace
    assert not (proj / "x.txt").exists()
    assert not (Path.cwd() / "x.txt").exists()


def test_claude_code_tool_names_are_enforced(home, tmp_path, skills, fake_llm):
    # An imported Claude skill names Claude Code's tools; they map to eVi's.
    skills("claude-style", "allowed-tools: Read, Grep, Glob, Bash(git status:*)\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    client = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "claude-style"})),
        fake_llm.call(
            ("read_file", {"path": "a.txt"}),
            ("write_file", {"path": "x.txt", "content": "nope"}),
        ),
        fake_llm.text("read it"),
    ])
    agent = _agent(client, ["invoke_skill", "read_file", "write_file"], cwd=str(proj))

    out = _results(agent.chat("go"))

    assert "[tools scoped to: find_files, read_file, run_command, search_files]" in out[0].output
    assert "hello" in out[1].output
    assert "not allowed by the active skill's scope" in out[2].output
    assert not (proj / "x.txt").exists()


def test_unrecognised_allow_list_is_ignored(home, tmp_path, skills, fake_llm):
    # Only Claude-only tools listed: not understood, so it doesn't lock eVi out.
    # A Claude-named deny-list still applies.
    skills("todo", "allowed-tools: TodoWrite\ndisallowed-tools: Write\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    client = fake_llm([
        fake_llm.call(("invoke_skill", {"name": "todo"})),
        fake_llm.call(
            ("read_file", {"path": "a.txt"}),
            ("write_file", {"path": "x.txt", "content": "nope"}),
        ),
        fake_llm.text("ok"),
    ])
    agent = _agent(client, ["invoke_skill", "read_file", "write_file"], cwd=str(proj))

    out = _results(agent.chat("go"))

    assert "hello" in out[1].output
    assert "not allowed by the active skill's scope" in out[2].output
    assert not (proj / "x.txt").exists()


# ---- hooks bind web sessions and subagents -------------------------------


def _parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    events.append(json.loads(payload))
    return events


@pytest.fixture
def web(home, monkeypatch, tmp_path):
    """Start the web app with `build_agent` replaced by `make(**kwargs)`."""
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.sdk.builder as builder_mod

    def start(make):
        monkeypatch.setattr(builder_mod, "build_agent", lambda *a, **kw: make(**kw))
        monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
        monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
        return TestClient(server_mod.create_app())

    return start


def test_web_session_veto_blocks_write_and_stop_fires_once(web, tmp_path, fake_llm, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    marker = tmp_path / "stop.log"
    client = fake_llm([
        fake_llm.call(
            ("write_file", {"path": "blocked.txt", "content": "no"}),
            ("write_file", {"path": "fine.txt", "content": "yes"}),
        ),
        fake_llm.text("one of two"),
    ])
    reg = HookRegistry([
        _veto_writes(arg_match=(("path", "blocked*"),)),
        _marker_hook("stop", marker),
    ])
    # The server hands every session the shared registry (see the tests below
    # for the one loaded from hooks.toml); here it's this one.
    monkeypatch.setattr(hooks_mod, "shared_registry", lambda: reg)
    http = web(lambda **_: _agent(client, ["write_file"]))

    # The folder chip, then a turn on the web worker thread.
    r = http.post("/api/session/cwd", json={"session_id": "s1", "path": str(proj)})
    assert r.status_code == 200
    events = _parse_sse(http.post("/api/chat", json={"session_id": "s1", "message": "go"}).text)

    outputs = [e["output"] for e in events if e["kind"] == "ToolResult"]
    assert any(out.startswith("BLOCKED BY HOOK 'no-writes'") for out in outputs)
    assert not (proj / "blocked.txt").exists()
    assert (proj / "fine.txt").read_text(encoding="utf-8") == "yes"
    assert not (Path.cwd() / "fine.txt").exists()
    assert events[-1]["kind"] == "Done"
    assert _lines(marker) == ["stop stop"]


def _real_build_agent(client):
    """The server's own build_agent call, made deterministic: our client and
    tools, nothing read from ~/.evi. Hooks stay whatever the server asks for."""
    import evi.sdk.builder as builder_mod

    real = builder_mod.build_agent

    def make(**kw):
        kw.update(
            client=client, config=Config(), tools=[REGISTRY["write_file"]],
            enable_memory=False, enable_skills=False, enable_guardrails=False,
            transcripts=None,
        )
        return real(**kw)

    return make


def test_web_session_loads_user_hooks(web, home, tmp_path, fake_llm):
    (home / "hooks.toml").write_text(
        '[[before_tool_call]]\nname = "no-writes"\nmatch = "write_file"\n'
        f"command = {json.dumps(_veto_writes().command)}\nveto_on_nonzero = true\n",
        encoding="utf-8",
    )
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "no"})),
        fake_llm.text("blocked"),
    ])
    http = web(_real_build_agent(client))
    events = _parse_sse(http.post("/api/chat", json={"session_id": "s1", "message": "go"}).text)
    outputs = [e["output"] for e in events if e["kind"] == "ToolResult"]
    assert outputs and "BLOCKED BY HOOK 'no-writes'" in outputs[0]
    assert not (Path.cwd() / "x.txt").exists()


def test_web_hooks_editor_save_applies_to_open_session(web, home, tmp_path, fake_llm):
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "first.txt", "content": "ok"})),
        fake_llm.text("wrote"),
        fake_llm.call(("write_file", {"path": "second.txt", "content": "no"})),
        fake_llm.text("blocked"),
    ])
    http = web(_real_build_agent(client))
    http.post("/api/chat", json={"session_id": "s1", "message": "one"})
    assert (Path.cwd() / "first.txt").exists()
    raw = (
        '[[before_tool_call]]\nname = "no-writes"\nmatch = "write_file"\n'
        f"command = {json.dumps(_veto_writes().command)}\nveto_on_nonzero = true\n"
    )
    assert http.post("/api/hooks", json={"raw": raw}).status_code == 200
    http.post("/api/chat", json={"session_id": "s1", "message": "two"})
    assert not (Path.cwd() / "second.txt").exists()


def _stub_runner(monkeypatch, runner: str, ran: list) -> tuple[str, str, dict]:
    """Stub everything the `runner` route loads or calls around build_agent, so
    the only real step is the agent build; `ran` gets each built agent."""
    import evi.evals as evals_mod
    import evi.headless as headless_mod
    import evi.recipes as recipes_mod
    import evi.routines as routines_mod
    import evi.ultracode as ultracode_mod
    import evi.workflows as workflows_mod

    done = SimpleNamespace(text="ok", error="")
    monkeypatch.setattr(headless_mod, "run_headless", lambda agent, *a, **k: ran.append(agent) or done)
    monkeypatch.setattr(recipes_mod, "load_recipe", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(recipes_mod, "run_recipe_headless", lambda agent, r: ran.append(agent) or [])
    if runner == "workflow":
        monkeypatch.setattr(workflows_mod, "load_workflow", lambda name: SimpleNamespace(name=name))
        monkeypatch.setattr(workflows_mod, "run_workflow", lambda wf, run_step, variables:
                            {"s": run_step("p", SimpleNamespace(mode=""))})
        return "post", "/api/dispatch/workflow/w", {}
    if runner == "recipe":
        return "post", "/api/recipes/run", {"name": "r"}
    if runner == "routine":
        monkeypatch.setattr(routines_mod, "get_by_token", lambda token: SimpleNamespace(
            name="rt", recipe="r", enabled=True, yes=True))
        return "post", "/api/routine/tok", {}
    if runner == "ultracode":
        def make_runner(factory, stage_models=None):
            ran.append(factory(None))
            return lambda *a, **k: ""
        monkeypatch.setattr(ultracode_mod, "make_runner", make_runner)
        monkeypatch.setattr(ultracode_mod, "run_ultracode", lambda task, run_one, cfg:
                            SimpleNamespace(answer="", stages=[], config=cfg))
        return "post", "/api/dispatch/ultracode", {"task": "t"}
    assert runner == "eval"
    monkeypatch.setattr(evals_mod, "load_suite", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(evals_mod, "make_runners", lambda factory, default_mode="":
                        (ran.append(factory()), None))
    monkeypatch.setattr(evals_mod, "run_eval", lambda suite, run_one, judge_fn=None: {})
    return "post", "/api/evals/run", {"name": "e"}


@pytest.mark.parametrize("runner", ["workflow", "recipe", "routine", "ultracode"])
def test_server_runs_apply_the_shared_hooks(web, home, monkeypatch, fake_llm, runner):
    ran: list = []
    method, url, body = _stub_runner(monkeypatch, runner, ran)
    http = web(lambda **_: _agent(fake_llm([]), []))
    r = getattr(http, method)(url, json=body)
    assert r.status_code == 200, r.text
    assert ran and all(a.hooks is hooks_mod.shared_registry() for a in ran)


def test_eval_runs_skip_user_hooks(web, home, monkeypatch, fake_llm):
    """Scores must not depend on this machine's hooks."""
    ran: list = []
    method, url, body = _stub_runner(monkeypatch, "eval", ran)
    http = web(lambda **_: _agent(fake_llm([]), []))
    assert getattr(http, method)(url, json=body).status_code == 200
    assert ran and all(a.hooks is None for a in ran)


def test_subagent_inherits_parent_hooks_and_folder(home, tmp_path, fake_llm, monkeypatch):
    import evi.llm.subagent as subagent_mod

    proj = tmp_path / "proj"
    proj.mkdir()
    marker = tmp_path / "stop.log"
    sub_client = fake_llm([
        fake_llm.call(
            ("write_file", {"path": "blocked.txt", "content": "no"}),
            ("write_file", {"path": "ok.txt", "content": "yes"}),
        ),
        fake_llm.text("wrote one"),
    ])
    monkeypatch.setattr(subagent_mod, "make_client", lambda *_: sub_client)
    monkeypatch.setattr(subagent_mod, "Config", SimpleNamespace(load=Config))
    _writer_profile(home, monkeypatch)
    parent = fake_llm([
        fake_llm.call(("delegate", {"profile": "writer", "task": "write two files"})),
        fake_llm.text("delegated"),
    ])
    reg = HookRegistry([
        _veto_writes(arg_match=(("path", "blocked*"),)),
        _marker_hook("stop", marker),
        _marker_hook("user_prompt_submit", marker),
    ])
    agent = _agent(parent, ["delegate"], hooks=reg, cwd=str(proj))

    out = _results(agent.chat("go"))

    assert "BLOCKED BY HOOK 'no-writes'" in out[0].output  # the subagent's trace
    assert not (proj / "blocked.txt").exists()
    assert (proj / "ok.txt").read_text(encoding="utf-8") == "yes"
    assert not (Path.cwd() / "ok.txt").exists()
    # The subagent gets the tool hooks only: its task is not a user prompt and
    # its end is not the end of the user's turn.
    assert _lines(marker) == ["user_prompt_submit go", "stop stop"]


def test_one_turn_with_parallel_research_fires_lifecycle_hooks_once(
    home, tmp_path, fake_llm, monkeypatch
):
    import evi.llm.subagent as subagent_mod

    marker = tmp_path / "lifecycle.log"
    clients = [fake_llm([fake_llm.text(f"found {n}")]) for n in ("a", "b", "c")]
    lock = threading.Lock()

    def next_client(*_):
        with lock:
            return clients.pop()

    monkeypatch.setattr(subagent_mod, "make_client", next_client)
    monkeypatch.setattr(subagent_mod, "Config", SimpleNamespace(load=Config))
    parent = fake_llm([
        fake_llm.call(("parallel_research", {"tasks": ["a", "b", "c"]})),
        fake_llm.text("summary"),
    ])
    reg = HookRegistry([
        _marker_hook("stop", marker),
        _marker_hook("user_prompt_submit", marker),
    ])
    agent = _agent(parent, ["parallel_research"], hooks=reg)

    out = _results(agent.chat("research"))

    assert "found a" in out[0].output and "found c" in out[0].output
    assert _lines(marker) == ["user_prompt_submit research", "stop stop"]


def test_subagent_hooks_can_be_turned_off(home, tmp_path, fake_llm, monkeypatch):
    import evi.llm.subagent as subagent_mod

    sub_client = fake_llm([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "yes"})),
        fake_llm.text("ok"),
    ])
    monkeypatch.setattr(subagent_mod, "make_client", lambda *_: sub_client)
    monkeypatch.setattr(subagent_mod, "Config", SimpleNamespace(load=Config))
    token = hooks_mod.bind(HookRegistry([_veto_writes()]))
    try:
        subagent_mod.run_subagent(
            system_prompt="sub", task="t", tool_categories=("fs",), hooks=None
        )
    finally:
        hooks_mod.unbind(token)
    assert (Path.cwd() / "x.txt").exists()


def test_shared_registry_reloads_in_place(home, tmp_path, fake_llm):
    shared = hooks_mod.shared_registry()
    assert shared.hooks == []
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "no"})),
        fake_llm.text("blocked"),
    ])
    agent = _agent(client, ["write_file"], hooks=shared)
    (home / "hooks.toml").write_text(
        '[[before_tool_call]]\nname = "no-writes"\nmatch = "write_file"\n'
        f"command = {json.dumps(_veto_writes().command)}\nveto_on_nonzero = true\n",
        encoding="utf-8",
    )
    assert hooks_mod.reload_shared() is shared and len(shared.hooks) == 1
    out = _results(agent.chat("go"))
    assert "BLOCKED BY HOOK 'no-writes'" in out[0].output
    assert not (Path.cwd() / "x.txt").exists()


def test_shared_registry_is_empty_in_safe_mode(home, monkeypatch):
    (home / "hooks.toml").write_text(
        '[[stop]]\nname = "s"\ncommand = ["x"]\n', encoding="utf-8"
    )
    monkeypatch.setenv("EVI_SAFE_MODE", "1")
    assert hooks_mod.shared_registry().hooks == []


# ---- stop fires exactly once per turn ------------------------------------


@pytest.fixture
def stop_agent(home, tmp_path, fake_llm):
    marker = tmp_path / "stop.log"

    def make(script, **kw):
        reg = HookRegistry([_marker_hook("stop", marker)] + kw.pop("hooks", []))
        return _agent(fake_llm(script), ["write_file"], hooks=reg, **kw)

    return make, marker


def test_stop_fires_once_when_the_consumer_breaks_on_done(stop_agent, fake_llm):
    make, marker = stop_agent
    agent = make([fake_llm.text("hi")])
    for event in agent.chat("hello"):
        if isinstance(event, Done):
            break
    assert _lines(marker) == ["stop stop"]


def test_stop_fires_once_when_the_generator_is_drained(stop_agent, fake_llm):
    make, marker = stop_agent
    agent = make([fake_llm.text("hi"), fake_llm.text("again")])
    list(agent.chat("hello"))
    assert _lines(marker) == ["stop stop"]
    list(agent.chat("more"))
    assert _lines(marker) == ["stop stop", "stop stop"]


def test_stop_fires_once_for_headless(stop_agent, fake_llm):
    from evi.headless import run_headless

    make, marker = stop_agent
    agent = make([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "1"})),
        fake_llm.text("done"),
    ])
    assert run_headless(agent, "go").text == "done"
    assert _lines(marker) == ["stop stop"]


def test_stop_fires_once_for_a_subagent(stop_agent, fake_llm, monkeypatch):
    import evi.llm.subagent as subagent_mod

    _make, marker = stop_agent
    client = fake_llm([fake_llm.text("report")])
    monkeypatch.setattr(subagent_mod, "make_client", lambda *_: client)
    monkeypatch.setattr(subagent_mod, "Config", SimpleNamespace(load=Config))
    reg = HookRegistry([_marker_hook("stop", marker)])
    out = subagent_mod.run_subagent(system_prompt="sub", task="t", hooks=reg)
    assert out == "report"
    assert _lines(marker) == ["stop stop"]


def test_stop_reports_max_turns(stop_agent, fake_llm):
    make, marker = stop_agent
    agent = make([fake_llm.call(("write_file", {"path": "x.txt", "content": "1"}))])
    events = list(agent.chat("go", max_turns=1))
    assert isinstance(events[-1], Done) and events[-1].reason == "max_turns_reached"
    assert _lines(marker) == ["stop max_turns_reached"]


def test_stop_fires_once_on_reroll(stop_agent, fake_llm):
    make, marker = stop_agent
    agent = make([fake_llm.text("first"), fake_llm.text("second")])
    list(agent.chat("hello"))
    agent.rewind_to_last_user()
    for event in agent.continue_chat():
        if isinstance(event, Done):
            break
    assert _lines(marker) == ["stop stop", "stop stop"]


def test_blocked_prompt_fires_no_stop(stop_agent, fake_llm):
    make, marker = stop_agent
    block = Hook(
        name="no-prompts", event="user_prompt_submit", match="*",
        command=_py("import sys; sys.exit(1)"), veto_on_nonzero=True,
    )
    agent = make([fake_llm.text("never")], hooks=[block])
    events = list(agent.chat("hello"))
    assert events[-1] == Done("hook_blocked")
    assert _lines(marker) == []


# ---- hook payload delivery ------------------------------------------------


@pytest.fixture
def stdin_never_closes():
    """Point this process's stdin at a pipe nobody writes to, so a hook that
    inherited it would block until its timeout (the desktop sidecar's stdin
    behaves like this)."""
    r, w = os.pipe()
    try:
        saved = os.dup(0)
    except OSError:
        saved = None

    def _sync_std_handle():
        if os.name == "nt":  # subprocess hands children the Win32 std handle
            import ctypes
            import msvcrt

            k32 = ctypes.windll.kernel32
            k32.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
            k32.SetStdHandle(ctypes.c_ulong(-10 & 0xFFFFFFFF),
                             ctypes.c_void_p(msvcrt.get_osfhandle(0)))

    os.dup2(r, 0)
    _sync_std_handle()
    try:
        yield
    finally:
        if saved is not None:
            os.dup2(saved, 0)
            os.close(saved)
            _sync_std_handle()
        os.close(r)
        os.close(w)


def test_hook_reading_stdin_does_not_block(home, tmp_path, stdin_never_closes):
    seen = tmp_path / "stdin.txt"
    hook = Hook(
        name="reads-stdin", event="before_tool_call", match="*", timeout=20,
        command=_py(f"import sys; open({str(seen)!r}, 'w').write(repr(sys.stdin.read()))"),
    )
    t0 = time.monotonic()
    res = hooks_mod._run_hook(hook, "write_file", "{}", None)
    assert res.exit_code == 0 and not res.timed_out, res.stderr
    assert seen.read_text() == "''"  # EOF straight away
    assert time.monotonic() - t0 < 20


_REPORT = (
    "import json, os; f = os.environ.get('EVI_HOOK_ARGS_FILE');"
    "json.dump({{'env': os.environ['EVI_HOOK_ARGS_JSON'],"
    " 'truncated': os.environ.get('EVI_HOOK_ARGS_TRUNCATED'), 'file': f,"
    " 'full': open(f, encoding='utf-8').read() if f else None}},"
    " open({out!r}, 'w', encoding='utf-8'))"
)


def _report(out: Path) -> dict:
    return json.loads(out.read_text(encoding="utf-8"))


def _over_limit() -> int:
    """A count of ASCII characters just past what EVI_HOOK_ARGS_JSON holds."""
    return hooks_mod._ENV_ARGS_LIMIT + 1000


def test_megabyte_payload_reaches_an_observer_hook(home, tmp_path):
    out = tmp_path / "report.json"
    hook = Hook(name="report", event="before_tool_call", match="*",
                command=_py(_REPORT.format(out=str(out))))
    # > 1 MB of UTF-8 with 2- and 4-byte characters around the cut.
    payload = json.dumps({"content": "aé😀" * 150_000, "path": "big.txt"}, ensure_ascii=False)
    assert len(payload.encode("utf-8")) > 1_000_000

    res = hooks_mod._run_hook(hook, "write_file", payload, None)

    assert res.exit_code == 0, res.stderr
    data = _report(out)
    assert data["full"] == payload
    assert data["truncated"] == "1"
    assert hooks_mod._env_size(data["env"]) <= hooks_mod._ENV_ARGS_LIMIT
    # Still valid JSON with every key: only the long value was shortened.
    cut = json.loads(data["env"])
    assert cut["path"] == "big.txt"
    assert cut["content"].startswith("aé😀aé😀")
    assert cut["content"].endswith("see EVI_HOOK_ARGS_FILE]")
    assert not Path(data["file"]).exists()  # cleaned up after the hook


def test_long_prompt_reaches_an_observer_as_a_prefix_and_a_file(home, tmp_path):
    out = tmp_path / "report.json"
    hook = Hook(name="report", event="user_prompt_submit", match="*",
                command=_py(_REPORT.format(out=str(out))))
    prompt = "é" * _over_limit()
    _, veto = HookRegistry([hook]).run_lifecycle("user_prompt_submit", payload=prompt)
    assert veto is None
    data = _report(out)
    assert data["truncated"] == "1" and data["full"] == prompt
    assert prompt.startswith(data["env"]) and len(data["env"]) > 1000


def _log_export(records: int) -> str:
    """A JSON log export: thousands of short string values (~430 KB at 3000)."""
    return json.dumps([
        {"ts": f"2026-10-14T12:00:{i % 60:02d}", "level": "INFO",
         "msg": f"request handled for user {i} path /api/v1/items/{i} with status 200"
                " and latency 12ms"}
        for i in range(records)
    ])


@pytest.mark.parametrize("payload", [
    pytest.param(json.dumps({"items": ["x" * 4000] * 600, "path": "p"}), id="many-long-strings"),
    pytest.param(_log_export(3000), id="log-export"),
])
def test_shortening_a_big_json_payload_is_quick(payload):
    # It runs on the loop thread before the hook starts, outside the hook's
    # timeout. Re-dumping the whole document once per string stalled a turn
    # for seconds (minutes at 1 MB) before any hook ran.
    t0 = time.perf_counter()
    out = hooks_mod._shorten(hooks_mod._env_safe(payload), hooks_mod._ENV_ARGS_LIMIT)
    assert time.perf_counter() - t0 < 1.0
    assert 1000 < hooks_mod._env_size(out) <= hooks_mod._ENV_ARGS_LIMIT


def test_a_shortened_object_keeps_every_key_and_cuts_long_strings_alike():
    limit = hooks_mod._ENV_ARGS_LIMIT
    payload = {"path": "notes/a.txt", "a": "é" * limit, "b": "y" * (2 * limit),
               "n": 7, "tags": ["t"] * 50, "rows": [{"id": "x" * 3 * limit}]}
    out = hooks_mod._shorten(json.dumps(payload, ensure_ascii=False), limit)
    size = hooks_mod._env_size(out)
    assert size <= limit
    cut = json.loads(out)
    assert cut["path"] == "notes/a.txt" and cut["n"] == 7 and cut["tags"] == ["t"] * 50

    def kept(value: str, original: str) -> int:
        mark = f"...[cut {len(original)} chars; see EVI_HOOK_ARGS_FILE]"
        assert value.endswith(mark)
        assert original.startswith(value[: -len(mark)])
        return len(value) - len(mark)

    lengths = {kept(cut["a"], payload["a"]), kept(cut["b"], payload["b"]),
               kept(cut["rows"][0]["id"], payload["rows"][0]["id"])}
    assert len(lengths) == 1 and lengths.pop() > 1000  # one shared length
    # ...the largest that fits: one more character each would not.
    assert size > limit - 3 * 4


def test_observer_hook_on_a_pasted_json_prompt_does_not_stall_the_turn(
    home, tmp_path, fake_llm
):
    seen = tmp_path / "prompt.txt"
    observer = Hook(
        name="log-prompts", event="user_prompt_submit", match="*",
        command=_py(f"import os; open({str(seen)!r}, 'w', encoding='utf-8')"
                    ".write(os.environ['EVI_HOOK_ARGS_JSON'])"),
    )
    prompt = _log_export(3000)
    agent = _agent(fake_llm([fake_llm.text("summarised")]), [],
                   hooks=HookRegistry([observer]))
    t0 = time.perf_counter()
    events = list(agent.chat(prompt))
    assert time.perf_counter() - t0 < 5
    assert isinstance(events[-1], Done)
    got = seen.read_text(encoding="utf-8")
    assert prompt.startswith(got) and len(got) > 1000


def test_small_payload_is_whole_in_env_and_stays_off_disk(home, tmp_path):
    out = tmp_path / "report.json"
    payload = json.dumps({"path": "x.txt", "content": "hi"})
    hook = Hook(name="report", event="before_tool_call", match="*",
                command=_py(_REPORT.format(out=str(out))))
    assert hooks_mod._run_hook(hook, "write_file", payload, None).exit_code == 0
    data = _report(out)
    assert data["env"] == payload
    assert data["truncated"] is None and data["file"] is None  # nothing on disk

    # A hook that reads the file always gets one.
    reader = Hook(name="report", event="before_tool_call", match="*", args_via_file=True,
                  command=_py(_REPORT.format(out=str(out))))
    assert hooks_mod._run_hook(reader, "write_file", payload, None).exit_code == 0
    data = _report(out)
    assert data["env"] == payload and data["full"] == payload
    assert data["truncated"] is None
    assert not Path(data["file"]).exists()


def test_payload_under_the_os_limit_reaches_a_json_guard_whole(home, tmp_path, fake_llm):
    # 20 KB: past the 16 KB the env copy was once cut at, well under the OS
    # limit. examples/hooks.toml's guard json-parses EVI_HOOK_ARGS_JSON; it must
    # still parse, and let a write under its allowed root through.
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    guard = Hook(
        name="no-system-writes", event="before_tool_call", match="write_file",
        veto_on_nonzero=True,
        command=_py(
            "import os, sys, json; a = json.loads(os.environ['EVI_HOOK_ARGS_JSON']);"
            " p = os.path.realpath(a.get('path', ''));"
            f" sys.exit(0 if p.startswith(os.path.realpath({str(allowed)!r})) else 1)"
        ),
    )
    client = fake_llm([
        fake_llm.call(("write_file", {"path": str(allowed / "big.txt"), "content": "x" * 20_000})),
        fake_llm.text("wrote"),
    ])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([guard]))
    out = _results(agent.chat("go"))
    assert (allowed / "big.txt").exists(), out[0].output


# hooks.md's no-system-writes guard (`grep -qv "/etc/"` over EVI_HOOK_ARGS_JSON).
_ETC_GUARD = "import os, sys; sys.exit(1 if '/etc/' in os.environ['EVI_HOOK_ARGS_JSON'] else 0)"


@pytest.mark.parametrize("pad", ["20k", "over-limit"])
def test_padding_cannot_push_the_path_past_an_env_guard(home, tmp_path, fake_llm, pad):
    # Content ahead of `path`, padded past any cut, must not hide the path from
    # a guard that reads EVI_HOOK_ARGS_JSON: under the OS limit the guard sees
    # all of it; over it, the guard isn't run and the write is blocked.
    n = 20_000 if pad == "20k" else _over_limit()
    target = tmp_path / "etc" / "passwd"
    target.parent.mkdir()
    guard = Hook(name="no-system-writes", event="before_tool_call", match="write_file",
                 veto_on_nonzero=True, command=_py(_ETC_GUARD))
    client = fake_llm([
        fake_llm.call(("write_file", {"content": "A" * n, "path": target.as_posix()})),
        fake_llm.text("tried"),
    ])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([guard]))
    out = _results(agent.chat("go"))
    assert out[0].output.startswith("BLOCKED BY HOOK 'no-system-writes'")
    assert not target.exists()


def test_oversize_write_is_blocked_when_the_guard_cannot_see_it(home, tmp_path, fake_llm):
    ran = tmp_path / "ran.txt"
    guard = Hook(
        name="lenient", event="before_tool_call", match="write_file", veto_on_nonzero=True,
        command=_py(f"open({str(ran)!r}, 'w').write('ran')"),  # would allow anything
    )
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "big.txt", "content": "x" * _over_limit()})),
        fake_llm.text("blocked"),
    ])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([guard]))
    out = _results(agent.chat("go"))
    assert out[0].output.startswith("BLOCKED BY HOOK 'lenient'")
    assert "set args_via_file = true" in out[0].output
    assert not ran.exists()  # the guard was never asked
    assert not (Path.cwd() / "big.txt").exists()



def test_oversize_input_is_blocked_when_the_args_file_cannot_be_written(
    home, tmp_path, monkeypatch
):
    monkeypatch.setattr(hooks_mod, "_write_args_file", lambda raw: "")
    ran = tmp_path / "ran.txt"
    reader = Hook(
        name="reader", event="before_tool_call", match="write_file", veto_on_nonzero=True,
        args_via_file=True, command=_py(f"open({str(ran)!r}, 'w').write('ran')"),
    )
    payload = json.dumps({"path": "big.txt", "content": "x" * _over_limit()})
    _, veto = HookRegistry([reader]).run_before("write_file", payload)
    assert veto is not None and "could not be written" in veto.stderr
    assert not ran.exists()


# hooks.md Example 2: the prompt secret scanner, reading the env or the file.
_SECRET = r"re.search(r'(?i)(api[_-]?key|secret|password)\s*[:=]\s*\S', prompt)"
_SCAN_ENV = f"import os, re, sys; prompt = os.environ.get('EVI_HOOK_ARGS_JSON', ''); sys.exit(1 if {_SECRET} else 0)"
_SCAN_FILE = (
    "import os, re, sys; prompt = open(os.environ['EVI_HOOK_ARGS_FILE'], encoding='utf-8').read();"
    f" sys.exit(1 if {_SECRET} else 0)"
)


@pytest.mark.parametrize("size", [22_000, "over-limit"])
def test_secret_at_the_end_of_a_long_prompt_is_caught(home, fake_llm, size):
    n = size if isinstance(size, int) else _over_limit()
    scanner = Hook(name="no-secrets", event="user_prompt_submit", match="*",
                   veto_on_nonzero=True, command=_py(_SCAN_ENV))
    agent = _agent(fake_llm([fake_llm.text("never")]), [], hooks=HookRegistry([scanner]))
    events = list(agent.chat("x" * n + "\napi_key = sk-live-123"))
    assert events[-1] == Done("hook_blocked")


def test_file_reading_scanner_judges_a_prompt_of_any_size(home, fake_llm):
    scanner = Hook(name="no-secrets", event="user_prompt_submit", match="*",
                   veto_on_nonzero=True, args_via_file=True, command=_py(_SCAN_FILE))
    agent = _agent(fake_llm([fake_llm.text("fine")]), [], hooks=HookRegistry([scanner]))
    n = _over_limit()
    assert list(agent.chat("x" * n + "\npassword: hunter2"))[-1] == Done("hook_blocked")
    assert list(agent.chat("x" * n + "\nnothing to hide"))[-1] == Done("stop")


def test_megabyte_write_vetoed_by_a_hook_that_reads_the_args_file(home, tmp_path, fake_llm):
    guard = Hook(
        name="size-guard", event="before_tool_call", match="write_file",
        veto_on_nonzero=True, args_via_file=True,
        command=_py(
            "import json, os, sys; a = json.load(open(os.environ['EVI_HOOK_ARGS_FILE'],"
            " encoding='utf-8')); n = len(a['content']);"
            " sys.stderr.write(f'too big: {n}') if n > 100000 else None;"
            " sys.exit(1 if n > 100000 else 0)"
        ),
    )
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "big.txt", "content": "x" * 1_048_576})),
        fake_llm.text("blocked"),
    ])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([guard]))
    out = _results(agent.chat("go"))
    assert out[0].output == "BLOCKED BY HOOK 'size-guard': too big: 1048576"
    assert not (Path.cwd() / "big.txt").exists()


# ---- a hook that can't be run never lets a guarded action through ----------


def test_nul_in_a_prompt_still_reaches_the_prompt_veto(home, fake_llm):
    block = Hook(name="no-prompts", event="user_prompt_submit", match="*",
                 veto_on_nonzero=True, command=_py("import sys; sys.exit(1)"))
    agent = _agent(fake_llm([fake_llm.text("never")]), [], hooks=HookRegistry([block]))
    assert list(agent.chat("secret\x00"))[-1] == Done("hook_blocked")


def test_nul_in_tool_args_reaches_the_guard(home, tmp_path, fake_llm):
    seen = tmp_path / "seen.txt"
    guard = Hook(
        name="no-writes", event="before_tool_call", match="write_file", veto_on_nonzero=True,
        command=_py(
            f"import os, sys; open({str(seen)!r}, 'w', encoding='utf-8')"
            ".write(os.environ['EVI_HOOK_ARGS_JSON']); sys.exit(1)"
        ),
    )
    raw = '{"path": "x.txt", "content": "a\x00b"}'  # a raw NUL from the model
    client = fake_llm([fake_llm.call(("write_file", raw)), fake_llm.text("blocked")])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([guard]))
    out = _results(agent.chat("go"))
    assert out[0].output.startswith("BLOCKED BY HOOK 'no-writes'")
    assert "a\\u0000b" in seen.read_text(encoding="utf-8")


def test_hook_command_with_a_nul_reports_an_exec_failure(home):
    hook = Hook(name="bad", event="before_tool_call", match="*",
                command=[sys.executable, "-c", "pass\x00"], veto_on_nonzero=True)
    res = hooks_mod._run_hook(hook, "write_file", "{}", None)
    assert res.exit_code == 126 and res.vetoed


def test_hooks_that_cannot_run_fail_closed_for_guards_only(home, tmp_path, fake_llm, monkeypatch):
    def broken(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks_mod, "_run_hook", broken)
    prompt_guard = Hook(name="p", event="user_prompt_submit", match="*",
                        veto_on_nonzero=True, command=["unused"])
    agent = _agent(fake_llm([fake_llm.text("never")]), [], hooks=HookRegistry([prompt_guard]))
    assert list(agent.chat("hi"))[-1] == Done("hook_blocked")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("hello", encoding="utf-8")
    observer = Hook(name="audit", event="before_tool_call", match="read_file", command=["unused"])
    stop = Hook(name="ding", event="stop", match="*", command=["unused"])
    client = fake_llm([
        fake_llm.call(
            ("write_file", {"path": "x.txt", "content": "no"}),
            ("read_file", {"path": "a.txt"}),
        ),
        fake_llm.text("ok"),
    ])
    reg = HookRegistry([_veto_writes(), observer, stop])
    agent = _agent(client, ["write_file", "read_file"], hooks=reg, cwd=str(proj))
    events = list(agent.chat("go"))
    out = _results(events)
    assert out[0].output.startswith("BLOCKED BY HOOK")
    assert not (proj / "x.txt").exists()
    assert "hello" in out[1].output  # an observer that can't run doesn't block
    assert events[-1] == Done("stop")


# ---- the args file never outlives eVi --------------------------------------


@pytest.fixture
def held_args_files(monkeypatch):
    """Make deleting a hook's args file fail, as on Windows while a background
    child the hook started still has it open. Set `.held = False` to let go."""
    real_unlink = os.unlink
    state = SimpleNamespace(held=True)

    def unlink(path, *a, **kw):
        if state.held and Path(path).name.startswith("args-"):
            raise PermissionError(13, "in use", str(path))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", unlink)
    return state


def _secret_report_hook(out: Path) -> Hook:
    return Hook(name="report", event="after_tool_call", match="*", args_via_file=True,
                command=_py(_REPORT.format(out=str(out))))


def test_args_file_a_hook_left_open_is_removed_on_the_next_run(home, tmp_path, held_args_files):
    out = tmp_path / "report.json"
    hooks_mod._run_hook(_secret_report_hook(out), "x", '{"k": "API_KEY=sk-live-123"}', "")
    left = Path(_report(out)["file"])
    assert left.exists()  # couldn't be deleted yet

    held_args_files.held = False
    hooks_mod._run_hook(_veto_writes(), "write_file", "{}", None)  # any later hook run
    assert not left.exists()


def test_args_files_are_removed_when_evi_exits(home, tmp_path, held_args_files):
    out = tmp_path / "report.json"
    hooks_mod._run_hook(_secret_report_hook(out), "x", '{"k": "API_KEY=sk-live-123"}', "")
    left = Path(_report(out)["file"])
    assert left.exists()
    if os.name != "nt":  # a private directory
        assert (left.parent.stat().st_mode & 0o777) == 0o700

    held_args_files.held = False
    hooks_mod._cleanup_args_files()  # what atexit runs
    assert not left.exists() and not left.parent.exists()


# ---- hooks run in the session folder ----------------------------------------


def test_hooks_run_in_the_session_folder(home, tmp_path, fake_llm):
    # A "stay inside this folder" guard resolves the tool's relative path the
    # way the tool does: against the session folder, not the process cwd.
    root = Path.cwd()
    outside = tmp_path / "outside"
    outside.mkdir()
    seen = tmp_path / "guard.json"
    norm = "os.path.normcase(os.path.realpath({}))"
    resolved = norm.format("a['path']")
    guard = Hook(
        name="stay-in-root", event="before_tool_call", match="write_file", veto_on_nonzero=True,
        command=_py(
            "import json, os, sys; a = json.loads(os.environ['EVI_HOOK_ARGS_JSON']);"
            f" json.dump({{'cwd': os.getcwd(), 'evi_cwd': os.environ['EVI_CWD']}},"
            f" open({str(seen)!r}, 'w'));"
            f" p = {resolved};"
            f" sys.exit(0 if p.startswith({norm.format(repr(str(root)))} + os.sep) else 1)"
        ),
    )
    client = fake_llm([
        fake_llm.call(("write_file", {"path": "x.txt", "content": "hi"})),
        fake_llm.text("tried"),
        fake_llm.call(("write_file", {"path": "y.txt", "content": "hi"})),
        fake_llm.text("wrote"),
    ])
    agent = _agent(client, ["write_file"], hooks=HookRegistry([guard]), cwd=str(outside))

    out = _results(agent.chat("go"))

    assert out[0].output.startswith("BLOCKED BY HOOK 'stay-in-root'")
    assert not (outside / "x.txt").exists()
    data = json.loads(seen.read_text())
    assert _same_dir(data["cwd"], outside) and _same_dir(data["evi_cwd"], outside)

    agent.cwd = ""  # no session folder: the process cwd, which the guard allows
    list(agent.chat("again"))
    assert (root / "y.txt").exists()
    assert _same_dir(json.loads(seen.read_text())["evi_cwd"], root)


def test_url_hook_is_told_the_session_folder(home, tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            received.update(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    hook = Hook(name="wh", event="before_tool_call", match="*", command=[], timeout=5,
                url=f"http://127.0.0.1:{srv.server_address[1]}/hook")
    proj = tmp_path / "proj"
    proj.mkdir()
    try:
        HookRegistry([hook]).run_before("write_file", '{"path": "x"}', cwd=str(proj))
    finally:
        srv.server_close()
    assert received["cwd"] == str(proj)
