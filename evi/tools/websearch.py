"""Web search + fetch tools.

`web_search` queries DuckDuckGo (no API key) via the `ddgs`
library when present. `web_fetch` downloads a URL and extracts the main
readable text using `beautifulsoup4` if available; otherwise it returns a
naïvely stripped version. The download goes through `evi.netguard` (no
loopback / LAN / metadata addresses unless the host is in [auto]
trusted_domains, redirects re-checked hop by hop, body capped while streaming).

Both deps are optional — installing `evi[web-tools]` (ddgs + beautifulsoup4;
distinct from the `web` server extra) gets you the full experience. Without
them, the tools return clear error strings rather than crashing the agent.
"""

from __future__ import annotations

import json
import re
from typing import Callable
from urllib.parse import urlparse

import httpx

from evi.tools.base import tool


_USER_AGENT = "eVi/0.1 (+local personal assistant)"
_FETCH_MAX_BYTES = 1_000_000   # 1 MB cap on raw page download
_FETCH_MAX_TEXT = 16_000       # ~16 KB of extracted text to the LLM
_HTTP_TIMEOUT = 30.0
_FETCH_DEADLINE = 60.0         # wall clock for a whole web_fetch (every hop, headers and body)


@tool(
    description=(
        "Search the web. Returns up to `limit` results as JSON: "
        "[{title, url, snippet}, …]. Backend is configurable ([tools] "
        "search_backend = ddg | searxng | ollama); DuckDuckGo (keyless) is the "
        "default. Use this for current events, public docs, or anything not in "
        "your training data."
    ),
    category="web",
    trust="untrusted",
    long=True,
)
def web_search(query: str, limit: int = 5) -> str:
    if not query.strip():
        return "ERROR: empty query"
    limit = max(1, min(int(limit), 25))
    try:
        from evi.config import Config

        tools = Config.load().tools
        backend = (tools.search_backend or "ddg").strip().lower()
    except Exception:
        backend, tools = "ddg", None
    if backend == "searxng":
        return _search_searxng((tools.searxng_url if tools else ""), query, limit)
    if backend == "ollama":
        return _search_ollama(query, limit)
    return _search_ddg(query, limit)


def _search_ddg(query: str, limit: int) -> str:
    # `ddgs` is the maintained successor to `duckduckgo_search` (the old name
    # was deprecated and DuckDuckGo now throttles it to empty results). Prefer
    # ddgs; fall back to the legacy package so pre-migration installs that
    # haven't reinstalled the [web-tools] extra keep working (in degraded form)
    # rather than hard-failing. Both expose DDGS().text(query, max_results=…)
    # returning dicts keyed title/href/body.
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # legacy fallback (deprecated)
        except ImportError:
            return (
                "ERROR: ddgs not installed — "
                "install with: pip install 'evi-assistant[web-tools]'"
            )
    try:
        with DDGS() as engine:
            results = list(engine.text(query, max_results=limit))
    except Exception as exc:
        return f"ERROR: search failed: {type(exc).__name__}: {exc}"
    cleaned = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", "") or r.get("url", ""),
            "snippet": r.get("body", "") or r.get("snippet", ""),
        }
        for r in results
    ]
    return json.dumps(cleaned)


