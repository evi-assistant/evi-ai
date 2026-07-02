"""A2A (Agent2Agent) adapter — expose eVi as an A2A agent, and call other A2A agents.

A2A (https://a2a-protocol.org, a Linux Foundation project) is the cross-vendor
standard for agent-to-agent delegation. It's complementary to eVi's own
federation (``evi/federation.py``):

- **federation** = the zero-dependency *private fast path* for delegating to your
  OTHER eVis on a LAN (reuses the web bearer token, plain HTTP, synchronous).
- **A2A** = the *interop path* for talking to ANY vendor's agent that speaks the
  standard, and for letting such agents call eVi.

Scope of this adapter (synchronous core):

- an **Agent Card** at ``/.well-known/agent-card.json`` (A2A's canonical discovery
  path) describing this node — served always, auth-exempt;
- the **JSON-RPC 2.0** methods ``message/send``, ``tasks/get``, ``tasks/cancel``
  over ``POST /a2a`` — gated by ``[federation] a2a = true`` + the web bearer token,
  and run NON-INTERACTIVELY (tools not already auto-approved are denied), same as
  ``/api/federate``;
- a **client** (``client_send``) to call an external A2A agent's ``message/send``.

Not yet implemented: streaming (``message/stream`` SSE) and push notifications —
the card advertises ``capabilities.streaming = false`` so a compliant client falls
back to blocking ``message/send``. Built by hand against the v0.3/v1.0 wire shapes;
no ``a2a-sdk`` dependency, keeping eVi's local-first zero-dep footprint.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

# A2A spec version this adapter targets (wire shapes are stable across 0.3/1.0).
A2A_PROTOCOL_VERSION = "0.3.0"

# JSON-RPC / A2A error codes.
_ERR_PARSE = -32700
_ERR_INVALID_REQUEST = -32600
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602
_ERR_TASK_NOT_FOUND = -32001
_ERR_TASK_NOT_CANCELABLE = -32002

_TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}

# In-memory task store (taskId -> Task). Federation/A2A tasks here are synchronous
# and already terminal by the time message/send returns; the store exists so a
# client can still tasks/get them afterward. Bounded so a long-lived server can't
# grow unboundedly.
_TASKS: "dict[str, dict]" = {}
_TASK_ORDER: list[str] = []
_MAX_TASKS = 256


class A2AError(Exception):
    """An A2A peer is unreachable or returned a protocol/agent error."""


def _new_id() -> str:
    return uuid.uuid4().hex


def _text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def message_text(message: dict) -> str:
    """Concatenate the text of an A2A Message's parts. Non-text parts (file/data)
    are ignored for now — this adapter is text-in/text-out."""
    if not isinstance(message, dict):
        return ""
    chunks: list[str] = []
    for p in message.get("parts") or []:
        if not isinstance(p, dict):
            continue
        # v1.0 uses `kind`, older lines used `type`; accept either, and any part
        # that simply carries a `text` field.
        if p.get("kind") == "text" or p.get("type") == "text" or "text" in p:
            chunks.append(str(p.get("text") or ""))
    return "\n".join(c for c in chunks if c).strip()


def _remember(task: dict) -> None:
    tid = task["id"]
    _TASKS[tid] = task
    _TASK_ORDER.append(tid)
    while len(_TASK_ORDER) > _MAX_TASKS:
        _TASKS.pop(_TASK_ORDER.pop(0), None)


def _make_task(task_id: str, context_id: str, state: str, *, text: str = "", error: str = "") -> dict:
    status: dict[str, Any] = {"state": state}
    if error:
        status["message"] = {
            "role": "agent",
            "messageId": _new_id(),
            "parts": [_text_part(error)],
        }
    task: dict[str, Any] = {
        "id": task_id,
        "contextId": context_id,
        "kind": "task",
        "status": status,
        "artifacts": [],
        "history": [],
    }
    if text:
        task["artifacts"] = [
            {"artifactId": _new_id(), "name": "response", "parts": [_text_part(text)]}
        ]
    return task


def build_agent_card(cfg: Any, *, url: str = "") -> dict:
    """This node's A2A Agent Card. Never raises — discovery must always answer.

    Carries the standard A2A fields plus an ``x-evi`` extension with the model and
    its capability flags (vision/tools/reasoning/audio), so eVi peers and
    capability-aware clients can route by modality.
    """
    from evi import __version__
    from evi.capabilities import capabilities as _caps

    model, serve = "", False
    try:
        model = (cfg.llm.model or "").strip()
        serve = bool(getattr(cfg.federation, "a2a", False) or cfg.federation.serve)
    except Exception:  # noqa: BLE001
        pass
    try:
        caps = _caps(model)
    except Exception:  # noqa: BLE001 — heuristics must not break discovery
        caps = {}
    base = url.rstrip("/")
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": socket.gethostname() or "eVi",
        "description": "eVi — local-first personal AI assistant",
        "url": (base + "/a2a") if base else "/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        # A2A `capabilities` = protocol features (NOT model modalities — those live
        # under x-evi). Streaming/push are not implemented yet.
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "assistant",
                "name": "General assistant",
                "description": (
                    "General-purpose local assistant with tools (files, code, "
                    "shell, web, git, …). Delegated tasks run non-interactively."
                ),
                "tags": ["assistant", "tools", "local"],
            }
        ],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "x-evi": {"model": model, "capabilities": caps, "serve": serve},
    }


def handle_rpc(body: dict, run_task: Callable[[str], tuple[str, str]]) -> dict:
    """Dispatch one A2A JSON-RPC request and return the JSON-RPC response.

    ``run_task(text) -> (answer, error)`` executes a delegated task (the caller
    wires this to eVi's non-interactive headless runner). ``message/stream`` is
    accepted but served synchronously (we don't stream yet), returning the final
    Task — a compliant client degrades gracefully.
    """
    rid = body.get("id") if isinstance(body, dict) else None

    def _ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not body.get("method"):
        return _err(_ERR_INVALID_REQUEST, "invalid JSON-RPC 2.0 request")

    method = str(body.get("method"))
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    if method in ("message/send", "message/stream"):
        message = params.get("message") if isinstance(params.get("message"), dict) else {}
        text = message_text(message)
        if not text:
            return _err(_ERR_INVALID_PARAMS, "message has no text parts")
        context_id = str(message.get("contextId") or _new_id())
        task_id = _new_id()
        try:
            answer, error = run_task(text)
        except Exception as exc:  # noqa: BLE001 — surface as an A2A failed task
            answer, error = "", f"{type(exc).__name__}: {exc}"
        task = _make_task(
            task_id, context_id, "failed" if error else "completed",
            text=answer, error=error,
        )
        _remember(task)
        return _ok(task)

    if method == "tasks/get":
        task = _TASKS.get(str(params.get("id") or params.get("taskId") or ""))
        return _ok(task) if task is not None else _err(_ERR_TASK_NOT_FOUND, "task not found")

    if method == "tasks/cancel":
        task = _TASKS.get(str(params.get("id") or params.get("taskId") or ""))
        if task is None:
            return _err(_ERR_TASK_NOT_FOUND, "task not found")
        if task["status"]["state"] in _TERMINAL_STATES:
            return _err(_ERR_TASK_NOT_CANCELABLE, "task is already terminal")
        task["status"]["state"] = "canceled"
        return _ok(task)

    return _err(_ERR_METHOD_NOT_FOUND, f"method not found: {method}")


def _extract_text(result: Any) -> str:
    """Pull text out of an A2A ``message/send`` result — either a Task (with
    artifacts) or a bare Message (with parts)."""
    if not isinstance(result, dict):
        return ""
    chunks: list[str] = []
    for art in result.get("artifacts") or []:
        if isinstance(art, dict):
            for p in art.get("parts") or []:
                if isinstance(p, dict) and (p.get("kind") == "text" or p.get("type") == "text" or "text" in p):
                    chunks.append(str(p.get("text") or ""))
    if chunks:
        return "\n".join(c for c in chunks if c).strip()
    return message_text(result)  # result was a Message


def client_send(url: str, text: str, *, token: str = "", timeout: float = 180.0) -> str:
    """Send `text` to an external A2A agent's JSON-RPC endpoint (`url`, e.g.
    ``https://host/a2a``) via ``message/send`` and return its answer text.

    Raises ``A2AError`` on transport failure or a JSON-RPC error response."""
    payload = {
        "jsonrpc": "2.0",
        "id": _new_id(),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": _new_id(),
                "parts": [_text_part(text)],
            }
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "evi-a2a"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise A2AError(f"A2A agent returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise A2AError(f"could not reach A2A agent at {url}: {exc}") from exc
    if isinstance(data, dict) and data.get("error"):
        e = data["error"]
        msg = e.get("message") if isinstance(e, dict) else e
        raise A2AError(f"A2A agent error: {msg}")
    return _extract_text((data or {}).get("result") if isinstance(data, dict) else None)


def reset_tasks() -> None:
    """Clear the in-memory task store (used by tests)."""
    _TASKS.clear()
    _TASK_ORDER.clear()
