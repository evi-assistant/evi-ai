"""Tests for web_search + web_fetch tools (network mocked)."""

from __future__ import annotations

import json
import sys
import types
from typing import Iterator

import httpx
import pytest

from evi import netguard
from evi.config import Config
from evi.tools.base import REGISTRY
import evi.tools.websearch  # noqa: F401  register tools


@pytest.fixture(autouse=True)
def _stock_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    """Both tools read config; give them a stock one instead of ~/.evi."""
    cfg = Config()
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    return cfg


# ---- web_search ---------------------------------------------------------


def _install_fake_ddgs(monkeypatch: pytest.MonkeyPatch, results: list[dict]) -> None:
    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query: str, max_results: int = 5) -> Iterator[dict]:
            yield from results[:max_results]

    module = types.ModuleType("ddgs")
    module.DDGS = _DDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)


def test_web_search_returns_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ddgs(monkeypatch, [
        {"title": "eVi", "href": "https://example.com", "body": "snippet"},
        {"title": "Two", "href": "https://example.org", "body": "second"},
    ])
    out = REGISTRY["web_search"].call(json.dumps({"query": "evi", "limit": 5}))
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]["title"] == "eVi"
    assert data[0]["url"] == "https://example.com"
    assert data[0]["snippet"] == "snippet"


def test_web_search_rejects_empty_query() -> None:
    out = REGISTRY["web_search"].call(json.dumps({"query": "  "}))
    assert out.startswith("ERROR:")


def test_web_search_handles_missing_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Block both the preferred (ddgs) and legacy (duckduckgo_search) packages so
    # _search_ddg exhausts its fallback ladder and returns the install hint.
    monkeypatch.setitem(sys.modules, "ddgs", None)
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)
    out = REGISTRY["web_search"].call(json.dumps({"query": "x"}))
    assert "ddgs not installed" in out


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
    # web_fetch resolves (and vets) the host itself; every test host is public.
    monkeypatch.setattr(netguard, "resolve", lambda host, port: ["93.184.215.14"])


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


def test_web_fetch_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx_client(monkeypatch, lambda req: httpx.Response(404))
    out = REGISTRY["web_fetch"].call(json.dumps({"url": "https://x/missing"}))
    assert out.startswith("ERROR: fetch failed: HTTP 404")


# ---- web_fetch: redirects are re-checked against the permission policy -------


def _redirecting(monkeypatch: pytest.MonkeyPatch, location: str) -> list[str]:
    """a.example redirects to `location`; every other host serves a page.
    Returns the hosts requested, in order."""
    hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        host = req.headers["host"]
        hosts.append(host)
        if host == "a.example":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(
            200, content=f"<p>page on {host}</p>".encode(),
            headers={"content-type": "text/html"},
        )

    _patch_httpx_client(monkeypatch, handler)
    return hosts


def _fetch(url: str) -> str:
    return REGISTRY["web_fetch"].call(json.dumps({"url": url}))


@pytest.mark.parametrize("field, entry", [
    ("trusted_domains", "a.example"),
    ("rules", "allow web_fetch https://a.example/*"),
])
def test_redirect_from_pre_approved_url_to_other_host_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, _stock_config: Config, field: str, entry: str,
) -> None:
    """The first URL got through without a prompt (trusted domain / allow
    rule); that approval doesn't stretch to the host a redirect names, or a
    trusted host's open redirect would auto-approve any URL."""
    setattr(_stock_config.auto, field, [entry])
    hosts = _redirecting(monkeypatch, "https://elsewhere.example/?leak=1")
    out = _fetch("https://a.example/")
    assert out.startswith("ERROR: blocked:")
    assert "https://elsewhere.example/?leak=1" in out and "not pre-approved" in out
    # The text is true in every session: the next call is checked by the
    # session's policy (which may well allow it, e.g. under /auto on).
    assert "session's permission check" in out
    assert "goes through approval" not in out
    assert hosts == ["a.example"]  # the other host was never contacted


