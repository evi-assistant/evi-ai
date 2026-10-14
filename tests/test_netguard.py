"""Tests for evi.netguard — web_fetch's SSRF guard.

A real local HTTP server (127.0.0.1) stands in for a "trusted" host that
redirects, and httpx.MockTransport for public hosts; a fake resolver maps test
hostnames to addresses (only `localhost` goes through the system resolver), so
nothing here reaches a real network. The `no_connect` fixture fails a test when
the guard lets a request through to an address it should have refused, instead
of letting it reach, say, a running eVi on 127.0.0.1:8473.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import gzip
import socket
import threading
import time
import tracemalloc
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from evi import netguard
from evi.config import Config
from evi.tools.base import REGISTRY
import evi.tools.websearch  # noqa: F401  register tools

CAP = 1_000_000
BIG = 50 * 1024 * 1024  # 50 MB


# ---- local server -----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        srv = self.server
        url = urlparse(self.path)
        srv.hits.append((url.path, self.headers.get("Host")))
        if url.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", parse_qs(url.query)["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == "/page":
            body = b"<html><body><p>hello page</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/host":
            body = (self.headers.get("Host") or "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(BIG))
            self.end_headers()
            chunk = b"x" * 65536
            try:
                while srv.sent < BIG:
                    self.wfile.write(chunk)
                    srv.sent += len(chunk)
            except OSError:
                pass  # the client hung up at its cap
            finally:
                srv.big_done.set()
        elif url.path == "/gzip-trailer":
            # A tiny gzip body followed by 50 MB of junk: decodes to 9 bytes,
            # so a cap on decoded bytes alone never trips.
            head = gzip.compress(b"<p>hi</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(head) + BIG))
            self.end_headers()
            self._stream(head, b"\0" * 65536, BIG)
        elif url.path == "/deflate-empty":
            # zlib header, then empty stored blocks forever: decodes to nothing.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "deflate")
            self.end_headers()  # no length: the body runs until the connection closes
            self._stream(b"\x78\x9c", b"\x00\x00\x00\xff\xff" * 13107, None)
        elif url.path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                while not srv.stop.is_set():
                    self.wfile.write(b"x")
                    srv.sent += 1
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                srv.big_done.set()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _stream(self, head: bytes, chunk: bytes, total: int | None) -> None:
        """Send `head`, then `chunk` until `total` bytes (None: until the client
        hangs up or the test ends)."""
        srv = self.server
        try:
            self.wfile.write(head)
            while not srv.stop.is_set() and (total is None or srv.sent < total):
                self.wfile.write(chunk)
                srv.sent += len(chunk)
        except OSError:
            pass  # the client hung up
        finally:
            srv.big_done.set()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.hits = []
    srv.sent = 0
    srv.big_done = threading.Event()
    srv.stop = threading.Event()  # ends the endless bodies when the test is over
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.stop.set()
    srv.shutdown()
    srv.server_close()


def _resolver(table: dict[str, list[str]], calls: list[str] | None = None):
    def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append(host)
        if host not in table:
            raise socket.gaierror(11001, "test resolver: unknown host")
        return list(table[host])

    return resolve


@pytest.fixture
def no_connect(monkeypatch):
    """Fail the test if the guard lets a connection go to a forbidden port or
    address (instead of letting it actually reach, say, a running eVi)."""
    real = netguard._send

    def guarded(client, url, addresses, headers, limit=None):
        assert url.port not in netguard.EVI_API_PORTS, f"guard let a request through to {url}"
        for a in addresses:
            why = netguard.address_problem(a)
            assert why in (None, "loopback"), f"guard let a request through to {a} ({why})"
        return real(client, url, addresses, headers, limit)

    monkeypatch.setattr(netguard, "_send", guarded)


@pytest.fixture
def raw_server():
    """A bare TCP server on 127.0.0.1 for responses http.server can't send:
    each connection's request is read, then `srv.respond(conn, srv)` runs."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(5)
    lsock.settimeout(0.05)
    srv = SimpleNamespace(port=lsock.getsockname()[1], sent=0, stop=threading.Event(),
                          respond=None)

    def serve(conn):
        with conn:
            try:
                conn.recv(65536)
                srv.respond(conn, srv)
            except OSError:
                pass  # the client hung up

    def accept():
        while not srv.stop.is_set():
            try:
                conn, _ = lsock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv
    srv.stop.set()
    t.join(5)
    lsock.close()


def _run_bounded(fn, wait: float):
    """(fn's result or exception, seconds taken); fails the test instead of
    hanging it when fn is still running after `wait` s."""
    out: dict = {}

    def run():
        try:
            out["r"] = fn()
        except Exception as exc:  # reported to the caller
            out["r"] = exc

    t0 = time.monotonic()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(wait)
    if t.is_alive():
        pytest.fail(f"fetch still running after {wait:g} s")
    return out["r"], time.monotonic() - t0


