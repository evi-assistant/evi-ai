"""Async-to-sync bridge — runs an asyncio event loop on a daemon thread.

The MCP Python SDK is all-async; Evi's tool layer is sync. Rather than wedge
async everywhere, we keep one long-lived loop running on a side thread and
let sync code submit coroutines via `run_coroutine_threadsafe`. The loop is
private to the bridge — never touch it directly from caller code.

The bridge is idempotent: `start()` is a no-op if already running, `stop()`
is a no-op if not. That keeps the lifecycle wiring (CLI atexit, FastAPI
lifespan) from caring about ordering corner cases.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine


class MCPBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("MCPBridge not started")
        return self._loop

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._ready.clear()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                # Drain pending tasks so we don't leak subprocesses on shutdown.
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()

        self._thread = threading.Thread(
            target=_run, name="evi-mcp-bridge", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        assert self._thread is not None
        self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float = 60.0) -> Any:
        """Submit a coroutine to the bridge loop and block until it returns.

        Raises whatever the coroutine raised, plus `concurrent.futures.TimeoutError`
        if it doesn't finish within `timeout` seconds.
        """
        if not self.is_running or self._loop is None:
            raise RuntimeError("MCPBridge not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)
