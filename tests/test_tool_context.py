"""ToolContext and session-bound tools (evi.tools.base), and the per-user
memory they make possible.

Before this, `Tool.call_rich` got only the model's arguments and every tool was
one process-wide REGISTRY entry, so nothing a tool did could belong to a
session. In multi-user web mode that meant remember() wrote the shared
~/.evi/memory store (user B could recall and list user A's notes) while each
login's prompt index read its own per-user store.

These tests drive Agent.chat on the shared `fake_llm` client (conftest.py),
and the multi-user one goes through the real web server and `build_agent`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

import evi.memory as memory_mod
import evi.tools.fs  # noqa: F401  registers write_file & co.
import evi.tools.memory as memory_tools
from evi import checkpoints
from evi import hooks as hooks_mod
from evi.citations import ToolOutput
from evi.config import Config
from evi.llm.agent import Agent, ToolResult
from evi.memory import MemoryStore
from evi.sdk.builder import build_agent
from evi.tools import base
from evi.tools.base import REGISTRY, Tool, ToolContext, active_context, tool


MEMORY_TOOLS = ("remember", "recall", "forget", "list_memories", "recall_by_tag")


# ---- helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_active_tool_context():
    """Every test starts outside any tool call, and none leaks one."""
    token = base.bind_context(None)
    try:
        yield
    finally:
        base.unbind_context(token)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """eVi's home, config, hooks file and the SHARED memory store all under
    tmp_path (never ~/.evi), no safe mode, and a process cwd no tool call
    should land in."""
    import evi.config as config_mod

    evi_home = tmp_path / "evi-home"
    evi_home.mkdir()
    (evi_home / "config.toml").write_text('[llm]\nmodel = "fake"\n', encoding="utf-8")
    monkeypatch.delenv("EVI_SAFE_MODE", raising=False)
    monkeypatch.setenv("EVI_HOME", str(evi_home))
    monkeypatch.setattr(config_mod, "HOME", evi_home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", evi_home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(memory_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", evi_home / "hooks.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    # The shared default store: MemoryStore() and the memory tools' fallback.
    shared = evi_home / "memory"
    monkeypatch.setattr(memory_mod, "MEMORY_DIR", shared)
    monkeypatch.setattr(memory_tools, "_store", MemoryStore(root=shared))
    elsewhere = tmp_path / "process-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return evi_home


def _files(root: Path) -> list[str]:
    """Every file under `root`, relative to it ([] when it doesn't exist)."""
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def _schema(**props: str) -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": v} for k, v in props.items()},
        "required": list(props),
    }


def _probe(seen: list) -> Tool:
    """A tool declaring `ctx`: records (x, ctx) for each call."""

    def probe(x: str, ctx: ToolContext | None = None) -> str:
        seen.append((x, ctx))
        return "ok"

    return Tool(name="probe", description="records its context",
                parameters=_schema(x="string"), func=probe)


def _note_tool() -> Tool:
    """A closure-bound tool: each session it is bound for keeps its own list."""

    def binder(ctx: ToolContext):
        notes: list[str] = []

        def note(text: str) -> str:
            notes.append(text)
            return json.dumps({"session": ctx.session_id, "notes": notes})

        return note

    return Tool(
        name="note",
        description="keep a note for this session",
        parameters=_schema(text="string"),
        func=lambda text: "unbound",
        binder=binder,
    )


def _schema_bytes(tools) -> bytes:
    return json.dumps([t.openai_schema() for t in tools]).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 1. the context reaches tools that declare it ----------------------------


