"""Supervise a managed `llama-server` process (Phase 1).

One server per eVi process, started with a model and pinned to a free loopback
port. Not a full service manager — start / health-check / stop, with a model
swap = stop + start. Registered with `atexit` so it dies with eVi (no orphan).
"""

from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from evi.config import RUNTIME_DIR
from evi.portprobe import is_openai_server, port_open

_API_KEY = "llamacpp"  # llama-server ignores it unless started with --api-key
# -c when a caller doesn't size it. eVi's system prompt and tool schemas alone
# take a few thousand tokens, so this is the smallest window a turn fits in;
# setup sizes the real one per machine (catalog.context_budget, whose floor
# this matches).
DEFAULT_CTX = 8192


def free_port(start: int = 8080, span: int = 20) -> int:
    """First unused loopback port at/after `start` (llama.cpp's default is 8080)."""
    for p in range(start, start + span):
        if not port_open("127.0.0.1", p, timeout=0.2):
            return p
    return start


@dataclass
class ManagedServer:
    server_bin: Path
    model_path: Path
    port: int
    ngl: int = 0  # 0 = CPU (Phase 1); GPU offload is Phase 3
    # Context window (-c). Setup writes the same number into llm.context_size,
    # so the agent compacts before the server would reject the prompt.
    ctx: int = DEFAULT_CTX
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log_path: Path | None = None

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def is_running(self) -> bool:
        return (
            self.proc is not None
            and self.proc.poll() is None
            and is_openai_server(self.base_url(), api_key=_API_KEY)
        )

    def command(self) -> list[str]:
        """The llama-server argv for this model, port, offload and window."""
        return [
            str(self.server_bin),
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", str(self.ctx),
            "-ngl", str(self.ngl),
        ]

    def start(self, *, ready_timeout: float = 120.0) -> None:
        (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path = RUNTIME_DIR / "logs" / "llama-server.log"
        cmd = self.command()
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x0800_0000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        # Reap-on-parent-death: even if eVi is hard-killed (no atexit), the OS
        # kills this child too (Windows Job Object). Best-effort, never fatal.
        try:
            from evi.runtime import reap

            reap.assign_to_parent_lifetime(self.proc.pid)
        except Exception:  # noqa: BLE001
            pass

        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server exited early (code {self.proc.returncode}); "
                    f"see {self.log_path}."
                )
            if is_openai_server(self.base_url(), api_key=_API_KEY):
                return
            time.sleep(0.5)
        raise RuntimeError(f"llama-server didn't become ready within {int(ready_timeout)}s.")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def swap(self, model_path: Path, *, ngl: int | None = None, ctx: int | None = None) -> None:
        """Load a different model: stop, then restart on the SAME port with a new
        ``-m`` (and ``-c`` when given — each model gets its own window).
        (llama-server is one-model-per-process, so a switch is a restart.)"""
        self.stop()
        self.model_path = Path(model_path)
        if ngl is not None:
            self.ngl = ngl
        if ctx:
            self.ctx = int(ctx)
        self.start()


_MANAGED: ManagedServer | None = None
_LOCK = threading.Lock()


def managed() -> ManagedServer | None:
    return _MANAGED


def ctx_for(base_url: str | None) -> int | None:
    """The managed server's window (-c) when ``base_url`` is its address, else
    None. Setup rewrites the saved ``llm.context_size`` on every start and swap,
    but an open session holds its own copy of the config: reading the window
    here keeps that session's compaction ceiling in step. No probe, so it's
    cheap enough to call once per turn."""
    srv = _MANAGED
    if srv is None or not base_url:
        return None
    if base_url.strip().rstrip("/").lower() != srv.base_url().rstrip("/").lower():
        return None
    return int(srv.ctx)


def start_managed(server_bin, model_path, *, port: int | None = None, ngl: int = 0,
                  ctx: int | None = None) -> ManagedServer:
    """Start (or reuse) the single managed server. Idempotent while it's alive —
    a reused server keeps its own ``ctx``, so read the window off the result."""
    global _MANAGED
    with _LOCK:
        if _MANAGED is not None and _MANAGED.is_running():
            return _MANAGED
        srv = ManagedServer(
            server_bin=Path(server_bin),
            model_path=Path(model_path),
            port=port or free_port(),
            ngl=ngl,
            ctx=int(ctx or DEFAULT_CTX),
        )
        srv.start()
        _MANAGED = srv
        return srv


def use_model(server_bin, model_path, *, ngl: int = 0, ctx: int | None = None) -> ManagedServer:
    """Start the managed server on ``model_path`` with window ``ctx`` — or, if
    one is already up, swap it (same port) to the new model and window. The
    single-server invariant holds."""
    global _MANAGED
    with _LOCK:
        if _MANAGED is not None and _MANAGED.proc is not None:
            _MANAGED.swap(Path(model_path), ngl=ngl, ctx=ctx)
            return _MANAGED
        srv = ManagedServer(
            server_bin=Path(server_bin), model_path=Path(model_path),
            port=free_port(), ngl=ngl, ctx=int(ctx or DEFAULT_CTX),
        )
        srv.start()
        _MANAGED = srv
        return srv


def stop_managed() -> None:
    global _MANAGED
    with _LOCK:
        if _MANAGED is not None:
            _MANAGED.stop()
            _MANAGED = None


atexit.register(stop_managed)
