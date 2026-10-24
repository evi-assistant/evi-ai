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

# Keywords that describe a schema document rather than a value; local
# grammars reject some of them, and none of them help the model.
_DROPPED = frozenset({"$schema", "$id", "$comment", "$anchor", "$defs", "definitions",
                      "examples", "deprecated", "readOnly", "writeOnly"})
# Deeper than this, a schema is cut to {} (any value): grammar builders
# recurse on it, and a model can't make use of it anyway.
MAX_DEPTH = 8


def _ref_target(ref: str, root: dict) -> dict | None:
    """The schema a local "#/$defs/X" (or "#/definitions/X") reference names."""
    if not ref.startswith("#/"):
        return None
    node: object = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return None
        node = node.get(part.replace("~1", "/").replace("~0", "~"))
    return node if isinstance(node, dict) else None


def _normalize(node: object, root: dict, depth: int, seen: tuple[str, ...]) -> object:
    if isinstance(node, list):
        return [_normalize(v, root, depth, seen) for v in node]
    if not isinstance(node, dict):
        return node
    if depth > MAX_DEPTH:
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _ref_target(ref, root)
        if target is None or ref in seen:  # unresolvable, or recursive: any value
            return {k: v for k, v in node.items() if k != "$ref" and k not in _DROPPED}
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _normalize(merged, root, depth, seen + (ref,))
    out: dict = {}
    for key, value in node.items():
        if key in _DROPPED:
            continue
        if key in ("properties", "patternProperties", "$defs", "definitions"):
            out[key] = ({k: _normalize(v, root, depth + 1, seen) for k, v in value.items()}
                        if isinstance(value, dict) else value)
        elif key in ("items", "additionalProperties", "not", "if", "then", "else",
                     "anyOf", "oneOf", "allOf", "prefixItems"):
            out[key] = _normalize(value, root, depth + 1, seen)
        else:
            out[key] = value
    if isinstance(out.get("allOf"), list):
        parts = [p for p in out.pop("allOf") if isinstance(p, dict)]
        for part in parts:
            for key, value in part.items():
                if key == "properties" and isinstance(value, dict):
                    out["properties"] = {**out.get("properties", {}), **value}
                elif key == "required" and isinstance(value, list):
                    out["required"] = sorted(set(out.get("required", [])) | set(value))
                else:
                    out.setdefault(key, value)
    for key in ("anyOf", "oneOf"):
        branches = out.get(key)
        if isinstance(branches, list):
            rest = [b for b in branches if b != {"type": "null"}]
            if len(rest) < len(branches):  # "X or null": the field is optional
                if len(rest) == 1 and isinstance(rest[0], dict):
                    out.pop(key)
                    out = {**rest[0], **out}
                else:
                    out[key] = rest
    if isinstance(out.get("type"), list) and "null" in out["type"]:
        kinds = [t for t in out["type"] if t != "null"]
        out["type"] = kinds[0] if len(kinds) == 1 else kinds
    return out


def normalize_schema(schema: dict) -> dict:
    """`schema` (a tool's JSON-schema parameters) in a form every provider and
    local grammar accepts: local $ref resolved inline ($defs dropped after),
    allOf merged into one object, "X or null" (anyOf/oneOf with a null branch,
    or a type list with "null") made plain X, document-only keywords
    (_DROPPED) removed, and anything deeper than MAX_DEPTH cut to {}. A
    recursive or unresolvable $ref becomes {} (any value). `schema` itself is
    left unchanged."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = _normalize(schema, schema, 0, ())
    return out if isinstance(out, dict) else {"type": "object", "properties": {}}
