"""E2E: request hardening in a real browser against the real server.

* eVi's own pages send `X-Evi-Client` on every request (the fetch wrapper), so
  the UI keeps working while a browser request without it is refused.
* A 409 from a busy session turns the message into a follow-up, and a slash
  command sent mid-turn leaves the turn in flight (no second, parallel turn).
* A server launched with a token in EVI_WEB_AUTH_TOKEN (what the desktop shell
  does when LAN access is on) still serves this machine without it. With
  `[web] auth_token` set, a page that has the token injected as
  `window.__EVI_TOKEN__` (the shell's init script) signs itself in with no
  login prompt; a stale injected token never replaces a working stored one,
  and a failing stored token gives way to a working injected one.

The eVi homes are pytest tmp dirs (see conftest.evi_base_url).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]


def test_ui_requests_carry_the_client_header(page: Page, evi_base_url: str) -> None:
    page.goto(evi_base_url)
    expect(page.locator("#input")).to_be_visible()
    with page.expect_request(lambda r: r.url.endswith("/api/chat")) as req:
        page.fill("#input", "hello evi")
        page.click("#send")
    assert req.value.headers.get("x-evi-client") == "web"
    assistant = page.locator(".msg.assistant").last
    expect(assistant).to_contain_text("fake backend", timeout=20000)


def test_browser_post_without_the_header_is_refused(page: Page, evi_base_url: str) -> None:
    """XMLHttpRequest bypasses the fetch wrapper: a same-origin POST that a
    browser stamps with an Origin but no X-Evi-Client is refused."""
    page.goto(evi_base_url)
    expect(page.locator("#input")).to_be_visible()
    res = page.evaluate(
        """() => new Promise((resolve) => {
          const x = new XMLHttpRequest();
          x.open('POST', '/api/reset');
          x.setRequestHeader('Content-Type', 'application/json');
          x.onload = () => resolve({ status: x.status, body: x.responseText });
          x.send(JSON.stringify({ session_id: 'e2e-hdr', message: '' }));
        })"""
    )
    assert res["status"] == 403, res
    assert "X-Evi-Client" in res["body"]
    ok = page.evaluate(
        """async () => (await fetch('/api/reset', { method: 'POST',
             headers: { 'Content-Type': 'application/json' },
             body: JSON.stringify({ session_id: 'e2e-hdr', message: '' }) })).status"""
    )
    assert ok == 200


def test_a_busy_session_turns_the_message_into_a_follow_up(
    page: Page, evi_base_url: str
) -> None:
    """After a reload (or from a second window) the page doesn't know a turn
    is running; the server answers 409 and the UI queues the message as a
    follow-up instead of dropping it. Here the 409 is staged, so the steer
    finds nothing running and the message is sent normally — answered once."""
    calls: list[str] = []

    def busy_once(route) -> None:
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"detail": "session busy"}))
        else:
            route.continue_()

    page.goto(evi_base_url)
    expect(page.locator("#input")).to_be_visible()
    page.route("**/api/chat", busy_once)
    with page.expect_request(lambda r: r.url.endswith("/steer")):
        page.fill("#input", "hello while busy")
        page.click("#send")
    expect(page.locator(".msg.assistant").last).to_contain_text("fake backend", timeout=20000)
    expect(page.locator("#log .msg.user", has_text="hello while busy")).to_have_count(1)
    assert len(calls) == 2


def test_a_slash_command_mid_turn_keeps_the_turn_in_flight(
    page: Page, evi_base_url: str
) -> None:
    """`/help` sent while a turn runs used to reset the page's in-flight state
    when it returned, so the next message started a second, concurrent turn
    instead of steering the first."""
    held: list = []

    def hold_turns(route) -> None:
        body = route.request.post_data_json or {}
        if str(body.get("message", "")).startswith("/"):
            route.continue_()
        else:
            held.append(route)

    page.goto(evi_base_url)
    expect(page.locator("#input")).to_be_visible()
    page.route("**/api/chat", hold_turns)
    send = page.locator("#send")
    page.fill("#input", "a long task")
    page.click("#send")
    expect(send).to_have_text("Queue")
    page.fill("#input", "/help")
    page.press("#input", "Enter")
    expect(page.locator(".msg.system").last).to_contain_text("Built-in commands")
    expect(send).to_have_text("Queue")        # still this turn's
    assert len(held) == 1
    held[0].continue_()
    expect(page.locator(".msg.assistant").last).to_contain_text("fake backend", timeout=20000)
    expect(send).to_have_text("Send", timeout=10000)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(tmp_path: Path, config: str, env: dict[str, str] | None = None):
    """A real `evi web` app on 127.0.0.1 with its own eVi home; yields its base URL."""
    import httpx

    home = tmp_path / "evi-home"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "evi.apps.web.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env={**os.environ, "EVI_HOME": str(home), **(env or {})}, cwd=str(ROOT),
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
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


LAUNCH_TOKEN = "e2e" + "0" * 61


@pytest.fixture
def launch_token_server(tmp_path: Path):
    """Started the way the desktop shell starts a LAN-bound sidecar: the token
    arrives in EVI_WEB_AUTH_TOKEN, config.toml has none. (Bound to 127.0.0.1
    here, so every client is this machine; the LAN side is unit-tested.)"""
    yield from _start_server(tmp_path, '[web]\nauth_token = ""\n',
                             {"EVI_WEB_AUTH_TOKEN": LAUNCH_TOKEN})


@pytest.fixture
def token_server(tmp_path: Path):
    """`[web] auth_token` set: this machine needs the token too."""
    yield from _start_server(tmp_path, '[web]\nauth_token = "current-token"\n')


def _inject(page: Page, base: str, token: str) -> None:
    """The desktop shell's init script (main.rs webview_init_script)."""
    page.context.add_init_script(
        "(function(){if([%r].indexOf(window.location.origin)>=0)"
        "{window.__EVI_TOKEN__=%r;}})();" % (base, token)
    )


