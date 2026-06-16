"""Federation — delegate a task to a trusted peer eVi.

Your eVi can hand a subtask to another eVi (e.g. your GPU box) by POSTing to its
``/api/federate`` endpoint. The peer runs the task on its own agent and returns
the answer — so you keep a small local model for chat and borrow a big remote
one for heavy lifting, without giving anything a shared cloud.

Peers live in ``~/.evi/peers.json`` (so per-peer tokens stay out of synced
config):

    [
      {"name": "gpu", "url": "http://gpu-box:8473", "token": "…"}
    ]

The peer must opt in to answering by setting ``[federation] serve = true``.
Transport is plain HTTP with the peer's web bearer token — no new trust model.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from evi.config import PEERS_PATH

# eVi's default web port — what `evi web` binds and what peers usually run on.
DEFAULT_PEER_PORT = 8473


class FederationError(Exception):
    """A peer is missing/misconfigured or a delegation failed."""


@dataclass
class Peer:
    name: str
    url: str          # base URL of the peer's web server, e.g. http://host:8473
    token: str = ""   # the peer's web bearer token (if it requires auth)


def load_peers(path: Path | None = None) -> list[Peer]:
    """Read ~/.evi/peers.json. Missing/malformed → []; bad entries skipped."""
    p = path or PEERS_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Peer] = []
    for e in data:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        url = str(e.get("url") or "").strip().rstrip("/")
        if not name or not url:
            continue
        out.append(Peer(name=name, url=url, token=str(e.get("token", "")).strip()))
    return out


def get_peer(name: str, peers: list[Peer] | None = None) -> Peer | None:
    n = name.strip().lower()
    for peer in peers if peers is not None else load_peers():
        if peer.name.lower() == n:
            return peer
    return None


def delegate(peer: Peer, task: str, *, mode: str = "", timeout: float = 180.0) -> str:
    """Run `task` on `peer` and return its answer text. Raises FederationError
    on transport/HTTP failure; a peer-side error comes back as 'ERROR: …'."""
    body = json.dumps({"task": task, "mode": mode}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "evi-federation"}
    if peer.token:
        headers["Authorization"] = f"Bearer {peer.token}"
    req = urllib.request.Request(
        f"{peer.url}/api/federate", data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise FederationError(f"peer {peer.name} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise FederationError(f"could not reach peer {peer.name}: {exc}") from exc
    if data.get("error"):
        return f"ERROR: peer {peer.name}: {data['error']}"
    return str(data.get("text", ""))


# --- managing peers.json ---------------------------------------------------


def save_peers(peers: list[Peer], path: Path | None = None) -> None:
    """Write the peer list to ~/.evi/peers.json (pretty, stable order)."""
    p = path or PEERS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([asdict(peer) for peer in peers], indent=2) + "\n",
        encoding="utf-8",
    )


def add_peer(peer: Peer, path: Path | None = None, *, overwrite: bool = False) -> bool:
    """Add (or with `overwrite` replace) a peer by name. False if it exists
    and overwrite is off."""
    peers = load_peers(path)
    for i, existing in enumerate(peers):
        if existing.name.lower() == peer.name.lower():
            if not overwrite:
                return False
            peers[i] = peer
            save_peers(peers, path)
            return True
    peers.append(peer)
    save_peers(peers, path)
    return True


def remove_peer(name: str, path: Path | None = None) -> bool:
    """Remove a peer by name. False if no such peer."""
    peers = load_peers(path)
    kept = [p for p in peers if p.name.lower() != name.strip().lower()]
    if len(kept) == len(peers):
        return False
    save_peers(kept, path)
    return True


# --- discovery -------------------------------------------------------------


def probe_evi(host: str, port: int = DEFAULT_PEER_PORT, timeout: float = 2.0) -> dict | None:
    """Return an eVi health fingerprint for host:port, or None.

    Two stages: a raw-socket connect (fails in milliseconds on closed ports,
    where an HTTP client would stall out), then GET /api/health — which is
    auth-exempt — and require the eVi shape (`ok` + `version`) so a random web
    server on the port isn't mistaken for a peer.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    url = f"http://{host}:{port}"
    req = urllib.request.Request(
        url + "/api/health", headers={"User-Agent": "evi-peer-scan"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    if not (isinstance(data, dict) and data.get("ok") and data.get("version")):
        return None
    return {
        "host": host,
        "url": url,
        "version": str(data.get("version", "")),
        "model": str(data.get("model", "")),
    }


def check_peer(peer: Peer, timeout: float = 3.0) -> dict:
    """Reachability + fingerprint for a configured peer: {reachable, version,
    model}. Never raises — UI/CLI status rows must render regardless."""
    try:
        host, port = _host_port(peer.url)
    except ValueError:
        return {"reachable": False, "version": "", "model": ""}
    info = probe_evi(host, port, timeout=timeout)
    if info is None:
        return {"reachable": False, "version": "", "model": ""}
    return {"reachable": True, "version": info["version"], "model": info["model"]}


def _host_port(url: str) -> tuple[str, int]:
    import urllib.parse

    u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    if not u.hostname:
        raise ValueError(f"bad peer url: {url}")
    return u.hostname, u.port or (443 if u.scheme == "https" else 80)


def local_ipv4() -> str:
    """This machine's primary LAN IPv4 ('' if loopback-only / offline).

    The UDP connect trick learns the outbound interface without sending any
    packets; offline boxes fall back to hostname resolution.
    """
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent
            ip = s.getsockname()[0]
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""
    return "" if (not ip or ip.startswith("127.")) else ip


def _local_subnet_hosts() -> list[str]:
    """The /24 around this machine's primary LAN address (254 hosts).

    Loopback or failure → [] (nothing sensible to sweep).
    """
    ip = local_ipv4()
    if not ip:
        return []
    base = ip.rsplit(".", 1)[0]
    return [f"{base}.{i}" for i in range(1, 255)]


def self_serving_status(
    port: int = DEFAULT_PEER_PORT, *, serve: bool = True, timeout: float = 1.0
) -> dict:
    """Is THIS machine's eVi reachable BY peers as a federation node?

    Probes the local server on both loopback and the LAN address and returns
    ``{status, lan_ip, port, loopback, lan}`` where status is one of:

      off       — ``[federation] serve`` is false (we won't answer peers)
      lan       — reachable on the LAN address (good; peers can reach us)
      loopback  — server is up but bound to 127.0.0.1 ONLY — the common bug:
                  a desktop build older than 0.2.15, or launched with
                  ``--host 127.0.0.1`` (so ``bind_lan`` never took effect)
      down      — serve is on but nothing is listening on the port
    """
    lan_ip = local_ipv4()
    loopback = probe_evi("127.0.0.1", port, timeout=timeout) is not None
    lan = bool(lan_ip) and probe_evi(lan_ip, port, timeout=timeout) is not None
    if not serve:
        status = "off"
    elif lan:
        status = "lan"
    elif loopback:
        status = "loopback"
    else:
        status = "down"
    return {"status": status, "lan_ip": lan_ip, "port": port,
            "loopback": loopback, "lan": lan}


def scan_network(
    port: int = DEFAULT_PEER_PORT,
    *,
    hosts: list[str] | None = None,
    timeout: float = 0.3,
    max_workers: int = 64,
) -> list[dict]:
    """Sweep `hosts` (default: this machine's /24) for eVi instances on `port`.

    Returns [{host, url, version, model}, …] sorted by host. With 64 workers
    and a 0.3 s connect timeout a full /24 finishes in ~1-2 s.
    """
    targets = hosts if hosts is not None else _local_subnet_hosts()
    if not targets:
        return []
    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for info in pool.map(lambda h: probe_evi(h, port, timeout=timeout), targets):
            if info is not None:
                found.append(info)
    return sorted(found, key=lambda d: d["host"])
