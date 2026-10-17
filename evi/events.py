"""The versioned event contract: one shape for every stream of a turn.

The web UI's SSE stream (``/api/chat``, a re-roll) and `evi run --format
stream-json` send the same events, serialized here, and both validate against
``SCHEMA`` (JSON Schema 2020-12). An ACP or A2A stream adopts it the same way.

Every event is one JSON object:

    {"schema_version": 1, "kind": "ToolCall", "seq": 3,
     "ts": "2026-10-16T09:30:12.345Z", "name": "read_file", "arguments": "..."}

- ``schema_version`` — ``SCHEMA_VERSION``. Adding a field, or a kind, keeps
  the version; renaming or removing one, or changing what one means, bumps it.
- ``kind`` — what happened: an agent event's class name (``TextDelta``,
  ``ToolCall``, ``ToolResult``, ``Done``, ... see `evi.llm.agent`), one the
  web server adds (``PermissionRequest``, ``PermissionExpired``,
  ``SystemMessage``), or one a headless run adds (``RunStart`` first,
  ``RunResult`` last, carrying the run report `evi.headless` builds).
- ``seq`` — 1, 2, 3, ... per stream, in the order the events were sent.
- ``ts`` — when it was sent, UTC, ISO 8601 with milliseconds.

The rest is the kind's own fields (``$defs`` in ``SCHEMA``). A tool call's
name and arguments, a tool's name and output (and its citations), and an
error's or a guardrail's message are redacted (`evi.redact`, one Redactor per
event): a stream is a display, while the agent's history keeps what the tool
returned.

``event_payload`` is an event without the envelope (what the server queues);
``EventStream`` numbers one stream's events and wraps each in it.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from evi.redact import Redactor

SCHEMA_VERSION = 1

# Why a headless run stopped (the run report's `stop_reason`).
STOP_REASONS = (
    "done", "max_turns", "loop_detected", "step_budget", "interrupted", "error",
    "permission_denied", "guardrail", "stop_hook_exhausted",
)

# The envelope's own keys: a payload can't override them.
_ENVELOPE = ("schema_version", "kind", "seq", "ts")


def event_kind(event: Any) -> str:
    """An event's ``kind``: its class name, or a payload dict's own kind."""
    if isinstance(event, dict):
        return str(event.get("kind", ""))
    return type(event).__name__


def event_payload(event: Any) -> dict[str, Any]:
    """An agent Event as the JSON object a stream sends, without the envelope.
    A tool call's name and arguments, a tool's name and output (and its
    citations), and an error's or a guardrail's message are redacted (a
    backend's error can quote a key, as a URL's query, say). A dict is a
    payload already (the server's PermissionRequest, a headless RunResult)
    and is returned as it is, but for an Error's message."""
    if isinstance(event, dict):
        if event.get("kind") == "Error" and isinstance(event.get("message"), str):
            return {**event, "message": Redactor()(event["message"])}
        return event
    payload: dict[str, Any] = {"kind": event_kind(event)}
    if is_dataclass(event):
        payload.update(asdict(event))
    kind = payload["kind"]
    if kind == "ToolCall":
        red = Redactor()
        payload["name"] = red(payload.get("name"))
        payload["arguments"] = red.args(payload.get("arguments"))
    elif kind == "ToolResult":
        red = Redactor()
        payload["name"] = red(payload.get("name"))
        payload["output"] = red(payload.get("output"))
        if payload.get("citations"):
            payload["citations"] = red.obj(payload["citations"])
    elif kind in ("Error", "Guardrail"):
        payload["message"] = Redactor()(payload.get("message"))
    return payload


def serialize(event: Any) -> str:
    """`event_payload` as a JSON string."""
    return json.dumps(event_payload(event))


def timestamp() -> str:
    """Now, as an envelope's ``ts``: UTC, ISO 8601, milliseconds, ``Z``."""
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventStream:
    """One stream's events, numbered from 1 and wrapped in the envelope.
    Thread-safe: the web queues events from a turn's worker and its
    permission prompts."""

    def __init__(self) -> None:
        self._seq = 0
        self._lock = threading.Lock()

    def frame(self, event: Any) -> dict[str, Any]:
        """`event` (an agent Event or a payload dict) with the envelope."""
        payload = event_payload(event)
        with self._lock:
            self._seq += 1
            seq = self._seq
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "kind": event_kind(payload),
            "seq": seq, "ts": timestamp(),
        }
        out.update((k, v) for k, v in payload.items() if k not in _ENVELOPE)
        return out

    def dumps(self, event: Any) -> str:
        """`frame` as one line of JSON (an SSE data field, an NDJSON line)."""
        return json.dumps(self.frame(event))


