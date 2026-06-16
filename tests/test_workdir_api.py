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