def _trusted_local(server) -> tuple[str, dict]:
    """A base URL on a 'trusted' hostname that resolves to the local server."""
    port = server.server_address[1]
    return f"http://trusted.test:{port}", {
        "trusted_domains": ["trusted.test"],
        "resolver": _resolver({"trusted.test": ["127.0.0.1"]}),
    }


# ---- acceptance: trusted host redirecting to loopback / metadata ----------


@pytest.mark.parametrize("target, why", [
    ("http://127.0.0.1:8473/api/config", "loopback"),
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
    ("http://[::ffff:127.0.0.1]:8473/api/config", "IPv6 form of 127.0.0.1"),
    ("http://10.0.0.5/admin", "private (RFC 1918)"),
])
def test_trusted_host_redirect_to_private_is_refused(server, no_connect, target, why):
    base, kw = _trusted_local(server)
    with pytest.raises(netguard.Blocked) as exc:
        netguard.fetch(f"{base}/redirect?to={target}", **kw)
    msg = str(exc.value)
    assert why in msg and "trusted_domains" in msg
    assert [p for p, _ in server.hits] == ["/redirect"]  # never reached the target


def test_redirect_to_private_does_not_reach_it(server, no_connect):
    """End to end: the redirect target is a live loopback server, and it is
    never hit because its address is not trusted."""
    base, kw = _trusted_local(server)
    port = server.server_address[1]
    with pytest.raises(netguard.Blocked):
        netguard.fetch(f"{base}/redirect?to=http://127.0.0.1:{port}/page", **kw)
    assert [p for p, _ in server.hits] == ["/redirect"]


@pytest.mark.parametrize("mode, auto_approve, why", [
    # Default policy: the cross-host hop isn't pre-approved, so it isn't followed.
    ("ask", [], {"http://127.0.0.1:8473/api/config": "not pre-approved",
                 "http://169.254.169.254/": "not pre-approved"}),
    # Web auto-approved / yolo: the hop passes the permission re-check, and the
    # address guard refuses it.
    ("ask", ["web"], {"http://127.0.0.1:8473/api/config": "loopback",
                      "http://169.254.169.254/": "cloud metadata"}),
    ("yolo", [], {"http://127.0.0.1:8473/api/config": "loopback",
                  "http://169.254.169.254/": "cloud metadata"}),
])
def test_web_fetch_trusted_redirect_to_evi_api_is_refused(
    server, no_connect, monkeypatch, mode, auto_approve, why,
):
    """Exploit regression: an auto-approved fetch of a trusted host whose open
    redirect points at eVi's own API or the metadata service is refused, and
    the model gets the reason."""
    cfg = Config()
    cfg.auto.mode = mode
    cfg.auto.auto_approve = auto_approve
    cfg.auto.trusted_domains = ["trusted.test"]
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(netguard, "resolve", _resolver({"trusted.test": ["127.0.0.1"]}))
    port = server.server_address[1]
    for target, reason in why.items():
        out = REGISTRY["web_fetch"].call(json.dumps(
            {"url": f"http://trusted.test:{port}/redirect?to={target}"}
        ))
        assert out.startswith("ERROR: blocked:"), out
        assert reason in out and "trusted_domains" in out, out
    assert {p for p, _ in server.hits} == {"/redirect"}
    # The trusted host itself still works.
    out = REGISTRY["web_fetch"].call(json.dumps({"url": f"http://trusted.test:{port}/page"}))
    assert "hello page" in out


def test_web_fetch_loopback_url_refused_without_trust(monkeypatch, no_connect):
    monkeypatch.setattr(Config, "load", staticmethod(lambda: Config()))
    for url in ("http://127.0.0.1:8473/api/config", "http://[::1]:8473/", "http://0.0.0.0:8473/"):
        out = REGISTRY["web_fetch"].call(json.dumps({"url": url}))
        assert out.startswith("ERROR: blocked:"), out


def test_trusted_private_host_is_allowed(server):
    base, kw = _trusted_local(server)
    r = netguard.fetch(f"{base}/page", **kw)
    assert r.status == 200 and b"hello page" in r.content and not r.truncated


def test_trusted_ip_literal_matches_exactly(server):
    port = server.server_address[1]
    r = netguard.fetch(f"http://127.0.0.1:{port}/page", trusted_domains=["127.0.0.1"])
    assert r.status == 200
    # A partial IP is not a "parent domain" of an address.
    with pytest.raises(netguard.Blocked):
        netguard.fetch(f"http://127.0.0.1:{port}/page", trusted_domains=["0.1"])
    # The IPv4-mapped spelling of a trusted address is the same address.
    assert netguard.host_trusted(httpx.URL("http://[::ffff:127.0.0.1]/"), ["127.0.0.1"])


# ---- trust never reaches eVi's API, or the LAN from a public page ------------


