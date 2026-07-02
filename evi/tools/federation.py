"""Federation tool — delegate a task to a trusted peer eVi.

Off by default (category `federation`, a network capability). Peers are
configured in ~/.evi/peers.json; the peer must have `[federation] serve = true`.
"""

from __future__ import annotations

import json

from evi.tools.base import tool


@tool(
    description=(
        "Delegate a self-contained task to a trusted peer eVi (e.g. a more "
        "powerful machine) and return its answer. Pass the peer name (from "
        "~/.evi/peers.json) and the task. Use for heavy work your local model "
        "struggles with. Returns the peer's final text."
    ),
    category="federation",
    long=True,
)
def delegate_peer(peer: str, task: str) -> str:
    from evi import federation

    p = federation.get_peer(str(peer).strip())
    if p is None:
        return (
            f"ERROR: unknown peer {peer!r}. Configure peers in ~/.evi/peers.json "
            "(or run `evi peer list`)."
        )
    try:
        return federation.delegate(p, str(task))
    except federation.FederationError as exc:
        return f"ERROR: {exc}"


@tool(
    description=(
        "List trusted federation peers and what each can do — name, model, "
        "reachability, and capability flags (vision/tools/reasoning/audio). Call "
        "this BEFORE delegate_peer to pick the right peer for a task (e.g. send a "
        "vision task to a peer whose model has vision). Returns JSON."
    ),
    category="federation",
)
def list_peers() -> str:
    from evi import federation

    peers = federation.load_peers()
    if not peers:
        return "[]  (no peers configured — add them in ~/.evi/peers.json or `evi peer add`)"
    out = []
    for p in peers:
        chk = federation.check_peer(p)
        out.append(
            {
                "name": p.name,
                "url": p.url,
                "reachable": chk["reachable"],
                "model": chk.get("model", ""),
                "capabilities": chk.get("capabilities", {}),
            }
        )
    return json.dumps(out)


@tool(
    description=(
        "Delegate a task to ANY external A2A (Agent2Agent) agent by its JSON-RPC "
        "URL (e.g. https://host/a2a) and return its answer. Use for non-eVi agents "
        "that speak the A2A standard; for your own eVi peers prefer list_peers + "
        "delegate_peer. Pass an optional bearer token if the agent requires auth."
    ),
    category="federation",
    long=True,
)
def delegate_a2a(url: str, task: str, token: str = "") -> str:
    from evi import a2a

    try:
        return a2a.client_send(str(url).strip(), str(task), token=str(token or ""))
    except a2a.A2AError as exc:
        return f"ERROR: {exc}"
