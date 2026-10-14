"""Guarded HTTP fetch for URLs the model chooses (SSRF guard).

`web_fetch` takes its URL from the model, and the page it lands on can redirect
anywhere. Left alone, a page (or a trusted host's open redirect) can point it at
eVi's own API on 127.0.0.1:8000 or :8473, a LAN service, or the cloud metadata
endpoint 169.254.169.254. `fetch()` closes that:

- Redirects are followed by hand, at most `MAX_REDIRECTS`, and the caller's
  `check_redirect` (the permission re-check) runs before each one is followed.
- Each hop's host is resolved once and EVERY address it resolves to is vetted:
  loopback, private (RFC 1918), link-local (incl. cloud metadata), CGNAT,
  unique-local / site-local IPv6, multicast and the other non-public ranges,
  including their IPv4-mapped, IPv4-compatible, NAT64 and 6to4 IPv6 forms, are
  refused unless the host is in ``[auto] trusted_domains`` (or the caller's
  `confirm` says yes; no `confirm` means refuse, so unattended runs fail closed).
- Two refusals hold even for trusted hosts: eVi's own web API ports on this
  machine (`EVI_API_PORTS` plus any `register_api_port`; trusting ``localhost``
  for a local SearXNG must not open eVi's API), and a redirect from a public
  address to a non-public one (a page on the internet must not steer the fetch
  into the LAN or loopback).
- The connection then goes to the vetted address itself (pinned: no second DNS
  lookup, so DNS rebinding can't swap it), with the original Host header and
  TLS server name, so certificates are still checked against the hostname.
- The body is streamed and decoded under `max_bytes`; it is never read whole,
  and a compressed body can't inflate past the cap either. The raw stream is
  capped too (a compressed body can decode to almost nothing however long it
  is), reading stops at the end of the compressed stream, and the whole fetch
  has a wall-clock deadline, held on every socket read and write (so endless
  1xx responses or headers sent a byte at a time can't stretch it either).

A refusal raises `Blocked`; its message is the reason, written for the model.
Environment proxies (HTTP(S)_PROXY) are not used: the guard has to see the
address it connects to.
"""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable

import httpcore
import httpx

from evi.federation import DEFAULT_PEER_PORT
from evi.permissions import _host_trusted

MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# `evi web`'s default --port (evi/apps/cli/main.py web(); a test keeps the two
# in step).
EVI_WEB_PORT = 8000
# eVi's own web API: `evi web`'s default port, and the desktop shell and
# federation default. Nothing on them is fetched on this machine's own
# addresses, whatever trusted_domains says. A server on another port adds it
# with `register_api_port`.
EVI_API_PORTS = frozenset({EVI_WEB_PORT, DEFAULT_PEER_PORT})
_registered_ports: set[int] = set()
# However short the decoded body, at most this many raw (still compressed)
# bytes are read: real compressed data never takes much more raw bytes than it
# decodes to, so only an endless or padded stream ever gets here.
_MIN_RAW_CAP = 4 * 1024 * 1024

# Checked in order; the first range that contains the address names it.
_V4_RANGES = tuple(
    (ipaddress.IPv4Network(net), why)
    for net, why in (
        ("0.0.0.0/8", "unspecified"),
        ("10.0.0.0/8", "private (RFC 1918)"),
        ("100.64.0.0/10", "carrier-grade NAT (RFC 6598)"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local"),
        ("172.16.0.0/12", "private (RFC 1918)"),
        ("192.168.0.0/16", "private (RFC 1918)"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
    )
)
_V6_RANGES = tuple(
    (ipaddress.IPv6Network(net), why)
    for net, why in (
        ("::/128", "unspecified"),
        ("::1/128", "loopback"),
        ("fc00::/7", "unique local (fc00::/7)"),
        ("fe80::/10", "link-local"),
        ("fec0::/10", "site-local"),
        ("ff00::/8", "multicast"),
        ("64:ff9b:1::/48", "local-use NAT64"),
    )
)
# IPv6 prefixes that carry an IPv4 address in their low 32 bits: the NAT64
# well-known prefix and the deprecated IPv4-compatible ::a.b.c.d form.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_V4_COMPAT = ipaddress.IPv6Network("::/96")
# Cloud metadata services (AWS/GCP/Azure/OCI, AWS over IPv6, Alibaba, and the
# Azure WireServer, which sits on a public-looking address).
_METADATA = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "168.63.129.16")
)


