"""Structured Outputs — constrain a turn's output to a JSON Schema.

eVi's `/json` forces *an object*; this forces a *specific schema*. We wrap a
JSON Schema into the OpenAI-style ``response_format`` the agent already forwards
to the backend:

    {"type": "json_schema",
     "json_schema": {"name": "...", "schema": {...}, "strict": true}}

Backends that support it (OpenAI, LM Studio, recent llama.cpp/Ollama) honour the
schema; others fall back to best-effort JSON. Pure stdlib, fully testable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SchemaError(ValueError):
    """The schema spec is missing or not valid JSON / not an object."""


def load_schema(spec: str) -> dict[str, Any]:
    """Load a JSON Schema from a file path or an inline JSON string."""
    spec = (spec or "").strip()
    if not spec:
        raise SchemaError("no schema given")
    text = spec
    p = Path(spec)
    if not spec.lstrip().startswith("{"):
        # Treat as a path.
        if not p.is_file():
            raise SchemaError(f"schema file not found: {spec}")
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaError(f"could not read {spec}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON schema: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("a JSON Schema must be an object")
    return data


def as_response_format(
    schema: dict[str, Any], *, name: str = "output", strict: bool = True
) -> dict[str, Any]:
    """Wrap a JSON Schema as an OpenAI-style json_schema response_format.

    If the caller already passed a full ``{"type": "json_schema", ...}`` or a
    ``{"name", "schema"}`` wrapper, respect it rather than double-wrapping.
    """
    if schema.get("type") == "json_schema" and "json_schema" in schema:
        return schema
    if "schema" in schema and "name" in schema:
        return {"type": "json_schema", "json_schema": schema}
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": strict},
    }
