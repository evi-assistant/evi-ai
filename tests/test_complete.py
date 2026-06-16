"""Tests for the local FIM completion engine (evi.complete)."""

from __future__ import annotations

import pytest

from evi import complete as C
from evi.config import Config


def test_supports_fim():
    assert C.supports_fim("qwen2.5-coder:14b")
    assert C.supports_fim("deepseek-coder:6.7b")
    assert C.supports_fim("starcoder2:3b")
    assert not C.supports_fim("qwen2.5:14b-instruct")
    assert not C.supports_fim("")


def test_pick_fim_model_prefers_coder_fast_model():
    cfg = Config()
    cfg.llm.model = "qwen2.5:14b"
    cfg.llm.fast_model = "qwen2.5-coder:3b"
    assert C.pick_fim_model(cfg) == "qwen2.5-coder:3b"


def test_pick_fim_model_uses_main_when_coder():
    cfg = Config()
    cfg.llm.model = "qwen2.5-coder:14b"
    cfg.llm.fast_model = ""
    assert C.pick_fim_model(cfg) == "qwen2.5-coder:14b"


class _Resp:
    def __init__(self, text):
        self.choices = [type("C", (), {"text": text})()]


class _Completions:
    def __init__(self, captured):
        self._c = captured

    def create(self, **kwargs):
        self._c.update(kwargs)
        return _Resp("INSERTED")


class _Client:
    def __init__(self, captured):
        self.completions = _Completions(captured)


def test_complete_sends_prefix_suffix(monkeypatch):
    captured = {}
    monkeypatch.setattr("evi.llm.client.make_client", lambda s: _Client(captured))
    cfg = Config()
    cfg.llm.model = "qwen2.5-coder:14b"
    out = C.complete("def add(a, b):\n    return ", " + b\n", config=cfg)
    assert out == "INSERTED"
    assert captured["prompt"].endswith("return ")
    assert captured["suffix"] == " + b\n"
    assert captured["temperature"] == 0.0


def test_complete_at_splits_at_cursor(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("evi.llm.client.make_client", lambda s: _Client(captured))
    f = tmp_path / "x.py"
    f.write_text("line1\nAB\nline3\n", encoding="utf-8")
    cfg = Config()
    cfg.llm.model = "qwen2.5-coder:14b"
    # cursor at line 2, col 2 -> prefix ends "...line1\nA", suffix starts "B\n..."
    C.complete_at(f, 2, 2, config=cfg)
    assert captured["prompt"] == "line1\nA"
    assert captured["suffix"] == "B\nline3\n"


def test_api_complete_endpoint(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as config_mod

    monkeypatch.setattr(config_mod, "HOME", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(C, "complete", lambda prefix, suffix="", **kw: "GHOST")
    client = TestClient(server_mod.create_app())
    r = client.post("/api/complete", json={"prefix": "a", "suffix": "b"})
    assert r.status_code == 200 and r.json()["completion"] == "GHOST"