class FetchError(Exception):
    """The fetch could not complete (bad URL, DNS failure, too many redirects…)."""


class Blocked(FetchError):
    """The guard refused a hop. The message is the reason, for the model."""


@dataclass
class FetchResult:
    url: str  # the final URL, after redirects
    status: int
    headers: httpx.Headers
    content: bytes = b""  # decoded body, at most max_bytes (empty for >= 400)
    truncated: bool = False  # the body went past max_bytes
    redirects: list[str] = field(default_factory=list)  # each hop followed


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address stands for (mapped, 6to4, NAT64 or
    IPv4-compatible), or None."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64 or ip in _V4_COMPAT:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_problem(addr: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why `addr` is not a public internet address (e.g. ``"loopback"``), or
    None when it is one."""
    ip = ipaddress.ip_address(addr) if isinstance(addr, str) else addr
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        ip = ipaddress.IPv6Address(str(ip).split("%", 1)[0])
    if ip in _METADATA:
        return "cloud metadata endpoint"
    if isinstance(ip, ipaddress.IPv6Address):
        for net, why in _V6_RANGES:
            if ip in net:
                return why
        v4 = _embedded_v4(ip)
        if v4 is not None:
            inner = address_problem(v4)
            return f"{inner}, IPv6 form of {v4}" if inner else None
    else:
        for net, why in _V4_RANGES:
            if ip in net:
                return why
    if not ip.is_global:
        return "reserved, not publicly routable"
    return None


def register_api_port(port: int) -> None:
    """Treat `port` on this machine as eVi's own API too, like `EVI_API_PORTS`.
    For a server bound to a non-default port to call at startup (web_fetch runs
    inside the server process for web sessions)."""
    _registered_ports.add(int(port))


def is_api_port(port: int) -> bool:
    """True if `port` on this machine is (or may be) eVi's own API."""
    return port in EVI_API_PORTS or port in _registered_ports


def resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, in resolver order, without repeats."""
    out: list[str] = []
    for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr[0] not in out:
            out.append(sockaddr[0])
    return out


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def host_trusted(url: httpx.URL, trusted_domains: Iterable[str]) -> bool:
    """True if the URL's host is in `trusted_domains`. A hostname matches an
    entry or a subdomain of it (as `permissions` auto-approval does); an IP
    literal only matches the same address."""
    domains = [d for d in (trusted_domains or ()) if d and d.strip()]
    host = url.raw_host.decode("ascii", "replace")
    ip = _ip(host)
    if ip is None:
        return _host_trusted([str(url)], domains)
    return any(_ip(d) == ip for d in domains)


def _parse(url: str | httpx.URL) -> httpx.URL:
    try:
        u = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise FetchError(f"invalid URL {url!r}: {exc}") from None
    if u.scheme not in ("http", "https"):
        raise Blocked(f"only http(s) URLs can be fetched, not {u.scheme or 'no'} scheme ({u})")
    if not u.raw_host:
        raise FetchError(f"URL has no host: {u}")
    return u


def _problem(addr: str) -> str | None:
    """`address_problem` for a resolver's answer; junk counts as non-public."""
    try:
        return address_problem(addr)
    except ValueError:
        return "not a valid IP address"


def _proxy_hint(url: httpx.URL) -> str:
    """A note for a resolve/connect failure when a proxy is configured, since
    web_fetch deliberately bypasses it; empty otherwise."""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return ""
    if not (proxies.get(url.scheme) or proxies.get("all")):
        return ""
    return (
        " (a proxy is configured, but web_fetch does not use HTTP(S)_PROXY: its "
        "SSRF guard has to see the address it connects to, so it connects directly)"
    )


