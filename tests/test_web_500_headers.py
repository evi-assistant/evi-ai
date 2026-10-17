"""An unhandled error's 500 carries the same security headers as every other
response (polish-small-fixes, part 2).

The security headers (`X-Content-Type-Options: nosniff`, and on the page its
CSP and `X-Frame-Options: DENY`) are set by _SecurityHeadersMiddleware, the
outermost middleware create_app adds. But Starlette answers an exception that
escapes a route from ServerErrorMiddleware, which sits outside every added
middleware and writes its 500 straight to the server, so that one response went
out without them. The app's own 500 handler (_internal_error) now sets them on
it, through the same function the middleware uses.

The app's home, config and data folders are under tmp_path, never ~/.evi.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402

# The headers _security_headers sets on one response or another.
SECURITY = ("x-content-type-options", "content-security-policy", "x-frame-options")


def _security(response) -> dict[str, str | None]:
    return {name: response.headers.get(name) for name in SECURITY}


@pytest.fixture()
def app(monkeypatch, tmp_path):
    import evi.config as _cfg

    # HOME and the server's data dirs are resolved at import, so the env var
    # alone doesn't redirect them; point every one the app touches at tmp_path.
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(_cfg, "HOME", tmp_path)
    monkeypatch.setattr(_cfg, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")
    app = server_mod.create_app()

    # Routes that fail the way a bug in a handler does: an exception nothing
    # catches, from a sync route (run in the threadpool) and an async one.
    def boom_sync() -> dict:
        raise RuntimeError("a bug in a sync route")

    async def boom_async() -> dict:
        raise ValueError("a bug in an async route")

    app.add_api_route("/api/test-boom-sync", boom_sync, methods=["GET"])
    app.add_api_route("/api/test-boom-async", boom_async, methods=["GET"])
    return app


@pytest.mark.parametrize("path", ["/api/test-boom-sync", "/api/test-boom-async"])
def test_an_unhandled_error_is_a_500_with_nosniff(app, path) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(path)
    assert r.status_code == 500
    assert r.text == "Internal Server Error"  # Starlette's own body: no traceback
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers.get("x-content-type-options") == "nosniff"
    # The same security headers as a route that answered on the same kind of
    # path: an API reply is nosniff, and is not a page (no CSP, no framing).
    ok = client.get("/api/health")
    assert ok.status_code == 200
    assert _security(r) == _security(ok) == {
        "x-content-type-options": "nosniff", "content-security-policy": None,
        "x-frame-options": None,
    }


def test_a_500_on_the_page_gets_the_page_headers(app, monkeypatch) -> None:
    """The page route failing: its 500 carries the page's CSP and no-framing
    headers too, as its 200 does (the headers follow the path)."""
    client = TestClient(app, raise_server_exceptions=False)
    ok = client.get("/")
    assert ok.status_code == 200

    def broken(*_a, **_k):
        raise OSError("index.html could not be read")

    monkeypatch.setattr(server_mod, "FileResponse", broken)  # what index() returns
    r = client.get("/")
    assert r.status_code == 500
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers["content-security-policy"] == server_mod.page_csp()
    assert r.headers["x-frame-options"] == "DENY"
    assert _security(r) == _security(ok)


def test_a_500_under_uploads_gets_the_sandbox_policy(app, monkeypatch) -> None:
    client = TestClient(app, raise_server_exceptions=False)

    def broken(*_a, **_k):
        raise PermissionError("the uploads folder can't be read")

    monkeypatch.setattr(server_mod, "_session_upload_dir", broken)
    r = client.get("/uploads/s1/page.html")
    assert r.status_code == 500
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers["content-security-policy"] == server_mod._FILE_CSP


def test_the_error_still_reaches_the_server(app) -> None:
    """The handler only answers: the exception still propagates past it, so
    uvicorn logs it (and a TestClient that raises server exceptions sees it)."""
    with pytest.raises(RuntimeError, match="a bug in a sync route"):
        TestClient(app).get("/api/test-boom-sync")


def test_http_errors_keep_their_own_answers(app) -> None:
    """Only an unhandled exception goes to the 500 handler: an HTTPException
    (a 404, a 400) keeps its status and JSON detail, with the headers as before."""
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/uploads/s1/..%5Cx")
    assert r.status_code in (400, 404)
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers.get("x-content-type-options") == "nosniff"
