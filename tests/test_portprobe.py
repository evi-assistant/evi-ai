"""Unit tests for evi.portprobe — socket/HTTP probing + llama.cpp port scan."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evi.portprobe as pp  # noqa: E402


class _Resp:
    def __init__(self, status, json_body=None, raise_json=False):
        self.status_code = status
        self._json = json_body
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json


# --- split_host_port / with_port ----------------------------------------


def test_split_host_port_normalizes_localhost():
    assert pp.split_host_port("http://localhost:1234/v1") == ("127.0.0.1", 1234)
    assert pp.split_host_port("http://127.0.0.1:8080/v1") == ("127.0.0.1", 8080)
    assert pp.split_host_port("localhost:11434") == ("127.0.0.1", 11434)


def test_with_port_preserves_scheme_and_path():
    assert pp.with_port("http://localhost:8080/v1", 8083) == "http://127.0.0.1:8083/v1"
    assert pp.with_port("http://127.0.0.1:8080", 8081) == "http://127.0.0.1:8081"


# --- is_openai_server ----------------------------------------------------


def test_is_openai_server_skips_http_when_port_closed(monkeypatch):
    monkeypatch.setattr(pp, "port_open", lambda *a, **k: False)

    def boom(*a, **k):
        raise AssertionError("httpx should not be called when port is closed")

    monkeypatch.setattr("httpx.get", boom)
    assert pp.is_openai_server("http://localhost:1234/v1") is False


def test_is_openai_server_true_for_openai_shape(monkeypatch):
    monkeypatch.setattr(pp, "port_open", lambda *a, **k: True)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(200, {"data": []}))
    assert pp.is_openai_server("http://localhost:11434/v1") is True


def test_is_openai_server_false_for_404_html(monkeypatch):
    monkeypatch.setattr(pp, "port_open", lambda *a, **k: True)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(404, raise_json=True))
    assert pp.is_openai_server("http://localhost:8080/v1") is False


def test_is_openai_server_false_for_200_without_data_list(monkeypatch):
    monkeypatch.setattr(pp, "port_open", lambda *a, **k: True)
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(200, {"hello": "world"}))
    assert pp.is_openai_server("http://localhost:8080/v1") is False


# --- discover_llamacpp_url -----------------------------------------------


def test_discover_finds_first_valid_port(monkeypatch):
    # 8080 occupied by a non-LLM service, real llama.cpp on 8083.
    monkeypatch.setattr(pp, "port_open", lambda host, port: port in (8080, 8083))

    def fake_is_openai(url, **k):
        return ":8083" in url  # only 8083 is a real llama.cpp

    monkeypatch.setattr(pp, "is_openai_server", fake_is_openai)
    found = pp.discover_llamacpp_url("http://localhost:8080/v1")
    assert found == "http://127.0.0.1:8083/v1"


def test_discover_prefers_lowest_port(monkeypatch):
    monkeypatch.setattr(pp, "port_open", lambda host, port: port in (8081, 8085))
    monkeypatch.setattr(pp, "is_openai_server", lambda url, **k: True)
    found = pp.discover_llamacpp_url("http://localhost:8080/v1")
    assert found == "http://127.0.0.1:8081/v1"


def test_discover_returns_none_when_nothing_valid(monkeypatch):
    monkeypatch.setattr(pp, "port_open", lambda host, port: port == 8080)
    monkeypatch.setattr(pp, "is_openai_server", lambda url, **k: False)
    assert pp.discover_llamacpp_url("http://localhost:8080/v1") is None


def test_discover_scans_span_ports(monkeypatch):
    seen = []
    monkeypatch.setattr(pp, "port_open", lambda host, port: seen.append(port) or False)
    monkeypatch.setattr(pp, "is_openai_server", lambda url, **k: False)
    pp.discover_llamacpp_url("http://localhost:8080/v1", span=10)
    assert sorted(seen) == list(range(8080, 8091))
