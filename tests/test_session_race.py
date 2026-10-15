"""Two first requests for one new session id must end up on ONE WebSession.

The page's load-time GET /api/session/cwd and its first /api/chat both ask
get_session for a tab's id, and building a session is slow. When both missed
the registry, both built one and the last store won: the chat's turn ran on an
orphan, so /steer reported the session idle and /history came back empty.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    """The web app on a tmp home, with build_agent replaced by `builds`, a
    list the test fills with its factory."""
    import evi.config as config_mod
    import evi.sdk.builder as builder_mod
    import evi.sessions as sessions_mod
    from evi import hooks as hooks_mod

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[llm]\nmodel = "fake"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", home / "hooks.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.setattr(sessions_mod, "find_session", lambda *a, **k: None)
    factory: list = []
    monkeypatch.setattr(builder_mod, "build_agent", lambda **kw: factory[0](**kw))
    return factory, TestClient(server_mod.create_app())


def _cwd(http: TestClient, sid: str) -> str:
    r = http.get("/api/session/cwd", params={"session_id": sid})
    assert r.status_code == 200, r.text
    return r.json()["working_dir"]


def test_concurrent_first_requests_share_one_session(app, tmp_path):
    factory, http = app
    # Both builds are held until the other has started, so both requests miss
    # the registry. Each agent gets its own folder, which is how a caller
    # tells which session it was handed. (A get_session that made the second
    # caller wait for the first build would only time the barrier out.)
    both_building = threading.Barrier(2, timeout=5)
    built: list[str] = []
    built_lock = threading.Lock()

    def build(**_kw):
        with built_lock:
            folder = tmp_path / f"agent{len(built)}"
            folder.mkdir()
            built.append(str(folder))
        try:
            both_building.wait()
        except threading.BrokenBarrierError:
            pass
        return SimpleNamespace(cwd=str(folder), history=[{"role": "system", "content": ""}],
                               hooks=None, permission_callback=None)

    factory.append(build)
    seen: list[str] = []
    errors: list[BaseException] = []

    def first_request() -> None:
        try:
            seen.append(_cwd(http, "newtab"))
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    callers = [threading.Thread(target=first_request) for _ in range(2)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(30)
    assert not errors, errors
    assert len(built) == 2, "both requests should have missed the registry"
    # One session won, and both callers got it...
    assert len(seen) == 2 and seen[0] == seen[1], seen
    assert seen[0] in built
    # ...and it is the one the registry keeps for every later request.
    assert _cwd(http, "newtab") == seen[0]


def test_a_known_session_is_never_rebuilt(app, tmp_path):
    factory, http = app
    calls: list[int] = []

    def build(**_kw):
        calls.append(1)
        return SimpleNamespace(cwd=str(tmp_path), history=[{"role": "system", "content": ""}],
                               hooks=None, permission_callback=None)

    factory.append(build)
    assert _cwd(http, "tab") == str(tmp_path)
    assert _cwd(http, "tab") == str(tmp_path)
    assert len(calls) == 1
