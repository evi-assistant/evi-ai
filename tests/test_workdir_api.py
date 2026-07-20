"""Tests for the web working-folder endpoints (/api/session/cwd)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
import evi.config as config_mod  # noqa: E402
from evi.config import Config  # noqa: E402


class _FakeAgent:
    def __init__(self, *_, **__) -> None:
        self.config = Config()
        self.cwd = ""
        self.history: list = []
        self.project = None

    def _compose_system_prompt(self) -> str:
        return "sys"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    import evi.sdk.builder as builder_mod

    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    return TestClient(server_mod.create_app())


def test_cwd_get_defaults_to_process_cwd(client: TestClient) -> None:
    r = client.get("/api/session/cwd", params={"session_id": "s1"})
    assert r.status_code == 200
    assert r.json()["working_dir"]  # non-empty (process cwd)


def test_cwd_set_and_readback(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    r = client.post("/api/session/cwd", json={"session_id": "s2", "path": str(target)})
    assert r.status_code == 200
    assert r.json()["working_dir"] == str(target.resolve())
    r2 = client.get("/api/session/cwd", params={"session_id": "s2"})
    assert r2.json()["working_dir"] == str(target.resolve())


def test_cwd_set_rejects_nonexistent(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/api/session/cwd", json={"session_id": "s3", "path": str(tmp_path / "nope")}
    )
    assert r.status_code == 400


def test_cwd_is_per_session(client: TestClient, tmp_path: Path) -> None:
    """Setting one session's folder must not touch another's.

    The desktop UI shows the working folder as a chip; if the server leaked the
    value across sessions the chip would be right for the wrong reason.
    """
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()

    client.post("/api/session/cwd", json={"session_id": "sa", "path": str(a)})
    client.post("/api/session/cwd", json={"session_id": "sb", "path": str(b)})

    assert client.get("/api/session/cwd", params={"session_id": "sa"}).json()[
        "working_dir"
    ] == str(a.resolve())
    assert client.get("/api/session/cwd", params={"session_id": "sb"}).json()[
        "working_dir"
    ] == str(b.resolve())
    # A session that never set one still reports the process cwd, not a neighbour's.
    other = client.get("/api/session/cwd", params={"session_id": "sc"}).json()["working_dir"]
    assert other not in (str(a.resolve()), str(b.resolve()))


def test_ui_routes_every_session_switch_through_setSessionId() -> None:
    """The working-folder chip is per-session, so every place that changes the
    active session must refresh it.

    Five call sites assigned `sessionId` directly and none refreshed the chip,
    so after a tab switch it advertised the PREVIOUS session's folder — and a
    relative write then landed in the server's cwd instead. Pin the invariant
    rather than the five call sites, so a sixth cannot reintroduce it.
    """
    import re

    html = (
        Path(server_mod.__file__).parent / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "function setSessionId(" in html, "the session-switch helper is gone"

    bare = [
        (n, ln.strip())
        for n, ln in enumerate(html.splitlines(), 1)
        if re.match(r"^\s*sessionId = ", ln)
    ]
    # Exactly one legitimate assignment remains: the one inside setSessionId.
    # (The `let sessionId = …` declaration does not match this pattern.)
    assert len(bare) == 1, (
        "assign the active session via setSessionId() so the working-folder "
        f"chip is refreshed; found bare assignments at {bare}"
    )
    assert "loadCwd" in html.split("function setSessionId(")[1][:250], (
        "setSessionId must refresh the working-folder chip"
    )
