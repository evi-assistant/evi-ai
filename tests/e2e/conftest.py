"""E2E test harness: run the REAL eVi web server (with a fake streaming LLM
backend) and drive its UI in a real browser via Playwright.

This is the layer the unit tests can't reach — it would have caught the
0.24.2 "chat renders nothing" bug, where the server streamed SSE correctly but
the browser's parser never rendered it.

The fake backend is a tiny OpenAI-compatible server that streams a canned
chat-completion, so no Ollama/LM Studio is needed (works in CI). The eVi server
runs as a SUBPROCESS so EVI_HOME (bound at import) points at an isolated tmp
config aimed at the fake backend.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")  # whole dir skips cleanly if e2e extra absent

ROOT = Path(__file__).resolve().parents[2]
FAKE_REPLY = "Hello from the fake backend! This is a streamed reply."


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_fake_llm_app():
    """A minimal OpenAI-compatible backend: /v1/models + streaming and
    non-streaming /v1/chat/completions."""
    import json

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    async def models(_request):
        return JSONResponse({"object": "list", "data": [{"id": "fake", "object": "model"}]})

    async def chat(request):
        body = await request.json()
        if not body.get("stream"):
            return JSONResponse({
                "id": "x", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": FAKE_REPLY}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        async def gen():
            for i, word in enumerate(FAKE_REPLY.split(" ")):
                delta = {"content": ("" if i == 0 else " ") + word}
                chunk = {"id": "x", "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            done = {"id": "x", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return Starlette(routes=[
        Route("/v1/models", models),
        Route("/v1/chat/completions", chat, methods=["POST"]),
    ])


@pytest.fixture(scope="session")
def evi_base_url(tmp_path_factory):
    import httpx
    import uvicorn

    home = tmp_path_factory.mktemp("evi-home")
    fake_port = _free_port()
    evi_port = _free_port()

    (home / "config.toml").write_text(
        "[llm]\n"
        'backend = "openai_compat"\n'
        f'base_url = "http://127.0.0.1:{fake_port}/v1"\n'
        'api_key = "test"\n'
        'model = "fake"\n'
        "[web]\n"
        'auth_token = ""\n',
        encoding="utf-8",
    )

    fake = uvicorn.Server(uvicorn.Config(_make_fake_llm_app(), host="127.0.0.1",
                                         port=fake_port, log_level="error"))
    threading.Thread(target=fake.run, daemon=True).start()

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "evi.apps.web.server:app",
         "--host", "127.0.0.1", "--port", str(evi_port)],
        env={**os.environ, "EVI_HOME": str(home)}, cwd=str(ROOT),
    )
    base = f"http://127.0.0.1:{evi_port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("evi web server did not become healthy")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        fake.should_exit = True