def _vet(
    url: httpx.URL,
    trusted_domains: Iterable[str],
    confirm: Callable[[str, str], bool] | None,
    resolver: Callable[[str, int], list[str]],
) -> list[str]:
    """The addresses this hop may connect to, or raise `Blocked`/`FetchError`."""
    host = url.raw_host.decode("ascii", "replace")
    port = url.port or (443 if url.scheme == "https" else 80)
    literal = _ip(host) is not None
    if literal:
        addresses = [host]
    else:
        try:
            addresses = resolver(host, port)
        except (OSError, UnicodeError) as exc:
            raise FetchError(f"could not resolve {host}: {exc}{_proxy_hint(url)}") from None
        if not addresses:
            raise FetchError(f"could not resolve {host}: no addresses{_proxy_hint(url)}")
    problems = [(addr, _problem(addr)) for addr in addresses]
    if is_api_port(port):
        for addr, why in problems:
            if why and why.startswith(("loopback", "unspecified")):
                shown = f"{host} is" if literal else f"{host} resolves to {addr},"
                raise Blocked(
                    f"{shown} this machine ({why}), and port {port} here is reserved for "
                    f"eVi's own web API. web_fetch never fetches it, not even for a host "
                    f"in trusted_domains; another local service has to use another port "
                    f"to be fetched."
                )
    for addr, why in problems:
        if why is None:
            continue
        if host_trusted(url, trusted_domains):
            break  # the user trusts this host, wherever it lives
        shown = f"{host} is" if literal else f"{host} resolves to {addr},"
        reason = (
            f"{shown} a non-public address ({why}). Loopback, LAN, link-local and "
            f"cloud-metadata addresses are only fetched for hosts listed in [auto] "
            f"trusted_domains in the eVi config, and {host} is not; add it there (a "
            f"config edit by the user) if it is meant to be reachable."
        )
        approved = False
        if confirm is not None:
            try:
                approved = bool(confirm(host, reason))
            except Exception:
                approved = False
        if not approved:
            raise Blocked(reason)
        break
    return addresses


class _Deadline:
    """The fetch's wall-clock limit, held on the network itself.

    httpx's timeouts are per operation, so a server that sends 1xx responses
    forever, or the response headers a byte at a time, never trips them.
    `trace` is an httpx "trace" request extension: it gets each new
    connection's network stream (after the TCP connect, and again after the
    TLS handshake) and clamps every read and write on it to the time left,
    failing them with `FetchError(message)` once that is gone.
    """

    def __init__(self, seconds: float) -> None:
        self.at = time.monotonic() + seconds
        self.message = "the fetch took too long"  # set per hop by fetch()

    def left(self) -> float:
        """Seconds left; raises `FetchError` once there are none."""
        left = self.at - time.monotonic()
        if left <= 0:
            raise FetchError(self.message)
        return left

    def trace(self, event: str, info: dict) -> None:
        if event not in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            return
        stream = info.get("return_value")
        try:
            stream.read = self._bound(stream.read)
            stream.write = self._bound(stream.write)
        except (AttributeError, TypeError):
            pass  # an httpcore of another shape: the per-operation timeouts still hold

    def _bound(self, op: Callable) -> Callable:
        def call(data, timeout=None):
            left = self.left()
            clamped = timeout is None or left < timeout
            try:
                return op(data, timeout=left if clamped else timeout)
            except httpcore.TimeoutException:
                if clamped:  # it was the deadline, not the per-operation timeout
                    raise FetchError(self.message) from None
                raise

        return call


