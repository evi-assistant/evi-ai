"""Tool names and schemas every provider accepts.

OpenAI-compatible APIs take a tool name only if it matches
^[a-zA-Z0-9_-]{1,64}$, and local grammars choke on JSON-schema features such
as $ref and allOf. eVi's own names ("github.search" for an MCP tool) and an
MCP server's schemas don't always fit, so what goes over the wire is made to.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

_SAFE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_UNSAFE_CHAR = re.compile(r"[^a-zA-Z0-9_-]")


def _digest(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def safe_name(name: str) -> str:
    """`name` as a provider-safe tool name: itself when it already is one;
    otherwise "." becomes "__" (an MCP tool "server.tool" is sent as
    "server__tool") and any other character outside [a-zA-Z0-9_-] becomes
    "_". A result over 64 characters, or an empty one, is cut and ends with
    "_" and 8 hex characters of the name's hash, so it stays unique and the
    same on every turn and run."""
    if _SAFE.fullmatch(name):
        return name
    flat = _UNSAFE_CHAR.sub("_", name.replace(".", "__"))
    if flat and len(flat) <= 64:
        return flat
    return f"{flat[:55]}_{_digest(name)}"


def wire_names(names: Iterable[str]) -> dict[str, str]:
    """Each tool name mapped to the name it goes over the wire as, all
    distinct: safe_name, or, when two names would come out the same ("a.b"
    and "a__b"), each one but a name that is already safe takes the hashed
    form. The same names always give the same map."""
    names = sorted(set(names))
    plain = {n: safe_name(n) for n in names}
    taken: dict[str, int] = {}
    for wire in plain.values():
        taken[wire] = taken.get(wire, 0) + 1
    out: dict[str, str] = {}
    for n in names:
        wire = plain[n]
        if taken[wire] > 1 and wire != n:
            wire = f"{wire[:55]}_{_digest(n)}"
        out[n] = wire
    return out
