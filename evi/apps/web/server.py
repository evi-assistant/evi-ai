"""eVi web frontend — FastAPI + Server-Sent Events.

One `WebSession` is held per session id; each owns an `Agent` plus a small
registry of pending permission decisions. The browser POSTs to `/api/chat`
with `{session_id, message}` and receives an SSE stream of JSON event
lines. Tool calls that aren't auto-approved surface as `PermissionRequest`
events that the browser answers via `/api/decide`. `POST
/api/session/{id}/cancel` stops the running turn (the UI's Stop button).

Tool-call displays are redacted (`evi.redact`): the ToolCall / ToolResult /
PermissionRequest events and the tool messages /history redraws show
`[REDACTED:<kind>]` where a secret was. The agent's own history, which the
model works from, keeps what the tools returned.

Slash commands (`/help`, `/goal`, `/plan`, `/auto`, `/reset`, `/tools`,
`/model`) match CLI behavior and are dispatched server-side so the browser
just types and reads; no client-side command logic needed.

Generated images saved under `IMAGE_DIR` are served from `/images/{name}`.
"""

from __future__ import annotations

import asyncio
import html as _htmlmod
import json
import logging
import math
import mimetypes
import os
import posixpath
import secrets
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from evi import events as _events
from evi import hooks as hooks_mod
from evi import safemode
from evi.apps.web.ids import SESSION_ID_MAX_LEN, valid_session_id
from evi.backends import get_backend
from evi.commands import CommandStore
from evi.config import HOME, IMAGE_DIR, UPLOADS_DIR, Config, ConfigUnreadable, ensure_dirs
from evi.history import COMPACTION_ACK, is_note, note_message
from evi.llm.agent import (
    CANCEL_GRACE, CANCEL_POLL, Agent, Done, Error, Event,
    make_unattended, remote_unattended,
)
from evi.llm.client import make_client
from evi.mcp import MCPManager, filter_allowed, load_servers
from evi.redact import Redactor

# Import tool modules for their @tool side effects.
import evi.tools.fs  # noqa: F401
import evi.tools.code  # noqa: F401
import evi.tools.image_comfy  # noqa: F401
import evi.tools.memory  # noqa: F401
import evi.tools.skills  # noqa: F401
import evi.tools.subagent  # noqa: F401
import evi.tools.websearch  # noqa: F401
import evi.tools.voice  # noqa: F401
import evi.tools.computer  # noqa: F401
import evi.tools.pdf  # noqa: F401
import evi.tools.sqlite  # noqa: F401
import evi.tools.index  # noqa: F401
import evi.tools.git  # noqa: F401
import evi.tools.federation  # noqa: F401
import evi.tools.ocr  # noqa: F401
import evi.tools.calendar  # noqa: F401
import evi.tools.rerank  # noqa: F401


logger = logging.getLogger(__name__)


STATIC_DIR = Path(__file__).parent / "static"

# The UI's own files go out with `X-Content-Type-Options: nosniff` (below),
# under which a browser runs a script only if it is labelled JavaScript and
# applies a stylesheet only if it is labelled CSS. Their type comes from
# `mimetypes`, which on Windows also reads the registry, where other software
# can have mapped .js to text/plain — and that would block the whole UI. So
# the types the UI depends on are pinned here.
for _ext, _type in ((".js", "text/javascript"), (".css", "text/css"),
                    (".html", "text/html"), (".json", "application/json")):
    mimetypes.add_type(_type, _ext)
del _ext, _type


# ---- response security headers ---------------------------------------------
#
# Every response: `X-Content-Type-Options: nosniff`, so a browser never treats
# a file as a type it isn't labelled as (an uploaded `.txt` holding HTML stays
# text; a JSON reply never runs as a script).
#
# The UI page (`/`, `/static/index.html`): its Content-Security-Policy as a
# header plus `frame-ancestors 'none'`, and `X-Frame-Options: DENY`, so no other
# page can load eVi in a frame and steer clicks onto Approve or Settings
# (clickjacking). A <meta> policy can't carry frame-ancestors. The policy has
# ONE source: the <meta http-equiv="Content-Security-Policy"> in index.html,
# which the browser enforces too, read here (page_csp) — not a copy kept in step
# by hand. scripts/vendor_ui_libs.py --check keeps that tag in the form
# _CSP_META reads.
#
# Files a person or a model put there (`/images/<name>` generated images,
# `/uploads/<session>/<name>` attachments): a policy under which nothing loads
# or runs, in a sandbox (an opaque origin), so an HTML or SVG file opened
# directly can't act as eVi's page.
_FILE_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
_FILE_PREFIXES = ("/images/", "/uploads/")
_PAGE_PATHS = frozenset({"/", "/static/index.html"})
_NO_FRAMING = "frame-ancestors 'none'"
_CSP_META = re.compile(
    r"""<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"\s*/?>""",
    re.IGNORECASE,
)
# The page's policy if index.html has none this can read: nothing loads — a
# broken page, never an unprotected one (the UI tests catch it first).
_PAGE_CSP_UNREADABLE = "default-src 'none'"
# (file key, header) for the index.html last read — one tuple, rebound whole.
_page_csp_cached: tuple[tuple, str] | None = None