def test_ctx_tool_gets_session_cwd_id_and_turn(home, tmp_path, fake_llm):
    seen: list = []
    client = fake_llm([
        fake_llm.call(("probe", {"x": "first"})),
        fake_llm.text("done"),
        fake_llm.call(("probe", {"x": "second"}), ("probe", {"x": "third"})),
        fake_llm.text("done"),
    ])
    agent = Agent(client=client, config=Config(), tools=[_probe(seen)], session_id="sess-1")
    agent.cwd = str(tmp_path)
    list(agent.chat("one"))
    list(agent.chat("two"))

    got = {x: ctx for x, ctx in seen}
    assert sorted(got) == ["first", "second", "third"]
    assert {x: c.turn_index for x, c in got.items()} == {"first": 1, "second": 2, "third": 2}
    for c in got.values():
        assert isinstance(c, ToolContext)
        assert c.agent is agent
        assert c.session_id == "sess-1"
        assert c.cwd == str(tmp_path)
        assert c.user_root is None  # single-user
        assert c.cancel_token is agent.cancel_token and not c.cancel_token.is_set()
    assert agent.turn_index == 2


def test_legacy_tools_are_called_exactly_as_before(home, fake_llm):
    got: list = []

    def legacy(x: str) -> str:
        got.append(("legacy", x))
        return "l"

    def catch_all(**kwargs) -> str:
        got.append(("catch_all", kwargs))
        return "k"

    def mcp_like(ctx: str = "", **kwargs) -> str:
        got.append(("mcp", ctx, kwargs))
        return "m"

    tools = [
        Tool(name="legacy", description="d", parameters=_schema(x="string"), func=legacy),
        Tool(name="catch_all", description="d", parameters=_schema(), func=catch_all),
        # An MCP tool never gets the context, even one whose server names an
        # argument `ctx`: every argument it gets goes to its server.
        Tool(name="srv.tool", description="d", parameters=_schema(ctx="string"),
             func=mcp_like, category="mcp"),
    ]
    assert [t.takes_ctx for t in tools] == [False, False, False]
    client = fake_llm([
        fake_llm.call(("legacy", {"x": "a"}), ("catch_all", {"y": 1}),
                      ("srv.tool", {"ctx": "the server's own argument"})),
        fake_llm.text("done"),
    ])
    agent = Agent(client=client, config=Config(), tools=tools)
    events = list(agent.chat("go"))

    assert sorted(got, key=repr) == sorted([
        ("legacy", "a"),
        ("catch_all", {"y": 1}),
        ("mcp", "the server's own argument", {}),
    ], key=repr)
    assert [r.output for r in _results(events)] == ["l", "k", "m"]
    # Legacy tools are shared, never copied per agent.
    assert all(agent.tools[t.name] is t for t in tools)


def test_the_model_cannot_forge_the_context(home, fake_llm):
    seen: list = []
    client = fake_llm([
        fake_llm.call(("probe", {"x": "a", "ctx": {"user_root": "/somewhere/else"}})),
        fake_llm.text("done"),
    ])
    agent = Agent(client=client, config=Config(), tools=[_probe(seen)])
    list(agent.chat("go"))
    [(_, ctx)] = seen
    assert isinstance(ctx, ToolContext) and ctx.agent is agent and ctx.user_root is None


def test_tool_decorator_keeps_ctx_out_of_the_schema(monkeypatch):
    monkeypatch.setattr(base, "REGISTRY", {})

    @tool(description="save a line", category="custom")
    def jot(text: str, ctx: ToolContext | None = None) -> str:
        return f"{text}:{ctx.session_id if ctx else 'no-session'}"

    t = base.REGISTRY["jot"]
    assert t.takes_ctx
    assert t.parameters == {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
    }
    # Outside an agent (`evi mcp serve`, a direct call) ctx is None.
    assert t.call({"text": "x"}) == "x:no-session"
    assert t.call({"text": "x"}, ToolContext(session_id="s9")) == "x:s9"
    assert jot("y") == "y:no-session"  # the function itself is unchanged


def test_builtin_ctx_tools_have_no_ctx_in_their_schemas():
    for name in (*MEMORY_TOOLS, "write_file", "edit_file", "apply_patch"):
        t = REGISTRY[name]
        assert t.takes_ctx, name
        assert "ctx" not in t.parameters["properties"], name
        assert "ctx" not in t.parameters["required"], name