def _mixed_client(monkeypatch, public_handler):
    """Requests pinned to PUBLIC / PUBLIC2 are answered by `public_handler`
    (standing in for pages on the internet); every other address goes over the
    real network stack, i.e. to the local test server."""
    mock = httpx.MockTransport(public_handler)

    class Mixed(httpx.BaseTransport):
        def __init__(self):
            self.real = httpx.HTTPTransport()

        def handle_request(self, request):
            if request.url.host in (PUBLIC, PUBLIC2):
                return mock.handle_request(request)
            return self.real.handle_request(request)

        def close(self):
            self.real.close()

    def fake_client(*args, **kwargs):
        kwargs["transport"] = Mixed()
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(netguard.httpx, "Client", fake_client)


def test_public_redirect_to_trusted_localhost_is_refused(server, no_connect, monkeypatch):
    """Exploit regression: `localhost` is trusted (for a local SearXNG), and a
    page on the internet 302s to another port on localhost. Not followed."""
    port = server.server_address[1]
    _mixed_client(monkeypatch, lambda req: httpx.Response(
        302, headers={"location": f"http://localhost:{port}/page"},
    ))
    with pytest.raises(netguard.Blocked, match="redirect from a public address"):
        netguard.fetch(
            "http://evil.example/", trusted_domains=["localhost"],
            resolver=_resolver({"evil.example": [PUBLIC], "localhost": ["127.0.0.1"]}),
        )
    assert server.hits == []  # the local service was never reached


def test_web_fetch_public_redirect_to_trusted_localhost_is_refused(
    server, no_connect, monkeypatch,
):
    """The same exploit through the tool, with the documented remedy for a
    local SearXNG in place (trusted_domains = ["localhost"], default mode)."""
    cfg = Config()
    cfg.auto.trusted_domains = ["localhost"]
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(netguard, "resolve", _resolver(
        {"evil.example": [PUBLIC], "localhost": ["127.0.0.1"]}
    ))
    port = server.server_address[1]
    _mixed_client(monkeypatch, lambda req: httpx.Response(
        302, headers={"location": f"http://localhost:{port}/page"},
    ))
    out = REGISTRY["web_fetch"].call(json.dumps({"url": "http://evil.example/"}))
    assert out.startswith("ERROR: blocked:") and "public address" in out, out
    assert server.hits == []


def test_public_hop_in_the_middle_cannot_lead_back_to_the_lan(server, no_connect, monkeypatch):
    """Trusted LAN host -> public page -> back into the LAN: the public page
    picked that last URL, so it is refused."""
    base, kw = _trusted_local(server)
    kw["resolver"] = _resolver({"trusted.test": ["127.0.0.1"], "pub.example": [PUBLIC]})
    _mixed_client(monkeypatch, lambda req: httpx.Response(
        302, headers={"location": f"{base}/page"},
    ))
    with pytest.raises(netguard.Blocked, match="public address"):
        netguard.fetch(f"{base}/redirect?to=http://pub.example/", **kw)
    assert [p for p, _ in server.hits] == ["/redirect"]


