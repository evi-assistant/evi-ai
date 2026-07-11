"""Tests for the LLM-backend availability endpoints (Phase 48):
/api/backend/status, /api/backend/start, /api/backend/open-download."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # Isolate config so the auth middleware sees no token AND so tests that
    # WRITE config (/api/backend/use) can't clobber the real ~/.evi/config.toml.
    # CONFIG_PATH is bound at import time, so setenv("EVI_HOME") alone is too
    # late — redirect the module global directly.
    import evi.config as _cfg
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(_cfg, "CONFIG_PATH", tmp_path / "config.toml")
    app = server_mod.create_app()
    return TestClient(app)


# --- _probe_candidate (kind dispatch) ------------------------------------


def test_probe_candidate_llamacpp_uses_discovery(monkeypatch):
    monkeypatch.setattr(
        server_mod, "discover_llamacpp_url", lambda u: "http://127.0.0.1:8083/v1"
    )
    ok, resolved = server_mod._probe_candidate("llamacpp", "http://localhost:8080/v1")
    assert ok is True
    assert resolved == "http://127.0.0.1:8083/v1"


def test_probe_candidate_llamacpp_none_keeps_default(monkeypatch):
    monkeypatch.setattr(server_mod, "discover_llamacpp_url", lambda u: None)
    ok, resolved = server_mod._probe_candidate("llamacpp", "http://localhost:8080/v1")
    assert ok is False
    assert resolved == "http://localhost:8080/v1"


def test_probe_candidate_other_uses_single_probe(monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_backend", lambda u: True)
    ok, resolved = server_mod._probe_candidate("ollama", "http://localhost:11434/v1")
    assert ok is True
    assert resolved == "http://localhost:11434/v1"


def test_probe_candidate_cli_agent_checks_path_not_http(monkeypatch):
    # CLI-agent backends have no HTTP endpoint — reachable iff the CLI is on PATH,
    # and the HTTP probe must NOT be consulted for them.
    calls = []
    monkeypatch.setattr("shutil.which", lambda n: (calls.append(n), "/usr/bin/claude")[1])

    def _no_http(_u):
        raise AssertionError("HTTP probe must not run for CLI-agent kinds")

    monkeypatch.setattr(server_mod, "_probe_backend", _no_http)
    ok, resolved = server_mod._probe_candidate("claude_agent", "")
    assert ok is True and resolved == "claude CLI"
    assert calls == ["claude"]


def test_probe_candidate_cli_agent_missing_cli(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    ok, resolved = server_mod._probe_candidate("codex", "")
    assert ok is False and resolved == "codex CLI"


def test_probe_candidate_cli_agent_binary_map():
    assert server_mod._CLI_AGENT_BINS == {
        "claude_agent": "claude", "codex": "codex", "gemini": "gemini",
        "amp": "amp", "qwen": "qwen", "copilot": "copilot",
    }


# --- /api/backend/status -------------------------------------------------


def test_status_none_reachable(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_candidate", lambda k, u: (False, u))
    monkeypatch.setattr("shutil.which", lambda _n: None)
    r = client.get("/api/backend/status")
    assert r.status_code == 200
    body = r.json()
    assert body["any_reachable"] is False
    assert body["ollama_installed"] is False
    assert {c["kind"] for c in body["candidates"]} == {"lmstudio", "ollama", "llamacpp"}
    assert all(c["reachable"] is False for c in body["candidates"])


def test_status_ollama_reachable(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_candidate", lambda k, u: ("11434" in u, u))
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/ollama")
    body = client.get("/api/backend/status").json()
    assert body["any_reachable"] is True
    assert body["ollama_installed"] is True
    ollama = next(c for c in body["candidates"] if c["kind"] == "ollama")
    assert ollama["reachable"] is True


def test_status_llamacpp_reports_alt_port(client, monkeypatch):
    """When llama.cpp is found on a non-default port, the candidate URL
    reflects where it actually is."""

    def fake_probe(kind, url):
        if kind == "llamacpp":
            return (True, "http://127.0.0.1:8083/v1")
        return (False, url)

    monkeypatch.setattr(server_mod, "_probe_candidate", fake_probe)
    monkeypatch.setattr("shutil.which", lambda _n: None)
    body = client.get("/api/backend/status").json()
    assert body["any_reachable"] is True
    llama = next(c for c in body["candidates"] if c["kind"] == "llamacpp")
    assert llama["reachable"] is True
    assert llama["url"] == "http://127.0.0.1:8083/v1"


# --- /api/backend/start --------------------------------------------------


def test_start_ollama_already_running(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_backend", lambda _u: True)
    r = client.post("/api/backend/start", json={"kind": "ollama"})
    body = r.json()
    assert body["started"] is False
    assert body["already_running"] is True


def test_start_ollama_not_installed(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_backend", lambda _u: False)
    monkeypatch.setattr("shutil.which", lambda _n: None)
    body = client.post("/api/backend/start", json={"kind": "ollama"}).json()
    assert body["started"] is False
    assert body["installed"] is False


def test_start_ollama_spawns(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_backend", lambda _u: False)
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/ollama")
    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    body = client.post("/api/backend/start", json={"kind": "ollama"}).json()
    assert body["started"] is True
    assert calls["cmd"] == ["/usr/bin/ollama", "serve"]
    # No window on Windows / detached on posix.
    if os.name == "nt":
        assert calls["kwargs"].get("creationflags") == 0x0800_0000
    else:
        assert calls["kwargs"].get("start_new_session") is True


def test_start_lmstudio_returns_manual_instructions(client):
    body = client.post("/api/backend/start", json={"kind": "lmstudio"}).json()
    assert body["started"] is False
    assert "LM Studio" in body["message"]


def test_open_download(client, monkeypatch):
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url) or True)
    body = client.post("/api/backend/open-download", json={"kind": "ollama"}).json()
    assert body["opened"] is True
    assert "ollama.com" in body["url"]


# --- first-run wizard: status hints + install + pull (Phase 50) -----------


def test_status_includes_firstrun_hints(client, monkeypatch):
    import evi.firstrun as fr

    monkeypatch.setattr(server_mod, "_probe_candidate", lambda k, u: (False, u))
    monkeypatch.setattr("shutil.which", lambda _n: None)
    monkeypatch.setattr(fr, "recommended_model", lambda: "qwen2.5:3b-instruct-q4_K_M")
    monkeypatch.setattr(
        fr, "ollama_install_plan",
        lambda **k: fr.OllamaInstallPlan(available=True, method="winget"),
    )
    body = client.get("/api/backend/status").json()
    assert body["recommended_model"] == "qwen2.5:3b-instruct-q4_K_M"
    assert body["can_auto_install_ollama"] is True


def test_install_endpoint_delegates_to_firstrun(client, monkeypatch):
    import evi.firstrun as fr

    monkeypatch.setattr(
        fr, "install_ollama",
        lambda **k: {"ok": True, "method": "winget", "message": "Ollama installed."},
    )
    body = client.post("/api/backend/install", json={"kind": "ollama"}).json()
    assert body["ok"] is True and body["method"] == "winget"


def test_install_endpoint_rejects_non_ollama(client):
    body = client.post("/api/backend/install", json={"kind": "lmstudio"}).json()
    assert body["ok"] is False


def test_backend_use_explicit_model_persists(client):
    body = client.post(
        "/api/backend/use",
        json={"kind": "ollama", "model": "qwen2.5:3b-instruct-q4_K_M"},
    ).json()
    assert body["ok"] is True
    assert body["backend"] == "ollama"
    assert "11434" in body["base_url"]
    assert body["model"] == "qwen2.5:3b-instruct-q4_K_M"
    # persisted: a fresh status reflects the new configured backend
    status = client.get("/api/backend/status").json()
    assert status["configured"]["backend"] == "ollama"


def test_backend_use_autopicks_recommended_when_installed(client, monkeypatch):
    import evi.backends.factory as fac
    import evi.firstrun as fr

    class FakeBackend:
        def list_models(self):
            return [SimpleNamespace(id="qwen2.5:3b-instruct-q4_K_M"), SimpleNamespace(id="other:7b")]

    monkeypatch.setattr(fac, "get_backend", lambda settings: FakeBackend())
    monkeypatch.setattr(fr, "recommended_model", lambda: "qwen2.5:3b-instruct-q4_K_M")
    body = client.post("/api/backend/use", json={"kind": "ollama"}).json()
    assert body["model"] == "qwen2.5:3b-instruct-q4_K_M"  # recommended + installed → chosen


def test_backend_use_autopick_falls_back_to_first_installed(client, monkeypatch):
    import evi.backends.factory as fac
    import evi.firstrun as fr

    monkeypatch.setattr(
        fac, "get_backend",
        lambda settings: SimpleNamespace(list_models=lambda: [SimpleNamespace(id="only:7b")]),
    )
    monkeypatch.setattr(fr, "recommended_model", lambda: "qwen2.5:3b-instruct-q4_K_M")  # not installed
    body = client.post("/api/backend/use", json={"kind": "ollama"}).json()
    assert body["model"] == "only:7b"  # recommended absent → first installed


def test_backend_use_rejects_unknown_kind(client):
    assert client.post("/api/backend/use", json={"kind": "bogus"}).status_code == 400


def test_pull_endpoint_streams_progress(client, monkeypatch):
    import evi.backends.ollama as ol
    from evi.backends.base import PullProgress

    def fake_pull(self, model_id):
        yield PullProgress(status="pulling manifest", downloaded=50, total=100, detail="d")
        yield PullProgress(status="success", downloaded=100, total=100, detail="d")

    monkeypatch.setattr(ol.OllamaBackend, "pull_model", fake_pull)
    r = client.get("/api/backend/pull?model=test:1b")
    assert r.status_code == 200
    text = r.text
    assert "progress" in text          # progress events streamed
    assert '"pct": 50.0' in text       # halfway computed from completed/total
    assert "done" in text              # terminal event
