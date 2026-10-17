"""E2E: verify before done in the web UI (loop-verify-gate).

A `protocol = "json"` stop hook that blocks sends the model back to work: the
UI shows the block as an inline notice (a note: it is in the model's
context, so it takes that history index, with no message actions), and at
the turn's end a files-changed footer lists what the turn's tools wrote,
under the reply, UI-only (never in the history).

This test runs its own eVi server and fake backend (not the shared
`evi_base_url` one), so the blocking hook it installs reaches no other test:
- the fake backend answers a prompt with one write_file call, a tool result
  with "Wrote hello.txt.", and a stop-hook note with "Checked again";
- the hook (hooks.toml in this server's own EVI_HOME) blocks once, when the
  turn wrote a file.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

HISTORY = """async () =>
    (await (await fetch('/api/session/' + sessionId + '/history')).json()).messages"""
BUBBLES = """() => [...document.querySelectorAll('#log .msg')].map(e => ({
    cls: e.className, index: e.dataset.index || null,
    actions: !!e.querySelector('.msg-actions'), text: e.textContent}))"""
REASON = "e2e gate: run the tests before you finish"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fake_backend():
    """An OpenAI-compatible backend scripted by the last message's role."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    def chunk(delta: dict, finish: str | None = None) -> str:
        return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    async def models(_request):
        return JSONResponse({"object": "list", "data": [{"id": "fake", "object": "model"}]})

    async def chat(request):
        body = await request.json()
        if not body.get("stream"):  # the tab title
            return JSONResponse({"id": "x", "object": "chat.completion", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "Verify"},
                "finish_reason": "stop"}]})
        last = (body.get("messages") or [{}])[-1]
        content = str(last.get("content") or "")

        def gen():
            if last.get("role") == "user" and not content.startswith("[message from"):
                call = {"index": 0, "id": "call_w", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "hello.txt", "content": "hi\n"})}}
                yield chunk({"role": "assistant", "tool_calls": [call]})
                yield chunk({}, "tool_calls")
            else:
                text = ("Checked again: all good." if last.get("role") == "user"
                        else "Wrote hello.txt.")
                yield chunk({"content": text})
                yield chunk({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/models", models),
                             Route("/v1/chat/completions", chat, methods=["POST"])])


@pytest.fixture
def verify_server(tmp_path):
    import httpx
    import uvicorn

    home, work = tmp_path / "evi-home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    fake_port = _free_port()
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
    marker = tmp_path / "blocked-once"
    script = (
        "import json, pathlib, sys\n"
        "ev = json.load(sys.stdin)\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if ev['files_changed'] and not marker.exists():\n"
        "    marker.write_text('x')\n"
        f"    print(json.dumps({{'decision': 'block', 'reason': {REASON!r}}}))\n"
    )
    (home / "hooks.toml").write_text(
        "[[stop]]\n"
        'name = "e2e-gate"\n'
        'protocol = "json"\n'
        f"command = [{json.dumps(sys.executable)}, \"-c\", {json.dumps(script)}]\n",
        encoding="utf-8",
    )
    fake = uvicorn.Server(uvicorn.Config(_fake_backend(), host="127.0.0.1", port=fake_port,
                                         log_level="error"))
    threading.Thread(target=fake.run, daemon=True).start()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "evi.apps.web.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env={**os.environ, "EVI_HOME": str(home)}, cwd=str(work),
    )
    base = f"http://127.0.0.1:{port}"
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
        yield base, work
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        fake.should_exit = True


def _wait_roles(page: Page, roles: list[str]) -> list[dict]:
    msgs: list[dict] = []
    for _ in range(100):
        msgs = page.evaluate(HISTORY)
        if [m["role"] for m in msgs] == roles:
            return msgs
        page.wait_for_timeout(200)
    pytest.fail(f"history never became {roles}: {[m['role'] for m in msgs]}")


