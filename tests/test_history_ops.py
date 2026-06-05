"""Tests for Agent's history-manipulation methods and the matching web
endpoints (truncate, edit, branch, reroll).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
import evi.config as config_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Agent, Done, Event, TextDelta  # noqa: E402


# ---- Agent unit tests ---------------------------------------------------


def _make_agent() -> Agent:
    class _Empty:
        def create(self, **_kwargs):
            return iter([])
    client = type("C", (), {"chat": type("X", (), {"completions": _Empty()})()})()
    return Agent(client=client, config=Config(), tools=[])


def test_truncate_drops_trailing_messages() -> None:
    agent = _make_agent()
    agent.history.extend([
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ])
    removed = agent.truncate_history(after_index=1)
    assert removed == 2
    assert len(agent.history) == 2  # system + first user
    assert agent.history[-1]["content"] == "one"


def test_truncate_protects_system_message() -> None:
    """Even after_index=0 keeps the system prompt at position 0."""
    agent = _make_agent()
    agent.history.append({"role": "user", "content": "x"})
    agent.truncate_history(after_index=-1)
    # System message survives.
    assert len(agent.history) == 1
    assert agent.history[0]["role"] == "system"


def test_edit_message_replaces_and_truncates() -> None:
    agent = _make_agent()
    agent.history.extend([
        {"role": "user", "content": "original"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "follow-up"},
    ])
    ok = agent.edit_message(at_index=1, new_content="edited")
    assert ok is True
    assert len(agent.history) == 2  # system + edited user
    assert agent.history[-1]["content"] == "edited"


def test_edit_refuses_system_index() -> None:
    agent = _make_agent()
    assert agent.edit_message(at_index=0, new_content="hack") is False
    assert agent.edit_message(at_index=99, new_content="oob") is False


def test_rewind_to_last_user_pops_trailing_assistant() -> None:
    agent = _make_agent()
    agent.history.extend([
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "another"},
        {"role": "assistant", "content": "answer 2"},
        {"role": "tool", "name": "x", "content": "result"},
    ])
    popped = agent.rewind_to_last_user()
    # Pops back through tool + assistant until tail is user.
    assert popped == 2
    assert agent.history[-1]["role"] == "user"
    assert agent.history[-1]["content"] == "another"


def test_rewind_when_no_assistant_is_noop() -> None:
    agent = _make_agent()
    agent.history.append({"role": "user", "content": "fresh"})
    assert agent.rewind_to_last_user() == 0


def test_refresh_config_re_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cfg_path = home / "config.toml"
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    cfg_path.write_text('[llm]\nmodel = "first"\n', encoding="utf-8")
    agent = _make_agent()
    agent.config = Config.load()
    assert agent.config.llm.model == "first"

    cfg_path.write_text('[llm]\nmodel = "second"\n', encoding="utf-8")
    agent.refresh_config()
    assert agent.config.llm.model == "second"


# ---- API endpoint tests -------------------------------------------------


class _FakeAgent:
    def __init__(self, *_, **__) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.auto_all = False
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None
        # The history endpoint reads this.
        self.history: list[dict] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
            {"role": "user", "content": "again"},
        ]

    def chat(self, *_args, **_kwargs) -> Iterator[Event]:
        yield TextDelta("ok")
        yield Done(reason="stop")

    def continue_chat(self, *_args, **_kwargs) -> Iterator[Event]:
        yield TextDelta("regenerated")
        yield Done(reason="stop")

    def reset(self): pass
    def set_goal(self, g): self.goal = g
    def clear_goal(self): self.goal = None
    def enable_plan_mode(self): self.plan_mode_once = True
    def enable_auto_all(self): self.auto_all = True
    def disable_auto_all(self): self.auto_all = False
    def token_usage(self): return (0, 0)
    def refresh_config(self): pass

    def truncate_history(self, after_index: int) -> int:
        before = len(self.history)
        self.history = self.history[: max(1, after_index + 1)]
        return before - len(self.history)

    def edit_message(self, at_index: int, new_content: str) -> bool:
        if at_index <= 0 or at_index >= len(self.history):
            return False
        msg = dict(self.history[at_index])
        msg["content"] = new_content
        self.history = self.history[:at_index] + [msg]
        return True

    def rewind_to_last_user(self) -> int:
        popped = 0
        while len(self.history) > 1 and self.history[-1].get("role") != "user":
            self.history.pop()
            popped += 1
        return popped


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(server_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "get_enabled_tools", lambda _: [])
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    return TestClient(server_mod.create_app())


def test_history_endpoint_returns_messages(client: TestClient) -> None:
    # Open a session first.
    client.post("/api/chat", json={"session_id": "h1", "message": "/help"})
    r = client.get("/api/session/h1/history")
    assert r.status_code == 200
    data = r.json()
    msgs = data["messages"]
    assert any(m["role"] == "user" and m["content"] == "hello" for m in msgs)


def test_truncate_endpoint(client: TestClient) -> None:
    client.post("/api/chat", json={"session_id": "t1", "message": "/help"})
    r = client.post(
        "/api/session/t1/truncate", json={"after_index": 1}
    )
    assert r.status_code == 200
    assert r.json()["length"] == 2


def test_edit_endpoint(client: TestClient) -> None:
    client.post("/api/chat", json={"session_id": "e1", "message": "/help"})
    r = client.post(
        "/api/session/e1/edit",
        json={"at_index": 1, "content": "new wording"},
    )
    assert r.status_code == 200
    # Verify the edit landed by reading history back.
    h = client.get("/api/session/e1/history").json()["messages"]
    assert h[1]["content"] == "new wording"
    assert len(h) == 2  # system + edited user (rest truncated)


def test_edit_rejects_system_index(client: TestClient) -> None:
    client.post("/api/chat", json={"session_id": "e2", "message": "/help"})
    r = client.post(
        "/api/session/e2/edit",
        json={"at_index": 0, "content": "nope"},
    )
    assert r.status_code == 400


def test_branch_creates_new_session_with_history_prefix(client: TestClient) -> None:
    client.post("/api/chat", json={"session_id": "b1", "message": "/help"})
    r = client.post("/api/session/b1/branch", json={"at_index": 2})
    assert r.status_code == 200
    body = r.json()
    new_id = body["new_session_id"]
    # The branched session has history up through at_index inclusive.
    h = client.get(f"/api/session/{new_id}/history").json()["messages"]
    assert len(h) == 3  # system + user + assistant


def test_truncate_missing_session_404(client: TestClient) -> None:
    r = client.post("/api/session/ghost/truncate", json={"after_index": 0})
    assert r.status_code == 404
