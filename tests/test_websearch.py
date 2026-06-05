"""Tests for web_search + web_fetch tools (network mocked)."""

from __future__ import annotations

import json
import sys
import types
from typing import Iterator

import httpx
import pytest

from evi.tools.base import REGISTRY
import evi.tools.websearch  # noqa: F401  register tools


# ---- web_search ---------------------------------------------------------


def _install_fake_ddgs(monkeypatch: pytest.MonkeyPatch, results: list[dict]) -> None:
    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query: str, max_results: int = 5) -> Iterator[dict]:
            yield from results[:max_results]

    module = types.ModuleType("duckduckgo_search")
    module.DDGS = _DDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "duckduckgo_search", module)


def test_web_search_returns_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ddgs(monkeypatch, [
        {"title": "Evi", "href": "https://example.com", "body": "snippet"},
        {"title": "Two", "href": "https://example.org", "body": "second"},
    ])
    out = REGISTRY["web_search"].call(json.dumps({"query": "evi", "limit": 5}))
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]["title"] == "Evi"
    assert data[0]["url"] == "https://example.com"
    assert data[0]["snippet"] == "snippet"


def test_web_search_rejects_empty_query() -> None:
    out = REGISTRY["web_search"].call(json.dumps({"query": "  "}))
    assert out.startswith("ERROR:")


def test_web_search_handles_missing_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)
    out = REGISTRY["web_search"].call(json.dumps({"query": "x"}))
    assert "duckduckgo_search not installed" in out


# ---- web_fetch ----------------------------------------------------------


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real_client(transport=transport)

    monkeypatch.setattr("evi.tools.websearch.httpx.Client", fake_client)


def test_web_fetch_extracts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = b"""
        <html><head><title>x</title><script>nope</script></head>
        <body><h1>Hello</h1><p>World of text</p></body></html>
        """
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html"}
        )

    _patch_httpx_client(monkeypatch, handler)
    out = REGISTRY["web_fetch"].call(json.dumps({"url": "https://x/"}))
    assert "Hello" in out
    assert "World of text" in out
    assert "nope" not in out  # script content stripped


def test_web_fetch_rejects_non_http() -> None:
    out = REGISTRY["web_fetch"].call(json.dumps({"url": "file:///etc/passwd"}))
    assert "ERROR" in out


def test_web_fetch_rejects_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\x00\x01\x02", headers={"content-type": "image/png"}
        )

    _patch_httpx_client(monkeypatch, handler)
    out = REGISTRY["web_fetch"].call(json.dumps({"url": "https://x/p.png"}))
    assert "unsupported content-type" in out