def test_a_stop_block_shows_inline_and_the_files_changed_footer_under_the_reply(
    page: Page, verify_server
):
    base, work = verify_server
    page.goto(base)
    page.wait_for_load_state("networkidle")
    page.fill("#input", "please write hello.txt")
    page.click("#send")

    # The block: an inline notice naming the hook and its reason.
    notice = page.locator("#log .msg.note.verify-note")
    expect(notice).to_contain_text('stop hook "e2e-gate" blocked the end', timeout=20_000)
    expect(notice).to_contain_text(REASON)
    # The model went back to work, and the turn ended with its new reply.
    last = page.locator("#log .msg.assistant").last
    expect(last).to_contain_text("Checked again: all good.", timeout=20_000)
    expect(page.locator("#send")).to_have_text("Send", timeout=20_000)
    assert (work / "hello.txt").read_text(encoding="utf-8") == "hi\n"

    # The footer: under the turn's last reply, listing what the turn wrote.
    footer = last.locator(".files-changed")
    expect(footer).to_contain_text("Files changed (1)")
    expect(footer.locator("li")).to_have_text(["hello.txt"])

    # The note joined the history in its place; the footer did not.
    msgs = _wait_roles(page, ["system", "user", "assistant", "tool", "assistant", "user",
                              "assistant"])
    assert msgs[5]["kind"] == "note" and REASON in msgs[5]["content"]
    assert "Files changed" not in json.dumps(msgs)
    # And the UI's bubbles stay aligned with the server's history indexes
    # (edit / branch / delete go by them): the notice took index 5 without
    # message actions, the reply after it is 6.
    assert page.evaluate("nextHistoryIndex") == 7
    bubbles = [b for b in page.evaluate(BUBBLES) if "check-line" not in b["cls"]]
    assert [(b["cls"].split()[1], b["index"], b["actions"]) for b in bubbles
            if b["cls"].split()[1] in ("user", "assistant", "tool", "note")] == [
        ("user", "1", True), ("assistant", "2", True), ("tool", "3", True),
        ("assistant", "4", True), ("note", None, False), ("assistant", "6", True),
    ]

    # A reload draws the history: the block is still a note in its place.
    page.reload()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#log .msg.note", has_text=REASON)).to_be_visible(timeout=20_000)
    assert page.evaluate("nextHistoryIndex") == 7


def test_the_verify_events_render_with_text_only(page: Page, verify_server):
    """Check lines and a skipped-checks notice render as text: markup in a
    check's output or a hook's reason is never parsed."""
    base, _work = verify_server
    page.goto(base)
    page.wait_for_load_state("networkidle")
    page.evaluate("""async () => {
        let a = null;
        a = await handleEvent({kind: 'CheckResult', name: 'pytest', cmd: 'pytest -q t.py',
            exit: 1, duration_ms: 812, tail: '<img src=x onerror=window.pwned=1>failed',
            round: 1, source: 'pytest'}, a, sessionId);
        a = await handleEvent({kind: 'ChecksSkipped', reason: '<b>untrusted</b>'}, a, sessionId);
        a = await handleEvent({kind: 'ChecksFailed', round: 1, max_rounds: 2,
            failed: ['<i>pytest</i>']}, a, sessionId);
        a = await handleEvent({kind: 'FilesChanged', paths: ['<script>x</script>.py']}, a,
            sessionId);
    }""")
    line = page.locator("#log .msg.check-line.fail")
    expect(line).to_contain_text("✗ check pytest (round 1) exit 1 · pytest -q t.py")
    expect(line.locator("pre")).to_have_text("<img src=x onerror=window.pwned=1>failed")
    expect(page.locator("#log .msg.check-line", has_text="checks skipped")).to_contain_text(
        "<b>untrusted</b>")
    expect(page.locator("#log .msg.verify-note")).to_contain_text("<i>pytest</i>")
    expect(page.locator(".files-changed li")).to_have_text(["<script>x</script>.py"])
    assert page.evaluate("window.pwned === undefined")
    assert page.locator("#log img").count() == 0
