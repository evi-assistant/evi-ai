"""eVi web frontend — FastAPI + Server-Sent Events.

One `WebSession` is held per session id; each owns an `Agent` plus a small
registry of pending permission decisions. The browser POSTs to `/api/chat`
with `{session_id, message}` and receives an SSE stream of JSON event
lines. Tool calls that aren't auto-approved surface as `PermissionRequest`
events that the browser answers via `/api/decide`.

Slash commands (`/help`, `/goal`, `/plan`, `/auto`, `/reset`, `/tools`,
`/model`) match CLI behavior and are dispatched server-side so the browser
just types and reads; no client-side command logic needed.

Generated images saved under `IMAGE_DIR` are served from `/images/{name}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import re
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from evi.backends import get_backend
from evi.commands import CommandStore
from evi.config import HOME, IMAGE_DIR, UPLOADS_DIR, Config, ensure_dirs
from evi.llm.agent import Agent, Done, Error, Event
from evi.llm.client import make_client
from evi.mcp import MCPManager, filter_allowed, load_servers
from evi.memory import MemoryStore
from evi.skills import SkillStore
from evi.tools.base import get_enabled_tools

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


def _probe_candidate(kind: str, base_url: str) -> tuple[bool, str]:
    """Return (reachable, resolved_url) for one backend.

    llama.cpp gets the 8080..8090 port scan so a busy default port doesn't
    hide it; everything else is a single-URL probe. The resolved URL lets the
    UI show where llama.cpp was actually found.
    """
    if kind == "llamacpp":
        found = discover_llamacpp_url(base_url)
        return (found is not None, found or base_url)
    return (_probe_backend(base_url), base_url)


class PickerUpdate(BaseModel):
    """Patch shape for POST /api/model-picker. All fields optional."""

    model: str | None = None
    fast_model: str | None = None
    effort: str | None = None
    fast_mode: bool | None = None


class TruncateRequest(BaseModel):
    after_index: int


class EditRequest(BaseModel):
    at_index: int
    content: str


class BranchRequest(BaseModel):
    at_index: int


class ModeRequest(BaseModel):
    mode: str  # chat | cowork | code


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
}
_SECRET_SENTINEL = "********"
# Sections that can be hot-applied to a live chat. Everything persists to
# disk regardless; these also push onto in-memory sessions so the change
# takes effect without starting a new chat.
_HOT_SECTIONS = (
    "llm", "auto", "tools", "telemetry", "comfy",
    "web", "google", "microsoft", "obsidian",
)

# Multi-user (opt-in): the authenticated user for the in-flight request, set by
# the auth middleware. None = single-user / open access (the default). Read in
# the request task to scope sessions + data dirs per user.
_CURRENT_USER: ContextVar[str | None] = ContextVar("evi_current_user", default=None)


def _safe_user(name: str) -> str:
    """Filesystem-safe slug for a user's data dir (blocks path traversal)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip()).strip("._-")
    return slug or "user"


def _config_snapshot(cfg: Config) -> dict[str, Any]:
    """asdict(cfg) with secrets masked for transport to the browser."""
    data = asdict(cfg)
    for section, secrets_ in _SECRET_FIELDS.items():
        body = data.get(section, {})
        for key in secrets_:
            if body.get(key):
                body[key] = _SECRET_SENTINEL
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


# ---- event serialization -------------------------------------------------


def _event_kind(event: Event) -> str:
    return type(event).__name__


def _serialize(event: Event) -> str:
    """Render an agent Event as a JSON line for SSE."""
    payload: dict[str, Any] = {"kind": _event_kind(event)}
    if is_dataclass(event):
        payload.update(asdict(event))
    return json.dumps(payload)


# ---- slash commands (server-side) ---------------------------------------


