"""Helpers for tests that drive the real web app with a real Agent.

`web_app` builds the FastAPI app on a tmp home serving ONE given Agent for
every session id (its model is a scripted client, `Scripted`), and `serving`
runs an app on a real uvicorn server in a thread: TestClient runs one
streamed response to completion before the next request (see the note in
tests/test_web.py), so a request made while a turn streams (a cancel, a
decision) needs a real server. Nothing here touches ~/.evi.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from tests.conftest import _text_chunk

UI = {"X-Evi-Client": "web"}  # what index.html's fetch wrapper adds


class Scripted:
    """An OpenAI-shaped client whose create() serves `turns` in order: a list
    of chunks, a stream object, or a callable(**kwargs) returning either."""

    def __init__(self, turns: list) -> None:
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any):
        self.requests.append(kwargs)
        turn = self.turns.pop(0)
        if callable(turn):
            return turn(**kwargs)
        return iter(turn) if isinstance(turn, list) else turn


class SlowStream:
    """A reply that streams `first`, then keeps the connection open, sending
    an empty chunk every `every` seconds, until it is closed."""

    def __init__(self, first: list, every: float = 0.05) -> None:
        self.first = first
        self.every = every
        self.closed = threading.Event()

    def __iter__(self):
        yield from self.first
        while not self.closed.wait(self.every):
            yield _text_chunk("")

    def close(self) -> None:
        self.closed.set()


def web_app(monkeypatch, tmp_path: Path, agent):
    """The web app on a tmp home, serving `agent` for every session."""
    import evi.apps.web.server as server_mod
    import evi.config as config_mod
    import evi.sdk.builder as builder_mod
    from evi import hooks as hooks_mod

    for var in ("EVI_ALLOW_CROSS_ORIGIN", "EVI_ALLOW_ANY_HOST", "EVI_TRUSTED_PROXY",
                "EVI_DECISION_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(hooks_mod, "HOOKS_CONFIG_PATH", home / "hooks.toml")
    monkeypatch.setattr("evi.plugins.plugin_dirs", lambda root=None: [])
    monkeypatch.setattr("evi.sessions.find_session", lambda *a, **k: None)
    monkeypatch.setattr(builder_mod, "build_agent", lambda *a, **k: agent)
    return server_mod.create_app()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def serving(app) -> Iterator[str]:
    """Run `app` on a real uvicorn server in a thread; yields its base URL."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error", lifespan="off"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(250):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        th.join(10)


def read_until(lines: Iterator[str], kind: str, seen: list | None = None) -> dict:
    """Read SSE lines up to the first event of `kind` (each event read is
    appended to `seen`)."""
    for line in lines:
        if line.startswith("data:") and line[5:].strip():
            ev = json.loads(line[5:].strip())
            if seen is not None:
                seen.append(ev)
            if ev["kind"] == kind:
                return ev
    raise AssertionError(f"stream ended before {kind}")


def sse_events(body: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in body.splitlines()
            if line.startswith("data:") and line[5:].strip()]
