"""Agent loop tests with a fake OpenAI client.

We don't hit a real LLM — the fake client yields the chunks our agent
needs to exercise both the plain-text and tool-call paths.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterator

import pytest

from evi.config import Config
from evi.llm.agent import Agent, Done, TextDelta, ToolCall, ToolProgress, ToolResult
from evi.tools.base import Tool


# ---- fake OpenAI stream primitives --------------------------------------


@dataclass
class _FnDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ToolCallDelta:
    index: int
    id: str | None = None
    function: _FnDelta | None = None


@dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[_ToolCallDelta] | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Chunk:
    choices: list[_Choice]


class _FakeCompletions:
    def __init__(self, scripts: list[list[_Chunk]]) -> None:
        self._scripts = scripts
        self.calls = 0

    def create(self, **_: object) -> Iterator[_Chunk]:
        script = self._scripts[self.calls]
        self.calls += 1
        return iter(script)


class _FakeClient:
    def __init__(self, scripts: list[list[_Chunk]]) -> None:
        self.chat = type("C", (), {"completions": _FakeCompletions(scripts)})()


# ---- tests --------------------------------------------------------------


def _text_chunk(t: str, finish: str | None = None) -> _Chunk:
    return _Chunk([_Choice(_Delta(content=t), finish)])


def test_plain_text_response() -> None:
    script = [
        [
            _text_chunk("Hello "),
            _text_chunk("world", finish="stop"),
        ]
    ]
    agent = Agent(client=_FakeClient(script), config=Config(), tools=[])
    events = list(agent.chat("hi"))

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Hello world"
    assert any(isinstance(e, Done) and e.reason == "stop" for e in events)
    # history contains: system, user, assistant
    assert len(agent.history) == 3
    assert agent.history[-1] == {"role": "assistant", "content": "Hello world"}


def test_system_prompt_states_model_identity() -> None:
    # Without this, local models hallucinate "I'm GPT-4" from training data.
    cfg = Config()
    cfg.llm.backend = "ollama"  # a LOCAL open-weight backend -> the anti-hallucination branch
    cfg.llm.model = "qwen2.5-coder:14b-instruct"
    agent = Agent(client=_FakeClient([]), config=cfg, tools=[])
    sp = agent._compose_system_prompt()
    assert "qwen2.5-coder:14b-instruct" in sp
    low = sp.lower()
    assert "gpt-4" in low or "chatgpt" in low  # explicitly disclaims the hallucination
    assert "not" in low


def test_system_prompt_cli_agent_backend_is_honest() -> None:
    # claude_agent serves a PROPRIETARY model (Claude Opus) via the CLI login —
    # eVi must NOT claim it's a "local open-weight" model or deny being Claude.
    cfg = Config()
    cfg.llm.backend = "claude_agent"
    cfg.llm.model = "opus"
    agent = Agent(client=_FakeClient([]), config=cfg, tools=[])
    sp = agent._compose_system_prompt()
    assert "opus" in sp
    ident = [p for p in sp.split("\n\n") if "eVi" in p and "opus" in p][0]
    assert "running the local open-weight model `opus`" not in ident  # no false claim
    assert "You are NOT" not in ident                                 # doesn't deny being Claude


@pytest.mark.parametrize(
    "backend,model",
    [
        ("ollama", "qwen2.5-coder:14b-instruct-q4_K_M"),  # local branch
        ("claude_agent", "opus"),                          # proprietary branch
    ],
)
def test_identity_is_stated_only_when_asked(backend: str, model: str) -> None:
    """Both identity branches must tell the model not to volunteer it.

    A 14b model read "if the user asks, tell them X" as a standing order and
    prefixed every single reply with "I'm running the `opus` model…", including
    right after being told to stop. The "only when asked" half has to be
    explicit, not implied by the conditional.
    """
    cfg = Config()
    cfg.llm.backend = backend
    cfg.llm.model = model
    sp = Agent(client=_FakeClient([]), config=cfg, tools=[])._compose_system_prompt()
    low = sp.lower()
    assert "only say this when asked" in low
    assert "never volunteer it" in low
    assert "prefix a reply" in low


@pytest.mark.parametrize("backend,model", [("ollama", "qwen"), ("claude_agent", "opus")])
def test_identity_marks_pre_switch_history_as_stale(backend: str, model: str) -> None:
    """Backends are switchable mid-conversation, so earlier turns can name the
    old model. Without this the model imitates its own stale transcript."""
    cfg = Config()
    cfg.llm.backend = backend
    cfg.llm.model = model
    sp = Agent(client=_FakeClient([]), config=cfg, tools=[])._compose_system_prompt()
    assert "naming a different model" in sp
    assert "stale" in sp


def test_refresh_prompt_updates_identity_on_model_switch() -> None:
    # A mid-session model switch (picker / backend-use / /model) must re-stitch the
    # frozen system prompt, or the identity keeps naming the old model.
    cfg = Config()
    cfg.llm.model = "qwen2.5-coder:14b"
    agent = Agent(client=_FakeClient([]), config=cfg, tools=[])
    assert "qwen2.5-coder:14b" in agent.history[0]["content"]
    agent.config.llm.model = "deepseek-r1:14b"  # what the switch endpoints do
    agent.refresh_prompt()
    sysmsg = agent.history[0]["content"]
    assert "deepseek-r1:14b" in sysmsg
    assert "qwen2.5-coder:14b" not in sysmsg


def test_text_tool_call_json_not_shown_as_text() -> None:
    # qwen-style: the model prints the tool call as a JSON blob in `content`
    # instead of using structured tool_calls. eVi recovers it as a call and must
    # NOT leak the raw JSON into the visible transcript (the leading-JSON hold).
    blob = '{"name": "echo", "arguments": {"msg": "hi"}}'
    script = [
        [_text_chunk(blob, finish="stop")],    # turn 1: tool call emitted as text
        [_text_chunk("done", finish="stop")],  # turn 2: final answer
    ]

    def _echo(msg: str) -> str:
        return msg

    echo_tool = Tool(
        name="echo",
        description="echo the message back",
        parameters={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
        func=_echo,
    )

    agent = Agent(client=_FakeClient(script), config=Config(), tools=[echo_tool])
    events = list(agent.chat("hi"))

    shown = "".join(e.text for e in events if isinstance(e, TextDelta))
    # The raw JSON blob must never reach the UI as visible text.
    assert "{" not in shown
    assert '"name"' not in shown
    # It was recovered and dispatched as a real tool call.
    tcalls = [e for e in events if isinstance(e, ToolCall)]
    assert len(tcalls) == 1 and tcalls[0].name == "echo"
    # The genuine turn-2 answer still renders.
    assert "done" in shown


def test_tool_call_dispatch() -> None:
    # Turn 1: model emits a tool call. Turn 2: model emits final text.
    script = [
        [
            _Chunk(
                [
                    _Choice(
                        _Delta(
                            tool_calls=[
                                _ToolCallDelta(
                                    index=0,
                                    id="call_1",
                                    function=_FnDelta(name="add", arguments='{"a":2,"b":3}'),
                                )
                            ]
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            )
        ],
        [_text_chunk("answer is 5", finish="stop")],
    ]

    def _add(a: int, b: int) -> int:
        return a + b

    add_tool = Tool(
        name="add",
        description="add two ints",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        func=_add,
    )

    agent = Agent(client=_FakeClient(script), config=Config(), tools=[add_tool])
    events = list(agent.chat("what is 2 + 3?"))

    tcalls = [e for e in events if isinstance(e, ToolCall)]
    tresults = [e for e in events if isinstance(e, ToolResult)]
    assert len(tcalls) == 1 and tcalls[0].name == "add"
    assert tresults[0].output == "5"
    assert any(isinstance(e, Done) for e in events)

    # history: system, user, assistant(tool_calls), tool, assistant(final)
    roles = [m["role"] for m in agent.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


def test_memory_injected_into_system_prompt(tmp_path) -> None:
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)
    store.write("project", "eVi lives at C:/evi")

    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[],
        memory=store,
    )
    system = agent.history[0]["content"]
    assert "You are eVi" in system
    assert "Memory index" in system
    assert "project" in system
    assert "eVi lives at C:/evi" in system


def test_memory_absent_when_store_empty(tmp_path) -> None:
    from evi.memory import MemoryStore

    store = MemoryStore(root=tmp_path)  # empty
    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[],
        memory=store,
    )
    assert "Memory index" not in agent.history[0]["content"]


def test_project_context_injected_into_system_prompt(tmp_path) -> None:
    from evi.project import load_project_context

    (tmp_path / "EVI.md").write_text(
        "Project conventions: use snake_case.", encoding="utf-8"
    )
    ctx = load_project_context(start=tmp_path)
    assert ctx is not None

    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[],
        project=ctx,
    )
    system = agent.history[0]["content"]
    assert "Project context" in system
    assert "snake_case" in system


def test_goal_prepended_to_user_message(tmp_path) -> None:
    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(client=_FakeClient(script), config=Config(), tools=[])
    agent.set_goal("ship the refactor")
    list(agent.chat("look at file X"))
    # The composed user message ends up in history at index -2 (user) / -1 (assistant).
    user_msg = next(m for m in agent.history if m["role"] == "user")
    assert "ongoing goal: ship the refactor" in user_msg["content"]
    assert "look at file X" in user_msg["content"]


def test_clear_goal_removes_injection(tmp_path) -> None:
    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(client=_FakeClient(script), config=Config(), tools=[])
    agent.set_goal("temp goal")
    agent.clear_goal()
    list(agent.chat("hi"))
    user_msg = next(m for m in agent.history if m["role"] == "user")
    assert "ongoing goal" not in user_msg["content"]


def test_plan_mode_one_shot_disables_tools(tmp_path) -> None:
    """plan_mode_once must pass tools=None and clear itself afterward."""
    captured: dict = {}

    class _CapturingCompletions:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs):
            captured.setdefault("tools_seen", []).append(kwargs.get("tools"))
            self.calls += 1
            return iter([_text_chunk("plan", finish="stop")])

    client = type("C", (), {"chat": type("X", (), {"completions": _CapturingCompletions()})()})()

    # A real tool so we can assert it WOULD have been passed in a normal turn.
    real_tool = Tool(
        name="x",
        description="d",
        parameters={"type": "object", "properties": {}},
        func=lambda: "ok",
    )
    agent = Agent(client=client, config=Config(), tools=[real_tool])
    agent.enable_plan_mode()
    list(agent.chat("design Y"))
    list(agent.chat("now go"))
    # First turn: plan mode → tools=None. Second turn: back to normal → tools list.
    assert captured["tools_seen"][0] is None
    assert captured["tools_seen"][1] is not None
    # plan_mode_once should have auto-cleared.
    assert agent.plan_mode_once is False


def test_persistent_plan_mode_holds_across_turns(tmp_path) -> None:
    """agent.plan_mode (persistent) keeps tools=None every turn until cleared."""
    captured: dict = {}

    class _CapturingCompletions:
        def create(self, **kwargs):
            captured.setdefault("tools_seen", []).append(kwargs.get("tools"))
            return iter([_text_chunk("plan", finish="stop")])

    client = type("C", (), {"chat": type("X", (), {"completions": _CapturingCompletions()})()})()
    real_tool = Tool(
        name="x", description="d",
        parameters={"type": "object", "properties": {}}, func=lambda: "ok",
    )
    agent = Agent(client=client, config=Config(), tools=[real_tool])
    agent.plan_mode = True
    list(agent.chat("a"))
    list(agent.chat("b"))
    assert captured["tools_seen"] == [None, None]  # both turns read-only
    agent.plan_mode = False
    list(agent.chat("c"))
    assert captured["tools_seen"][2] is not None  # tools back after toggle off


def test_permission_callback_denies_tool() -> None:
    """When the callback returns False the tool result becomes a PERMISSION DENIED string."""
    # Turn 1: model emits a tool call. Turn 2: model emits final text.
    script = [
        [
            _Chunk([
                _Choice(
                    _Delta(tool_calls=[_ToolCallDelta(
                        index=0, id="c1",
                        function=_FnDelta(name="touch", arguments="{}"),
                    )]),
                    finish_reason="tool_calls",
                )
            ])
        ],
        [_text_chunk("ok then", finish="stop")],
    ]
    touch = Tool(
        name="touch",
        description="",
        parameters={"type": "object", "properties": {}},
        func=lambda: "ran",
        category="shell",
    )
    decisions: list[tuple[str, str, str]] = []

    def deny_all(name, args, category):
        decisions.append((name, args, category))
        return False

    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[touch],
        permission_callback=deny_all,
    )
    # `shell` category isn't in the default auto_approve list, so permission asked.
    events = list(agent.chat("do it"))
    results = [e for e in events if isinstance(e, ToolResult)]
    assert results[0].output.startswith("PERMISSION DENIED")
    assert decisions == [("touch", "{}", "shell")]


def test_permission_skipped_for_auto_approved_category() -> None:
    """fs is in the default auto_approve list — callback must NOT be called."""
    script = [
        [
            _Chunk([
                _Choice(
                    _Delta(tool_calls=[_ToolCallDelta(
                        index=0, id="c1",
                        function=_FnDelta(name="read", arguments="{}"),
                    )]),
                    finish_reason="tool_calls",
                )
            ])
        ],
        [_text_chunk("done", finish="stop")],
    ]
    read = Tool(
        name="read",
        description="",
        parameters={"type": "object", "properties": {}},
        func=lambda: "contents",
        category="fs",
    )

    def fail(*_):
        raise AssertionError("permission_callback should not have been called")

    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[read],
        permission_callback=fail,
    )
    events = list(agent.chat("read x"))
    results = [e for e in events if isinstance(e, ToolResult)]
    assert results[0].output == "contents"


def test_auto_all_bypasses_callback() -> None:
    """enable_auto_all() should auto-approve every category."""
    script = [
        [
            _Chunk([
                _Choice(
                    _Delta(tool_calls=[_ToolCallDelta(
                        index=0, id="c1",
                        function=_FnDelta(name="ex", arguments="{}"),
                    )]),
                    finish_reason="tool_calls",
                )
            ])
        ],
        [_text_chunk("done", finish="stop")],
    ]
    ex = Tool(
        name="ex",
        description="",
        parameters={"type": "object", "properties": {}},
        func=lambda: "ok",
        category="shell",
    )
    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[ex],
        permission_callback=lambda *_: (_ for _ in ()).throw(
            AssertionError("should not prompt")
        ),
    )
    agent.enable_auto_all()
    list(agent.chat("go"))


def test_chat_attaches_images_to_user_content_for_vlm(tmp_path) -> None:
    """When the model name looks vision-capable, attaching images should
    flip user content into the multipart shape."""
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG-data")

    captured: dict = {}

    class _CapturingCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return iter([_text_chunk("ok", finish="stop")])

    client = type("C", (), {"chat": type("X", (), {"completions": _CapturingCompletions()})()})()

    cfg = Config()
    cfg.llm.model = "qwen2.5-vl-7b"
    agent = Agent(client=client, config=cfg, tools=[])
    list(agent.chat("what's this?", images=[str(img)]))

    last_user = next(m for m in captured["messages"] if m["role"] == "user")
    assert isinstance(last_user["content"], list)
    types = [p["type"] for p in last_user["content"]]
    assert "text" in types and "image_url" in types


def test_chat_falls_back_when_model_not_vision(tmp_path) -> None:
    """A non-vision model gets the image paths as plain text instead of
    multipart content."""
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG-data")

    captured: dict = {}

    class _CapturingCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return iter([_text_chunk("ok", finish="stop")])

    client = type("C", (), {"chat": type("X", (), {"completions": _CapturingCompletions()})()})()
    cfg = Config()
    cfg.llm.model = "qwen2.5-7b-instruct"  # not a VLM
    agent = Agent(client=client, config=cfg, tools=[])
    list(agent.chat("what's this?", images=[str(img)]))

    last_user = next(m for m in captured["messages"] if m["role"] == "user")
    # Falls back to text — path mentioned, no multipart structure.
    assert isinstance(last_user["content"], str)
    assert str(img) in last_user["content"]
    assert "attached files" in last_user["content"]


def test_compact_history_collapses_middle(tmp_path) -> None:
    """compact_history should keep head + tail and summarise the middle."""

    class _OneShotChat:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            # Non-streaming response (compaction uses stream=False).
            return type("R", (), {
                "choices": [type("C", (), {"message": type("M", (), {"content": "SUMMARISED"})()})()]
            })()

    cfg = Config()
    cfg.llm.compact_keep_recent = 2
    client = type("X", (), {"chat": type("Y", (), {"completions": _OneShotChat()})()})()
    agent = Agent(client=client, config=cfg, tools=[])
    # Seed history past the keep_recent threshold (system + 5 messages).
    for i in range(5):
        agent.history.append({"role": "user", "content": f"msg {i}"})

    collapsed = agent.compact_history()
    assert collapsed == 3  # head + 3 middle + 2 tail = 6; middle = 3
    # head + summary + 2 tail = 4
    assert len(agent.history) == 4
    assert agent.history[0]["role"] == "system"
    assert agent.history[1]["content"].startswith("[compacted: 3 earlier messages")
    assert "SUMMARISED" in agent.history[1]["content"]
    # Tail preserved verbatim.
    assert agent.history[-1]["content"] == "msg 4"
    assert agent.history[-2]["content"] == "msg 3"


def test_parallel_tool_calls_preserve_order() -> None:
    """Two tool_calls in one turn should run concurrently but results must
    come back in the original order so tool_call_ids align."""
    import threading
    import time

    started = threading.Event()
    barrier = threading.Barrier(2)

    def slow_a() -> str:
        started.set()
        barrier.wait(timeout=2)
        return "A done"

    def slow_b() -> str:
        barrier.wait(timeout=2)
        return "B done"

    tool_a = Tool(
        name="slow_a", description="", parameters={"type": "object", "properties": {}},
        func=slow_a, category="fs",
    )
    tool_b = Tool(
        name="slow_b", description="", parameters={"type": "object", "properties": {}},
        func=slow_b, category="fs",
    )

    # Two tool_calls in the first assistant chunk, then a final-text turn.
    script = [
        [
            _Chunk([
                _Choice(
                    _Delta(tool_calls=[
                        _ToolCallDelta(index=0, id="c1",
                                       function=_FnDelta(name="slow_a", arguments="{}")),
                        _ToolCallDelta(index=1, id="c2",
                                       function=_FnDelta(name="slow_b", arguments="{}")),
                    ]),
                    finish_reason="tool_calls",
                )
            ])
        ],
        [_text_chunk("done", finish="stop")],
    ]

    t0 = time.time()
    agent = Agent(
        client=_FakeClient(script), config=Config(),
        tools=[tool_a, tool_b],
    )
    events = list(agent.chat("go"))
    elapsed = time.time() - t0

    results = [e for e in events if isinstance(e, ToolResult)]
    # Order preserved (slow_a first, then slow_b).
    assert [r.name for r in results] == ["slow_a", "slow_b"]
    assert results[0].output == "A done"
    assert results[1].output == "B done"
    # The barrier means each tool waits for the other; if they ran serially
    # they'd both time out at 2s. Concurrent execution finishes near-immediately.
    assert elapsed < 3.0


def test_token_usage_counts_chars() -> None:
    cfg = Config()
    cfg.llm.context_size = 1000
    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(client=_FakeClient(script), config=cfg, tools=[])
    agent.history.append({"role": "user", "content": "x" * 400})
    used, ceiling = agent.token_usage()
    # ~4 chars per token. Derive the expectation from the real history instead
    # of a magic ceiling: the old `used <= 250` was a de-facto system-prompt
    # size limit that sat ~2 tokens from saturation, so any legitimate prompt
    # edit failed a test that is actually about the char->token heuristic.
    chars = sum(len(str(m.get("content") or "")) for m in agent.history)
    assert ceiling == 1000
    assert used >= 100  # the 400-char message alone is ~100 tokens
    assert abs(used - chars / 4) <= max(20, chars * 0.1), (
        f"token_usage()={used} is not ~chars/4 for {chars} chars of history"
    )


def test_compact_history_no_op_when_short(tmp_path) -> None:
    class _Boom:
        def create(self, **_kwargs):
            raise AssertionError("should not have been called")

    cfg = Config()
    cfg.llm.compact_keep_recent = 10
    client = type("X", (), {"chat": type("Y", (), {"completions": _Boom()})()})()
    agent = Agent(client=client, config=cfg, tools=[])
    agent.history.append({"role": "user", "content": "hi"})
    # 1 (system) + 1 (user) = 2 messages; threshold is system + keep_recent = 11.
    assert agent.compact_history() == 0


def test_hook_veto_blocks_tool_call() -> None:
    """A before-hook with veto_on_nonzero=true returning non-zero must keep the
    tool from running and put a BLOCKED message in the result."""
    from evi.hooks import Hook, HookRegistry

    script = [
        [
            _Chunk([
                _Choice(
                    _Delta(tool_calls=[_ToolCallDelta(
                        index=0, id="c1",
                        function=_FnDelta(name="dangerous", arguments="{}"),
                    )]),
                    finish_reason="tool_calls",
                )
            ])
        ],
        [_text_chunk("understood", finish="stop")],
    ]
    invocations: list[None] = []

    def the_tool() -> str:
        invocations.append(None)
        return "ran"

    danger = Tool(
        name="dangerous",
        description="",
        parameters={"type": "object", "properties": {}},
        func=the_tool,
        category="fs",
    )
    blocker = Hook(
        name="no-way",
        event="before_tool_call",
        match="*",
        command=[sys.executable, "-c",
                 "import sys; sys.stderr.write('policy says no'); sys.exit(1)"],
        timeout=10,
        veto_on_nonzero=True,
    )

    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[danger],
        hooks=HookRegistry(hooks=[blocker]),
    )
    events = list(agent.chat("try it"))
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.output.startswith("BLOCKED BY HOOK")
    assert "policy says no" in result.output
    # Critically, the actual tool function never ran.
    assert invocations == []


def test_skills_injected_into_system_prompt(tmp_path) -> None:
    from evi.skills import SkillStore

    # Lay out a single skill so SkillStore picks it up.
    skill_dir = tmp_path / "summarize"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\n\nbody",
        encoding="utf-8",
    )
    store = SkillStore(root=tmp_path)

    script = [[_text_chunk("ok", finish="stop")]]
    agent = Agent(
        client=_FakeClient(script),
        config=Config(),
        tools=[],
        skills=store,
    )
    system = agent.history[0]["content"]
    assert "Available skills" in system
    assert "summarize" in system
    assert "invoke_skill" in system


def test_tool_progress_heartbeat(monkeypatch) -> None:
    """A slow tool emits ToolProgress heartbeats (Phase 60) — including an
    immediate elapsed=0 announce for tools flagged long — and still returns
    its result."""
    import time as _time

    import evi.llm.agent as agent_mod

    monkeypatch.setattr(agent_mod, "PROGRESS_INTERVAL", 0.05)

    script = [
        [
            _Chunk(
                [
                    _Choice(
                        _Delta(
                            tool_calls=[
                                _ToolCallDelta(
                                    index=0,
                                    id="call_1",
                                    function=_FnDelta(name="slow", arguments="{}"),
                                )
                            ]
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            )
        ],
        [_text_chunk("done", finish="stop")],
    ]

    def _slow() -> str:
        _time.sleep(0.2)
        return "ok"

    slow_tool = Tool(
        name="slow",
        description="a slow tool",
        parameters={"type": "object", "properties": {}, "required": []},
        func=_slow,
        long=True,
    )

    agent = Agent(client=_FakeClient(script), config=Config(), tools=[slow_tool])
    events = list(agent.chat("go"))

    progs = [e for e in events if isinstance(e, ToolProgress)]
    assert progs, "expected at least one ToolProgress heartbeat"
    assert all("slow" in p.names for p in progs)
    assert any(p.elapsed == 0.0 for p in progs)  # immediate long= announce
    assert any(p.elapsed > 0 for p in progs)  # plus interval heartbeats
    # the tool still produced its result
    assert any(isinstance(e, ToolResult) and e.output == "ok" for e in events)