@dataclass
class _SlashOutcome:
    handled: bool                 # True = no LLM call needed; emit `text` + Done
    text: str = ""
    expand_to: str | None = None  # If set, replace the user's message with this


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
        return _SlashOutcome(handled=True, text="history cleared.")

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

    # Fall through to user-defined command.
    expanded = cmd_store.expand(name, args)
    if expanded is None:
        return _SlashOutcome(handled=True, text=f"unknown command: `/{name}` (try /help)")
    return _SlashOutcome(handled=False, expand_to=expanded)


# ---- app factory --------------------------------------------------------


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
        cfg = Config.load()
        if cfg.tools.mcp:
            servers = filter_allowed(load_servers(), cfg.tools.mcp_allow)
            if servers:
                try:
                    mcp_manager = MCPManager(servers)
                    mcp_manager.start()
                except ImportError:
                    logger.warning(
                        "MCP enabled but `mcp` package not installed; "
                        "run `pip install 'evi-assistant[mcp]'`"
                    )
                    mcp_manager = None

        try:
            from evi.scheduler import Scheduler

            scheduler_obj = Scheduler()
            scheduler_obj.start()
        except RuntimeError as exc:
            logger.info("scheduler not started: %s", exc)
            scheduler_obj = None
        except Exception as exc:
            logger.warning("scheduler failed to start: %s", exc)
            scheduler_obj = None

        try:
            yield
        finally:
            if scheduler_obj is not None:
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

    def _user_data_roots() -> tuple[Path | None, Path | None]:
        """(transcripts_root, memory_root) for the current user, else (None,
        None) → the shared defaults (single-user behaviour)."""
        u = _CURRENT_USER.get()
        if not u:
            return (None, None)
        base = HOME / "users" / _safe_user(u)
        return (base / "transcripts", base / "memory")

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
        {"/", "/api/health", "/api/auth/check", "/api/backend/status"}
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Config is cheap to re-read (small TOML); doing it per-request
        # means `evi web token rotate` takes effect without a restart.
        from evi.users import authenticate, load_users

        _CURRENT_USER.set(None)  # reset per request; set below when authenticated
        cfg = Config.load()
        token = cfg.web.auth_token.strip()
        users = load_users() if cfg.web.multi_user else []
        # Open access only when no auth is configured at all.
        if not token and not users:
            return await call_next(request)
        path = request.url.path
        if (
            path in _PUBLIC_PATHS
            or path.startswith("/static/")
            or path.startswith("/images/")
            # Routine webhooks authenticate via the unguessable path token
            # (external callers don't have the web auth token). Validated in
            # the handler via secrets.compare_digest.
            or path.startswith("/api/routine/")
        ):
            return await call_next(request)

        header = request.headers.get("Authorization", "")
        provided = ""
        if header.lower().startswith("bearer "):
            provided = header[7:].strip()
        if not provided:
            provided = request.query_params.get("token", "")

        # Multi-user: any user token (or the owner's auth_token) authenticates.
        if users:
            user = authenticate(provided, users)
            if user is not None:
                request.state.evi_user = user.name
                _CURRENT_USER.set(user.name)
                return await call_next(request)
            if token and provided and secrets.compare_digest(provided, token):
                request.state.evi_user = "owner"
                _CURRENT_USER.set("owner")
                return await call_next(request)
        # Single-user: the one shared token.
        elif provided and secrets.compare_digest(provided, token):
            return await call_next(request)
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
        from evi.users import authenticate, load_users

        cfg = Config.load()
        token = cfg.web.auth_token.strip()
        users = load_users() if cfg.web.multi_user else []
        if not token and not users:
            return {"ok": True, "required": False}
        header = request.headers.get("Authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not provided:
            provided = request.query_params.get("token", "")
        ok = bool(provided) and secrets.compare_digest(provided, token)
        user = authenticate(provided, users) if users else None
        if user is not None:
            ok = True
        result: dict[str, object] = {"ok": ok, "required": True}
        if user is not None:
            result["user"] = user.name
        return result

    def _make_permission_callback(bucket: dict, session_id: str,
                                  loop: asyncio.AbstractEventLoop, enqueue):
        """Build a permission_callback that bridges worker thread → SSE client.

        The callback (invoked on a worker thread inside Agent.chat) generates
        a decision_id, pushes a PermissionRequest into the SSE queue via
        `enqueue`, and blocks on a threading.Event until /api/decide flips it.

        `bucket` is captured here (in the request task) so the worker-thread
        lookup resolves the right user's session without the ContextVar.
        """
        def callback(tool_name: str, args_json: str, category: str) -> bool:
            decision_id = secrets.token_hex(8)
            sess = bucket.get(session_id)
            if sess is None:
                return False
            pending = PendingDecision(event=threading.Event())
            sess.pending[decision_id] = pending
            enqueue({
                "kind": "PermissionRequest",
                "decision_id": decision_id,
                "tool_name": tool_name,
                "args": args_json,
                "category": category,
            })
            # Block forever — the worker thread is dedicated to this turn.
            pending.event.wait()
            sess.pending.pop(decision_id, None)
            return pending.approved
        return callback

    def get_session(session_id: str) -> WebSession:
        bucket = _my_sessions()
        sess = bucket.get(session_id)
        if sess is None:
            config = Config.load()
            client = make_client(config.llm)
            toggles = asdict(config.tools)
            tools = get_enabled_tools(toggles)
            # Per-user data dirs in multi-user mode (None → shared defaults).
            tr_root, mem_root = _user_data_roots()
            memory = MemoryStore(root=mem_root) if toggles.get("memory") else None
            skills = SkillStore() if toggles.get("skills") else None
            from evi.guardrails import Guardrails
            from evi.transcripts import TranscriptStore

            guardrails = Guardrails.load()
            agent = Agent(
                client=client,
                config=config,
                tools=tools,
                memory=memory,
                skills=skills,
                guardrails=guardrails if guardrails.enabled else None,
                # Persist per-turn so a chat survives a server/app restart, and
                # write under the CLIENT's session id so the transcript file
                # matches the tab. permission_callback is attached per-request.
                transcripts=TranscriptStore(root=tr_root) if toggles.get("transcripts") else None,
                session_id=session_id,
            )
            # Revive history from disk if this session was seen before (e.g. the
            # desktop app was closed + reopened). The composed system prompt at
            # index 0 stays; everything after is rebuilt from the transcript.
            # Scoped to the user's own transcript dir so revival can't cross users.
            try:
                from evi.sessions import find_session, history_from_transcript

                path = find_session(session_id, root=tr_root)
                if path is not None:
                    restored = history_from_transcript(path)
                    if restored:
                        agent.history = [agent.history[0], *restored]
            except Exception:  # noqa: BLE001
                pass
            sess = WebSession(agent=agent)
            bucket[session_id] = sess
        return sess

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        from evi import __version__
        cfg = Config.load()
        return {
            "ok": True,
            "version": __version__,
            "model": cfg.llm.model,
            "sessions": sum(len(b) for b in _user_sessions.values()),
        }

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
        # Probe the configured backend + all known backends at once so the
        # total latency is the slowest single probe, not their sum. Each entry
        # is (kind, url); llama.cpp's probe scans 8080..8090.
        probes: list[tuple[str, str]] = [(cfg.llm.backend, cfg.llm.base_url)]
        probes += list(_KNOWN_BACKENDS)
        with ThreadPoolExecutor(max_workers=len(probes)) as ex:
            results = list(ex.map(lambda kv: _probe_candidate(*kv), probes))

        configured = {
            "backend": cfg.llm.backend,
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "reachable": results[0][0],
        }
        candidates = [
            {"kind": kind, "url": resolved, "reachable": ok}
            for (kind, _url), (ok, resolved) in zip(_KNOWN_BACKENDS, results[1:])
        ]
        any_reachable = configured["reachable"] or any(c["reachable"] for c in candidates)
        data: dict[str, object] = {
            "configured": configured,
            "candidates": candidates,
            "ollama_installed": shutil.which("ollama") is not None,
            "any_reachable": any_reachable,
        }
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
        Idempotent: pulling an already-present model just re-verifies quickly."""
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
        if kind not in {"lmstudio", "ollama", "llamacpp", "openai_compat"}:
            raise HTTPException(400, f"unknown backend {kind!r}")
        base_url = (req.base_url or default_base_url(kind)).strip()

        cfg = Config.load()
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

        # Apply to every live session (rebuild the client for the new backend).
        for sess in _all_sessions():
            sess.agent.config.llm.backend = cfg.llm.backend
            sess.agent.config.llm.base_url = cfg.llm.base_url
            sess.agent.config.llm.api_key = cfg.llm.api_key
            sess.agent.config.llm.model = cfg.llm.model
            try:
                sess.agent.client = make_client(cfg.llm)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "backend": cfg.llm.backend,
                "base_url": cfg.llm.base_url, "model": cfg.llm.model}

    @app.get("/api/session/{session_id}/usage")
    def session_usage(session_id: str) -> dict[str, int]:
        """Return approximate token usage for the named session."""
        sess = _my_sessions().get(session_id)
        if sess is None:
            return {"used": 0, "ceiling": 0}
        used, ceiling = sess.agent.token_usage()
        return {"used": used, "ceiling": ceiling}

    @app.get("/api/session/{session_id}/context")
    def session_context(session_id: str) -> dict[str, object]:
        """Per-category breakdown of where the context window is spent (Ph 88)."""
        from evi.context_report import context_breakdown

        sess = _my_sessions().get(session_id)
        if sess is None:
            return context_breakdown([], 0)
        return context_breakdown(
            sess.agent.history, sess.agent.config.llm.context_size or 0
        )

    @app.post("/api/session/{session_id}/channel")
    def push_channel(session_id: str, req: dict[str, Any]) -> dict[str, Any]:
        """Push an external alert/notification into a (live or revived) session
        (Phase 83). The text is added as a system note so the assistant sees it
        on its next turn; an external sender (webhook, script) authenticates with
        the normal web token. Live-session context — not persisted across reloads.
        """
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        text = str(req.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        source = (str(req.get("source") or "channel").strip() or "channel")[:64]
        sess = get_session(session_id)
        sess.agent.history.append(
            {"role": "system", "content": f"[channel:{source}] {text}"}
        )
        sess.channel_log.append({"source": source, "text": text})
        return {"ok": True, "source": source, "pending": len(sess.channel_log)}

    @app.get("/api/session/{session_id}/channel")
    def list_channel(session_id: str) -> dict[str, Any]:
        """Recent channel messages pushed into this session (for a UI badge)."""
        sess = _my_sessions().get(session_id)
        return {"messages": sess.channel_log if sess is not None else []}

    @app.post("/api/session/{session_id}/handoff")
    def handoff_session(session_id: str, request: Request) -> dict[str, Any]:
        """Hand a session off to another device (Phase 87).

        Transcripts are written per-turn, so the on-disk copy is current as of
        the last completed turn; this returns the resume affordances (a
        `/?session=<id>` URL the web UI opens, and the `evi sessions resume`
        command). `evi sync` the ~/.evi state, then open either on the other
        device. 404 if the session has no transcript yet (send a turn first,
        with transcripts enabled).
        """
        from evi import sessions as _sessions

        info = _sessions.handoff_info(
            session_id, base_url=str(request.base_url)
        )
        if info is None:
            raise HTTPException(
                404,
                "session not persisted yet — send a message first "
                "(tools.transcripts must be on)",
            )
        return {"ok": True, **info}

    @app.get("/api/dispatch")
    def dispatch_snapshot() -> dict[str, Any]:
        """Dashboard data (Phase 85): every live session + its state, plus the
        workflows you can launch. Powers the dispatch view for managing many
        concurrent sessions at once."""
        from evi import workflows as _wf

        sess_list = []
        for sid, s in _my_sessions().items():
            try:
                used, ceiling = s.agent.token_usage()
            except Exception:
                used, ceiling = 0, 0
            sess_list.append(
                {
                    "id": sid,
                    "mode": s.mode,
                    "messages": len(getattr(s.agent, "history", [])),
                    "used": used,
                    "ceiling": ceiling,
                    "pending": len(s.pending),
                    "channels": len(s.channel_log),
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
        return {"sessions": sess_list, "workflows": wfs}

    @app.post("/api/dispatch/workflow/{name}")
    def dispatch_run_workflow(name: str, req: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a workflow headless, server-side (each step its own auto-approved
        agent; parallel blocks concurrent). Returns {step_id: output}."""
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
            cfg = Config.load()
            toggles = asdict(cfg.tools)
            agent = Agent(
                client=make_client(cfg.llm),
                config=cfg,
                tools=get_enabled_tools(toggles),
                memory=MemoryStore() if toggles.get("memory") else None,
                skills=SkillStore() if toggles.get("skills") else None,
            )
            if step.mode:
                agent.tools = {t.name: t for t in mode_tools(step.mode)}
            agent.enable_auto_all()
            res = run_headless(agent, prompt)
            return res.text or (f"ERROR: {res.error}" if res.error else "")

        try:
            outputs = _wf.run_workflow(wf, run_step=run_step, variables=variables)
        except _wf.WorkflowError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "outputs": outputs}

    @app.post("/api/federate")
    def federate(req: dict[str, Any]) -> dict[str, Any]:
        """Run a task delegated by a trusted peer eVi (federation). Off unless
        `[federation] serve = true`. Runs non-interactively — tools not already
        auto-approved are denied, so a remote task can't trigger surprises."""
        cfg = Config.load()
        if not cfg.federation.serve:
            raise HTTPException(
                403, "federation serving is disabled (set [federation] serve = true)"
            )
        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        task = str(req.get("task") or "").strip()
        if not task:
            raise HTTPException(400, "task is required")

        from evi.headless import run_headless

        toggles = asdict(cfg.tools)
        agent = Agent(
            client=make_client(cfg.llm),
            config=cfg,
            tools=get_enabled_tools(toggles),
            memory=MemoryStore() if toggles.get("memory") else None,
            skills=SkillStore() if toggles.get("skills") else None,
        )
        mode = str(req.get("mode") or "")
        if mode:
            from evi.modes import mode_tools

            agent.tools = {t.name: t for t in mode_tools(mode)}
        # Non-interactive: deny tools not already in the auto-approve list.
        agent.permission_callback = lambda *a, **k: False
        agent.permission_batch_callback = None
        res = run_headless(agent, task)
        return {"text": res.text, "error": res.error}

    @app.get("/api/model-picker")
    def model_picker_get() -> dict[str, object]:
        """Snapshot for the picker UI: available models + current settings."""
        cfg = Config.load()
        backend = get_backend(cfg.llm)
        try:
            models = [m.id for m in backend.list_models()]
        except Exception:
            models = []
        # Always include the currently-active model so the picker can show
        # a non-empty list even when the backend isn't reachable.
        active = cfg.llm.model
        if active and active not in models:
            models = [active, *models]
        return {
            "active": active,
            "fast_model": cfg.llm.fast_model,
            "models": models,
            "effort": (cfg.llm.reasoning_effort or "medium").lower(),
            "effort_levels": ["low", "medium", "high", "max"],
            "fast_mode": bool(cfg.llm.fast_mode),
            "backend": cfg.llm.backend,
        }

    @app.post("/api/model-picker")
    def model_picker_set(req: PickerUpdate) -> dict[str, object]:
        """Apply any subset of {model, fast_model, effort, fast_mode}.

        Persists to config.toml AND nudges every in-memory session's agent
        so the change takes effect on the next turn without a restart.
        """
        cfg = Config.load()
        if req.model is not None:
            cfg.llm.model = req.model.strip()
        if req.fast_model is not None:
            cfg.llm.fast_model = req.fast_model.strip()
        if req.effort is not None:
            level = req.effort.strip().lower()
            if level not in ("low", "medium", "high", "max"):
                raise HTTPException(400, f"invalid effort {level!r}")
            cfg.llm.reasoning_effort = level
        if req.fast_mode is not None:
            cfg.llm.fast_mode = bool(req.fast_mode)
        cfg.save()
        # Push into every live session so the current chat reflects the new
        # settings on the next turn.
        for sess in _all_sessions():
            sess.agent.config.llm.model = cfg.llm.model
            sess.agent.config.llm.fast_model = cfg.llm.fast_model
            sess.agent.config.llm.reasoning_effort = cfg.llm.reasoning_effort
            sess.agent.config.llm.fast_mode = cfg.llm.fast_mode
        return {
            "ok": True,
            "active": cfg.llm.model,
            "fast_model": cfg.llm.fast_model,
            "effort": cfg.llm.reasoning_effort,
            "fast_mode": cfg.llm.fast_mode,
        }

    @app.get("/api/config")
    def config_get() -> dict[str, Any]:
        """Full config snapshot for the settings screen (secrets masked)."""
        return _config_snapshot(Config.load())

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
        from evi import hooks as hooks_mod

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
        loader, which skips bad rows)."""
        from evi import hooks as hooks_mod

        if not isinstance(req, dict) or "raw" not in req:
            raise HTTPException(400, "expected {raw: <toml>}")
        raw = str(req["raw"])
        err = hooks_mod.validate(raw)
        if err:
            raise HTTPException(400, err)
        hooks_mod.write_raw(raw)
        registry = hooks_mod.load_hooks()
        return {"ok": True, "count": len(registry.hooks)}

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
        return {"name": case.name, "prompt": case.prompt, "checks": checks,
                "mode": case.mode}

    @app.get("/api/evals")
    def evals_list() -> dict[str, Any]:
        """List eval suites with their cases (read-only; no model calls)."""
        from evi import evals

        return {"suites": [
            {"name": s.name, "description": s.description,
             "cases": [_case_summary(c) for c in s.cases]}
            for s in evals.list_suites()
        ]}

    @app.post("/api/evals/run")
    def evals_run(req: dict[str, Any]) -> dict[str, Any]:
        """Run an eval suite server-side and return the report. This calls the
        model once per case (plus once per judged case), so it blocks until the
        whole suite finishes — fine for the small local suites evals are meant
        to be."""
        from evi import evals

        if not isinstance(req, dict):
            raise HTTPException(400, "expected an object body")
        name = str(req.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "expected {name}")
        try:
            suite = evals.load_suite(name)
        except evals.EvalError as exc:
            raise HTTPException(404, str(exc))

        def agent_factory():
            cfg = Config.load()
            toggles = asdict(cfg.tools)
            return Agent(
                client=make_client(cfg.llm),
                config=cfg,
                tools=get_enabled_tools(toggles),
                memory=MemoryStore() if toggles.get("memory") else None,
                skills=SkillStore() if toggles.get("skills") else None,
            )

        run_one, judge_fn = evals.make_runners(
            agent_factory, default_mode=str(req.get("mode") or "")
        )
        return evals.run_eval(suite, run_one, judge_fn=judge_fn)

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
        step). Returns [{label, prompt, text, error}, …]."""
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
        cfg = Config.load()
        toggles = asdict(cfg.tools)
        agent = Agent(
            client=make_client(cfg.llm),
            config=cfg,
            tools=get_enabled_tools(toggles),
            memory=MemoryStore() if toggles.get("memory") else None,
            skills=SkillStore() if toggles.get("skills") else None,
        )
        agent.enable_auto_all()
        results = recipes.run_recipe_headless(agent, recipe)
        return {"ok": True, "name": recipe.name, "steps": results}

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

        def agent_factory(system_prompt: str | None):
            cfg = Config.load()
            toggles = asdict(cfg.tools)
            kwargs: dict[str, Any] = dict(
                client=make_client(cfg.llm),
                config=cfg,
                tools=get_enabled_tools(toggles),
                memory=MemoryStore() if toggles.get("memory") else None,
                skills=SkillStore() if toggles.get("skills") else None,
            )
            if system_prompt is not None:
                kwargs["system_prompt"] = system_prompt
            return Agent(**kwargs)

        cfg_obj = Config.load()
        ucfg = uc.load_ultra_config(cfg_obj)
        if req.get("breadth"):
            ucfg.breadth = int(req["breadth"])
        if req.get("rounds") is not None:
            ucfg.rounds = int(req["rounds"])
        if req.get("mode"):
            ucfg.mode = str(req["mode"])
        if cfg_obj.ultracode.auto_tune:
            ucfg = uc.default_tuning(cfg_obj.llm.model, cfg_obj.llm.context_size, ucfg)
        res = uc.run_ultracode(task, run_one=uc.make_runner(agent_factory), cfg=ucfg)
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
        return {
            "peers": [
                {"name": p.name, "url": p.url, "has_token": bool(p.token), **st}
                for p, st in zip(peers, statuses)
            ],
            "serving": Config.load().federation.serve,
        }

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

        cfg = Config.load()
        toggles = asdict(cfg.tools)
        agent = Agent(
            client=make_client(cfg.llm),
            config=cfg,
            tools=get_enabled_tools(toggles),
            memory=MemoryStore() if toggles.get("memory") else None,
            skills=SkillStore() if toggles.get("skills") else None,
        )
        if r.yes:
            agent.enable_auto_all()
        else:
            # Non-interactive: deny non-auto-approved tools rather than block.
            agent.permission_callback = lambda *a, **k: False
            agent.permission_batch_callback = None
        results = recipes.run_recipe_headless(agent, recipe)
        return {"ok": True, "routine": r.name, "results": results}

    @app.post("/api/reset")
    def reset(req: ChatRequest) -> dict[str, str]:
        sess = _my_sessions().get(req.session_id)
        if sess is not None:
            sess.agent.reset()
        return {"status": "ok"}

    @app.post("/api/decide")
    def decide(req: DecisionRequest) -> dict[str, object]:
        sess = _my_sessions().get(req.session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        pending = sess.pending.get(req.decision_id)
        if pending is None:
            return {"ok": False, "reason": "no such decision"}
        pending.approved = req.approved
        pending.event.set()
        return {"ok": True}

    # --- session history manipulation (edit / re-roll / branch) --------

    @app.get("/api/session/{session_id}/history")
    def session_history(session_id: str) -> dict[str, object]:
        """Return the session's history so the browser can rebuild state after a
        reload. Uses get_session so a session that's only on disk (e.g. the
        desktop app was reopened) is revived from its transcript rather than
        404'ing into a blank chat."""
        sess = get_session(session_id)
        # Strip the bulky image_url data URLs from any multipart user content
        # so the response stays small. The agent still has them in memory.
        # NB: the system message at index 0 is kept so `index` stays aligned
        # with the agent's history (edit/branch use it); the client skips it.
        cleaned: list[dict] = []
        for i, msg in enumerate(sess.agent.history):
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            cleaned.append({
                "index": i,
                "role": msg.get("role"),
                "content": content,
                "tool_calls": msg.get("tool_calls"),
                "name": msg.get("name"),
            })
        return {"messages": cleaned}

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
        removed = sess.agent.truncate_history(req.after_index)
        return {"ok": True, "removed": removed, "length": len(sess.agent.history)}

    @app.post("/api/session/{session_id}/edit")
    def session_edit(session_id: str, req: EditRequest) -> dict[str, object]:
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
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
        return {"new_session_id": new_id, "length": len(snapshot)}

    @app.post("/api/session/{session_id}/reroll")
    async def session_reroll(session_id: str) -> EventSourceResponse:
        """Drop the last assistant turn (and any tool messages after the
        last user) and regenerate. SSE shape matches /api/chat."""
        sess = _my_sessions().get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        if not sess.agent.rewind_to_last_user():
            # Nothing to re-roll (no prior assistant turn).
            async def _noop() -> AsyncIterator[dict[str, str]]:
                yield {"event": "message", "data": json.dumps(
                    {"kind": "Error", "message": "nothing to re-roll"}
                )}
                yield {"event": "message", "data": json.dumps(
                    {"kind": "Done", "reason": "noop"}
                )}
            return EventSourceResponse(_noop())

        # Same machinery as /api/chat but invoke continue_chat (no new user).
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def enqueue(payload: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        sess.agent.permission_callback = _make_permission_callback(
            _my_sessions(), session_id, loop, enqueue,
        )

        def worker() -> None:
            try:
                for event in sess.agent.continue_chat():
                    payload = {"kind": _event_kind(event)}
                    if is_dataclass(event):
                        payload.update(asdict(event))
                    enqueue(payload)
                    if isinstance(event, (Done, Error)):
                        break
            except Exception as exc:  # noqa: BLE001
                enqueue({"kind": "Error", "message": f"{type(exc).__name__}: {exc}"})
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

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> EventSourceResponse:
        message = req.message.strip()
        if not message:
            raise HTTPException(400, "empty message")
        sess = get_session(req.session_id)

        # Server-side slash command dispatch — matches CLI semantics.
        if message.startswith("/"):
            outcome = _handle_slash(sess.agent, message, cmd_store)
            if outcome.handled:
                async def _ack() -> AsyncIterator[dict[str, str]]:
                    yield {"event": "message", "data": json.dumps(
                        {"kind": "SystemMessage", "text": outcome.text}
                    )}
                    yield {"event": "message", "data": json.dumps(
                        {"kind": "Done", "reason": "slash"}
                    )}
                return EventSourceResponse(_ack())
            if outcome.expand_to is not None:
                message = outcome.expand_to

        # Real LLM turn — run Agent.chat() in a worker thread so the async
        # SSE response stays responsive (and the permission_callback can
        # block on a threading.Event without freezing the event loop).
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def enqueue(payload: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        # Attach a session-scoped permission_callback for this turn.
        sess.agent.permission_callback = _make_permission_callback(
            _my_sessions(), req.session_id, loop, enqueue,
        )

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

        def worker() -> None:
            try:
                for event in sess.agent.chat(
                    message,
                    images=req.images,
                    prediction=req.prediction,
                    parallel_tool_calls=req.parallel_tool_calls,
                    logit_bias=req.logit_bias,
                    audio=req.audio,
                    response_format=response_format,
                ):
                    payload = {"kind": _event_kind(event)}
                    if is_dataclass(event):
                        payload.update(asdict(event))
                    enqueue(payload)
                    if isinstance(event, (Done, Error)):
                        break
            except Exception as exc:  # noqa: BLE001
                enqueue({"kind": "Error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                enqueue(None)  # sentinel

        threading.Thread(target=worker, daemon=True).start()

        async def stream() -> AsyncIterator[dict[str, str]]:
            while True:
                payload = await queue.get()
                if payload is None:
                    return
                yield {"event": "message", "data": json.dumps(payload)}

        return EventSourceResponse(stream())

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
        target_dir = UPLOADS_DIR / session_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        with target.open("wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
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

        target_dir = UPLOADS_DIR / session_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name

        # Stream to disk in chunks; cap at 32 MB to refuse mistakes.
        max_bytes = 32 * 1024 * 1024
        written = 0
        with target.open("wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, "upload exceeds 32 MB")
                out.write(chunk)

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
        if not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()
