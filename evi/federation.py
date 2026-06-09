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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from evi.config import PEERS_PATH


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
