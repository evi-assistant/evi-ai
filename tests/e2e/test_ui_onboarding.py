"""E2E: first run offers what the machine can already do (1.0.49).

When nothing answers, setup takes the whole screen and renders the server's
ranked `suggestions` ladder. Picking an option is a *verified* switch: the
server sends a real completion (and a tool-bearing one), reports
"Chat ✓ · Tools ✓", and rolls the switch back if nothing answered.

`/api/backend/status` is stubbed in the browser: the real probe looks at this
machine's PATH, environment and local ports, which a test can't pin. The switch
itself goes to the real server, pointed (by rewriting the request) at the fake
backend or at a closed port, so the verify and the rollback are the real ones.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e

# The server's ladder order: an installed CLI, then an API key already in the
# environment, then the download (_first_run_suggestions in server.py). A
# running local server would rank second, but then chat isn't blocked and the
# banner shows instead of this screen.
SUGGESTIONS = [
    {"kind": "claude_agent", "action": "use", "title": "Use Claude Code",
     "detail": "Installed and signed in.", "badge": "installed", "instant": True},
    {"kind": "codex", "action": "use", "title": "Use Codex",
     "detail": "Installed on this PC.", "badge": "installed", "instant": True},
    {"kind": "openai_compat", "action": "env", "title": "Use your OpenAI API key",
     "detail": "Found in this machine's environment.", "badge": "key found", "instant": True},
    {"kind": "openai_compat", "action": "env", "title": "Use your Anthropic API key",
     "detail": "Found in this machine's environment.", "badge": "key found", "instant": True},
    {"kind": "llamacpp", "action": "runtime", "title": "Download a model",
     "detail": "About 1 GB. Runs on this PC, nothing leaves it. No account.",
     "badge": "key-free", "instant": False},
]


def _status(reachable: bool) -> dict:
    return {
        "configured": {"backend": "openai_compat", "base_url": "http://127.0.0.1:9/v1",
                       "model": "fake", "reachable": reachable},
        "candidates": [{"kind": "ollama", "url": "http://localhost:11434/v1", "reachable": False}],
        "ollama_installed": False, "any_reachable": reachable,
        "cli_agents": ["claude_agent", "codex"], "api_keys": ["openai", "anthropic"],
        "recommended_model": "qwen2.5:3b-instruct-q4_K_M", "can_auto_install_ollama": False,
        "runtime_supported": True, "runtime_installed": False,
        "suggestions": [] if reachable else SUGGESTIONS,
    }


def _stub_status(page: Page) -> dict:
    """Serve a first-run status until the test flips `state['ready']`."""
    state = {"ready": False}
    page.route("**/api/backend/status",
               lambda route: route.fulfill(json=_status(state["ready"])))
    return state


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _llm(page: Page) -> dict:
    return page.evaluate("() => fetch('/api/config').then(r => r.json()).then(c => c.llm)")


def _page_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def _redirect_switch(page: Page, base_url: str, sent: list[dict]) -> None:
    """Let the UI's own POST /api/backend/use through to the real server, but
    aimed at `base_url` with the fake model, recording what the UI sent."""
    def handle(route: Route) -> None:
        body = json.loads(route.request.post_data or "{}")
        sent.append(body)
        route.continue_(post_data=json.dumps({**body, "base_url": base_url, "model": "fake"}))
    page.route("**/api/backend/use", handle)


def test_setup_screen_ranks_what_the_machine_already_has(page: Page, evi_base_url: str):
    _stub_status(page)
    page.goto(evi_base_url)
    screen = page.locator("#setup-screen")
    expect(screen).to_be_visible(timeout=10_000)
    expect(screen.locator("h2")).to_have_text("Get eVi answering")
    expect(page.locator("#backend-banner")).to_be_hidden()   # a screen, not a strip

    # The first four, in the server's order, each with its badge.
    opts = screen.locator(".setup-opt")
    expect(opts).to_have_count(len(SUGGESTIONS))
    for i, s in enumerate(SUGGESTIONS[:4]):
        expect(opts.nth(i)).to_be_visible()
        expect(opts.nth(i)).to_contain_text(s["title"])
        expect(opts.nth(i)).to_contain_text(s["badge"])
    # The rest fold under "Other options found (n)".
    expect(opts.nth(4)).to_be_hidden()
    screen.locator("summary", has_text="Other options found (1)").click()
    expect(opts.nth(4)).to_be_visible()
    expect(opts.nth(4)).to_contain_text("Download a model")

    # "Set this up later" gives the chat back.
    screen.locator("#setup-later").click()
    expect(page.locator("#setup-screen")).to_have_count(0)
    expect(page.locator("#input")).to_be_visible()


def test_verified_switch_reports_chat_and_tools(page: Page, evi_base_url: str):
    state = _stub_status(page)
    page.goto(evi_base_url)
    fake_url = _llm(page)["base_url"]
    sent: list[dict] = []
    _redirect_switch(page, fake_url, sent)

    page.locator(".setup-opt", has_text="Use your OpenAI API key").click()
    progress = page.locator("#setup-progress")
    expect(progress).to_contain_text("Chat", timeout=30_000)
    expect(progress).to_contain_text("replied in")
    expect(progress).to_contain_text("tool calls accepted")
    expect(progress).not_to_contain_text("✗")
    assert sent == [{"kind": "openai_compat", "verify": True}], sent

    state["ready"] = True
    progress.locator("#setup-done", has_text="Start chatting").click()
    expect(page.locator("#setup-screen")).to_have_count(0)
    expect(page.locator("#backend-banner")).to_be_hidden()
    llm = _llm(page)
    assert (llm["backend"], llm["base_url"], llm["model"]) == ("openai_compat", fake_url, "fake")


def test_failed_switch_rolls_back_and_changes_nothing(
    page: Page, evi_base_url: str, evi_home
):
    _stub_status(page)
    page.goto(evi_base_url)
    before = _llm(page)
    dead = f"http://127.0.0.1:{_closed_port()}/v1"
    sent: list[dict] = []
    _redirect_switch(page, dead, sent)
    try:
        page.locator(".setup-opt", has_text="Use your OpenAI API key").click()
        progress = page.locator("#setup-progress")
        expect(progress).to_contain_text("It didn't answer — nothing was changed.",
                                         timeout=60_000)
        expect(progress.locator("#setup-diag")).to_be_visible()
        # The options are usable again, to pick another one.
        expect(page.locator(".setup-opt").first).to_be_enabled()
        assert sent and sent[0]["verify"] is True, sent

        # Rolled back: the live config and the file on disk still name the
        # backend that worked, never the one that didn't answer.
        after = _llm(page)
        for key in ("backend", "base_url", "model"):
            assert after[key] == before[key], (key, before, after)
        assert dead not in (evi_home / "config.toml").read_text(encoding="utf-8")
    finally:
        # If the rollback regressed, don't leave the shared server pointed at
        # a closed port for every later test. Straight to the server: the
        # page's route would aim this request at the closed port too.
        httpx.post(f"{evi_base_url}/api/backend/use", timeout=30, json={
            "kind": before["backend"], "base_url": before["base_url"],
            "model": before["model"]})


def test_failed_switch_run_diagnostics_button_works(page: Page, evi_base_url: str):
    """1.0.49 shipped this button dead: it called a bare openDiagnostics(),
    which lives inside the app IIFE, so the click threw a ReferenceError."""
    errors = _page_errors(page)
    _stub_status(page)
    page.route("**/api/backend/use", lambda route: route.fulfill(json={
        "ok": False, "verified": False,
        "verify": {"chat": False, "tools": False, "latency_ms": 3,
                   "error": "APIConnectionError: Connection error."},
    }))
    page.goto(evi_base_url)
    page.locator(".setup-opt", has_text="Use your OpenAI API key").click()
    progress = page.locator("#setup-progress")
    expect(progress).to_contain_text("nothing was changed")
    expect(progress).to_contain_text("APIConnectionError")

    progress.locator("#setup-diag", has_text="Run diagnostics").click()
    expect(page.locator("#setup-screen")).to_have_count(0)
    expect(page.locator("#dialog-overlay")).to_be_visible()
    expect(page.locator("#dialog-title")).to_have_text("Diagnostics")
    expect(page.locator("#dialog-content .set-about-row").first).to_be_visible(timeout=20_000)
    assert errors == [], errors
