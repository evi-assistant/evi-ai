"""One-click managed-runtime setup (Phase 1): download the CPU llama.cpp runtime,
download a small starter model, start the server, and point `[llm]` at it.

Runs in a background thread with a pollable progress state, so the web/desktop
"no backend" banner can drive it and show progress. Stdlib-only downloads.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from evi.config import MODELS_DIR, Config, ensure_dirs
from evi.runtime import llama_server, llamacpp_runtime

# A small, good-for-size default that runs on any CPU. Bigger models arrive with
# the Phase-2 catalog. A direct HF `resolve` URL keeps this dependency-free.
STARTER_REPO = "bartowski/Qwen2.5-1.5B-Instruct-GGUF"
STARTER_FILE = "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
STARTER_DISPLAY = "Qwen2.5-1.5B-Instruct"
_HF_BASE = "https://huggingface.co"


@dataclass
class _Progress:
    stage: str = "idle"  # idle | runtime | model | starting | done | error
    pct: int = 0  # 0..100 within the current stage
    message: str = ""
    running: bool = False
    done: bool = False
    error: str = ""


_state = _Progress()
_lock = threading.Lock()


def _set(**kw) -> None:
    with _lock:
        for k, v in kw.items():
            setattr(_state, k, v)


def _mb(n: int) -> int:
    return round(n / (1 << 20))


def _pct(done: int, total: int) -> int:
    return int(done * 100 / total) if total else 0


def starter_model_path() -> Path:
    return MODELS_DIR / STARTER_REPO.replace("/", "__") / STARTER_FILE


def status() -> dict:
    with _lock:
        d = asdict(_state)
    srv = llama_server.managed()
    d["server_running"] = bool(srv and srv.is_running())
    d["runtime_installed"] = llamacpp_runtime.is_installed()
    d["model_present"] = starter_model_path().is_file()
    d["supported"] = llamacpp_runtime.supported()
    return d


def _run() -> None:
    try:
        ensure_dirs()

        # 1. runtime binary
        _set(stage="runtime", pct=0, message="Downloading llama.cpp runtime…",
             running=True, done=False, error="")
        server_bin = llamacpp_runtime.ensure_runtime(
            on_bytes=lambda d, t: _set(pct=_pct(d, t),
                                       message=f"Runtime {_mb(d)}/{_mb(t)} MB")
        )

        # 2. starter model
        model_path = starter_model_path()
        if not model_path.is_file():
            _set(stage="model", pct=0, message="Downloading starter model…")
            url = f"{_HF_BASE}/{STARTER_REPO}/resolve/main/{STARTER_FILE}"
            llamacpp_runtime.download_file(
                url, model_path,
                on_bytes=lambda d, t: _set(pct=_pct(d, t),
                                           message=f"Model {_mb(d)}/{_mb(t)} MB"),
            )

        # 3. start the server
        _set(stage="starting", pct=0, message="Starting your local model…")
        srv = llama_server.start_managed(server_bin, model_path, ngl=0)

        # 4. point eVi at it
        cfg = Config.load()
        cfg.llm.backend = "llamacpp"
        cfg.llm.base_url = srv.base_url()
        cfg.llm.model = STARTER_DISPLAY
        cfg.save()

        _set(stage="done", pct=100, message="Local AI is ready.",
             running=False, done=True)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        _set(stage="error", message=str(exc), running=False, done=False, error=str(exc))


def start(*, background: bool = True) -> dict:
    """Kick off setup. Idempotent while running; returns the current status."""
    with _lock:
        if _state.running:
            return status()
    if background:
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()
    return status()


def ensure_running() -> bool:
    """If `[llm]` uses the managed llama.cpp runtime and it's installed but no
    server answers, start it — so the managed model persists across app restarts.

    A no-op unless: backend is `llamacpp`, the configured URL isn't already
    answering (don't hijack a server the user runs themselves), and the runtime +
    starter model are both present. Blocking (waits for model load); call it off
    the main thread at startup. Returns whether a server is running afterward.
    """
    from evi.portprobe import is_openai_server

    cfg = Config.load()
    if cfg.llm.backend != "llamacpp":
        return False
    if is_openai_server(cfg.llm.base_url, api_key="llamacpp"):
        return True  # theirs or ours already answers
    server_bin = llamacpp_runtime.server_path()
    model_path = starter_model_path()
    if server_bin is None or not model_path.is_file():
        return False
    try:
        srv = llama_server.start_managed(server_bin, model_path, ngl=0)
    except Exception:  # noqa: BLE001 — best-effort autostart
        return False
    if cfg.llm.base_url != srv.base_url():
        cfg.llm.base_url = srv.base_url()
        cfg.save()
    return srv.is_running()