SHOWN = re.compile(r"(^|\s)shown(\s|$)")


def _settled(page: Page) -> None:
    expect(page.locator("#input")).to_be_visible()
    page.wait_for_timeout(1000)  # let bootstrapAuth settle


def _signed_in(page: Page) -> bool:
    return "shown" not in (page.locator("#auth-overlay").get_attribute("class") or "")


def _reset_status(page: Page) -> int:
    return page.evaluate(
        """async () => (await fetch('/api/reset', { method: 'POST',
             headers: { 'Content-Type': 'application/json' },
             body: JSON.stringify({ session_id: 'e2e-tok', message: '' }) })).status"""
    )


def test_launch_token_spares_this_machine(page: Page, launch_token_server: str) -> None:
    """The LAN token gates other machines only: an old VS Code extension on
    loopback (Node fetch: Sec-Fetch-Mode only, no token, no header) and the
    UI keep working with no sign-in."""
    import httpx

    base = launch_token_server
    node_fetch = {"accept": "*/*", "accept-language": "*", "sec-fetch-mode": "cors",
                  "user-agent": "node"}
    body = {"session_id": "x", "message": ""}
    assert httpx.post(base + "/api/reset", json=body, headers=node_fetch).status_code == 200
    # A proxy-relayed request is someone else's: it needs the token.
    relayed = {"X-Forwarded-For": "203.0.113.7"}
    assert httpx.post(base + "/api/reset", json=body, headers=relayed).status_code == 401
    assert httpx.post(base + "/api/reset", json=body, headers={
        **relayed, "Authorization": f"Bearer {LAUNCH_TOKEN}"}).status_code == 200
    page.goto(base)
    _settled(page)
    assert _signed_in(page)
    assert _reset_status(page) == 200


def test_injected_token_signs_the_ui_in(page: Page, token_server: str) -> None:
    import httpx

    base = token_server
    assert httpx.post(base + "/api/reset", json={"session_id": "x", "message": ""}
                      ).status_code == 401
    # Without the injected token the UI asks for one...
    page.goto(base)
    expect(page.locator("#auth-overlay")).to_have_class(SHOWN, timeout=10000)
    # ...with it (the desktop shell's init script, guarded by origin) it signs
    # itself in: no prompt, and its API calls are authenticated.
    _inject(page, base, "current-token")
    fresh = page.context.new_page()
    fresh.goto(base)
    _settled(fresh)
    assert _signed_in(fresh)
    assert _reset_status(fresh) == 200
    fresh.close()


def test_a_stale_injected_token_never_replaces_a_working_one(
    page: Page, token_server: str
) -> None:
    """The shell reads the token at launch. After a rotation while the app
    runs, the token you sign in with must stick across reloads — it used to be
    overwritten by the stale injected one on every load (a sign-in loop)."""
    base = token_server
    _inject(page, base, "token-before-rotation")
    page.goto(base)
    expect(page.locator("#auth-overlay")).to_have_class(SHOWN, timeout=10000)
    page.fill("#auth-token", "current-token")
    with page.expect_navigation():
        page.click("#auth-submit")
    for _ in range(2):  # and again after another reload
        _settled(page)
        assert _signed_in(page)
        assert page.evaluate("localStorage.getItem('evi.authToken')") == "current-token"
        assert _reset_status(page) == 200
        page.reload()


def test_a_failing_stored_token_falls_back_to_the_injected_one(
    page: Page, token_server: str
) -> None:
    """A stored token that stopped working (rotated elsewhere) gives way to a
    working injected one, with no prompt."""
    base = token_server
    page.context.add_init_script(
        "if(!sessionStorage.getItem('seeded')){sessionStorage.setItem('seeded','1');"
        "localStorage.setItem('evi.authToken','an-old-token');}"
    )
    _inject(page, base, "current-token")
    loads: list[int] = []
    page.on("load", lambda _p: loads.append(1))
    page.goto(base)
    # bootstrapAuth finds the stored token failing, checks the injected one,
    # adopts it and reloads once.
    for _ in range(100):
        if len(loads) >= 2:
            break
        page.wait_for_timeout(100)
    assert len(loads) == 2, loads
    _settled(page)
    assert _signed_in(page)
    assert page.evaluate("localStorage.getItem('evi.authToken')") == "current-token"
    assert _reset_status(page) == 200
    assert len(loads) == 2  # settled: no reload loop