# ---- the schema ---------------------------------------------------------------

_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}
_STRS = {"type": "array", "items": _STR}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [{"type": "null"}, schema]}


def _kind(name: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict:
    """An event kind: the envelope plus its own fields (more may be added)."""
    return {"allOf": [
        {"$ref": "#/$defs/envelope"},
        {
            "type": "object",
            "properties": {"kind": {"const": name}, **properties},
            "required": list(required or properties),
        },
    ]}


_REPORT_FIELDS: dict[str, Any] = {
    "schema_version": {"const": SCHEMA_VERSION},
    "session_id": _nullable(_STR),
    "transcript_path": _nullable(_STR),
    "backend": _nullable(_STR),
    "model": _nullable(_STR),
    "cwd": _nullable(_STR),
    # The working folder's trust state (evi.trust): its .evi.toml applied only
    # when "trusted".
    "trust": {"enum": [None, "trusted", "changed", "untrusted"]},
    "turns_used": {"type": "integer", "minimum": 0},
    "max_turns": _nullable({"type": "integer", "minimum": 1}),
    "stop_reason": {"enum": list(STOP_REASONS)},
    # The agent's own Done reason ("stop", "length", "max_turns_reached", ...).
    "done_reason": _nullable(_STR),
    "text": _STR,
    "error": _nullable(_STR),
    "tool_calls": {"type": "array", "items": {"$ref": "#/$defs/tool_call"}},
    "denied_tools": _STRS,
    "usage": _nullable({"$ref": "#/$defs/usage"}),
    "duration_s": {"type": "number", "minimum": 0},
    # None: not tracked (a batch item, a folder too big to snapshot).
    "files_changed": _nullable({"type": "array", "items": {"$ref": "#/$defs/file_change"}}),
    "checks": {"type": "array", "items": {"type": "object"}},
    "policy": _nullable(_STR),
    # What was redacted from the report, by kind: {"OPENAI_API_KEY": 1}.
    "redactions": {"type": "object", "additionalProperties": _INT},
}

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"urn:evi:events:v{SCHEMA_VERSION}",
    "title": f"eVi event stream, v{SCHEMA_VERSION}",
    "description": "One event of an eVi turn's stream: the web UI's SSE and "
                   "`evi run --format stream-json` (evi/events.py).",
    "oneOf": [{"$ref": f"#/$defs/{name}"} for name in (
        "TextDelta", "ThinkingDelta", "ToolCall", "ToolResult", "ToolProgress",
        "UsageStats", "LogProbs", "Guardrail", "RouteInfo", "Steering", "Done", "Error",
        "PermissionRequest", "PermissionExpired", "SystemMessage", "RunStart", "RunResult",
    )],
    "$defs": {
        "envelope": {
            "type": "object",
            "properties": {
                "schema_version": {"const": SCHEMA_VERSION},
                "kind": _STR,
                "seq": {"type": "integer", "minimum": 1},
                "ts": {"type": "string",
                       "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"},
            },
            "required": list(_ENVELOPE),
        },
        # -- agent events (evi.llm.agent) --
        "TextDelta": _kind("TextDelta", {"text": _STR}),
        "ThinkingDelta": _kind("ThinkingDelta", {"text": _STR}),
        "ToolCall": _kind("ToolCall", {"name": _STR, "arguments": _STR}),
        "ToolResult": _kind("ToolResult", {
            "name": _STR, "output": _STR,
            "citations": {"type": "array", "items": {"$ref": "#/$defs/citation"}},
        }),
        "ToolProgress": _kind("ToolProgress", {"names": _STRS, "elapsed": _NUM}),
        "UsageStats": _kind("UsageStats", {
            "prompt_tokens": _INT, "completion_tokens": _INT, "total_tokens": _INT,
        }),
        "LogProbs": _kind("LogProbs", {
            "tokens": {"type": "array", "items": {"type": "object"}},
            "avg_logprob": _NUM, "min_logprob": _NUM, "low_count": _INT,
            "low_threshold": _NUM,
        }),
        "Guardrail": _kind("Guardrail", {
            "direction": {"enum": ["input", "output"]}, "blocked": _BOOL,
            "blocked_by": _STRS, "redacted_by": _STRS, "message": _STR,
        }),
        "RouteInfo": _kind("RouteInfo", {"model": _STR, "route": _STR}),
        "Steering": _kind("Steering", {"text": _STR, "applied": _BOOL}),
        "Done": _kind("Done", {"reason": _STR}),
        "Error": _kind("Error", {"message": _STR}),
        # -- the web server's own --
        "PermissionRequest": _kind("PermissionRequest", {
            "decision_id": _STR, "tool_name": _STR, "args": _STR, "category": _STR,
            "timeout_s": _NUM,
        }),
        "PermissionExpired": _kind("PermissionExpired", {
            "decision_id": _STR, "tool_name": _STR, "timeout_s": _NUM,
        }),
        "SystemMessage": _kind("SystemMessage", {"text": _STR, "clear_transcript": _BOOL},
                               required=("text",)),
        # -- a headless run's (evi run --format stream-json) --
        "RunStart": _kind("RunStart", {
            "session_id": _nullable(_STR), "backend": _nullable(_STR),
            "model": _nullable(_STR), "cwd": _nullable(_STR),
            "max_turns": _nullable({"type": "integer", "minimum": 1}),
            "policy": _nullable(_STR), "resumed": _BOOL,
        }),
        "RunResult": {"allOf": [
            {"$ref": "#/$defs/envelope"},
            {"properties": {"kind": {"const": "RunResult"}}},
            {"$ref": "#/$defs/report"},
        ]},
        # -- parts --
        "citation": {
            "type": "object",
            "properties": {"id": _STR, "source_type": _STR, "source_id": _STR,
                           "excerpt": _STR, "start": _INT, "end": _INT},
            "required": ["id", "source_type", "source_id"],
        },
        # The run report: `evi run --format json`, and the RunResult event.
        "report": {
            "type": "object",
            "properties": _REPORT_FIELDS,
            "required": list(_REPORT_FIELDS),
        },
        "tool_call": {
            "type": "object",
            "properties": {
                "name": _STR,
                # Redacted, then cut to evi.headless.MAX_SHOWN characters.
                "args": _nullable(_STR),
                "status": {"enum": ["ok", "error", "denied", "blocked", "cancelled"]},
                # From the call to its result being reported: the calls of one
                # batch run in parallel and report together.
                "duration_ms": _nullable({"type": "integer", "minimum": 0}),
                "output": _nullable(_STR),
            },
            "required": ["name", "args", "status", "duration_ms", "output"],
        },
        "usage": {
            "type": "object",
            "properties": {"prompt": _INT, "completion": _INT, "total": _INT},
            "required": ["prompt", "completion", "total"],
        },
        "file_change": {
            "type": "object",
            "properties": {
                "path": _STR,
                "status": {"enum": ["added", "modified", "deleted"]},
                # Lines, against the commit checked out when the run started
                # (a new file: its lines); None when unknown (binary, no git).
                "added": _nullable({"type": "integer", "minimum": 0}),
                "deleted": _nullable({"type": "integer", "minimum": 0}),
            },
            "required": ["path", "status", "added", "deleted"],
        },
    },
}


def schema() -> dict[str, Any]:
    """A copy of ``SCHEMA``: what one event of a stream validates against."""
    return copy.deepcopy(SCHEMA)


def report_schema() -> dict[str, Any]:
    """What a run report (`evi run --format json`) validates against: the
    ``report`` definition of ``SCHEMA``."""
    out = {k: v for k, v in schema().items() if k != "oneOf"}
    out["$id"] = f"urn:evi:run-report:v{SCHEMA_VERSION}"
    out["title"] = f"eVi run report, v{SCHEMA_VERSION}"
    out["description"] = "The result of a headless run: `evi run --format json` (evi/headless.py)."
    out["$ref"] = "#/$defs/report"
    return out