def _search_searxng(base_url: str, query: str, limit: int) -> str:
    """Query a self-hosted SearXNG instance's JSON API (fully local-first, no
    key). Configure the instance URL via [tools] searxng_url."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "ERROR: searxng backend selected but [tools] searxng_url is unset"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.get(
                f"{base}/search",
                params={"q": query, "format": "json"},
                headers={"User-Agent": _USER_AGENT},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return f"ERROR: searxng search failed: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return "ERROR: searxng returned an unexpected response shape"
    cleaned = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        }
        for item in (data.get("results") or [])[:limit]
    ]
    return json.dumps(cleaned)


def _search_ollama(query: str, limit: int) -> str:
    """Ollama's hosted web-search API (needs OLLAMA_API_KEY). Kept here for
    parity; SearXNG is the fully-self-hosted option."""
    import os

    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if not key:
        return "ERROR: ollama search backend needs OLLAMA_API_KEY in the environment"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.post(
                "https://ollama.com/api/web_search",
                headers={"Authorization": f"Bearer {key}", "User-Agent": _USER_AGENT},
                json={"query": query, "max_results": limit},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return f"ERROR: ollama search failed: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return "ERROR: ollama returned an unexpected response shape"
    cleaned = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", "") or item.get("snippet", ""),
        }
        for item in (data.get("results") or [])[:limit]
    ]
    return json.dumps(cleaned)


@tool(
    description=(
        "Fetch a URL and return its main text content (HTML stripped). "
        "Use after `web_search` to read a result. Returns up to ~16 KB."
    ),
    category="web",
    trust="untrusted",
    long=True,
)
def web_fetch(url: str):
    from evi import netguard
    from evi.citations import Citation, ToolOutput, trim_excerpt

    if not url.startswith(("http://", "https://")):
        return "ERROR: only http(s) URLs are allowed"
    # The URL is the model's choice and the page can redirect anywhere, so the
    # fetch goes through evi.netguard: no loopback / LAN / metadata address
    # unless the host is in [auto] trusted_domains, every redirect re-checked
    # against the permission policy, and the body streamed under the cap. No
    # `confirm`: a tool has no handle on the session's UI here, so a non-public
    # address for an untrusted host is refused with the reason (fail closed).
    auto = _auto_settings()
    try:
        r = netguard.fetch(
            url,
            trusted_domains=getattr(auto, "trusted_domains", None) or [],
            check_redirect=_redirect_check(auto, _policy_decision(auto, url)),
            headers={"User-Agent": _USER_AGENT},
            max_bytes=_FETCH_MAX_BYTES,
            timeout=_HTTP_TIMEOUT,
            total_timeout=_FETCH_DEADLINE,
        )
    except netguard.Blocked as exc:
        return f"ERROR: blocked: {exc}"
    except Exception as exc:
        return f"ERROR: fetch failed: {type(exc).__name__}: {exc}"
    if r.status >= 400:
        return f"ERROR: fetch failed: HTTP {r.status} for {r.url}"
    raw = r.content

    ctype = r.headers.get("content-type", "").lower()
    if "text/html" not in ctype and "application/xhtml" not in ctype:
        # Return as-is if it's plain text; refuse binaries.
        if ctype.startswith("text/"):
            text = raw.decode(errors="replace")[:_FETCH_MAX_TEXT]
            return ToolOutput(
                text=text,
                citations=[Citation(
                    id="1", source_type="url", source_id=url,
                    excerpt=trim_excerpt(text),
                )],
            )
        return f"ERROR: unsupported content-type: {ctype}"

    try:
        text = _extract_text(raw)
    except Exception as exc:
        return f"ERROR: extract failed: {type(exc).__name__}: {exc}"
    if len(text) > _FETCH_MAX_TEXT:
        text = text[:_FETCH_MAX_TEXT] + "\n…(truncated)"
    return ToolOutput(
        text=text,
        citations=[Citation(
            id="1", source_type="url", source_id=url,
            excerpt=trim_excerpt(text),
        )],
    )


def _auto_settings():
    """The [auto] permission settings, or None when config can't be read (the
    fetch then trusts no host and follows no cross-host redirect)."""
    try:
        from evi.config import Config

        return Config.load().auto
    except Exception:
        return None


def _same_site(a: str, b: str) -> bool:
    """True when two URLs share a host, ignoring a leading ``www.``."""
    ha = (urlparse(a).hostname or "").lower().removeprefix("www.")
    hb = (urlparse(b).hostname or "").lower().removeprefix("www.")
    return bool(ha) and ha == hb


def _policy_decision(auto, url: str) -> str:
    """'allow' / 'deny' / 'ask' for a web_fetch of `url` under the [auto]
    settings from the config file ('ask' when they can't be read)."""
    if auto is None:
        return "ask"
    from evi.permissions import decide

    try:
        return decide(
            getattr(auto, "mode", "ask"),
            getattr(auto, "auto_approve", None) or [],
            getattr(auto, "rules", None) or [],
            "web_fetch",
            "web",
            {"url": url},
            trusted_dirs=getattr(auto, "trusted_dirs", None) or [],
            trusted_domains=getattr(auto, "trusted_domains", None) or [],
            hard_deny=getattr(auto, "hard_deny", None) or [],
            protected_paths=getattr(auto, "protected_paths", None) or [],
        )
    except Exception:
        return "ask"


def _redirect_check(auto, first: str = "ask") -> Callable[[str, str], str | None]:
    """The permission re-check `netguard.fetch` runs before following each
    redirect; `first` is the policy's decision for the first URL.

    A hop the policy denies is never followed, and one it allows (trusted
    domain, allow rule, auto-approved category) or that stays on the same host
    always is. A hop to another host that would need approval depends on how
    the first URL got through:

    - it needed approval too ('ask': a prompt, /auto on, or a run with no one
      to ask approved the call with that URL in view), so the hop is followed,
      as a browser would; netguard still vets every address;
    - the policy let it through without a prompt (trusted_domains or an allow
      rule), so the hop is refused: that approval covers only what the settings
      name, and a trusted host's open redirect must not stretch it to any host.

    This reads the config file; the session's own state (/auto on, an SDK
    embedder's in-code config) is not visible to a tool yet. When the config
    can't be read (`auto` is None) no cross-host hop is followed, since its deny
    rules can't be checked.
    """
    follow_any = first == "ask" and auto is not None

    def check(src: str, dst: str) -> str | None:
        decision = _policy_decision(auto, dst)
        if decision == "deny":
            return f"{src} redirected to {dst}, which your permission settings deny."
        if decision == "allow" or _same_site(src, dst) or follow_any:
            return None
        why = (
            "the eVi config could not be read to check it" if auto is None else
            "the first URL was let through without a prompt by your permission "
            "settings ([auto] trusted_domains or an allow rule), and that covers "
            "only what they name"
        )
        return (
            f"{src} redirected to {dst}, on another host that is not pre-approved, so "
            f"it was not followed: {why}. Call web_fetch on that URL if you need it; "
            "that call goes through the session's permission check like any other."
        )

    return check


def _extract_text(html_bytes: bytes) -> str:
    """Pull human-readable text out of an HTML page.

    Prefers BeautifulSoup for clean output; falls back to a simple tag-strip
    regex when bs4 isn't installed (the agent still gets something usable).
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError:
        # Crude fallback — strip tags + collapse whitespace.
        text = re.sub(rb"<script.*?</script>", b"", html_bytes, flags=re.S | re.I)
        text = re.sub(rb"<style.*?</style>", b"", text, flags=re.S | re.I)
        text = re.sub(rb"<[^>]+>", b" ", text)
        decoded = text.decode("utf-8", errors="replace")
        return re.sub(r"\s+", " ", decoded).strip()

    soup = BeautifulSoup(html_bytes, "html.parser")
    for el in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        el.decompose()
    text = soup.get_text("\n", strip=True)
    # Collapse runs of blank lines but preserve paragraph breaks.
    return re.sub(r"\n{3,}", "\n\n", text)