class _CappedDecoder:
    """Decode a Content-Encoding without ever producing more than asked for."""

    def __init__(self, encoding: str) -> None:
        enc = encoding.strip().lower()
        self._deflate = enc == "deflate"
        self._first = True
        if enc in ("", "identity"):
            self._z = None
        elif enc in ("gzip", "x-gzip", "deflate"):
            self._z = zlib.decompressobj(zlib.MAX_WBITS | 32)  # gzip or zlib header
        else:
            raise FetchError(f"unsupported content-encoding: {encoding}")

    def decode(self, chunk: bytes, limit: int) -> bytes:
        """Up to `limit` (>= 1) decoded bytes of `chunk`; check `pending` after."""
        if self._z is None:
            return chunk[:limit]
        first, self._first = self._first, False
        try:
            return self._z.decompress(chunk, limit)
        except zlib.error:
            if not (first and self._deflate):
                raise FetchError("could not decode the compressed body") from None
            # Some servers send "deflate" as a raw stream with no zlib header.
            self._z = zlib.decompressobj(-zlib.MAX_WBITS)
            return self._z.decompress(chunk, limit)

    def flush(self, limit: int) -> bytes:
        """What is left once the body has ended (at most `limit` bytes)."""
        return self._z.flush()[:limit] if self._z is not None else b""

    @property
    def pending(self) -> bool:
        """Input is left over because the output limit was reached."""
        return bool(self._z is not None and self._z.unconsumed_tail)

    @property
    def finished(self) -> bool:
        """The compressed stream has ended; anything after it is ignored."""
        return bool(self._z is not None and self._z.eof)


def _read_capped(
    resp: httpx.Response, max_bytes: int, limit: _Deadline | None = None
) -> tuple[bytes, bool]:
    """Stream the decoded body, stopping as soon as it passes `max_bytes`, the
    raw stream passes its own cap, or the compressed stream ends. Past the
    `limit` it raises `FetchError`."""
    if resp.is_stream_consumed:  # already in memory (a mock transport's response)
        body = resp.content
        return body[:max_bytes], len(body) > max_bytes
    decoder = _CappedDecoder(resp.headers.get("content-encoding", ""))
    want = max_bytes + 1  # one byte past the cap tells us it was truncated
    raw_cap = max(4 * max_bytes, _MIN_RAW_CAP)
    raw = 0
    buf = bytearray()
    cut = False  # stopped before the body ended
    for chunk in resp.iter_raw():
        raw += len(chunk)
        buf += decoder.decode(chunk, want - len(buf))
        if len(buf) >= want or decoder.pending or raw >= raw_cap:
            cut = True
            break
        if decoder.finished:  # e.g. gzip followed by 50 MB of junk: not read
            break
        if limit is not None:
            limit.left()
    else:
        buf += decoder.flush(want - len(buf))
    truncated = len(buf) > max_bytes or (cut and not decoder.finished)
    return bytes(buf[:max_bytes]), truncated


def _send(
    client: httpx.Client,
    url: httpx.URL,
    addresses: list[str],
    headers: dict[str, str],
    limit: _Deadline | None = None,
) -> httpx.Response:
    """GET `url` from the first of `addresses` that accepts a connection,
    connecting to the address itself (no DNS) with the original Host header and
    TLS server name. Each attempt, and all I/O after it, stays within `limit`."""
    host_header = url.netloc.decode("ascii")
    sni = url.raw_host.decode("ascii")
    last: Exception | None = None
    for addr in addresses:
        pinned = url.copy_with(host=addr.split("%", 1)[0])
        extensions: dict = {"sni_hostname": sni} if url.scheme == "https" else {}
        timeout = client.timeout
        if limit is not None:
            extensions["trace"] = limit.trace
            # One unanswering address must not use up the next one's time.
            left = limit.left()
            connect = left if timeout.connect is None else min(timeout.connect, left)
            timeout = httpx.Timeout(
                timeout.read, connect=connect, write=timeout.write, pool=timeout.pool
            )
        request = client.build_request(
            "GET",
            pinned,
            headers={**headers, "Host": host_header},
            extensions=extensions,
            timeout=timeout,
        )
        try:
            return client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last = exc  # nothing was sent; try the host's next address
    assert last is not None
    hint = _proxy_hint(url)
    if hint:
        raise FetchError(f"could not connect to {sni}: {last}{hint}") from last
    raise last


