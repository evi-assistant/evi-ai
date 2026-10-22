"""Web request hardening (Phase 0, fix-web-request-hardening).

Exploit regressions for the verified findings, each driven through the real
FastAPI app:

* No-token mode answers only loopback clients: a LAN client that forges
  `Host: localhost` used to get the whole API (bind_lan, --host 0.0.0.0, Docker),
  and a forwarded request (X-Forwarded-For & co.) never counts as loopback. The
  launch token alone gates only other machines.
* A browser's state-changing request needs `X-Evi-Client` (or a token); the
  old VSIX (Node fetch: `Sec-Fetch-Mode` only) / CLI / peers keep working. The
  side-effecting GET /api/backend/pull needs it from every caller: an <img>,
  <link>, <iframe> or link on eVi's own page can't start a model download.
* One turn per session: a second /api/chat (or a history-rewriting slash
  command, reset, truncate, edit, re-roll) gets 409 while one runs.
* A permission prompt denies at its deadline; a dropped SSE stream does not
  deny, and the answer may come from another of the caller's tabs. A channel
  `run: true` on a used session no longer waits on a dead stream's callback.
* Multi-user: a users.json login gets 403 on host-level (owner-only) routes,
  checkpoints and rewind included; logins whose names map to one data folder
  ('bob smith' / 'bob_smith', 'Alice' / 'alice') don't sign in at all.

Concurrency is exercised against a real uvicorn server in a thread, because
TestClient runs one streamed response to completion before the next request
(see the note in test_web.py). Every path is a tmp dir; nothing touches ~/.evi.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Done, Event, TextDelta, ToolResult  # noqa: E402

LOCAL = "http://127.0.0.1:8473"
UI = {"X-Evi-Client": "web"}  # what index.html's fetch wrapper adds


class _GateAgent:
    """A turn that asks permission for one tool (unless the message says
    "no-tool") and reports the answer in its reply."""

    def __init__(self) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.auto_all = False
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None
        self.history: list[dict] = [{"role": "system", "content": "sys"}]
        self.decisions: list[bool] = []
        self.resets = 0

    def chat(self, message: str, images=None, **_kw) -> Iterator[Event]:
        self.history.append({"role": "user", "content": message})
        yield TextDelta(text="working ")
        if "no-tool" not in message:
            approved = bool(self.permission_callback("shell", '{"cmd":"x"}', "shell"))
            self.decisions.append(approved)
            yield ToolResult(name="shell", output="ran" if approved else "PERMISSION DENIED")
            yield TextDelta(text="approved" if approved else "denied")
        self.history.append({"role": "assistant", "content": "done"})
        yield Done(reason="stop")

    def continue_chat(self) -> Iterator[Event]:
        yield Done(reason="stop")

    def rewind_to_last_user(self) -> int:
        return 0

    def truncate_history(self, after_index: int) -> int:
        return 0

    def edit_message(self, at_index: int, content: str) -> bool:
        return True

    def reset(self) -> None:
        self.resets += 1
        self.history = self.history[:1]

    def steer(self, text: str) -> bool:
        return True

    def pending_steering(self) -> int:
        return 0

    def take_pending_steering(self) -> list[str]:
        return []

    def token_usage(self) -> tuple[int, int]:
        return (0, 0)


def _make_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    token: str = "",
    launch_token: str = "",
    multi_user: bool = False,
    users: list[dict] | None = None,
    agents: list[_GateAgent] | None = None,
    configure=None,
    real_config: bool = False,
):
    """`real_config` keeps the real Config.load (the tmp home's config.toml,
    re-read on every request) instead of a fixed one built from the other
    arguments."""
    import evi.config as config_mod
    import evi.sdk.builder as builder_mod
    import evi.sessions as sessions_mod

    for var in ("EVI_ALLOW_CROSS_ORIGIN", "EVI_ALLOW_ANY_HOST", "EVI_TRUSTED_PROXY",
                "EVI_DECISION_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # Everything that could persist lands in the tmp home.
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "HOME", home)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", launch_token)
    monkeypatch.setattr("evi.users.USERS_PATH", home / "users.json")
    if users is not None:
        (home / "users.json").write_text(json.dumps(users), encoding="utf-8")
    monkeypatch.setattr(sessions_mod, "find_session", lambda *_, **__: None)

    made = agents if agents is not None else []

    def build(*_a, **_k):
        agent = _GateAgent()
        made.append(agent)
        return agent

    monkeypatch.setattr(builder_mod, "build_agent", build)

    def fake_load(cls=Config):
        cfg = Config()
        cfg.web.auth_token = token
        cfg.web.multi_user = multi_user
        if configure is not None:
            configure(cfg)
        return cfg

    if not real_config:
        monkeypatch.setattr(server_mod.Config, "load", classmethod(lambda cls: fake_load()))
    return server_mod.create_app()


def _events(body: str) -> list[dict]:
    out: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data:") and line[5:].strip():
            out.append(json.loads(line[5:].strip()))
    return out


def _chat(c, sid: str, message: str, headers: dict | None = None):
    return c.post("/api/chat", json={"session_id": sid, "message": message},
                  headers=headers or {})


# ---- no-token mode: loopback clients only ----------------------------------


@pytest.mark.parametrize("path,method", [
    ("/api/chat", "POST"),
    ("/api/config", "GET"),
    ("/api/health", "GET"),
    ("/", "GET"),
])
def test_lan_client_forging_host_localhost_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str, method: str
) -> None:
    """The exploit: `curl -H 'Host: localhost' http://<lan-ip>:8473/api/chat`."""
    agents: list[_GateAgent] = []
    app = _make_app(monkeypatch, tmp_path, agents=agents)
    lan = TestClient(app, base_url="http://localhost:8473", client=("192.168.1.50", 50123))
    kw = {"json": {"session_id": "s1", "message": "no-tool"}} if method == "POST" else {}
    r = lan.request(method, path, headers={"Host": "localhost:8473"}, **kw)
    assert r.status_code == 403, r.text
    assert "another machine" in r.json()["detail"]
    assert agents == []  # no session, no turn


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "127.0.0.9"])
def test_loopback_clients_are_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client_host: str
) -> None:
    app = _make_app(monkeypatch, tmp_path)
    c = TestClient(app, base_url=LOCAL, client=(client_host, 50123))
    assert _chat(c, "s1", "no-tool").status_code == 200


def test_lan_client_with_a_token_is_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _make_app(monkeypatch, tmp_path, token="s3cret")
    lan = TestClient(app, base_url="http://192.168.1.10:8473", client=("192.168.1.50", 50123))
    assert _chat(lan, "s1", "no-tool").status_code == 401
    r = _chat(lan, "s1", "no-tool", {"Authorization": "Bearer s3cret"})
    assert r.status_code == 200, r.text


def test_launch_token_gates_like_the_configured_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The desktop shell's generated LAN token (EVI_WEB_AUTH_TOKEN): no config
    token needed, LAN clients must present it, the UI's auth check knows it."""
    app = _make_app(monkeypatch, tmp_path, launch_token="launch-tok")
    lan = TestClient(app, base_url="http://192.168.1.10:8473", client=("192.168.1.50", 50123))
    assert _chat(lan, "s1", "no-tool").status_code == 401
    assert _chat(lan, "s1", "no-tool", {"Authorization": "Bearer launch-tok"}).status_code == 200
    assert lan.get("/api/auth/check").json() == {"ok": False, "required": True}
    ok = lan.get("/api/auth/check", headers={"Authorization": "Bearer launch-tok"}).json()
    assert ok == {"ok": True, "required": True}
    # Federation's LAN fail-safe counts it as "a token is set".
    req = SimpleNamespace(client=SimpleNamespace(host="192.168.1.50"))
    server_mod._federation_lan_guard(req, Config())


