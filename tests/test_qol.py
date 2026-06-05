"""Tests for Phase 36 QoL bundle: doctor, read_file cache, permission
batching, suggest_title, and skills hot-reload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evi.config import Config
from evi.llm.agent import Agent


# ---- evi doctor ---------------------------------------------------------


def test_doctor_run_checks_shape() -> None:
    from evi.doctor import Check, run_checks, summarize

    checks = run_checks()
    assert checks and all(isinstance(c, Check) for c in checks)
    assert all(c.status in ("ok", "warn", "fail") for c in checks)
    ok, warn, fail = summarize(checks)
    assert ok + warn + fail == len(checks)


def test_doctor_reports_known_check_names() -> None:
    from evi.doctor import run_checks

    names = {c.name for c in run_checks()}
    assert any("config" in n for n in names)
    assert any("backend" in n for n in names)
    assert "hardware" in names


# ---- read_file caching --------------------------------------------------


def test_read_file_cache_hit_avoids_disk(tmp_path: Path, monkeypatch) -> None:
    from evi.tools import fs

    fs.clear_read_cache()
    f = tmp_path / "a.txt"
    f.write_text("hello world", encoding="utf-8")

    out1 = fs.read_file(str(f))
    assert out1.text == "hello world"

    # Second read should hit the cache — make a real disk read blow up to
    # prove we never touch it.
    def _boom(*a, **k):
        raise AssertionError("read_bytes should not be called on a cache hit")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    out2 = fs.read_file(str(f))
    assert out2.text == "hello world"
    assert out2 is out1  # same cached object


def test_read_file_cache_invalidated_on_change(tmp_path: Path) -> None:
    from evi.tools import fs

    fs.clear_read_cache()
    f = tmp_path / "b.txt"
    f.write_text("first", encoding="utf-8")
    assert fs.read_file(str(f)).text == "first"

    # Rewrite with different size → mtime/size change invalidates.
    f.write_text("second and longer", encoding="utf-8")
    assert fs.read_file(str(f)).text == "second and longer"


# ---- permission batching ------------------------------------------------


class _Delta:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content="", tool_calls=None, finish="stop"):
        self.delta = _Delta(content=content, tool_calls=tool_calls)
        self.finish_reason = finish


class _Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _TCDelta:
    def __init__(self, idx, call_id, name, args):
        self.index = idx
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": args})()


class _ScriptedClient:
    def __init__(self, *responses):
        self.calls: list[dict[str, Any]] = []
        self._scripted = list(responses)
        self.chat = type("X", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self._scripted:
            return iter(self._scripted.pop(0))
        return iter([_Chunk(choices=[_Choice(content="ok", finish="stop")])])


class _ShellTool:
    """Category 'shell' is NOT in the default auto_approve set → prompts."""

    def __init__(self, name):
        self.name = name
        self.description = "test"
        self.category = "shell"
        self.ran = False

    def openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def call_rich(self, args_json):
        from evi.citations import ToolOutput

        self.ran = True
        return ToolOutput(text=f"{self.name} ran")

    # Back-compat path used by _invoke_tool in some flows.
    def call(self, args_json):
        self.ran = True
        return f"{self.name} ran"


def _two_tool_turn():
    """A first round that requests two tool calls, then a final text round."""
    return (
        [
            _Chunk(choices=[_Choice(
                content="",
                tool_calls=[
                    _TCDelta(0, "c1", "tool_a", "{}"),
                    _TCDelta(1, "c2", "tool_b", "{}"),
                ],
                finish="tool_calls",
            )]),
        ],
        [_Chunk(choices=[_Choice(content="done", finish="stop")])],
    )


def test_permission_batch_called_once_for_multi_tool_turn() -> None:
    client = _ScriptedClient(*_two_tool_turn())
    a, b = _ShellTool("tool_a"), _ShellTool("tool_b")
    batch_calls: list[list[tuple]] = []

    def batch_cb(triples):
        batch_calls.append(list(triples))
        return [True, False]  # approve a, deny b

    agent = Agent(
        client=client,
        config=Config(),
        tools=[a, b],
        permission_batch_callback=batch_cb,
    )
    list(agent.chat("do two things"))

    assert len(batch_calls) == 1            # ONE prompt for both
    assert len(batch_calls[0]) == 2
    assert a.ran is True                    # approved
    assert b.ran is False                   # denied
    # The denied tool's result message is the permission-denied text.
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    denied = [m for m in tool_msgs if "PERMISSION DENIED" in m["content"]]
    assert len(denied) == 1


def test_single_tool_turn_uses_per_call_not_batch() -> None:
    one_call = (
        [
            _Chunk(choices=[_Choice(
                content="",
                tool_calls=[_TCDelta(0, "c1", "tool_a", "{}")],
                finish="tool_calls",
            )]),
        ],
        [_Chunk(choices=[_Choice(content="done", finish="stop")])],
    )
    client = _ScriptedClient(*one_call)
    a = _ShellTool("tool_a")
    batch_calls: list = []
    per_calls: list = []

    agent = Agent(
        client=client,
        config=Config(),
        tools=[a],
        permission_callback=lambda n, ar, c: (per_calls.append(n) or True),
        permission_batch_callback=lambda triples: (batch_calls.append(triples) or [True]),
    )
    list(agent.chat("do one thing"))

    assert per_calls == ["tool_a"]   # per-call path
    assert batch_calls == []         # batch NOT used for a single call
    assert a.ran is True


def test_pre_approved_category_never_prompts() -> None:
    """fs is in the default auto_approve set → no callback fires."""
    one_call = (
        [
            _Chunk(choices=[_Choice(
                content="",
                tool_calls=[_TCDelta(0, "c1", "fs_tool", "{}")],
                finish="tool_calls",
            )]),
        ],
        [_Chunk(choices=[_Choice(content="done", finish="stop")])],
    )
    client = _ScriptedClient(*one_call)
    t = _ShellTool("fs_tool")
    t.category = "fs"
    fired: list = []
    agent = Agent(
        client=client,
        config=Config(),
        tools=[t],
        permission_callback=lambda *a: (fired.append(a) or True),
        permission_batch_callback=lambda triples: (fired.append(triples) or [True]),
    )
    list(agent.chat("read a file"))
    assert fired == []
    assert t.ran is True


# ---- suggest_title ------------------------------------------------------


class _TitleClient:
    def __init__(self, content):
        self._content = content
        self.chat = type("X", (), {"completions": self})()

    def create(self, **kwargs):
        msg = type("M", (), {"content": self._content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


def test_suggest_title_cleans_output() -> None:
    client = _TitleClient('"Parsing JSON in Python"')
    agent = Agent(client=client, config=Config(), tools=[])
    agent.history.append({"role": "user", "content": "how do I parse json"})
    agent.history.append({"role": "assistant", "content": "use json.loads"})
    title = agent.suggest_title()
    assert title == "Parsing JSON in Python"  # quotes stripped, no trailing dot


def test_suggest_title_clamps_word_count() -> None:
    client = _TitleClient("one two three four five six seven eight")
    agent = Agent(client=client, config=Config(), tools=[])
    agent.history.append({"role": "user", "content": "x"})
    assert agent.suggest_title(max_words=6) == "one two three four five six"


def test_suggest_title_empty_history_returns_blank() -> None:
    client = _TitleClient("Whatever")
    agent = Agent(client=client, config=Config(), tools=[])  # only system msg
    assert agent.suggest_title() == ""


# ---- skills hot-reload --------------------------------------------------


def test_skillstore_rescans_on_each_list(tmp_path: Path) -> None:
    from evi.skills import SkillStore

    store = SkillStore(root=tmp_path)
    assert store.list() == []

    sk = tmp_path / "greet"
    sk.mkdir()
    (sk / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi\n---\nBody", encoding="utf-8"
    )
    names = [e.name for e in store.list()]
    assert "greet" in names


def test_compose_system_prompt_picks_up_new_skill(tmp_path: Path) -> None:
    from evi.skills import SkillStore

    store = SkillStore(root=tmp_path)
    agent = Agent(client=object(), config=Config(), tools=[], skills=store)
    assert "newskill" not in agent._compose_system_prompt()

    sk = tmp_path / "newskill"
    sk.mkdir()
    (sk / "SKILL.md").write_text(
        "---\nname: newskill\ndescription: does a thing\n---\nBody", encoding="utf-8"
    )
    # No restart, no re-instantiation — recomposing rescans disk.
    assert "newskill" in agent._compose_system_prompt()
