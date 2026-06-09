"""evi:// deep links — build, parse, and route app URLs (lighter/later item).

Routes:

    evi://session/<id>      open (or resume) a session
    evi://new               start a new chat
    evi://workflow/<name>   open the dispatch view, ready to run a workflow

The desktop app registers the ``evi://`` scheme and routes an incoming link to
the matching in-app web path via :func:`to_web_path`. The web UI already
understands ``/?session=<id>`` (Phase 87) and ``/?workflow=<name>`` (dispatch),
so the same links work when opened in the browser.

Pure stdlib + no side effects, so it's fully testable.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

SCHEME = "evi"


class DeepLinkError(ValueError):
    """The string isn't a valid evi:// link."""


def build_link(kind: str, value: str = "", **params: str) -> str:
    """Build an ``evi://<kind>/<value>?<params>`` link."""
    kind = kind.strip().strip("/")
    if not kind:
        raise DeepLinkError("kind is required")
    url = f"{SCHEME}://{kind}"
    if value:
        url += "/" + quote(str(value), safe="")
    if params:
        url += "?" + urlencode(params)
    return url


def parse_link(url: str) -> tuple[str, str, dict[str, str]]:
    """Parse an evi:// link into ``(kind, value, params)``."""
    parts = urlsplit(url.strip())
    if parts.scheme != SCHEME:
        raise DeepLinkError(f"not an {SCHEME}:// link: {url!r}")
    kind = (parts.netloc or "").strip()
    if not kind:
        raise DeepLinkError(f"missing route in {url!r}")
    value = unquote(parts.path.lstrip("/"))
    params = dict(parse_qsl(parts.query))
    return kind, value, params


def to_web_path(url: str) -> str:
    """Map an evi:// link to the in-app web path the webview should load.

    Unknown routes fall back to ``/`` so a stray link never errors the shell.
    """
    kind, value, _params = parse_link(url)
    if kind == "session" and value:
        return f"/?session={quote(value, safe='')}"
    if kind == "workflow" and value:
        return f"/?workflow={quote(value, safe='')}"
    if kind == "new":
        return "/"
    return "/"
