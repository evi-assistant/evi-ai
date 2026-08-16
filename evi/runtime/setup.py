"""Managed-runtime orchestration: install the CPU llama.cpp runtime, download a
catalog model, start/swap the server, and point `[llm]` at it.

One pollable progress state drives BOTH first-run setup (`start`) and in-app
model switching (`use_model`), so the UI can show download/switch progress the
same way. Stdlib-only downloads (works in the frozen sidecar).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass

from evi.config import Config, ensure_dirs
from evi.runtime import catalog, llama_server, llamacpp_runtime


@dataclass
class _Progress:
    stage: str = "idle"  # idle | runtime | model | starting | done | error
    pct: int = 0
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


set_progress = _set  # public alias for other runtime modules


def _mb(n: int) -> int:
    return round(n / (1 << 20))


def _pct(done: int, total: int) -> int:
    return int(done * 100 / total) if total else 0


def starter_model_path():
    return catalog.model_path(catalog.starter())


def status() -> dict:
    with _lock:
        d = asdict(_state)
    srv = llama_server.managed()
    d["server_running"] = bool(srv and srv.is_running())
    d["runtime_installed"] = llamacpp_runtime.is_installed()
    d["supported"] = llamacpp_runtime.supported()
    # Which catalog model the running server actually loaded (robust across
    # restarts — derived from the server's -m path, not just config).
    active = None
    if srv and srv.model_path:
        for m in catalog.catalog():
            if catalog.model_path(m) == srv.model_path:
                active = m["id"]
                break
    d["active_model_id"] = active
    return d


def _install_and_run(entry: dict) -> None:
    try:
        ensure_dirs()
        _set(stage="runtime", pct=0, message="Downloading llama.cpp runtime…",
             running=True, done=False, error="")
        server_bin = llamacpp_runtime.ensure_runtime(
            on_bytes=lambda d, t: _set(pct=_pct(d, t), message=f"Runtime {_mb(d)}/{_mb(t)} MB")
        )
        if not catalog.is_installed(entry):
            _set(stage="model", pct=0, message=f"Downloading {entry['name']}…")
            catalog.download(
                entry,
                on_bytes=lambda d, t: _set(
                    pct=_pct(d, t), message=f"{entry['name']} {_mb(d)}/{_mb(t)} MB"),
            )
        _set(stage="starting", pct=0, message=f"Starting {entry['name']}…")
        srv = llama_server.use_model(server_bin, catalog.model_path(entry), ngl=0)
        cfg = Config.load()
        cfg.llm.backend = "llamacpp"
        cfg.llm.base_url = srv.base_url()
        cfg.llm.model = entry["name"]
        cfg.save()
        _set(stage="done", pct=100, message=f"{entry['name']} is ready.",
             running=False, done=True)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        _set(stage="error", message=str(exc), running=False, done=False, error=str(exc))


def _kick(entry: dict, background: bool) -> dict:
    with _lock:
        if _state.running:
            return status()
    if background:
        threading.Thread(target=lambda: _install_and_run(entry), daemon=True).start()
    else:
        _install_and_run(entry)
    return status()


def start(*, background: bool = True) -> dict:
    """First-run: install runtime + the zero-config starter model + start."""
    return _kick(catalog.starter(), background)


def use_model(model_id: str, *, background: bool = True) -> dict:
    """Download (if needed) + switch the managed server to a catalog model."""
    entry = catalog.get(model_id)
    if entry is None:
        return {**status(), "error": f"unknown model: {model_id}"}
    return _kick(entry, background)


def ensure_running() -> bool:
    """Re-launch the managed server on the configured (or first-installed) model
    if `[llm]` is the managed runtime and nothing is answering. For startup —
    blocking (waits for load); call off the main thread."""
    from evi.portprobe import is_openai_server

    cfg = Config.load()
    if cfg.llm.backend != "llamacpp":
        return False
    if is_openai_server(cfg.llm.base_url, api_key="llamacpp"):
        return True
    server_bin = llamacpp_runtime.server_path()
    if server_bin is None:
        return False
    entry = next((m for m in catalog.catalog()
                  if m["name"] == cfg.llm.model and catalog.is_installed(m)), None)
    if entry is None:
        entry = next((m for m in catalog.catalog() if catalog.is_installed(m)), None)
    if entry is None:
        return False
    try:
        srv = llama_server.use_model(server_bin, catalog.model_path(entry), ngl=0)
    except Exception:  # noqa: BLE001 — best-effort autostart
        return False
    if cfg.llm.base_url != srv.base_url() or cfg.llm.model != entry["name"]:
        cfg.llm.base_url = srv.base_url()
        cfg.llm.model = entry["name"]
        cfg.save()
    return srv.is_running()
