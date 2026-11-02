"""/api/backend/use carries the configured API key over only when the switch
keeps the same server: an online provider's key must never go to the URL the
switch names."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.config import Config  # noqa: E402

PROVIDER_URL = "https://api.provider.example/v1"
PROVIDER_KEY = "env:EVI_TEST_PROVIDER_KEY"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # Isolated config (CONFIG_PATH is bound at import), set up with an online
    # provider and its key.
    import evi.config as _cfg

    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(_cfg, "CONFIG_PATH", tmp_path / "config.toml")
    cfg = Config()
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.api_key = "openai_compat", PROVIDER_URL, PROVIDER_KEY
    cfg.save()
    return TestClient(server_mod.create_app())


@pytest.mark.parametrize("kind,url", [("openai_compat", "http://other-host.example:8000/v1"),
                                      ("llamacpp", "http://127.0.0.1:8080/v1"),
                                      ("openai_compat", None)])  # None: the kind's default URL
def test_a_switch_to_another_server_drops_the_key(client, kind, url):
    body = {"kind": kind, "model": "m"} | ({"base_url": url} if url else {})
    assert client.post("/api/backend/use", json=body).json()["ok"] is True
    assert Config.load().llm.api_key == "sk-noauth"


def test_the_verify_request_never_carries_the_old_key(client, monkeypatch):
    # The verify step is the first request to the new server: it must already
    # go out without the provider's key.
    seen = []
    monkeypatch.setattr(server_mod, "_verify_llm",
                        lambda cfg: seen.append(cfg.llm.api_key) or {"chat": True})
    client.post("/api/backend/use", json={"kind": "openai_compat", "model": "m", "verify": True,
                                          "base_url": "http://other-host.example:8000/v1"})
    assert seen == ["sk-noauth"]


@pytest.mark.parametrize("url", [PROVIDER_URL, PROVIDER_URL + "/", " " + PROVIDER_URL])
def test_a_switch_that_keeps_the_server_keeps_the_key(client, url):
    # Changing only the model (or the kind) on the same server keeps its key.
    body = {"kind": "openai_compat", "model": "bigger-model", "base_url": url}
    assert client.post("/api/backend/use", json=body).json()["ok"] is True
    assert Config.load().llm.api_key == PROVIDER_KEY


def test_a_failed_verify_restores_the_key(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_verify_llm", lambda cfg: {"chat": False})
    client.post("/api/backend/use", json={"kind": "openai_compat", "model": "m", "verify": True,
                                          "base_url": "http://other-host.example:8000/v1"})
    cfg = Config.load()
    assert (cfg.llm.base_url, cfg.llm.api_key) == (PROVIDER_URL, PROVIDER_KEY)