NODE_FETCH = {  # exactly what Node's fetch (undici) sends: the VS Code extension host
    "accept": "*/*", "accept-language": "*", "sec-fetch-mode": "cors", "user-agent": "node",
}


def test_launch_token_spares_this_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With only the desktop's LAN token, an old VSIX on loopback (no token,
    no header) keeps working and the UI needs no sign-in; this machine still
    gets the no-token checks, and a proxied request is not this machine."""
    app = _make_app(monkeypatch, tmp_path, launch_token="launch-tok")
    local = TestClient(app, base_url=LOCAL)
    assert _chat(local, "s1", "no-tool", NODE_FETCH).status_code == 200
    assert local.post("/api/reset", json={"session_id": "s1", "message": ""},
                      headers=NODE_FETCH).status_code == 200
    assert local.get("/api/auth/check").json() == {"ok": True, "required": False}
    # A stale token from this machine doesn't lock it out either.
    assert _chat(local, "s1", "no-tool", {"Authorization": "Bearer old"}).status_code == 200
    # Still the no-token rules: a browser needs the header, the Host must be local.
    r = _chat(local, "s1", "no-tool", {"Origin": LOCAL})
    assert r.status_code == 403 and "X-Evi-Client" in r.json()["detail"]
    assert _chat(local, "s1", "no-tool", {"Host": "evil.example"}).status_code == 403
    # Relayed by a proxy: someone else's request, so it needs the token.
    assert _chat(local, "s1", "no-tool", {"X-Forwarded-For": "203.0.113.7"}).status_code == 401
    # `[web] auth_token` or multi-user logins gate this machine too, as before.
    for kw in ({"token": "cfg-tok"},
               {"multi_user": True, "users": [{"name": "alice", "token": "a-tok"}]}):
        c = TestClient(_make_app(monkeypatch, tmp_path, launch_token="launch-tok", **kw),
                       base_url=LOCAL)
        assert _chat(c, "s1", "no-tool").status_code == 401, kw
        assert c.get("/api/auth/check").json()["required"] is True


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
def test_forwarded_requests_are_not_this_machine(local: TestClient, header: str) -> None:
    """A same-machine proxy relays the LAN into loopback; without a token its
    requests are refused like any other machine's."""
    value = "for=203.0.113.7" if header == "Forwarded" else "203.0.113.7"
    r = _chat(local, "s1", "no-tool", {header: value})
    assert r.status_code == 403 and "another machine" in r.json()["detail"]
    assert local.get("/api/health", headers={header: value}).status_code == 403


def test_forwarded_for_cannot_make_a_lan_client_look_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uvicorn rewrites the peer from X-Forwarded-For when it trusts the
    sender (FORWARDED_ALLOW_IPS=*): a LAN client claiming 127.0.0.1 is refused."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    agents: list[_GateAgent] = []
    app = ProxyHeadersMiddleware(_make_app(monkeypatch, tmp_path, agents=agents),
                                 trusted_hosts="*")
    lan = TestClient(app, base_url=LOCAL, client=("192.168.1.50", 50123))
    assert _chat(lan, "s1", "no-tool").status_code == 403
    r = _chat(lan, "s1", "no-tool", {"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 403, r.text
    assert agents == []


def test_container_port_mapping_needs_a_token_and_rotate_fixes_a_running_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The image runs `evi web --host 0.0.0.0` with no token, and a published
    port arrives from the Docker bridge, so even the host's own browser is
    refused. docs/security.md (Containers): on a running stack,
    `docker compose exec evi evi web-config token rotate` fixes it with no
    restart, because the server re-reads config.toml on every request."""
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    monkeypatch.delenv("EVI_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)  # no project .evi.toml in play
    agents: list[_GateAgent] = []
    app = _make_app(monkeypatch, tmp_path, agents=agents, real_config=True)
    bridge = TestClient(app, base_url="http://localhost:8000", client=("172.17.0.1", 50123))
    for path in ("/", "/api/health", "/api/config"):
        r = bridge.get(path, headers={"Host": "localhost:8000"})
        assert r.status_code == 403 and "token" in r.json()["detail"], (path, r.text)

    out = CliRunner().invoke(cli_main.app, ["web-config", "token", "rotate"])
    assert out.exit_code == 0, out.output
    token = Config.load().web.auth_token
    assert len(token) >= 32 and token in out.output

    # Same app object, no restart: the bridge client now needs only the token.
    assert bridge.get("/api/health").status_code == 200
    assert bridge.get("/api/config").status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    assert bridge.get("/api/config", headers=auth).status_code == 200
    assert _chat(bridge, "s1", "no-tool", auth).status_code == 200
    assert len(agents) == 1


def test_settings_report_the_access_posture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Settings → Server shows whether a token gates the server, the launch
    token included (it never appears in config.toml)."""
    c = TestClient(_make_app(monkeypatch, tmp_path), base_url=LOCAL)
    assert c.get("/api/config").json()["access"] == {
        "token": False, "launch_token": False, "logins": False}
    c = TestClient(_make_app(monkeypatch, tmp_path, launch_token="lt"), base_url=LOCAL)
    r = c.get("/api/config", headers={"Authorization": "Bearer lt"}).json()
    assert r["access"] == {"token": True, "launch_token": True, "logins": False}
    assert r["web"]["auth_token"] == ""  # still nothing in config


def test_launch_token_is_taken_out_of_the_environment(tmp_path: Path) -> None:
    """Read once at import and removed, so the agent's own subprocesses (shell
    tools, MCP servers) never inherit the token."""
    import os

    code = (
        "import os, evi.apps.web.server as s;"
        "print(s._LAUNCH_TOKEN, 'EVI_WEB_AUTH_TOKEN' in os.environ)"
    )
    env = {**os.environ, "EVI_HOME": str(tmp_path), "EVI_WEB_AUTH_TOKEN": " tok123 "}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=120, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["tok123", "False"]


@pytest.mark.parametrize("host,refused", [
    ("127.0.0.1", False), ("::1", False), ("[::1]", False), ("localhost", False),
    ("0.0.0.0", True), ("", True), ("192.168.1.5", True), ("::", True), ("myhost.lan", True),
])
def test_lan_bind_refusal(monkeypatch: pytest.MonkeyPatch, host: str, refused: bool) -> None:
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "")
    msg = server_mod.lan_bind_refusal(host, Config())
    assert (msg is not None) == refused, msg
    if refused:
        assert "token" in msg
    with_token = Config()
    with_token.web.auth_token = "t"
    assert server_mod.lan_bind_refusal(host, with_token) is None
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "launch")
    assert server_mod.lan_bind_refusal(host, Config()) is None


# ---- the X-Evi-Client header for browser requests --------------------------


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    return TestClient(_make_app(monkeypatch, tmp_path), base_url=LOCAL)


