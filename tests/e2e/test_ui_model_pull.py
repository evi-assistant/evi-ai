"""E2E: the UI's model-download progress still works now that GET
/api/backend/pull needs X-Evi-Client.

A pull starts a multi-GB download, so the server refuses that GET without the
header (or the token): no <img>, <link>, <iframe> or link — on another site or
in eVi's own page — can start one (test_ui_security.py, test_web_hardening.py).
An EventSource can't send headers, so the UI reads the progress stream with
fetch (streamModelPull) and parses the server-sent events itself. These tests
drive both progress UIs that use it: the first-run download step
(pullModelSSE -> #bk-progress) and Settings -> Model & Backend's "Recommended
for your hardware" Pull button. Each runs once against the real server (the
UI's request gets through; the name is one Ollama refuses, so with or without
Ollama nothing downloads and the stream ends in an error the UI shows) and
once against a canned stream of progress and done.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e

# A model name Ollama rejects outright: the real server never downloads it.
BOGUS = "!evi-e2e-invalid!"
PULL = re.compile(r".*/api/backend/pull\b.*")


def _sse(events: list[dict]) -> str:
    # sse-starlette's framing (CRLF), with a keep-alive comment in between.
    frames = [f"data: {json.dumps(e)}\r\n\r\n" for e in events]
    return ": ping\r\n\r\n".join(frames)


def _progress(pct: float, status: str = "pulling 1a2b3c") -> dict:
    return {"kind": "progress", "model": BOGUS, "status": status,
            "downloaded": int(pct * 10), "total": 1000, "pct": pct}


def _canned_pull(page: Page, events: list[dict]) -> list[dict]:
    """Answer the UI's pull with `events`; returns each request's headers."""
    seen: list[dict] = []

    def handle(route: Route) -> None:
        seen.append({**route.request.all_headers(), "url": route.request.url})
        route.fulfill(status=200, body=_sse(events),
                      headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})

    page.route(PULL, handle)
    return seen


def _assert_the_ui_sent_the_header(seen: list[dict]) -> None:
    assert len(seen) == 1, seen
    assert seen[0].get("x-evi-client") == "web", seen
    assert "token=" not in seen[0]["url"]  # no token here; it would ride in a header


def _first_run_progress_box(page: Page) -> None:
    """The first-run download step's progress line (#bk-progress, which the
    backend banner draws), on the loaded page."""
    page.evaluate("""() => {
        const p = document.createElement('div');
        p.id = 'bk-progress';
        p.style.display = 'none';
        document.body.appendChild(p);
    }""")


def _open_page(page: Page, evi_base_url: str) -> None:
    page.goto(evi_base_url)
    expect(page.locator("#input")).to_be_visible()


def test_first_run_download_shows_progress(page: Page, evi_base_url: str):
    seen = _canned_pull(page, [_progress(12.5), _progress(42), {"kind": "done", "model": BOGUS}])
    _open_page(page, evi_base_url)
    _first_run_progress_box(page)
    assert page.evaluate("(m) => pullModelSSE(m)", BOGUS) is True
    box = page.locator("#bk-progress")
    expect(box).to_be_visible()
    expect(box).to_contain_text("Downloading model — pulling 1a2b3c (42%)")
    assert box.evaluate("p => p.querySelector('div > div > div').style.width") == "42%"
    _assert_the_ui_sent_the_header(seen)


def test_first_run_download_reaches_the_server_and_shows_its_error(
        page: Page, evi_base_url: str):
    _open_page(page, evi_base_url)
    _first_run_progress_box(page)
    with page.expect_response(PULL) as resp:
        ok = page.evaluate("(m) => pullModelSSE(m)", BOGUS)
    assert resp.value.status == 200  # not refused: the UI's fetch sent the header
    assert resp.value.request.header_value("x-evi-client") == "web"
    assert ok is False
    expect(page.locator("#bk-progress")).to_contain_text("Model download error: ")


def _ollama_system(page: Page) -> dict:
    """Settings -> Model & Backend as on an Ollama machine whose recommended
    model isn't installed (so it offers Pull) — until a pull finishes, after
    which the model is the current one."""
    state = {"pulled": False}

    def handle(route: Route) -> None:
        route.fulfill(json={
            "os": "E2E", "os_detail": "E2E OS", "ram_gb": 16, "gpus": [], "mode": "cpu",
            "backend": "ollama",
            "current_model": BOGUS if state["pulled"] else "e2e-current:1b",
            "recommended": {"chat": BOGUS, "chat_installed": state["pulled"]},
        })

    page.route("**/api/system", handle)
    return state


def _open_recommended(page: Page, evi_base_url: str) -> None:
    _open_page(page, evi_base_url)
    page.evaluate("window.eviUI.openSettings('model')")
    expect(page.locator("#rec-action button", has_text="Pull")).to_be_visible(timeout=10_000)
    # Every status line the pull shows, in order — from the mutation records,
    # since one network chunk can carry several events (and the section
    # re-renders after a finished pull, replacing this node).
    page.evaluate("""() => {
        const s = document.getElementById('rec-status');
        window.__recStatus = [];
        new MutationObserver(recs => {
            for (const r of recs) for (const n of r.addedNodes) window.__recStatus.push(n.textContent);
        }).observe(s, { childList: true });
    }""")


def test_settings_pull_streams_progress_to_installed(page: Page, evi_base_url: str):
    state = _ollama_system(page)
    events = [_progress(42), _progress(100, "verifying sha256 digest"),
              {"kind": "done", "model": BOGUS}]
    seen = _canned_pull(page, events)
    _open_recommended(page, evi_base_url)
    state["pulled"] = True  # what /api/system says once the download is in
    page.locator("#rec-action button", has_text="Pull").click()
    expect(page.locator("#rec-action")).to_contain_text("✓ current", timeout=10_000)
    shown = page.evaluate("() => window.__recStatus")
    assert "pulling 1a2b3c (42%)" in shown, shown
    assert "verifying sha256 digest (100%)" in shown, shown
    assert shown[-1] == "Installed ✓", shown
    _assert_the_ui_sent_the_header(seen)


def test_settings_pull_reaches_the_server_and_shows_its_error(page: Page, evi_base_url: str):
    _ollama_system(page)
    _open_recommended(page, evi_base_url)
    with page.expect_response(PULL) as resp:
        page.locator("#rec-action button", has_text="Pull").click()
    assert resp.value.status == 200  # not "Download failed: HTTP 403"
    expect(page.locator("#rec-status")).to_have_text(re.compile(r"^Error: "), timeout=15_000)
    assert page.evaluate("() => window.__recStatus")[-1].startswith("Error: ")