def test_trust_slot_and_output_extra(monkeypatch):
    monkeypatch.setattr(base, "REGISTRY", {})
    assert Tool(name="t", description="d", parameters=_schema(), func=lambda: "").trust == "trusted"

    @tool(description="fetch", trust="untrusted")
    def fetch_thing() -> str:
        return ""

    assert base.REGISTRY["fetch_thing"].trust == "untrusted"
    with pytest.raises(ValueError):
        Tool(name="t", description="d", parameters=_schema(), func=lambda: "", trust="maybe")
    assert ToolOutput(text="x").extra == {}
    assert ToolOutput(text="x", extra={"todo": [1]}).extra == {"todo": [1]}


# ---- 2. closure-bound tools belong to one agent -------------------------------


def test_two_agents_never_see_each_others_bound_state(home, fake_llm):
    shared = _note_tool()  # one registry entry both agents are built from
    a = Agent(
        client=fake_llm([
            fake_llm.call(("note", {"text": "a1"})), fake_llm.text("ok"),
            fake_llm.call(("note", {"text": "a2"})), fake_llm.text("ok"),
        ]),
        config=Config(), tools=[shared], session_id="A",
    )
    b = Agent(
        client=fake_llm([fake_llm.call(("note", {"text": "b1"})), fake_llm.text("ok")]),
        config=Config(), tools=[shared], session_id="B",
    )
    first_a = _results(a.chat("1"))
    only_b = _results(b.chat("1"))
    second_a = _results(a.chat("2"))

    assert json.loads(first_a[0].output) == {"session": "A", "notes": ["a1"]}
    assert json.loads(only_b[0].output) == {"session": "B", "notes": ["b1"]}
    assert json.loads(second_a[0].output) == {"session": "A", "notes": ["a1", "a2"]}
    # Each agent runs its own copy; the registry entry itself is untouched.
    assert a.tools["note"] is shared and b.tools["note"] is shared
    a_note, b_note = a.tools_for_session()["note"], b.tools_for_session()["note"]
    assert a_note is not b_note and a_note.bound_from is shared
    assert shared.binder is not None and a_note.binder is None


def test_an_agent_built_from_another_agents_tools_binds_its_own(home, fake_llm):
    """Handing B the copies A runs (its `tools_for_session()`) never hands B
    A's state: B binds each copy's source tool for itself."""
    shared = _note_tool()
    a = Agent(client=fake_llm([fake_llm.call(("note", {"text": "secret-a"})),
                               fake_llm.text("ok")]),
              config=Config(), tools=[shared], session_id="A")
    list(a.chat("1"))
    a_copies = list(a.tools_for_session().values())
    b = Agent(client=fake_llm([fake_llm.call(("note", {"text": "b1"})), fake_llm.text("ok")]),
              config=Config(), tools=a_copies, session_id="B")
    [got] = _results(b.chat("1"))
    assert json.loads(got.output) == {"session": "B", "notes": ["b1"]}
    assert b.tools_for_session(a_copies)["note"] is b.tools_for_session()["note"]
    # A's own state is still A's alone.
    assert json.loads(a.tools_for_session()["note"].call({"text": "a2"})) == {
        "session": "A", "notes": ["secret-a", "a2"],
    }


def _binder_tool(name: str, binder) -> Tool:
    return Tool(name=name, description="d", parameters=_schema(),
                func=lambda: "unbound", binder=binder)


def test_a_failing_binder_breaks_only_its_own_calls(home, fake_llm):
    """A faulty plugin binder must not stop agents being built, nor take the
    other tools of the turn down with it."""
    def bad_binder(ctx):
        raise RuntimeError("binder exploded")

    bad = _binder_tool("bad", bad_binder)
    seen: list = []
    client = fake_llm([fake_llm.call(("bad", {}), ("probe", {"x": "fine"})),
                       fake_llm.text("ok")])
    agent = Agent(client=client, config=Config(), tools=[bad, _probe(seen)])  # no raise
    results = {r.name: r.output for r in _results(agent.chat("go"))}
    assert results["bad"] == "ERROR: RuntimeError: binder exploded"
    assert results["probe"] == "ok" and [x for x, _ in seen] == ["fine"]
    assert agent.tools_for_session()["bad"] is bad  # left unbound, not raised
    # Called outside an agent it reports the error too, never raises.
    assert bad.call({}) == "ERROR: RuntimeError: binder exploded"


