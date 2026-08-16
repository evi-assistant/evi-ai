"""Managed-runtime orchestration: install the llama.cpp runtime (CPU by default,
CUDA on request), download a catalog model, start/swap the server, and point
`[llm]` at it.

One pollable progress state drives first-run setup (`start`), model switching
(`use_model`), and the GPU upgrade (`enable_gpu`). Stdlib-only downloads (works
in the frozen sidecar).
"""

from __future__ import annotations

import platform
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


def _gpu_info() -> tuple[str | None, float, str | None]:
    """(compute_capability, vram_gb, name) of the best GPU, or (None, 0, None)."""
    try:
        from evi.hardware import detect

        hw = detect()
        if not hw.gpus:
            return None, 0.0, None
        g = max(hw.gpus, key=lambda x: x.vram_total_mb)
        return g.compute_capability, float(g.vram_total_mb or 0) / 1024.0, g.name
    except Exception:  # noqa: BLE001
        return None, 0.0, None


def _pick_runtime(entry: dict) -> tuple:
    """(server_bin, ngl) for this model. Prefer the managed GPU build when it's
    installed and the model fits VRAM (Windows CUDA), Metal on macOS; else CPU."""
    if platform.system().lower() == "darwin":
        return llamacpp_runtime.ensure_runtime(), 99  # base build is Metal
    _cc, vram, _name = _gpu_info()
    fits = bool(vram) and vram >= entry.get("min_vram_gb", 1e9)
    if fits and llamacpp_runtime.gpu_installed():
        return llamacpp_runtime.gpu_server_path(), 99
    return llamacpp_runtime.ensure_runtime(), 0


def _current_entry() -> dict | None:
    srv = llama_server.managed()
    if srv and srv.model_path:
        for m in catalog.catalog():
            if catalog.model_path(m) == srv.model_path:
                return m
    cfg = Config.load()
    return next((m for m in catalog.catalog() if m["name"] == cfg.llm.model), None)


def status() -> dict:
    with _lock:
        d = asdict(_state)
    srv = llama_server.managed()
    d["server_running"] = bool(srv and srv.is_running())
    d["runtime_installed"] = llamacpp_runtime.is_installed()
    d["supported"] = llamacpp_runtime.supported()
    # active model (derived from the running server's -m path — robust across restarts)
    active = None
    if srv and srv.model_path:
        for m in catalog.catalog():
            if catalog.model_path(m) == srv.model_path:
                active = m["id"]
                break
    d["active_model_id"] = active
    # GPU
    cc, vram, name = _gpu_info()
    on_mac = platform.system().lower() == "darwin"
    d["gpu_name"] = name or ("Apple GPU (Metal)" if on_mac else None)
    d["gpu_available"] = on_mac or bool(llamacpp_runtime.gpu_plan(compute_cap=cc))
    d["gpu_installed"] = on_mac or llamacpp_runtime.gpu_installed()
    d["on_gpu"] = bool(srv and getattr(srv, "ngl", 0) and srv.ngl > 0)
    return d


def _install_and_run(entry: dict) -> None:
    try:
        ensure_dirs()
        _set(stage="runtime", pct=0, message="Downloading llama.cpp runtime…",
             running=True, done=False, error="")
        # always ensure the CPU build (the universal fallback) is present
        llamacpp_runtime.ensure_runtime(
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
        server_bin, ngl = _pick_runtime(entry)  # GPU if already installed + fits
        srv = llama_server.use_model(server_bin, catalog.model_path(entry), ngl=ngl)
        cfg = Config.load()
        cfg.llm.backend = "llamacpp"
        cfg.llm.base_url = srv.base_url()
        cfg.llm.model = entry["name"]
        cfg.save()
        _set(stage="done", pct=100, message=f"{entry['name']} is ready.",
             running=False, done=True)
    except Exception as exc:  # noqa: BLE001
        _set(stage="error", message=str(exc), running=False, done=False, error=str(exc))


def _kick(fn, background: bool) -> dict:
    with _lock:
        if _state.running:
            return status()
    if background:
        threading.Thread(target=fn, daemon=True).start()
    else:
        fn()
    return status()


def start(*, background: bool = True) -> dict:
    """First-run: install runtime + the zero-config starter model + start."""
    entry = catalog.starter()
    return _kick(lambda: _install_and_run(entry), background)


def use_model(model_id: str, *, background: bool = True) -> dict:
    """Download (if needed) + switch the managed server to a catalog model."""
    entry = catalog.get(model_id)
    if entry is None:
        return {**status(), "error": f"unknown model: {model_id}"}
    return _kick(lambda: _install_and_run(entry), background)


def _enable_gpu_run() -> None:
    try:
        cc, _vram, _name = _gpu_info()
        on_mac = platform.system().lower() == "darwin"
        if not on_mac:
            plan = llamacpp_runtime.gpu_plan(compute_cap=cc)
            if not plan or plan.get("mode") != "cuda":
                raise RuntimeError("No supported GPU / prebuilt CUDA runtime for this machine.")
            _set(stage="runtime", pct=0, running=True, done=False, error="",
                 message=f"Downloading GPU runtime (CUDA {plan['build']})…")
            llamacpp_runtime.ensure_gpu_runtime(
                cc, on_bytes=lambda d, t: _set(pct=_pct(d, t), message=f"GPU runtime {_mb(d)}/{_mb(t)} MB"))
        entry = _current_entry() or catalog.starter()
        if not catalog.is_installed(entry):
            _set(stage="model", pct=0, message=f"Downloading {entry['name']}…")
            catalog.download(entry, on_bytes=lambda d, t: _set(
                pct=_pct(d, t), message=f"{entry['name']} {_mb(d)}/{_mb(t)} MB"))
        _set(stage="starting", pct=0, message=f"Starting {entry['name']} on your GPU…")
        server_bin, ngl = _pick_runtime(entry)
        srv = llama_server.use_model(server_bin, catalog.model_path(entry), ngl=ngl)
        cfg = Config.load()
        cfg.llm.backend = "llamacpp"
        cfg.llm.base_url = srv.base_url()
        cfg.llm.model = entry["name"]
        cfg.save()
        msg = (f"{entry['name']} is running on your GPU." if ngl
               else f"{entry['name']} started, but couldn't offload to the GPU — running on CPU.")
        _set(stage="done", pct=100, message=msg, running=False, done=True)
    except Exception as exc:  # noqa: BLE001
        _set(stage="error", message=str(exc), running=False, done=False, error=str(exc))


def enable_gpu(*, background: bool = True) -> dict:
    """Download the CUDA build (if needed) + re-run the current model on the GPU."""
    return _kick(_enable_gpu_run, background)


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
    if llamacpp_runtime.server_path() is None:
        return False
    entry = next((m for m in catalog.catalog()
                  if m["name"] == cfg.llm.model and catalog.is_installed(m)), None)
    if entry is None:
        entry = next((m for m in catalog.catalog() if catalog.is_installed(m)), None)
    if entry is None:
        return False
    try:
        server_bin, ngl = _pick_runtime(entry)
        srv = llama_server.use_model(server_bin, catalog.model_path(entry), ngl=ngl)
    except Exception:  # noqa: BLE001
        return False
    if cfg.llm.base_url != srv.base_url() or cfg.llm.model != entry["name"]:
        cfg.llm.base_url = srv.base_url()
        cfg.llm.model = entry["name"]
        cfg.save()
    return srv.is_running()
