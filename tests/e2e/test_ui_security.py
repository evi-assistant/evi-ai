"""E2E: a page on ANOTHER origin can't drive eVi's upload API through the
browser (1.0.53 hotfix).

A multipart `fetch(..., {mode: 'no-cors'})` POST is a CORS "simple request": the
browser sends it without a preflight, the attacker just can't read the reply.
Before the hotfix that was enough — `session_id='../../pwned'` wrote outside
~/.evi/uploads. Here a real Chromium page served from a different local port
fires exactly that request at the real eVi server, and we assert the server
refused it (403, seen at the network layer) and wrote nothing. The same page
can't start a model pull through the one side-effecting GET either, while the
real Chromium header pairs for eVi's own pages (same origin, a loopback alias
of its port) still get through.

The eVi home is a pytest tmp dir (see conftest.evi_base_url), so even a
regression could only write inside the tmp tree.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

ATTACK_PAGE = b"""<!doctype html><html><head><title>not evi</title></head><body>
<script>
window.attack = async (target, sid) => {
  const fd = new FormData();
  fd.append('session_id', sid);
  fd.append('file', new Blob(['owned'], { type: 'text/plain' }), 'pwned.txt');
  try {
    await fetch(target + '/api/upload', { method: 'POST', mode: 'no-cors', body: fd });
    return 'sent';
  } catch (e) {
    return 'error: ' + e;
  }
};
// GET-shaped attacks on the side-effecting /api/backend/pull: a no-cors GET (the
// header shape of an <img src>: no Origin, only Sec-Fetch-Site. An actual <img>
// works too, but Chromium's ORB hides the JSON reply's status from the test) and
// an EventSource (CORS mode, with Origin).
window.pullNoCors = (url) => { fetch(url, { mode: 'no-cors' }).catch(() => {}); };
window.pullEs = (url) => { const es = new EventSource(url); es.onerror = () => es.close(); };
</script></body></html>"""

# A name Ollama rejects outright, so even a regression that reached the
# handler would make no download (and none at all when Ollama isn't running).
BOGUS_MODEL = "%21evi-e2e-invalid%21"


class _AttackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(ATTACK_PAGE)))
        self.end_headers()
        self.wfile.write(ATTACK_PAGE)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture(scope="module")
def attacker_port():
    """A throwaway 'hostile' site on another local port (a different origin)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _AttackHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _network_view(page: Page, script: str, url_part: str) -> tuple[dict, int]:
    """Run `script` in the page; return (request headers, status) of the first
    request whose URL contains `url_part`, as Chromium's network layer saw
    them. CDP, because ORB / a failed CORS check hide the reply from the page
    and from Playwright's response events, and Playwright's request headers
    lack the Sec-Fetch-* the network stack adds."""
    cdp = page.context.new_cdp_session(page)
    urls: dict[str, str] = {}
    sent: dict[str, dict] = {}
    status: dict[str, int] = {}
    cdp.on("Network.requestWillBeSent",
           lambda e: urls.setdefault(e["requestId"], e["request"]["url"]))
    cdp.on("Network.requestWillBeSentExtraInfo",
           lambda e: sent.setdefault(e["requestId"],
                                     {k.lower(): v for k, v in e["headers"].items()}))
    cdp.on("Network.responseReceivedExtraInfo",
           lambda e: status.setdefault(e["requestId"], e["statusCode"]))
    cdp.send("Network.enable")
    try:
        page.evaluate(script)
        for _ in range(100):
            rid = next((r for r, u in urls.items() if url_part in u), None)
            if rid is not None and rid in sent and rid in status:
                return sent[rid], status[rid]
            page.wait_for_timeout(100)
        raise AssertionError(f"no network response for {url_part}: {urls}")
    finally:
        cdp.detach()


def _same_origin_upload(page: Page, evi_base_url: str, sid: str) -> dict:
    """Upload from eVi's OWN page — the legit path the UI uses."""
    page.goto(evi_base_url)
    return page.evaluate(
        """async (sid) => {
          const fd = new FormData();
          fd.append('session_id', sid);
          fd.append('file', new Blob(['legit'], { type: 'text/plain' }), 'legit.txt');
          const r = await fetch('/api/upload', { method: 'POST', body: fd });
          return { status: r.status, body: await r.json() };
        }""",
        sid,
    )


@pytest.mark.parametrize("attacker_host", ["127.0.0.1", "localhost"])
def test_cross_origin_upload_is_refused_and_writes_nothing(
    page: Page, evi_base_url: str, attacker_port: int, attacker_host: str
) -> None:
    # Locate the server's real UPLOADS_DIR via a legit upload (also proves the
    # guard leaves the app's own requests alone).
    legit = _same_origin_upload(page, evi_base_url, "e2e-sec-legit")
    assert legit["status"] == 200, legit
    uploads = Path(legit["body"]["path"]).parent.parent
    assert uploads.name == "uploads" and uploads.is_dir()
    home = uploads.parent
    escaped = (uploads / "../../pwned").resolve()        # where the traversal aims
    assert escaped == (home.parent / "pwned").resolve()
    assert not escaped.exists()

    # The hostile page: same machine, different port → a different origin
    # (127.0.0.1 is same-SITE, localhost is cross-site; both must be refused).
    page.goto(f"http://{attacker_host}:{attacker_port}/")
    expect(page).to_have_title("not evi")
    for sid in ("../../pwned", "e2e-sec-xsite"):
        with page.expect_response(lambda r: r.url.endswith("/api/upload")) as resp:
            outcome = page.evaluate("([t, s]) => window.attack(t, s)", [evi_base_url, sid])
        assert outcome == "sent"  # no-cors: the browser DID send it…
        assert resp.value.status == 403, (sid, resp.value.status)  # …and eVi refused

    # Nothing written: not at the traversal target, not anywhere under the tmp
    # tree that holds the eVi home, not even for the well-formed id.
    assert not escaped.exists()
    assert not (uploads / "e2e-sec-xsite").exists()
    assert list(home.parent.rglob("pwned.txt")) == []

    # And the app is unaffected: it loads and its own uploads still work.
    again = _same_origin_upload(page, evi_base_url, "e2e-sec-legit2")
    assert again["status"] == 200, again
    expect(page.locator("#input")).to_be_visible()