def test_redirect_to_other_host_followed_when_first_url_needed_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stock policy: the first URL itself needed approval (a prompt, /auto on,
    or a run with no one to ask let it through), so its public redirects are
    followed as before; netguard still vets every address."""
    hosts = _redirecting(monkeypatch, "https://elsewhere.example/moved")
    assert "page on elsewhere.example" in _fetch("https://a.example/")
    assert hosts == ["a.example", "elsewhere.example"]


def test_redirect_to_other_host_refused_when_config_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken():
        raise OSError("config unreadable")

    monkeypatch.setattr(Config, "load", staticmethod(broken))
    hosts = _redirecting(monkeypatch, "https://elsewhere.example/")
    out = _fetch("https://a.example/")
    assert out.startswith("ERROR: blocked:") and "could not be read" in out
    assert hosts == ["a.example"]


def test_auto_all_session_follows_cross_host_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-level: under /auto on (evi run -y, scheduler, workflows...) the
    agent allows web_fetch, and a cross-host redirect (golang.org -> go.dev,
    GitHub raw links) is followed instead of costing a failed tool call."""
    from evi.llm.agent import Agent, ToolResult
    from tests.test_agent import (
        _Chunk, _Choice, _Delta, _FakeClient, _FnDelta, _text_chunk, _ToolCallDelta,
    )

    hosts = _redirecting(monkeypatch, "https://elsewhere.example/doc")
    call = _ToolCallDelta(index=0, id="c1", function=_FnDelta(
        name="web_fetch", arguments=json.dumps({"url": "https://a.example/"}),
    ))
    script = [
        [_Chunk([_Choice(_Delta(tool_calls=[call]), finish_reason="tool_calls")])],
        [_text_chunk("done", finish="stop")],
    ]

    def no_prompt(*_):
        raise AssertionError("auto_all must not prompt")

    agent = Agent(
        client=_FakeClient(script), config=Config(), tools=[REGISTRY["web_fetch"]],
        permission_callback=no_prompt,
    )
    agent.enable_auto_all()
    assert agent._permission_decision(REGISTRY["web_fetch"], call.function.arguments) == "allow"
    results = [e for e in agent.chat("read it") if isinstance(e, ToolResult)]
    assert "page on elsewhere.example" in results[0].output
    assert hosts == ["a.example", "elsewhere.example"]


def test_redirect_on_same_host_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = _redirecting(monkeypatch, "https://www.a.example/moved")
    assert "page on www.a.example" in _fetch("https://a.example/")
    assert hosts == ["a.example", "www.a.example"]


def test_redirect_to_trusted_domain_is_followed(
    monkeypatch: pytest.MonkeyPatch, _stock_config: Config,
) -> None:
    _stock_config.auto.trusted_domains = ["docs.example"]
    _redirecting(monkeypatch, "https://api.docs.example/v1")
    assert "page on api.docs.example" in _fetch("https://a.example/")


def test_redirect_followed_when_web_is_auto_approved(
    monkeypatch: pytest.MonkeyPatch, _stock_config: Config,
) -> None:
    _stock_config.auto.auto_approve = ["web"]
    _redirecting(monkeypatch, "https://elsewhere.example/")
    assert "page on elsewhere.example" in _fetch("https://a.example/")


@pytest.mark.parametrize("field, entry", [
    ("rules", "deny web_fetch *elsewhere*"),
    ("hard_deny", "web_fetch *elsewhere*"),
])
def test_redirect_denied_by_rules(
    monkeypatch: pytest.MonkeyPatch, _stock_config: Config, field: str, entry: str,
) -> None:
    _stock_config.auto.auto_approve = ["web"]  # deny still wins
    setattr(_stock_config.auto, field, [entry])
    hosts = _redirecting(monkeypatch, "https://elsewhere.example/")
    out = _fetch("https://a.example/")
    assert out.startswith("ERROR: blocked:") and "deny" in out
    assert hosts == ["a.example"]