def fetch(
    url: str,
    *,
    trusted_domains: Iterable[str] = (),
    check_redirect: Callable[[str, str], str | None] | None = None,
    confirm: Callable[[str, str], bool] | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int = 1_000_000,
    max_redirects: int = MAX_REDIRECTS,
    timeout: float = 30.0,
    total_timeout: float = 60.0,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> FetchResult:
    """GET `url` under the guard described in the module docstring.

    `check_redirect(from_url, to_url)` returns a refusal reason (str) or None to
    follow; `confirm(host, reason)` may approve a non-public address for an
    untrusted host (None = refuse). `timeout` bounds each network operation and
    `total_timeout` the whole fetch (every read and write is held to it).
    Raises `Blocked` on a refusal and `FetchError` / `httpx.HTTPError` on other
    failures. Bodies of responses with status >= 400 are not read.
    """
    domains = list(trusted_domains or ())
    resolve_fn = resolver or resolve
    base_headers = {"Accept-Encoding": "gzip, deflate", **(headers or {})}
    jar = httpx.Cookies()  # cookies follow the hops, keyed by hostname not address
    pins: dict[tuple[str, int | None], list[str]] = {}
    current = _parse(url)
    followed: list[str] = []
    limit = _Deadline(total_timeout)
    went_public = False  # some earlier hop was served from a public address
    came_from = ""  # the URL that redirected to `current`
    while True:
        limit.message = f"gave up on {current}: the fetch took longer than {total_timeout:g} s"
        limit.left()
        key = (current.raw_host.decode("ascii", "replace"), current.port)
        if key not in pins:
            pins[key] = _vet(current, domains, confirm, resolve_fn)
        addresses = pins[key]
        if went_public:
            # A public page must not steer the fetch into loopback or the LAN,
            # trusted host or not, so only the host's public addresses are kept.
            private = [(a, why) for a in addresses if (why := _problem(a))]
            addresses = [a for a in addresses if _problem(a) is None]
            if not addresses:
                addr, why = private[0]
                raise Blocked(
                    f"{came_from} redirected to {current}, and {key[0]} is on a "
                    f"non-public address ({addr}: {why}). A redirect from a public "
                    f"address is never followed to loopback, LAN, link-local or "
                    f"cloud-metadata addresses, even for a host in trusted_domains, so "
                    f"it was not followed; fetch that URL directly only if the user "
                    f"asked for it."
                )
        logical = httpx.Request("GET", current)
        jar.set_cookie_header(logical)
        hop_headers = dict(base_headers)
        if "cookie" in logical.headers:
            hop_headers["Cookie"] = logical.headers["cookie"]
        # A fresh client per hop: a pooled connection is keyed by address, so it
        # must never carry one host's TLS session over to another host. An
        # explicit transport keeps env proxies out (the guard must see the
        # address it connects to) while still honouring SSL_CERT_FILE/DIR.
        with httpx.Client(
            timeout=timeout, follow_redirects=False, transport=httpx.HTTPTransport(),
        ) as client:
            resp = _send(client, current, addresses, hop_headers, limit)
            try:
                # The request went to the pinned address itself.
                if _problem(resp.request.url.host) is None:
                    went_public = True
                jar.extract_cookies(
                    httpx.Response(resp.status_code, headers=resp.headers, request=logical)
                )
                location = resp.headers.get("location")
                if resp.status_code in _REDIRECT_CODES and location:
                    if len(followed) >= max_redirects:
                        raise FetchError(f"too many redirects (more than {max_redirects})")
                    try:
                        target = current.join(location)
                    except (httpx.InvalidURL, ValueError) as exc:
                        raise FetchError(f"bad redirect Location {location!r}: {exc}") from None
                    nxt = _parse(target)
                    if check_redirect is not None:
                        refusal = check_redirect(str(current), str(nxt))
                        if refusal:
                            raise Blocked(refusal)
                    followed.append(str(nxt))
                    came_from, current = str(current), nxt
                    continue
                result = FetchResult(
                    url=str(current), status=resp.status_code, headers=resp.headers,
                    redirects=followed,
                )
                if resp.status_code < 400:
                    result.content, result.truncated = _read_capped(resp, max_bytes, limit)
                return result
            finally:
                resp.close()
