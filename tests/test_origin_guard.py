"""1.0.53 hotfix: the cross-site request guard.

A multipart POST is a CORS "simple request" (no preflight), so before this any
web page could drive /api/upload & co. through the user's browser on the default
no-token desktop setup. State-changing requests now need our own Origin (or a
loopback alias of the same port, or the desktop shell's), and are refused when
the browser says Sec-Fetch-Site: cross-site. No Origin = a non-browser client
(CLI, VS Code extension host, federation/A2A peers) and passes. The one GET
with side effects (/api/backend/pull) is guarded the same way, plus a no-Origin
`same-site` refusal for <img>-style requests from another local port.
X-Forwarded-Host counts only with EVI_TRUSTED_PROXY=1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Done, Event, TextDelta  # noqa: E402


class _FakeAgent:
    def __init__(self) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.goal = None
        self.plan_mode_once = False
        self.auto_all = False
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None
        self.history: list[dict] = [{"role": "system", "content": "sys"}]

    def chat(self, message: str, images=None, **_) -> Iterator[Event]:
        yield TextDelta(text="ok")
        yield Done(reason="stop")

    def reset(self) -> None:
        pass


def _make_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str = ""):
    import evi.sdk.builder as builder_mod
    import evi.sessions as sessions_mod

    monkeypatch.delenv("EVI_ALLOW_CROSS_ORIGIN", raising=False)
    monkeypatch.delenv("EVI_ALLOW_ANY_HOST", raising=False)
    monkeypatch.delenv("EVI_TRUSTED_PROXY", raising=False)
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(sessions_mod, "find_session", lambda *_, **__: None)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")

    def fake_load(cls=Config):
        cfg = Config()
        cfg.web.auth_token = token
        return cfg

    monkeypatch.setattr(server_mod.Config, "load", classmethod(lambda cls: fake_load()))
    return server_mod.create_app()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return _make_app(monkeypatch, tmp_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)  # Host: testserver → own origin http://testserver


@pytest.fixture
def local_client(app) -> TestClient:
    """What the desktop app / browser actually sends: Host 127.0.0.1:8473."""
    return TestClient(app, base_url="http://127.0.0.1:8473")


def _upload(c: TestClient, headers: dict[str, str], sid: str = "s1"):
    return c.post("/api/upload", data={"session_id": sid},
                  files={"file": ("x.txt", b"hi", "text/plain")}, headers=headers)


def _chat(c: TestClient, headers: dict[str, str]):
    return c.post("/api/chat", json={"session_id": "s1", "message": "hi"}, headers=headers)


# ---- refused ---------------------------------------------------------------


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://evil.example:8473",
    "http://127.0.0.1:9999",        # another local port: same SITE, different origin
    "http://localhost:9999",
    "null",                         # sandboxed iframe / file:// / data:
    "NULL",
    "chrome-extension://abcdef",
    "file://",
    "http://127.0.0.1:notaport",
    "tauri://evil",
])
def test_foreign_origin_post_is_refused(
    local_client: TestClient, tmp_path: Path, origin: str
) -> None:
    r = _upload(local_client, {"Origin": origin})
    assert r.status_code == 403, (origin, r.text)
    detail = r.json()["detail"]
    assert "cross-site" in detail and "EVI_ALLOW_CROSS_ORIGIN=1" in detail
    assert not (tmp_path / "uploads").exists()   # nothing written


def test_sec_fetch_site_cross_site_is_refused(local_client: TestClient) -> None:
    r = _upload(local_client, {"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert "Sec-Fetch-Site" in r.json()["detail"]


def test_sec_fetch_site_cross_site_refused_even_with_own_origin(
    local_client: TestClient,
) -> None:
    r = _upload(local_client, {"Origin": "http://127.0.0.1:8473",
                               "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_every_state_changing_method_is_guarded(local_client: TestClient, method: str) -> None:
    r = local_client.request(method.upper(), "/api/chat", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_json_route_is_guarded_too(local_client: TestClient) -> None:
    assert _chat(local_client, {"Origin": "https://evil.example"}).status_code == 403


def test_a2a_and_federation_routes_are_guarded(local_client: TestClient) -> None:
    for path in ("/a2a", "/api/federate"):
        r = local_client.post(path, json={}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert "cross-site" in r.json()["detail"], path


# ---- allowed ---------------------------------------------------------------


def test_no_origin_is_allowed(local_client: TestClient) -> None:
    """CLI, VS Code extension host (Node fetch), federation/A2A peers, curl."""
    assert _upload(local_client, {}).status_code == 200
    assert _chat(local_client, {}).status_code == 200


def test_peers_without_origin_reach_a2a_handler(local_client: TestClient) -> None:
    # A2A is off by default, so the HANDLER answers 403 — but with its own
    # message, proving the guard let the peer through.
    r = local_client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get"})
    assert "cross-site" not in r.text


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:8473",        # same origin (the desktop webview + browser UI)
    "http://127.0.0.1:8473/",
    "HTTP://127.0.0.1:8473",
])
def test_own_origin_allowed(local_client: TestClient, origin: str) -> None:
    r = _upload(local_client, {"Origin": origin, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, (origin, r.text)


@pytest.mark.parametrize("origin", [
    "http://localhost:8473",        # loopback aliases of the same port
    "http://[::1]:8473",
    "tauri://localhost",            # desktop shell's bundled loading page
    "http://tauri.localhost",
    "https://tauri.localhost",
])
@pytest.mark.parametrize("site", ["cross-site", None])
def test_loopback_alias_and_tauri_origins_allowed(
    local_client: TestClient, origin: str, site: str | None
) -> None:
    """With the header pair a real browser sends: Chromium/WebView2 label a
    request from these pages to 127.0.0.1 `cross-site` (a different site), so
    the allowlist must win over that; None = a browser without Sec-Fetch-*."""
    headers = {"Origin": origin, **({"Sec-Fetch-Site": site} if site else {})}
    r = _upload(local_client, headers)
    assert r.status_code == 200, (origin, site, r.text)


@pytest.mark.parametrize("origin", [
    "http://tauri.localhost:5555",  # a page on some port of tauri.localhost, not the shell
    "tauri://localhost.evil",
    "http://localhost:9999",        # alias host, but another port
    "http://127.0.0.2:8473",        # loopback range, but not an alias we accept
])
def test_near_miss_allowlist_origins_refused_cross_site(
    local_client: TestClient, origin: str
) -> None:
    r = _upload(local_client, {"Origin": origin, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403, (origin, r.text)


def test_same_origin_on_testserver_host(client: TestClient) -> None:
    assert _upload(client, {"Origin": "http://testserver"}).status_code == 200
    assert _upload(client, {"Origin": "http://localhost"}).status_code == 403


def test_loopback_alias_requires_same_port(local_client: TestClient) -> None:
    assert _upload(local_client, {"Origin": "http://localhost:8474"}).status_code == 403


def test_get_with_foreign_origin_unaffected(local_client: TestClient) -> None:
    r = local_client.get("/api/health", headers={"Origin": "https://evil.example",
                                                 "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_env_override(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVI_ALLOW_CROSS_ORIGIN", "1")
    assert _upload(local_client, {"Origin": "https://evil.example"}).status_code == 200
    assert _upload(local_client, {"Sec-Fetch-Site": "cross-site"}).status_code == 200


# ---- with a token / behind a proxy ----------------------------------------


def test_guard_applies_even_with_valid_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = TestClient(_make_app(monkeypatch, tmp_path, token="s3cret"),
                   base_url="http://127.0.0.1:8473")
    auth = {"Authorization": "Bearer s3cret"}
    assert _chat(c, {**auth, "Origin": "https://evil.example"}).status_code == 403
    assert _chat(c, auth).status_code == 200
    assert _chat(c, {**auth, "Origin": "http://127.0.0.1:8473"}).status_code == 200


def test_tls_proxy_origin_matches_plain_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behind a TLS-terminating proxy the app sees `http` + `Host: evi.example.com`
    while the browser's Origin is `https://evi.example.com` — same site."""
    c = TestClient(_make_app(monkeypatch, tmp_path, token="s3cret"),
                   base_url="http://evi.example.com")
    auth = {"Authorization": "Bearer s3cret"}
    assert _chat(c, {**auth, "Origin": "https://evi.example.com"}).status_code == 200
    assert _chat(c, {**auth, "Origin": "https://evil.example.com"}).status_code == 403
    assert _chat(c, {**auth, "Origin": "https://evi.example.com:8443"}).status_code == 403


