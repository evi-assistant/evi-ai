"""Evi web frontend — FastAPI + Server-Sent Events.

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
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from evi.backends import get_backend
from evi.commands import CommandStore
from evi.config import IMAGE_DIR, UPLOADS_DIR, Config, ensure_dirs
from evi.llm.agent import Agent, Done, Error, Event
from evi.llm.client import make_client
from evi.mcp import MCPManager, load_servers
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


class DecisionRequest(BaseModel):
    session_id: str
    decision_id: str
    approved: bool


class BackendActionRequest(BaseModel):
    """POST body for /api/backend/start and /api/backend/open-download."""

    kind: str  # "ollama" | "lmstudio" | "llamacpp"


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

    mcp_manager: MCPManager | None = None
    scheduler_obj: object | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal mcp_manager, scheduler_obj
        cfg = Config.load()
        if cfg.tools.mcp:
            servers = load_servers()
            if servers:
                try:
                    mcp_manager = MCPManager(servers)
                    mcp_manager.start()
                except ImportError:
                    logger.warning(
                        "MCP enabled but `mcp` package not installed; "
                        "run `pip install 'evi-ai[mcp]'`"
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

    app = FastAPI(title="Evi", lifespan=lifespan)

    sessions: dict[str, WebSession] = {}
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
        token = Config.load().web.auth_token.strip()
        if not token:
            return await call_next(request)
        path = request.url.path
        if (
            path in _PUBLIC_PATHS
            or path.startswith("/static/")
            or path.startswith("/images/")
        ):
            return await call_next(request)

        header = request.headers.get("Authorization", "")
        provided = ""
        if header.lower().startswith("bearer "):
            provided = header[7:].strip()
        if not provided:
            provided = request.query_params.get("token", "")
        # Constant-time compare so an attacker can't time character mismatches.
        if provided and secrets.compare_digest(provided, token):
            return await call_next(request)
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/api/auth/check")
    def auth_check(request: Request) -> dict[str, object]:
        """Validate the caller's token. Returns `{ok, required}`.

        The login overlay calls this with the user-supplied token in the
        `Authorization` header. If auth is disabled (`auth_token=""`),
        `required=false` and the overlay never shows.
        """
        token = Config.load().web.auth_token.strip()
        if not token:
            return {"ok": True, "required": False}
        header = request.headers.get("Authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not provided:
            provided = request.query_params.get("token", "")
        ok = bool(provided) and secrets.compare_digest(provided, token)
        return {"ok": ok, "required": True}

    def _make_permission_callback(session_id: str, loop: asyncio.AbstractEventLoop,
                                  enqueue):
        """Build a permission_callback that bridges worker thread → SSE client.

        The callback (invoked on a worker thread inside Agent.chat) generates
        a decision_id, pushes a PermissionRequest into the SSE queue via
        `enqueue`, and blocks on a threading.Event until /api/decide flips it.
        """
        def callback(tool_name: str, args_json: str, category: str) -> bool:
            decision_id = secrets.token_hex(8)
            sess = sessions.get(session_id)
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
        sess = sessions.get(session_id)
        if sess is None:
            config = Config.load()
            client = make_client(config.llm)
            toggles = asdict(config.tools)
            tools = get_enabled_tools(toggles)
            memory = MemoryStore() if toggles.get("memory") else None
            skills = SkillStore() if toggles.get("skills") else None
            from evi.guardrails import Guardrails

            guardrails = Guardrails.load()
            agent = Agent(
                client=client,
                config=config,
                tools=tools,
                memory=memory,
                skills=skills,
                guardrails=guardrails if guardrails.enabled else None,
                # permission_callback gets attached per-request so it sees
                # the right SSE enqueue function.
            )
            sess = WebSession(agent=agent)
            sessions[session_id] = sess
        return sess

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        cfg = Config.load()
        return {
            "ok": True,
            "model": cfg.llm.model,
            "sessions": len(sessions),
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

    @app.get("/api/session/{session_id}/usage")
    def session_usage(session_id: str) -> dict[str, int]:
        """Return approximate token usage for the named session."""
        sess = sessions.get(session_id)
        if sess is None:
            return {"used": 0, "ceiling": 0}
        used, ceiling = sess.agent.token_usage()
        return {"used": used, "ceiling": ceiling}

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
        for sess in sessions.values():
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

    @app.post("/api/reset")
    def reset(req: ChatRequest) -> dict[str, str]:
        sess = sessions.get(req.session_id)
        if sess is not None:
            sess.agent.reset()
        return {"status": "ok"}

    @app.post("/api/decide")
    def decide(req: DecisionRequest) -> dict[str, object]:
        sess = sessions.get(req.session_id)
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
        """Return the session's full history so the browser can rebuild
        state after a reload."""
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        # Strip the bulky image_url data URLs from any multipart user content
        # so the response stays small. The agent still has them in memory.
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
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        return {"title": sess.agent.suggest_title()}

    @app.post("/api/session/{session_id}/truncate")
    def session_truncate(session_id: str, req: TruncateRequest) -> dict[str, object]:
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        removed = sess.agent.truncate_history(req.after_index)
        return {"ok": True, "removed": removed, "length": len(sess.agent.history)}

    @app.post("/api/session/{session_id}/edit")
    def session_edit(session_id: str, req: EditRequest) -> dict[str, object]:
        sess = sessions.get(session_id)
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
        sess = sessions.get(session_id)
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
        sess = sessions.get(session_id)
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
            session_id, loop, enqueue,
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
            req.session_id, loop, enqueue,
        )

        def worker() -> None:
            try:
                for event in sess.agent.chat(
                    message,
                    images=req.images,
                    prediction=req.prediction,
                    parallel_tool_calls=req.parallel_tool_calls,
                    logit_bias=req.logit_bias,
                    audio=req.audio,
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