def test_trusted_lan_redirect_to_trusted_lan_is_followed(server):
    port = server.server_address[1]
    r = netguard.fetch(
        f"http://trusted.test:{port}/redirect?to=http://other.test:{port}/page",
        trusted_domains=["trusted.test", "other.test"],
        resolver=_resolver({"trusted.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]}),
    )
    assert b"hello page" in r.content
    assert server.hits == [("/redirect", f"trusted.test:{port}"), ("/page", f"other.test:{port}")]


def test_after_a_public_hop_only_public_addresses_are_used(monkeypatch):
    """A trusted host with both a public and a LAN address, reached by a
    redirect from a public page, is only contacted on its public address."""
    seen = []

    def handler(req):
        seen.append((req.url.host, req.headers["host"]))
        if req.headers["host"] == "pub.example":
            return httpx.Response(302, headers={"location": "http://mixed.test/next"})
        return httpx.Response(200, stream=_Body(b"ok"))

    _mock_client(monkeypatch, handler)
    r = netguard.fetch(
        "http://pub.example/", trusted_domains=["mixed.test"],
        resolver=_resolver({"pub.example": [PUBLIC], "mixed.test": ["127.0.0.1", PUBLIC2]}),
    )
    assert r.content == b"ok"
    assert seen == [(PUBLIC, "pub.example"), (PUBLIC2, "mixed.test")]


@pytest.mark.parametrize("url, trusted, table", [
    ("http://localhost:8473/api/config", ["localhost"], None),
    ("http://127.0.0.1:8473/api/config", ["127.0.0.1"], None),
    ("http://[::1]:8473/api/config", ["::1"], None),
    ("http://0.0.0.0:8473/api/config", ["0.0.0.0"], None),
    ("http://evi.test:8473/api/config", ["evi.test"], {"evi.test": ["::ffff:127.0.0.1"]}),
    ("http://localhost:8000/api/dispatch/stream", ["localhost"], None),
    ("http://127.0.0.1:8000/api/config", ["127.0.0.1"], None),
    ("http://[::ffff:7f00:1]:8000/api/config", ["127.0.0.1"], None),
    ("http://evi.test:8000/api/backend/pull", ["evi.test"], {"evi.test": ["127.0.0.2"]}),
])
def test_evi_api_port_on_this_machine_is_never_fetched(url, trusted, table, no_connect):
    """Trusting a loopback name (say for SearXNG) must not expose eVi's own
    API, on `evi web`'s default port or the desktop/federation one, not even
    with an approving confirm callback."""
    kw = {"resolver": _resolver(table)} if table else {}
    with pytest.raises(netguard.Blocked, match="eVi's own web API"):
        netguard.fetch(url, trusted_domains=trusted, confirm=lambda h, r: True, **kw)
    assert {8000, 8473} <= netguard.EVI_API_PORTS


def test_evi_web_default_port_is_an_api_port():
    """EVI_WEB_PORT mirrors `evi web`'s --port default; if the CLI default
    changes, this fails instead of the guard quietly missing the new port."""
    from evi.apps.cli.main import web

    assert inspect.signature(web).parameters["port"].default == netguard.EVI_WEB_PORT
    assert netguard.is_api_port(netguard.EVI_WEB_PORT)


@pytest.mark.parametrize("url", [
    "http://localhost:8473/api/config",
    "http://localhost:8000/api/dispatch/stream?limit=1",
])
def test_web_fetch_of_evi_api_refused_with_localhost_trusted(monkeypatch, no_connect, url):
    """Exploit regression: with localhost trusted (the SearXNG remedy) the
    gate auto-approves a direct web_fetch of eVi's API, on `evi web`'s default
    port too; the guard refuses it."""
    from evi.permissions import decide

    cfg = Config()
    cfg.auto.trusted_domains = ["localhost"]
    monkeypatch.setattr(Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(netguard, "resolve", _resolver({"localhost": ["127.0.0.1"]}))
    assert decide("ask", [], [], "web_fetch", "web", {"url": url},
                  trusted_domains=["localhost"]) == "allow"  # no prompt: only the guard is left
    out = REGISTRY["web_fetch"].call(json.dumps({"url": url}))
    assert out.startswith("ERROR: blocked:") and "eVi's own web API" in out, out


def test_registered_api_port_is_never_fetched(server, monkeypatch):
    """A server bound to a non-default port registers it, and from then on
    that port here is refused like the defaults, trusted host or not."""
    monkeypatch.setattr(netguard, "_registered_ports", set())
    port = server.server_address[1]
    url = f"http://localhost:{port}/page"
    kw = {"trusted_domains": ["localhost"], "resolver": _resolver({"localhost": ["127.0.0.1"]})}
    assert netguard.fetch(url, **kw).status == 200
    netguard.register_api_port(port)
    assert netguard.is_api_port(port)
    with pytest.raises(netguard.Blocked, match="eVi's own web API"):
        netguard.fetch(url, confirm=lambda h, r: True, **kw)
    assert [path for path, _ in server.hits] == ["/page"]  # only the first fetch got there


# ---- confirm hook (interactive ask) ------------------------------------------


def test_confirm_decides_untrusted_private_address(server):
    port = server.server_address[1]
    url = f"http://lan.test:{port}/page"
    resolver = _resolver({"lan.test": ["127.0.0.1"]})
    asked = []

    def yes(host, reason):
        asked.append((host, reason))
        return True

    r = netguard.fetch(url, resolver=resolver, confirm=yes)
    assert r.status == 200
    assert asked[0][0] == "lan.test" and "loopback" in asked[0][1]

    with pytest.raises(netguard.Blocked):
        netguard.fetch(url, resolver=resolver, confirm=lambda h, r: False)

    def boom(host, reason):
        raise RuntimeError("no UI")

    with pytest.raises(netguard.Blocked):
        netguard.fetch(url, resolver=resolver, confirm=boom)


# ---- acceptance: the byte cap streams ---------------------------------------


def test_50mb_body_stops_at_cap_over_the_wire(server):
    base, kw = _trusted_local(server)
    tracemalloc.start()
    try:
        r = netguard.fetch(f"{base}/big", max_bytes=CAP, **kw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.truncated and len(r.content) == CAP
    assert peak < 16 * 1024 * 1024  # never held anything near 50 MB
    assert server.big_done.wait(10)
    assert server.sent < 16 * 1024 * 1024  # cut off near the cap, not drained


class _Body(httpx.SyncByteStream):
    """A body that streams like a network response (httpx pre-reads a
    Response built with `content=`)."""

    def __init__(self, data: bytes = b""):
        self.data = data

    def __iter__(self):
        if self.data:
            yield self.data


class _CountingStream(httpx.SyncByteStream):
    def __init__(self, total: int, chunk: bytes):
        self.total, self.chunk, self.pulled = total, chunk, 0

    def __iter__(self):
        while self.pulled < self.total:
            self.pulled += len(self.chunk)
            yield self.chunk


_REAL_CLIENT = httpx.Client


def _mock_client(monkeypatch, handler):
    """Route netguard's requests to `handler` (the request URL carries the
    pinned address; the Host header the hostname)."""
    transport = httpx.MockTransport(handler)

    def fake_client(*args, **kwargs):
        return _REAL_CLIENT(transport=transport, timeout=kwargs.get("timeout", 5.0))

    monkeypatch.setattr(netguard.httpx, "Client", fake_client)


PUBLIC = "93.184.215.14"
PUBLIC2 = "93.184.215.15"


def test_50mb_body_is_not_buffered(monkeypatch):
    stream = _CountingStream(BIG, b"x" * 65536)

    def handler(req):
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)

    _mock_client(monkeypatch, handler)
    r = netguard.fetch("http://big.test/", max_bytes=CAP,
                       resolver=_resolver({"big.test": [PUBLIC]}))
    assert r.truncated and len(r.content) == CAP
    assert stream.pulled <= CAP + 65536  # read one chunk past the cap, no more


def test_compressed_body_cannot_inflate_past_cap(monkeypatch):
    comp = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)  # gzip
    zero = bytes(1024 * 1024)
    bomb = b"".join(comp.compress(zero) for _ in range(50)) + comp.flush()
    assert len(bomb) < 1024 * 1024

    def handler(req):
        assert "br" not in req.headers.get("accept-encoding", "")
        return httpx.Response(
            200, stream=_Body(bomb),
            headers={"content-type": "text/plain", "content-encoding": "gzip"},
        )

    _mock_client(monkeypatch, handler)
    tracemalloc.start()
    try:
        r = netguard.fetch("http://bomb.test/", max_bytes=CAP,
                           resolver=_resolver({"bomb.test": [PUBLIC]}))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.truncated and len(r.content) == CAP and r.content == bytes(CAP)
    assert peak < 16 * 1024 * 1024


def test_small_compressed_body_decodes_whole(monkeypatch):
    body = b"<p>" + b"hello " * 1000 + b"</p>"
    for enc, data in (
        ("gzip", zlib.compress(body, wbits=zlib.MAX_WBITS | 16)),
        ("deflate", zlib.compress(body)),
        ("deflate", zlib.compress(body, wbits=-zlib.MAX_WBITS)),  # raw deflate
    ):
        _mock_client(monkeypatch, lambda req, d=data, e=enc: httpx.Response(
            200, stream=_Body(d), headers={"content-type": "text/html", "content-encoding": e},
        ))
        r = netguard.fetch("http://z.test/", resolver=_resolver({"z.test": [PUBLIC]}))
        assert r.content == body and not r.truncated, enc


def test_unknown_content_encoding_is_an_error(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(
        200, stream=_Body(b"??"), headers={"content-encoding": "br"},
    ))
    with pytest.raises(netguard.FetchError, match="content-encoding"):
        netguard.fetch("http://z.test/", resolver=_resolver({"z.test": [PUBLIC]}))


def test_gzip_followed_by_junk_is_not_read(server):
    """A gzip member followed by a 50 MB trailer decodes to 9 bytes, so the
    decoded cap never trips; reading stops at the end of the gzip stream."""
    base, kw = _trusted_local(server)
    tracemalloc.start()
    t0 = time.monotonic()
    try:
        r = netguard.fetch(f"{base}/gzip-trailer", max_bytes=CAP, **kw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.content == b"<p>hi</p>" and not r.truncated
    assert peak < 16 * 1024 * 1024
    assert time.monotonic() - t0 < 10
    assert server.big_done.wait(10)
    assert server.sent < 16 * 1024 * 1024  # cut off, not drained


def test_endless_empty_deflate_stream_stops_at_raw_cap(server):
    """Empty stored deflate blocks decode to nothing, forever; the raw byte
    cap ends the read instead of hanging the turn."""
    base, kw = _trusted_local(server)
    out: dict = {}

    def run():
        try:
            out["r"] = netguard.fetch(f"{base}/deflate-empty", max_bytes=CAP, **kw)
        except Exception as exc:  # reported below
            out["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(30)
    if t.is_alive():
        server.stop.set()
        pytest.fail(f"fetch still reading after 30 s ({server.sent / 1e6:.0f} MB raw)")
    assert "exc" not in out, out
    assert out["r"].content == b"" and out["r"].truncated
    assert server.big_done.wait(10)
    assert server.sent < 64 * 1024 * 1024  # stopped near the 4 MB raw cap


def test_slow_body_hits_the_overall_deadline(server):
    """One byte every 20 ms never trips the per-read timeout; the wall-clock
    deadline for the whole fetch does."""
    base, kw = _trusted_local(server)
    out: dict = {}

    def run():
        try:
            out["r"] = netguard.fetch(f"{base}/trickle", timeout=5, total_timeout=0.5, **kw)
        except Exception as exc:
            out["exc"] = exc

    t0 = time.monotonic()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    if t.is_alive():
        server.stop.set()
        pytest.fail("fetch still reading a trickling body after 10 s")
    assert isinstance(out.get("exc"), netguard.FetchError), out
    assert "gave up" in str(out["exc"])
    assert time.monotonic() - t0 < 5


def test_deadline_is_checked_between_hops(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(netguard.time, "monotonic", lambda: now[0])
    requested = []

    def handler(req):
        requested.append(req.url.path)
        now[0] = 100.0  # this hop took 100 s
        return _chain_handler(req)

    _mock_client(monkeypatch, handler)
    with pytest.raises(netguard.FetchError, match="gave up"):
        netguard.fetch("http://chain.test/r/2", total_timeout=60,
                       resolver=_resolver({"chain.test": [PUBLIC]}))
    assert requested == ["/r/2"]  # the redirect was never requested


def test_each_connect_attempt_is_held_to_the_deadline(monkeypatch):
    """Addresses that never answer each get the time left, not a fresh full
    timeout, and the ones after the deadline aren't tried."""
    now = [0.0]
    monkeypatch.setattr(netguard.time, "monotonic", lambda: now[0])
    tried = []

    def handler(req):
        tried.append((req.url.host, req.extensions["timeout"]["connect"]))
        now[0] += 7  # no answer for 7 s
        raise httpx.ConnectTimeout("timed out", request=req)

    _mock_client(monkeypatch, handler)
    resolver = _resolver({"many.test": [PUBLIC, PUBLIC2, "93.184.215.16"]})
    with pytest.raises(netguard.FetchError, match="gave up"):
        netguard.fetch("http://many.test/", timeout=30, total_timeout=10, resolver=resolver)
    assert tried == [(PUBLIC, 10), (PUBLIC2, 3)]


def _endless_1xx(conn, srv):
    msg = b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\n\r\n"
    while not srv.stop.is_set():
        conn.sendall(msg)
        srv.sent += len(msg)


def _header_drip(conn, srv):
    conn.sendall(b"HTTP/1.1 200 OK\r\nX-Pad: ")
    while not srv.stop.is_set():
        conn.sendall(b"a")
        srv.sent += 1
        time.sleep(0.05)


@pytest.mark.parametrize("respond", [_endless_1xx, _header_drip], ids=["endless-1xx", "header-drip"])
def test_response_headers_are_held_to_the_deadline(raw_server, respond):
    """Endless 103 Early Hints, or the headers one byte at a time: the server
    never goes quiet, so the per-read timeout never trips, and the fetch's
    wall-clock deadline has to end the wait for the final response."""
    raw_server.respond = respond
    url = f"http://slow.test:{raw_server.port}/"
    kw = {"trusted_domains": ["slow.test"], "resolver": _resolver({"slow.test": ["127.0.0.1"]})}
    out, took = _run_bounded(lambda: netguard.fetch(url, timeout=5, total_timeout=1, **kw), 15)
    assert isinstance(out, netguard.FetchError) and "gave up" in str(out), out
    assert took < 4  # the 1 s deadline, plus slack for a busy test machine
    assert raw_server.sent > 0  # the server really was still sending


def test_deadline_hook_wraps_plain_and_tls_streams():
    """The hook clamps both the TCP stream and the one after the TLS
    handshake, and fails a read once the time is gone."""
    limit = netguard._Deadline(30)
    seen = []

    class Stream:
        def read(self, n, timeout=None):
            seen.append(timeout)
            return b"x"

        def write(self, data, timeout=None):
            seen.append(timeout)

    for event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
        stream = Stream()
        limit.trace(event, {"return_value": stream})
        stream.read(10, timeout=5)
        stream.write(b"x", timeout=60)
    assert seen[0] == 5 and seen[1] <= 30 and seen[2] == 5 and seen[3] <= 30
    limit.trace("connection.connect_tcp.started", {})  # other events are ignored
    limit.at = time.monotonic() - 1
    with pytest.raises(netguard.FetchError):
        stream.read(10, timeout=5)


# ---- acceptance: IPv6 and v4-mapped forms -----------------------------------


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.255.255.254", "10.1.2.3", "172.16.0.1", "172.31.255.255",
    "192.168.1.1", "169.254.169.254", "169.254.1.1", "100.64.0.1", "100.100.100.200",
    "0.0.0.0", "224.0.0.1", "255.255.255.255", "240.0.0.1", "192.0.2.1",
    "168.63.129.16",  # Azure WireServer
    # IPv6
    "::1", "::", "fe80::1", "fe80::1%5", "fc00::1", "fd12:3456::1", "fd00:ec2::254",
    "fec0::1", "ff02::1", "64:ff9b:1::1",
    # IPv4 in IPv6 clothing
    "::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:169.254.169.254", "::ffff:10.0.0.1",
    "::ffff:192.168.0.1", "::ffff:100.64.0.1", "::127.0.0.1", "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe", "2002:7f00:1::", "2002:c0a8:101::1",
])
def test_non_public_addresses_are_flagged(addr):
    assert netguard.address_problem(addr) is not None


@pytest.mark.parametrize("addr", [
    "8.8.8.8", "93.184.215.14", "1.1.1.1", "2606:4700:4700::1111",
    "2001:4860:4860::8888", "::ffff:8.8.8.8", "64:ff9b::808:808",
])
def test_public_addresses_pass(addr):
    assert netguard.address_problem(addr) is None


def test_azure_wireserver_is_a_metadata_endpoint():
    assert netguard.address_problem("168.63.129.16") == "cloud metadata endpoint"
    assert netguard.address_problem("::ffff:168.63.129.16").startswith("cloud metadata")


def test_v4_mapped_reason_names_the_ipv4_address():
    assert netguard.address_problem("::ffff:169.254.169.254") == (
        "cloud metadata endpoint, IPv6 form of 169.254.169.254"
    )
    assert netguard.address_problem("::ffff:7f00:1") == "loopback, IPv6 form of 127.0.0.1"


@pytest.mark.parametrize("url", [
    "http://[::1]:8473/api/config",
    "http://[::ffff:127.0.0.1]:8473/",
    "http://[::ffff:7f00:1]/",
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://[fe80::1]/",
    "http://[64:ff9b::a9fe:a9fe]/",
])
def test_ipv6_literal_urls_are_refused(url, no_connect):
    with pytest.raises(netguard.Blocked):
        netguard.fetch(url)


@pytest.mark.parametrize("addrs", [
    ["::1"], ["::ffff:10.0.0.1"], ["fd00::5"], [PUBLIC, "127.0.0.1"], ["2606:4700::1", "fe80::1"],
])
def test_hostnames_resolving_to_private_ipv6_are_refused(addrs, no_connect):
    with pytest.raises(netguard.Blocked):
        netguard.fetch("http://sneaky.test/", resolver=_resolver({"sneaky.test": addrs}))


def test_localhost_name_is_refused(no_connect):
    # Real resolver: localhost is loopback on every platform.
    with pytest.raises(netguard.Blocked, match="loopback"):
        netguard.fetch("http://localhost:8473/api/config")


# ---- DNS pinning ------------------------------------------------------------


def test_connection_is_pinned_to_the_vetted_address(server, monkeypatch):
    """The request goes to the address the guard checked, with the original
    Host header; httpx never resolves the hostname itself."""
    port = server.server_address[1]
    real_gai = socket.getaddrinfo

    def gai(host, *a, **k):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise AssertionError(f"hostname {host!r} was resolved outside the guard")
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    r = netguard.fetch(
        f"http://pinned.test:{port}/host",
        trusted_domains=["pinned.test"],
        resolver=_resolver({"pinned.test": ["127.0.0.1"]}),
    )
    assert r.content == f"pinned.test:{port}".encode()
    assert server.hits == [("/host", f"pinned.test:{port}")]


def test_env_proxy_is_not_used(server, monkeypatch):
    """A proxy would connect wherever it likes, so HTTP(S)_PROXY is ignored."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    base, kw = _trusted_local(server)
    assert netguard.fetch(f"{base}/page", **kw).status == 200


def test_rebinding_resolver_cannot_swap_the_address(monkeypatch):
    """A resolver that answers public first and loopback afterwards (DNS
    rebinding) is asked once per host; every hop goes to the vetted address."""
    answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
    calls: list[str] = []

    def rebinding(host, port):
        calls.append(host)
        return next(answers)

    seen = []

    def handler(req):
        seen.append((req.url.host, req.headers["host"]))
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, stream=_Body(b"ok"), headers={"content-type": "text/plain"})

    _mock_client(monkeypatch, handler)
    r = netguard.fetch("http://rebind.test/start", resolver=rebinding)
    assert r.content == b"ok"
    assert calls == ["rebind.test"]
    assert seen == [(PUBLIC, "rebind.test"), (PUBLIC, "rebind.test")]


def test_https_keeps_hostname_for_tls(monkeypatch):
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["host"], req.extensions.get("sni_hostname")))
        return httpx.Response(200, stream=_Body(b"ok"))

    _mock_client(monkeypatch, handler)
    netguard.fetch("https://tls.test:8443/x", resolver=_resolver({"tls.test": ["2606:4700::1"]}))
    assert seen == [("https://[2606:4700::1]:8443/x", "tls.test:8443", "tls.test")]


def test_next_address_tried_when_first_refuses(monkeypatch):
    tried = []

    def handler(req):
        tried.append(req.url.host)
        if req.url.host == PUBLIC:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, stream=_Body(b"ok"))

    _mock_client(monkeypatch, handler)
    r = netguard.fetch("http://two.test/", resolver=_resolver({"two.test": [PUBLIC, PUBLIC2]}))
    assert r.content == b"ok" and tried == [PUBLIC, PUBLIC2]


def test_unresolvable_host_is_a_fetch_error():
    with pytest.raises(netguard.FetchError, match="could not resolve"):
        netguard.fetch("http://nowhere.test/", resolver=_resolver({}))


def test_resolve_failure_mentions_an_ignored_proxy(monkeypatch):
    """On a proxy-only network web_fetch fails to resolve or connect; the
    error says the proxy is deliberately not used."""
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"http": "http://proxy.corp:3128"})
    with pytest.raises(netguard.FetchError, match=r"HTTP\(S\)_PROXY"):
        netguard.fetch("http://nowhere.test/", resolver=_resolver({}))
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    with pytest.raises(netguard.FetchError) as exc:
        netguard.fetch("http://nowhere.test/", resolver=_resolver({}))
    assert "proxy" not in str(exc.value).lower()


def test_connect_failure_mentions_an_ignored_proxy(monkeypatch):
    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    _mock_client(monkeypatch, refuse)
    resolver = _resolver({"far.test": [PUBLIC]})
    monkeypatch.setattr("urllib.request.getproxies", lambda: {"https": "http://proxy.corp:3128"})
    with pytest.raises(netguard.FetchError, match=r"far\.test.*HTTP\(S\)_PROXY"):
        netguard.fetch("https://far.test/", resolver=resolver)
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})
    with pytest.raises(httpx.ConnectError):
        netguard.fetch("https://far.test/", resolver=resolver)


# ---- redirect loop -------------------------------------------------------------


def _chain_handler(req):
    n = int(req.url.path.rsplit("/", 1)[1])
    if n:
        return httpx.Response(302, headers={"location": f"/r/{n - 1}"})
    return httpx.Response(200, stream=_Body(b"end"), headers={"content-type": "text/plain"})


def test_at_most_five_redirects(monkeypatch):
    _mock_client(monkeypatch, _chain_handler)
    resolver = _resolver({"chain.test": [PUBLIC]})
    r = netguard.fetch("http://chain.test/r/5", resolver=resolver)
    assert r.content == b"end" and len(r.redirects) == 5
    with pytest.raises(netguard.FetchError, match="too many redirects"):
        netguard.fetch("http://chain.test/r/6", resolver=resolver)


def test_redirect_to_other_scheme_is_refused(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(
        302, headers={"location": "file:///etc/passwd"},
    ))
    with pytest.raises(netguard.Blocked, match="only http"):
        netguard.fetch("http://x.test/", resolver=_resolver({"x.test": [PUBLIC]}))


def test_check_redirect_runs_before_each_hop(monkeypatch):
    requested = []

    def handler(req):
        requested.append(req.headers["host"])
        if req.headers["host"] == "a.test":
            return httpx.Response(302, headers={"location": "http://b.test/x"})
        return httpx.Response(200, stream=_Body(b"b"))

    _mock_client(monkeypatch, handler)
    resolver = _resolver({"a.test": [PUBLIC], "b.test": [PUBLIC2]})
    hops = []
    r = netguard.fetch("http://a.test/", resolver=resolver,
                       check_redirect=lambda s, d: hops.append((s, d)))
    assert r.content == b"b" and hops == [("http://a.test/", "http://b.test/x")]

    requested.clear()
    with pytest.raises(netguard.Blocked, match="nope"):
        netguard.fetch("http://a.test/", resolver=resolver, check_redirect=lambda s, d: "nope")
    assert requested == ["a.test"]  # b.test never contacted


def test_cookies_follow_redirects_by_hostname(monkeypatch):
    def handler(req):
        if req.url.path == "/login":
            return httpx.Response(302, headers={"location": "/home", "set-cookie": "sid=abc; Path=/"})
        return httpx.Response(200, stream=_Body(req.headers.get("cookie", "").encode()))

    _mock_client(monkeypatch, handler)
    r = netguard.fetch("http://c.test/login", resolver=_resolver({"c.test": [PUBLIC]}))
    assert r.content == b"sid=abc"


def test_error_status_body_is_not_read(monkeypatch):
    stream = _CountingStream(BIG, b"x" * 65536)
    _mock_client(monkeypatch, lambda req: httpx.Response(404, stream=stream))
    r = netguard.fetch("http://e.test/", resolver=_resolver({"e.test": [PUBLIC]}))
    assert r.status == 404 and r.content == b"" and stream.pulled == 0
