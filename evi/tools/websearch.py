"""Web search + fetch tools.

`web_search` queries DuckDuckGo (no API key) via the `duckduckgo_search`
library when present. `web_fetch` downloads a URL and extracts the main
readable text using `beautifulsoup4` if available; otherwise it returns a
naïvely stripped version.

Both deps are optional — installing `evi[web]` (note: distinct from the
web-server extras) gets you the full experience. Without them, the tools
return clear error strings rather than crashing the agent.
"""

from __future__ import annotations

import json
import re

import httpx

from evi.tools.base import tool


_USER_AGENT = "eVi/0.1 (+local personal assistant)"
_FETCH_MAX_BYTES = 1_000_000   # 1 MB cap on raw page download
_FETCH_MAX_TEXT = 16_000       # ~16 KB of extracted text to the LLM
_HTTP_TIMEOUT = 30.0


@tool(
    description=(
        "Search the web. Returns up to `limit` results as JSON: "
        "[{title, url, snippet}, …]. Backend is configurable ([tools] "
        "search_backend = ddg | searxng | ollama); DuckDuckGo (keyless) is the "
        "default. Use this for current events, public docs, or anything not in "
        "your training data."
    ),
    category="web",
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
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return (
            "ERROR: duckduckgo_search not installed — "
            "install with: pip install 'evi-assistant[web-tools]'"
        )
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
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
    long=True,
)
def web_fetch(url: str):
    from evi.citations import Citation, ToolOutput, trim_excerpt

    if not url.startswith(("http://", "https://")):
        return "ERROR: only http(s) URLs are allowed"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": _USER_AGENT})
            r.raise_for_status()
            raw = r.content[:_FETCH_MAX_BYTES]
    except Exception as exc:
        return f"ERROR: fetch failed: {type(exc).__name__}: {exc}"

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