@pytest.mark.parametrize("browser", [
    {"Origin": LOCAL},                                       # fetch() from our page
    {"Origin": LOCAL, "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},  # no Origin, still a browser
    {"Origin": "tauri://localhost"},                         # the desktop shell's page
])
def test_browser_post_without_client_header_is_refused(
    local: TestClient, browser: dict
) -> None:
    r = _chat(local, "s1", "no-tool", browser)
    assert r.status_code == 403, r.text
    assert "X-Evi-Client" in r.json()["detail"]
    assert _chat(local, "s1", "no-tool", {**browser, **UI}).status_code == 200


def test_browser_upload_without_client_header_writes_nothing(
    local: TestClient, tmp_path: Path
) -> None:
    r = local.post("/api/upload", data={"session_id": "s1"},
                   files={"file": ("x.txt", b"hi", "text/plain")}, headers={"Origin": LOCAL})
    assert r.status_code == 403
    assert not (tmp_path / "uploads").exists()


def test_non_browser_clients_need_no_header(local: TestClient) -> None:
    """The CLI, curl, peers: no Origin, no Sec-Fetch-*."""
    assert _chat(local, "s1", "no-tool").status_code == 200
    assert local.post("/api/reset", json={"session_id": "s1", "message": ""}).status_code == 200


def test_old_vsix_node_fetch_is_not_a_browser(local: TestClient) -> None:
    """An old VSIX build posts with Node's fetch, which adds
    `Sec-Fetch-Mode: cors` (and no Origin or Sec-Fetch-Site): still served."""
    assert _chat(local, "s1", "no-tool", NODE_FETCH).status_code == 200
    r = local.post("/api/reset", json={"session_id": "s1", "message": ""}, headers=NODE_FETCH)
    assert r.status_code == 200, r.text
    # `Sec-Fetch-Site: none` is a user-started request, never a page's.
    assert _chat(local, "s1", "no-tool", {**NODE_FETCH, "Sec-Fetch-Site": "none"}).status_code == 200
    # Any page's request names its site: that is a browser.
    r = _chat(local, "s1", "no-tool", {**NODE_FETCH, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403 and "X-Evi-Client" in r.json()["detail"]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_side_effect_get_from_another_site_is_refused_even_cross_origin_allowed(
    local: TestClient, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """EVI_ALLOW_CROSS_ORIGIN waives only the Origin check, so it must not let
    another site's <img src=/api/backend/pull?model=huge> start a download."""
    from evi.backends.ollama import OllamaBackend

    pulls: list[str] = []
    monkeypatch.setattr(OllamaBackend, "pull_model", lambda self, m: pulls.append(m) or iter(()))
    monkeypatch.setenv("EVI_ALLOW_CROSS_ORIGIN", "1")
    r = local.get("/api/backend/pull?model=huge-70b", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403, r.text
    # ...nor with the header (a cross-site fetch can't send it without a
    # preflight eVi never answers; refused here all the same).
    r = local.get("/api/backend/pull?model=huge-70b", headers={"Sec-Fetch-Site": site, **UI})
    assert r.status_code == 403, r.text
    assert pulls == []
    # The UI's own fetch (same-origin, with the header) still streams.
    r = local.get("/api/backend/pull?model=tiny",
                  headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", **UI})
    assert r.status_code == 200 and '"done"' in r.text
    assert pulls == ["tiny"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("headers", [
    # <img>/<link>/<iframe> on eVi's own page (markup past the sanitizer, a
    # diagram's resource): no Origin, Sec-Fetch-Site same-origin.
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"},
    # An EventSource on eVi's own page (CORS mode, Origin, no custom header).
    {"Origin": LOCAL, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},
    # A link opened in the system browser (the desktop sends links there).
    {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"},
    # An older browser that sends no Sec-Fetch-* at all — and curl.
    {},
])
def test_model_pull_needs_the_client_header_from_everyone(
    local: TestClient, monkeypatch: pytest.MonkeyPatch, headers: dict, method: str
) -> None:
    """GET /api/backend/pull starts a download, and a GET is what any
    subresource or link sends: no same-origin <img>, <link>, <iframe> or
    EventSource — nor any caller without the header — may start one."""
    from evi.backends.ollama import OllamaBackend

    pulls: list[str] = []
    monkeypatch.setattr(OllamaBackend, "pull_model", lambda self, m: pulls.append(m) or iter(()))
    r = local.request(method, "/api/backend/pull?model=huge-70b", headers=headers)
    assert r.status_code == 403, r.text
    assert pulls == []
    if method == "GET":
        assert "X-Evi-Client" in r.json()["detail"]
        r = local.get("/api/backend/pull?model=tiny", headers={**headers, **UI})
        assert r.status_code == 200 and '"done"' in r.text
        assert pulls == ["tiny"]


def test_model_pull_with_the_token_needs_no_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A page can't know the token, so a token-authenticated pull (header or
    ?token=) passes as before; without either it's a 401."""
    from evi.backends.ollama import OllamaBackend

    pulls: list[str] = []
    monkeypatch.setattr(OllamaBackend, "pull_model", lambda self, m: pulls.append(m) or iter(()))
    c = TestClient(_make_app(monkeypatch, tmp_path, token="s3cret"), base_url=LOCAL)
    img = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "no-cors"}
    assert c.get("/api/backend/pull?model=a", headers=img).status_code == 401
    r = c.get("/api/backend/pull?model=b", headers={**img, "Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and '"done"' in r.text
    assert pulls == ["b"]


def test_reads_need_no_header(local: TestClient) -> None:
    assert local.get("/api/health", headers={"Origin": LOCAL}).status_code == 200
    assert local.get("/api/dispatch", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_token_authenticated_browser_requests_skip_the_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = TestClient(_make_app(monkeypatch, tmp_path, token="s3cret"), base_url=LOCAL)
    auth = {"Authorization": "Bearer s3cret", "Origin": LOCAL}
    assert _chat(c, "s1", "no-tool", auth).status_code == 200
    assert _chat(c, "s1", "no-tool", {"Origin": LOCAL}).status_code == 401


def test_cross_site_refusal_still_comes_first(local: TestClient) -> None:
    r = _chat(local, "s1", "no-tool", {"Origin": "https://evil.example", **UI})
    assert r.status_code == 403 and "cross-site" in r.json()["detail"]


def test_routine_webhooks_are_not_header_gated(local: TestClient) -> None:
    # The path token is the credential: refused as unknown, not for the header.
    r = local.post("/api/routine/nope", headers={"Origin": LOCAL})
    assert r.status_code == 404


# ---- session ids, uploads -------------------------------------------------


@pytest.mark.parametrize("bad", ["bad.id", "con", "a%20b", "x" * 97])
def test_session_id_in_any_route_path_is_validated(local: TestClient, bad: str) -> None:
    for path in (f"/api/session/{bad}/usage", f"/api/session/{bad}/channel"):
        assert local.get(path).status_code == 400, path
    assert local.post(f"/api/session/{bad}/steer", json={"text": "x"}).status_code == 400
    assert local.get("/api/session/good-id_1/usage").status_code == 200


def test_session_id_in_bodies_is_validated(local: TestClient) -> None:
    bad = "../x"
    r = local.post("/api/decide", json={"session_id": bad, "decision_id": "d", "approved": True})
    assert r.status_code == 400
    assert local.post("/api/reset", json={"session_id": bad, "message": ""}).status_code == 400


@pytest.mark.parametrize("route", ["/api/transcribe", "/api/upload"])
def test_uploads_are_size_capped(
    local: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    import evi.voice as voice

    monkeypatch.setattr(server_mod, "_UPLOAD_MAX_BYTES", 1024)
    monkeypatch.setattr(voice, "transcribe_wav", lambda *_a, **_k: "hello")
    r = local.post(route, data={"session_id": "s1"},
                   files={"file": ("a.wav", b"x" * 2048, "audio/wav")})
    assert r.status_code == 413, r.text
    assert not (tmp_path / "uploads" / "s1" / "a.wav").exists()
    r = local.post(route, data={"session_id": "s1"},
                   files={"file": ("a.wav", b"x" * 512, "audio/wav")})
    assert r.status_code == 200, r.text


# ---- busy guard and decision deadlines (real server) ---------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def serve():
    """Start an app on a real uvicorn server in a thread; returns its base URL."""
    import uvicorn

    running: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start(app) -> str:
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="error", lifespan="off"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started, "uvicorn did not start"
        running.append((server, th))
        return f"http://127.0.0.1:{port}"

    yield start
    for server, th in running:
        server.should_exit = True
        th.join(10)


def _read_until(lines: Iterator[str], kind: str) -> dict:
    for line in lines:
        if line.startswith("data:") and line[5:].strip():
            ev = json.loads(line[5:].strip())
            if ev["kind"] == kind:
                return ev
    raise AssertionError(f"stream ended before {kind}")


def _session(c: httpx.Client, sid: str) -> dict:
    snap = c.get("/api/dispatch").json()
    return next(s for s in snap["sessions"] if s["id"] == sid)


def _wait(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_second_chat_on_a_busy_session_is_409_and_the_first_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve
) -> None:
    agents: list[_GateAgent] = []
    base = serve(_make_app(monkeypatch, tmp_path, agents=agents))
    with httpx.Client(base_url=base, timeout=20) as c, \
            httpx.Client(base_url=base, timeout=20) as c2:
        with c.stream("POST", "/api/chat", json={"session_id": "b1", "message": "go"}) as first:
            lines = first.iter_lines()
            req = _read_until(lines, "PermissionRequest")
            assert req["timeout_s"] == 300.0  # the default deadline, told to the client

            # The turn is waiting on a human: the session is busy.
            assert _session(c2, "b1")["busy"] is True
            second = _chat(c2, "b1", "again")
            assert second.status_code == 409
            assert "/api/session/b1/steer" in second.json()["detail"]
            # History-rewriting commands and routes wait for the turn...
            for cmd in ("/reset", "/compact", "/model other", "/goal x", "/auto on"):
                assert _chat(c2, "b1", cmd).status_code == 409, cmd
            assert c2.post("/api/reset", json={"session_id": "b1", "message": ""}).status_code == 409
            assert c2.post("/api/session/b1/truncate", json={"after_index": 0}).status_code == 409
            assert c2.post("/api/session/b1/edit",
                           json={"at_index": 1, "content": "x"}).status_code == 409
            assert c2.post("/api/session/b1/reroll").status_code == 409
            # ...read-only ones still answer, and steering queues onto it.
            help_ = _chat(c2, "b1", "/help")
            assert help_.status_code == 200 and "Built-in commands" in help_.text
            assert c2.post("/api/session/b1/steer", json={"text": "hi"}).json()["queued"] is True
            assert agents[0].resets == 0

            assert c2.post("/api/decide", json={"session_id": "b1", "decision_id":
                                                req["decision_id"], "approved": True}).json() == {"ok": True}
            done = _read_until(lines, "Done")
            assert done["reason"] == "stop"
        assert agents[0].decisions == [True]  # the first turn ran to completion, approved
        _wait(lambda: _session(c, "b1")["busy"] is False)
        # Free again: a new turn starts normally.
        assert "approved" not in _chat(c, "b1", "no-tool").text
        assert len(agents) == 1


def test_unanswered_permission_request_denies_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents: list[_GateAgent] = []
    c = TestClient(_make_app(monkeypatch, tmp_path, agents=agents), base_url=LOCAL)
    monkeypatch.setenv("EVI_DECISION_TIMEOUT_S", "0.3")
    started = time.monotonic()
    r = _chat(c, "d1", "go")
    assert time.monotonic() - started < 10
    kinds = [e["kind"] for e in _events(r.text)]
    assert kinds == ["TextDelta", "PermissionRequest", "PermissionExpired", "ToolResult",
                     "TextDelta", "Done"], kinds
    evs = _events(r.text)
    assert evs[1]["timeout_s"] == 0.3
    assert evs[2]["decision_id"] == evs[1]["decision_id"]
    assert evs[3]["output"] == "PERMISSION DENIED"
    assert agents[0].decisions == [False]
    snap = next(s for s in c.get("/api/dispatch").json()["sessions"] if s["id"] == "d1")
    assert snap["busy"] is False and snap["pending"] == 0
    # A late answer finds nothing to approve.
    late = c.post("/api/decide", json={"session_id": "d1", "decision_id": evs[1]["decision_id"],
                                       "approved": True})
    assert late.json() == {"ok": False, "reason": "no such decision"}


def test_sse_disconnect_does_not_deny_and_another_tab_can_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve
) -> None:
    agents: list[_GateAgent] = []
    base = serve(_make_app(monkeypatch, tmp_path, agents=agents))
    with httpx.Client(base_url=base, timeout=20) as c:
        with c.stream("POST", "/api/chat", json={"session_id": "t1", "message": "go"}) as s:
            req = _read_until(s.iter_lines(), "PermissionRequest")
        # The client went away (tab closed / reloaded) — nothing is denied.
        time.sleep(1.0)
        assert agents[0].decisions == []
        snap = _session(c, "t1")
        assert snap["busy"] is True and snap["pending"] == 1
        # Another tab of the same user answers it later.
        assert _chat(c, "t2", "/help").status_code == 200
        r = c.post("/api/decide", json={"session_id": "t2", "decision_id": req["decision_id"],
                                        "approved": True})
        assert r.json() == {"ok": True}
        _wait(lambda: _session(c, "t1")["busy"] is False)
        assert agents[0].decisions == [True]


def test_decision_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVI_DECISION_TIMEOUT_S", raising=False)
    cfg = SimpleNamespace(web=SimpleNamespace())
    assert server_mod._decision_timeout_s(cfg) == 300.0
    assert server_mod._decision_timeout_s(None) == 300.0
    cfg.web.decision_timeout_s = 45
    assert server_mod._decision_timeout_s(cfg) == 45.0
    monkeypatch.setenv("EVI_DECISION_TIMEOUT_S", "7.5")
    assert server_mod._decision_timeout_s(cfg) == 7.5
    # Never "no deadline": junk, zero and negatives fall through to the default.
    for junk in ("0", "-1", "nan", "inf", "soon"):
        monkeypatch.setenv("EVI_DECISION_TIMEOUT_S", junk)
        assert server_mod._decision_timeout_s(SimpleNamespace(web=SimpleNamespace())) == 300.0


def test_channel_run_on_a_used_session_does_not_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A channel `run: true` used to reuse the last /api/chat's permission
    callback, whose SSE stream was gone — it waited forever. Now nobody is
    watching, so a tool that needs approval is denied at once."""
    agents: list[_GateAgent] = []
    c = TestClient(_make_app(monkeypatch, tmp_path, agents=agents), base_url=LOCAL)
    monkeypatch.setenv("EVI_DECISION_TIMEOUT_S", "60")  # a stale callback would block 60 s
    assert _chat(c, "ch1", "no-tool").status_code == 200  # a used session
    started = time.monotonic()
    r = c.post("/api/session/ch1/channel", json={"text": "run the tool", "run": True})
    assert time.monotonic() - started < 10
    assert r.status_code == 200 and r.json()["ran"] is True
    assert "denied" in r.json()["reply"]
    assert agents[0].decisions == [False]
    # A fresh session is denied too — not "no callback, so allow".
    r = c.post("/api/session/ch2/channel", json={"text": "run the tool", "run": True})
    assert r.json()["ran"] is True and agents[1].decisions == [False]
    assert all(s["busy"] is False for s in c.get("/api/dispatch").json()["sessions"])


def test_turns_leave_a_deny_callback_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents: list[_GateAgent] = []
    c = TestClient(_make_app(monkeypatch, tmp_path, agents=agents), base_url=LOCAL)
    assert _chat(c, "cb1", "no-tool").status_code == 200
    assert agents[0].permission_callback is server_mod._deny_unattended


def test_read_only_slash_commands() -> None:
    for raw in ("/help", "/?", "/tools", "/model", "/goal", "/auto", "/HELP"):
        assert server_mod._slash_is_read_only(raw), raw
    for raw in ("/reset", "/compact", "/reload", "/model x", "/goal do it", "/auto on",
                "/plan", "/plan go", "/mycommand", "/"):
        assert not server_mod._slash_is_read_only(raw), raw


# ---- multi-user: owner-only routes ----------------------------------------


def _serve_federation(cfg: Config) -> None:
    cfg.federation.serve = True
    cfg.federation.a2a = True


@pytest.fixture
def multi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    app = _make_app(monkeypatch, tmp_path, token="owner-tok", multi_user=True,
                    users=[{"name": "alice", "token": "alice-tok"},
                           {"name": "owner", "token": "guest-tok"}],
                    configure=_serve_federation)
    return TestClient(app, base_url=LOCAL)


ALICE = {"Authorization": "Bearer alice-tok"}
OWNER = {"Authorization": "Bearer owner-tok"}


def _concrete(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_owner_only_route_refuses_a_user_login(multi: TestClient) -> None:
    from fastapi.routing import APIRoute

    checked = set()
    for route in multi.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD"}:
            if not server_mod._owner_only_route(method, _concrete(route.path)):
                continue
            r = multi.request(method, _concrete(route.path), headers=ALICE, json={})
            assert r.status_code == 403, (method, route.path, r.status_code, r.text)
            # A login NAMED owner doesn't even sign in (see the next test).
            r = multi.request(method, _concrete(route.path),
                              headers={"Authorization": "Bearer guest-tok"}, json={})
            assert r.status_code == 401, (method, route.path, r.status_code, r.text)
            checked.add((method, route.path))
    # The routes the verifier named are all in the table.
    for want in [
        ("POST", "/api/config"), ("GET", "/api/config"), ("POST", "/api/hooks"),
        ("POST", "/api/plugins/install"), ("POST", "/api/channels/telegram/approve"),
        ("POST", "/api/peers/bind-lan"), ("POST", "/api/peers"), ("POST", "/api/runtime/stop"),
        ("POST", "/api/mcp"), ("POST", "/api/mcp/serve"), ("POST", "/api/backends"),
        ("POST", "/api/backend/use"), ("GET", "/api/backend/pull"),
        ("POST", "/api/model-picker"), ("GET", "/api/checkpoints"), ("POST", "/api/rewind"),
        ("POST", "/api/project/trust"), ("POST", "/api/dispatch/workflow/{name}"),
        # Delegated tasks run headless agents on the host's config and memory.
        ("POST", "/api/federate"), ("POST", "/a2a"),
        # The Companion: the host's one watcher, its feed and approvals.
        ("GET", "/api/companion/status"), ("GET", "/api/companion/feed"),
        ("GET", "/api/companion/proposals"), ("POST", "/api/companion/check"),
        ("POST", "/api/companion/proposals/{proposal_id}/approve"),
        ("POST", "/api/companion/proposals/{proposal_id}/reject"),
        ("GET", "/api/companion/instructions"), ("PUT", "/api/companion/instructions"),
        ("POST", "/api/companion/enabled"),
    ]:
        assert want in checked, want


def test_user_login_cannot_run_federated_tasks(
    multi: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evi.headless as headless

    runs: list[str] = []
    monkeypatch.setattr(headless, "run_headless",
                        lambda agent, task: runs.append(task) or SimpleNamespace(text="ok", error=""))
    assert multi.post("/api/federate", json={"task": "t"}, headers=ALICE).status_code == 403
    a2a = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
           "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "t"}]}}}
    assert multi.post("/a2a", json=a2a, headers=ALICE).status_code == 403
    assert runs == []
    # Peers send the owner's token.
    r = multi.post("/api/federate", json={"task": "t"}, headers=OWNER)
    assert r.status_code == 200 and r.json()["text"] == "ok"
    assert runs == ["t"]


@pytest.mark.parametrize("name", ["owner", "Owner", "OWNER", "owner.", "-owner-", " owner "])
def test_login_named_owner_cannot_reach_the_owners_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """The owner's sessions are keyed "owner" and its data lives in
    users/owner: a users.json login that maps there must not sign in, or it
    reads the owner's sessions and drives the owner's agent."""
    app = _make_app(monkeypatch, tmp_path, token="owner-tok", multi_user=True,
                    users=[{"name": "alice", "token": "alice-tok"},
                           {"name": name, "token": "guest-tok"}])
    c = TestClient(app, base_url=LOCAL)
    assert _chat(c, "owner-private", "no-tool secret plans", OWNER).status_code == 200
    guest = {"Authorization": "Bearer guest-tok"}
    assert c.get("/api/dispatch", headers=guest).status_code == 401
    assert c.get("/api/session/owner-private/history", headers=guest).status_code == 401
    assert _chat(c, "owner-private", "no-tool", guest).status_code == 401
    assert c.get("/api/auth/check", headers=guest).json() == {"ok": False, "required": True}
    # Other logins are unaffected, and still isolated from the owner.
    assert c.get("/api/auth/check", headers=ALICE).json()["user"] == "alice"
    ids = [s["id"] for s in c.get("/api/dispatch", headers=ALICE).json()["sessions"]]
    assert "owner-private" not in ids
    ids = [s["id"] for s in c.get("/api/dispatch", headers=OWNER).json()["sessions"]]
    assert "owner-private" in ids


def test_reserved_login_names() -> None:
    from evi.doctor import _owner_named

    for name in ("owner", "Owner", "owner.", "-owner-", "_owner", " owner "):
        assert server_mod._reserved_login(name) and _owner_named(name), name
    for name in ("alice", "owners", "co-owner", "owner2", "the owner"):
        assert not server_mod._reserved_login(name) and not _owner_named(name), name


@pytest.mark.parametrize("first,second", [
    ("bob smith", "bob_smith"),         # both users/bob_smith
    ("bob_smith", "bob/smith"),
    ("Alice", "alice"),                 # one folder on Windows and macOS
    (".", "user"),                      # an empty name falls back to users/user
])
def test_logins_sharing_a_data_folder_cannot_sign_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    first: str, second: str,
) -> None:
    """Two logins whose names map to one users/<dir> would read each other's
    chats and memory: neither signs in, each is told why (without naming the
    other), the owner's log names both, and other logins are unaffected."""
    monkeypatch.setattr(server_mod, "_logged_collisions", set())  # logged once per process
    app = _make_app(monkeypatch, tmp_path, token="owner-tok", multi_user=True,
                    users=[{"name": "carol", "token": "carol-tok"},
                           {"name": first, "token": "first-tok"},
                           {"name": second, "token": "second-tok"}])
    c = TestClient(app, base_url=LOCAL)
    with caplog.at_level("WARNING", logger=server_mod.logger.name):
        for tok, name, other in (("first-tok", first, second), ("second-tok", second, first)):
            auth = {"Authorization": f"Bearer {tok}"}
            r = c.get("/api/dispatch", headers=auth)
            assert r.status_code == 403, (name, r.text)
            detail = r.json()["detail"]
            assert f"The login {name!r} can't sign in" in detail
            assert f"users/{server_mod._safe_user(name)})" in detail and "rename" in detail
            assert repr(other) not in detail
            assert _chat(c, "s1", "no-tool", auth).status_code == 403
            check = c.get("/api/auth/check", headers=auth).json()
            assert check == {"ok": False, "required": True, "error": detail}
    warnings = [rec.getMessage() for rec in caplog.records if "users.json" in rec.getMessage()]
    assert len(warnings) == 1, warnings  # once, however many requests
    assert repr(first) in warnings[0] and repr(second) in warnings[0]
    # A distinct login, the owner and an unknown token are as before.
    carol = {"Authorization": "Bearer carol-tok"}
    assert c.get("/api/auth/check", headers=carol).json() == {"ok": True, "required": True,
                                                              "user": "carol"}
    assert _chat(c, "c1", "no-tool", carol).status_code == 200
    assert c.get("/api/dispatch", headers=OWNER).status_code == 200
    stranger = {"Authorization": "Bearer nope"}
    assert c.get("/api/dispatch", headers=stranger).status_code == 401
    assert c.get("/api/auth/check", headers=stranger).json() == {"ok": False, "required": True}


def test_distinct_logins_keep_their_own_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Names that only look alike but map to different folders, and one login
    listed twice with two tokens, all still sign in."""
    users = [{"name": "bob-smith", "token": "t1"}, {"name": "bob_smith", "token": "t2"},
             {"name": "dana", "token": "t3"}, {"name": "dana", "token": "t4"}]
    assert server_mod._colliding_logins(
        [SimpleNamespace(name=u["name"]) for u in users]) == {}
    c = TestClient(_make_app(monkeypatch, tmp_path, token="owner-tok", multi_user=True,
                             users=users), base_url=LOCAL)
    for tok, name in (("t1", "bob-smith"), ("t2", "bob_smith"), ("t3", "dana"), ("t4", "dana")):
        r = c.get("/api/whoami", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200 and r.json() == {"user": name}, tok


def test_colliding_logins_groups_every_login_on_one_folder() -> None:
    names = ["Bob Smith", "bob_smith", "bob/smith", "BOB-SMITH", "eve", "Eve.", "zed"]
    got = server_mod._colliding_logins([SimpleNamespace(name=n) for n in names])
    assert got == {
        "Bob Smith": ["bob_smith", "bob/smith"],
        "bob_smith": ["Bob Smith", "bob/smith"],
        "bob/smith": ["Bob Smith", "bob_smith"],
        "eve": ["Eve."],
        "Eve.": ["eve"],
    }


def test_non_ascii_login_token_is_a_401_not_a_500(multi: TestClient) -> None:
    r = multi.get("/api/dispatch", headers={"Authorization": "Bearer café".encode("latin-1")})
    assert r.status_code == 401


def test_user_cannot_list_or_rewind_checkpoints(
    multi: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evi.checkpoints as cp

    calls: list[str] = []
    monkeypatch.setattr(cp, "list_checkpoints", lambda *a, **k: calls.append("list") or [{"seq": 1}])
    monkeypatch.setattr(cp, "rewind", lambda *a, **k: calls.append("rewind") or [])
    assert multi.get("/api/checkpoints", headers=ALICE).status_code == 403
    assert multi.post("/api/rewind", json={"seq": 1}, headers=ALICE).status_code == 403
    assert calls == []
    assert multi.get("/api/checkpoints", headers=OWNER).json() == {"checkpoints": [{"seq": 1}]}
    assert multi.post("/api/rewind", json={}, headers=OWNER).json()["ok"] is True
    assert calls == ["list", "rewind"]


def test_users_keep_their_own_session_routes(multi: TestClient) -> None:
    assert _chat(multi, "u1", "no-tool", ALICE).status_code == 200
    assert multi.get("/api/session/u1/usage", headers=ALICE).status_code == 200
    assert multi.get("/api/whoami", headers=ALICE).json() == {"user": "alice"}


def test_user_cannot_switch_the_host_model_by_slash_command(multi: TestClient) -> None:
    # `/model <id>` saves [llm] model to config.toml, like POST /api/model-picker.
    assert _chat(multi, "u2", "/model other", ALICE).status_code == 403
    assert _chat(multi, "u2", "/model", ALICE).status_code == 200  # reading it is fine


def test_owner_reaches_owner_only_routes(multi: TestClient) -> None:
    assert multi.get("/api/config", headers=OWNER).status_code == 200
    r = multi.post("/api/peers/bind-lan", json={"enabled": True}, headers=OWNER)
    assert r.status_code == 200 and r.json()["bind_lan"] is True


def test_owner_only_classifier() -> None:
    f = server_mod._owner_only_route
    assert f("GET", "/api/config") and f("POST", "/api/config")
    assert f("GET", "/api/backend/pull") and f("POST", "/api/backend/start")
    assert not f("GET", "/api/backend/status")
    assert f("POST", "/api/backends") and f("DELETE", "/api/backends/x")
    assert f("POST", "/api/model-picker") and not f("GET", "/api/model-picker")
    assert not f("POST", "/api/chat") and not f("GET", "/api/configx")
    assert not f("POST", "/api/session/s1/channel")
    assert f("POST", "/api/project/trust") and not f("GET", "/api/project/trust")
    assert f("GET", "/api/companion/feed") and f("POST", "/api/companion/proposals/x/approve")
    assert not f("GET", "/api/companionx")


# ---- doctor: web access posture ---------------------------------------------


@pytest.fixture
def doctor_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import evi.config as config_mod

    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr("evi.users.USERS_PATH", tmp_path / "users.json")
    monkeypatch.delenv("EVI_WEB_AUTH_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # no project .evi.toml in play

    def write(text: str) -> None:
        (tmp_path / "config.toml").write_text(text, encoding="utf-8")

    return SimpleNamespace(home=tmp_path, write=write)


def _web_checks():
    from evi.doctor import _check_web_access

    return {c.name: c for c in _check_web_access()}


def test_doctor_default_is_loopback_only(doctor_home) -> None:
    c = _web_checks()["web access"]
    assert c.status == "ok" and "only this machine" in c.detail


def test_doctor_bind_lan_migration_message(doctor_home, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "")
    doctor_home.write("[federation]\nbind_lan = true\n")
    c = _web_checks()["web access"]
    assert c.status == "warn" and "no access token" in c.detail
    assert c.fix == ("evi web-config token rotate",)
    (doctor_home.home / "desktop-token").write_text("a" * 64, encoding="utf-8")
    c = _web_checks()["web access"]
    assert c.status == "ok" and "desktop-token" in c.detail
    # Only the desktop app reads that file: a CLI or container server on the
    # LAN still has no token, and doctor says so instead of an unqualified ok.
    assert server_mod.lan_bind_refusal("0.0.0.0", Config.load()) is not None
    assert "only the desktop app reads" in c.detail.lower()
    assert "evi web --host 0.0.0.0" in c.detail and "EVI_WEB_AUTH_TOKEN" in c.detail


def test_doctor_token_set(doctor_home) -> None:
    doctor_home.write('[web]\nauth_token = "t"\n[federation]\nbind_lan = true\n')
    assert _web_checks()["web access"].status == "ok"


def test_doctor_multi_user_without_owner_token(doctor_home) -> None:
    doctor_home.write("[web]\nmulti_user = true\n")
    (doctor_home.home / "users.json").write_text(
        json.dumps([{"name": "alice", "token": "a"}]), encoding="utf-8")
    c = _web_checks()["multi-user owner"]
    assert c.status == "warn" and "owner-only" in c.detail


def test_doctor_flags_a_login_named_owner(doctor_home) -> None:
    from evi.doctor import _check_web_access

    doctor_home.write('[web]\nmulti_user = true\nauth_token = "t"\n')
    (doctor_home.home / "users.json").write_text(
        json.dumps([{"name": "alice", "token": "a"}, {"name": "Owner", "token": "b"}]),
        encoding="utf-8")
    flagged = [c for c in _check_web_access() if c.name == "multi-user owner"]
    assert len(flagged) == 1 and flagged[0].status == "warn"
    assert "'Owner'" in flagged[0].detail and "reserved" in flagged[0].detail
    assert flagged[0].fix == ('evi web-config users remove "Owner"',)


# ---- desktop core updates: ABI-1 shells never get a token core ---------------


def test_published_cores_require_the_token_shell_abi(tmp_path: Path) -> None:
    """Criterion: an ABI-1 shell is offered no core that requires the token.
    The shell stages a core only when the manifest's min_shell_abi <= its
    SHELL_ABI (sidecar_update.rs shell_can_run), and desktop-release.yml builds
    the manifest with the script's default. So the manifest it builds must ask
    for the ABI that speaks the token handshake (2): an ABI-1 shell refuses
    it, and the current shell still takes it."""
    import importlib.util
    import re

    token_abi = 2  # the shell hands a LAN-bound core its token (SHELL_ABI docs)
    root = Path(__file__).resolve().parents[1]
    rs = (root / "desktop" / "src-tauri" / "src" / "sidecar_update.rs").read_text(encoding="utf-8")
    shell_abi = int(re.search(r"pub const SHELL_ABI: u32 = (\d+);", rs).group(1))
    assert shell_abi >= token_abi
    # The gate this test mirrors.
    assert re.search(r"fn shell_can_run\(min_shell_abi: u32, shell_abi: u32\) -> bool \{"
                     r"\s*min_shell_abi <= shell_abi\s*\}", rs)

    def shell_can_run(min_shell_abi: int, abi: int) -> bool:
        return min_shell_abi <= abi

    spec = importlib.util.spec_from_file_location(
        "build_sidecar_manifest", root / "scripts" / "build-sidecar-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Exactly as the release workflow runs it: no --min-shell-abi.
    out = tmp_path / "sidecar-latest.json"
    assert mod.main(["--version", "1.0.54", "--repo", "o/r",
                     "--sha", f"windows-x86_64={'0' * 64}", "--out", str(out)]) == 0
    min_abi = json.loads(out.read_text(encoding="utf-8"))["min_shell_abi"]
    assert not shell_can_run(min_abi, token_abi - 1), (
        f"min_shell_abi {min_abi}: ABI-1 shells would stage cores that need the LAN token "
        "(scripts/build-sidecar-manifest.py DEFAULT_MIN_SHELL_ABI)")
    assert shell_can_run(min_abi, shell_abi), "the current shell would refuse its own core"
    wf = (root / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
    for passed in re.findall(r"--min-shell-abi[ =]+(\d+)", wf):
        assert token_abi <= int(passed) <= shell_abi, passed


# ---- launchers: `evi web`, `evi web-config token`, the Docker entrypoint -----
#
# `evi web --host 0.0.0.0` with no token used to start and then answer nobody
# else; the published image ran exactly that, so behind a port mapping it was
# "healthy" and refused every user. These drive the real CLI (CliRunner) and
# the real entrypoint script (a POSIX sh, with a stand-in `evi` on PATH).


@pytest.fixture
def cli_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A tmp eVi home for CLI commands — config.toml, users.json and the
    launch-token files live there — and no launch token anywhere."""
    import evi.config as config_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr("evi.users.USERS_PATH", home / "users.json")
    monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "")
    for var in ("EVI_WEB_AUTH_TOKEN", "EVI_PROFILE", "EVI_DECISION_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no project .evi.toml in play

    def write(text: str) -> None:
        (home / "config.toml").write_text(text, encoding="utf-8")

    write("")
    return SimpleNamespace(home=home, write=write)


def _cli(*args: str):
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    return CliRunner().invoke(cli_main.app, list(args))


def _flat(text: str) -> str:
    """Rich wraps long lines at the console width: compare the words."""
    return " ".join(text.split())


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list:
    """`evi web` up to the point it would serve: uvicorn.run records its call."""
    import uvicorn

    calls: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append(k))
    monkeypatch.setattr("evi.runtime.setup.ensure_running", lambda: None)
    return calls


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_evi_web_refuses_a_lan_bind_without_a_token(
    cli_home, fake_uvicorn: list, host: str
) -> None:
    out = _cli("web", "--host", host, "--port", "8000")
    assert out.exit_code == 1, out.output
    assert fake_uvicorn == []  # never served
    text = _flat(out.output)
    assert "refusing to serve" in text and "without an access token" in text
    assert "evi web-config token rotate" in text and "EVI_WEB_AUTH_TOKEN" in text


@pytest.mark.parametrize("setup,banner", [
    ("config", "web auth: token required ([web] auth_token, from this machine too"),
    ("launch", "web auth: token required from other machines (EVI_WEB_AUTH_TOKEN; "
               "this machine is served without it)"),
    ("users", "web auth: users.json logins (multi-user; no owner token"),
])
def test_evi_web_serves_a_lan_bind_that_has_a_token(
    cli_home, fake_uvicorn: list, monkeypatch: pytest.MonkeyPatch, setup: str, banner: str
) -> None:
    if setup == "config":
        cli_home.write('[web]\nauth_token = "cfg-tok"\n')
    elif setup == "launch":
        monkeypatch.setattr(server_mod, "_LAUNCH_TOKEN", "launch-tok")
    else:  # users.json logins gate the LAN like a token does
        cli_home.write("[web]\nmulti_user = true\n")
        (cli_home.home / "users.json").write_text(
            json.dumps([{"name": "alice", "token": "a-tok"}]), encoding="utf-8")
    out = _cli("web", "--host", "0.0.0.0", "--port", "8000")
    assert out.exit_code == 0, out.output
    assert [(k["host"], k["port"]) for k in fake_uvicorn] == [("0.0.0.0", 8000)]
    text = _flat(out.output)
    assert banner in text, text
    assert "disabled" not in text


def test_evi_web_on_loopback_without_a_token_says_only_this_machine(
    cli_home, fake_uvicorn: list
) -> None:
    out = _cli("web")
    assert out.exit_code == 0, out.output
    assert fake_uvicorn[0]["host"] == "127.0.0.1"
    text = _flat(out.output)
    assert "web auth: no token — only this machine can connect" in text
    # The command it names exists (it used to say `evi web token rotate`).
    assert "`evi web-config token rotate`" in text and "evi web token" not in text
    assert "desktop-token" not in text
    # The desktop app's LAN token in the home is not this server's.
    (cli_home.home / "desktop-token").write_text("ab" * 32, encoding="utf-8")
    text = _flat(_cli("web").output)
    assert "desktop-token is the desktop app's LAN token" in text
    assert "this server does not use it" in text


def test_token_show_prints_the_token_a_server_here_uses(
    cli_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the token goes to stdout (`$(evi web-config token show)`); where
    it comes from goes to stderr. Precedence: [web] auth_token, then
    EVI_WEB_AUTH_TOKEN, then a launcher's generated token in the home."""
    def show() -> tuple[str, str]:
        out = _cli("web-config", "token", "show")
        assert out.exit_code == 0, out.output
        return out.stdout, _flat(out.stderr)

    stdout, note = show()
    assert stdout == "" and "no access token" in note and "only this machine" in note
    desktop = "d" * 64
    (cli_home.home / "desktop-token").write_text(desktop + "\n", encoding="utf-8")
    stdout, note = show()
    assert stdout == desktop + "\n"
    assert "desktop app's LAN token" in note and "LAN access is off now" in note
    cli_home.write("[federation]\nbind_lan = true\n")
    stdout, note = show()
    assert stdout == desktop + "\n" and "LAN access is off" not in note
    container = "c" * 64
    (cli_home.home / "container-token").write_text(container + "\n", encoding="utf-8")
    stdout, note = show()
    assert stdout == container + "\n" and "image's entrypoint" in note
    monkeypatch.setenv("EVI_WEB_AUTH_TOKEN", " env-tok ")
    stdout, note = show()
    assert stdout == "env-tok\n" and "EVI_WEB_AUTH_TOKEN" in note
    cli_home.write('[web]\nauth_token = "cfg-tok"\n')
    stdout, note = show()
    assert stdout == "cfg-tok\n" and "this machine's too" in note


def test_token_show_skips_a_token_file_no_launcher_wrote(cli_home) -> None:
    """Launchers reuse only a 64-hex token and replace anything else, so
    anything else is not the token a server uses."""
    (cli_home.home / "desktop-token").write_text("guessable", encoding="utf-8")
    (cli_home.home / "container-token").write_bytes(b"\xff\xfe")
    out = _cli("web-config", "token", "show")
    assert out.exit_code == 0 and out.stdout == ""


def test_token_rotate_says_it_takes_effect_without_a_restart(cli_home) -> None:
    """The server re-reads config.toml on every request
    (test_container_port_mapping_needs_a_token_and_rotate_fixes_a_running_one
    shows it), so the message no longer says to restart `evi web`."""
    out = _cli("web-config", "token", "rotate")
    assert out.exit_code == 0, out.output
    token = Config.load().web.auth_token
    text = _flat(out.output)
    assert len(token) == 64 and token in text
    assert "In effect now, no restart needed" in text and "Restart" not in text
    assert "stays valid until its server restarts" in text  # a launch token
    assert _cli("web-config", "token", "show").stdout == token + "\n"


def test_decision_timeout_in_config_toml_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`[web] decision_timeout_s` used to be dropped as an unknown key, so only
    EVI_DECISION_TIMEOUT_S worked."""
    monkeypatch.delenv("EVI_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)  # no project .evi.toml in play
    agents: list[_GateAgent] = []
    c = TestClient(_make_app(monkeypatch, tmp_path, agents=agents, real_config=True),
                   base_url=LOCAL)
    (tmp_path / "home" / "config.toml").write_text(
        "[web]\ndecision_timeout_s = 0.3\n", encoding="utf-8")
    assert Config._from_dict({"web": {"decision_timeout_s": 0.3}})[1] == []  # a known key
    assert Config().web.decision_timeout_s == 300.0
    started = time.monotonic()
    r = _chat(c, "d1", "go")
    assert time.monotonic() - started < 10
    evs = _events(r.text)
    assert [e["kind"] for e in evs][:3] == ["TextDelta", "PermissionRequest",
                                           "PermissionExpired"], evs
    assert evs[1]["timeout_s"] == 0.3
    assert agents[0].decisions == [False]
    # The environment variable still wins.
    monkeypatch.setenv("EVI_DECISION_TIMEOUT_S", "7")
    assert server_mod._decision_timeout_s(Config.load()) == 7.0


_ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "docker-entrypoint.sh"
_WEB_CMD = ("evi", "web", "--host", "0.0.0.0", "--port", "8000")  # the image's CMD


def _run_entrypoint(tmp_path: Path, home: Path, *cmd: str,
                    env_token: str | None = None) -> tuple[str, str, str]:
    """Run scripts/docker-entrypoint.sh with a stand-in `evi` that reports the
    EVI_WEB_AUTH_TOKEN it was started with and its arguments. Returns (that
    token, the arguments, the entrypoint's log)."""
    import os
    import shutil

    sh = shutil.which("dash") or shutil.which("sh")  # the image's /bin/sh is dash
    if not sh:
        pytest.skip("no POSIX sh to run scripts/docker-entrypoint.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "evi"
    fake.write_bytes(b'#!/bin/sh\nprintf "token=%s\\nargs=%s\\n" '
                     b'"${EVI_WEB_AUTH_TOKEN:-}" "$*"\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("EVI_WEB_AUTH_TOKEN", "EVI_PROFILE")}
    env.update(
        EVI_HOME=str(home),
        # `python` is this interpreter, importing this checkout's evi.
        PATH=os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent),
                              env.get("PATH", "")]),
        PYTHONPATH=str(_ENTRYPOINT.parents[1]),
        PYTHONUTF8="1",
    )
    if env_token is not None:
        env["EVI_WEB_AUTH_TOKEN"] = env_token
    out = subprocess.run([sh, str(_ENTRYPOINT), *cmd], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    got = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    return got["token"], got["args"], out.stderr


def test_docker_entrypoint_generates_and_keeps_a_token(tmp_path: Path) -> None:
    import os
    import re
    import stat

    home = tmp_path / "evi-home"
    token, args, log = _run_entrypoint(tmp_path, home, *_WEB_CMD)
    assert re.fullmatch(r"[0-9a-f]{64}", token), token  # 256 bits
    assert args == "web --host 0.0.0.0 --port 8000"  # the command runs as given
    path = home / "container-token"
    assert path.read_text(encoding="utf-8").strip() == token
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The log says how to read it, never the token itself.
    assert "docker exec <container> evi web-config token show" in log
    assert token not in log
    # A restart reuses it (the state volume keeps the file).
    assert _run_entrypoint(tmp_path, home, *_WEB_CMD)[0] == token
    # A file it could not have written is replaced, not trusted.
    path.write_text("guessable", encoding="utf-8")
    fresh = _run_entrypoint(tmp_path, home, *_WEB_CMD)[0]
    assert re.fullmatch(r"[0-9a-f]{64}", fresh) and fresh != token


def test_docker_entrypoint_leaves_a_chosen_token_alone(tmp_path: Path) -> None:
    home = tmp_path / "evi-home"
    # EVI_WEB_AUTH_TOKEN passed in (docker run -e, compose `environment:`).
    token, args, _log = _run_entrypoint(tmp_path, home, *_WEB_CMD, env_token="my-own")
    assert (token, args) == ("my-own", "web --host 0.0.0.0 --port 8000")
    # [web] auth_token in config.toml: no launch token on top of it.
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text('[web]\nauth_token = "cfg-tok"\n', encoding="utf-8")
    assert _run_entrypoint(tmp_path, home, *_WEB_CMD)[0] == ""
    # Any other command runs untouched.
    (home / "config.toml").unlink()
    assert _run_entrypoint(tmp_path, home, "evi", "doctor")[:2] == ("", "doctor")
    assert not (home / "container-token").exists()


def test_generated_container_token_opens_the_published_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the entrypoint's token, the image's `evi web --host 0.0.0.0`
    starts, the in-container HEALTHCHECK (loopback) passes, a client through
    the port mapping (the Docker bridge) gets in with the token, and `docker
    exec … evi web-config token show` prints it."""
    import evi.config as config_mod

    home = tmp_path / "evi-home"
    token = _run_entrypoint(tmp_path, home, *_WEB_CMD)[0]
    app = _make_app(monkeypatch, tmp_path, launch_token=token)
    assert server_mod.lan_bind_refusal("0.0.0.0", Config()) is None
    healthcheck = TestClient(app, base_url="http://localhost:8000")
    assert healthcheck.get("/api/health").status_code == 200
    bridge = TestClient(app, base_url="http://localhost:8000", client=("172.17.0.1", 50123))
    assert bridge.get("/api/config").status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    assert bridge.get("/api/config", headers=auth).status_code == 200
    assert _chat(bridge, "s1", "no-tool", auth).status_code == 200
    # `docker exec` gets the container's env, not the entrypoint's export.
    monkeypatch.delenv("EVI_WEB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("EVI_PROFILE", raising=False)
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.chdir(tmp_path)
    assert _cli("web-config", "token", "show").stdout == token + "\n"


def test_mcp_serve_config_does_not_offer_the_agent_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The director-facing task tools need their repository (--repo) and a
    # server of their own; the web snippet would hand out a refused command.
    pytest.importorskip("mcp")
    client = TestClient(_make_app(monkeypatch, tmp_path), base_url=LOCAL)
    r = client.get("/api/mcp/serve/config", params={"categories": "memory,agent"})
    assert r.status_code == 400 and "evi mcp serve-config" in r.text
    r = client.get("/api/mcp/serve/config", params={"categories": "memory,git"})
    assert r.status_code == 200 and "agent" not in r.json()["categories"]
