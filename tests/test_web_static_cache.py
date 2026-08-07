"""The index page + static assets must revalidate on every load.

Without `Cache-Control: no-cache`, WebView2/browsers heuristically cache
index.html — and since the whole single-file UI (all its JS) lives in that one
file, a cached index.html means a cached UI. A feature shipped through the
sidecar update channel then appears "not there" in an already-updated app until
the cache happens to expire. `no-cache` forces a conditional revalidation (the
ETag still yields a cheap 304 when nothing changed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import evi.config as _cfg

    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(_cfg, "CONFIG_PATH", tmp_path / "config.toml")
    return TestClient(server_mod.create_app())


def test_index_html_revalidates(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    assert "etag" in r.headers  # still cheap — 304 when unchanged


def test_static_assets_revalidate(client):
    r = client.get("/static/manifest.json")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