def test_a_binder_may_look_up_another_session_tool(home, fake_llm):
    """Binding runs outside the agent's lock: a binder resolving another of
    its session's tools used to deadlock."""
    helper = _note_tool()

    def binder(ctx):
        other = ctx.agent._session_tool(helper)  # re-enters the binding path
        return lambda: other.call({"text": "via-outer"})

    outer = _binder_tool("outer", binder)
    agent = Agent(client=fake_llm([fake_llm.call(("outer", {})), fake_llm.text("ok")]),
                  config=Config(), tools=[outer, helper], session_id="S")
    out: list = []
    t = threading.Thread(target=lambda: out.extend(_results(agent.chat("go"))), daemon=True)
    t.start()
    t.join(timeout=20)
    if t.is_alive():
        # Free the stuck tool thread (a plain Lock may be released from any
        # thread), or the test run would hang at exit on the tool pool.
        agent._bound_lock.release()
        t.join(timeout=20)
        pytest.fail("binding a tool that binds another deadlocked")
    assert json.loads(out[0].output) == {"session": "S", "notes": ["via-outer"]}


def test_reset_starts_session_tools_afresh_and_turns_keep_counting(home, fake_llm):
    """`reset` (/reset, /api/reset) begins a new conversation in the same
    session: bound state starts over, while the turn count goes on, so the
    session's (session, turn) checkpoint tags stay unique."""
    seen: list = []
    agent = Agent(
        client=fake_llm([
            fake_llm.call(("note", {"text": "before-reset"})), fake_llm.text("ok"),
            fake_llm.call(("note", {"text": "after"}), ("probe", {"x": "t"})),
            fake_llm.text("ok"),
        ]),
        config=Config(), tools=[_note_tool(), _probe(seen)], session_id="S",
    )
    list(agent.chat("1"))
    agent.reset()
    after = {r.name: r.output for r in _results(agent.chat("2"))}
    assert json.loads(after["note"]) == {"session": "S", "notes": ["after"]}
    assert [c.turn_index for _, c in seen] == [2]


def test_the_session_bound_is_the_one_a_call_runs_in(home, fake_llm):
    """`evi sessions resume/continue` set agent.session_id after building the
    agent: a binding follows it, and a session switch starts a new binding."""
    agent = Agent(
        client=fake_llm([
            fake_llm.call(("note", {"text": "r1"})), fake_llm.text("ok"),
            fake_llm.call(("note", {"text": "o1"})), fake_llm.text("ok"),
        ]),
        config=Config(), tools=[_note_tool()],
    )
    agent.session_id = "resumed-session"
    [first] = _results(agent.chat("1"))
    assert json.loads(first.output) == {"session": "resumed-session", "notes": ["r1"]}
    agent.session_id = "other-session"
    [second] = _results(agent.chat("2"))
    assert json.loads(second.output) == {"session": "other-session", "notes": ["o1"]}