@pytest.mark.parametrize("attacker_host", ["127.0.0.1", "localhost"])
def test_cross_origin_page_cannot_start_a_model_pull(
    page: Page, evi_base_url: str, attacker_port: int, attacker_host: str
) -> None:
    """GET /api/backend/pull starts an Ollama download; a hostile page must not
    be able to fire it <img>-style (no Origin: only Sec-Fetch-Site tells —
    `same-site` from another 127.0.0.1 port, `cross-site` from localhost) or
    with an EventSource (Origin present)."""
    page.goto(f"http://{attacker_host}:{attacker_port}/")
    expect(page).to_have_title("not evi")
    url = f"{evi_base_url}/api/backend/pull?model={BOGUS_MODEL}"
    site = "same-site" if attacker_host == "127.0.0.1" else "cross-site"
    for fn in ("pullNoCors", "pullEs"):
        sent, status = _network_view(page, f"window.{fn}('{url}')", "/api/backend/pull")
        # What Chromium really sends (the guard's whole premise): no Origin on
        # the no-cors GET, so Sec-Fetch-Site must carry it — `same-site` too.
        assert sent.get("sec-fetch-site") == site, (fn, sent)
        assert ("origin" in sent) == (fn == "pullEs"), (fn, sent)
        assert status == 403, (fn, status)


def test_ui_own_model_pull_stream_still_opens(page: Page, evi_base_url: str) -> None:
    """The first-run wizard's same-origin EventSource is not caught by the guard
    (the handler answers 200 and streams; the bogus name makes Ollama, if it is
    running at all, refuse rather than download)."""
    page.goto(evi_base_url)
    with page.expect_response(lambda r: "/api/backend/pull" in r.url) as resp:
        page.evaluate(
            "(m) => { window.__es = new EventSource('/api/backend/pull?model=' + m); }",
            BOGUS_MODEL,
        )
    assert resp.value.status == 200
    page.evaluate("() => window.__es.close()")


def test_loopback_alias_page_works_but_cannot_post_across_aliases(
    page: Page, evi_base_url: str
) -> None:
    """eVi's own UI opened as http://localhost:<port> works: its requests are
    same-origin there and carry X-Evi-Client. A no-cors POST from it to
    http://127.0.0.1:<port> (the same server by its other name) passes the
    Origin check — a loopback alias, although Chromium labels it
    `Sec-Fetch-Site: cross-site` — but a no-cors request can't carry
    X-Evi-Client, so it is refused like any browser request without it."""
    legit = _same_origin_upload(page, evi_base_url, "e2e-sec-legit")
    uploads = Path(legit["body"]["path"]).parent.parent
    port = evi_base_url.rsplit(":", 1)[1]
    alias = f"http://localhost:{port}"
    own = _same_origin_upload(page, alias, "e2e-sec-alias")
    assert own["status"] == 200, own
    assert (uploads / "e2e-sec-alias" / "legit.txt").read_text(encoding="utf-8") == "legit"
    with page.expect_response(lambda r: r.url == evi_base_url + "/api/upload") as resp:
        page.evaluate(
            """async (target) => {
              const fd = new FormData();
              fd.append('session_id', 'e2e-sec-alias2');
              fd.append('file', new Blob(['alias'], { type: 'text/plain' }), 'alias.txt');
              await fetch(target + '/api/upload', { method: 'POST', mode: 'no-cors', body: fd });
            }""",
            evi_base_url,
        )
    assert resp.value.status == 403, resp.value.status
    assert not (uploads / "e2e-sec-alias2").exists()


def test_same_origin_traversal_id_is_rejected(page: Page, evi_base_url: str) -> None:
    """Even from eVi's own page, a path-like session id is a 400, not a write."""
    res = _same_origin_upload(page, evi_base_url, "../../pwned-same-origin")
    assert res["status"] == 400, res


@pytest.mark.parametrize("ui_path", ["/", "/static/index.html"])
def test_crafted_session_link_is_refused(
    page: Page, evi_base_url: str, attacker_port: int, ui_path: str
) -> None:
    """A hostile page LINKS to the UI with a path-like `?session=` value. Every
    request the UI then makes is same-origin (the guard can't tell), so the UI
    must never be served for such an id — else it adopts it as a tab and
    splices it into its own transcript writes and `/api/session/<id>/…` POSTs."""
    crafted = f"{evi_base_url}{ui_path}?session=..%2F..%2Fapi%2Fruntime%2Fstop%3F"
    page.goto(f"http://localhost:{attacker_port}/")
    expect(page).to_have_title("not evi")
    with page.expect_response(lambda r: "session=" in r.url) as resp:
        page.evaluate("(u) => { location.href = u; }", crafted)   # = clicking the link
    assert resp.value.status == 400
    page.wait_for_load_state()
    expect(page.locator("#input")).to_have_count(0)               # the UI never ran

    # The crafted id never reached the UI's saved tabs, and the real UI still
    # opens a legitimate handoff link.
    good = "3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e"
    page.goto(f"{evi_base_url}/?session={good}")
    expect(page.locator("#input")).to_be_visible()
    tabs = page.evaluate("() => localStorage.getItem('evi.tabs') || ''")
    assert "runtime" not in tabs and ".." not in tabs
    assert good in tabs