def test_tls_proxy_explicit_443_host_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`proxy_set_header Host $host:$server_port` forwards `Host: x:443` over
    plain HTTP — still the browser's `https://x`."""
    c = TestClient(_make_app(monkeypatch, tmp_path, token="s3cret"),
                   base_url="http://evi.example.com:443")
    auth = {"Authorization": "Bearer s3cret"}
    assert _chat(c, {**auth, "Origin": "https://evi.example.com"}).status_code == 200
    assert _chat(c, {**auth, "Origin": "https://evil.example.com"}).status_code == 403
    assert _chat(c, {**auth, "Origin": "https://evi.example.com:8443"}).status_code == 403


# ---- reverse proxy that rewrites Host (nginx default, Apache) --------------


@pytest.fixture
def proxied_client(app, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """nginx's default `Host $proxy_host`: the app sees its own upstream address.
    The operator has opted in to trusting the proxy's X-Forwarded-Host."""
    monkeypatch.setenv("EVI_TRUSTED_PROXY", "1")
    return TestClient(app, base_url="http://127.0.0.1:8000")


@pytest.mark.parametrize("origin,fwd", [
    ("https://evi.example.com", {"X-Forwarded-Host": "evi.example.com"}),
    ("http://evil.example:8080", {"X-Forwarded-Host": "evil.example:8080"}),
])
def test_forwarded_host_ignored_without_opt_in(
    local_client: TestClient, tmp_path: Path, origin: str, fwd: dict[str, str]
) -> None:
    """A DNS-rebound page is same-origin with itself, so it can send any
    X-Forwarded-Host (or a Host-rewriting proxy forwards its rebound Host as
    one) — matching its own Origin must not get it in by default."""
    r = _upload(local_client, {"Origin": origin, **fwd})
    assert r.status_code == 403, (origin, fwd, r.text)
    assert not (tmp_path / "uploads").exists()


@pytest.mark.parametrize("value", ["", "0", "yes"])
def test_forwarded_host_opt_in_needs_a_true_value(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EVI_TRUSTED_PROXY", value)
    r = _upload(local_client, {"Origin": "https://evi.example.com",
                               "X-Forwarded-Host": "evi.example.com"})
    assert r.status_code == 403


@pytest.mark.parametrize("origin,fwd", [
    ("https://evi.example.com", {"X-Forwarded-Host": "evi.example.com"}),
    ("https://evi.example.com", {"X-Forwarded-Host": "EVI.example.com, 10.0.0.2"}),
    ("https://evi.example.com", {"X-Forwarded-Host": "evi.example.com",
                                 "X-Forwarded-Port": "443"}),
    ("https://evi.example.com:8443", {"X-Forwarded-Host": "evi.example.com:8443"}),
    ("https://evi.example.com:8443", {"X-Forwarded-Host": "evi.example.com",
                                      "X-Forwarded-Port": "8443"}),
])
def test_forwarded_host_matches_origin(
    proxied_client: TestClient, origin: str, fwd: dict[str, str]
) -> None:
    r = _upload(proxied_client, {"Origin": origin, **fwd})
    assert r.status_code == 200, (origin, fwd, r.text)


@pytest.mark.parametrize("origin,fwd", [
    ("https://evi.example.com", {}),                                       # no hint
    ("https://evil.example", {"X-Forwarded-Host": "evi.example.com"}),
    ("https://evi.example.com", {"X-Forwarded-Host": "evil.example"}),
    ("https://evi.example.com:8443", {"X-Forwarded-Host": "evi.example.com"}),
    ("https://evi.example.com", {"X-Forwarded-Host": "evi.example.com:bad"}),
    ("https://evi.example.com", {"X-Forwarded-Host": "evi.example.com",
                                 "X-Forwarded-Port": "8443"}),
    ("https://evi.example.com:8443", {"X-Forwarded-Host": "evi.example.com",
                                      "X-Forwarded-Port": b"8443\xb2"}),  # latin-1 "²"
    ("null", {"X-Forwarded-Host": "evi.example.com"}),
])
def test_forwarded_host_mismatch_is_refused(
    proxied_client: TestClient, tmp_path: Path, origin: str, fwd: dict
) -> None:
    r = _upload(proxied_client, {"Origin": origin, **fwd})
    assert r.status_code == 403, (origin, fwd, r.text)
    assert not (tmp_path / "uploads").exists()


def test_forwarded_host_never_overrides_sec_fetch_site(proxied_client: TestClient) -> None:
    r = _upload(proxied_client, {"Origin": "https://evi.example.com",
                                 "X-Forwarded-Host": "evi.example.com",
                                 "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_refusal_explains_the_proxy_fix(local_client: TestClient) -> None:
    r = _upload(local_client, {"Origin": "https://evi.example.com"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "X-Forwarded-Host" in detail and "EVI_TRUSTED_PROXY=1" in detail
    assert "EVI_ALLOW_CROSS_ORIGIN=1" in detail


# ---- side-effecting GET: /api/backend/pull starts an Ollama download -------


@pytest.fixture
def pulls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import evi.backends.ollama as ol
    from evi.backends.base import PullProgress

    seen: list[str] = []

    def fake_pull(self, model_id):
        seen.append(model_id)
        yield PullProgress(status="success", downloaded=1, total=1, detail="d")

    monkeypatch.setattr(ol.OllamaBackend, "pull_model", fake_pull)
    return seen


_PULL = "/api/backend/pull?model=registry.evil.example/x/huge:latest"


@pytest.mark.parametrize("headers", [
    # new EventSource(...) / fetch() from a hostile page: CORS mode, has Origin
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    # <img src> / <link> from a hostile page: no-cors, no Origin
    {"Sec-Fetch-Site": "cross-site"},
    # <img src> from a page on another local port: same-SITE, still not us
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "http://127.0.0.1:9999", "Sec-Fetch-Site": "same-site"},
    {"Origin": "null"},
])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_cross_site_pull_is_refused(
    local_client: TestClient, pulls: list[str], headers: dict[str, str], method: str
) -> None:
    r = local_client.request(method, _PULL, headers=headers)
    assert r.status_code == 403, (method, headers, r.text)
    assert pulls == []


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},   # the wizard's own EventSource
    {"Sec-Fetch-Site": "none"},          # URL typed into the address bar
    {},                                  # curl / CLI
])
def test_own_pull_still_streams(
    local_client: TestClient, pulls: list[str], headers: dict[str, str]
) -> None:
    r = local_client.get("/api/backend/pull?model=test:1b", headers=headers)
    assert r.status_code == 200, (headers, r.text)
    assert "done" in r.text
    assert pulls == ["test:1b"]


def test_cross_site_pull_env_override(
    local_client: TestClient, pulls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVI_ALLOW_CROSS_ORIGIN", "1")
    r = local_client.get("/api/backend/pull?model=test:1b",
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    assert pulls == ["test:1b"]


def test_read_only_gets_unaffected_by_same_site(local_client: TestClient) -> None:
    """Only the listed side-effecting GETs are guarded; reads stay open (a
    cross-site page can't read the reply anyway)."""
    for path in ("/api/health", "/api/modes"):
        r = local_client.get(path, headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 200, path


# ---- routine webhooks: the path token is the credential --------------------


def test_routine_webhook_from_a_browser_reaches_handler(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home-automation dashboard (another origin) firing a routine webhook is
    the documented use; the unguessable path token already stops forgery."""
    import evi.routines as routines_mod

    seen: list[str] = []
    monkeypatch.setattr(routines_mod, "get_by_token",
                        lambda token: seen.append(token) or None)
    for headers in ({"Origin": "http://homeassistant.local:8123"},
                    {"Origin": "null"},
                    {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}):
        r = local_client.post("/api/routine/tok123", headers=headers)
        assert r.status_code == 404, (headers, r.text)     # the handler's "no such routine"
        assert "cross-site" not in r.text
    assert seen == ["tok123"] * 3


def test_routine_exemption_is_prefix_exact(local_client: TestClient) -> None:
    # `/api/routines…` (other endpoints) and the bare `/api/routine` stay guarded.
    for path in ("/api/routine", "/api/routines", "/api/routinex/t"):
        r = local_client.post(path, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path