def page_csp(index: Path | None = None) -> str:
    """The Content-Security-Policy header for the UI page: index.html's own
    <meta> policy (without any frame-ancestors, which a <meta> can't carry)
    plus `frame-ancestors 'none'`. Re-read whenever the file changes (a dev
    edit, a sidecar UI update), so the header never disagrees with the page."""
    global _page_csp_cached
    path = index or STATIC_DIR / "index.html"
    try:
        st = path.stat()
        key: tuple | None = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    cached = _page_csp_cached
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    directives: list[str] = []
    if key is not None:
        try:
            m = _CSP_META.search(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            m = None
        if m:
            for part in _htmlmod.unescape(m.group(1)).split(";"):
                part = " ".join(part.split())
                if part and part.split(" ", 1)[0].lower() != "frame-ancestors":
                    directives.append(part)
    if not directives:
        logger.error("web UI: no Content-Security-Policy <meta> readable in %s; "
                     "serving the page under %r", path, _PAGE_CSP_UNREADABLE)
        directives = [_PAGE_CSP_UNREADABLE]
    value = "; ".join([*directives, _NO_FRAMING])
    if key is not None:
        _page_csp_cached = (key, value)
    return value


def _security_headers(path: str, headers: Any) -> None:
    """Set the headers above on one response (`headers`: a MutableHeaders)."""
    headers["X-Content-Type-Options"] = "nosniff"
    norm = posixpath.normpath("/" + (path or "/").lstrip("/"))
    if norm.startswith(_FILE_PREFIXES):
        headers["Content-Security-Policy"] = _FILE_CSP
        return
    html_page = (headers.get("content-type") or "").lower().startswith("text/html")
    if norm in _PAGE_PATHS or (html_page and norm.startswith("/static/")):
        headers["Content-Security-Policy"] = page_csp()
        headers["X-Frame-Options"] = "DENY"
    elif html_page:
        # Any other page eVi serves (FastAPI's /docs): not frameable either.
        if "content-security-policy" not in headers:
            headers["Content-Security-Policy"] = _NO_FRAMING
        headers["X-Frame-Options"] = "DENY"


class _SecurityHeadersMiddleware:
    """Pure ASGI, so streamed (SSE) responses pass through untouched. Added
    last in create_app, so it wraps everything — refusals from the auth
    middleware get the headers too — except the 500 for an unhandled
    exception, which _internal_error gives them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import MutableHeaders

        path = scope.get("path") or "/"

        async def send_with_headers(message: Any) -> None:
            if message.get("type") == "http.response.start":
                _security_headers(path, MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _internal_error(request: Request, exc: Exception) -> Any:
    """The answer to an exception no route caught: Starlette's own plain 500,
    with the headers above. Starlette sends it from ServerErrorMiddleware,
    which sits outside every middleware create_app adds, so
    _SecurityHeadersMiddleware never sees that response. The exception still
    propagates after it, for the server to log."""
    from starlette.responses import PlainTextResponse

    response = PlainTextResponse("Internal Server Error", status_code=500)
    _security_headers(request.scope.get("path") or "/", response.headers)
    return response


async def _config_unreadable(request: Request, exc: Exception) -> Any:
    """A setting couldn't be saved because config.toml is there but can't be
    read (Config.save refuses to write over it): 409 with the reason, and the
    same headers as every other response."""
    response = JSONResponse({"detail": str(exc)}, status_code=409)
    _security_headers(request.scope.get("path") or "/", response.headers)
    return response


# ---- request / session shapes -------------------------------------------


class ChatRequest(BaseModel):
    session_id: str
    message: str
    images: list[str] | None = None  # optional local file paths; VLM only
    # OpenAI predicted-outputs hint for the next LLM round-trip. The agent
    # only applies it to the first call (any tool round clears it). Most
    # local backends ignore unknown fields — we forward via extra_body —
    # so it's safe to set even when the backend won't actually speculate.
    prediction: str | None = None
    # Per-turn override: when False the model may only request one tool per
    # turn. None = use the config default.
    parallel_tool_calls: bool | None = None
    # Per-turn token-id bias map ({"123": -100}). None = use config default.
    logit_bias: dict | None = None
    # Optional local audio file paths. Omni models get raw input_audio
    # parts; others fall back to local Whisper transcription.
    audio: list[str] | None = None
    # Structured Outputs: a JSON Schema (object) or inline-JSON string to
    # constrain this turn's output. Wrapped into response_format.
    output_schema: dict | str | None = None


class DecisionRequest(BaseModel):
    session_id: str
    decision_id: str
    approved: bool


class BackendActionRequest(BaseModel):
    """POST body for /api/backend/start and /api/backend/open-download."""

    kind: str  # "ollama" | "lmstudio" | "llamacpp"


class BackendUseRequest(BaseModel):
    """POST body for /api/backend/use — switch the active backend (+ model)."""

    kind: str                      # ollama | lmstudio | llamacpp | openai_compat
    model: str | None = None       # explicit; else auto-pick an installed one
    base_url: str | None = None    # override; else the backend's default URL
    verify: bool = False           # prove it answers; roll back if it doesn't


# --- LLM backend probing -------------------------------------------------
#
# Known local backends we probe regardless of which one is configured, so the
# UI can warn + offer to start one. The probe helpers live in `evi.portprobe`
# (shared with the llama.cpp backend); `_probe_backend` is re-exported here so
# tests can monkeypatch `server_mod._probe_backend` and the endpoints can
# reference it as a module global.
from evi.portprobe import discover_llamacpp_url  # noqa: E402
from evi.portprobe import is_openai_server as _probe_backend  # noqa: E402

_KNOWN_BACKENDS: list[tuple[str, str]] = [
    ("lmstudio", "http://localhost:1234/v1"),
    ("ollama", "http://localhost:11434/v1"),
    ("llamacpp", "http://localhost:8080/v1"),
]

# CLI-agent backends have NO HTTP endpoint — they drive a local CLI that
# authenticates via its own subscription/login. "reachable" for them means the
# CLI binary is installed and on PATH, not that a port answers. (Kind -> binary.)
_CLI_AGENT_BINS: dict[str, str] = {
    "claude_agent": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "amp": "amp",
    "qwen": "qwen",
    "copilot": "copilot",
}


def _verify_llm(cfg) -> dict[str, object]:
    """Send a real 1-token completion (then a tool-bearing one) and report what
    actually worked. Shared by `/api/backend/verify` and the verified switch."""
    import time as _t

    from evi.llm.client import make_client as _mk

    out: dict[str, object] = {
        "model": cfg.llm.model, "backend": cfg.llm.backend,
        "chat": False, "tools": False, "error": "", "latency_ms": 0,
    }
    started = _t.monotonic()
    try:
        client = _mk(cfg.llm)
        client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=1,
        )
        out["chat"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        out["latency_ms"] = int((_t.monotonic() - started) * 1000)
        return out
    out["latency_ms"] = int((_t.monotonic() - started) * 1000)
    # Tools are a separate question: a backend can chat fine and still reject or
    # ignore tool definitions. Offering one is enough — the model needn't call it.
    try:
        client.chat.completions.create(
            model=cfg.llm.model,
            messages=[{"role": "user", "content": "ok"}],
            max_tokens=1,
            tools=[{"type": "function", "function": {
                "name": "evi_probe", "description": "probe",
                "parameters": {"type": "object", "properties": {}}}}],
        )
        out["tools"] = True
    except Exception:  # noqa: BLE001
        out["tools"] = False
    return out


def _host_is_local(request) -> bool:
    """True if the request's Host header names this machine.

    Anti-DNS-rebinding: a hostile page can make a browser send requests to
    127.0.0.1, but it cannot forge the Host header, so a request arriving as
    `evil.example` (resolved to loopback) is rejected. Bare IPv6 and a missing
    port are both handled; an absent Host is treated as local because non-browser
    clients (curl -H, some test clients) legitimately omit it.
    """
    import os as _os

    if _os.environ.get("EVI_ALLOW_ANY_HOST", "").strip() in {"1", "true", "TRUE"}:
        return True
    raw = (request.headers.get("host") or "").strip().lower()
    if not raw:
        return True
    host = raw
    if host.startswith("["):                      # [::1]:8473
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:                    # 127.0.0.1:8473
        host = host.split(":", 1)[0]
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver"}


_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# GETs with side effects, guarded like a POST: /api/backend/pull starts an
# Ollama model download of whatever `?model=` names. Any page can fire a GET
# with no script — an <img>, <link> or <iframe>, a link — so without the token
# these also need X-Evi-Client from every caller (_client_header_refusal); the
# UI streams the pull with fetch, which sends it. HEAD runs the same handler.
_SIDE_EFFECT_GETS = frozenset({"/api/backend/pull"})
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# The desktop shell's bundled loading page (tauri://localhost on macOS/Linux,
# http(s)://tauri.localhost on Windows). Today it only polls /api/health and then
# navigates to http://127.0.0.1:<port>/ (same-origin from there on), but the
# shell must never be locked out of its own sidecar.
_TAURI_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)


def _authority(host: str | None, port: int | None) -> tuple[str, int | None]:
    """(host, port) with 80 and 443 folded to None whatever the scheme, so an
    `https://x` Origin matches a TLS-terminating proxy's plain-HTTP `Host: x`
    or `Host: x:443` (an implicit http and https port already compare equal)."""
    if port in (80, 443):
        port = None
    return (host or "").lower(), port


def _cross_site_refusal(request) -> str | None:
    """Why a state-changing request must be refused as cross-site, else None.

    Anti-CSRF: any web page can POST to 127.0.0.1, and a multipart or
    form-encoded POST is a CORS "simple request" — no preflight — so without
    this a hostile page could drive the upload/shell-backed API through the
    user's browser on the default no-token setup. Browsers stamp every
    non-GET request with an Origin (and Sec-Fetch-Site) the page can't forge,
    so we accept only our own origin, a loopback alias of the same port, or the
    desktop shell. No Origin at all means a non-browser client (CLI, the VS Code
    extension host, federation/A2A peers, curl) — those pass, unless the browser
    says another site sent it: a no-cors GET (<img>, <link>) carries no Origin,
    so for the side-effecting GETs Sec-Fetch-Site is the only tell. `null`
    (sandboxed iframe, file://) is refused. `EVI_ALLOW_CROSS_ORIGIN=1` overrides.

    Behind a reverse proxy that rewrites Host (nginx's default
    `Host $proxy_host`, Apache without ProxyPreserveHost) the public name the
    browser used can arrive as X-Forwarded-Host (+ X-Forwarded-Port), matched
    only with `EVI_TRUSTED_PROXY=1`. Not by default: a page that reaches eVi
    through such a proxy by DNS rebinding is same-origin with itself, so it can
    set that header (or the proxy forwards its rebound Host as it) and match
    its own Origin. Opting in vouches that the proxy sets the header itself.
    """
    import os as _os
    from urllib.parse import urlsplit

    def _flag(name: str) -> bool:
        return _os.environ.get(name, "").strip() in {"1", "true", "TRUE"}

    if request.method not in _STATE_CHANGING_METHODS and not (
        request.method in ("GET", "HEAD") and request.url.path in _SIDE_EFFECT_GETS
    ):
        return None
    if _flag("EVI_ALLOW_CROSS_ORIGIN"):
        return None
    # Routine webhooks: the unguessable path token is the credential (the same
    # reason they skip web auth), so a page that doesn't know it can't forge the
    # call — and a browser dashboard that does know it may fire it.
    if request.url.path.startswith("/api/routine/"):
        return None
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        # Every browser POST carries an Origin; a no-cors GET doesn't. Another
        # local port is `same-site` (ports don't count), so refuse that too.
        if site in ("cross-site", "same-site"):
            return f"Sec-Fetch-Site: {site}"
        return None
    norm = origin.lower().rstrip("/")
    refusal = f"Origin: {origin[:200]}"
    # The desktop shell and a loopback alias of this very port are this
    # machine's own pages, but a real browser (WebView2 included) labels their
    # requests to 127.0.0.1 `cross-site` — a different site — so they're
    # matched before the Sec-Fetch-Site check below.
    if norm in _TAURI_ORIGINS:
        return None
    try:
        o = urlsplit(norm)
        if o.scheme not in ("http", "https") or not o.hostname:
            return refusal
        theirs = _authority(o.hostname, o.port)
        mine = _authority(request.url.hostname, request.url.port)
    except ValueError:  # malformed port in either header
        return refusal
    if (theirs != mine and theirs[1] == mine[1]
            and theirs[0] in _LOOPBACK_NAMES and mine[0] in _LOOPBACK_NAMES):
        return None
    if site == "cross-site":  # a browser never says so for its own origin
        return "Sec-Fetch-Site: cross-site"
    if theirs == mine:
        return None
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if fwd_host and _flag("EVI_TRUSTED_PROXY"):
        fwd_port = (request.headers.get("x-forwarded-port") or "").split(",")[0].strip()
        try:
            f = urlsplit("//" + fwd_host.lower())
            port = f.port
        except ValueError:  # malformed port
            return refusal
        if port is None and fwd_port.isascii() and fwd_port.isdigit():
            port = int(fwd_port)
        if f.hostname and _authority(f.hostname, port) == theirs:
            return None
    return refusal


# ---- request hardening: tokens, clients, owner-only routes ----------------

# The access token a launcher hands the server for this run, beside `[web]
# auth_token` in config.toml: the desktop shell's generated LAN token (main.rs,
# when `[federation] bind_lan` is on) or a container's. Read once and removed
# from the environment, so the agent's own subprocesses (shell, code runners,
# MCP servers) never inherit it.
_LAUNCH_TOKEN_ENV = "EVI_WEB_AUTH_TOKEN"
_LAUNCH_TOKEN: str = os.environ.pop(_LAUNCH_TOKEN_ENV, "").strip()


def _configured_token(cfg: Any) -> str:
    return str(getattr(getattr(cfg, "web", None), "auth_token", "") or "").strip()


def _web_tokens(cfg: Any) -> list[str]:
    """Every token that authenticates as the owner: `[web] auth_token` and the
    launch token. Empty (and no users.json logins) = no-token mode."""
    tokens: list[str] = []
    configured = _configured_token(cfg)
    if configured:
        tokens.append(configured)
    if _LAUNCH_TOKEN and _LAUNCH_TOKEN not in tokens:
        tokens.append(_LAUNCH_TOKEN)
    return tokens


def _lan_token_only(cfg: Any, users: list) -> bool:
    """True when the launch token is the server's only credential (the desktop
    app's LAN token, a container's): it gates other machines, and this machine
    is served as in no-token mode — so an old VS Code extension or `evi` on
    loopback keeps working. A `[web] auth_token` or users.json logins gate
    loopback too."""
    return bool(_LAUNCH_TOKEN) and not _configured_token(cfg) and not users


def _provided_token(request) -> str:
    """The bearer token a request carries: `Authorization: Bearer`, else
    `?token=` (EventSource and <img> can't set headers)."""
    header = request.headers.get("Authorization", "")
    provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return provided or request.query_params.get("token", "")


def _token_matches(provided: str, tokens: list[str]) -> bool:
    """Constant-time match against any of `tokens` (bytes, so a non-ASCII
    guess is a mismatch rather than a TypeError)."""
    if not provided:
        return False
    got = provided.encode("utf-8", "surrogatepass")
    hit = False
    for t in tokens:
        hit |= secrets.compare_digest(got, t.encode("utf-8", "surrogatepass"))
    return hit


def _client_is_loopback(request) -> bool:
    """True if the TCP peer is this machine. Unlike the Host header (any
    non-browser client sets it freely), the peer address can't be forged
    without owning the route back."""
    client = getattr(request, "client", None)
    return _is_loopback_host(client.host if client else "")


# Headers a reverse proxy adds for the client it forwards.
_FORWARDING_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def _client_is_local(request) -> bool:
    """True if the request comes from this machine: a loopback TCP peer that
    nothing forwarded. A forwarded request is another client's, whatever the
    peer — a same-machine proxy relays the LAN into loopback — and uvicorn
    rewrites the peer from X-Forwarded-For when it trusts the proxy
    (FORWARDED_ALLOW_IPS), so a LAN client could claim 127.0.0.1 with it."""
    if not _client_is_loopback(request):
        return False
    return not any((request.headers.get(h) or "").strip() for h in _FORWARDING_HEADERS)


def lan_bind_refusal(host: str, cfg: Any = None) -> str | None:
    """Why serving on `host` must be refused, else None.

    Without an access token the server only answers loopback clients, so a
    non-loopback bind (`--host 0.0.0.0`, a LAN address) with no token would
    look up and refuse everyone it was bound for. For a user-run launcher
    (`evi web`, a container entrypoint) to call before binding and stop with
    the message. Not the desktop sidecar's entry: the shell decides its bind
    and always passes a token when it binds the LAN."""
    import ipaddress

    h = str(host or "").strip().strip("[]").lower()
    try:
        loopback = h == "localhost" or ipaddress.ip_address(h).is_loopback
    except ValueError:
        loopback = False  # a hostname binds whatever interface it names
    if loopback:
        return None
    if cfg is None:
        cfg = Config.load()
    if _web_tokens(cfg):
        return None
    return (
        f"refusing to serve on {host or '(all interfaces)'} without an access token: "
        "other machines would be refused anyway. Set one with `evi web-config token "
        f"rotate` (or the {_LAUNCH_TOKEN_ENV} environment variable), or bind 127.0.0.1."
    )


# A browser request is one with an Origin, or a Sec-Fetch-Site naming the page
# that sent it. Every browser POST carries an Origin, and every current browser
# labels its requests with Sec-Fetch-Site. Not Sec-Fetch-Mode alone: Node's
# fetch (undici) — the VS Code extension host's, so old VSIX builds — sends
# `Sec-Fetch-Mode: cors` and nothing else. `Sec-Fetch-Site: none` is a request
# the user started (an address-bar load), never one a page made. curl, the CLI
# and peers send none of these. A state-changing /api request from a browser
# must also carry this header unless it authenticated with a token (and so must
# any caller of a side-effecting GET): eVi's own pages add it (the fetch
# wrapper in static/app.js), and another page can't add a custom header to a
# cross-origin request without a CORS preflight, which eVi never answers.
_CLIENT_HEADER = "x-evi-client"


def _is_browser_request(request) -> bool:
    headers = request.headers
    if (headers.get("origin") or "").strip():
        return True
    return (headers.get("sec-fetch-site") or "").strip().lower() not in ("", "none")


def _client_header_refusal(request) -> str | None:
    """Why a browser's state-changing /api request without X-Evi-Client must
    be refused, else None. Callers skip it for token-authenticated requests."""
    path = request.url.path
    if request.method in ("GET", "HEAD") and path in _SIDE_EFFECT_GETS:
        # Another site or another local port never starts one, header or not —
        # not even with EVI_ALLOW_CROSS_ORIGIN, which waives only the Origin
        # check (_cross_site_refusal).
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return (
                f"Refusing a {request.method} {path} that another site started "
                f"(Sec-Fetch-Site: {site}): it has side effects, so only eVi's own "
                "pages (or a client with the access token) may call it."
            )
        # And every caller needs the header, browser or not: a GET is what an
        # <img>, <link>, <iframe> or a clicked link sends, on another site or
        # in eVi's own page (markup that got past the sanitizer, a diagram) —
        # with Sec-Fetch-Site: same-origin, or none at all from an older
        # browser or a link opened in the system browser. None of them can
        # add a header; eVi's UI streams the pull with fetch, which does.
        if (request.headers.get(_CLIENT_HEADER) or "").strip():
            return None
        return (
            f"Refusing a {request.method} {path} without the X-Evi-Client header: it "
            "starts a model download, so a link or an embedded image, stylesheet or "
            "frame must not be able to set one off. eVi's own pages send the header; "
            "a script or curl must send `X-Evi-Client: 1` (or authenticate with the "
            "access token)."
        )
    if request.method not in _STATE_CHANGING_METHODS or not path.startswith("/api/"):
        return None
    # Routine webhooks: the path token is the credential (see _cross_site_refusal).
    if path.startswith("/api/routine/"):
        return None
    if (request.headers.get(_CLIENT_HEADER) or "").strip():
        return None
    if not _is_browser_request(request):
        return None
    return (
        "Refusing a browser request without the X-Evi-Client header. eVi's own "
        "pages send it; a script calling the API from a browser must send "
        "`X-Evi-Client: 1` (or authenticate with the access token)."
    )


# Multi-user mode: routes that reconfigure or act on the HOST rather than the
# caller's own sessions. Only the owner (whoever holds `[web] auth_token` or the
# launch token) may use them; a users.json login gets 403. (methods or None for
# every method, path prefix.) A prefix matches itself and anything below it.
_OWNER_ONLY_ROUTES: tuple[tuple[frozenset[str] | None, str], ...] = (
    (None, "/api/config"),
    (None, "/api/hooks"),
    (None, "/api/plugins"),
    (None, "/api/channels"),
    (None, "/api/peers"),
    (None, "/api/runtime"),
    (None, "/api/mcp"),
    (None, "/api/backends"),
    (None, "/api/backend"),        # start / install / use / verify / pull
    (frozenset({"POST"}), "/api/model-picker"),  # persists [llm]
    (None, "/api/guardrails"),
    (None, "/api/routes"),
    # One global journal holds every user's file writes (checkpoints.py).
    (None, "/api/checkpoints"),
    (None, "/api/rewind"),
    (frozenset({"POST"}), "/api/project/trust"),
    # Headless runs on the host's own config and data dirs, outside the
    # caller's per-user sessions (several auto-approve every tool).
    (None, "/api/dispatch/workflow"),
    (None, "/api/dispatch/ultracode"),
    (None, "/api/recipes/run"),
    (None, "/api/evals/run"),
    # Delegated tasks from peers and A2A agents: headless agents on the host's
    # config and default memory, like the runs above. Peers send the owner's token.
    (None, "/api/federate"),
    (None, "/a2a"),
    # The Companion watches the owner's own sources and runs approved actions
    # on the host (one store under eVi's home, not per user).
    (None, "/api/companion"),
)
# Reads every login needs (the chat UI polls it before each send).
_OWNER_ONLY_EXEMPT = frozenset({("GET", "/api/backend/status"), ("HEAD", "/api/backend/status")})


def _owner_only_route(method: str, path: str) -> bool:
    if (method, path) in _OWNER_ONLY_EXEMPT:
        return False
    for methods, prefix in _OWNER_ONLY_ROUTES:
        if (path == prefix or path.startswith(prefix + "/")) and (
            methods is None or method in methods
        ):
            return True
    return False


def _require_owner(request) -> None:
    """403 unless the caller is the owner (always true outside multi-user mode,
    where the one token holder or the loopback user is the owner)."""
    if not getattr(request.state, "evi_owner", True):
        raise HTTPException(403, "only the owner can do this (multi-user mode)")


# `/api/session/<id>/...`: the id is a path segment on every per-session route.
_SESSION_PATH = re.compile(r"^/api/session/([^/]+)/")

# Pending permission decisions resolve to deny at this deadline unless
# `[web] decision_timeout_s` (or EVI_DECISION_TIMEOUT_S) says otherwise.
_DECISION_TIMEOUT_DEFAULT_S = 300.0


def _decision_timeout_s(cfg: Any = None) -> float:
    """Seconds a permission prompt waits for an answer before it denies.
    EVI_DECISION_TIMEOUT_S overrides `[web] decision_timeout_s`; anything that
    isn't a positive number means the default — a prompt always has one."""
    configured = getattr(getattr(cfg, "web", None), "decision_timeout_s", None)
    for raw in (os.environ.get("EVI_DECISION_TIMEOUT_S"), configured):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and math.isfinite(value):
            return value
    return _DECISION_TIMEOUT_DEFAULT_S


def _deny_unattended(_tool_name: str, _args_json: str, _category: str) -> bool:
    """The permission callback while no client is attached to a session (between
    turns, channel runs): a tool that would ask is denied. With no callback at
    all the Agent follows [auto] unattended_policy, which the user may set to
    "allow" for their own scheduled jobs; a session reachable from outside
    (channel push, a stale stream) is never left without this one."""
    return False


@contextmanager
def _unattended_run(entry: str, settings: Any):
    """Mark a server-side run nobody watches (`evi.llm.cli_agent.unattended`):
    a delegate CLI-agent backend the user hasn't allowed for unattended runs
    ([llm] allow_cli_agent_unattended) is a 409 naming the setting, before any
    agent work."""
    from evi.llm.cli_agent import CliAgentUnattended, unattended

    try:
        with unattended(entry, settings):
            yield
    except CliAgentUnattended as exc:
        raise HTTPException(409, str(exc)) from None


def _unattended_policy(cfg: Config) -> str:
    """The policy a local unattended run on `cfg` follows, as its response
    reports it (`evi.llm.agent.policy_label`): [auto] unattended_policy
    ("deny" | "allow"), naming an [auto] mode that lets calls run unasked."""
    from evi.llm.agent import config_policy_label

    return config_policy_label(cfg)


# Uploads (and audio sent for transcription) stream to disk with this cap.
_UPLOAD_MAX_BYTES = 32 * 1024 * 1024


async def _save_upload(file: UploadFile, target: Path) -> int:
    """Stream `file` to `target`, refusing (413, nothing left behind) past
    _UPLOAD_MAX_BYTES. Returns the bytes written."""
    written = 0
    with target.open("wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > _UPLOAD_MAX_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                cap = _UPLOAD_MAX_BYTES
                size = f"{cap // (1024 * 1024)} MB" if cap >= 1024 * 1024 else f"{cap} bytes"
                raise HTTPException(413, f"upload exceeds {size}")
            out.write(chunk)
    return written


def _within(path: Path, root: Path) -> bool:
    """True if `path` resolves strictly inside `root` (`..`, absolute or
    drive-relative components and symlinks all resolved first)."""
    try:
        p, r = path.resolve(), root.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return p != r and p.is_relative_to(r)


_BAD_SESSION_ID = (
    f"invalid session_id (expected 1-{SESSION_ID_MAX_LEN} letters, digits, '_' or '-')"
)


def _require_session_id(session_id: str) -> str:
    """`session_id`, or HTTP 400 if it isn't a plain token (see ids.py).

    One contract for every surface: the id names the session's transcript file
    and its uploads dir, so a path-like one (`../..`, `C:/x`) is refused up
    front rather than at whichever join happens to see it first."""
    if not valid_session_id(session_id):
        raise HTTPException(400, _BAD_SESSION_ID)
    return session_id


def _session_upload_dir(session_id: str) -> Path:
    """`UPLOADS_DIR/<session_id>`, or HTTP 400 if the id isn't a plain token.

    The id arrives as a raw form field / URL segment, and pathlib walks `..` or
    swaps the base for an absolute id (`C:/x`, `/abs`), so an unchecked join
    writes anywhere the user can. Shape check first, then — defense in depth —
    the resolved path must still sit under UPLOADS_DIR.
    """
    target = UPLOADS_DIR / _require_session_id(session_id)
    if not _within(target, UPLOADS_DIR):
        raise HTTPException(400, _BAD_SESSION_ID)
    return target


# Human labels for the first-run ladder. The point of naming the product rather
# than the backend kind is that a beginner recognises "Claude Code", not
# "claude_agent".
# NB: detection is a PATH lookup — it proves the CLI is installed, not that it's
# logged in. The copy says "installed", never "signed in"; the live verify step
# is what actually proves it answers.
_CLI_AGENT_LABELS: dict[str, tuple[str, str]] = {
    "claude_agent": ("Claude Code", "Installed here — uses your Claude plan. No key, no download."),
    "codex": ("Codex", "Installed here — uses your ChatGPT plan. No key, no download."),
    "gemini": ("Gemini CLI", "Installed here — uses your Google login. No key, no download."),
    "copilot": ("GitHub Copilot", "Installed here — uses your Copilot subscription."),
    "amp": ("Amp", "Installed here — uses your Amp subscription."),
    "qwen": ("Qwen Code", "Installed here — uses your Qwen login."),
}
_SERVER_LABELS: dict[str, str] = {
    "ollama": "Ollama", "lmstudio": "LM Studio", "llamacpp": "llama.cpp",
}


def _first_run_suggestions(data: dict) -> list[dict]:
    """Rank the ways this machine could answer a prompt RIGHT NOW.

    Ordered by time-to-first-reply, not by preference: something already
    installed and authenticated beats a multi-gigabyte download every time.
    Pure function of an already-probed status dict, so it costs nothing extra.
    """
    out: list[dict] = []
    for kind in data.get("cli_agents", []):  # type: ignore[union-attr]
        name, detail = _CLI_AGENT_LABELS.get(kind, (kind, "Installed on this PC."))
        out.append({"kind": kind, "action": "use", "title": f"Use {name}",
                    "detail": detail, "badge": "installed", "instant": True})
    for c in data.get("candidates", []):  # type: ignore[union-attr]
        if c.get("reachable"):
            name = _SERVER_LABELS.get(c["kind"], c["kind"])
            out.append({"kind": c["kind"], "action": "use", "title": f"Use {name}",
                        "detail": f"Already running at {c.get('url', '')}.",
                        "badge": "running", "instant": True})
    for key in data.get("api_keys", []):  # type: ignore[union-attr]
        pretty = {"anthropic": "Anthropic", "openai": "OpenAI"}.get(key, key)
        out.append({"kind": "openai_compat", "action": "env", "title": f"Use your {pretty} API key",
                    "detail": "Found in this machine's environment.",
                    "badge": "key found", "instant": True})
    if data.get("runtime_supported"):
        installed = data.get("runtime_installed")
        out.append({
            "kind": "llamacpp", "action": "runtime",
            "title": "Start local AI" if installed else "Download a model",
            "detail": ("Ready to start — runs on this PC, nothing leaves it."
                       if installed else
                       "About 1 GB. Runs on this PC, nothing leaves it. No account."),
            "badge": "key-free", "instant": bool(installed),
        })
    return out


def _probe_candidate(kind: str, base_url: str) -> tuple[bool, str]:
    """Return (reachable, resolved_url) for one backend.

    CLI-agent kinds have no HTTP endpoint, so "reachable" = their CLI is on PATH.
    llama.cpp gets the 8080..8090 port scan so a busy default port doesn't hide
    it; everything else is a single-URL HTTP probe. The resolved value lets the
    UI show where llama.cpp was found (or which CLI a CLI-agent kind needs).
    """
    cli = _CLI_AGENT_BINS.get(kind)
    if cli is not None:
        import shutil
        return (shutil.which(cli) is not None, f"{cli} CLI")
    if kind == "llamacpp":
        found = discover_llamacpp_url(base_url)
        return (found is not None, found or base_url)
    return (_probe_backend(base_url), base_url)


class PickerUpdate(BaseModel):
    """Patch shape for POST /api/model-picker. All fields optional."""

    model: str | None = None
    # Registry entry name to switch the ACTIVE backend to (its kind/base_url/
    # api_key get copied into [llm]). None = keep the current backend.
    backend: str | None = None
    fast_model: str | None = None
    effort: str | None = None
    fast_mode: bool | None = None


class BackendUpsert(BaseModel):
    """POST /api/backends — register a backend, either from a preset or custom."""

    name: str | None = None            # label; defaults to the preset name/kind
    preset: str | None = None          # openai|xai|anthropic|openrouter|groq|together
    kind: str | None = None            # lmstudio|ollama|llamacpp|openai_compat (custom)
    base_url: str | None = None
    api_key: str | None = None         # inline secret OR "env:VARNAME"
    enabled: bool | None = None
    fanout: bool | None = None         # allow this backend's models for subagent fan-out


class BackendFlags(BaseModel):
    """PATCH /api/backends/{name} — toggle enabled / fanout on an existing entry."""

    enabled: bool | None = None
    fanout: bool | None = None


class TruncateRequest(BaseModel):
    after_index: int


class EditRequest(BaseModel):
    at_index: int
    content: str


class BranchRequest(BaseModel):
    at_index: int


class ModeRequest(BaseModel):
    mode: str  # chat | cowork | code


class CwdRequest(BaseModel):
    session_id: str
    path: str


class ProjectTrustRequest(BaseModel):
    session_id: str
    trust: bool = True  # False revokes
    # The `config_sha256` the GET showed the user: refused if the file has
    # changed since, so what gets approved is what was reviewed.
    config_sha256: str = ""


class CompleteRequest(BaseModel):
    prefix: str
    suffix: str = ""
    model: str = ""
    max_tokens: int = 128


# --- full-config read/write (settings screen) ----------------------------
#
# Secret fields are never sent to the browser in the clear. GET returns the
# sentinel for any non-empty secret; POST treats an incoming sentinel as
# "leave unchanged" (so re-saving the form doesn't wipe a token the user
# never saw). An empty string still clears the value.
_SECRET_FIELDS: dict[str, frozenset[str]] = {
    "llm": frozenset({"api_key"}),
    "web": frozenset({"auth_token"}),
    "telemetry": frozenset({"dsn"}),
    # A bot token is a bearer credential for the bot — anyone holding it can
    # impersonate it, so it must never be echoed back by /api/config.
    "channels": frozenset({"telegram_token"}),
}
_SECRET_SENTINEL = "********"
# Sections that can be hot-applied to a live chat. Everything persists to
# disk regardless; these also push onto in-memory sessions so the change
# takes effect without starting a new chat.
_HOT_SECTIONS = (
    "llm", "auto", "tools", "telemetry", "comfy",
    "web", "google", "microsoft", "obsidian",
)

# Holds the single in-process HTTP MCP server the UI can start/stop (eVi serving
# ITSELF as an MCP server). {server, thread, port, categories, token_set}.
_mcp_serve: dict[str, Any] = {}

# Multi-user (opt-in): the authenticated user for the in-flight request, set by
# the auth middleware. None = single-user / open access (the default). Read in
# the request task to scope sessions + data dirs per user.
_CURRENT_USER: ContextVar[str | None] = ContextVar("evi_current_user", default=None)


def _safe_user(name: str) -> str:
    """Filesystem-safe slug for a user's data dir (blocks path traversal)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip()).strip("._-")
    return slug or "user"


# Multi-user mode keys the owner's sessions as "owner" and keeps its transcripts
# and memory in users/owner.
_OWNER_LOGIN = "owner"


def _reserved_login(name: str) -> bool:
    """True for a users.json login that would land in the owner's workspace:
    named owner in any case (Windows and macOS fold case in paths), or with a
    name whose data-dir slug is owner ("owner.", "-owner-")."""
    return (name.strip().casefold() == _OWNER_LOGIN
            or _safe_user(name).casefold() == _OWNER_LOGIN)


def _login_dir_key(name: str) -> str:
    """What decides that two logins share a data dir: its name (_safe_user),
    case-folded, since Windows and macOS fold case in paths."""
    return _safe_user(name).casefold()


def _colliding_logins(users: list) -> dict[str, list[str]]:
    """{login name: [the other logins it would share a data dir with]} for
    each users.json login whose users/<dir> a differently named login maps to
    as well: 'bob smith', 'bob_smith' and 'bob/smith' are all users/bob_smith,
    and 'Alice' and 'alice' are one folder on Windows and macOS. None of them
    signs in — they would read each other's transcripts and memory — until the
    owner renames all but one. (Entries with the very same name are one login
    with several tokens, not a collision.)"""
    by_dir: dict[str, list[str]] = {}
    for u in users:
        names = by_dir.setdefault(_login_dir_key(u.name), [])
        if u.name not in names:
            names.append(u.name)
    return {n: [m for m in names if m != n]
            for names in by_dir.values() if len(names) > 1 for n in names}


# Collision sets already logged, so the warning comes once per users.json state
# rather than on every request (users.json is re-read per request).
_logged_collisions: set[frozenset[str]] = set()


def _note_login_collisions(users: list) -> None:
    """Log each new login collision for the owner, naming every login in it
    (a refused user is only told that one exists: _collision_refusal)."""
    for name, others in _colliding_logins(users).items():
        group = frozenset([name, *others])
        if group not in _logged_collisions:
            _logged_collisions.add(group)
            logger.warning(
                "users.json: the logins %s all map to the data folder users/%s; none "
                "of them can sign in until all but one are renamed or removed",
                ", ".join(repr(n) for n in sorted(group)), _safe_user(name))


def _collision_refusal(name: str) -> str:
    return (f"The login {name!r} can't sign in: another login in users.json maps to the "
            f"same data folder (users/{_safe_user(name)}), so they would share chats and "
            "memory. Ask the owner of this eVi to rename one of them.")


def _authenticate_login(provided: str, users: list) -> Any:
    """The users.json login `provided` belongs to, else None. A login with a
    reserved name never signs in — it would share the owner's sessions,
    transcripts and memory (`evi doctor` flags it) — nor does one whose data
    dir another login maps to as well (_colliding_logins; _refused_login says
    why)."""
    from evi.users import authenticate

    if not provided or not provided.isascii():  # compare_digest wants ASCII str
        return None
    colliding = _colliding_logins(users)
    return authenticate(provided, [u for u in users
                                   if not _reserved_login(u.name) and u.name not in colliding])


def _refused_login(provided: str, users: list) -> str | None:
    """Why `provided`, a token of a users.json login that may not sign in
    because its data dir collides with another login's, is refused — else
    None (an unknown token stays a plain 401)."""
    from evi.users import authenticate

    if not provided or not provided.isascii():
        return None
    colliding = _colliding_logins(users)
    if not colliding:
        return None
    user = authenticate(provided, [u for u in users
                                   if u.name in colliding and not _reserved_login(u.name)])
    return _collision_refusal(user.name) if user is not None else None


def _config_snapshot(cfg: Config) -> dict[str, Any]:
    """asdict(cfg) with secrets masked for transport to the browser, plus
    `auto_approve_migration`: `[auto] auto_approve` with "code" replaced by
    "code_read" when the list still holds the old default's "code" (which now
    auto-approves running code), else None. Settings → Permissions offers it
    as a one-click switch (evi.permissions.auto_approve_migration, the fix
    `evi lint` and `evi doctor` suggest). It is no config section, so a POST
    of it back changes nothing (_apply_config_patch skips it)."""
    from evi.permissions import auto_approve_migration

    data = asdict(cfg)
    for section, secrets_ in _SECRET_FIELDS.items():
        body = data.get(section, {})
        for key in secrets_:
            if body.get(key):
                body[key] = _SECRET_SENTINEL
    data["auto_approve_migration"] = auto_approve_migration(cfg.auto.auto_approve)
    return data


def _coerce_to(current: Any, value: Any) -> Any:
    """Coerce an incoming JSON value to the type of the existing field.

    bool must be checked before int (bool is a subclass of int). Lists are
    shallow-copied; everything else falls back to the value as-is (str).
    """
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        return list(value)
    return value


def _docs_dir() -> Path | None:
    """Locate the bundled ``docs/`` folder across install layouts.

    Source/editable installs find it at the repo root; the frozen desktop
    sidecar finds the copy PyInstaller stages (``--add-data docs``) under
    ``sys._MEIPASS``. Returns None when no docs ship (plain ``pip install`` —
    the UI then falls back to the public wiki link)."""
    import sys

    meipass = getattr(sys, "_MEIPASS", "")
    candidates = [
        Path(meipass) / "docs" if meipass else None,
        Path(__file__).resolve().parents[3] / "docs",  # repo root (web→apps→evi→root)
        Path(__file__).resolve().parent / "docs",
    ]
    for c in candidates:
        if c is not None and c.is_dir():
            return c
    return None


def _doc_title(path: Path) -> str:
    """First ``# `` heading, else a title-cased slug."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ").title()


def _apply_config_patch(cfg: Config, patch: dict[str, Any]) -> list[str]:
    """Mutate `cfg` in place from a nested {section: {key: val}} patch.

    Returns the list of section names actually touched. Unknown sections and
    unknown keys are skipped (forward-compatible with older configs). Secret
    fields left at the sentinel are not overwritten.
    """
    touched: list[str] = []
    for section, values in patch.items():
        target = getattr(cfg, section, None)
        if target is None or not is_dataclass(target) or not isinstance(values, dict):
            continue
        valid = {f.name for f in fields(target)}
        secrets_ = _SECRET_FIELDS.get(section, frozenset())
        changed = False
        for key, raw in values.items():
            if key not in valid:
                continue
            if key in secrets_ and raw == _SECRET_SENTINEL:
                continue  # unchanged — user never saw the real value
            setattr(target, key, _coerce_to(getattr(target, key), raw))
            changed = True
        if changed:
            touched.append(section)
    return touched


@dataclass
class PendingDecision:
    """Holds a worker-thread block waiting for the browser to answer."""

    event: threading.Event
    approved: bool = False


@dataclass
class WebSession:
    """One browser session — an Agent plus its pending-permission registry."""

    agent: Agent
    pending: dict[str, PendingDecision] = field(default_factory=dict)
    mode: str = "chat"  # Chat / Cowork / Code — gates the agent's tool set
    channel_log: list[dict[str, str]] = field(default_factory=list)  # pushed-in alerts (Ph 83)
    busy: bool = False  # a turn is mid-flight — surfaced by the dispatch live-watch
    title: str = ""  # server-stored session name, for addressing in session messaging (Ph 95)
    # Guards `busy`: one turn per session. Two workers on one Agent would
    # interleave history and swap each other's permission callback.
    turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # Notes (session messages, channel pushes) that arrived mid-turn, held until
    # the turn ends: appended then, one could land between a tool call and its
    # result, which strict backends reject (_post_note). A re-roll holds the
    # ones after the reply it regenerates (_hold_notes_past_last_prompt).
    held_notes: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    # id() of the held notes to log when they join the history (_log_note):
    # the ones delivered mid-turn, and the ones a re-roll holds again, whose
    # earlier line its recorded cut dropped from the replay. Each id belongs
    # to a note in held_notes.
    unlogged_notes: set[int] = field(default_factory=set, repr=False, compare=False)
    # Why the claimed turn was cancelled (cancel_turn), None if it wasn't. A
    # cancel can land after the session is claimed but before the turn exists
    # (begin_turn, then agent.chat()): start_turn applies it then.
    cancel_reason: str | None = field(default=None, repr=False, compare=False)

    def begin_turn(self) -> bool:
        """Claim the session for a turn; False if one is already running."""
        with self.turn_lock:
            if self.busy:
                return False
            self.busy = True
            self.cancel_reason = None
            return True

    def start_turn(self, start):
        """Start the claimed turn: `start()` calls agent.chat / continue_chat,
        which begins the turn right then (a fresh cancel token) and returns its
        events for a worker to iterate. A cancel sent since the claim
        (cancel_turn) is applied to it, so a Stop pressed right after Send
        sticks. If `start()` raises, the events raise it, so the stream ends
        with an Error as when the worker started the turn itself."""
        try:
            events = start()
        except Exception as exc:  # noqa: BLE001 — reported on the stream
            def _failed(exc: Exception = exc):
                raise exc
                yield  # a generator, so the worker raises it while iterating

            return _failed()
        with self.turn_lock:
            reason = self.cancel_reason
        if reason is not None:
            cancel = getattr(self.agent, "cancel", None)
            if callable(cancel):
                cancel(reason)
        return events

    def cancel_turn(self, reason: str) -> bool:
        """Cancel the running turn (`Agent.cancel`: it ends with
        Done("interrupted") and the worker frees the session) and deny every
        permission question it is waiting on, which a cancel can't interrupt
        on its own. False when no turn is running."""
        with self.turn_lock:
            if not self.busy:
                return False
            self.cancel_reason = reason
            cancel = getattr(self.agent, "cancel", None)
            if callable(cancel):
                cancel(reason)
            waiting = tuple(self.pending.values())
        for pending in waiting:
            pending.approved = False
            pending.event.set()
        return True

    def end_turn(self) -> None:
        with self.turn_lock:
            self.busy = False
            self._release_notes()

    def _release_notes(self) -> None:
        """Append the notes held during the turn, logging the ones in
        unlogged_notes, after the turn's own messages, so a revived session
        has them in the same place. Caller holds turn_lock."""
        if self.held_notes:
            self.agent.history.extend(self.held_notes)
            for note in self.held_notes:
                if id(note) in self.unlogged_notes:
                    _log_note(self.agent, note)
            self.held_notes.clear()
        self.unlogged_notes.clear()

    def steer(self, text: str) -> bool | None:
        """Queue a follow-up on the running turn: None when no turn is running,
        else whether the agent took it. Under turn_lock, like finish_turn, so a
        follow-up is either handed back by the turn or refused here — never
        queued on an agent whose turn has already ended."""
        with self.turn_lock:
            if not self.busy:
                return None
            return self.agent.steer(text)

    def finish_turn(self) -> list[str]:
        """Free the session and return the follow-ups the turn never applied
        (it ended before another step), for the stream to hand back."""
        with self.turn_lock:
            self.busy = False
            self._release_notes()
            take = getattr(self.agent, "take_pending_steering", None)
            return list(take()) if take else []


def _busy_error(session_id: str) -> HTTPException:
    """409 for a request that would start or rewrite a turn mid-flight."""
    return HTTPException(
        409,
        f"session {session_id} is busy: a turn is already running. Send a "
        f"follow-up with POST /api/session/{session_id}/steer (it applies at the "
        "turn's next step), or wait for the turn to finish.",
    )


@contextmanager
def _holding_turn(sess: WebSession, session_id: str):
    """Hold the session for a quick history rewrite (/reset, truncate, edit…):
    409 if a turn is running, and no turn can start until it's done."""
    if not sess.begin_turn():
        raise _busy_error(session_id)
    try:
        yield
    finally:
        sess.end_turn()


def _stream_turn(sess: WebSession, run, enqueue) -> None:
    """Run one claimed turn (`run()` yields its events) on a worker thread and
    feed it to an SSE stream via `enqueue`; frees the session when it ends.

    The frame that closes the turn (Done, or the Error that ended it) goes out
    LAST. A turn can end before there's another step (no tool calls, an error,
    max_turns), and a follow-up still queued then was never applied: it's
    handed back first (Steering applied=false) so the UI resends it. After
    Done it could arrive in a later network read, which the UI never makes.
    """
    closing: dict | None = None
    try:
        for event in run():
            payload = _event_payload(event)
            if isinstance(event, (Done, Error)):
                closing = payload
                break
            enqueue(payload)
    except Exception as exc:  # noqa: BLE001
        closing = {"kind": "Error", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        # Detach this turn's client before freeing the session, so a later
        # headless run (channel run:true) can't reach the dead stream's
        # callback and wait on it.
        sess.agent.permission_callback = _deny_unattended
        # Guarded, and strictly before the sentinel: anything raising here
        # would skip `enqueue(None)` and leave the SSE stream open forever.
        # A hang is worse than a dropped follow-up.
        try:
            leftovers = sess.finish_turn()
        except Exception:  # noqa: BLE001
            leftovers = []
        for text in leftovers:
            enqueue({"kind": "Steering", "text": text, "applied": False})
        if closing is not None:
            enqueue(closing)
        enqueue(None)  # sentinel — must always run or the stream never closes


def _turn_cancelled(sess: WebSession) -> bool:
    """Whether the turn a permission question asked on this thread belongs to
    was cancelled: that of the tool call running here (a subagent asks on its
    parent's tool thread) or of one it runs inside, or the session's own."""
    from evi.tools.base import active_context

    ctx = active_context()
    while ctx is not None:
        token = getattr(ctx, "cancel_token", None)
        if token is not None and token.is_set():
            return True
        ctx = getattr(ctx, "parent", None)
    token = getattr(sess.agent, "cancel_token", None)
    return isinstance(token, threading.Event) and token.is_set()


def _make_permission_callback(bucket: "dict[str, WebSession]", session_id: str,
                              enqueue, timeout_s: float):
    """Build a permission_callback that bridges worker thread → SSE client.

    The callback (invoked on a worker thread inside Agent.chat) generates a
    decision_id, pushes a PermissionRequest into the SSE queue via `enqueue`,
    and waits for /api/decide — up to `timeout_s`, then it denies (and says so
    with a PermissionExpired event). A dropped SSE stream does NOT deny: the
    decision stays answerable until the deadline (another tab, a reattach).

    `bucket` is captured here (in the request task) so the worker-thread
    lookup resolves the right user's session without the ContextVar.

    Once the turn is cancelled (POST /api/session/{id}/cancel) nothing more
    is asked: a question already waiting is denied by WebSession.cancel_turn,
    and one asked after it (a second subagent that waited its turn to ask)
    is denied here without being posted. The arguments shown are redacted
    (`evi.redact`).
    """
    def callback(tool_name: str, args_json: str, category: str) -> bool:
        decision_id = secrets.token_hex(8)
        sess = bucket.get(session_id)
        if sess is None or _turn_cancelled(sess):
            return False
        pending = PendingDecision(event=threading.Event())
        sess.pending[decision_id] = pending
        # Checked again once registered: a cancel_turn from here on denies it,
        # and one that came in between saw nothing to deny.
        if _turn_cancelled(sess):
            sess.pending.pop(decision_id, None)
            return False
        enqueue({
            "kind": "PermissionRequest",
            "decision_id": decision_id,
            "tool_name": tool_name,
            "args": Redactor().args(args_json),
            "category": category,
            "timeout_s": timeout_s,
        })
        answered = pending.event.wait(timeout_s)
        sess.pending.pop(decision_id, None)
        if answered or pending.event.is_set():  # an answer racing the deadline wins
            return pending.approved
        enqueue({
            "kind": "PermissionExpired",
            "decision_id": decision_id,
            "tool_name": tool_name,
            "timeout_s": timeout_s,
        })
        return False
    return callback


# Slash commands that only read state, so they may answer while a turn runs.
# Everything else rewrites history or the agent's settings (/reset, /compact,
# /model <id>, /goal <text>, /auto on, /plan, user commands that start a turn).
_READ_ONLY_SLASH = frozenset({"help", "?", "tools"})
_READ_ONLY_WITHOUT_ARGS = frozenset({"model", "goal", "auto"})


def _slash_is_read_only(raw: str) -> bool:
    name, _, args = raw[1:].strip().partition(" ")
    name = name.lower()
    return name in _READ_ONLY_SLASH or (name in _READ_ONLY_WITHOUT_ARGS and not args.strip())


# ---- federation activity (inbound peer requests) -------------------------
# Module-level so it survives across requests. federate() records each delegated
# task here; the dispatch snapshot exposes `active` + recent items so the UI can
# show a "processing a peer request" indicator on the RECEIVER.
from collections import deque as _deque  # noqa: E402

_FED_ACTIVITY: "_deque[dict[str, Any]]" = _deque(maxlen=20)
_FED_ACTIVE = {"n": 0}


def _fed_begin(source: str, task: str) -> dict[str, Any]:
    import time

    entry = {"source": source or "peer", "task": task[:100],
             "ts": time.time(), "status": "running"}
    _FED_ACTIVITY.appendleft(entry)
    _FED_ACTIVE["n"] += 1
    return entry


def _fed_end(entry: dict[str, Any], status: str) -> None:
    entry["status"] = status
    _FED_ACTIVE["n"] = max(0, _FED_ACTIVE["n"] - 1)


def _is_loopback_host(host: str) -> bool:
    import ipaddress
    h = (host or "").strip().lower()
    if h in ("127.0.0.1", "::1", "localhost", "") or h.startswith("127."):
        return True
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        # Not a real IP — e.g. the in-process TestClient's synthetic "testclient"
        # host. A request off a real socket always carries an IP, so only a
        # parseable non-loopback IP is a genuine remote peer; treat the rest local.
        return True
    # A dual-stack socket reports an IPv4 client as ::ffff:a.b.c.d.
    mapped = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or bool(mapped is not None and mapped.is_loopback)


def _federation_lan_guard(request: "Request", cfg: Any) -> None:
    """Fail-safe: refuse a federation/A2A request from a NON-loopback client
    when no auth token is set. The bind-to-LAN decision lives in the desktop
    shell (main.rs) or a manual `evi web --host 0.0.0.0`, so a config-side check
    can't catch every path — but an OPEN server (`[web] auth_token` empty) reachable
    on the LAN would let anyone delegate tasks. We gate at the request instead:
    loopback (local testing) is always allowed; a remote peer must have a token
    configured (auth_middleware then enforces it). This closes the open-on-LAN
    hole regardless of how the host was bound."""
    client_host = request.client.host if request.client else ""
    if _is_loopback_host(client_host):
        return
    if not _web_tokens(cfg):
        raise HTTPException(
            403,
            "federation refused: this eVi is serving on the network without an "
            "auth token, so remote delegation is disabled. Set [web] auth_token "
            "to accept peers (local/loopback requests are unaffected).",
        )


# ---- intra-instance session messaging (Phase 95) -------------------------
# Let one live web session send a lightweight PLAIN-TEXT message to another of
# YOUR live sessions in the same sidecar (the common desktop case: several chat
# tabs). Delivery reuses the Phase-83 channel mechanism: the note lands in the
# target's history (seen on its next turn) + channel_log (the UI inbox). A
# message is plain text — it never approves a permission, changes config, or runs
# a command in the target; any tool the receiving agent then runs still goes
# through that session's own permission prompt. Scoped to one user's bucket, so
# tabs can only reach tabs (never another user's sessions).

_MSG_LOG_CAP = 100  # keep the per-session inbox bounded


def _with_user_hooks(agent: Agent) -> Agent:
    """Give a server-built agent the user's hooks: the one registry this
    process shares, which POST /api/hooks reloads in place, so an open session
    picks up a Save without a restart. Empty in safe mode (load_hooks)."""
    agent.hooks = hooks_mod.shared_registry()
    return agent


def _reload_user_hooks() -> int:
    """Re-read hooks.toml and plugin hooks into the shared registry; the count."""
    return len(hooks_mod.reload_shared().hooks)


def _follow_runtime_window(sess: "WebSession") -> None:
    """Keep an open session's compaction ceiling at the managed llama.cpp
    server's window: a model swap or GPU switch relaunches it with a new `-c`
    after the session loaded its config (Agent.refresh_context_size). Guarded
    so lightweight test doubles still work."""
    refresh = getattr(sess.agent, "refresh_context_size", None)
    if callable(refresh):
        try:
            refresh()
        except Exception:  # noqa: BLE001 — the old ceiling beats a failed request
            pass


def _deliver_note(sess: "WebSession", source: str, text: str) -> bool:
    """Append a pushed-in message to a session's history (and transcript, so a
    revived session keeps it: _post_note) + channel_log. Dedupes an identical
    (source, text) repeat of the most recent entry (loop guard) and caps the
    log. Returns True if delivered, False if deduped."""
    source = (str(source or "channel").strip() or "channel")[:64]
    text = str(text or "")
    last = sess.channel_log[-1] if sess.channel_log else None
    if last is not None and last.get("source") == source and last.get("text") == text:
        return False  # identical repeat — drop (loop / double-send guard)
    # Framed clearly as a peer message so the receiving model treats it as
    # information from another session (or whoever named themself in POST
    # /message's `from`), NOT an instruction from the user. (Any tool it then
    # runs still goes through this session's own permission prompt.) "you" is
    # the default for that `from`: the Dispatch panel's own compose, so the
    # user's words, which are theirs to give.
    if source == "you":
        note = note_message(text, None)
    elif source.startswith("session:"):
        note = note_message(text, source[len("session:"):], "session")
    else:
        note = note_message(text, source)
    _post_note(sess, note)
    sess.channel_log.append({"source": source, "text": text, "ran": False})
    if len(sess.channel_log) > _MSG_LOG_CAP:
        del sess.channel_log[: len(sess.channel_log) - _MSG_LOG_CAP]
    return True


def _post_note(sess: "WebSession", note: dict[str, str]) -> None:
    """Add a note to the session's history now, or, while a turn is running,
    when it ends (WebSession.held_notes): appended mid-turn it could land
    between a tool call and its result. It goes to the transcript when it
    joins the history (_log_note), so the transcript keeps the history's
    order."""
    lock = getattr(sess, "turn_lock", None)
    if lock is None:  # a lightweight test double: no turns to wait for
        sess.agent.history.append(note)
        _log_note(sess.agent, note)
        return
    with lock:
        if sess.busy:
            sess.held_notes.append(note)
            sess.unlogged_notes.add(id(note))
        else:
            sess.agent.history.append(note)
            _log_note(sess.agent, note)


def _log_note(agent: Any, note: dict[str, Any]) -> None:
    """Write a note the session's history just took to its transcript, as the
    user line it is (note_message: never system, so a revived session gets it
    back where it was, through the same path as every other message, and
    evi.history.is_note still tells it from a prompt). Without a transcript
    (transcripts off, a test double) there is nothing to write; a disk error
    never fails the delivery."""
    store = getattr(agent, "transcripts", None)
    write = getattr(store, "write_message", None)
    if not callable(write):
        return
    try:
        write(session=getattr(agent, "session_id", ""), role="user",
              content=str(note.get("content") or ""))
    except OSError:
        pass


def _hold_notes_past_last_prompt(sess: "WebSession") -> None:
    """Before a re-roll rewinds: move the notes that arrived after the last
    prompt the user typed into held_notes, ahead of any held already. A note is
    a user message too, so the rewind would otherwise stop at it and answer it,
    keeping the reply being re-rolled. Held, they come back after the new reply
    when the turn ends. Caller has claimed the turn.

    With notes to hold, the history is cut back to that prompt here, notes
    and reply together, with Agent.truncate_history, so the transcript records
    the cut (the rewind then has nothing left to drop). The cut takes the notes
    out of the transcript's replay too, so each is logged again when it
    rejoins the history, after the new reply: a revived session has them
    there."""
    with sess.turn_lock:
        hist = sess.agent.history
        last = next(
            (i for i in range(len(hist) - 1, 0, -1)
             if hist[i].get("role") == "user" and not is_note(hist[i])),
            None,
        )
        if last is None:
            return  # nothing but notes to answer: leave it to the rewind
        notes = [m for m in hist[last + 1:] if is_note(m)]
        if notes:
            sess.agent.truncate_history(last)
            sess.held_notes[:0] = notes
            sess.unlogged_notes.update(id(n) for n in notes)


# The user half of the compaction pair (evi.history.compaction_messages).
_COMPACTION_SUMMARY = re.compile(r"\[compacted: \d+ earlier messages summarised\]\n")


def _history_kind(history: list[dict[str, Any]], i: int) -> str | None:
    """What the browser should draw history[i] as when it isn't a plain
    message: "note" for a note from outside the chat (evi.history.is_note),
    "compaction" for the summary compaction put first after the system prompt,
    and "compaction_ack" for the assistant acknowledgement right after it
    (evi.history.compaction_messages). None for everything else."""
    m = history[i]
    if is_note(m):
        return "note"
    content = m.get("content")
    if not isinstance(content, str) or i not in (1, 2):
        return None
    if i == 1 and m.get("role") == "user" and _COMPACTION_SUMMARY.match(content):
        return "compaction"
    if (
        i == 2 and m.get("role") == "assistant" and content == COMPACTION_ACK
        and not m.get("tool_calls") and _history_kind(history, 1) == "compaction"
    ):
        return "compaction_ack"
    return None


def _resolve_target(bucket: "dict[str, WebSession]", self_id: str, target: str):
    """Resolve a target session within `bucket` (excluding self) by exact id,
    else exact case-insensitive title. Returns (id, WebSession), a list of
    candidate labels on ambiguity, or None when nothing matches."""
    target = str(target or "").strip()
    if not target:
        return None
    if target != self_id and target in bucket:
        return (target, bucket[target])
    matches = [
        (sid, s) for sid, s in bucket.items()
        if sid != self_id and (s.title or "").strip().lower() == target.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return [f"{s.title or sid[:8]} ({sid[:8]})" for sid, s in matches]
    return None


def _make_session_tools(bucket: "dict[str, WebSession]", self_id: str) -> list:
    """Build the per-session list_sessions + send_to_session tools, closing over
    the caller's session bucket + own id (the worker thread can't rely on the
    request ContextVar — same reason _make_permission_callback captures them)."""
    from evi.tools.base import Tool

    def list_sessions() -> str:
        out = []
        for sid, s in bucket.items():
            if sid == self_id:
                continue
            out.append({
                "id": sid,
                "name": s.title or sid[:8],
                "busy": bool(s.busy),
                "mode": s.mode,
                "messages": max(0, len(getattr(s.agent, "history", [])) - 1),
            })
        return json.dumps(out)

    def send_to_session(target: str, text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return "ERROR: text is required"
        tgt = _resolve_target(bucket, self_id, target)
        if tgt is None:
            return (f"ERROR: no other live session named '{target}'. "
                    "Call list_sessions to see reachable sessions.")
        if isinstance(tgt, list):
            return "ERROR: ambiguous target — matches: " + "; ".join(tgt) + ". Use the exact id."
        tsid, tsess = tgt
        me = bucket.get(self_id)
        sender = (me.title if me and me.title else self_id[:8])
        delivered = _deliver_note(tsess, f"session:{sender}", text)
        who = tsess.title or tsid[:8]
        return (f"delivered to session '{who}'." if delivered
                else f"(duplicate message to '{who}' skipped)")

    return [
        Tool(
            name="list_sessions",
            description=(
                "List your OTHER live chat sessions (open tabs) that you can message. "
                "Returns each one's id, name, whether it's busy, its mode, and message "
                "count. Call this before send_to_session to get a target name or id."
            ),
            parameters={"type": "object", "properties": {}},
            func=list_sessions,
            category="session",
        ),
        Tool(
            name="send_to_session",
            description=(
                "Send a short PLAIN-TEXT message to one of your other live sessions "
                "(address it by the name or id from list_sessions). Use it to hand a "
                "finding, status, or decision to a session working on something related "
                "— e.g. 'schema migration finished, rebasing is safe now'. The message "
                "is delivered as a note the other session sees on its next turn; it does "
                "NOT run commands or approve anything there."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "target session name or id (from list_sessions)"},
                    "text": {"type": "string", "description": "the plain-text message to deliver"},
                },
                "required": ["target", "text"],
            },
            func=send_to_session,
            category="session",
        ),
    ]


# ---- event serialization -------------------------------------------------
# One versioned contract for SSE and `evi run --format stream-json`
# (evi/events.py): a turn's stream wraps each payload in the envelope
# ({schema_version, kind, seq, ts}) as it sends it (_sse_frames).


def _event_kind(event: Event) -> str:
    return _events.event_kind(event)


def _event_payload(event: Event) -> dict[str, Any]:
    """An agent Event as the JSON object an SSE stream sends, without the
    envelope (evi.events.event_payload: a tool call's arguments and a tool's
    output, and its citations, are redacted)."""
    return _events.event_payload(event)


def _serialize(event: Event) -> str:
    """Render an agent Event as a JSON line for SSE."""
    return _events.serialize(event)


async def _sse_frames(payloads: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, str]]:
    """A turn's payloads as SSE messages, each in the versioned envelope and
    numbered in the order sent (evi.events.EventStream)."""
    frames = _events.EventStream()
    async for payload in payloads:
        yield {"event": "message", "data": frames.dumps(payload)}


# ---- slash commands (server-side) ---------------------------------------


@dataclass
class _SlashOutcome:
    handled: bool                 # True = no LLM call needed; emit `text` + Done
    text: str = ""
    expand_to: str | None = None  # If set, replace the user's message with this
    # True = the command wiped the session's history (e.g. /reset), so the web
    # UI should clear the visible transcript too — otherwise the screen keeps
    # showing messages the model has actually forgotten. (The CLI REPL redraws
    # from scratch, so this flag only matters to the web/desktop frontend.)
    clear_transcript: bool = False


def _handle_slash(agent: Agent, raw: str, cmd_store: CommandStore) -> _SlashOutcome:
    """Mirror the CLI dispatcher, returning either a handled-text response
    or an expanded prompt to forward to the LLM."""
    body = raw[1:].strip()
    if not body:
        return _SlashOutcome(handled=True, text="empty command — try /help")
    name, _, args = body.partition(" ")
    name = name.lower()
    args = args.strip()

    if name in ("help", "?"):
        builtins = [
            ("/help", "show this list"),
            ("/reset", "clear conversation history"),
            ("/tools", "list active tools"),
            ("/model [id]", "show / switch the active model"),
            ("/goal [text|clear]", "set / clear / show the ongoing goal"),
            ("/plan [task]", "next turn runs in plan-only mode"),
            ("/auto [on|off]", "auto-approve every tool call this session"),
            ("/compact", "summarise older history into one note"),
            ("/reload", "re-read config.toml across the running session"),
        ]
        lines = ["**Built-in commands**"]
        for cmd, desc in builtins:
            lines.append(f"- `{cmd}` — {desc}")
        user_cmds = cmd_store.list()
        if user_cmds:
            lines.append("\n**User commands** (`~/.evi/commands/`)")
            for e in user_cmds:
                lines.append(f"- `/{e.name}` — {e.summary}")
        return _SlashOutcome(handled=True, text="\n".join(lines))

    if name == "reset":
        agent.reset()
        return _SlashOutcome(handled=True, text="history cleared.", clear_transcript=True)

    if name == "tools":
        if not agent.tools:
            return _SlashOutcome(handled=True, text="(no tools enabled)")
        lines = [f"- **{t.name}** ({t.category})" for t in agent.tools.values()]
        return _SlashOutcome(handled=True, text="\n".join(lines))

    if name == "model":
        if not args:
            return _SlashOutcome(
                handled=True,
                text=f"**{agent.config.llm.model}** via {agent.config.llm.backend}",
            )
        cfg = Config.load()
        cfg.llm.model = args
        cfg.save()
        agent.config.llm.model = args
        agent.refresh_prompt()  # re-stitch identity so it reflects the new model
        return _SlashOutcome(handled=True, text=f"using **{args}** (persisted)")

    if name == "goal":
        if not args:
            return _SlashOutcome(
                handled=True,
                text=f"goal: **{agent.goal}**" if agent.goal else "no goal set",
            )
        if args.lower() == "clear":
            agent.clear_goal()
            return _SlashOutcome(handled=True, text="goal cleared")
        agent.set_goal(args)
        return _SlashOutcome(handled=True, text=f"goal set: **{args}**")

    if name == "plan":
        agent.enable_plan_mode()
        if args:
            # Treat trailing text as the task and forward to the LLM.
            return _SlashOutcome(handled=False, expand_to=args)
        return _SlashOutcome(
            handled=True,
            text="plan-only mode enabled for the next turn.",
        )

    if name == "reload":
        agent.refresh_config()
        return _SlashOutcome(
            handled=True,
            text=(
                f"**config reloaded** · model={agent.config.llm.model}"
                f" · effort={agent.config.llm.reasoning_effort}"
            ),
        )

    if name == "compact":
        collapsed = agent.compact_history()
        if collapsed == 0:
            return _SlashOutcome(handled=True, text="nothing to compact (history is short)")
        return _SlashOutcome(
            handled=True,
            text=f"**compacted** {collapsed} messages into a summary",
        )

    if name == "auto":
        a = args.lower()
        if a in ("on", "yes", "all"):
            agent.enable_auto_all()
            return _SlashOutcome(handled=True, text="auto mode ON — all tool calls auto-approved.")
        if a in ("off", "no"):
            agent.disable_auto_all()
            return _SlashOutcome(handled=True, text="auto mode OFF")
        status = "ON" if agent.auto_all else "OFF"
        cats = ", ".join(sorted(agent.auto_approve_categories)) or "(none)"
        return _SlashOutcome(
            handled=True,
            text=f"auto-all: **{status}** · always-allowed: {cats}",
        )

    # Fall through to user-defined command. Only the template's own @refs are
    # inlined (args stay text), confined to the session's folder + trusted dirs.
    try:
        expanded = cmd_store.expand_for_agent(agent, name, args)
    except (ValueError, OSError) as exc:  # e.g. its file isn't UTF-8: say so, don't 500
        return _SlashOutcome(handled=True, text=f"can't run `/{name}`: {exc}")
    if expanded is None:
        return _SlashOutcome(handled=True, text=f"unknown command: `/{name}` (try /help)")
    return _SlashOutcome(handled=False, expand_to=expanded)


# ---- app factory --------------------------------------------------------

# How often the lifespan scheduler re-reads [companion] (`evi companion on`, a
# hand edit of config.toml): the same minute `evi scheduler` reloads at.
_COMPANION_RESYNC_S = 60.0


def create_app() -> FastAPI:
    ensure_dirs()

    # Opt-in crash reporting (inert unless [telemetry] crash_reports + dsn).
    # Initialised before the FastAPI app so sentry-sdk's Starlette/FastAPI
    # integration auto-captures server errors. Covers the frozen desktop
    # sidecar too, since it runs this same app.
    from evi.reporting import init_reporting
    init_reporting()
    from evi import otel
    otel.init_telemetry()

    mcp_manager: MCPManager | None = None
    scheduler_obj: object | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal mcp_manager, scheduler_obj
        # The server hosts agents (`evi web`, the desktop sidecar, the dev
        # uvicorn): EVI_AGENT=1 for everything it starts (evi.proc).
        from evi.proc import mark_agent_process

        mark_agent_process()
        cfg = Config.load()
        # Safe mode: never spawn external MCP servers — they're user
        # customization (and arbitrary local processes), exactly what a clean
        # boot is meant to rule out. build_agent handles the rest.
        if cfg.tools.mcp and not safemode.enabled():
            servers = filter_allowed(load_servers(), cfg.tools.mcp_allow)
            if servers:
                try:
                    mcp_manager = MCPManager(
                        servers, max_output_chars=cfg.tools.mcp_max_output_chars
                    )
                    mcp_manager.start()
                except ImportError:
                    logger.warning(
                        "MCP enabled but `mcp` package not installed; "
                        "run `pip install 'evi-assistant[mcp]'`"
                    )
                    mcp_manager = None

        # Safe mode: no scheduled jobs either. They run the user's saved prompts
        # unattended, exactly what a clean boot rules out.
        if safemode.enabled():
            logger.info("scheduler not started: safe mode is on")
        else:
            try:
                from evi.scheduler import Scheduler

                scheduler_obj = Scheduler()
                scheduler_obj.start()
                # The Companion routes and POST /api/config reload its jobs;
                # a change made elsewhere (the CLI, an editor) is picked up by
                # the re-sync thread within _COMPANION_RESYNC_S.
                app.state.evi_scheduler = scheduler_obj
                companion_jobs_applied["sig"] = _companion_jobs_signature()
                companion_resync_stop.clear()
                threading.Thread(target=_companion_resync_loop, args=(_COMPANION_RESYNC_S,),
                                 name="evi-companion-resync", daemon=True).start()
            except RuntimeError as exc:
                logger.info("scheduler not started: %s", exc)
                scheduler_obj = None
            except Exception as exc:
                logger.warning("scheduler failed to start: %s", exc)
                scheduler_obj = None

        # Inbound chat channels (Telegram). Off unless enabled AND given a token,
        # so the default install starts no poller and opens no inbound path.
        try:
            from evi.channels import telegram as _tg

            _tg.start_if_configured(cfg)
        except Exception as exc:  # noqa: BLE001 — a channel must never block boot
            logger.warning("channel start failed: %s", exc)

        try:
            yield
        finally:
            try:
                from evi.channels import telegram as _tg

                _tg.stop_active()
            except Exception:  # noqa: BLE001
                pass
            if scheduler_obj is not None:
                companion_resync_stop.set()
                app.state.evi_scheduler = None
                scheduler_obj.stop()
            if mcp_manager is not None:
                mcp_manager.stop()

    app = FastAPI(title="eVi", lifespan=lifespan)

    # Per-user session registry: {user -> {session_id -> WebSession}}. With
    # multi_user off, everything lives in the "" bucket (single workspace).
    # `_sessions()` returns the CURRENT request's user bucket, so every lookup +
    # iteration is automatically scoped — no per-call-site prefixing to forget.
    _user_sessions: dict[str, dict[str, WebSession]] = {}

    def _my_sessions() -> dict[str, WebSession]:
        return _user_sessions.setdefault(_CURRENT_USER.get() or "", {})

    def _all_sessions():
        """Every live session across all users — for global config push only."""
        for bucket in _user_sessions.values():
            yield from bucket.values()

    def _user_root() -> Path | None:
        """The current user's data dir (HOME/users/<name>), else None → the
        shared defaults (single-user behaviour). An agent built with it hands
        it to its tools (ToolContext.user_root), so memory tools stay per-user."""
        u = _CURRENT_USER.get()
        return HOME / "users" / _safe_user(u) if u else None

    def _user_data_roots() -> tuple[Path | None, Path | None]:
        """(transcripts_root, memory_root) for the current user, else (None,
        None) → the shared defaults (single-user behaviour)."""
        base = _user_root()
        if base is None:
            return (None, None)
        return (base / "transcripts", base / "memory")

    # User/plugin slash commands are a customization too (mirrors the CLI REPL).
    if safemode.enabled():
        from evi.commands import safe_mode_store

        cmd_store = safe_mode_store()
    else:
        cmd_store = CommandStore()  # rescans the dir per call, safe to share

    # --- bearer-token auth (optional) -----------------------------------
    #
    # Auth fires only when `[web] auth_token` is set in config.toml. Empty
    # token = open access (backwards compatible). When configured, every
    # `/api/*` route demands either an `Authorization: Bearer <token>`
    # header OR a `?token=<token>` query parameter. The query path covers
    # `<img src>` and streaming endpoints whose XHR shape can't carry
    # custom headers.
    #
    # We allowlist a few endpoints so the login page can self-bootstrap:
    # - `/`, `/static/*`              — the HTML + JS for the login page
    # - `/api/health`                 — sanity probe for "is the server up"
    # - `/api/auth/check`             — used by the login overlay
    # - `/images/*`                   — capability-URL-style (filename is random hex)
    _PUBLIC_PATHS = frozenset(
        {"/", "/api/health", "/api/auth/check", "/api/backend/status",
         "/.well-known/agent-card.json"}
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Config is cheap to re-read (small TOML); doing it per-request
        # means `evi web token rotate` takes effect without a restart.
        from evi.users import load_users

        _CURRENT_USER.set(None)  # reset per request; set below when authenticated
        # Cross-site request guard — first, for every route (incl. /a2a and
        # federation), whatever the auth setup. A page on another site can make
        # the browser POST here without a preflight (multipart uploads are
        # "simple requests"); the Origin it can't forge gives it away. Peers and
        # CLIs send no Origin and pass. See _cross_site_refusal.
        refusal = _cross_site_refusal(request)
        if refusal is not None:
            return JSONResponse(
                {"detail": f"Refusing a cross-site request ({refusal}): eVi only accepts "
                           "changes from its own pages, so another website can't drive it "
                           "through your browser. Behind a reverse proxy that rewrites "
                           "Host, have it send the public host as X-Forwarded-Host and "
                           "set EVI_TRUSTED_PROXY=1 (or set EVI_ALLOW_CROSS_ORIGIN=1 if "
                           "this is intended)."},
                status_code=403,
            )
        # A `?session=<id>` link (handoff URL, evi://session deep link) seeds the
        # web UI's tab id, which the page then splices into its own same-origin
        # API paths and request bodies — so a crafted link from another site
        # would steer those requests past the guard above. Refuse a malformed
        # id before the page is served, whatever path serves it.
        for want in request.query_params.getlist("session"):
            if want and not valid_session_id(want):
                return JSONResponse({"detail": _BAD_SESSION_ID}, status_code=400)
        # Same contract for the id in a per-session route's path, so no handler
        # (or a future one) ever sees a path-like id.
        m = _SESSION_PATH.match(request.url.path)
        if m and not valid_session_id(m.group(1)):
            return JSONResponse({"detail": _BAD_SESSION_ID}, status_code=400)
        cfg = Config.load()
        tokens = _web_tokens(cfg)
        users = load_users() if cfg.web.multi_user else []
        if users:
            _note_login_collisions(users)  # logged once per collision, for the owner
        request.state.evi_owner = True  # narrowed below for a users.json login
        provided = _provided_token(request)
        local = _client_is_local(request)
        # Open access only when no auth is configured at all — the common desktop
        # case, where the server is loopback-bound and the user is the only one
        # on the machine. So only this machine may talk to it: the TCP peer must
        # be loopback, and not a proxy relaying someone else (_client_is_local).
        # A Host check alone isn't enough off-box — curl or a script on the LAN
        # sets `Host: localhost` freely (bind_lan, a manual --host 0.0.0.0, a
        # container).
        #
        # The same goes for this machine when the launch token is the only
        # credential (_lan_token_only): it was minted to gate the LAN, and on
        # loopback it would only lock out clients that predate it (an old VS
        # Code extension) without keeping anyone out.
        #
        # And "loopback" does NOT by itself keep a browser out: any web page
        # can send requests to 127.0.0.1, and DNS rebinding lets a hostile site
        # point its own hostname at loopback and drive this API — which, with the
        # filesystem and shell tools behind it, is the whole machine. Browsers
        # can't forge the Host header, so requiring a loopback Host closes that
        # vector at zero cost to real local use. A browser's state-changing
        # request must also carry X-Evi-Client (_client_header_refusal).
        #
        # Deliberately scoped to the unauthenticated case: if a token or users
        # are configured, that's the real control and these guards step aside
        # (federation peers, LAN and container hostnames all authenticate).
        # `EVI_ALLOW_ANY_HOST=1` overrides the Host check for exotic no-auth
        # deployments; nothing overrides the peer check.
        if (not tokens and not users) or (
            local and _lan_token_only(cfg, users) and not _token_matches(provided, tokens)
        ):
            if not local and not request.url.path.startswith(
                "/api/routine/"  # the path token is its credential
            ):
                return JSONResponse(
                    {"detail": "Refusing a request from another machine (or relayed by a "
                               "proxy) while no access token is set: without one, eVi only "
                               "answers this computer. Set one with `evi web-config token rotate` (or "
                               f"the {_LAUNCH_TOKEN_ENV} environment variable) to serve "
                               "the network — the desktop app does this itself when LAN "
                               "access is on."},
                    status_code=403,
                )
            if not _host_is_local(request):
                return JSONResponse(
                    {"detail": "Refusing a request for a non-local hostname while no "
                               "access token is set. Set one with `evi web-config token` "
                               "(or set EVI_ALLOW_ANY_HOST=1 if this is intended)."},
                    status_code=403,
                )
            header_refusal = _client_header_refusal(request)
            if header_refusal is not None:
                return JSONResponse({"detail": header_refusal}, status_code=403)
            return await call_next(request)
        path = request.url.path
        if (
            path in _PUBLIC_PATHS
            or path.startswith("/static/")
            or path.startswith("/images/")
            # Attachment previews: the unguessable session UUID in the path is
            # the capability (same rationale as /images), so <img> tags can load
            # without carrying the web auth token.
            or path.startswith("/uploads/")
            # Routine webhooks authenticate via the unguessable path token
            # (external callers don't have the web auth token). Validated in
            # the handler via secrets.compare_digest.
            or path.startswith("/api/routine/")
        ):
            return await call_next(request)

        # A token-authenticated request needs no X-Evi-Client: a page on
        # another site can't know the token.
        if _token_matches(provided, tokens):
            # The owner's token. In multi-user mode the owner gets its own
            # workspace bucket, as before.
            if users:
                request.state.evi_user = _OWNER_LOGIN
                _CURRENT_USER.set(_OWNER_LOGIN)
            return await call_next(request)
        # Multi-user: a users.json login is a user, never the owner, so
        # host-level routes are refused to it — and one whose name would map
        # onto the owner's workspace doesn't sign in at all (_reserved_login).
        user = _authenticate_login(provided, users) if users else None
        if user is not None:
            request.state.evi_user = user.name
            request.state.evi_owner = False
            _CURRENT_USER.set(user.name)
            if _owner_only_route(request.method, path):
                return JSONResponse(
                    {"detail": "Only the owner can do this: in multi-user mode, settings, "
                               "hooks, plugins, channels, peers, the runtime, MCP, backends, "
                               "file checkpoints, the Companion and host-level runs belong "
                               "to whoever "
                               "holds the [web] auth_token."},
                    status_code=403,
                )
            return await call_next(request)
        # A known login that may not sign in (its data dir collides with
        # another login's) is told why, rather than looking like a bad token.
        refused = _refused_login(provided, users) if users else None
        if refused is not None:
            return JSONResponse({"detail": refused}, status_code=403)
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict[str, object]:
        """The authenticated user's name (multi-user mode), else null."""
        return {"user": getattr(request.state, "evi_user", None)}

    @app.get("/api/auth/check")
    def auth_check(request: Request) -> dict[str, object]:
        """Validate the caller's token. Returns `{ok, required}`.

        The login overlay calls this with the user-supplied token in the
        `Authorization` header. If auth is disabled (`auth_token=""`),
        `required=false` and the overlay never shows.
        """
        from evi.users import load_users

        cfg = Config.load()
        tokens = _web_tokens(cfg)
        users = load_users() if cfg.web.multi_user else []
        if not tokens and not users:
            return {"ok": True, "required": False}
        # The launch token alone gates only other machines (see auth_middleware).
        if _lan_token_only(cfg, users) and _client_is_local(request):
            return {"ok": True, "required": False}
        provided = _provided_token(request)
        ok = _token_matches(provided, tokens)
        user = _authenticate_login(provided, users) if users and not ok else None
        if user is not None:
            ok = True
        result: dict[str, object] = {"ok": ok, "required": True}
        if user is not None:
            result["user"] = user.name
        elif not ok and users:
            refused = _refused_login(provided, users)
            if refused is not None:
                result["error"] = refused  # the login overlay shows it
        return result

    def get_session(session_id: str) -> WebSession:
        # The id names the transcript file (TranscriptStore joins it onto a day
        # dir) and the uploads dir, so a path-like one is a 400 here — the one
        # door every session-creating route goes through.
        _require_session_id(session_id)
        bucket = _my_sessions()
        sess = bucket.get(session_id)
        if sess is None:
            from evi.sdk.builder import build_agent
            from evi.transcripts import TranscriptStore

            config = Config.load()
            toggles = asdict(config.tools)
            # Per-user data dirs in multi-user mode (None → shared defaults).
            tr_root, mem_root = _user_data_roots()
            # Delegate the generic Agent assembly (client/tools/memory/skills/
            # guardrails) to the SDK; the web dispatcher injects only its per-user
            # concerns: the per-user data dir (memory index AND memory tools) +
            # transcript store and the CLIENT's session id (so the transcript
            # file matches the tab, and a chat survives a server/app restart).
            # Project context stays off here (the folder chip loads it). Hooks
            # come from the shared registry rather than a per-session load, so a
            # Settings → Hooks save reaches every open tab.
            # permission_callback is attached per-request.
            agent = _with_user_hooks(build_agent(
                config=config,
                memory_root=mem_root,
                user_root=_user_root(),
                transcripts=TranscriptStore(root=tr_root) if toggles.get("transcripts") else None,
                session_id=session_id,
                enable_project=False,
                enable_hooks=False,
                frontend="web",  # desktop app shares this surface — both have the Settings GUI
            ))
            # Revive history from disk if this session was seen before (e.g. the
            # desktop app was closed + reopened). The composed system prompt at
            # index 0 stays; everything after is rebuilt from the transcript.
            # Scoped to the user's own transcript dir so revival can't cross users.
            if tr_root is None:
                # A session handed off from another device (`evi sessions
                # handoff`, then `evi sync`) is taken in first. Single-user
                # only: the handoff folder isn't per user. A copy that can't
                # be taken in leaves this device's transcript to revive.
                try:
                    from evi.sessions import adopt_handoff

                    adopt_handoff(session_id)
                except Exception:  # noqa: BLE001
                    pass
            try:
                from evi.sessions import find_session, history_from_transcript

                path = find_session(session_id, root=tr_root)
                if path is not None:
                    # Meta records replayed (compactions, edits, cuts, the
                    # fork it started from), every day's file of it: the
                    # conversation as the user and the model last had it.
                    restored = history_from_transcript(path, replay=True)
                    if restored:
                        agent.history = [agent.history[0], *restored]
            except Exception:  # noqa: BLE001
                pass
            # No client attached yet: anything that would ask is denied until
            # a turn attaches its own callback (_deny_unattended).
            agent.permission_callback = _deny_unattended
            sess = WebSession(agent=agent)
            # Session messaging (Ph 95): give the agent tools to list + message
            # your OTHER live tabs. Injected directly (not via [tools] toggles),
            # and auto-approved — safe local ops that shouldn't prompt. Guarded
            # so lightweight test doubles (no real .tools dict) still work.
            if isinstance(getattr(agent, "tools", None), dict):
                for _t in _make_session_tools(bucket, session_id):
                    agent.tools[_t.name] = _t
                if isinstance(getattr(agent, "auto_approve_categories", None), set):
                    agent.auto_approve_categories.add("session")
            # Built without a lock: it is slow, and the async routes call this on
            # the event loop. So two first requests for a new id (the page's GET
            # /api/session/cwd and its first /api/chat) can both get here. The
            # first one stored wins and every caller gets that one; the other
            # build is dropped. A turn must never run on a session nobody else
            # can find (/steer would call it idle, /history would be empty).
            sess = bucket.setdefault(session_id, sess)
        _follow_runtime_window(sess)  # every turn starts here
        return sess

    @app.get("/")
    def index() -> FileResponse:
        # `no-cache` = the WebView/browser MUST revalidate index.html on every
        # load (ETag still yields a cheap 304 when unchanged). Without it, a UI
        # update shipped via the sidecar channel is masked for days by a stale
        # cached copy. This is what made a just-shipped feature (e.g. clipboard
        # paste) appear "not there" in an updated app. The app's JS
        # (/static/app.js) revalidates the same way (the /static mount below).
        # The CSP and no-framing headers come from _SecurityHeadersMiddleware.
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        from evi import __version__
        from evi.capabilities import capabilities as _caps
        cfg = Config.load()
        try:  # health must never 500 — capability heuristics are best-effort
            caps = _caps(cfg.llm.model or "")
        except Exception:  # noqa: BLE001
            caps = {}
        return {
            "ok": True,
            "version": __version__,
            "model": cfg.llm.model,
            # Capability flags so a federating peer's LAN scan / check_peer can
            # route work by capability without a second request (full card lives
            # at /.well-known/agent-card.json).
            "capabilities": caps,
            "sessions": sum(len(b) for b in _user_sessions.values()),
            # False when config.toml can't be read and eVi runs on the
            # defaults (Config.load); the detail is in GET /api/config.
            "config_ok": cfg.load_error is None,
        }

    @app.get("/.well-known/agent-card.json")
    def agent_card(request: Request) -> dict[str, object]:
        """A2A Agent Card (also carries eVi capability flags under `x-evi`).
        Auth-exempt so any agent can discover this node before delegating."""
        from evi.a2a import build_agent_card
        return build_agent_card(Config.load(), url=str(request.base_url).rstrip("/"))

    # --- LLM backend availability (so the UI can warn + offer to start one) -
    #
    # The UI calls this before sending a message, so it must stay snappy. We
    # probe the configured + known backends concurrently and cache the result
    # for a few seconds (the browser may re-check on every send/keystroke).
    _status_cache: dict[str, Any] = {"at": 0.0, "data": None}
    _status_lock = threading.Lock()

    @app.get("/api/backend/status")
    def backend_status() -> dict[str, object]:
        """Report whether an LLM backend is reachable, for the no-backend
        warning + 'start it for me' UX. Probes the configured backend plus
        the known local ones (LM Studio / Ollama / llama.cpp)."""
        import shutil
        import time
        from concurrent.futures import ThreadPoolExecutor

        now = time.monotonic()
        with _status_lock:
            cached = _status_cache["data"]
            if cached is not None and now - _status_cache["at"] < 3.0:
                return cached

        cfg = Config.load()
        # Probe the configured backend + all known backends + every CLI-agent
        # backend at once, so the total latency is the slowest single probe and
        # not their sum. Each entry is (kind, url); llama.cpp's probe scans
        # 8080..8090, and CLI-agent probes are a PATH lookup.
        #
        # The CLI agents matter for FIRST RUN: eVi ships six subscription-CLI
        # backends, so a machine with Claude Code already signed in should never
        # be asked to download a model. Previously they were probed only when one
        # was the *configured* backend, so first-run never noticed them.
        cli_kinds = list(_CLI_AGENT_BINS)
        probes: list[tuple[str, str]] = [(cfg.llm.backend, cfg.llm.base_url)]
        probes += list(_KNOWN_BACKENDS)
        probes += [(k, "") for k in cli_kinds]
        with ThreadPoolExecutor(max_workers=len(probes)) as ex:
            results = list(ex.map(lambda kv: _probe_candidate(*kv), probes))

        configured = {
            "backend": cfg.llm.backend,
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "reachable": results[0][0],
        }
        n_known = len(_KNOWN_BACKENDS)
        candidates = [
            {"kind": kind, "url": resolved, "reachable": ok}
            for (kind, _url), (ok, resolved) in zip(
                _KNOWN_BACKENDS, results[1 : 1 + n_known])
        ]
        cli_found = [
            kind for kind, (ok, _r) in zip(cli_kinds, results[1 + n_known :]) if ok
        ]
        any_reachable = configured["reachable"] or any(c["reachable"] for c in candidates)
        data: dict[str, object] = {
            "configured": configured,
            "candidates": candidates,
            "ollama_installed": shutil.which("ollama") is not None,
            "any_reachable": any_reachable,
            "cli_agents": cli_found,
        }
        # An API key already in the environment is another zero-setup path.
        import os as _os

        data["api_keys"] = [
            name
            for name, env in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"))
            if _os.environ.get(env, "").strip()
        ]
        # First-run wizard hints (the recommended model + whether we can install
        # Ollama unattended here). Hardware doesn't change within a run, so
        # compute these once and reuse — keeps the frequently-polled status fast.
        if "recommended_model" not in _status_cache:
            try:
                from evi import firstrun
                _status_cache["recommended_model"] = firstrun.recommended_model()
                _status_cache["can_auto_install"] = firstrun.ollama_install_plan().available
            except Exception:  # noqa: BLE001
                _status_cache["recommended_model"] = "qwen2.5:3b-instruct-q4_K_M"
                _status_cache["can_auto_install"] = False
        data["recommended_model"] = _status_cache["recommended_model"]
        data["can_auto_install_ollama"] = _status_cache["can_auto_install"]
        # Managed-runtime awareness, so the first-run banner can offer local AI in
        # EVERY not-ready state (not only when nothing at all is reachable).
        try:
            from evi.runtime import llamacpp_runtime as _rt

            data["runtime_supported"] = _rt.supported()
            data["runtime_installed"] = _rt.is_installed()
        except Exception:  # noqa: BLE001
            data["runtime_supported"] = False
            data["runtime_installed"] = False
        # Ranked first-run options, cheapest-to-first-reply first. The UI renders
        # these in order, so the ladder lives here rather than in the frontend:
        #   1. a subscription CLI already signed in   (no key, no download)
        #   2. a local server already running         (no download)
        #   3. an API key already in the environment
        #   4. download a small model                 (last resort, but works
        #                                              with no account at all)
        data["suggestions"] = _first_run_suggestions(data)
        with _status_lock:
            _status_cache["at"] = time.monotonic()
            _status_cache["data"] = data
        return data

    @app.post("/api/backend/start")
    def backend_start(req: BackendActionRequest) -> dict[str, object]:
        """Best-effort start of a local backend. Only Ollama is scriptable
        (`ollama serve`); LM Studio / llama.cpp are launched by the user."""
        import os
        import shutil
        import subprocess

        kind = req.kind.strip().lower()
        if kind == "ollama":
            if _probe_backend("http://localhost:11434/v1"):
                return {"started": False, "already_running": True,
                        "message": "Ollama is already running."}
            exe = shutil.which("ollama")
            if not exe:
                return {"started": False, "installed": False,
                        "message": "Ollama isn't installed yet."}
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            }
            if os.name == "nt":
                kwargs["creationflags"] = 0x0800_0000  # CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            try:
                subprocess.Popen([exe, "serve"], **kwargs)
            except Exception as exc:  # noqa: BLE001
                return {"started": False, "message": f"failed to start Ollama: {exc}"}
            return {"started": True,
                    "message": "Starting Ollama… give it a few seconds, then Recheck. "
                               "You may also need to pull the model."}
        return {"started": False,
                "message": f"Can't auto-start {kind}. For LM Studio: open it, load a "
                           "model, then Developer → Start Server (port 1234)."}

    _integ_cache: dict[str, Any] = {"at": 0.0, "data": None}
    _integ_lock = threading.Lock()

    @app.get("/api/integrations/status")
    def integrations_status() -> dict[str, Any]:
        """Live reachability for the Settings → Integrations banner: ComfyUI and
        (only when selected) SearXNG. Email OAuth has no backing token store, so
        it is intentionally NOT probed — filling the credential paths in doesn't
        sign you in, so there's nothing to report."""
        import time
        from concurrent.futures import ThreadPoolExecutor

        import httpx

        now = time.monotonic()
        with _integ_lock:
            c = _integ_cache["data"]
            if c is not None and now - _integ_cache["at"] < 3.0:
                return c
        cfg = Config.load()
        comfy_url = (cfg.comfy.base_url or "").rstrip("/")
        searxng_active = cfg.tools.search_backend == "searxng"
        searxng_url = (cfg.tools.searxng_url or "").rstrip("/")

        def _reach(url: str, path: str) -> bool:
            try:
                r = httpx.get(url + path, timeout=httpx.Timeout(1.5, connect=0.5))
                return r.status_code == 200
            except Exception:  # noqa: BLE001 — unreachable is the answer, not an error
                return False

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_comfy = ex.submit(_reach, comfy_url, "/system_stats") if comfy_url else None
            f_searx = (ex.submit(_reach, searxng_url, "/")
                       if (searxng_active and searxng_url) else None)
            comfy_reach = f_comfy.result() if f_comfy else False
            searx_reach = f_searx.result() if f_searx else False
        data: dict[str, Any] = {
            "comfy": {"configured_url": comfy_url, "reachable": comfy_reach},
            "searxng": {"active": searxng_active, "url": searxng_url, "reachable": searx_reach},
        }
        with _integ_lock:
            _integ_cache["at"] = time.monotonic()
            _integ_cache["data"] = data
        return data

    _spec_cache: dict[str, Any] = {"at": 0.0, "data": None}
    _spec_lock = threading.Lock()

    @app.get("/api/specialty/status")
    def specialty_status() -> dict[str, Any]:
        """Reachability of the configured specialty-model backends (OCR / vision /
        guard, all served over the OpenAI schema) + the STT dependency. Powers the
        Settings → Specialty models and Guardrails (guard-model) banners."""
        import importlib.util
        import time
        from concurrent.futures import ThreadPoolExecutor

        now = time.monotonic()
        with _spec_lock:
            c = _spec_cache["data"]
            if c is not None and now - _spec_cache["at"] < 3.0:
                return c
        cfg = Config.load()
        tasks = ("ocr", "vision", "guard")

        def _slice(task: str) -> dict[str, Any]:
            mid = (getattr(cfg.models, task, "") or "").strip()
            if not mid:
                return {"set": False}
            base = (getattr(cfg.models, task + "_base_url", "") or "").strip() or cfg.llm.base_url
            return {"set": True, "model": mid, "base_url": base,
                    "reachable": bool(_probe_backend(base))}

        with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            slices: dict[str, Any] = dict(zip(tasks, ex.map(_slice, tasks)))
        stt_id = (getattr(cfg.models, "stt", "") or "").strip()
        try:
            stt_dep = importlib.util.find_spec("faster_whisper") is not None
        except Exception:  # noqa: BLE001
            stt_dep = False
        slices["stt"] = {"set": bool(stt_id), "model": stt_id, "dep_ok": stt_dep}
        with _spec_lock:
            _spec_cache["at"] = time.monotonic()
            _spec_cache["data"] = slices
        return slices

    @app.get("/api/voice/status")
    def voice_status() -> dict[str, Any]:
        """Is the SELECTED TTS engine actually installed? (Settings → Voice.)
        Side-effect-free: find_spec / shutil.which only, never imports the engine."""
        import importlib.util
        import shutil

        from evi import voice as _voice

        def _have(name: str) -> bool:
            try:
                return importlib.util.find_spec(name) is not None
            except Exception:  # noqa: BLE001
                return False

        cfg = Config.load()
        engine = (cfg.voice.engine or "system").strip().lower()
        if engine == "system":
            be = _voice.detect_backend()
            ok = be != "none"
            return {"engine": engine, "available": ok, "detail": be,
                    "how_to_enable": "" if ok else "Install espeak-ng (Linux) or pick a neural engine."}
        # Each neural engine gates differently — TWO are CLI binaries, not modules.
        if engine == "coqui":
            ok, hint = _have("TTS"), "pip install TTS"
        elif engine == "kokoro":
            ok, hint = (_have("kokoro") or _have("kokoro_onnx")), "pip install kokoro-onnx"
        elif engine == "f5":
            ok, hint = (shutil.which("f5-tts_infer-cli") is not None), "pip install f5-tts"
        elif engine == "piper":
            ok, hint = (shutil.which("piper") is not None), "install the piper binary (or pip install piper-tts)"
        else:
            ok, hint = True, ""  # unknown engine — don't cry wolf
        return {"engine": engine, "available": ok, "detail": engine,
                "how_to_enable": "" if ok else hint}

    @app.post("/api/runtime/setup")
    def runtime_setup() -> dict[str, Any]:
        """Zero-config local AI: download a CPU llama.cpp runtime + a small
        starter model, start the server, and point [llm] at it — no Ollama, no
        manual pull. Runs in the background; poll GET /api/runtime/status."""
        from evi.runtime import setup as _rt_setup
        return _rt_setup.start(background=True)

    @app.get("/api/runtime/status")
    def runtime_status() -> dict[str, Any]:
        """Progress of the managed-runtime setup + whether the server is up."""
        from evi.runtime import setup as _rt_setup
        return _rt_setup.status()

    @app.post("/api/runtime/stop")
    def runtime_stop() -> dict[str, Any]:
        from evi.runtime import llama_server as _rt_srv
        _rt_srv.stop_managed()
        return {"ok": True}

    @app.get("/api/runtime/catalog")
    def runtime_catalog() -> dict[str, Any]:
        """Curated local models (each flagged `installed`), the hardware-fit
        recommendation, and which one is currently loaded (and its window)."""
        from evi.runtime import catalog as _cat
        from evi.runtime import setup as _rt_setup

        st = _rt_setup.status()
        return {
            "models": [{**m, "installed": _cat.is_installed(m)} for m in _cat.catalog()],
            "recommended_id": _cat.recommended_id(),
            "starter_id": _cat.starter().get("id"),
            "active_model_id": st.get("active_model_id"),
            "server_running": st.get("server_running"),
            "gpu_available": st.get("gpu_available"),
            "gpu_installed": st.get("gpu_installed"),
            "on_gpu": st.get("on_gpu"),
            "gpu_name": st.get("gpu_name"),
            "ctx": st.get("ctx"),  # the running server's window (-c), None if none
        }

    @app.post("/api/runtime/model/use")
    def runtime_model_use(req: dict[str, Any]) -> dict[str, Any]:
        """Download (if needed) + switch the managed server to a catalog model.
        Runs in the background; poll GET /api/runtime/status for progress."""
        from evi.runtime import setup as _rt_setup

        mid = str((req or {}).get("id", "")).strip()
        if not mid:
            raise HTTPException(400, "expected {id}")
        return _rt_setup.use_model(mid, background=True)

    @app.post("/api/runtime/model/remove")
    def runtime_model_remove(req: dict[str, Any]) -> dict[str, Any]:
        from evi.runtime import catalog as _cat
        from evi.runtime import setup as _rt_setup

        mid = str((req or {}).get("id", "")).strip()
        entry = _cat.get(mid)
        if entry is None:
            raise HTTPException(404, f"unknown model: {mid}")
        # The active model's file is mmap'd by the running server (locked on
        # Windows) — make the user switch away first.
        if _rt_setup.status().get("active_model_id") == mid:
            raise HTTPException(409, "That model is in use — switch to another first.")
        return {"ok": _cat.remove(entry)}

    @app.post("/api/runtime/gpu")
    def runtime_gpu() -> dict[str, Any]:
        """Download the CUDA build (if needed) + re-run the current model on the
        GPU (with -ngl). Background; poll GET /api/runtime/status."""
        from evi.runtime import setup as _rt_setup
        return _rt_setup.enable_gpu(background=True)

    @app.get("/api/channels/telegram")
    def channels_telegram_status() -> dict[str, Any]:
        """Channel state + who is paired or waiting. Never returns the token."""
        from evi.channels import telegram as _tg

        return _tg.status()

    @app.post("/api/channels/telegram/approve")
    def channels_telegram_approve(req: dict[str, Any]) -> dict[str, Any]:
        """Approve a pairing code. Reachable only from this machine's UI, which is
        the property that makes pairing meaningful — see evi/channels/pairing.py."""
        from evi.channels import pairing as _p

        code = str((req or {}).get("code", "")).strip()
        if not code:
            raise HTTPException(400, "expected {code}")
        entry = _p.approve("telegram", code)
        if entry is None:
            raise HTTPException(404, "Unknown or expired code — codes last one hour.")
        return {"ok": True, "paired": entry}

    @app.post("/api/channels/telegram/revoke")
    def channels_telegram_revoke(req: dict[str, Any]) -> dict[str, Any]:
        from evi.channels import pairing as _p

        uid = str((req or {}).get("user_id", "")).strip()
        if not uid:
            raise HTTPException(400, "expected {user_id}")
        return {"ok": _p.revoke("telegram", uid)}

    @app.post("/api/channels/telegram/restart")
    def channels_telegram_restart() -> dict[str, Any]:
        """Apply a token/enable change without restarting eVi."""
        from evi.channels import telegram as _tg

        _tg.stop_active()
        _tg.start_if_configured(Config.load())
        return _tg.status()

    # ---- the Companion (evi/companion/) -------------------------------------
    # Owner-only (_OWNER_ONLY_ROUTES); the state-changing ones go through the
    # same Origin / X-Evi-Client / token checks as every other POST and PUT.
    # Approving runs the stored call, exactly as stored, once (Companion.approve).

    # The check-in this server started (POST /api/companion/check): one at a
    # time, in a worker thread; its outcome stays here for the status route.
    companion_run: dict[str, Any] = {"running": False, "started_at": None, "last": None}
    companion_run_lock = threading.Lock()

    # The Companion jobs the scheduler was last synced with, as (id, cron)
    # pairs; the re-sync thread reloads when config.toml asks for others.
    companion_jobs_applied: dict[str, Any] = {"sig": None}
    companion_resync_stop = threading.Event()

    def _companion_jobs_signature() -> tuple[tuple[str, str], ...]:
        from evi.scheduler import companion_jobs

        return tuple(sorted((j.id, j.cron) for j in companion_jobs()))

    def _reload_companion_jobs() -> None:
        """Re-sync this server's scheduler (its Companion jobs come and go
        with ``[companion] enabled``; `evi.scheduler.companion_jobs`)."""
        sched = getattr(app.state, "evi_scheduler", None)
        if sched is None:
            return
        try:
            companion_jobs_applied["sig"] = _companion_jobs_signature()
            sched.reload()
        except Exception as exc:  # noqa: BLE001 — the setting is saved either way
            logger.warning("companion: the scheduler did not reload: %s", exc)

    def _resync_companion_jobs() -> bool:
        """Reload the scheduler when [companion] now asks for other jobs than
        it was last synced with: `evi companion on` / `off` or a hand edit of
        config.toml, which this server is not told about. True if it did."""
        if getattr(app.state, "evi_scheduler", None) is None:
            return False
        try:
            sig = _companion_jobs_signature()
        except Exception as exc:  # noqa: BLE001 — try again next time
            logger.warning("companion: could not read its jobs: %s", exc)
            return False
        if sig == companion_jobs_applied["sig"]:
            return False
        _reload_companion_jobs()
        return True

    def _companion_resync_loop(interval: float) -> None:
        while not companion_resync_stop.wait(interval):
            _resync_companion_jobs()

    def _companion_status() -> dict[str, Any]:
        from evi import companion as _companion
        from evi.companion.store import INSTRUCTIONS_FILE

        st = _companion.status()
        with companion_run_lock:
            run = dict(companion_run)
        st.update(
            safe_mode=safemode.enabled(),
            scheduler=getattr(app.state, "evi_scheduler", None) is not None,
            instructions_path=str(Path(st["root"]) / INSTRUCTIONS_FILE),
            check_running=run["running"], check_started_at=run["started_at"],
            last_run=run["last"], now=time.time(),
        )
        return st

    @app.get("/api/companion/status")
    def companion_status() -> dict[str, Any]:
        """Whether the Companion is on, its sources and actions, the last and
        next check-in, the last brief, pending proposals, today's
        notifications; and here: safe mode, whether this server's scheduler
        runs, the check-in it is running and the last one it ran."""
        return _companion_status()

    @app.get("/api/companion/feed")
    def companion_feed(limit: int = 50, since: str = "") -> dict[str, Any]:
        """The newest `limit` feed items (oldest first), only those after
        `since` (epoch seconds or an ISO time) when given."""
        from datetime import datetime as _dt

        from evi import companion as _companion
        from evi.companion.store import FEED_MAX

        if not 1 <= limit <= FEED_MAX:
            raise HTTPException(400, f"limit must be 1..{FEED_MAX}")
        cut: float | None = None
        if since.strip():
            try:
                cut = float(since)
            except ValueError:
                try:
                    cut = _dt.fromisoformat(since.strip()).astimezone().timestamp()
                except ValueError:
                    raise HTTPException(400, "since must be epoch seconds or an ISO time")
        return {"items": _companion.feed(limit, cut)}

    @app.get("/api/companion/proposals")
    def companion_proposals(state: str = "") -> dict[str, Any]:
        """Proposals (oldest first; the expired ones marked first), only those
        in `state` when given; `now` is the server's clock, for countdowns."""
        from evi import companion as _companion

        if state and state not in _companion.PROPOSAL_FILTERS:
            raise HTTPException(400, "state must be one of "
                                     f"{', '.join(_companion.PROPOSAL_FILTERS)}")
        return {"proposals": _companion.proposals(state or None), "now": time.time()}

    def _decide(proposal_id: str, body: dict[str, Any] | None, approve: bool) -> dict[str, Any]:
        from evi import companion as _companion

        if body is not None and not isinstance(body, dict):
            raise HTTPException(400, "expected an object body")
        want = (body or {}).get("args_hash")
        try:
            if want is not None:
                # The caller names the arguments it showed: anything else is
                # not what the owner approved.
                p = next((p for p in _companion.proposals() if p.get("id") == proposal_id),
                         None)
                if p is None:
                    raise KeyError(proposal_id)
                if want != p.get("args_hash"):
                    raise _companion.ProposalError(
                        f"proposal {proposal_id} is not the one shown (its arguments differ)")
            done = (_companion.approve if approve else _companion.reject)(proposal_id)
        except KeyError:
            raise HTTPException(404, f"no proposal {proposal_id}")
        except _companion.ProposalError as exc:
            raise HTTPException(409, str(exc))
        except _companion.CompanionBusy as exc:
            raise HTTPException(503, str(exc))
        return {"proposal": {"id": proposal_id, **done}}

    @app.post("/api/companion/proposals/{proposal_id}/approve")
    def companion_approve(proposal_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a pending proposal once, exactly as stored. 409 with the
        reason when it can't (not pending, expired, its arguments changed,
        no longer allowed, a hook vetoed it, the Companion is off, safe
        mode); `args_hash`, when sent, must be the one the caller showed."""
        return _decide(proposal_id, body, approve=True)

    @app.post("/api/companion/proposals/{proposal_id}/reject")
    def companion_reject(proposal_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reject a pending proposal (409 when it isn't pending)."""
        return _decide(proposal_id, body, approve=False)

    @app.post("/api/companion/check")
    def companion_check() -> JSONResponse:
        """Start a check-in now in a worker thread (202); 409 while one this
        server started is running, or with the Companion off. Its outcome
        shows as `last_run` in GET /api/companion/status."""
        from evi import companion as _companion
        from evi.companion import engine as _engine

        if not _engine.settings(Config.load()).enabled:
            raise HTTPException(409, "the Companion is off: switch it on first")
        if safemode.enabled():
            raise HTTPException(409, "safe mode is on: the Companion doesn't run")
        with companion_run_lock:
            if companion_run["running"]:
                raise HTTPException(409, "a check-in is already running")
            companion_run.update(running=True, started_at=time.time())

        def work() -> None:
            try:
                out = _companion.check_in().to_dict()
            except Exception as exc:  # noqa: BLE001 — reported, never raised
                out = {"ran": False, "skipped": None, "error": f"{type(exc).__name__}: {exc}"}
            out["finished_at"] = time.time()
            with companion_run_lock:
                companion_run.update(running=False, last=out)

        try:
            threading.Thread(target=work, name="companion-check-in", daemon=True).start()
        except Exception:
            with companion_run_lock:
                companion_run["running"] = False
            raise
        return JSONResponse({"started": True}, status_code=202)

    @app.get("/api/companion/instructions")
    def companion_instructions_get() -> dict[str, Any]:
        """instructions.md: the owner's steering (what to watch, what to
        ignore, how often to speak up)."""
        from evi import companion as _companion
        from evi.companion.store import INSTRUCTIONS_MAX

        return {"text": _companion.read_instructions(), "max_chars": INSTRUCTIONS_MAX}

    @app.put("/api/companion/instructions")
    def companion_instructions_put(body: dict[str, Any]) -> dict[str, Any]:
        """Replace instructions.md with {text} (audited)."""
        from evi import companion as _companion
        from evi.companion.store import INSTRUCTIONS_MAX

        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise HTTPException(400, "expected {text: <string>}")
        if len(text) > INSTRUCTIONS_MAX:
            raise HTTPException(413, f"instructions are limited to {INSTRUCTIONS_MAX} characters")
        _companion.write_instructions(text)
        return {"ok": True, "text": _companion.read_instructions()}

    @app.post("/api/companion/enabled")
    def companion_enabled(body: dict[str, Any]) -> dict[str, Any]:
        """Switch the Companion on or off ({on: bool}) in config.toml (the
        user layer), then re-sync this server's scheduler."""
        from evi import companion as _companion

        on = body.get("on") if isinstance(body, dict) else None
        if not isinstance(on, bool):
            raise HTTPException(400, "expected {on: true|false}")
        _companion.set_enabled(on)
        _reload_companion_jobs()
        return _companion_status()

    @app.get("/api/runtime/plan")
    def runtime_plan(model_id: str = "") -> dict[str, Any]:
        """Exactly what setting up local AI will put on this machine.

        Shown BEFORE anything downloads, so a multi-hundred-MB write is a choice
        rather than a surprise: every item names its real size and destination
        path, and `commands` spells out the literal URLs for anyone who wants to
        audit or do it by hand. Items already present are marked `needed: false`
        so a second run doesn't threaten to re-download what's on disk.
        """
        from evi.config import MODELS_DIR
        from evi.runtime import catalog as _cat
        from evi.runtime import llamacpp_runtime as _rt
        from evi.runtime import setup as _rt_setup

        entry = _cat.get(model_id) if model_id else None
        if entry is None:
            entry = _cat.starter()
        external = ""
        try:
            from pathlib import Path as _P

            raw = Config.load().runtime.server_path or ""
            # Apply the same existence gate every other consumer applies. A stale
            # path (binary moved or uninstalled) must NOT make this manifest
            # promise "no runtime download" when the run would download one —
            # that is exactly the surprise this endpoint exists to prevent.
            external = raw if raw and _P(raw).is_file() else ""
        except Exception:  # noqa: BLE001
            pass

        items: list[dict[str, Any]] = []
        cmds: list[str] = []
        if external:
            items.append({"kind": "runtime", "label": "Use your llama.cpp install",
                          "detail": external, "size": "", "needed": False})
        else:
            have = _rt.is_installed()
            asset = _rt.cpu_asset() or ""
            items.append({
                "kind": "runtime",
                "label": f"llama.cpp runtime {_rt.LLAMACPP_VERSION}",
                "detail": ("Already downloaded." if have else
                           "CPU build — works on any machine, no drivers."),
                "size": "" if have else _rt.asset_size_label(),
                "path": str(_rt.runtime_root()),
                "needed": not have,
            })
            if not have and asset:
                cmds.append(f"{_rt._RELEASE_BASE}/{asset}")

        have_model = _cat.is_installed(entry)
        items.append({
            "kind": "model",
            "label": f"{entry['name']} ({entry.get('quant', '')})".strip(),
            "detail": "Already downloaded." if have_model else entry.get("note", "") or "",
            "size": "" if have_model else f"{entry.get('size_gb', '?')} GB",
            "path": str(MODELS_DIR),
            "needed": not have_model,
        })
        if not have_model:
            cmds.append(_cat.download_url(entry))

        items.append({
            "kind": "server", "label": "A local server on 127.0.0.1",
            "detail": "Loopback only — not reachable from your network or the internet.",
            "size": "", "needed": True,
        })
        items.append({
            "kind": "admin", "label": "No administrator access needed",
            "detail": "Everything is written inside your user folder.",
            "size": "", "needed": False,
        })
        return {
            "model_id": entry["id"], "items": items, "commands": cmds,
            "external": external, "supported": _rt.supported(),
            "log_path": str(_rt_setup.server_log_path()),
        }

    @app.get("/api/runtime/log")
    def runtime_log(lines: int = 200) -> dict[str, Any]:
        """Tail of the managed server's log. The desktop shell's 'open logs'
        opens ~/.evi/logs, but the runtime writes under ~/.evi/runtime/logs — and
        the browser has no file manager at all, so serve it here instead."""
        from evi.runtime import setup as _rt_setup

        p = _rt_setup.server_log_path()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return {"path": str(p), "text": "", "exists": False}
        tail = text.splitlines()[-max(1, min(lines, 2000)):]
        return {"path": str(p), "text": "\n".join(tail), "exists": True}

    @app.get("/api/runtime/detect")
    def runtime_detect() -> dict[str, Any]:
        """Existing `llama-server` binaries already on this machine (PATH + common
        install dirs) for the 'locate existing install' option. Filesystem-only."""
        from evi.runtime import llamacpp_runtime as _rt

        return {"found": _rt.detect_external()}

    @app.post("/api/runtime/locate")
    def runtime_locate(req: dict[str, Any]) -> dict[str, Any]:
        """Use an EXISTING llama-server binary (skip the download): validate the
        path, save it, then start on a model. Background; poll GET
        /api/runtime/status. 400 if the path isn't a runnable llama-server."""
        from evi.runtime import setup as _rt_setup

        path = str((req or {}).get("path", "")).strip()
        model_id = str((req or {}).get("model_id", "")).strip() or None
        if not path:
            raise HTTPException(400, "expected {path}")
        st = _rt_setup.locate(path, model_id=model_id, background=True)
        # locate() sets this exact prefix ONLY when the binary fails validation (it
        # returns before kicking the install). Match the prefix precisely so a
        # background/runtime error is never mistaken for a validation failure.
        if str(st.get("error", "")).startswith("Not a runnable llama-server"):
            raise HTTPException(400, st["error"])
        return st

    @app.post("/api/backend/open-download")
    def backend_open_download(req: BackendActionRequest) -> dict[str, object]:
        """Open the backend's download page in the system browser."""
        import webbrowser

        urls = {
            "ollama": "https://ollama.com/download",
            "lmstudio": "https://lmstudio.ai/",
            "llamacpp": "https://github.com/ggml-org/llama.cpp/releases",
        }
        url = urls.get(req.kind.strip().lower(), "https://ollama.com/download")
        try:
            opened = webbrowser.open(url)
        except Exception:  # noqa: BLE001
            opened = False
        return {"opened": bool(opened), "url": url}

    @app.post("/api/backend/install")
    def backend_install(req: BackendActionRequest) -> dict[str, object]:
        """Unattended install of a local backend (Ollama only) via the OS
        package manager (winget/brew) or the official install script. Blocking
        — installs can take a minute or two; the UI shows a spinner. Falls back
        to a manual-download URL when no unattended path exists on this OS."""
        from evi import firstrun

        kind = req.kind.strip().lower()
        if kind != "ollama":
            return {"ok": False,
                    "message": f"Automatic install isn't supported for {kind}. "
                               "Install it yourself, then click Recheck."}
        return firstrun.install_ollama()

    @app.get("/api/backend/pull")
    async def backend_pull(model: str | None = None) -> EventSourceResponse:
        """Stream Ollama model-pull progress over SSE — the first-run wizard's
        'downloading model' step. Defaults to the hardware-recommended model.
        Idempotent: pulling an already-present model just re-verifies quickly.
        Without the access token the request must carry X-Evi-Client
        (_client_header_refusal), so the UI reads this stream with fetch: a GET
        that starts a download must not be something an <img> can fire."""
        from evi import firstrun
        from evi.backends.ollama import OllamaBackend

        model_id = (model or "").strip() or firstrun.recommended_model()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def enqueue(payload: dict | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        def worker() -> None:
            try:
                for p in OllamaBackend().pull_model(model_id):
                    pct = None
                    if p.total:
                        pct = round((p.downloaded or 0) * 100.0 / p.total, 1)
                    enqueue({"kind": "progress", "model": model_id,
                             "status": p.status, "downloaded": p.downloaded,
                             "total": p.total, "pct": pct})
                enqueue({"kind": "done", "model": model_id})
            except Exception as exc:  # noqa: BLE001
                enqueue({"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                enqueue(None)

        threading.Thread(target=worker, daemon=True).start()

        async def stream() -> AsyncIterator[dict[str, str]]:
            while True:
                payload = await queue.get()
                if payload is None:
                    return
                yield {"event": "message", "data": json.dumps(payload)}

        return EventSourceResponse(stream())

    @app.post("/api/backend/verify")
    def backend_verify() -> dict[str, object]:
        """Prove the configured backend actually ANSWERS — don't just check a port.

        `/api/backend/status` (and the old first-run flow) only established that
        something was listening. A reachable port is not a working backend: the
        model can be missing, the key wrong, the CLI's login expired, or the
        endpoint an unrelated server. This sends a real 1-token completion, then
        offers a trivial tool so the reply also says whether tool calling works —
        eVi's known trap is a model that reports no tool support, which silently
        disables image generation and every other tool.
        """
        return _verify_llm(Config.load())

    @app.post("/api/backend/use")
    def backend_use(req: BackendUseRequest) -> dict[str, object]:
        """Switch the active LLM backend (+ model) and persist it.

        This is what makes the first-run wizard actually *work*: after
        install→serve→pull we call this so eVi talks to the backend it just set
        up, instead of the shipped default. Also backs the banner's
        'Use <backend>' action. When no model is given we pick one that's
        actually installed on the backend (preferring the recommended one), so
        we never point the config at a model that isn't there. Rebuilds live
        sessions' clients so the current chat works without a restart."""
        from evi import firstrun
        from evi.backends.factory import default_base_url, get_backend

        kind = req.kind.strip().lower()
        if kind not in {"lmstudio", "ollama", "llamacpp", "openai_compat", "claude_agent", "codex", "gemini", "amp", "qwen", "copilot"}:
            raise HTTPException(400, f"unknown backend {kind!r}")
        base_url = (req.base_url or default_base_url(kind)).strip()

        cfg = Config.load()
        # Remember the outgoing backend so a verified switch can roll back. A
        # first-run switch that leaves config pointing at something that doesn't
        # answer is worse than not switching at all.
        prev = (cfg.llm.backend, cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model)
        cfg.llm.backend = kind
        cfg.llm.base_url = base_url
        cfg.llm.api_key = {"ollama": "ollama", "lmstudio": "lm-studio"}.get(
            kind, cfg.llm.api_key or "sk-noauth"
        )

        model = (req.model or "").strip()
        if not model:
            # Pick a model that exists on the backend, preferring the
            # hardware-recommended one; never leave config pointing at a
            # model the backend doesn't have.
            try:
                installed = [m.id for m in get_backend(cfg.llm).list_models()]
            except Exception:  # noqa: BLE001
                installed = []
            try:
                rec = firstrun.recommended_model()
            except Exception:  # noqa: BLE001
                rec = ""
            model = rec if rec in installed else (installed[0] if installed else rec)
        if model:
            cfg.llm.model = model
        cfg.save()

        # Verified switch: prove it answers, and undo the switch if it doesn't.
        # Used by first-run so "connected" never means "a port was open".
        verdict: dict[str, object] | None = None
        if req.verify:
            verdict = _verify_llm(cfg)
            if not verdict.get("chat"):
                cfg.llm.backend, cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model = prev
                cfg.save()
                return {"ok": False, "verified": False, "verify": verdict,
                        "backend": cfg.llm.backend, "base_url": cfg.llm.base_url,
                        "model": cfg.llm.model}

        # The switch is final (a failed verify above keeps the managed runtime
        # and its window): leaving that runtime gives context_size back.
        from evi.runtime import setup as _rt_setup

        context_size = _rt_setup.release_context_size(cfg)
        # Apply to every live session (rebuild the client for the new backend).
        for sess in _all_sessions():
            sess.agent.config.llm.backend = cfg.llm.backend
            sess.agent.config.llm.base_url = cfg.llm.base_url
            sess.agent.config.llm.api_key = cfg.llm.api_key
            sess.agent.config.llm.model = cfg.llm.model
            if context_size is not None:
                sess.agent.config.llm.context_size = context_size
            try:
                sess.agent.client = make_client(cfg.llm)
            except Exception:  # noqa: BLE001
                pass
            sess.agent.refresh_prompt()  # re-stitch identity for the new model
        return {"ok": True, "verified": bool(verdict), "verify": verdict,
                "backend": cfg.llm.backend,
                "base_url": cfg.llm.base_url, "model": cfg.llm.model}

    @app.get("/api/session/{session_id}/usage")
    def session_usage(session_id: str) -> dict[str, int]:
        """Return approximate token usage for the named session."""
        sess = _my_sessions().get(session_id)
        if sess is None:
            return {"used": 0, "ceiling": 0}
        _follow_runtime_window(sess)
        used, ceiling = sess.agent.token_usage()
        return {"used": used, "ceiling": ceiling}

    @app.get("/api/session/{session_id}/context")
    def session_context(session_id: str) -> dict[str, object]:
        """Per-category breakdown of where the context window is spent (Ph 88)."""
        from evi.context_report import context_breakdown

        sess = _my_sessions().get(session_id)
        if sess is None:
            return context_breakdown([], 0)
        _follow_runtime_window(sess)
        return context_breakdown(sess.agent.history, sess.agent.context_ceiling())

    @app.post("/api/session/{session_id}/channel")
    def push_channel(session_id: str, req: dict[str, Any]) -> dict[str, Any]:
        """Push an external alert/notification into a (live or revived) session
        (Phase 83). By default the text is added as a note (a user message
        headed as information, not instructions; held until a running turn
        ends; logged to the transcript, so a revived session keeps it) so the
        assistant sees it on its NEXT turn. With ``run: true`` it
        pushes into the LIVE session — the agent acts on it immediately
        (headless turn) and the reply lands in the session history. An external
        sender (webhook, script) authenticates with the normal web token.
        """
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        text = str(req.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        source = (str(req.get("source") or "channel").strip() or "channel")[:64]
        run_now = bool(req.get("run"))
        sess = get_session(session_id)

        if run_now:
            # A delegate CLI agent's own tools would run with nobody to approve
            # them: refused (409) unless [llm] allow_cli_agent_unattended.
            settings = getattr(sess.agent, "config", None)
            if settings is not None:
                from evi.llm.cli_agent import CliAgentUnattended, check_unattended

                try:
                    check_unattended(settings, "channel")
                except CliAgentUnattended as exc:
                    raise HTTPException(409, str(exc)) from None
            if not sess.begin_turn():
                # Can't drive a turn while one is mid-flight — fall back to a note.
                _post_note(sess, note_message(text, source, "channel"))
                sess.channel_log.append({"source": source, "text": text, "ran": False})
                return {"ok": True, "source": source, "ran": False, "reason": "session busy"}
            from evi.headless import run_headless

            # Nobody is watching this turn, so nothing can be asked: a tool that
            # needs approval is denied (not left waiting on a callback a past
            # /api/chat attached, whose stream is long gone). The text comes from
            # outside, so what the desk user turned on for this session (/auto
            # on, [auto] mode yolo / accept_edits, its own auto-approvals) does
            # not apply either; all of it is put back afterwards. The deny
            # callback stays attached throughout.
            sess.agent.permission_callback = _deny_unattended
            try:
                remote = remote_unattended(sess.agent, _deny_unattended)
                with _unattended_run("channel", settings), remote:
                    res = run_headless(sess.agent, f"[channel:{source}] {text}")
                reply = (res.text or res.error or "").strip()
            finally:
                sess.end_turn()
            sess.channel_log.append({"source": source, "text": text, "ran": True, "reply": reply[:1000]})
            return {"ok": True, "source": source, "ran": True, "reply": reply}

        _post_note(sess, note_message(text, source, "channel"))
        sess.channel_log.append({"source": source, "text": text, "ran": False})
        return {"ok": True, "source": source, "pending": len(sess.channel_log)}

    @app.get("/api/session/{session_id}/channel")
    def list_channel(session_id: str) -> dict[str, Any]:
        """Recent channel messages pushed into this session (for a UI badge)."""
        sess = _my_sessions().get(session_id)
        return {"messages": sess.channel_log if sess is not None else []}

    @app.post("/api/session/{session_id}/message")
    def session_message(session_id: str, req: dict[str, Any]) -> dict[str, Any]:
        """Deliver a plain-text message into one of the user's OWN live sessions
        (Ph 95) — the Dispatch panel's "Message" compose. Same delivery as the
        send_to_session tool. 404 if the target session isn't live."""
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        text = str(req.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        source = (str(req.get("from") or "you").strip() or "you")[:64]
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such live session")
        delivered = _deliver_note(sess, source, text)
        return {"ok": True, "delivered": delivered, "unread": len(sess.channel_log)}

    @app.post("/api/session/{session_id}/name")
    def session_set_name(session_id: str, req: dict[str, Any]) -> dict[str, Any]:
        """Store a human name for a session so other sessions can address it by
        name (Ph 95). The web UI posts the tab's label here."""
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()[:80]
        sess = get_session(session_id)
        sess.title = name
        return {"ok": True, "name": sess.title}

    @app.post("/api/session/{session_id}/handoff")
    def handoff_session(session_id: str, request: Request) -> dict[str, Any]:
        """Hand a session off to another device (Phase 87).

        Transcripts are written per-turn, so the on-disk copy is current as of
        the last completed turn; this returns the resume affordances (a
        `/?session=<id>` URL the web UI opens, and the `evi sessions resume`
        command), reading the signed-in user's own transcripts in multi-user
        mode. In single-user mode it also stages a copy where `evi sync`
        carries it (sessions.stage_handoff); sync carries only that shared
        folder, so a multi-user login's session is not copied there. 404 if
        the session has no transcript yet (send a turn first, with
        transcripts enabled).
        """
        from evi import sessions as _sessions

        sid = _require_session_id(session_id)
        transcripts_root, _ = _user_data_roots()
        # handoff_info looks the id up as `<day dir>/<id>.jsonl`.
        info = _sessions.handoff_info(
            sid,
            base_url=str(request.base_url),
            root=transcripts_root,
        )
        if info is None:
            raise HTTPException(
                404,
                "session not persisted yet — send a message first "
                "(tools.transcripts must be on)",
            )

        staged = None
        if transcripts_root is None:
            try:
                staged = _sessions.stage_handoff(sid)
            except (OSError, ValueError) as exc:
                # The reason (it may name a path on this machine) goes to the log only.
                logger.warning("handoff: could not stage session %s: %s", sid, exc)
                raise HTTPException(
                    500, "could not copy the session for sync (the reason is in the server log)"
                ) from exc

        return {"ok": True, **info, "staged": str(staged) if staged else None}

    def _dispatch_snapshot_data() -> dict[str, Any]:
        """Build the dispatch dashboard snapshot. Shared by the one-shot GET and
        the live SSE stream so both report identical shape."""
        from evi import workflows as _wf

        sess_list = []
        for sid, s in _my_sessions().items():
            _follow_runtime_window(s)
            try:
                used, ceiling = s.agent.token_usage()
            except Exception:
                used, ceiling = 0, 0
            sess_list.append(
                {
                    "id": sid,
                    "name": s.title,  # server-stored session name (Ph 95 messaging)
                    "mode": s.mode,
                    "messages": len(getattr(s.agent, "history", [])),
                    "used": used,
                    "ceiling": ceiling,
                    "pending": len(s.pending),
                    "channels": len(s.channel_log),
                    # "busy" while a turn is mid-flight (live agent indicator)
                    "busy": bool(getattr(s, "busy", False)),
                }
            )
        wfs = [
            {
                "name": w.name,
                "description": w.description,
                "steps": len(w.steps),
                "parallel": sum(1 for st in w.steps if st.parallel),
            }
            for w in _wf.list_workflows()
        ]
        return {
            "sessions": sess_list,
            "workflows": wfs,
            # Inbound federation (peer→this node) so the receiver UI can show a
            # "processing a peer request" indicator.
            "federation": {
                "active": _FED_ACTIVE["n"],
                "recent": list(_FED_ACTIVITY)[:8],
            },
        }

    @app.get("/api/dispatch")
    def dispatch_snapshot() -> dict[str, Any]:
        """Dashboard data (Phase 85): every live session + its state, plus the
        workflows you can launch. Powers the dispatch view for managing many
        concurrent sessions at once."""
        return _dispatch_snapshot_data()

    @app.get("/api/dispatch/stream")
    async def dispatch_stream(
        interval: float = 1.5, limit: int = 0
    ) -> EventSourceResponse:
        """Live agent-watch: stream the dispatch snapshot every `interval`
        seconds so the UI shows in-flight sessions/agents update in real time
        (eVi's analogue of the Claude Code Agent view). `limit` > 0 ends the
        stream after that many snapshots (used by tests / one-off polls)."""
        interval = min(max(interval, 0.25), 10.0)  # clamp to a sane range

        async def stream() -> AsyncIterator[dict[str, str]]:
            sent = 0
            while True:
                yield {"event": "message", "data": json.dumps(_dispatch_snapshot_data())}
                sent += 1
                if limit and sent >= limit:
                    return
                await asyncio.sleep(interval)

        return EventSourceResponse(stream())

    @app.post("/api/dispatch/workflow/{name}")
    def dispatch_run_workflow(name: str, req: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a workflow headless, server-side (each step its own agent;
        parallel blocks concurrent). Nobody can approve a step's tool call, so
        each runs unattended: auto-approved categories run, anything else
        follows [auto] unattended_policy (deny by default). Returns
        {ok, outputs: {step_id: output}, policy}."""
        from evi import workflows as _wf
        from evi.headless import run_headless
        from evi.modes import mode_tools

        try:
            wf = _wf.load_workflow(name)
        except _wf.WorkflowError as exc:
            raise HTTPException(404, str(exc))

        variables: dict[str, str] = {}
        if isinstance(req, dict) and isinstance(req.get("vars"), dict):
            variables = {str(k): str(v) for k, v in req["vars"].items()}

        def run_step(prompt: str, step) -> str:
            from evi.sdk.builder import build_agent

            # Single-user headless runner: project/guardrails stay off to match
            # the previous inline construction; the user's hooks apply (shared).
            agent = _with_user_hooks(build_agent(
                config=Config.load(),
                enable_project=False,
                enable_hooks=False,
                enable_guardrails=False,
            ))
            if step.mode:
                agent.tools = {t.name: t for t in mode_tools(step.mode)}
            make_unattended(agent)  # nobody to ask: [auto] unattended_policy
            res = run_headless(agent, prompt)
            return _wf.step_output(res)  # an Error mid-reply is a failed step

        cfg = Config.load()
        try:
            with _unattended_run("workflow", cfg):
                outputs = _wf.run_workflow(wf, run_step=run_step, variables=variables)
        except _wf.WorkflowError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "outputs": outputs, "policy": _unattended_policy(cfg)}

    @app.post("/api/federate")
    def federate(req: dict[str, Any], request: Request) -> dict[str, Any]:
        """Run a task delegated by a trusted peer eVi (federation). Off unless
        `[federation] serve = true`. Runs non-interactively — tools not already
        auto-approved are denied, so a remote task can't trigger surprises."""
        cfg = Config.load()
        if not cfg.federation.serve:
            raise HTTPException(
                403, "federation serving is disabled (set [federation] serve = true)"
            )
        _federation_lan_guard(request, cfg)
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        task = str(req.get("task") or "").strip()
        if not task:
            raise HTTPException(400, "task is required")

        from evi.headless import run_headless
        from evi.sdk.builder import build_agent

        # Single-user headless runner; project/guardrails stay off to match the
        # previous inline construction; the user's hooks apply (shared registry).
        agent = _with_user_hooks(build_agent(
            config=cfg,
            enable_project=False,
            enable_hooks=False,
            enable_guardrails=False,
        ))
        mode = str(req.get("mode") or "")
        if mode:
            from evi.modes import mode_tools

            agent.tools = {t.name: t for t in mode_tools(mode)}
        # Record inbound peer activity so the receiver UI can show an indicator.
        entry = _fed_begin(str(req.get("source") or "peer"), task)
        try:
            # Non-interactive, and a remote peer's task: deny tools not already
            # in the auto-approve list, whatever [auto] unattended_policy or
            # [auto] mode (yolo, accept_edits) says.
            with _unattended_run("federation", cfg), remote_unattended(agent):
                res = run_headless(agent, task)
        except Exception:
            _fed_end(entry, "error")
            raise
        _fed_end(entry, "error" if res.error else "ok")
        return {"text": res.text, "error": res.error}

    @app.post("/a2a")
    def a2a_rpc(body: dict, request: Request) -> dict:
        """A2A (Agent2Agent) JSON-RPC endpoint — message/send, tasks/get,
        tasks/cancel. Off unless `[federation] a2a = true`. Delegated tasks run
        non-interactively (tools not auto-approved are denied), same as
        /api/federate. Bearer-token-gated by the auth middleware."""
        cfg = Config.load()
        if not cfg.federation.a2a:
            raise HTTPException(403, "A2A serving is disabled (set [federation] a2a = true)")
        _federation_lan_guard(request, cfg)
        from evi import a2a as a2a_mod
        from evi.headless import run_headless
        from evi.llm.cli_agent import unattended
        from evi.sdk.builder import build_agent

        def _run(text: str) -> tuple[str, str]:
            agent = _with_user_hooks(build_agent(
                config=cfg,
                enable_project=False,
                enable_hooks=False,
                enable_guardrails=False,
            ))
            entry = _fed_begin("a2a", text)
            try:
                # A remote agent's task: deny anything not auto-approved, whatever
                # [auto] unattended_policy or mode says. A refused CLI-agent
                # backend becomes a failed A2A task.
                with unattended("A2A", cfg), remote_unattended(agent):
                    res = run_headless(agent, text)
            except Exception:
                _fed_end(entry, "error")
                raise
            _fed_end(entry, "error" if res.error else "ok")
            return res.text, res.error

        return a2a_mod.handle_rpc(body if isinstance(body, dict) else {}, _run)

    @app.get("/api/model-picker")
    def model_picker_get() -> dict[str, object]:
        """Snapshot for the picker UI: models across ALL configured backends,
        each tagged with its source backend, + current settings."""
        cfg = Config.load()
        from evi.backends import registry as _reg
        from evi.capabilities import capabilities as _caps

        groups = _reg.all_models(cfg)  # [{backend, kind, reachable, models:[ids]}]
        active = cfg.llm.model
        active_backend = _reg.active_backend_name(cfg)
        # Flat, de-duped model list + a source map (model id -> [backend names]).
        flat: list[str] = []
        sources: dict[str, list[str]] = {}
        for g in groups:
            for mid in g["models"]:
                if mid not in sources:
                    sources[mid] = []
                    flat.append(mid)
                if g["backend"] not in sources[mid]:
                    sources[mid].append(g["backend"])
        # Always include the active model so the picker is never empty.
        if active and active not in sources:
            flat.insert(0, active)
            sources[active] = [active_backend] if active_backend else []

        return {
            "active": active,
            "active_backend": active_backend,
            "fast_model": cfg.llm.fast_model,
            "models": flat,
            # Per-backend grouping (name/kind/reachable/models) for source tags
            # + greying-out unreachable backends in the UI.
            "backends": groups,
            # model id -> [backend names] that serve it (the source tag/hover).
            "sources": sources,
            "effort": (cfg.llm.reasoning_effort or "medium").lower(),
            "effort_levels": ["off", "low", "medium", "high", "max"],
            "fast_mode": bool(cfg.llm.fast_mode),
            "backend": cfg.llm.backend,
            "api": cfg.llm.api,  # "chat" | "responses" (cloud server-side tools)
            # Per-model capability flags (vision/reasoning/infill/audio) + the
            # active model's, so the UI can show capability chips.
            "capabilities": {m: _caps(m) for m in flat},
            "active_capabilities": _caps(active),
        }

    @app.post("/api/model-picker")
    def model_picker_set(req: PickerUpdate) -> dict[str, object]:
        """Apply any subset of {model, fast_model, effort, fast_mode}.

        Persists to config.toml AND nudges every in-memory session's agent
        so the change takes effect on the next turn without a restart.
        """
        cfg = Config.load()
        from evi.backends import registry as _reg
        if req.backend is not None:
            # Resolve against effective_backends — which includes the synthetic
            # entry for the legacy single [llm] backend when no backends.json
            # exists — not just the file. Otherwise switching a model on the
            # DEFAULT backend (the picker passes its implicit name) 400s with
            # "unknown backend", breaking model switching for no-registry users.
            entry = _reg.get_entry(req.backend.strip(), _reg.effective_backends(cfg))
            if entry is None:
                raise HTTPException(400, f"unknown backend {req.backend!r}")
            # Materialize the chosen registry entry into [llm] (the active backend).
            cfg.llm.backend = entry.kind
            cfg.llm.base_url = entry.base_url
            cfg.llm.api_key = entry.api_key
        if req.model is not None:
            cfg.llm.model = req.model.strip()
        if req.fast_model is not None:
            cfg.llm.fast_model = req.fast_model.strip()
        if req.effort is not None:
            level = req.effort.strip().lower()
            if level not in ("off", "low", "medium", "high", "max"):
                raise HTTPException(400, f"invalid effort {level!r}")
            cfg.llm.reasoning_effort = level
        if req.fast_mode is not None:
            cfg.llm.fast_mode = bool(req.fast_mode)
        cfg.save()
        context_size = None
        if req.backend is not None:  # leaving the managed runtime gives it back
            from evi.runtime import setup as _rt_setup

            context_size = _rt_setup.release_context_size(cfg)
        # Push into every live session so the current chat reflects the new
        # settings on the next turn.
        for sess in _all_sessions():
            sess.agent.config.llm.backend = cfg.llm.backend
            sess.agent.config.llm.base_url = cfg.llm.base_url
            sess.agent.config.llm.api_key = cfg.llm.api_key
            sess.agent.config.llm.model = cfg.llm.model
            if context_size is not None:
                sess.agent.config.llm.context_size = context_size
            sess.agent.config.llm.fast_model = cfg.llm.fast_model
            sess.agent.config.llm.reasoning_effort = cfg.llm.reasoning_effort
            sess.agent.config.llm.fast_mode = cfg.llm.fast_mode
            if req.backend is not None:  # backend changed → rebuild the client
                try:
                    sess.agent.client = make_client(cfg.llm)
                except Exception:  # noqa: BLE001
                    pass
            sess.agent.refresh_prompt()  # re-stitch identity for the new model
        return {
            "ok": True,
            "active": cfg.llm.model,
            "active_backend": _reg.active_backend_name(cfg),
            "backend": cfg.llm.backend,
            "fast_model": cfg.llm.fast_model,
            "effort": cfg.llm.reasoning_effort,
            "fast_mode": cfg.llm.fast_mode,
        }

    @app.get("/api/backends")
    def backends_list() -> dict[str, object]:
        """The configured backend registry + available presets. Never leaks the
        raw API key — only whether one is set and if it's an env reference."""
        from evi.backends import registry as _reg
        from evi.backends.presets import ONLINE_PRESETS

        cfg = Config.load()
        entries = _reg.load_backends()
        return {
            "backends": [
                {
                    "name": e.name, "kind": e.kind, "base_url": e.base_url,
                    "enabled": e.enabled, "fanout": e.fanout,
                    "has_key": bool(e.api_key),
                    "key_is_env": e.api_key.startswith("env:"),
                }
                for e in entries
            ],
            "active_backend": _reg.active_backend_name(cfg),
            "presets": [
                {"name": p.name, "base_url": p.base_url, "api_key_env": p.api_key_env,
                 "default_model": p.default_model, "note": p.note}
                for p in ONLINE_PRESETS.values()
            ],
            "kinds": ["lmstudio", "ollama", "llamacpp", "openai_compat", "claude_agent", "codex", "gemini", "amp", "qwen", "copilot"],
        }

    @app.post("/api/backends")
    def backends_add(req: BackendUpsert) -> dict[str, object]:
        """Register (or overwrite) a backend — from a preset or a custom entry."""
        from evi.backends import registry as _reg

        if req.preset:
            entry = _reg.from_preset(
                req.preset.strip(), name=(req.name or "").strip(),
                api_key=(req.api_key or "").strip(),
            )
            if entry is None:
                raise HTTPException(400, f"unknown preset {req.preset!r}")
        else:
            name = (req.name or "").strip()
            if not name:
                raise HTTPException(400, "name is required for a custom backend")
            kind = (req.kind or "openai_compat").strip()
            base = (req.base_url or "").strip() or _reg._default_base_url(kind)
            entry = _reg.BackendEntry(
                name=name, kind=kind, base_url=base, api_key=(req.api_key or "").strip(),
            )
        if req.enabled is not None:
            entry.enabled = bool(req.enabled)
        if req.fanout is not None:
            entry.fanout = bool(req.fanout)
        _reg.add_backend(entry, overwrite=True)
        return {"ok": True, "name": entry.name, "kind": entry.kind}

    @app.patch("/api/backends/{name}")
    def backends_patch(name: str, req: BackendFlags) -> dict[str, object]:
        """Toggle enabled / fanout on an existing backend without touching its
        url or key (used by the settings-panel checkboxes)."""
        from evi.backends import registry as _reg

        entry = _reg.get_entry(name)
        if entry is None:
            raise HTTPException(404, f"no such backend {name!r}")
        if req.enabled is not None:
            entry.enabled = bool(req.enabled)
        if req.fanout is not None:
            entry.fanout = bool(req.fanout)
        _reg.add_backend(entry, overwrite=True)
        return {"ok": True, "name": entry.name, "enabled": entry.enabled, "fanout": entry.fanout}

    @app.delete("/api/backends/{name}")
    def backends_remove(name: str) -> dict[str, object]:
        from evi.backends import registry as _reg

        return {"ok": _reg.remove_backend(name)}

    @app.get("/api/session/cwd")
    def session_cwd_get(session_id: str) -> dict[str, str]:
        """The session's working folder (empty cwd → the server process cwd)."""
        import os as _os

        sess = get_session(session_id)
        return {"working_dir": (getattr(sess.agent, "cwd", "") or _os.getcwd())}

    @app.post("/api/session/cwd")
    def session_cwd_set(req: CwdRequest) -> dict[str, str]:
        """Set the session's working folder. Relative file ops then resolve
        there and EVI.md re-discovers from it (mirrors the CLI `/cd`)."""
        from pathlib import Path as _Path

        sess = get_session(req.session_id)
        p = _Path(req.path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {p}")
        sess.agent.cwd = str(p.resolve())
        try:
            from evi.project import load_project_context

            sess.agent.project = load_project_context(start=p.resolve())
            if sess.agent.history:
                sess.agent.history[0] = {
                    "role": "system", "content": sess.agent._compose_system_prompt()
                }
        except Exception:  # noqa: BLE001
            pass
        return {"working_dir": sess.agent.cwd}

    @app.get("/api/project/trust")
    def project_trust_get(session_id: str) -> dict[str, Any]:
        """Folder trust (evi/trust.py) for the session's working folder: its
        state ("trusted" / "changed" / "untrusted"), what its .evi.toml sets
        and what of that would apply. Read-only — the server never prompts;
        an untrusted folder's project config simply stays off."""
        import os as _os

        from evi import trust as _trust

        sess = get_session(session_id)
        folder = Path(getattr(sess.agent, "cwd", "") or _os.getcwd())
        return _trust.status_dict(_trust.status(folder))

    @app.post("/api/project/trust")
    def project_trust_set(req: ProjectTrustRequest, request: Request) -> dict[str, Any]:
        """Trust (or, with trust=false, stop trusting) the session's working
        folder — the user's answer to the trust prompt. Approves the
        .evi.toml as it is now; pass the `config_sha256` the GET returned to
        refuse (409) a file that changed after it was reviewed. In multi-user
        mode only the owner may change the trust list: it applies to the
        owner's config."""
        import os as _os

        from evi import trust as _trust

        # Multi-user: only the owner's own token — not whoever holds an account
        # that happens to be *named* "owner" (the middleware refuses it too).
        _require_owner(request)
        sess = get_session(req.session_id)
        folder = Path(getattr(sess.agent, "cwd", "") or _os.getcwd())
        if req.trust:
            st = _trust.status(folder)
            current = st.config.sha256 if st.config is not None else ""
            if req.config_sha256 and req.config_sha256 != current:
                raise HTTPException(409, "the project config changed; review it again")
            st = _trust.grant(folder, config=st.config)
        else:
            _trust.revoke(folder)
            st = _trust.status(folder)
        return _trust.status_dict(st)

    @app.post("/api/complete")
    def complete_post(req: CompleteRequest) -> dict[str, str]:
        """Local FIM code completion — eVi as a Tab/Copilot backend for an
        editor extension. Returns the insertion between prefix and suffix."""
        from evi import complete as _complete

        try:
            text = _complete.complete(
                req.prefix, req.suffix, model=req.model, max_tokens=req.max_tokens
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"completion failed: {exc}")
        return {"completion": text}

    @app.get("/api/config")
    def config_get() -> dict[str, Any]:
        """Full config snapshot for the settings screen (secrets masked)."""
        cfg = Config.load()
        snap = _config_snapshot(cfg)
        # Why config.toml couldn't be read (the values above are then the
        # defaults), for the settings-file banner; None when it was fine.
        import evi.config as _config_mod

        snap["load_error"] = (f"{_config_mod.CONFIG_PATH}: {cfg.load_error}"
                              if cfg.load_error else None)
        # Per-tool-category install availability (for the "needs setup" badge in
        # Settings → Tools). Only categories whose dependency/binary is missing
        # appear; it's a static probe, distinct from the risk ⚠ and config.
        try:
            from evi.tools.availability import tool_availability
            snap["availability"] = tool_availability()
        except Exception:  # noqa: BLE001 — never let the probe break settings
            snap["availability"] = {}
        # Whether a token gates this server — config's, the launch token the
        # desktop shell passes when LAN access is on (never in config.toml), or
        # multi-user logins.
        from evi.users import load_users

        snap["access"] = {
            "token": bool(_web_tokens(cfg)),
            "launch_token": bool(_LAUNCH_TOKEN),
            "logins": bool(cfg.web.multi_user and load_users()),
        }
        return snap

    @app.post("/api/config")
    def config_set(patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a nested {section: {key: value}} patch and persist it.

        Unknown sections/keys are ignored. Touched sections are also pushed
        onto every live session so changes take effect without a new chat;
        the LLM client is rebuilt when the llm section changes (covers
        backend/base_url/api_key/model switches). Returns the fresh masked
        snapshot so the UI can re-render."""
        if not isinstance(patch, dict):
            raise HTTPException(400, "expected an object body")
        cfg = Config.load()
        touched = _apply_config_patch(cfg, patch)
        cfg.save()
        if "companion" in touched:
            # Switched on (or retimed) here, the Companion's jobs must follow
            # in this server's scheduler, not wait for a restart.
            _reload_companion_jobs()

        for sess in _all_sessions():
            ac = sess.agent.config
            for section in _HOT_SECTIONS:
                if section not in touched:
                    continue
                src = getattr(cfg, section)
                dst = getattr(ac, section)
                for f in fields(src):
                    setattr(dst, f.name, getattr(src, f.name))
            if "llm" in touched:
                try:
                    sess.agent.client = make_client(cfg.llm)
                except Exception:  # noqa: BLE001
                    pass
                # Re-stitch the system prompt so the agent's self-identity
                # (embedded model id) follows the new model — mirrors
                # model_picker_set and the /model slash command. Without this a
                # Settings model change left the live session claiming the old
                # model until /reload or restart.
                sess.agent.refresh_prompt()

        # Some sections only fully bind at session creation (tool enablement,
        # permission policy). Tell the UI which of those changed so it can hint
        # "applies to new chats".
        deferred = [s for s in ("tools", "auto") if s in touched]
        return {"ok": True, "touched": touched, "deferred": deferred,
                "config": _config_snapshot(cfg)}

    @app.get("/api/guardrails")
    def guardrails_get() -> dict[str, Any]:
        """The content-filter config: raw guardrails.toml + a parsed summary."""
        from evi.guardrails import Guardrails, read_raw

        g = Guardrails.load()
        return {
            "raw": read_raw(),
            "enabled": g.enabled,
            "summary": {
                "regex": len(g.rules),
                "judge": len(g.judge_rules),
                "classifier": len(g.classifier_rules),
            },
            "rules": [{"name": r.name, "action": r.action, "applies_to": r.applies_to}
                      for r in g.rules],
            "judge": [{"name": j.name, "applies_to": j.applies_to} for j in g.judge_rules],
            "classifier": [{"name": c.name, "model": c.model} for c in g.classifier_rules],
        }

    @app.post("/api/guardrails")
    def guardrails_set(req: dict[str, Any]) -> dict[str, Any]:
        """Validate + save guardrails.toml from the editor. 400 on bad TOML."""
        from evi.guardrails import Guardrails, validate, write_raw

        if not isinstance(req, dict) or "raw" not in req:
            raise HTTPException(400, "expected {raw: <toml>}")
        raw = str(req["raw"])
        err = validate(raw)
        if err:
            raise HTTPException(400, err)
        write_raw(raw)
        g = Guardrails.load()
        return {"ok": True, "enabled": g.enabled,
                "summary": {"regex": len(g.rules), "judge": len(g.judge_rules),
                            "classifier": len(g.classifier_rules)}}

    @app.get("/api/hooks")
    def hooks_get() -> dict[str, Any]:
        """The hooks config: raw hooks.toml + a summary of every loaded hook
        (including plugin-supplied ones, which are read-only here)."""
        registry = hooks_mod.load_hooks()
        return {
            "raw": hooks_mod.read_raw(),
            "events": list(hooks_mod.ALL_EVENTS),
            "hooks": [
                {"name": h.name, "event": h.event, "match": h.match,
                 "kind": "url" if h.url else "command",
                 "veto": h.veto_on_nonzero}
                for h in registry.hooks
            ],
        }

    @app.post("/api/hooks")
    def hooks_set(req: dict[str, Any]) -> dict[str, Any]:
        """Validate + save hooks.toml from the editor. 400 on bad TOML, a
        typo'd event name, or a malformed entry (loudly — unlike the runtime
        loader, which skips bad rows). Open sessions and the runs this server
        starts share one registry, reloaded here, so a save applies at once."""
        if not isinstance(req, dict) or "raw" not in req:
            raise HTTPException(400, "expected {raw: <toml>}")
        raw = str(req["raw"])
        err = hooks_mod.validate(raw)
        if err:
            raise HTTPException(400, err)
        hooks_mod.write_raw(raw)
        return {"ok": True, "count": _reload_user_hooks()}

    @app.get("/api/plugins")
    def plugins_list() -> dict[str, Any]:
        """Installed plugins + the marketplace index (with an `installed` flag)."""
        from evi import marketplace, plugins

        installed = plugins.list_plugins()
        names = {p.name.lower() for p in installed}
        try:
            entries = marketplace.load_index(index_urls=Config.load().plugins.index_urls)
        except Exception:  # index is best-effort; never wedge the page
            entries = []
        return {
            "installed": [
                {"name": p.name, "description": p.description, "version": p.version,
                 "commands": p.commands, "skills": p.skills, "hooks": p.hooks,
                 "mcp": p.mcp, "agents": p.agents}
                for p in installed
            ],
            # Plugin dirs whose manifest failed to parse — surfaced so a broken
            # plugin doesn't just silently vanish from the list.
            "failed": plugins.list_plugin_errors(),
            "marketplace": [
                {"name": e.name, "description": e.description, "author": e.author,
                 "source": e.source, "tags": e.tags,
                 "installed": e.name.lower() in names}
                for e in entries
            ],
        }

    @app.post("/api/plugins/install")
    def plugins_install(req: dict[str, Any]) -> dict[str, Any]:
        """Install a plugin — by marketplace `name`, or directly from a `source`
        (local dir or git URL)."""
        from evi import marketplace, plugins

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object")
        source = str(req.get("source") or "").strip()
        name = str(req.get("name") or "").strip()
        if not source and name:
            entries = marketplace.load_index(index_urls=Config.load().plugins.index_urls)
            entry = marketplace.resolve(name, entries)
            if entry is None:
                raise HTTPException(404, f"no plugin named {name!r} in the index")
            source = entry.source
        if not source:
            raise HTTPException(400, "expected {name} or {source}")
        try:
            installed = plugins.install(source)
        except plugins.PluginError as exc:
            raise HTTPException(400, str(exc))
        _reload_user_hooks()  # a plugin can ship a hooks.toml
        return {"ok": True, "name": installed}

    @app.post("/api/plugins/remove")
    def plugins_remove(req: dict[str, Any]) -> dict[str, Any]:
        """Remove an installed plugin by name."""
        from evi import plugins

        name = str((req or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        if not plugins.remove(name):
            raise HTTPException(404, f"no such plugin: {name}")
        _reload_user_hooks()  # its hooks stop now, not at the next restart
        return {"ok": True}

    @app.get("/api/stats")
    def stats_get(days: int = 0) -> dict[str, Any]:
        """Local usage analytics from transcripts (same data as `evi stats`)."""
        from evi import stats as _stats

        return _stats.compute_stats(days=(days or None))

    def _case_summary(case) -> dict[str, Any]:
        """A compact, assertion-focused view of a case for the evals browser."""
        checks = []
        if case.contains:
            checks.append("contains " + ", ".join(case.contains))
        if case.not_contains:
            checks.append("not " + ", ".join(case.not_contains))
        if case.regex:
            checks.append(f"regex /{case.regex}/")
        if case.equals is not None:
            checks.append(f"equals {case.equals!r}")
        if case.judge:
            checks.append("judge: " + case.judge)
        if case.expect_tools:
            checks.append("calls " + ", ".join(
                t if isinstance(t, str) else " or ".join(t) for t in case.expect_tools))
        if case.forbid_tools:
            checks.append("never calls " + ", ".join(case.forbid_tools))
        if case.tool_order:
            checks.append("order " + " → ".join(case.tool_order))
        for m in case.tool_args:
            checks.append(f"{'no ' if m.absent else ''}{m.tool} {m.arg} {m.describe()}")
        if case.stop_reason:
            checks.append(f"stops with {case.stop_reason}")
        for argv in case.command_succeeds:
            checks.append("succeeds: " + " ".join(argv))
        for fm in case.file_contains:
            checks.append(f"{fm.path} has " + (repr(fm.contains) if fm.contains is not None
                                               else f"/{fm.regex}/"))
        if case.diff_touches_only is not None:
            checks.append("changes only " + (", ".join(case.diff_touches_only) or "nothing"))
        if case.runs > 1 or case.pass_rate < 1:
            checks.append(f"{case.runs} run{'s' if case.runs != 1 else ''}, "
                          f"passes at {round(case.pass_rate * 100)}%")
        if case.expected_fail:
            checks.append("expected to fail")
        return {"name": case.name, "prompt": case.prompt, "checks": checks,
                "mode": case.mode}

    @app.get("/api/evals")
    def evals_list() -> dict[str, Any]:
        """List eval suites with their cases, and the latest report of each
        (suite, model) under <eVi home>/evals/reports (read-only; no model
        calls). A suite's `file` (its file name, without .toml) is what POST
        /api/evals/run takes, as `evi eval run` does: its `name` may differ.
        A `shadowed` one (a self.toml: `self` means a repository's
        evals/self) can't be run by that name."""
        from evi import evals

        return {
            "suites": [
                {"name": s.name, "file": s.path.stem if s.path is not None else s.name,
                 "shadowed": evals.shadowed(s), "description": s.description,
                 "cases": [_case_summary(c) for c in s.cases]}
                for s in evals.list_suites()
            ],
            "reports": evals.latest_reports(),
        }

    @app.post("/api/evals/run")
    def evals_run(req: dict[str, Any]) -> dict[str, Any]:
        """Run one of your eval suites (~/.evi/evals; `name` is its file
        name, GET /api/evals' `file`) as `evi eval run` does
        (`evals.run_suite`): each case in a throwaway workspace under the
        implementer policy, behavioral assertions, fixtures and repo
        worktrees (of a trusted repository only) included, on the configured
        [llm] backend and model (`evals.configured_context`; the fake backend
        is never offered here). Keeps the report as the CLI does
        (`evals.write_report`) and returns it (`evals.REPORT_SCHEMA`).

        A plain def: FastAPI runs it on a worker thread, so the event loop
        keeps serving while the suite runs (many model calls); like the
        recipe and ultracode runs, it is not cancelled when the browser goes
        away. No user hooks (the engine builds bare agents): a hook that
        vetoes or rewrites a call would make the scores depend on this
        machine."""
        from evi import evals

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        if str(req.get("backend") or "").strip():
            raise HTTPException(400, "evals run here on the configured [llm] backend; "
                                     "`evi eval run --backend` runs another")
        if evals.is_self_name(name):
            raise HTTPException(400, "`self` names a repository's evals/self suites, which "
                                     "only `evi eval run self` runs (in that repository)")
        try:
            suite = evals.load_suite(name)
        except evals.EvalError as exc:
            raise HTTPException(404, str(exc))
        cfg = Config.load()
        with _unattended_run(evals.ENTRY, cfg):
            try:
                ctx = evals.configured_context(cfg, default_mode=str(req.get("mode") or ""))
                report = evals.run_suite(suite, ctx)
            except evals.EvalError as exc:
                # A backend no policy can hold (a CLI agent): nothing ran.
                raise HTTPException(409, str(exc)) from None
        try:
            evals.write_report(report)
        except evals.EvalError as exc:
            raise HTTPException(500, str(exc)) from None
        except OSError as exc:
            raise HTTPException(500, f"the suite ran, but its report could not be kept in "
                                     f"{evals.reports_dir()}: {exc}") from None
        return report

    @app.get("/api/routes")
    def routes_get() -> dict[str, Any]:
        """List multi-model routing rules (~/.evi/routes.json)."""
        from evi.routing import RouterStore

        return {"routes": [asdict(r) for r in RouterStore().load()]}

    @app.post("/api/routes")
    def routes_add(req: dict[str, Any]) -> dict[str, Any]:
        """Add or replace a route. Keywords may be a list or a comma string."""
        from evi.routing import Route, RouterStore

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()
        model = str(req.get("model") or "").strip()
        if not name or not model:
            raise HTTPException(400, "name and model are required")
        kws_in = req.get("keywords") or []
        if isinstance(kws_in, str):
            kws = [k.strip() for k in kws_in.split(",") if k.strip()]
        elif isinstance(kws_in, list):
            kws = [str(k).strip() for k in kws_in if str(k).strip()]
        else:
            kws = []
        route = Route(name=name, model=model,
                      description=str(req.get("description") or ""), match_keywords=kws)
        # The UI edits in place, so default to overwrite (single-user, local).
        RouterStore().add(route, overwrite=bool(req.get("overwrite", True)))
        return {"ok": True, "name": name}

    @app.post("/api/routes/remove")
    def routes_remove(req: dict[str, Any]) -> dict[str, Any]:
        """Remove a route by name."""
        from evi.routing import RouterStore

        name = str((req or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        if not RouterStore().remove(name):
            raise HTTPException(404, f"no such route: {name}")
        return {"ok": True}

    @app.get("/api/recipes")
    def recipes_get() -> dict[str, Any]:
        """List saved multi-turn recipes (~/.evi/recipes/)."""
        from evi import recipes

        return {"recipes": [
            {"name": r.name, "description": r.description,
             "steps": [{"label": s.label, "prompt": s.prompt} for s in r.steps]}
            for r in recipes.list_recipes()
        ]}

    @app.post("/api/recipes/run")
    def recipes_run(req: dict[str, Any]) -> dict[str, Any]:
        """Run a recipe's steps through one shared agent (calls the model per
        step). Nobody can approve a tool call mid-run: auto-approved categories
        run, anything else follows [auto] unattended_policy (deny by default).
        Returns {ok, name, steps: [{label, prompt, text, error}, …], policy}."""
        from evi import recipes

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        try:
            recipe = recipes.load_recipe(name)
        except recipes.RecipeError as exc:
            raise HTTPException(404, str(exc))
        from evi.sdk.builder import build_agent

        # Single-user headless runner; project/guardrails stay off to match the
        # previous inline construction; the user's hooks apply (shared registry).
        cfg = Config.load()
        with _unattended_run("recipe", cfg):
            agent = _with_user_hooks(build_agent(
                config=cfg,
                enable_project=False,
                enable_hooks=False,
                enable_guardrails=False,
            ))
            policy = make_unattended(agent)  # [auto] unattended_policy
            results = recipes.run_recipe_headless(agent, recipe)
        return {"ok": True, "name": recipe.name, "steps": results, "policy": policy}

    @app.post("/api/dispatch/ultracode")
    def dispatch_ultracode(req: dict[str, Any]) -> dict[str, Any]:
        """Run one task through the ultracode pipeline server-side (decompose →
        parallel solvers → adversarial verify → synthesize). Blocks until done
        (many model calls), like the eval/recipe runners. Returns the final
        answer + every stage."""
        from evi import ultracode as uc

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        task = str(req.get("task") or "").strip()
        if not task:
            raise HTTPException(400, "expected {task}")

        def agent_factory(system_prompt: str | None, model: str | None = None):
            from evi.sdk.builder import build_agent

            # Single-user ultracode runner; project/guardrails stay off to match
            # the previous inline construction; the user's hooks apply (shared).
            # build_agent ignores system_prompt=None, preserving the eVi default.
            # `model` lets a stage run on a cheaper model (cheap fan-out).
            return _with_user_hooks(build_agent(
                config=Config.load(),
                system_prompt=system_prompt,
                model=model,
                enable_project=False,
                enable_hooks=False,
                enable_guardrails=False,
            ))

        cfg_obj = Config.load()
        ucfg = uc.load_ultra_config(cfg_obj)  # resolves [ultracode] cheap_fanout
        if req.get("breadth"):
            ucfg.breadth = int(req["breadth"])
        if req.get("rounds") is not None:
            ucfg.rounds = int(req["rounds"])
        if req.get("mode"):
            ucfg.mode = str(req["mode"])
        # Per-request cheaper fan-out: {cheap_fanout: true} or {solver_model: id}.
        if req.get("cheap_fanout") and cfg_obj.llm.fast_model:
            ucfg.stage_models.setdefault("solve", cfg_obj.llm.fast_model)
        if req.get("solver_model"):
            ucfg.stage_models["solve"] = str(req["solver_model"])
        if cfg_obj.ultracode.auto_tune:
            ucfg = uc.default_tuning(cfg_obj.llm.model, cfg_obj.llm.context_size, ucfg)
        # Stages run unattended (make_runner): refuse a delegate CLI agent up
        # front rather than as an ERROR in every stage.
        with _unattended_run("ultracode", cfg_obj):
            res = uc.run_ultracode(
                task, run_one=uc.make_runner(agent_factory, stage_models=ucfg.stage_models),
                cfg=ucfg,
            )
        return {
            "ok": True, "answer": res.answer,
            "stages": [asdict(s) for s in res.stages],
            "config": asdict(res.config),
        }

    @app.get("/api/peers")
    def peers_list() -> dict[str, Any]:
        """Configured peers (~/.evi/peers.json) with live reachability, plus
        whether this instance serves federation requests itself."""
        from concurrent.futures import ThreadPoolExecutor

        from evi import federation

        peers = federation.load_peers()
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(
                lambda p: federation.check_peer(p, timeout=2.0), peers
            ))
        cfg = Config.load()
        serving = cfg.federation.serve
        # Real serving posture (lan/loopback/off/down) + LAN ip — powers the
        # panel's proactive warnings without needing a full preflight.
        self_status = federation.self_serving_status(
            federation.DEFAULT_PEER_PORT, serve=serving
        )
        return {
            "peers": [
                {"name": p.name, "url": p.url, "has_token": bool(p.token), **st}
                for p, st in zip(peers, statuses)
            ],
            "serving": serving,
            "bind_lan": cfg.federation.bind_lan,
            # The desktop's generated LAN token counts: it gates this server too.
            "auth_token_set": bool(_web_tokens(cfg)),
            "self_status": self_status,
        }

    @app.post("/api/peers/serve")
    def peers_set_serve(req: dict[str, Any]) -> dict[str, Any]:
        """Toggle whether THIS eVi answers federation requests ([federation]
        serve) — so the Settings UI flips it instead of hand-editing config.toml.
        /api/federate reads the flag per request, so it takes effect immediately."""
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        cfg = Config.load()
        cfg.federation.serve = bool(req.get("enabled"))
        cfg.save()
        return {"ok": True, "serving": cfg.federation.serve}

    @app.post("/api/peers/bind-lan")
    def peers_set_bind_lan(req: dict[str, Any]) -> dict[str, Any]:
        """Toggle [federation] bind_lan — whether the DESKTOP app serves on
        0.0.0.0 (LAN) so it can be a reachable peer. Takes effect on the next
        desktop launch (the Tauri shell reads it at spawn)."""
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        cfg = Config.load()
        cfg.federation.bind_lan = bool(req.get("enabled"))
        cfg.save()
        return {"ok": True, "bind_lan": cfg.federation.bind_lan}

    @app.post("/api/peers")
    def peers_add(req: dict[str, Any]) -> dict[str, Any]:
        """Add or replace a peer ({name, url, token?})."""
        from evi import federation

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()
        url = str(req.get("url") or "").strip().rstrip("/")
        if not name or not url:
            raise HTTPException(400, "name and url are required")
        peer = federation.Peer(name=name, url=url,
                               token=str(req.get("token") or "").strip())
        federation.add_peer(peer, overwrite=bool(req.get("overwrite", True)))
        return {"ok": True, "name": name}

    @app.post("/api/peers/remove")
    def peers_remove(req: dict[str, Any]) -> dict[str, Any]:
        """Remove a peer by name."""
        from evi import federation

        name = str((req or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        if not federation.remove_peer(name):
            raise HTTPException(404, f"no such peer: {name}")
        return {"ok": True}

    @app.post("/api/peers/preflight")
    def peers_preflight(req: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the federation preflight (this node's serving posture + each peer's
        reachability/auth/test-delegation) and return {checks:[{name,status,detail,
        hint}], summary:{ok,warn,fail}}. Can take up to ~timeout per peer."""
        from evi import federation

        body = req if isinstance(req, dict) else {}
        cfg = Config.load()
        timeout = float(body.get("timeout") or 20.0)
        checks = federation.federation_preflight(
            serve=bool(getattr(cfg.federation, "serve", False)),
            auth_token=next(iter(_web_tokens(cfg)), ""),
            delegate_timeout=timeout,
            probe_delegation=not bool(body.get("fast")),
        )
        summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail")}
        return {"checks": checks, "summary": summary}

    @app.post("/api/peers/scan")
    def peers_scan(req: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sweep the local /24 (or explicit {hosts}) for eVi instances on
        {port} (default 8473). Marks hits that are already configured peers."""
        from evi import federation

        body = req if isinstance(req, dict) else {}
        port = int(body.get("port") or federation.DEFAULT_PEER_PORT)
        hosts = body.get("hosts")
        if hosts is not None and not (
            isinstance(hosts, list) and all(isinstance(h, str) for h in hosts)
        ):
            raise HTTPException(400, "hosts must be a list of strings")
        found = federation.scan_network(port, hosts=hosts)
        configured = {p.url.rstrip("/") for p in federation.load_peers()}
        for f in found:
            f["configured"] = f["url"].rstrip("/") in configured
        return {"found": found, "port": port,
                "scanned": len(hosts) if hosts is not None else 254}

    @app.get("/api/mcp")
    def mcp_list() -> dict[str, Any]:
        """Configured MCP servers (user + plugin-namespaced). Env VALUES are
        never echoed (they may hold API keys) — only the key names."""
        from evi.mcp.servers import load_servers

        cfg = Config.load()
        allow = set(cfg.tools.mcp_allow or ())
        return {
            "enabled": cfg.tools.mcp,
            "allowlist": sorted(allow),
            "servers": [
                {
                    "name": s.name,
                    "command": s.command,
                    "args": s.args,
                    "env_keys": sorted(s.env),
                    "on": s.enabled,
                    "plugin": ":" in s.name,
                    "allowed": (not allow) or s.name in allow,
                }
                for s in load_servers()
            ],
        }

    @app.post("/api/mcp")
    def mcp_add_ep(req: dict[str, Any]) -> dict[str, Any]:
        """Add or replace a user MCP server. `args` may be a list or a single
        shell-style string (split with shlex)."""
        import shlex

        from evi.mcp.servers import MCPServer, add_server

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()
        command = str(req.get("command") or "").strip()
        if not name or not command:
            raise HTTPException(400, "name and command are required")
        if ":" in name:
            raise HTTPException(400, "':' is reserved for plugin-supplied servers")
        raw_args = req.get("args") or []
        if isinstance(raw_args, str):
            try:
                args = shlex.split(raw_args)
            except ValueError as exc:
                raise HTTPException(400, f"could not parse args: {exc}")
        elif isinstance(raw_args, list):
            args = [str(a) for a in raw_args]
        else:
            raise HTTPException(400, "args must be a string or a list")
        env = req.get("env") or {}
        if not isinstance(env, dict):
            raise HTTPException(400, "env must be an object")
        server = MCPServer(name=name, command=command, args=args,
                           env={str(k): str(v) for k, v in env.items()},
                           enabled=bool(req.get("enabled", True)))
        add_server(server, overwrite=bool(req.get("overwrite", True)))
        return {"ok": True, "name": name}

    @app.post("/api/mcp/remove")
    def mcp_remove_ep(req: dict[str, Any]) -> dict[str, Any]:
        """Remove a user MCP server (plugin servers are plugin-owned)."""
        from evi.mcp.servers import remove_server

        name = str((req or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        if ":" in name:
            raise HTTPException(400, f"{name} is plugin-supplied — remove its plugin instead")
        if not remove_server(name):
            raise HTTPException(404, f"no such server: {name}")
        return {"ok": True}

    @app.post("/api/mcp/toggle")
    def mcp_toggle_ep(req: dict[str, Any]) -> dict[str, Any]:
        """Switch a user MCP server on/off."""
        from evi.mcp.servers import set_enabled

        name = str((req or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name, on}")
        if not set_enabled(name, bool(req.get("on", True))):
            raise HTTPException(404, f"no such server: {name}")
        return {"ok": True}

    # --- serve: run eVi AS an MCP server (the inverse of the panel above) ----
    # One in-process HTTP MCP server the user can start/stop from the UI so
    # other agents can reach eVi's curated tools/memory/prompts. stdio serving
    # stays a CLI concern (a desktop client spawns it); the UI hands out a
    # copy-paste config for that via /api/mcp/serve/config.
    @app.get("/api/mcp/serve")
    def mcp_serve_status() -> dict[str, Any]:
        from evi.mcp import publish

        th = _mcp_serve.get("thread")
        running = bool(th and th.is_alive())
        port = _mcp_serve.get("port", 0) if running else 0
        return {
            "running": running,
            "port": port,
            "url": (f"http://127.0.0.1:{port}/mcp" if running else ""),
            "categories": _mcp_serve.get("categories", []) if running else [],
            "token_set": _mcp_serve.get("token_set", False) if running else False,
            "default_categories": list(publish.DEFAULT_CATEGORIES),
        }

    @app.post("/api/mcp/serve")
    def mcp_serve_start(req: dict[str, Any]) -> dict[str, Any]:
        """Start a localhost HTTP MCP server exposing eVi's curated surface.
        Body: {categories?: [str], token?: str, port?: int}. Bound to 127.0.0.1
        (a token is optional for localhost; required if you later expose it)."""
        import socket as _socket
        import threading as _threading

        if _mcp_serve.get("thread") and _mcp_serve["thread"].is_alive():
            raise HTTPException(409, "already serving — stop it first")
        try:
            import uvicorn

            from evi.mcp import publish
        except ImportError as exc:
            raise HTTPException(400, f"MCP serve needs the [mcp] + web extras: {exc}") from exc

        body = req or {}
        cats = tuple(c for c in (body.get("categories") or publish.DEFAULT_CATEGORIES) if c)
        token = str(body.get("token") or "").strip()
        port = int(body.get("port") or 0)
        if not port:
            s = _socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
        try:
            http_app = publish.build_http_app(cats, None, token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"could not build MCP server: {exc}") from exc
        server = uvicorn.Server(uvicorn.Config(http_app, host="127.0.0.1", port=port,
                                               log_level="warning"))

        def _run() -> None:
            try:
                server.run()  # uvicorn skips signal handlers off the main thread
            except Exception:  # noqa: BLE001
                pass

        th = _threading.Thread(target=_run, name="mcp-serve", daemon=True)
        th.start()
        _mcp_serve.update({"server": server, "thread": th, "port": port,
                           "categories": list(cats), "token_set": bool(token)})
        return {"ok": True, "port": port, "url": f"http://127.0.0.1:{port}/mcp",
                "categories": list(cats), "token_set": bool(token)}

    @app.post("/api/mcp/serve/stop")
    def mcp_serve_stop() -> dict[str, Any]:
        server = _mcp_serve.get("server")
        if server is not None:
            server.should_exit = True  # uvicorn polls this and unwinds the loop
        _mcp_serve.update({"server": None, "thread": None, "port": 0})
        return {"ok": True}

    @app.get("/api/mcp/serve/config")
    def mcp_serve_config(categories: str = "") -> dict[str, Any]:
        """The stdio client-config snippet a desktop MCP client (Claude Desktop,
        Cursor) pastes to spawn eVi as an MCP server (mirrors `evi mcp serve-config`)."""
        import sys as _sys

        from evi.mcp import publish

        cats = categories.strip() or ",".join(publish.DEFAULT_CATEGORIES)
        if publish.AGENT_CATEGORY in (c.strip() for c in cats.split(",")):
            # The director-facing task tools need the repository (--repo) and
            # their own server; POST /api/mcp/serve refuses them too.
            raise HTTPException(
                400, f"the {publish.AGENT_CATEGORY!r} category isn't offered here: run "
                     f"`evi mcp serve-config --categories {publish.AGENT_CATEGORY}` in "
                     "the repository instead")
        snippet = {"mcpServers": {"evi": {
            "command": _sys.executable,
            "args": ["-m", "evi", "mcp", "serve", "--categories", cats],
        }}}
        return {"snippet": snippet, "categories": cats}

    @app.get("/api/docs")
    def docs_list() -> dict[str, Any]:
        """List bundled documentation pages for the in-app docs viewer."""
        d = _docs_dir()
        if d is None:
            return {"pages": []}
        pages = [{"slug": p.stem, "title": _doc_title(p)} for p in sorted(d.glob("*.md"))]
        return {"pages": pages}

    @app.get("/api/docs/{slug}")
    def docs_page(slug: str) -> dict[str, Any]:
        """Render one doc page to HTML (offline, no CDN). Slug is sanitised to
        the bare filename to prevent path traversal."""
        d = _docs_dir()
        if d is None:
            raise HTTPException(404, "docs not bundled")
        safe = Path(slug).name  # strip any directory components
        page = d / f"{safe}.md"
        if not page.is_file():
            raise HTTPException(404, f"no such doc {safe!r}")
        from evi.apps.web.mdlite import render

        return {"slug": safe, "title": _doc_title(page),
                "html": render(page.read_text(encoding="utf-8"))}

    @app.get("/api/doctor")
    def doctor_api() -> dict[str, Any]:
        """Run `evi doctor` checks and return them as JSON for the in-app
        Diagnostics panel (Help → Run Diagnostics)."""
        from evi.doctor import run_checks, summarize

        checks = run_checks()
        ok, warn, fail = summarize(checks)
        return {
            "checks": [
                {"name": c.name, "ok": c.status == "ok", "level": c.status, "detail": c.detail}
                for c in checks
            ],
            "summary": {"ok": ok, "warn": warn, "fail": fail},
        }

    @app.get("/api/system")
    def system_info() -> dict[str, Any]:
        """Hardware + OS stats for the Settings → Model & Backend page, plus the
        hardware-recommended model and whether it's already installed."""
        import platform as _plat

        from evi import hardware
        from evi.recommend import recommend

        hw = hardware.detect()
        gpus = [
            {
                "name": g.name,
                "vram_total_mb": g.vram_total_mb,
                "vram_free_mb": g.vram_free_mb,
                "driver": g.driver_version,
                "compute": g.compute_capability,
            }
            for g in hw.gpus
        ]
        try:
            rec = recommend(hw)
        except Exception:  # noqa: BLE001
            rec = None

        cfg = Config.load()
        installed: list[str] = []
        try:
            installed = [m.id for m in get_backend(cfg.llm).list_models()]
        except Exception:  # noqa: BLE001
            installed = []

        rec_chat = rec.chat.id if rec and rec.chat else ""
        rec_coder = rec.coder.id if rec and rec.coder else ""
        from evi import sandbox as _sandbox

        return {
            "os": f"{_plat.system()} {_plat.release()}",
            "os_detail": _plat.platform(),
            "python": _plat.python_version(),
            "platform": hw.platform,
            "ram_gb": round(hw.ram_total_gb, 1),
            "gpus": gpus,
            "gpu_vendor": "nvidia" if gpus else "none",
            "mode": rec.mode if rec else "unknown",
            "recommended": {
                "chat": rec_chat,
                "coder": rec_coder,
                "chat_installed": rec_chat in installed if rec_chat else False,
                "coder_installed": rec_coder in installed if rec_coder else False,
                "notes": rec.notes if rec else [],
            },
            "backend": cfg.llm.backend,
            "current_model": cfg.llm.model,
            "sandbox": {"enabled": cfg.tools.sandbox, **_sandbox.status()},
        }

    @app.get("/api/styles")
    def styles_list() -> dict[str, Any]:
        """Available output styles for the settings picker."""
        from evi import styles

        return {"styles": styles.list_styles(), "active": Config.load().llm.output_style}

    @app.get("/api/modes")
    def modes_list() -> dict[str, Any]:
        """The session modes for the Chat/Cowork/Code switcher."""
        from evi.modes import DEFAULT_MODE, MODES

        return {
            "modes": [
                {"name": m.name, "label": m.label, "blurb": m.blurb} for m in MODES.values()
            ],
            "default": DEFAULT_MODE,
        }

    @app.post("/api/session/{session_id}/mode")
    def session_set_mode(session_id: str, req: ModeRequest) -> dict[str, Any]:
        """Set a session's mode, hot-swapping the agent's tool set so it takes
        effect on the next turn (no new chat needed)."""
        from evi.modes import mode_tools, resolve

        sess = get_session(session_id)
        mode = resolve(req.mode).name
        sess.mode = mode
        sess.agent.tools = {t.name: t for t in mode_tools(mode)}
        # [checks] verify = "always" runs the project checks in Code mode.
        sess.agent.mode = mode
        return {"ok": True, "mode": mode,
                "tools": sorted(t.name for t in sess.agent.tools.values())}

    @app.get("/api/checkpoints")
    def checkpoints_list() -> dict[str, Any]:
        """Recent file checkpoints for the rewind UI."""
        from evi import checkpoints

        return {"checkpoints": checkpoints.list_checkpoints()}

    @app.post("/api/rewind")
    def rewind_files(req: dict[str, Any]) -> dict[str, Any]:
        """Undo file writes from a checkpoint seq onward (or just the latest)."""
        from evi import checkpoints

        seq = req.get("seq") if isinstance(req, dict) else None
        actions = checkpoints.rewind(int(seq) if seq else None)
        return {"ok": True, "actions": [{"path": p, "action": a} for p, a in actions]}

    @app.post("/api/routine/{token}")
    def run_routine_endpoint(token: str) -> dict[str, Any]:
        """Webhook trigger: run a routine's recipe headless. Auth is the
        unguessable path token. Restricted permissions unless the routine is
        marked `yes` (auto-approve all)."""
        from evi import recipes, routines

        r = routines.get_by_token(token)
        if r is None or not r.enabled:
            raise HTTPException(404, "no such routine")
        try:
            recipe = recipes.load_recipe(r.recipe)
        except recipes.RecipeError as exc:
            raise HTTPException(400, str(exc))

        from evi.sdk.builder import build_agent

        # Single-user headless runner; project/guardrails stay off to match the
        # previous inline construction; the user's hooks apply (shared registry).
        cfg = Config.load()
        with _unattended_run("routine", cfg):
            agent = _with_user_hooks(build_agent(
                config=cfg,
                enable_project=False,
                enable_hooks=False,
                enable_guardrails=False,
            ))
            if r.yes:
                agent.enable_auto_all()  # this routine's own, explicit opt-in
                results = recipes.run_recipe_headless(agent, recipe)
            else:
                # Triggered from outside: deny non-auto-approved tools, whatever
                # [auto] unattended_policy or mode says.
                with remote_unattended(agent):
                    results = recipes.run_recipe_headless(agent, recipe)
        return {"ok": True, "routine": r.name, "results": results}

    @app.post("/api/reset")
    def reset(req: ChatRequest) -> dict[str, str]:
        sess = _my_sessions().get(_require_session_id(req.session_id))
        if sess is not None:
            with _holding_turn(sess, req.session_id):
                sess.agent.reset()
        return {"status": "ok"}

    @app.post("/api/decide")
    def decide(req: DecisionRequest) -> dict[str, object]:
        bucket = _my_sessions()
        sess = bucket.get(_require_session_id(req.session_id))
        pending = sess.pending.get(req.decision_id) if sess is not None else None
        if pending is None:
            # The prompt may belong to another of the caller's tabs (the tab was
            # switched mid-turn). Decision ids are random, so look across the
            # caller's own sessions — never another user's bucket.
            for other in list(bucket.values()):
                pending = other.pending.get(req.decision_id)
                if pending is not None:
                    break
        if pending is None:
            if sess is None:
                raise HTTPException(404, "no such session")
            return {"ok": False, "reason": "no such decision"}
        pending.approved = req.approved
        pending.event.set()
        return {"ok": True}

    # --- session history manipulation (edit / re-roll / branch) --------

    @app.get("/api/session/{session_id}/history")
    def session_history(session_id: str, since: int = 0) -> dict[str, object]:
        """Return the session's history so the browser can rebuild state after a
        reload. Uses get_session so a session that's only on disk (e.g. the
        desktop app was reopened) is revived from its transcript rather than
        404'ing into a blank chat.

        `since` returns only the messages from that index on (the browser
        catching up on notes that arrived after what it drew), and `length` is
        the whole history's. A message drawn as something other than a plain
        bubble carries a `kind` (_history_kind: a note, the compaction summary
        and its acknowledgement). `held` lists the notes waiting for the
        running turn to end (WebSession.held_notes): they have no index yet,
        and join the history, after the turn's messages, when it ends."""
        sess = get_session(session_id)
        # Strip the bulky image_url data URLs from any multipart user content
        # so the response stays small. The agent still has them in memory.
        # NB: the system message at index 0 is kept so `index` stays aligned
        # with the agent's history (edit/branch use it); the client skips it.
        # Uploaded files for this session, so we can re-attach image previews to
        # the user messages that reference them — the attach-time thumbnail is a
        # transient blob, lost when the browser rebuilds history on a tab switch.
        # Stateless (no per-message tracking): a user message "owns" any uploaded
        # image whose filename appears in its (folded) text, so this also works
        # for a session revived from its transcript.
        from urllib.parse import quote

        # get_session above already refused a malformed id; re-checked here so
        # the listing can never walk off UPLOADS_DIR.
        udir = UPLOADS_DIR / session_id if valid_session_id(session_id) else None
        upload_imgs = (
            [
                p.name
                for p in udir.iterdir()
                if p.is_file()
                and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
            ]
            if udir is not None and _within(udir, UPLOADS_DIR) and udir.is_dir()
            else []
        )

        # One snapshot of both, under the lock the turn's end releases held
        # notes under, so a note is in exactly one of them. (The turn's worker
        # may still append to the history meanwhile.)
        with sess.turn_lock:
            history = list(sess.agent.history)
            held = [dict(n) for n in sess.held_notes]
        # Tool calls and results redraw redacted, as the stream showed them
        # (_event_payload); the agent's history keeps the real values.
        red = Redactor()
        cleaned: list[dict] = []
        for i in range(max(0, since), len(history)):
            msg = history[i]
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            if msg.get("role") == "tool" and isinstance(content, str):
                content = red(content)
            tool_calls = msg.get("tool_calls")
            entry: dict = {
                "index": i,
                "role": msg.get("role"),
                "content": content,
                "tool_calls": red.tool_calls(tool_calls) if tool_calls else tool_calls,
                "name": msg.get("name"),
            }
            kind = _history_kind(history, i)
            if kind is not None:
                entry["kind"] = kind
            elif msg.get("role") == "user" and upload_imgs and isinstance(content, str):
                imgs = [
                    f"/uploads/{session_id}/{quote(n)}" for n in upload_imgs if n in content
                ]
                if imgs:
                    entry["images"] = imgs
            cleaned.append(entry)
        return {
            "messages": cleaned,
            "length": len(history),
            "held": [
                {"role": n.get("role"), "content": n.get("content"), "kind": "note"}
                for n in held
            ],
        }

    @app.post("/api/session/{session_id}/title")
    def session_title(session_id: str) -> dict[str, object]:
        """Generate a short LLM-written title for the tab. Returns
        `{title}` (empty string if the model couldn't produce one)."""
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        return {"title": sess.agent.suggest_title()}

    @app.post("/api/session/{session_id}/truncate")
    def session_truncate(session_id: str, req: TruncateRequest) -> dict[str, object]:
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        with _holding_turn(sess, session_id):
            removed = sess.agent.truncate_history(req.after_index)
        return {"ok": True, "removed": removed, "length": len(sess.agent.history)}

    @app.post("/api/session/{session_id}/edit")
    def session_edit(session_id: str, req: EditRequest) -> dict[str, object]:
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        with _holding_turn(sess, session_id):
            ok = sess.agent.edit_message(req.at_index, req.content)
        if not ok:
            raise HTTPException(400, "cannot edit that index")
        return {"ok": True, "length": len(sess.agent.history)}

    @app.post("/api/session/{session_id}/branch")
    def session_branch(session_id: str, req: BranchRequest) -> dict[str, object]:
        """Copy history up to and including at_index into a new session.

        Returns `{new_session_id}` so the client can switch to it.
        """
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        cutoff = max(1, req.at_index + 1)
        snapshot = [dict(m) for m in sess.agent.history[:cutoff]]

        # Spin up a fresh session that mirrors the parent's setup; copy
        # history wholesale onto it.
        new_id = secrets.token_hex(8)
        new_session = get_session(new_id)
        # The factory inserts a system message at index 0; replace history
        # outright with our snapshot (the snapshot's index 0 is also system).
        new_session.agent.history = snapshot
        # The copy goes into the branch's transcript (a fork_of record), so
        # the branch survives a restart. Guarded for lightweight test doubles.
        log_fork = getattr(new_session.agent, "log_fork", None)
        if callable(log_fork):
            log_fork(session_id)
        # Inherit the parent's model/backend/effort + working folder so the
        # branch continues under the same setup instead of falling back to app
        # defaults (a branch off a coder model shouldn't silently become the
        # default chat model).
        for f in fields(sess.agent.config.llm):
            setattr(new_session.agent.config.llm, f.name,
                    getattr(sess.agent.config.llm, f.name))
        new_session.agent.cwd = getattr(sess.agent, "cwd", "")
        try:
            new_session.agent.client = make_client(new_session.agent.config.llm)
        except Exception:  # noqa: BLE001
            pass
        return {"new_session_id": new_id, "length": len(snapshot)}

    @app.post("/api/session/{session_id}/reroll")
    async def session_reroll(session_id: str) -> EventSourceResponse:
        """Drop the last assistant turn (and any tool messages after the
        last user) and regenerate. Notes that arrived after it come back
        after the new reply. SSE shape matches /api/chat."""
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        timeout_s = _decision_timeout_s(Config.load())
        # A re-roll is a turn: claimed before the history is touched, so it
        # can't rewind under (or run beside) one already in flight.
        if not sess.begin_turn():
            raise _busy_error(session_id)
        try:
            _hold_notes_past_last_prompt(sess)
            sess.agent.rewind_to_last_user()
        except BaseException:
            sess.end_turn()
            raise
        # rewind_to_last_user() returns the count it popped, which is 0 when the
        # history ALREADY ends at a user message — e.g. right after editing a
        # user message truncated its stale reply. That's a valid re-roll target,
        # not a no-op; only bail when there is genuinely no user turn to answer.
        _hist = sess.agent.history
        # Where the rewind left the history, for the browser to redraw up to
        # (X-Evi-History-Length): the new reply can be in it before the
        # browser asks, and the stream draws that.
        rewound = len(_hist)
        if len(_hist) <= 1 or _hist[-1].get("role") != "user":
            sess.end_turn()

            async def _noop() -> AsyncIterator[dict[str, Any]]:
                yield {"kind": "Error", "message": "nothing to re-roll"}
                yield {"kind": "Done", "reason": "noop"}
            return EventSourceResponse(_sse_frames(_noop()))

        # Same machinery as /api/chat but invoke continue_chat (no new user).
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def enqueue(payload: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        sess.agent.permission_callback = _make_permission_callback(
            _my_sessions(), session_id, enqueue, timeout_s,
        )
        # The turn starts here, in the request, so a Stop sent before the
        # worker gets going still ends it (WebSession.start_turn).
        try:
            events = sess.start_turn(sess.agent.continue_chat)
        except BaseException:
            sess.end_turn()
            raise

        # A follow-up can be steered onto a re-roll too (/api/chat answers 409
        # and the UI queues it), so the re-roll hands leftovers back the same way.
        threading.Thread(
            target=_stream_turn, args=(sess, lambda: events, enqueue), daemon=True,
        ).start()

        async def stream() -> AsyncIterator[dict[str, Any]]:
            while True:
                payload = await queue.get()
                if payload is None:
                    return
                yield payload

        return EventSourceResponse(_sse_frames(stream()),
                                   headers={"X-Evi-History-Length": str(rewound)})

    @app.post("/api/session/{session_id}/steer")
    def session_steer(session_id: str, req: dict[str, Any]) -> dict[str, Any]:
        """Queue a follow-up onto a turn that's already running.

        eVi applies it at the next step rather than interrupting the current
        one, so a mid-turn correction doesn't throw away work in flight. When
        the session ISN'T busy there is nothing to steer — we say so, and the
        caller sends a normal message instead.
        """
        text = str((req or {}).get("text", "")).strip()
        if not text:
            raise HTTPException(400, "expected {text}")
        sess = get_session(session_id)
        queued = sess.steer(text)
        if queued is None:
            return {"queued": False, "reason": "not busy"}
        return {"queued": queued, "pending": sess.agent.pending_steering()}

    @app.post("/api/session/{session_id}/cancel")
    def session_cancel(session_id: str) -> dict[str, Any]:
        """Stop the turn running in one of the caller's own live sessions (the
        UI's Stop button; the web side of REPL Ctrl+C). The turn ends with
        Done("interrupted") on its stream: a reply being streamed keeps its
        text, marked as cut off; running commands have their process trees
        killed; calls that never ran are answered "(cancelled)"; a permission
        question waiting on the browser is denied. Answers once the session
        is free again (normally well under a second; at most CANCEL_GRACE
        after the cancel while tools that ignore it are left behind), so the
        caller can send its next message at once. {"cancelled": false} when
        no turn is running."""
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such live session")
        if not sess.cancel_turn("the user stopped it from the web UI"):
            return {"cancelled": False, "reason": "not busy", "busy": False}
        deadline = time.monotonic() + CANCEL_GRACE + 1.0
        while sess.busy and time.monotonic() < deadline:
            time.sleep(CANCEL_POLL / 2)
        return {"cancelled": True, "busy": sess.busy}

    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request) -> EventSourceResponse:
        message = req.message.strip()
        if not message:
            raise HTTPException(400, "empty message")
        sess = get_session(req.session_id)

        # Server-side slash command dispatch — matches CLI semantics. While a
        # turn runs, only the read-only ones answer: /reset, /compact, /model
        # <id>… would rewrite the history the running turn is appending to.
        if message.startswith("/"):
            name, _, args = message[1:].strip().partition(" ")
            if name.lower() == "model" and args.strip():
                # `/model <id>` persists [llm] model to the host's config.toml,
                # like POST /api/model-picker — owner-only in multi-user mode.
                _require_owner(request)
            if _slash_is_read_only(message):
                outcome = _handle_slash(sess.agent, message, cmd_store)
            else:
                with _holding_turn(sess, req.session_id):
                    outcome = _handle_slash(sess.agent, message, cmd_store)
            if outcome.handled:
                async def _ack() -> AsyncIterator[dict[str, Any]]:
                    yield {"kind": "SystemMessage", "text": outcome.text,
                           "clear_transcript": outcome.clear_transcript}
                    yield {"kind": "Done", "reason": "slash"}
                return EventSourceResponse(_sse_frames(_ack()))
            if outcome.expand_to is not None:
                message = outcome.expand_to

        response_format = None
        if req.output_schema:
            from evi.structured import SchemaError, as_response_format, load_schema

            try:
                raw = (
                    req.output_schema
                    if isinstance(req.output_schema, dict)
                    else load_schema(req.output_schema)
                )
                response_format = as_response_format(raw)
            except SchemaError as exc:
                raise HTTPException(400, f"bad schema: {exc}")
        timeout_s = _decision_timeout_s(Config.load())

        # One turn per session. Claimed here, in the request, before the worker
        # starts — a second /api/chat (a reload mid-turn, a second VS Code
        # window, a slash command that expanded to a prompt) gets 409 and a
        # pointer at steering instead of a concurrent turn on the same Agent.
        if not sess.begin_turn():
            raise _busy_error(req.session_id)

        # Real LLM turn — run Agent.chat() in a worker thread so the async
        # SSE response stays responsive (and the permission_callback can
        # block on a threading.Event without freezing the event loop).
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def enqueue(payload: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        # Attach a session-scoped permission_callback for this turn.
        sess.agent.permission_callback = _make_permission_callback(
            _my_sessions(), req.session_id, enqueue, timeout_s,
        )

        # The turn starts here, in the request, not on the worker: a Stop sent
        # right after Send (POST /api/session/{id}/cancel) then has a turn to
        # end, however late the worker runs (WebSession.start_turn).
        try:
            events = sess.start_turn(lambda: sess.agent.chat(
                message,
                images=req.images,
                prediction=req.prediction,
                parallel_tool_calls=req.parallel_tool_calls,
                logit_bias=req.logit_bias,
                audio=req.audio,
                response_format=response_format,
            ))
        except BaseException:
            sess.end_turn()
            raise

        threading.Thread(
            target=_stream_turn, args=(sess, lambda: events, enqueue), daemon=True,
        ).start()

        async def stream() -> AsyncIterator[dict[str, Any]]:
            while True:
                payload = await queue.get()
                if payload is None:
                    return
                yield payload

        return EventSourceResponse(_sse_frames(stream()))

    @app.post("/api/transcribe")
    async def transcribe(
        session_id: str = Form(...),
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        """Save an audio upload, run it through Whisper, return the text.

        Used by the web UI when the user drops a .wav / .mp3 / .m4a / .ogg
        onto the chat. Requires `evi[stt]` to be installed.
        """
        if not session_id.strip() or not file.filename:
            raise HTTPException(400, "session_id and file are required")
        safe_name = Path(file.filename).name
        if not safe_name or safe_name in (".", ".."):
            raise HTTPException(400, "invalid filename")
        target_dir = _session_upload_dir(session_id)
        target = target_dir / safe_name
        if not _within(target, target_dir):
            raise HTTPException(400, "invalid filename")
        target_dir.mkdir(parents=True, exist_ok=True)
        # Same cap as /api/upload: an unbounded body would fill the disk.
        await _save_upload(file, target)
        try:
            from evi.voice import transcribe_wav

            text = transcribe_wav(target)
        except (RuntimeError, Exception) as exc:  # noqa: BLE001
            err = (
                exc.args[0] if isinstance(exc, RuntimeError) and exc.args
                else f"{type(exc).__name__}: {exc}"
            )
            return {"ok": False, "error": err, "path": str(target)}
        return {"ok": True, "text": text, "path": str(target)}

    @app.post("/api/upload")
    async def upload(
        session_id: str = Form(...),
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        """Receive a file dropped on the chat UI. Saves under
        ~/.evi/uploads/<session>/ and returns the local path the agent can
        read with read_file."""
        if not session_id.strip() or not file.filename:
            raise HTTPException(400, "session_id and file are required")
        # Sanitise filename — strip directory components.
        safe_name = Path(file.filename).name
        if not safe_name or safe_name in (".", ".."):
            raise HTTPException(400, "invalid filename")

        target_dir = _session_upload_dir(session_id)
        target = target_dir / safe_name
        if not _within(target, target_dir):
            raise HTTPException(400, "invalid filename")
        target_dir.mkdir(parents=True, exist_ok=True)

        # Stream to disk in chunks; capped to refuse mistakes.
        written = await _save_upload(file, target)

        return {
            "ok": True,
            "path": str(target),
            "filename": safe_name,
            "size": written,
        }

    @app.get("/images/{name:path}")
    def image(name: str) -> FileResponse:
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(400, "invalid image name")
        path = IMAGE_DIR / name
        if not _within(path, IMAGE_DIR):  # e.g. a drive-relative `D:x.png` on Windows
            raise HTTPException(400, "invalid image name")
        if not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path)

    @app.get("/uploads/{session_id}/{name}")
    def uploaded_file(session_id: str, name: str) -> FileResponse:
        # Serve a user-attached upload so the browser can re-render its preview
        # after a history rebuild (tab switch) — the attach-time thumbnail is an
        # ephemeral blob otherwise lost. The unguessable session UUID in the path
        # is the capability, same idea as /images's random names.
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(400, "invalid path")
        udir = _session_upload_dir(session_id)
        path = udir / name
        if not _within(path, udir):  # e.g. a drive-relative `D:x.png` on Windows
            raise HTTPException(400, "invalid path")
        if not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path)

    if STATIC_DIR.exists():
        # Same revalidate-on-every-load policy as the index route, so a sidecar
        # UI update reaches the WebView instead of hiding behind a stale cache.
        class _NoCacheStatic(StaticFiles):
            async def get_response(self, path: str, scope: Any):
                resp = await super().get_response(path, scope)
                resp.headers["Cache-Control"] = "no-cache"
                return resp

        app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")

    # Outermost (added last): every response, refusals included, gets nosniff,
    # the page its CSP and no-framing headers, uploads and images the sandbox.
    app.add_middleware(_SecurityHeadersMiddleware)
    # The 500 for an unhandled exception is sent from outside it: the same
    # headers go on that one here.
    app.add_exception_handler(Exception, _internal_error)
    app.add_exception_handler(ConfigUnreadable, _config_unreadable)
    return app


app = create_app()