def test_two_agents_running_at_once_keep_their_own_state(home, fake_llm):
    shared = _note_tool()
    agents = {}
    for sid in ("A", "B"):
        calls = [("note", {"text": f"{sid}{i}"}) for i in range(3)]
        agents[sid] = Agent(
            client=fake_llm([fake_llm.call(*calls), fake_llm.text("ok")]),
            config=Config(), tools=[shared], session_id=sid,
        )
    start = threading.Barrier(2)

    def run(agent: Agent) -> None:
        start.wait(timeout=30)
        list(agent.chat("go"))

    threads = [threading.Thread(target=run, args=(ag,)) for ag in agents.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for sid, agent in agents.items():
        state = json.loads(agent.tools_for_session()["note"].call({"text": "last"}))
        assert state["session"] == sid
        assert sorted(state["notes"]) == sorted([f"{sid}0", f"{sid}1", f"{sid}2", "last"])


def test_a_tool_added_after_build_is_bound_once(home, fake_llm):
    """A mode switch or search_tools puts the unbound registry entry into
    agent.tools: its first call binds it, and later calls reuse that binding."""
    shared = _note_tool()
    agent = Agent(
        client=fake_llm([
            fake_llm.call(("note", {"text": "x"})), fake_llm.text("ok"),
            fake_llm.call(("note", {"text": "y"})), fake_llm.text("ok"),
        ]),
        config=Config(), tools=[], session_id="S",
    )
    agent.tools = {"note": shared}
    list(agent.chat("1"))
    second = _results(agent.chat("2"))
    assert json.loads(second[0].output) == {"session": "S", "notes": ["x", "y"]}


def test_bind_keeps_the_tool_list_and_unbound_calls_still_work():
    shared = _note_tool()
    bound = shared.bind(ToolContext(session_id="s"))
    assert bound is not shared
    assert (bound.name, bound.description, bound.category) == (
        shared.name, shared.description, shared.category,
    )
    assert bound.parameters is shared.parameters  # shared, not copied
    plain = Tool(name="p", description="d", parameters=_schema(), func=lambda: "p")
    assert plain.bind(ToolContext()) is plain
    # Outside an agent a binder tool is bound for the one call.
    assert json.loads(shared.call({"text": "t"})) == {"session": "", "notes": ["t"]}


# ---- 3. the tool list the model sees never changes ----------------------------


def test_tool_schema_bytes_identical_with_and_without_binding(home, fake_llm):
    raw = [REGISTRY[n] for n in (
        "read_file", "write_file", "edit_file", "apply_patch", *MEMORY_TOOLS,
    )] + [_note_tool(), _probe([])]
    want = _digest(_schema_bytes(raw))
    client = fake_llm([
        fake_llm.call(("note", {"text": "x"})), fake_llm.text("ok"),
        fake_llm.text("again"),
    ])
    agent = Agent(client=client, config=Config(), tools=raw)

    assert list(agent.tools) == [t.name for t in raw]  # same names, same order
    assert _digest(_schema_bytes(agent.tools.values())) == want
    assert _digest(_schema_bytes(agent.tools_for_session(raw).values())) == want
    list(agent.chat("1"))
    list(agent.chat("2"))
    # Every request offered byte-identical tools, before and after binding.
    assert len(client.requests) == 3
    for req in client.requests:
        assert _digest(json.dumps(req["tools"]).encode("utf-8")) == want


# ---- 4. memory tools work on the session user's store -------------------------


def _memory_agent(client, user_root: Path | None) -> Agent:
    return build_agent(
        config=Config.load(), client=client, tool_categories=["memory"],
        user_root=user_root, enable_skills=False, enable_project=False,
        enable_hooks=False, enable_guardrails=False,
    )


def test_remember_in_a_users_session_stays_in_that_users_root(home, fake_llm):
    users = home / "users"
    alice = _memory_agent(fake_llm([
        fake_llm.call(("remember", {"name": "plans-zq7", "content": "alice's plans",
                                    "tags": "private"})),
        fake_llm.text("saved"),
    ]), users / "alice")
    bob = _memory_agent(fake_llm([
        fake_llm.call(("recall", {"name": "plans-zq7"}), ("list_memories", {}),
                      ("recall_by_tag", {"tag": "private"}), ("forget", {"name": "plans-zq7"})),
        fake_llm.text("nothing"),
    ]), users / "bob")

    saved = _results(alice.chat("remember my plans"))
    assert "users" in saved[0].output and "alice" in saved[0].output
    assert _files(users / "alice" / "memory") == ["INDEX.md", "plans-zq7.md"]
    assert _files(home / "memory") == []  # the shared store is never touched

    recall, listed, tagged, forgot = (r.output for r in _results(bob.chat("what are alice's plans?")))
    assert recall.startswith("ERROR: no memory named 'plans-zq7'")
    assert json.loads(listed) == [] and json.loads(tagged) == []
    assert forgot == "no memory named 'plans-zq7'"
    assert _files(users / "bob") == []
    assert _files(users / "alice" / "memory") == ["INDEX.md", "plans-zq7.md"]  # untouched
    # And it shows up in alice's OWN prompt index (it never did before).
    assert alice.memory is not None and alice.memory.root == users / "alice" / "memory"
    alice.refresh_prompt()
    assert "plans-zq7" in alice.history[0]["content"]
    assert "plans-zq7" not in bob.history[0]["content"]


def test_single_user_memory_tools_are_unchanged(home, fake_llm):
    agent = _memory_agent(fake_llm([
        fake_llm.call(("remember", {"name": "pref", "content": "dark mode"})),
        fake_llm.text("ok"),
    ]), None)
    list(agent.chat("remember"))
    assert agent.user_root is None
    assert _files(home / "memory") == ["INDEX.md", "pref.md"]
    # Outside an agent the tools use the shared store too.
    assert "dark mode" in REGISTRY["recall"].call({"name": "pref"})


def test_memory_tools_use_the_agents_own_store(home, fake_llm, tmp_path):
    """An SDK caller's custom store (`memory_root`) is the one the tools write."""
    custom = tmp_path / "custom-memory"
    agent = build_agent(
        config=Config.load(), client=fake_llm([
            fake_llm.call(("remember", {"name": "n", "content": "c"})), fake_llm.text("ok"),
        ]), tool_categories=["memory"], memory_root=custom, enable_skills=False,
        enable_project=False, enable_hooks=False, enable_guardrails=False,
    )
    list(agent.chat("go"))
    assert _files(custom) == ["INDEX.md", "n.md"]
    assert _files(home / "memory") == []


def test_agents_built_inside_a_tool_call_work_for_the_same_user(home, fake_llm):
    """A subagent (or a workflow step) a user's tool builds inherits that user,
    so its memory index and tools are the user's, never the shared store."""
    alice_root = home / "users" / "alice"
    inner: dict = {}

    def spawn() -> str:
        inner["context"] = active_context()
        inner["agent"] = Agent(client=fake_llm([]), config=Config(), tools=[])
        inner["built"] = build_agent(
            config=Config(), client=fake_llm([]), tools=[], enable_memory=True,
            enable_skills=False, enable_project=False, enable_hooks=False,
            enable_guardrails=False,
        )
        return "spawned"

    parent = Agent(
        client=fake_llm([fake_llm.call(("spawn", {})), fake_llm.text("ok")]),
        config=Config(), tools=[Tool(name="spawn", description="d",
                                     parameters=_schema(), func=spawn)],
        user_root=alice_root,
    )
    list(parent.chat("go"))
    assert inner["context"].agent is parent
    assert inner["agent"].user_root == alice_root
    assert inner["built"].user_root == alice_root
    assert inner["built"].memory.root == alice_root / "memory"
    # Outside a tool call nothing is inherited.
    assert active_context() is None
    assert Agent(client=fake_llm([]), config=Config(), tools=[]).user_root is None


def _sse_events(body: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in body.splitlines()
            if line.startswith("data:") and line[5:].strip()]


def test_multi_user_web_remember_never_reaches_another_root(
    home, tmp_path, fake_llm, monkeypatch
):
    """The exploit, end to end: alice's remember() in her web session used to
    land in the shared store, where bob's recall/list_memories read it."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.sdk.builder as builder_mod
    import evi.sessions as sessions_mod

    for var in ("EVI_ALLOW_CROSS_ORIGIN", "EVI_ALLOW_ANY_HOST", "EVI_TRUSTED_PROXY"):
        monkeypatch.delenv(var, raising=False)
    (home / "config.toml").write_text(
        '[llm]\nmodel = "fake"\n'
        '[web]\nmulti_user = true\nauth_token = "owner-tok"\n'
        "[tools]\nskills = false\ntranscripts = false\nmcp = false\n",
        encoding="utf-8",
    )
    (home / "users.json").write_text(json.dumps([
        {"name": "alice", "token": "alice-tok"}, {"name": "bob", "token": "bob-tok"},
    ]), encoding="utf-8")
    monkeypatch.setattr("evi.users.USERS_PATH", home / "users.json")
    monkeypatch.setattr(server_mod, "HOME", home)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(sessions_mod, "find_session", lambda *_, **__: None)

    clients = {
        "alice": fake_llm([
            fake_llm.call(("remember", {"name": "plans-zq7", "content": "alice's plans"})),
            fake_llm.text("saved"),
        ]),
        "bob": fake_llm([
            fake_llm.call(("recall", {"name": "plans-zq7"}), ("list_memories", {})),
            fake_llm.text("nothing there"),
        ]),
    }
    real_build_agent = builder_mod.build_agent
    built: list[dict] = []

    def build(**kw):
        # The real assembly, with the user's scripted model and no guardrails
        # file from the machine running the tests.
        built.append(kw)
        user = Path(kw["user_root"]).name
        return real_build_agent(**{**kw, "client": clients[user], "enable_guardrails": False})

    monkeypatch.setattr(builder_mod, "build_agent", build)
    c = TestClient(server_mod.create_app(), base_url="http://127.0.0.1:8473")

    def chat(token: str, message: str) -> list[dict]:
        r = c.post("/api/chat", json={"session_id": "s1", "message": message},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        return [e for e in _sse_events(r.text) if e.get("kind") == "ToolResult"]

    saved = chat("alice-tok", "remember my plans")
    assert saved and saved[0]["name"] == "remember"
    users = home / "users"
    assert [Path(kw["user_root"]) for kw in built] == [users / "alice"]
    assert _files(users / "alice" / "memory") == ["INDEX.md", "plans-zq7.md"]
    assert _files(home / "memory") == []

    recall, listed = chat("bob-tok", "what did alice save?")
    assert [Path(kw["user_root"]) for kw in built] == [users / "alice", users / "bob"]
    assert recall["output"].startswith("ERROR: no memory named 'plans-zq7'")
    assert json.loads(listed["output"]) == []
    assert _files(users / "bob") == []
    assert _files(home / "memory") == []


# ---- 5. file checkpoints know who wrote them ----------------------------------


def test_checkpoints_record_session_turn_and_user(home, tmp_path, fake_llm):
    target = tmp_path / "work" / "out.txt"
    target.parent.mkdir()
    client = fake_llm([
        fake_llm.call(("write_file", {"path": str(target), "content": "v1"})),
        fake_llm.text("ok"),
    ])
    agent = Agent(client=client, config=Config(), tools=[REGISTRY["write_file"]],
                  session_id="sess-w", user_root=home / "users" / "alice")
    agent.auto_all = True
    list(agent.chat("write it"))
    assert target.read_text(encoding="utf-8") == "v1"
    [entry] = checkpoints.list_checkpoints(root=home)
    assert (entry["session"], entry["turn"], entry["user"]) == ("sess-w", 1, "alice")
    assert entry["op"] == "create"
    assert "root_session" not in entry  # a top-level agent's own write
    # A write made outside an agent records none of them.
    checkpoints.record_before_write(target, root=home)
    assert "session" not in checkpoints.list_checkpoints(root=home)[-1]


def _write_call(target: Path, content: str) -> tuple[str, dict]:
    return ("write_file", {"path": str(target), "content": content})


def _tags(home: Path) -> list[tuple]:
    return [(e["session"], e["turn"]) for e in checkpoints.list_checkpoints(limit=100, root=home)]


def test_a_revived_session_counts_turns_on_from_its_history(home, tmp_path, fake_llm):
    """The web revives a session from its transcript (server.get_session) and
    `evi sessions resume` hydrates one: its new turns used to start again at
    turn 1, so two different turns of one session shared a checkpoint tag."""
    from evi.sessions import find_session, history_from_transcript
    from evi.transcripts import TranscriptStore

    tr_root = tmp_path / "transcripts"
    work = tmp_path / "work"
    work.mkdir()

    def agent_for(script) -> Agent:
        ag = Agent(client=fake_llm(script), config=Config(), tools=[REGISTRY["write_file"]],
                   transcripts=TranscriptStore(root=tr_root), session_id="S1")
        ag.auto_all = True
        return ag

    def revive(ag: Agent) -> Agent:  # what server.get_session does
        restored = history_from_transcript(find_session("S1", root=tr_root))
        ag.history = [ag.history[0], *restored]
        return ag

    first = agent_for([
        fake_llm.call(_write_call(work / "a.txt", "1")), fake_llm.text("ok"),
        fake_llm.call(_write_call(work / "b.txt", "2")), fake_llm.text("ok"),
    ])
    list(first.chat("one"))
    list(first.chat("two"))
    revived = revive(agent_for([
        fake_llm.call(_write_call(work / "c.txt", "3")), fake_llm.text("ok"),
    ]))
    list(revived.chat("three"))
    assert _tags(home) == [("S1", 1), ("S1", 2), ("S1", 3)]
    assert revived.turn_index == 3

    # A re-roll on a revived session (continue_chat) redoes its last turn,
    # never "turn 0".
    rerolled = revive(agent_for([
        fake_llm.call(_write_call(work / "d.txt", "4")), fake_llm.text("ok"),
    ]))
    rerolled.rewind_to_last_user()
    list(rerolled.continue_chat())
    assert _tags(home)[-1] == ("S1", 3)


def test_a_nested_agents_writes_carry_the_top_level_session_and_turn(
    home, tmp_path, fake_llm
):
    """A subagent gets its own session id; its writes also record the session
    and turn of the top-level agent whose tool built it, so a per-session
    changes view can claim them."""
    work = tmp_path / "work"
    work.mkdir()
    built: dict = {}

    def spawn() -> str:
        child = Agent(
            client=fake_llm([fake_llm.call(_write_call(work / "child.txt", "c")),
                             fake_llm.text("ok")]),
            config=Config(), tools=[REGISTRY["write_file"]],
        )
        child.auto_all = True
        built["child"] = child
        list(child.chat("write it"))
        return "spawned"

    parent = Agent(
        client=fake_llm([
            fake_llm.text("hello"),
            fake_llm.call(("spawn", {})), fake_llm.text("ok"),
        ]),
        config=Config(), session_id="P", user_root=home / "users" / "alice",
        tools=[Tool(name="spawn", description="d", parameters=_schema(), func=spawn)],
    )
    list(parent.chat("hi"))
    list(parent.chat("delegate"))
    child = built["child"]
    assert child.session_id != "P"
    assert child.parent_context.agent is parent and child.parent_context.turn_index == 2
    [entry] = checkpoints.list_checkpoints(root=home)
    assert (entry["session"], entry["turn"]) == (child.session_id, 1)
    assert (entry["root_session"], entry["root_turn"]) == ("P", 2)
    assert entry["user"] == "alice"
    # `root` follows the chain to the top however deep it goes.
    top = ToolContext(session_id="top", turn_index=4)
    assert ToolContext(parent=ToolContext(parent=top)).root is top
    assert top.root is top


def test_parallel_writes_get_distinct_checkpoint_seqs(tmp_path):
    root = tmp_path / "home"
    files = [tmp_path / f"f{i}.txt" for i in range(8)]
    start = threading.Barrier(len(files))
    seqs: list[int] = []
    lock = threading.Lock()

    def write(p: Path) -> None:
        start.wait(timeout=30)
        seq = checkpoints.record_before_write(p, root=root)
        with lock:
            seqs.append(seq)

    threads = [threading.Thread(target=write, args=(p,)) for p in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(seqs) == list(range(1, len(files) + 1))
    assert [e["seq"] for e in checkpoints.list_checkpoints(limit=100, root=root)] == sorted(seqs)
    assert os.path.isfile(root / "checkpoints" / "journal.jsonl")
