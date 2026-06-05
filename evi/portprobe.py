"""Lightweight probing of local OpenAI-compatible LLM servers.

Shared by the web frontend's backend-availability UX and by the llama.cpp
backend's port-fallback discovery. Deliberately dependency-light and fast:

* `port_open` does a raw-socket connect so a closed/filtered loopback port
  fails in milliseconds instead of stalling for the multi-second
  ConnectTimeout httpx incurs on Windows' dual-stack (`::1` first) loopback.
* `is_openai_server` confirms not just that *something* answers, but that it
  answers like an OpenAI `/v1/models` endpoint (200 + a JSON ``data`` list) —
  so an unrelated service squatting on the port (e.g. a dev server returning
  a 404 HTML page) is not mistaken for an LLM backend.
* `discover_llamacpp_url` scans a small port span (8080..8090 by default) for
  a real llama.cpp server, so a busy default port doesn't hide it.
"""

from __future__ import annotations

import urllib.parse

# llama.cpp's default is 8080; when that's taken people commonly bump to the
# next free port. Scan the default plus the next ten.
LLAMACPP_PORT_SPAN = 10


def split_host_port(base_url: str) -> tuple[str, int]:
    """Extract (host, port) from a base URL, forcing 127.0.0.1 for localhost.

    On Windows, `localhost` resolves to IPv6 `::1` first; a connect to a
    closed `::1` port is *dropped* (SYN filtered) rather than refused, so the
    attempt blocks for the full timeout before falling back to IPv4. Pinning
    to 127.0.0.1 keeps probes (and connections) fast.
    """
    u = urllib.parse.urlparse(base_url if "://" in base_url else "http://" + base_url)
    host = u.hostname or "127.0.0.1"
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port


def with_port(base_url: str, port: int) -> str:
    """Rebuild `base_url` with a different port (host normalised to IPv4)."""
    u = urllib.parse.urlparse(base_url if "://" in base_url else "http://" + base_url)
    host = u.hostname or "127.0.0.1"
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    scheme = u.scheme or "http"
    path = u.path or ""
    return f"{scheme}://{host}:{port}{path}"


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    """Fast 'is anything listening here' check via a raw socket connect.

    An open port returns instantly; a closed/filtered one fails within
    `timeout`.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def is_openai_server(
    base_url: str,
    *,
    api_key: str | None = None,
    connect_timeout: float = 0.5,
    read_timeout: float = 1.5,
) -> bool:
    """True only if a working OpenAI-compatible LLM server answers here.

    Two-stage: a fast socket check that the port is open at all, then
    GET <base_url>/models requiring a 200 with an OpenAI-shaped body
    (``{"data": [...]}``).
    """
    host, port = split_host_port(base_url)
    if not port_open(host, port):
        return False

    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    # Request against the normalised (127.0.0.1) host too — not just the socket
    # check — so a `localhost` URL doesn't pay the IPv6-first stall on Windows.
    url = with_port(base_url, port).rstrip("/") + "/models"
    try:
        resp = httpx.get(url, headers=headers, timeout=httpx.Timeout(read_timeout, connect=connect_timeout))
    except Exception:  # noqa: BLE001
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False
    return isinstance(body, dict) and isinstance(body.get("data"), list)


def discover_llamacpp_url(
    base_url: str,
    *,
    span: int = LLAMACPP_PORT_SPAN,
    api_key: str | None = None,
) -> str | None:
    """Find a real llama.cpp server near `base_url`'s port.

    Scans ``port .. port+span`` (e.g. 8080..8090). Returns the first URL that
    answers like an OpenAI server, or None if none do. Open ports are checked
    concurrently first, then HTTP-probed in ascending order so the lowest
    working port wins.
    """
    from concurrent.futures import ThreadPoolExecutor

    host, start = split_host_port(base_url)
    ports = [start + i for i in range(span + 1)]

    with ThreadPoolExecutor(max_workers=len(ports)) as ex:
        open_flags = list(ex.map(lambda p: port_open(host, p), ports))
    open_ports = [p for p, ok in zip(ports, open_flags) if ok]

    for p in open_ports:
        candidate = with_port(base_url, p)
        if is_openai_server(candidate, api_key=api_key):
            return candidate
    return None
