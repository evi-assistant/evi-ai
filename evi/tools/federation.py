"""Federation tool — delegate a task to a trusted peer eVi.

Off by default (category `federation`, a network capability). Peers are
configured in ~/.evi/peers.json; the peer must have `[federation] serve = true`.
"""

from __future__ import annotations

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
